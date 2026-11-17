"""SQLite 저장소.

역할:
- device: MAC 명부와 표시 이름
- packet: CSV와 같은 수신 원본. 통계 계산은 하지 않는다
- sensor_config: 설정 적용 때 보낸 JSON
- measurement 조회와 최신화는 store_query.QueryMixin
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from pathlib import Path

from web_db.src.db_util import _float, _int, _iso, _rate, _samples_text
from web_db.src.grouping import assign_set
from web_db.src.store_query import QueryMixin

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "sensor.sqlite"


class Store(QueryMixin):
    """sensor.sqlite 하나를 프로세스 전체가 같이 쓰는 저장소.

    연결은 하나다(check_same_thread=False). MQTT 수신 스레드, HTTP 요청 스레드,
    하루 통계 스레드가 모두 이 연결을 쓰므로, 쿼리와 commit은 반드시 self.lock 안에서 한다.
    lock 없이 두 스레드가 쓰면 한쪽 commit이 다른 쪽의 반쯤 한 작업까지 확정할 수 있다.

    self.stats_lock은 통계 최신화(update_statistics) 전체를 한 번에 하나만 돌리는 잠금이다.
    최신화는 계산하는 동안 self.lock을 풀기 때문에, 이것이 없으면 두 최신화가 같은 위치를
    읽어 같은 세트를 두 번 넣을 수 있다. 자세한 순서는 store_query._update_statistics.

    self.lock은 재진입이 안 된다. lock을 잡은 메서드 안에서 lock을 잡는 공개 메서드
    (get_srss_limit 등)를 부르면 멈춘다. 그런 곳은 _read_srss_limit처럼 lock 없는 내부 함수를 쓴다.
    """
    def __init__(self, path=DB_PATH):
        """DB 파일을 열고 스키마를 맞춘다."""
        self.path = Path(path)
        self.lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self):
        """테이블과 인덱스가 없으면 만든다. 이미 있으면 그대로 둔다.

        테이블 역할은 docs/web_db-DB명세서.md에 있다.
        - device: MAC 명부, 표시 이름, 마지막 수신 정보
        - packet: 수신 원본. 지우지 않는다
        - measurement: 최신화 구간마다 한 줄인 통계
        - stat_cursor: MAC별 통계 읽기 위치
        - srss_limit, sensor_config, stat_meta: 한계값, 마지막 설정 JSON, 마지막 최신화 시각
        """
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS device (
                mac TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_gw TEXT NOT NULL DEFAULT '',
                last_battery TEXT NOT NULL DEFAULT '',
                last_sensor TEXT NOT NULL DEFAULT '',
                last_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS packet (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac TEXT NOT NULL,
                received_at TEXT NOT NULL,
                gw_mac TEXT NOT NULL DEFAULT '',
                sensor_type TEXT NOT NULL,
                axis TEXT NOT NULL,
                seq TEXT NOT NULL DEFAULT '',
                tick TEXT NOT NULL DEFAULT '',
                battery TEXT NOT NULL DEFAULT '',
                sample_rate REAL,
                sample_count INTEGER,
                payload_bytes INTEGER,
                sensor_data_hex TEXT NOT NULL,
                decoded_samples TEXT NOT NULL,
                min_khz REAL,
                interval_hz REAL,
                freq_start_khz REAL,
                freq_end_khz REAL,
                set_key TEXT NOT NULL,
                set_no INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_packet_mac_time ON packet(mac, received_at);
            CREATE INDEX IF NOT EXISTS idx_packet_set ON packet(set_key);
            CREATE TABLE IF NOT EXISTS measurement (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac TEXT NOT NULL,
                received_at TEXT NOT NULL,
                sensor_type TEXT NOT NULL,
                sample_rate REAL,
                sample_count INTEGER,
                rms REAL,
                peak REAL,
                srss REAL,
                srss_mms REAL,
                unit TEXT NOT NULL,
                g_per_lsb REAL,
                set_key TEXT NOT NULL UNIQUE,
                set_count INTEGER,
                first_at TEXT,
                last_at TEXT,
                computed_at TEXT,
                through_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_measurement_mac_time ON measurement(mac, received_at);
            CREATE TABLE IF NOT EXISTS srss_limit (
                mac TEXT PRIMARY KEY,
                limit_mm_s REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sensor_config (
                mac TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stat_cursor (
                mac TEXT PRIMARY KEY,
                through_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stat_meta (
                key TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL
            );
            DROP TABLE IF EXISTS daily_stat;
            """
        )
        self._conn.commit()

    def save_display_name(self, mac, name):
        """MAC의 표시 이름을 device에 저장한다."""
        mac = str(mac or "").strip()
        if not mac:
            raise ValueError("MAC이 필요합니다")
        now = _iso()
        label = str(name or "").strip()
        with self.lock:
            self._conn.execute(
                """
                INSERT INTO device(
                    mac, display_name, created_at, updated_at, last_gw, last_battery, last_sensor, last_at
                ) VALUES (?, ?, ?, ?, '', '', '', '')
                ON CONFLICT(mac) DO UPDATE SET
                    display_name=excluded.display_name,
                    updated_at=excluded.updated_at
                """,
                (mac, label, now, now),
            )
            self._conn.commit()
        return {"mac": mac, "display_name": label}

    def clear_display_name(self, mac):
        """표시 이름을 지운다. 수신 패킷이 없는 MAC은 목록에서도 뺀다."""
        mac = str(mac or "").strip()
        if not mac:
            raise ValueError("MAC이 필요합니다")
        with self.lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM packet WHERE mac=?",
                (mac,),
            ).fetchone()
            packets = int(row["n"] or 0)
            if packets:
                self._conn.execute(
                    "UPDATE device SET display_name='', updated_at=? WHERE mac=?",
                    (_iso(), mac),
                )
                self._conn.commit()
                return {"mac": mac, "display_name": "", "removed": False}
            self._conn.execute("DELETE FROM sensor_config WHERE mac=?", (mac,))
            self._conn.execute("DELETE FROM device WHERE mac=?", (mac,))
            self._conn.commit()
        self.delete_srss_limit(mac)
        return {"mac": mac, "display_name": "", "removed": True}

    def limit_key(self, mac):
        """SRSS 한계 키. 대문자이며 콜론과 하이픈은 뺀다."""
        return str(mac or "").strip().upper().replace(":", "").replace("-", "")

    def _limit_number(self, value):
        """0보다 큰 유한한 한계. 아니면 기본 4.5."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 4.5
        if not math.isfinite(number) or number <= 0:
            return 4.5
        return number

    def _read_srss_limit(self, mac):
        """lock을 가진 상태에서 srss_limit 한 줄을 읽는다. 없으면 4.5."""
        key = self.limit_key(mac)
        if not key:
            return 4.5
        row = self._conn.execute(
            "SELECT limit_mm_s FROM srss_limit WHERE mac=?",
            (key,),
        ).fetchone()
        if row is None:
            return 4.5
        return self._limit_number(row["limit_mm_s"])

    def get_srss_limit(self, mac):
        """장치 SRSS 한계(mm/s RMS). 테이블에 없으면 4.5."""
        with self.lock:
            return self._read_srss_limit(mac)

    def save_srss_limit(self, mac, value):
        """장치 SRSS 한계를 srss_limit 테이블에 저장한다."""
        key = self.limit_key(mac)
        number = float(value)
        if not key or not math.isfinite(number) or number <= 0:
            raise ValueError("장치 ID와 0보다 큰 유한한 한계값이 필요합니다")
        now = _iso()
        with self.lock:
            self._conn.execute(
                """
                INSERT INTO srss_limit(mac, limit_mm_s, updated_at) VALUES (?,?,?)
                ON CONFLICT(mac) DO UPDATE SET
                    limit_mm_s=excluded.limit_mm_s,
                    updated_at=excluded.updated_at
                """,
                (key, number, now),
            )
            self._conn.commit()
        return number

    def delete_srss_limit(self, mac):
        """장치 SRSS 한계 줄을 지운다. 없으면 그대로 둔다."""
        key = self.limit_key(mac)
        if not key:
            return
        with self.lock:
            self._conn.execute("DELETE FROM srss_limit WHERE mac=?", (key,))
            self._conn.commit()

    def save_sensor_config(self, mac, payload, action):
        """설정 적용으로 보낸 JSON을 그 MAC의 sensor_config에 남긴다."""
        mac = str(mac or "").strip()
        if not mac:
            return
        now = _iso()
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            self._conn.execute(
                """
                INSERT INTO device(mac, created_at, updated_at, last_gw, last_battery, last_sensor, last_at)
                VALUES (?, ?, ?, '', '', '', '')
                ON CONFLICT(mac) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (mac, now, now),
            )
            self._conn.execute(
                """
                INSERT INTO sensor_config(mac, action, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    action=excluded.action,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (mac, str(action or ""), text, now),
            )
            self._conn.commit()

    def device_profile(self, mac):
        """표시 이름과 마지막으로 저장한 설정 JSON을 돌려준다."""
        mac = str(mac or "").strip()
        with self.lock:
            device = self._conn.execute(
                "SELECT mac, display_name FROM device WHERE mac=?",
                (mac,),
            ).fetchone()
            config = self._conn.execute(
                "SELECT action, payload, updated_at FROM sensor_config WHERE mac=?",
                (mac,),
            ).fetchone()
        payload = None
        if config and config["payload"]:
            payload = json.loads(config["payload"])
        return {
            "mac": mac,
            "display_name": "" if device is None else device["display_name"],
            "action": "" if config is None else config["action"],
            "payload": payload,
            "updated_at": "" if config is None else config["updated_at"],
        }

    def close(self):
        """DB 연결을 닫는다."""
        self._conn.close()

    def save_packet(self, item):
        """수신 원본 한 줄을 packet에 넣고 새 id를 돌려준다. 통계는 계산하지 않는다.

        MonitorService._save_csv가 MQTT 수신 스레드에서 패킷마다 부른다.
        - 센서 종류: kind가 ULTRASONIC이면 초음파(축 H), ADXL-로 시작하면 ADXL, 나머지는 PIEZO.
        - sample_rate: 수신 JSON 값을 그대로 둔다(_rate). 장비 코드 `0x0F`도 글자로 남고,
          Hz로 읽는 것은 쓰는 쪽(stats_calc, 그래프)이 normalize_sample_rate로 한다.
        - decoded_samples: 디코드한 샘플을 쉼표 문자열로. 통계가 HEX를 다시 풀지 않게 한다.
        - set_key / set_no: assign_set이 직전 패킷과 seq를 비교해 정한다. 같은 lock 안에서
          '직전 패킷 조회 → INSERT'를 하므로, 두 패킷이 같은 직전 값을 보고 번호가 꼬이지 않는다.
        한 패킷마다 commit한다. 서버가 꺼져도 받은 패킷까지는 남는다.
        """
        mac = str(item.get("device") or "UNKNOWN").strip() or "UNKNOWN"
        kind = str(item.get("kind") or "")
        sensor_type = "ULTRASONIC" if kind == "ULTRASONIC" else ("ADXL" if kind.startswith("ADXL") else "PIEZO")
        axis = "H" if sensor_type == "ULTRASONIC" else str(item.get("axis") or "")
        seq = str(item.get("seq") or "")
        tick = str(item.get("tick") or "")
        received = _iso(item.get("received_at"))
        decoded = _samples_text(item.get("decoded"), kind)
        meta = item.get("meta") or {}
        with self.lock:
            key, set_no = assign_set(self._conn, mac, seq, sensor_type, axis)
            self._touch_device(mac, received, str(item.get("gw") or ""), str(item.get("battery") or ""), sensor_type)
            cursor = self._conn.execute(
                """
                INSERT INTO packet(
                    mac, received_at, gw_mac, sensor_type, axis, seq, tick,
                    battery, sample_rate, sample_count, payload_bytes, sensor_data_hex,
                    decoded_samples, min_khz, interval_hz, freq_start_khz, freq_end_khz,
                    set_key, set_no
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    mac,
                    received,
                    str(item.get("gw") or ""),
                    sensor_type,
                    axis,
                    seq,
                    tick,
                    str(item.get("battery") or ""),
                    _rate(item.get("sample_rate")),
                    _int(item.get("num_samples")),
                    _int(item.get("payload_bytes")),
                    str(item.get("sensor_data") or ""),
                    decoded,
                    _float(meta.get("minKhz")) if meta else None,
                    _float(meta.get("intervalHz")) if meta else None,
                    _float(meta.get("freq_start_khz")) if meta else None,
                    _float(meta.get("freq_end_khz")) if meta else None,
                    key,
                    set_no,
                ),
            )
            packet_id = int(cursor.lastrowid)
            self._conn.commit()
            return packet_id

    def _touch_device(self, mac, now, gw, battery, sensor_type):
        """패킷이 온 MAC을 device에 등록하거나 마지막 수신 정보를 갱신한다."""
        self._conn.execute(
            """
            INSERT INTO device(
                mac, created_at, updated_at, last_gw, last_battery, last_sensor, last_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                updated_at=excluded.updated_at,
                last_gw=excluded.last_gw,
                last_battery=excluded.last_battery,
                last_sensor=excluded.last_sensor,
                last_at=excluded.last_at
            """,
            (mac, now, now, gw, battery, sensor_type, now),
        )


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

from web_db.src.db_util import _float, _int, _iso, _samples_text
from web_db.src.grouping import assign_set, continues_set, seq_low
from web_db.src.store_query import QueryMixin

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "sensor.sqlite"


class Store(QueryMixin):
    def __init__(self, path=DB_PATH):
        """DB 파일을 열고 스키마를 맞춘다."""
        self.path = Path(path)
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self):
        """테이블이 없으면 만들고, 예전 session 스키마면 표를 비운 뒤 다시 만든다."""
        names = {row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        packet_cols = set()
        if "packet" in names:
            packet_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(packet)")}
        if "session" in names or "session_id" in packet_cols:
            self._conn.executescript(
                """
                DROP TABLE IF EXISTS measurement;
                DROP TABLE IF EXISTS daily_stat;
                DROP TABLE IF EXISTS packet;
                DROP TABLE IF EXISTS device;
                DROP TABLE IF EXISTS session;
                DROP TABLE IF EXISTS srss_limit;
                """
            )
            self._conn.commit()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS device (
                mac TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                asset_no TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
                unit TEXT NOT NULL,
                g_per_lsb REAL,
                set_key TEXT NOT NULL UNIQUE,
                packet_id INTEGER,
                packet_x_id INTEGER,
                packet_y_id INTEGER,
                packet_z_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_measurement_mac_time ON measurement(mac, received_at);
            CREATE TABLE IF NOT EXISTS daily_stat (
                mac TEXT NOT NULL,
                stat_date TEXT NOT NULL,
                srss_avg REAL,
                srss_count INTEGER NOT NULL DEFAULT 0,
                rms_avg REAL,
                peak_max REAL,
                unit TEXT NOT NULL DEFAULT '',
                computed_at TEXT NOT NULL,
                PRIMARY KEY (mac, stat_date)
            );
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
            CREATE TABLE IF NOT EXISTS stat_meta (
                key TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL
            );
            """
        )
        for name, ddl in (
            ("last_gw", "TEXT NOT NULL DEFAULT ''"),
            ("last_battery", "TEXT NOT NULL DEFAULT ''"),
            ("last_sensor", "TEXT NOT NULL DEFAULT ''"),
            ("last_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            self._add_column("device", name, ddl)
        self._add_column("packet", "set_no", "INTEGER")
        self._add_column("measurement", "srss_mms", "REAL")
        self._add_column("measurement", "set_count", "INTEGER")
        self._add_column("measurement", "first_at", "TEXT")
        self._add_column("measurement", "last_at", "TEXT")
        self._add_column("measurement", "computed_at", "TEXT")
        self._conn.commit()
        self._import_srss_limits()
        self._regroup_adxl()

    def _add_column(self, table, name, ddl):
        """이미 있는 표에 컬럼이 없을 때만 추가한다."""
        names = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if name not in names:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _regroup_adxl(self):
        """저장된 ADXL 패킷의 set_no만 seq 규칙으로 다시 맞춘다. 통계 줄은 만들지 않는다."""
        rows = self._conn.execute(
            """
            SELECT id, mac, axis, seq FROM packet
            WHERE sensor_type='ADXL' ORDER BY mac, id
            """
        ).fetchall()
        current = None
        last_axis = None
        last_low = None
        set_no = 0
        for row in rows:
            if row["mac"] != current:
                current = row["mac"]
                last_axis = None
                last_low = None
                set_no = 0
            low = seq_low(row["seq"])
            if not continues_set(last_axis, last_low, row["axis"], low):
                set_no += 1
            last_axis, last_low = row["axis"], low
            key = f"ADXL|{row['mac']}|{set_no}"
            self._conn.execute(
                "UPDATE packet SET set_key=?, set_no=? WHERE id=?",
                (key, set_no, row["id"]),
            )
        self._conn.execute(
            """
            UPDATE device SET
                last_at=COALESCE((SELECT MAX(received_at) FROM packet p WHERE p.mac=device.mac), last_at),
                last_gw=COALESCE((SELECT gw_mac FROM packet p WHERE p.mac=device.mac ORDER BY id DESC LIMIT 1), last_gw),
                last_battery=COALESCE((SELECT battery FROM packet p WHERE p.mac=device.mac ORDER BY id DESC LIMIT 1), last_battery),
                last_sensor=COALESCE((SELECT sensor_type FROM packet p WHERE p.mac=device.mac ORDER BY id DESC LIMIT 1), last_sensor)
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

    def _import_srss_limits(self):
        """예전에 홈 JSON에 있던 한계를 테이블에 한 번 옮긴다. 이미 있는 키는 유지한다."""
        path = Path.home() / ".sensor-program" / "srss_limits.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        now = _iso()
        for raw, value in data.items():
            key = self.limit_key(raw)
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not key or not math.isfinite(number) or number <= 0:
                continue
            self._conn.execute(
                """
                INSERT OR IGNORE INTO srss_limit(mac, limit_mm_s, updated_at)
                VALUES (?,?,?)
                """,
                (key, number, now),
            )
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
        """수신 원본 한 줄을 packet에 넣는다. 통계는 계산하지 않는다."""
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
                    _float(item.get("sample_rate")),
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


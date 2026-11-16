"""SQLite 저장소.

역할:
- device: MAC 명부와 표시 이름
- packet: CSV와 같은 수신 원본. 통계 계산은 하지 않는다
- sensor_config: 설정 적용 때 보낸 JSON
- measurement 조회와 최신화 시각. 숫자 계산은 stats_calc, 묶음은 grouping
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

from web import core as gen
from web_db.grouping import assign_set, continues_set, seq_low
from web_db.stats_calc import adxl_set_stats

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "sensor.sqlite"


def _iso(value=None):
    """시각 문자. 값이 없으면 지금 시각."""
    if value:
        return str(value)
    return datetime.now().isoformat(timespec="milliseconds")


def _samples_text(values, kind):
    """샘플 배열을 packet.decoded_samples 칸에 넣을 문자열로 만든다."""
    data = np.asarray(values, dtype=np.float64).reshape(-1)
    if kind == "ULTRASONIC":
        return ",".join(format(float(item), ".9g") for item in data)
    return ",".join(str(int(item)) for item in data)


class Store:
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
        self._conn.commit()
        self._regroup_adxl()

    def _add_column(self, table, name, ddl):
        """이미 있는 표에 컬럼이 없을 때만 추가한다."""
        names = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if name not in names:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _regroup_adxl(self):
        """저장된 ADXL을 CSV와 같은 seq 규칙으로 다시 묶고 measurement를 다시 만든다."""
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
        self._conn.execute("DELETE FROM measurement WHERE sensor_type='ADXL'")
        keys = self._conn.execute(
            "SELECT DISTINCT set_key, mac FROM packet WHERE sensor_type='ADXL'"
        ).fetchall()
        for item in keys:
            found_rows = self._conn.execute(
                """
                SELECT id, axis, decoded_samples, sample_rate, sample_count, received_at
                FROM packet WHERE set_key=? ORDER BY id
                """,
                (item["set_key"],),
            ).fetchall()
            found = {row["axis"]: row for row in found_rows}
            if any(axis not in found for axis in ("X", "Y", "Z")):
                continue
            try:
                stats = adxl_set_stats(found)
            except (ValueError, TypeError, OverflowError):
                continue
            self._conn.execute(
                """
                INSERT INTO measurement(
                    mac, received_at, sensor_type, sample_rate, sample_count, rms, peak, srss, srss_mms,
                    unit, g_per_lsb, set_key, packet_x_id, packet_y_id, packet_z_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item["mac"], found["Z"]["received_at"], "ADXL", stats["sample_rate"],
                    stats["sample_count"], stats["rms"], stats["peak"], stats["srss"], stats["srss_mms"],
                    "g RMS", stats["g_per_lsb"], item["set_key"],
                    found["X"]["id"], found["Y"]["id"], found["Z"]["id"],
                ),
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

    def devices(self):
        """MAC 목록과 패킷 수를 돌려준다."""
        with self.lock:
            rows = self._conn.execute(
                """
                SELECT d.mac, d.display_name, d.location, d.asset_no,
                       COUNT(p.id) AS packets,
                       MAX(p.received_at) AS last_at
                FROM device d
                LEFT JOIN packet p ON p.mac=d.mac
                GROUP BY d.mac
                ORDER BY last_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self, mac):
        """한 MAC의 저장된 통계 평균과 최대값을 돌려준다."""
        with self.lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS measurements,
                       AVG(srss) AS srss_avg,
                       SUM(srss IS NOT NULL) AS srss_count,
                       AVG(rms) AS rms_avg,
                       MAX(peak) AS peak_max,
                       MAX(unit) AS unit
                FROM measurement
                WHERE mac=?
                """,
                (mac,),
            ).fetchone()
        data = dict(row) if row else {}
        data["mac"] = mac
        data["measurements"] = int(data.get("measurements") or 0)
        return data

    def dashboard(self):
        """MAC별 최신 SRSS/RMS/PEAK. 화면은 저장된 숫자만 읽는다."""
        with self.lock:
            rows = self._conn.execute(
                """
                SELECT d.mac, d.display_name,
                       m.srss AS srss, m.srss_mms AS srss_mms, m.rms AS rms, m.peak AS peak,
                       m.received_at AS received_at,
                       a.srss_avg AS srss_avg, a.rms_avg AS rms_avg, a.peak_max AS peak_max,
                       a.n AS measurements
                FROM device d
                LEFT JOIN measurement m ON m.id = (
                    SELECT id FROM measurement
                    WHERE mac=d.mac AND sensor_type='ADXL' AND srss IS NOT NULL
                    ORDER BY id DESC LIMIT 1
                )
                LEFT JOIN (
                    SELECT mac, AVG(srss_mms) AS srss_avg, AVG(rms) AS rms_avg,
                           MAX(peak) AS peak_max, COUNT(*) AS n
                    FROM measurement
                    WHERE sensor_type='ADXL' AND srss IS NOT NULL
                    GROUP BY mac
                ) a ON a.mac=d.mac
                ORDER BY d.created_at, d.mac
                """
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["limit"] = gen.srss_limit_get(item["mac"])
                item["measurements"] = int(item["measurements"] or 0)
                history = self._conn.execute(
                    """
                    SELECT srss_mms, rms, peak, received_at FROM measurement
                    WHERE mac=? AND sensor_type='ADXL' AND srss IS NOT NULL
                    ORDER BY id DESC LIMIT 40
                    """,
                    (item["mac"],),
                ).fetchall()
                item["series"] = [
                    {
                        "t": point["received_at"],
                        "srss_mms": point["srss_mms"],
                        "rms": point["rms"],
                        "peak": point["peak"],
                    }
                    for point in reversed(history)
                ]
                items.append(item)
        return {"updated_at": self.stats_updated_at(), "items": items}

    def stats_updated_at(self):
        """통계를 마지막으로 만든 시각. 기록이 없으면 마지막 measurement 시각."""
        with self.lock:
            row = self._conn.execute(
                "SELECT updated_at FROM stat_meta WHERE key='measurement'"
            ).fetchone()
            if row is not None:
                return row["updated_at"]
            fallback = self._conn.execute(
                "SELECT MAX(received_at) AS updated_at FROM measurement WHERE sensor_type='ADXL'"
            ).fetchone()
        return None if fallback is None else fallback["updated_at"]

    def update_statistics(self):
        """새 XYZ 세트만 SRSS, RMS, PEAK로 만든다. 이미 있는 측정은 다시 계산하지 않는다."""
        with self.lock:
            pending = self._conn.execute(
                """
                SELECT set_key, mac, MAX(received_at) AS received_at
                FROM packet
                WHERE sensor_type='ADXL'
                GROUP BY set_key
                HAVING SUM(axis='X')>0 AND SUM(axis='Y')>0 AND SUM(axis='Z')>0
                   AND set_key NOT IN (SELECT set_key FROM measurement)
                """
            ).fetchall()
        added = 0
        for item in pending:
            with self.lock:
                found_rows = self._conn.execute(
                    """
                    SELECT id, axis, decoded_samples, sample_rate, sample_count
                    FROM packet WHERE set_key=? ORDER BY id
                    """,
                    (item["set_key"],),
                ).fetchall()
            found = {row["axis"]: row for row in found_rows}
            if any(axis not in found for axis in ("X", "Y", "Z")):
                continue
            try:
                stats = adxl_set_stats(found)
            except (ValueError, TypeError, OverflowError):
                continue
            with self.lock:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO measurement(
                        mac, received_at, sensor_type, sample_rate, sample_count, rms, peak, srss, srss_mms,
                        unit, g_per_lsb, set_key, packet_x_id, packet_y_id, packet_z_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item["mac"], item["received_at"], "ADXL", stats["sample_rate"],
                        stats["sample_count"], stats["rms"], stats["peak"], stats["srss"],
                        stats["srss_mms"], "g RMS", stats["g_per_lsb"], item["set_key"],
                        found["X"]["id"], found["Y"]["id"], found["Z"]["id"],
                    ),
                )
                self._conn.commit()
            added += 1
        now = _iso()
        with self.lock:
            self._conn.execute(
                """
                INSERT INTO stat_meta(key, updated_at) VALUES ('measurement', ?)
                ON CONFLICT(key) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (now,),
            )
            self._conn.commit()
        return {"added": added, "updated_at": now}

    def rebuild_daily(self):
        """measurement를 날짜별 평균으로 daily_stat에 다시 쓴다."""
        now = _iso()
        with self.lock:
            self._conn.execute("DELETE FROM daily_stat")
            self._conn.execute(
                """
                INSERT INTO daily_stat(mac, stat_date, srss_avg, srss_count, rms_avg, peak_max, unit, computed_at)
                SELECT mac, substr(received_at, 1, 10), AVG(srss), SUM(srss IS NOT NULL),
                       AVG(rms), MAX(peak), MAX(unit), ?
                FROM measurement
                GROUP BY mac, substr(received_at, 1, 10)
                """,
                (now,),
            )
            self._conn.commit()
            count = self._conn.execute("SELECT COUNT(*) AS n FROM daily_stat").fetchone()["n"]
        return int(count)

    def packets_for(self, mac, sensor_type, mode="recent", limit=50, start="", end=""):
        """분석 화면 조회. ADXL은 X/Y/Z가 갖춘 set_no 단위로 최근 N개 또는 기간을 고른다."""
        limit = max(1, min(int(limit or 50), 200))
        with self.lock:
            if sensor_type == "ADXL":
                sql = """
                    SELECT set_key FROM packet
                    WHERE mac=? AND sensor_type='ADXL'
                    GROUP BY set_key
                    HAVING SUM(axis='X')>0 AND SUM(axis='Y')>0 AND SUM(axis='Z')>0
                """
                params = [mac]
                if mode == "range":
                    sql += " AND MAX(received_at)>=? AND MAX(received_at)<=?"
                    params.extend([start, end])
                sql += " ORDER BY MAX(received_at) DESC LIMIT ?"
                params.append(limit)
                keys = [row["set_key"] for row in self._conn.execute(sql, params).fetchall()]
                if not keys:
                    return []
                marks = ",".join("?" for _key in keys)
                rows = self._conn.execute(
                    f"""
                    SELECT * FROM packet
                    WHERE set_key IN ({marks})
                    ORDER BY received_at, id
                    """,
                    keys,
                ).fetchall()
                return [dict(row) for row in rows]
            sql = "SELECT * FROM packet WHERE mac=? AND sensor_type=?"
            params = [mac, sensor_type]
            if mode == "range":
                sql += " AND received_at>=? AND received_at<=?"
                params.extend([start, end])
            sql += " ORDER BY received_at DESC, id DESC LIMIT ?"
            params.append(limit)
            rows = list(self._conn.execute(sql, params).fetchall())
        rows.reverse()
        return [dict(row) for row in rows]


def _float(value):
    """숫자가 아니면 None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _int(value):
    """정수가 아니면 None."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

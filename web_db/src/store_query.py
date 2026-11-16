"""조회와 통계 최신화. 수신 콜백에서는 통계를 계산하지 않는다."""

from __future__ import annotations

from web_db.src import core as gen
from web_db.src.db_util import _iso
from web_db.src.stats_calc import adxl_set_stats


class QueryMixin:
    """MAC 목록, 통계 조회, 측정 최신화, 분석용 패킷 조회."""

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

"""조회와 통계 최신화. 수신 콜백에서는 통계를 계산하지 않는다."""

from __future__ import annotations

from web_db.src.db_util import _iso
from web_db.src.grouping import complete_sets
from web_db.src.stats_calc import adxl_set_stats, summarize_mac


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
        items = [dict(row) for row in rows]
        for item in items:
            item["limit"] = self.get_srss_limit(item["mac"])
        return items

    def stats(self, mac):
        """한 MAC의 measurement 한 줄을 돌려준다. 없으면 측정 수 0이다."""
        with self.lock:
            row = self._conn.execute(
                """
                SELECT set_count, rms, peak, srss, srss_mms, unit, first_at, last_at
                FROM measurement
                WHERE mac=? AND sensor_type='ADXL' AND set_count IS NOT NULL
                ORDER BY id DESC LIMIT 1
                """,
                (mac,),
            ).fetchone()
        if row is None:
            return {"mac": mac, "measurements": 0, "srss_avg": None, "rms_avg": None, "peak_max": None, "unit": ""}
        return {
            "mac": mac,
            "measurements": int(row["set_count"] or 0),
            "srss_avg": row["srss"],
            "srss_mms": row["srss_mms"],
            "rms_avg": row["rms"],
            "peak_max": row["peak"],
            "unit": row["unit"] or "",
            "first_at": row["first_at"],
            "last_at": row["last_at"],
        }

    def dashboard(self):
        """MAC별 measurement 한 줄. 화면은 저장된 숫자만 읽는다."""
        with self.lock:
            rows = self._conn.execute(
                """
                SELECT d.mac, d.display_name,
                       m.srss AS srss, m.srss_mms AS srss_mms, m.rms AS rms, m.peak AS peak,
                       m.set_count AS set_count, m.first_at AS first_at, m.last_at AS last_at,
                       m.computed_at AS computed_at
                FROM device d
                LEFT JOIN measurement m ON m.id = (
                    SELECT id FROM measurement
                    WHERE mac=d.mac AND sensor_type='ADXL' AND set_count IS NOT NULL
                    ORDER BY id DESC LIMIT 1
                )
                ORDER BY d.created_at, d.mac
                """
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["limit"] = self._read_srss_limit(item["mac"])
                item["set_count"] = int(item["set_count"] or 0)
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
        """MAC마다 packet의 완성 XYZ를 읽어 measurement 한 줄로 바꾼다."""
        with self.lock:
            macs = [
                row["mac"]
                for row in self._conn.execute(
                    "SELECT DISTINCT mac FROM packet WHERE sensor_type='ADXL'"
                ).fetchall()
            ]
        written = 0
        for mac in macs:
            with self.lock:
                rows = [
                    dict(row)
                    for row in self._conn.execute(
                        """
                        SELECT id, axis, seq, decoded_samples, sample_rate, sample_count, received_at
                        FROM packet
                        WHERE mac=? AND sensor_type='ADXL'
                        ORDER BY id
                        """,
                        (mac,),
                    ).fetchall()
                ]
            collected = []
            times = []
            for found in complete_sets(rows):
                try:
                    collected.append(adxl_set_stats(found))
                except (ValueError, TypeError, OverflowError):
                    continue
                times.append(max(found[axis]["received_at"] for axis in ("X", "Y", "Z")))
            now = _iso()
            with self.lock:
                self._conn.execute(
                    "DELETE FROM measurement WHERE mac=? AND sensor_type='ADXL'",
                    (mac,),
                )
                if collected:
                    summary = summarize_mac(collected)
                    self._conn.execute(
                        """
                        INSERT INTO measurement(
                            mac, received_at, sensor_type, sample_rate, sample_count,
                            rms, peak, srss, srss_mms, unit, g_per_lsb, set_key,
                            set_count, first_at, last_at, computed_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            mac,
                            max(times),
                            "ADXL",
                            summary["sample_rate"],
                            summary["sample_count"],
                            summary["rms"],
                            summary["peak"],
                            summary["srss"],
                            summary["srss_mms"],
                            "g RMS",
                            summary["g_per_lsb"],
                            f"ADXL|{mac}|all",
                            summary["set_count"],
                            min(times),
                            max(times),
                            now,
                        ),
                    )
                    written += 1
                self._conn.commit()
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
        return {"macs": written, "updated_at": now}

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

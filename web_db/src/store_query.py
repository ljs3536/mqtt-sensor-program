"""조회와 통계 최신화. 수신 콜백에서는 통계를 계산하지 않는다."""

from __future__ import annotations

from web_db.src.db_util import _iso
from web_db.src.grouping import complete_sets, settled_through
from web_db.src.stats_calc import adxl_set_stats, summarize_mac


class QueryMixin:
    """MAC 목록, 통계 조회, 측정 최신화, 분석용 패킷 조회."""

    def devices(self):
        """MAC 목록과 패킷 수를 돌려준다."""
        with self.lock:
            rows = self._conn.execute(
                """
                SELECT d.mac, d.display_name,
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
        """한 MAC의 전체 통계. 구간 줄을 세트 수로 가중해 평균한다."""
        with self.lock:
            row = self._conn.execute(
                """
                SELECT SUM(set_count) AS measurements,
                       SUM(rms * set_count) / SUM(set_count) AS rms_avg,
                       MAX(peak) AS peak_max,
                       SUM(srss * set_count) / SUM(set_count) AS srss_avg,
                       SUM(srss_mms * set_count) / SUM(set_count) AS srss_mms,
                       MAX(unit) AS unit,
                       MIN(first_at) AS first_at,
                       MAX(last_at) AS last_at
                FROM measurement
                WHERE mac=? AND sensor_type='ADXL'
                """,
                (mac,),
            ).fetchone()
        if row is None or not row["measurements"]:
            return {"mac": mac, "measurements": 0, "srss_avg": None, "rms_avg": None, "peak_max": None, "unit": ""}
        return {
            "mac": mac,
            "measurements": int(row["measurements"] or 0),
            "srss_avg": row["srss_avg"],
            "srss_mms": row["srss_mms"],
            "rms_avg": row["rms_avg"],
            "peak_max": row["peak_max"],
            "unit": row["unit"] or "",
            "first_at": row["first_at"],
            "last_at": row["last_at"],
        }

    def dashboard(self):
        """MAC별 전체 평균과 구간별 변화. 화면은 저장된 숫자만 읽는다."""
        with self.lock:
            rows = self._conn.execute(
                """
                SELECT d.mac, d.display_name,
                       SUM(m.set_count) AS set_count,
                       SUM(m.rms * m.set_count) / SUM(m.set_count) AS rms,
                       MAX(m.peak) AS peak,
                       SUM(m.srss * m.set_count) / SUM(m.set_count) AS srss,
                       SUM(m.srss_mms * m.set_count) / SUM(m.set_count) AS srss_mms,
                       MIN(m.first_at) AS first_at,
                       MAX(m.last_at) AS last_at
                FROM device d
                LEFT JOIN measurement m
                  ON m.mac=d.mac AND m.sensor_type='ADXL'
                GROUP BY d.mac
                ORDER BY d.created_at, d.mac
                """
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["limit"] = self._read_srss_limit(item["mac"])
                item["set_count"] = int(item["set_count"] or 0)
                history = self._conn.execute(
                    """
                    SELECT srss_mms, rms, peak, last_at, set_count
                    FROM measurement
                    WHERE mac=? AND sensor_type='ADXL'
                    ORDER BY id DESC LIMIT 40
                    """,
                    (item["mac"],),
                ).fetchall()
                item["series"] = [
                    {
                        "t": point["last_at"],
                        "srss_mms": point["srss_mms"],
                        "rms": point["rms"],
                        "peak": point["peak"],
                        "set_count": point["set_count"],
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
        """마지막 통계 이후의 완성 XYZ만 모아 measurement에 구간 한 줄을 추가한다.

        스케줄러와 최신화 버튼이 겹치면 뒤에 온 쪽은 앞 최신화가 끝날 때까지 기다린다.
        계산은 self.lock 밖에서 하므로, 이 잠금이 없으면 둘이 같은 through_id를 읽는다.
        """
        with self.stats_lock:
            return self._update_statistics()

    def _update_statistics(self):
        """update_statistics 본문. stats_lock을 가진 상태에서만 부른다.

        MAC마다 다음 순서로 돈다. 패킷 수신은 그동안에도 계속된다.
        1. (self.lock) 읽기 위치 = stat_cursor와 measurement.through_id 중 큰 값.
           그보다 id가 큰 ADXL 패킷만 읽는다. 이 순간까지 commit된 패킷이 이번 대상이다.
        2. (lock 없음) complete_sets로 X/Y/Z를 묶고 세트마다 RMS·PEAK·SRSS를 계산한다.
           오래 걸리는 부분이라 lock을 풀어, 이 사이에 들어온 패킷이 저장을 기다리지 않게 한다.
           이 사이에 들어온 패킷은 1에서 읽지 않았으므로 이번 계산에는 없다.
        3. settled_through로 '다 본 마지막 id'를 정한다. 실패한 세트도 다 본 것이다.
           맨 끝에 Z가 아직 없는 묶음은 남겨 두어, 다음 최신화가 뒤 축과 함께 다시 읽는다.
        4. (self.lock) 계산된 세트가 있으면 measurement에 구간 한 줄을 넣고,
           stat_cursor를 3의 위치로 옮긴다. 같은 commit이라 둘이 어긋나지 않는다.
        마지막에 stat_meta 시각을 갱신한다. 하루 스케줄러는 이 날짜로 오늘 돌았는지 본다.
        """
        with self.lock:
            macs = [
                row["mac"]
                for row in self._conn.execute(
                    "SELECT DISTINCT mac FROM packet WHERE sensor_type='ADXL'"
                ).fetchall()
            ]
        written = 0
        failed = 0
        for mac in macs:
            with self.lock:
                through = self._stat_through(mac)
                rows = [
                    dict(row)
                    for row in self._conn.execute(
                        """
                        SELECT id, axis, seq, decoded_samples, sample_rate, sample_count, received_at
                        FROM packet
                        WHERE mac=? AND sensor_type='ADXL' AND id>?
                        ORDER BY id
                        """,
                        (mac, through),
                    ).fetchall()
                ]
            collected = []
            times = []
            included = []
            for found in complete_sets(rows):
                try:
                    collected.append(adxl_set_stats(found))
                except (ValueError, TypeError, OverflowError):
                    failed += 1
                    continue
                times.append(max(found[axis]["received_at"] for axis in ("X", "Y", "Z")))
                included.extend(int(found[axis]["id"]) for axis in ("X", "Y", "Z"))
            # 계산에 실패한 세트와 축이 빠진 묶음도 여기까지 본 것으로 친다. 다음에는 다시 읽지 않는다.
            settled = settled_through(rows, through)
            if not collected:
                if settled > through:
                    with self.lock:
                        self._save_stat_through(mac, settled)
                        self._conn.commit()
                continue
            summary = summarize_mac(collected)
            now = _iso()
            with self.lock:
                self._save_stat_through(mac, settled)
                self._conn.execute(
                    """
                    INSERT INTO measurement(
                        mac, received_at, sensor_type, sample_rate, sample_count,
                        rms, peak, srss, srss_mms, unit, g_per_lsb, set_key,
                        set_count, first_at, last_at, computed_at, through_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                        f"ADXL|{mac}|{max(included)}",
                        summary["set_count"],
                        min(times),
                        max(times),
                        now,
                        settled,
                    ),
                )
                self._conn.commit()
            written += 1
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
        return {"macs": written, "failed_sets": failed, "updated_at": now}

    def _stat_through(self, mac):
        """lock을 가진 상태에서, 그 MAC이 통계에서 이미 본 마지막 packet id를 읽는다."""
        cursor = self._conn.execute(
            "SELECT through_id FROM stat_cursor WHERE mac=?",
            (mac,),
        ).fetchone()
        marker = self._conn.execute(
            """
            SELECT COALESCE(MAX(through_id), 0) AS through_id
            FROM measurement
            WHERE mac=? AND sensor_type='ADXL'
            """,
            (mac,),
        ).fetchone()
        saved = 0 if cursor is None else int(cursor["through_id"] or 0)
        return max(saved, int(marker["through_id"] or 0))

    def _save_stat_through(self, mac, through_id):
        """lock을 가진 상태에서 그 MAC의 통계 읽기 위치를 남긴다. commit은 부르는 쪽이 한다."""
        self._conn.execute(
            """
            INSERT INTO stat_cursor(mac, through_id, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                through_id=MAX(stat_cursor.through_id, excluded.through_id),
                updated_at=excluded.updated_at
            """,
            (mac, int(through_id), _iso()),
        )

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

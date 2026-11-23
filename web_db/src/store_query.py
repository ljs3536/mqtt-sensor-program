"""조회와 통계 최신화. 수신 콜백에서는 통계를 계산하지 않는다.

통계(measurement)는 2026-11-23부터 'MAC마다 한 시간 한 줄'이고, 계산 기준은 DB stat_profile 버전이다.
- 조회(stats, dashboard, store_site.site_rows)는 지금 버전(app_setting 'stat_profile_active') 줄만 본다.
  지금 버전이 아직 없으면(업그레이드 직후, 재계산 전) 옛 하루 통계 줄(profile_id 없음)을 본다.
- 최신화(update_statistics)는 매시간 스케줄러가, 재계산(rebuild_statistics)은 기준 저장·관리 버튼이 부른다.
  둘 다 SQLite를 오래 잡지 않는다: 300줄씩 짧게 읽고(self.lock), 잠금 없이 계산하고, 짧게 쓴다.
  계산은 세트당 약 5ms라 수신 저장이 기다리는 시간은 읽기·쓰기 순간뿐이다.
"""

from __future__ import annotations

import json
import time

from web_db.src import stat_profile as sp
from web_db.src.db_util import _iso
from web_db.src.grouping import complete_sets, packet_to_template, settled_through

# 한 번에 읽는 packet 줄 수(ADXL 약 100세트, HEX 포함 약 10MB). 이만큼 계산하고 쓴 뒤 다음을 읽는다.
STAT_CHUNK = 300
ACTIVE_KEY = "stat_profile_active"


class QueryMixin:
    """MAC 목록, 통계 조회, 측정 최신화, 분석용 패킷 조회."""

    def display_names(self):
        """이름이 있는 MAC만 {MAC: 표시 이름}으로 돌려준다.

        수신 화면이 상태를 읽을 때마다(연결 중 1초) 부른다. device 표는 MAC당 한 줄이라 가볍다.
        """
        with self.lock:
            rows = self._conn.execute(
                "SELECT mac, display_name FROM device WHERE display_name<>''"
            ).fetchall()
        return {row["mac"]: row["display_name"] for row in rows}

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

    # ---------- 통계 계산 기준(stat_profile) ----------
    def _active_profile_id(self):
        """lock을 가진 상태에서 지금 화면·경보가 보는 기준 버전 id. 없으면 0(옛 하루 통계)."""
        row = self._conn.execute("SELECT value FROM app_setting WHERE key=?", (ACTIVE_KEY,)).fetchone()
        try:
            return int(json.loads(row["value"]).get("id") or 0) if row else 0
        except (ValueError, TypeError, AttributeError):
            return 0

    def ensure_stat_profile(self):
        """기준 버전이 하나도 없으면 첫 버전(stat_profile.default_settings)을 만든다. 가장 최근 버전 id."""
        with self.lock:
            row = self._conn.execute("SELECT MAX(id) AS id FROM stat_profile").fetchone()
            if row["id"]:
                return int(row["id"])
        return self.add_stat_profile(sp.default_settings(), "첫 버전: 그래프 설정 파일(g/LSB·MEMS 보정) + HPF 10Hz·LPF 1000Hz")

    def add_stat_profile(self, settings, note=""):
        """새 기준 버전을 저장하고 id를 돌려준다. 지금 버전으로 바꾸는 것은 재계산이 끝날 때다."""
        settings = sp.validate(settings)
        with self.lock:
            cursor = self._conn.execute(
                "INSERT INTO stat_profile(settings, note, created_at) VALUES (?, ?, ?)",
                (sp.to_json(settings), str(note or "").strip()[:200], _iso()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def stat_profile(self, profile_id):
        """버전 하나 {id, settings, note, created_at, summary}. 없으면 None."""
        with self.lock:
            row = self._conn.execute("SELECT * FROM stat_profile WHERE id=?", (int(profile_id),)).fetchone()
        if row is None:
            return None
        settings = sp.validate(json.loads(row["settings"]))
        return {"id": int(row["id"]), "settings": settings, "note": row["note"], "created_at": row["created_at"],
                "summary": sp.summary_text(settings)}

    def stat_profiles(self):
        """설정 화면용: 지금 버전 id, 가장 최근 버전 id, 버전 목록(최근 50개, 최신 먼저)."""
        with self.lock:
            active = self._active_profile_id()
            rows = self._conn.execute("SELECT * FROM stat_profile ORDER BY id DESC LIMIT 50").fetchall()
        items = []
        for row in rows:
            try:
                settings = sp.validate(json.loads(row["settings"]))
                summary = sp.summary_text(settings)
            except (ValueError, TypeError):
                settings, summary = None, "(읽을 수 없는 기준)"
            items.append({"id": int(row["id"]), "note": row["note"], "created_at": row["created_at"],
                          "settings": settings, "summary": summary, "active": int(row["id"]) == active})
        return {"active": active, "latest": items[0]["id"] if items else 0, "items": items}

    def stats(self, mac):
        """한 MAC의 전체 통계(지금 기준 버전). 시간 줄을 세트 수로 가중해 평균한다."""
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
                WHERE mac=? AND sensor_type='ADXL' AND COALESCE(profile_id, 0)=?
                """,
                (mac, self._active_profile_id()),
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
        """MAC별 전체 평균과 시간별 변화(지금 기준 버전). 화면은 저장된 숫자만 읽는다.

        series는 최근 168줄(한 시간 한 줄이면 7일). 옛 하루 통계(버전 0)면 하루 한 줄이다.
        """
        with self.lock:
            active = self._active_profile_id()
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
                  ON m.mac=d.mac AND m.sensor_type='ADXL' AND COALESCE(m.profile_id, 0)=?
                GROUP BY d.mac
                ORDER BY d.created_at, d.mac
                """,
                (active,),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["limit"] = self._read_srss_limit(item["mac"])
                item["set_count"] = int(item["set_count"] or 0)
                # 최근 줄 = 가장 늦은 시간 줄(센서 현황 경보와 같은 기준: last_at 순).
                history = self._conn.execute(
                    """
                    SELECT srss_mms, rms, peak, last_at, set_count, hour, x_mms, y_mms, z_mms, srss_mms_max
                    FROM measurement
                    WHERE mac=? AND sensor_type='ADXL' AND COALESCE(profile_id, 0)=?
                    ORDER BY last_at DESC, id DESC LIMIT 168
                    """,
                    (item["mac"], active),
                ).fetchall()
                # 가장 최근 줄(대문 경보 판정과 같은 값). 통계 화면이 '최근'으로 함께 보여 준다.
                item["latest_srss_mms"] = history[0]["srss_mms"] if history else None
                item["latest_at"] = history[0]["last_at"] if history else None
                item["series"] = [
                    {
                        # 시간 줄은 묶음 시(hour), 옛 하루 통계는 마지막 세트 시각.
                        "t": point["hour"] or point["last_at"],
                        "srss_mms": point["srss_mms"],
                        "srss_mms_max": point["srss_mms_max"],
                        "x_mms": point["x_mms"],
                        "y_mms": point["y_mms"],
                        "z_mms": point["z_mms"],
                        "rms": point["rms"],
                        "peak": point["peak"],
                        "set_count": point["set_count"],
                    }
                    for point in reversed(history)
                ]
                items.append(item)
        profile = self.stat_profile(active) if active else None
        return {
            "updated_at": self.stats_updated_at(),
            "items": items,
            # 통계 화면 안내 줄: 어떤 기준으로 계산한 값인지.
            "profile": {"id": active, "summary": profile["summary"] if profile else "옛 하루 통계(필터·보정 없음)",
                        "created_at": profile["created_at"] if profile else ""},
        }

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
        """매시간 스케줄러(와 명령줄 import_csv)가 부른다. 지금 기준 버전으로 새 세트만 더한다.

        지금 버전이 아직 없으면(업그레이드 직후 재계산 전) 아무것도 하지 않는다. 재계산이 끝나면 버전이 정해진다.
        재계산·CSV 통계와 겹치지 않게 stats_lock을 잡는다(뒤에 온 쪽은 기다린다).
        """
        with self.stats_lock:
            with self.lock:
                active = self._active_profile_id()
            if not active:
                return {"macs": 0, "sets": 0, "failed_sets": 0, "skipped": "통계 기준 버전이 아직 없습니다(재계산 대기)"}
            profile = self.stat_profile(active)
            result = self._compute_statistics(active, profile["settings"], cursors=None)
            result["updated_at"] = self._touch_stat_meta()
            return result

    def rebuild_statistics(self, profile_id, progress=None):
        """packet 전체를 기준 버전 profile_id로 다시 계산하고, 끝나면 그 버전을 '지금 버전'으로 바꾼다.

        부르는 쪽(stats_job.StatRebuildJob)이 stats_lock을 잡고 있다(매시간 최신화·CSV 통계와 겹치지 않음).
        1. 새 버전 줄은 계산되는 대로 measurement에 쓰지만, 화면·경보는 지금 버전만 보므로 끝날 때까지 안 보인다.
           그 사이에도 화면에는 이전 통계가 그대로 보인다.
        2. 다 끝나면 한 commit에서: 지금 버전 = profile_id, 다른 버전 줄 삭제, 통계 읽기 위치 = 이번에 다 본 위치.
        중간에 실패하면 이미 쓴 새 버전 줄만 지우고 지금 버전은 그대로 둔다.
        """
        profile = self.stat_profile(profile_id)
        if profile is None:
            raise ValueError(f"통계 기준 버전 {profile_id}이 없습니다")
        profile_id = int(profile_id)
        with self.lock:
            # 같은 버전을 다시 계산할 때 남은 줄이 섞이지 않게 먼저 비운다.
            self._conn.execute("DELETE FROM measurement WHERE profile_id=?", (profile_id,))
            self._conn.commit()
        cursors = {}
        try:
            result = self._compute_statistics(profile_id, profile["settings"], cursors=cursors, progress=progress)
        except Exception:
            with self.lock:
                self._conn.execute("DELETE FROM measurement WHERE profile_id=?", (profile_id,))
                self._conn.commit()
            raise
        now = _iso()
        with self.lock:
            self._save_active_profile(profile_id, now)
            self._conn.execute("DELETE FROM measurement WHERE COALESCE(profile_id, 0)<>?", (profile_id,))
            # 읽기 위치는 MAX가 아니라 그대로 덮어쓴다(이번 재계산이 처음부터 다 봤으므로 이 값이 정답).
            for mac, through in cursors.items():
                self._conn.execute(
                    """
                    INSERT INTO stat_cursor(mac, through_id, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(mac) DO UPDATE SET through_id=excluded.through_id, updated_at=excluded.updated_at
                    """,
                    (mac, int(through), now),
                )
            self._save_stat_meta(now)
            self._conn.commit()
            result["hours"] = int(self._conn.execute(
                "SELECT COUNT(*) FROM measurement WHERE profile_id=?", (profile_id,)).fetchone()[0])
        result["updated_at"] = now
        result["profile_id"] = profile_id
        return result

    def _save_active_profile(self, profile_id, now):
        """lock을 가진 상태에서 지금 버전을 바꾼다. commit은 부르는 쪽이 한다."""
        self._conn.execute(
            """
            INSERT INTO app_setting(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (ACTIVE_KEY, json.dumps({"id": int(profile_id)}), now),
        )

    def _save_stat_meta(self, now):
        """lock을 가진 상태에서 마지막 통계 계산 시각을 남긴다. commit은 부르는 쪽이 한다."""
        self._conn.execute(
            """
            INSERT INTO stat_meta(key, updated_at) VALUES ('measurement', ?)
            ON CONFLICT(key) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (now,),
        )

    def _touch_stat_meta(self):
        """마지막 통계 계산 시각을 지금으로. 매시간 스케줄러는 이 시각의 '시'로 이번 시간에 돌았는지 본다."""
        now = _iso()
        with self.lock:
            self._save_stat_meta(now)
            self._conn.commit()
        return now

    def _compute_statistics(self, profile_id, settings, cursors=None, progress=None):
        """MAC마다 읽기 위치 뒤의 ADXL 패킷을 STAT_CHUNK줄씩 읽어 시간 줄로 더한다.

        cursors가 None이면 매시간 최신화: 읽기 위치는 stat_cursor, 조각마다 위치를 같은 commit으로 저장한다.
        cursors가 dict면 재계산: 위치 0부터, 위치는 dict에만 두고(끝날 때 rebuild_statistics가 저장) 줄만 쓴다.
        조각 하나의 순서(수신 저장은 그동안에도 계속된다):
          1. (self.lock) id > 위치인 줄을 STAT_CHUNK개 읽는다. HEX 칸만 읽고 큰 decoded_samples는 안 읽는다.
          2. (잠금 없음) seq로 X/Y/Z 세트를 묶고 세트마다 stat_profile.set_stats. 시(hour)별로 모은다.
          3. settled_through로 다 본 위치를 정한다. 끝에 Z가 아직 없는 묶음은 다음에 다시 읽는다.
          4. (self.lock) 시간 줄이 이미 있으면 세트 수 가중으로 합치고(merge_rows), 없으면 넣는다. 위치 저장. commit.
        조각 사이에 잠깐 쉬어(10ms) 수신 스레드가 잠금을 먼저 잡을 수 있게 한다.
        """
        with self.lock:
            macs = [row["mac"] for row in self._conn.execute(
                "SELECT DISTINCT mac FROM packet WHERE sensor_type='ADXL' ORDER BY mac").fetchall()]
        totals = {"macs": 0, "sets": 0, "failed_sets": 0, "hours": 0, "notes": []}
        for done, mac in enumerate(macs):
            with self.lock:
                through = 0 if cursors is not None else self._stat_through(mac)
            touched = False
            while True:
                with self.lock:
                    rows = [dict(row) for row in self._conn.execute(
                        """
                        SELECT id, mac, axis, seq, sample_rate, sample_count, received_at, sensor_type, sensor_data_hex
                        FROM packet
                        WHERE mac=? AND sensor_type='ADXL' AND id>?
                        ORDER BY id LIMIT ?
                        """,
                        (mac, through, STAT_CHUNK),
                    ).fetchall()]
                if not rows:
                    break
                full = len(rows) == STAT_CHUNK
                buckets = {}
                for found in complete_sets(rows):
                    try:
                        objs = {axis: packet_to_template(found[axis]) for axis in ("X", "Y", "Z")}
                        stats = sp.set_stats(objs, settings)
                        when = max(str(found[axis]["received_at"]) for axis in ("X", "Y", "Z"))
                        hour = sp.hour_bucket(when)
                    except (ValueError, TypeError, OverflowError, ZeroDivisionError, KeyError):
                        totals["failed_sets"] += 1
                        continue
                    ids = [int(found[axis]["id"]) for axis in ("X", "Y", "Z")]
                    buckets.setdefault(hour, []).append((stats, when, min(ids), max(ids)))
                    totals["sets"] += 1
                    for text in stats["notes"]:
                        if text not in totals["notes"]:
                            totals["notes"].append(text)
                settled = settled_through(rows, through)
                if settled <= through and full:
                    # 묶음은 최대 3줄이라 조각 하나가 통째로 '끝나지 않은 묶음'일 수는 없지만, 혹시라도 멈추지 않게 넘긴다.
                    settled = max(int(row["id"]) for row in rows)
                self._write_hours(mac, profile_id, settings, buckets, settled, save_cursor=cursors is None)
                touched = touched or bool(buckets)
                totals["hours"] += len(buckets)
                through = settled
                if cursors is not None:
                    cursors[mac] = through
                if not full:
                    break
                time.sleep(0.01)
            if touched:
                totals["macs"] += 1
            if progress:
                progress(done + 1, len(macs))
        return totals

    def _write_hours(self, mac, profile_id, settings, buckets, settled, save_cursor):
        """시간 묶음들을 measurement에 넣거나 합친다(한 commit). save_cursor면 읽기 위치도 같은 commit에."""
        now = _iso()
        with self.lock:
            for hour, items in sorted(buckets.items()):
                new = sp.row_from_sets([(stats, when, first_id) for stats, when, first_id, _last in items])
                last_id = max(item[3] for item in items)
                key = f"H|{profile_id}|{mac}|{hour}"
                old = self._conn.execute("SELECT * FROM measurement WHERE set_key=?", (key,)).fetchone()
                if old is not None:
                    previous = dict(old)
                    try:
                        previous["notes"] = json.loads(previous.get("notes") or "[]")
                    except ValueError:
                        previous["notes"] = []
                    new = sp.merge_rows(previous, new)
                    last_id = max(last_id, int(previous.get("through_id") or 0))
                self._upsert_hour(mac, profile_id, hour, key, new, settings["g_per_lsb"], last_id, now)
            if save_cursor:
                self._save_stat_through(mac, settled)
            self._conn.commit()

    def _upsert_hour(self, mac, profile_id, hour, key, row, g_per_lsb, through_id, now):
        """lock을 가진 상태에서 시간 줄 하나를 넣거나 같은 set_key 줄을 덮어쓴다. commit은 부르는 쪽."""
        values = (
            mac, row["last_at"], "ADXL", row["sample_rate"], row["sample_count"],
            row["rms"], row["peak"], row["srss"], row["srss_mms"], "g RMS", g_per_lsb, key,
            row["set_count"], row["first_at"], row["last_at"], now, int(through_id),
            int(profile_id), hour, row["x_mms"], row["y_mms"], row["z_mms"], row["srss_mms_max"],
            int(row.get("first_id") or 0), json.dumps(row["notes"], ensure_ascii=False) if row.get("notes") else "",
        )
        self._conn.execute(
            """
            INSERT INTO measurement(
                mac, received_at, sensor_type, sample_rate, sample_count,
                rms, peak, srss, srss_mms, unit, g_per_lsb, set_key,
                set_count, first_at, last_at, computed_at, through_id,
                profile_id, hour, x_mms, y_mms, z_mms, srss_mms_max, first_id, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(set_key) DO UPDATE SET
                received_at=excluded.received_at, sample_rate=excluded.sample_rate,
                sample_count=excluded.sample_count, rms=excluded.rms, peak=excluded.peak,
                srss=excluded.srss, srss_mms=excluded.srss_mms, g_per_lsb=excluded.g_per_lsb,
                set_count=excluded.set_count, first_at=excluded.first_at, last_at=excluded.last_at,
                computed_at=excluded.computed_at, through_id=excluded.through_id,
                x_mms=excluded.x_mms, y_mms=excluded.y_mms, z_mms=excluded.z_mms,
                srss_mms_max=excluded.srss_mms_max, first_id=excluded.first_id, notes=excluded.notes
            """,
            values,
        )

    # ---------- CSV 시간별 통계(csv_stats.CsvStatsJob) ----------
    def adxl_packet_marks(self):
        """MAC별 지금 마지막 ADXL packet id. CSV 통계가 끝난 뒤 통계 읽기 위치로 쓴다."""
        with self.lock:
            rows = self._conn.execute(
                "SELECT mac, MAX(id) AS last_id FROM packet WHERE sensor_type='ADXL' GROUP BY mac"
            ).fetchall()
        return {row["mac"]: int(row["last_id"] or 0) for row in rows}

    def replace_measurements_from_csv(self, rows, marks, profile_id, g_per_lsb):
        """measurement를 전부 지우고 CSV 시간별 줄(기준 버전 profile_id)로 바꾼다. 한 commit이다.

        rows: csv_stats.rows_for_store 결과(MAC·시간마다 한 줄). marks: adxl_packet_marks() (시작 순간 값).
        - 지금 버전을 profile_id로 바꾼다(CSV 통계는 가장 최근 기준 버전으로 계산한다).
        - stat_cursor를 marks로 옮긴다. 그 id까지의 packet은 CSV에도 있으므로 매시간 최신화가 다시 세지 않는다.
          (MAX로 저장하므로 이미 더 앞선 위치면 그대로.) 그 뒤에 들어온 패킷만 다음 최신화에 들어간다.
        - device에 없는 MAC은 등록한다. 마지막 수신이 비어 있으면 CSV 마지막 시각을 넣는다(대문 '마지막 수신').
        stats_lock은 부르는 쪽(CsvStatsJob)이 잡고 있다.
        """
        now = _iso()
        last_seen = {}
        for row in rows:
            last_seen[row["mac"]] = max(last_seen.get(row["mac"], ""), row["last_at"])
        with self.lock:
            self._conn.execute("DELETE FROM measurement")
            for row in rows:
                key = f"CSV|{profile_id}|{row['mac']}|{row['hour']}"
                self._upsert_hour(row["mac"], profile_id, row["hour"], key, row, g_per_lsb, 0, now)
            for mac, through in marks.items():
                self._save_stat_through(mac, through)
            for mac, seen in last_seen.items():
                self._conn.execute(
                    """
                    INSERT INTO device(mac, created_at, updated_at, last_gw, last_battery, last_sensor, last_at)
                    VALUES (?, ?, ?, '', '', 'ADXL', ?)
                    ON CONFLICT(mac) DO UPDATE SET
                        last_sensor=CASE WHEN device.last_at='' THEN 'ADXL' ELSE device.last_sensor END,
                        last_at=CASE WHEN device.last_at='' THEN excluded.last_at ELSE device.last_at END
                    """,
                    (mac, now, now, seen),
                )
            self._save_active_profile(profile_id, now)
            self._save_stat_meta(now)
            self._conn.commit()

    def _stat_through(self, mac):
        """lock을 가진 상태에서, 그 MAC이 통계에서 이미 본 마지막 packet id(stat_cursor)."""
        cursor = self._conn.execute(
            "SELECT through_id FROM stat_cursor WHERE mac=?",
            (mac,),
        ).fetchone()
        return 0 if cursor is None else int(cursor["through_id"] or 0)

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

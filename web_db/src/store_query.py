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
# 통계를 내는 센서 종류(2026-11-23 PIEZO 추가). PIEZO는 패킷 하나가 측정 하나(한 축).
STAT_SENSORS_SQL = "('ADXL','PIEZO')"


def cursor_key(sensor, mac, source=""):
    """stat_cursor 키. ADXL은 예전처럼 MAC 그대로, PIEZO는 'PIEZO|MAC'(같은 MAC이 두 종류를 보내도 섞이지 않게).

    ADXL 가져온 CSV 줄(packet.source='csv', 2026-11-24)은 'CSV|MAC'. 실시간 줄과 읽기 위치를 따로 둔다
    (가져오기가 수신 중에 돌아 두 줄이 id 순서로 섞이므로, 한 위치로 읽으면 세트가 끊긴다).
    채운 줄(packet.source='fill', 2026-11-24: 실측이 빈 시간을 실측 파형으로 한 시간마다 채운 것)은 'FILL|MAC'.
    PIEZO는 묶을 축이 없어 source를 나누지 않는다.
    """
    if sensor == "ADXL":
        return {"csv": f"CSV|{mac}", "fill": f"FILL|{mac}"}.get(source, mac)
    return f"{sensor}|{mac}"


def hour_key(prefix, sensor, profile_id, mac, hour):
    """measurement.set_key. ADXL 'H|버전|MAC|시'(예전 그대로), PIEZO 'HP|버전|MAC|시'. 2026-11-24 전 'CSV로 시간별 통계'가 만든 옛 줄은 앞이 CSV / CSVP(다시 계산하면 지워진다)."""
    return f"{prefix}{'' if sensor == 'ADXL' else 'P'}|{profile_id}|{mac}|{hour}"


def scale_of(settings, sensor):
    """measurement.g_per_lsb 칸 값: ADXL g/LSB, PIEZO g/Count = 1000 × Vmax / Amax / 감도."""
    if sensor == "PIEZO":
        p = settings.get("piezo") or sp.DEFAULT_PIEZO
        return 1000.0 * p["vmax"] / p["amax"] / p["sensitivity"]
    return settings["g_per_lsb"]


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

    def devices(self, named=False):
        """MAC 목록과 패킷 수를 돌려준다.

        named=True면 이름이 있고 '표시'가 켜진 MAC만(센서 데이터 분석 MAC 선택, 2026-11-24). 설정 > 장치 표는 이름을 붙여야 하므로 전부 받는다.
        """
        where = "WHERE TRIM(d.display_name)<>'' AND d.visible=1" if named else ""
        with self.lock:
            rows = self._conn.execute(
                f"""
                SELECT d.mac, d.display_name, d.location, d.visible,
                       COUNT(p.id) AS packets,
                       MAX(p.received_at) AS last_at
                FROM device d
                LEFT JOIN packet p ON p.mac=d.mac
                {where}
                GROUP BY d.mac
                ORDER BY last_at DESC
                """
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["limit"] = self.get_srss_limit(item["mac"])
            item["zones"] = self.get_zones(item["mac"])  # 설정 > 장치 표의 양호·주의 상한 칸
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
                WHERE mac=? AND sensor_type=? AND COALESCE(profile_id, 0)=?
                """,
                (mac, self._stat_kind(mac, self._active_profile_id()), self._active_profile_id()),
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

    # 통계 화면 기간(2026-11-24). 키 → (지금부터 거슬러 올라갈 시간, 그래프 한 점의 묶음).
    # 막대·RMS 평균·PEAK 최대·그래프가 모두 고른 기간의 시간 줄로 계산된다(전에는 막대·숫자는 전체, 그래프는 최근 7일).
    # 7일·30일·전체는 그래프 점을 하루 단위로 묶는다(rms는 세트 수 가중 평균, peak는 최대). 7일 = 7점(2026-11-24, 전에는 한 시간 한 점).
    # 시간 단위가 필요하면 bucket='hour'로 부른다(통계2 자세히 보기의 '시간별').
    DASHBOARD_PERIODS = {"24h": (24, "hour"), "7d": (24 * 7, "day"), "30d": (24 * 30, "day"), "all": (None, "day")}

    def dashboard(self, period="24h", mac=None, bucket=None):
        """MAC별 고른 기간의 평균과 변화(지금 기준 버전). 화면은 저장된 숫자만 읽는다.

        period: DASHBOARD_PERIODS의 키. 모르는 값이면 24h.
        mac: 주면 그 MAC 줄만(통계2 화면 '+' 자세히 보기, 2026-11-24). 없으면 전체.
        bucket: 'hour'·'day'로 그래프 점 묶음을 바꾼다(자세히 보기에서 30일도 시간 단위로). 없으면 기간 기본값.
        - 기간의 시작은 '지금'에서 거슬러 올라간다(시간 줄의 hour 'YYYY-MM-DDTHH:00:00' 기준, 서버 지역 시각).
          수신이 멈춘 MAC은 기간 안에 줄이 없어 '이 기간 통계 없음'으로 보인다.
        - latest_*(최근 줄)는 기간과 상관없이 가장 늦은 시간 줄이다(센서 현황 경보와 같은 값).
        - 이름(display_name)이 없거나 '표시'를 끈(visible=0) MAC은 넣지 않는다(2026-11-24. 통계 계산은 그대로).
        - 초음파만 보내는 MAC(통계 줄이 없고 마지막 수신 종류가 ULTRASONIC)은 넣지 않는다(2026-11-24).
          초음파는 아직 이상 판정 기준이 없어 통계 계산도 하지 않는다(STAT_SENSORS_SQL).
        """
        if period not in self.DASHBOARD_PERIODS:
            period = "24h"
        hours, default_bucket = self.DASHBOARD_PERIODS[period]
        bucket = bucket if bucket in ("hour", "day") else default_bucket
        since = ""
        if hours is not None:
            from datetime import datetime, timedelta
            # 시간 줄 키와 같은 모양으로 자른다. 24h면 '지금 시각의 정시 - 23시간'부터(정시 줄 24개).
            # 7d·30d는 날짜 단위(2026-11-24): 오늘 0시에서 (일수-1)일 앞부터 → 오늘 포함 7일·30일, 하루 점이 정확히 7·30개.
            # (그냥 168시간 전부터 자르면 첫날이 일부만 들어가 점이 8개가 된다.) 시간별로 읽어도(bucket=hour) 같은 기간이다.
            now = datetime.now()
            if hours >= 48:
                start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=hours // 24 - 1)
            else:
                start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)
            since = start.strftime("%Y-%m-%dT%H:00:00")
        # 옛 하루 통계(버전 0)는 hour가 비어 있어 last_at으로 대신 본다.
        when = "COALESCE(hour, last_at)"
        with self.lock:
            active = self._active_profile_id()
            devices = self._conn.execute(
                "SELECT mac, display_name, location, last_sensor FROM device WHERE TRIM(display_name)<>'' AND visible=1 "
                "ORDER BY created_at, mac").fetchall()
            items = []
            pairs = []
            if mac:
                devices = [device for device in devices if device["mac"] == str(mac).strip()]
            for device in devices:
                # 종류마다 한 줄. 같은 MAC이 SENSORTYPE을 바꿔 PIEZO·MEMS를 둘 다 보낸 적이 있으면(실제 865872312270)
                # 두 줄로 따로 보인다(섞어 평균하지 않는다). 통계가 없으면 ADXL 한 줄('통계 전').
                kinds = self._stat_kinds(device["mac"], active)
                if not kinds and str(device["last_sensor"] or "").upper() == "ULTRASONIC":
                    continue  # 초음파 센서는 통계 화면에 보이지 않는다
                pairs.extend((device, kind) for kind in (kinds or ["ADXL"]))
            for device, kind in pairs:
                base = (device["mac"], kind, active, since)
                row = self._conn.execute(
                    f"""
                    SELECT SUM(set_count) AS set_count,
                           SUM(rms * set_count) / SUM(set_count) AS rms,
                           MAX(peak) AS peak,
                           SUM(srss * set_count) / SUM(set_count) AS srss,
                           SUM(srss_mms * set_count) / SUM(set_count) AS srss_mms,
                           MIN(first_at) AS first_at,
                           MAX(last_at) AS last_at,
                           COUNT(*) AS rows
                    FROM measurement
                    WHERE mac=? AND sensor_type=? AND COALESCE(profile_id, 0)=? AND {when}>=?
                    """,
                    base,
                ).fetchone()
                item = dict(row)
                item.update({"mac": device["mac"], "display_name": device["display_name"],
                             "location": device["location"], "sensor_type": kind})
                item["limit"] = self._read_srss_limit(item["mac"])
                item["zones"] = self._read_zones(item["mac"])  # 막대 색 구간(양호·주의·경고·위험)
                item["set_count"] = int(item["set_count"] or 0)
                # 이 MAC에 통계 줄이 하나라도 있는지(기간 밖이라도). 화면이 '통계 전'과 '이 기간 없음'을 나눠 쓴다.
                item["has_stats"] = kind in self._stat_kinds(item["mac"], active)
                # 최근 줄 = 가장 늦은 시간 줄(센서 현황 경보와 같은 기준: last_at 순). 기간과 상관없다.
                latest = self._conn.execute(
                    """
                    SELECT srss_mms, last_at FROM measurement
                    WHERE mac=? AND sensor_type=? AND COALESCE(profile_id, 0)=?
                    ORDER BY last_at DESC, id DESC LIMIT 1
                    """,
                    base[:3],
                ).fetchone()
                item["latest_srss_mms"] = latest["srss_mms"] if latest else None
                item["latest_at"] = latest["last_at"] if latest else None
                if bucket == "hour":
                    # 시간 줄 그대로(24h 최대 24점, 7d 최대 168점). 시간 줄은 묶음 시(hour), 옛 하루 통계는 마지막 세트 시각.
                    points = self._conn.execute(
                        f"""
                        SELECT {when} AS t, srss_mms, srss_mms_max, x_mms, y_mms, z_mms, rms, peak, set_count
                        FROM measurement
                        WHERE mac=? AND sensor_type=? AND COALESCE(profile_id, 0)=? AND {when}>=?
                        ORDER BY t, id
                        """,
                        base,
                    ).fetchall()
                else:
                    # 하루 한 점: t는 'YYYY-MM-DD'. 평균은 세트 수 가중, 최대는 그날의 최대.
                    points = self._conn.execute(
                        f"""
                        SELECT substr({when}, 1, 10) AS t,
                               SUM(srss_mms * set_count) / SUM(set_count) AS srss_mms,
                               MAX(srss_mms_max) AS srss_mms_max,
                               SUM(x_mms * set_count) / SUM(set_count) AS x_mms,
                               SUM(y_mms * set_count) / SUM(set_count) AS y_mms,
                               SUM(z_mms * set_count) / SUM(set_count) AS z_mms,
                               SUM(rms * set_count) / SUM(set_count) AS rms,
                               MAX(peak) AS peak,
                               SUM(set_count) AS set_count
                        FROM measurement
                        WHERE mac=? AND sensor_type=? AND COALESCE(profile_id, 0)=? AND {when}>=?
                        GROUP BY t ORDER BY t
                        """,
                        base,
                    ).fetchall()
                item["series"] = [dict(point) for point in points]
                items.append(item)
        profile = self.stat_profile(active) if active else None
        return {
            "updated_at": self.stats_updated_at(),
            "period": period,
            "since": since,
            "bucket": bucket,
            "items": items,
            # 통계 화면 안내 줄: 어떤 기준으로 계산한 값인지.
            "profile": {"id": active, "summary": profile["summary"] if profile else "옛 하루 통계(필터·보정 없음)",
                        "created_at": profile["created_at"] if profile else ""},
        }

    def _stat_kinds(self, mac, profile_id):
        """lock을 가진 상태에서 그 MAC에 통계 줄이 있는 종류 목록(ADXL 먼저)."""
        rows = self._conn.execute(
            f"""
            SELECT sensor_type FROM measurement
            WHERE mac=? AND sensor_type IN {STAT_SENSORS_SQL} AND COALESCE(profile_id, 0)=?
            GROUP BY sensor_type ORDER BY CASE sensor_type WHEN 'ADXL' THEN 0 ELSE 1 END
            """,
            (mac, int(profile_id)),
        ).fetchall()
        return [row["sensor_type"] for row in rows]

    def _stat_kind(self, mac, profile_id):
        """lock을 가진 상태에서 그 MAC의 대표 통계 종류(ADXL 줄이 있으면 ADXL, 없고 PIEZO만 있으면 PIEZO)."""
        kinds = self._stat_kinds(mac, profile_id)
        return kinds[0] if kinds else "ADXL"

    def stats_updated_at(self):
        """통계를 마지막으로 만든 시각. 기록이 없으면 마지막 measurement 시각."""
        with self.lock:
            row = self._conn.execute(
                "SELECT updated_at FROM stat_meta WHERE key='measurement'"
            ).fetchone()
            if row is not None:
                return row["updated_at"]
            fallback = self._conn.execute(
                f"SELECT MAX(received_at) AS updated_at FROM measurement WHERE sensor_type IN {STAT_SENSORS_SQL}"
            ).fetchone()
        return None if fallback is None else fallback["updated_at"]

    def update_statistics(self):
        """매시간 스케줄러(와 명령줄 import_csv)가 부른다. 지금 기준 버전으로 새 세트만 더한다.

        지금 버전이 아직 없으면(업그레이드 직후 재계산 전) 아무것도 하지 않는다. 재계산이 끝나면 버전이 정해진다.
        재계산과 겹치지 않게 stats_lock을 잡는다(뒤에 온 쪽은 기다린다).
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

        부르는 쪽(stats_job.StatRebuildJob)이 stats_lock을 잡고 있다(매시간 최신화와 겹치지 않음).
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
        ADXL이 끝나면 PIEZO도 같은 순서로 한다(_compute_piezo, 패킷 하나 = 측정 하나).
        cursors의 키는 cursor_key다(ADXL은 MAC, 가져온 CSV ADXL은 'CSV|MAC', PIEZO는 'PIEZO|MAC').
        ADXL은 MAC마다 실시간 줄과 가져온 줄을 따로 읽는다(_compute_adxl).
        """
        with self.lock:
            macs = [row["mac"] for row in self._conn.execute(
                "SELECT DISTINCT mac FROM packet WHERE sensor_type='ADXL' ORDER BY mac").fetchall()]
            piezo_macs = [row["mac"] for row in self._conn.execute(
                "SELECT DISTINCT mac FROM packet WHERE sensor_type='PIEZO' ORDER BY mac").fetchall()]
        totals = {"macs": 0, "sets": 0, "failed_sets": 0, "hours": 0, "notes": [], "piezo_packets": 0}
        steps = len(macs) + len(piezo_macs)
        for done, mac in enumerate(macs):
            # 실시간 줄('')·가져온 CSV 줄('csv')·채운 줄('fill')을 따로 읽는다(cursor_key 설명). 모두 같은 시간 줄에 더해진다.
            touched = False
            for source in ("", "csv", "fill"):
                touched = self._compute_adxl(mac, source, profile_id, settings, cursors, totals) or touched
            if touched:
                totals["macs"] += 1
            if progress:
                progress(done + 1, steps)
        for done, mac in enumerate(piezo_macs, len(macs)):
            if self._compute_piezo(mac, profile_id, settings, cursors, totals):
                totals["macs"] += 1
            if progress:
                progress(done + 1, steps)
        return totals

    def _compute_adxl(self, mac, source, profile_id, settings, cursors, totals):
        """ADXL 한 MAC·한 source: 읽기 위치 뒤 줄을 STAT_CHUNK줄씩 읽어 세트 → 시간 줄. 줄을 썼으면 True.

        source '' = 실시간 수신 줄, 'csv' = 'CSV 데이터 가져오기'로 넣은 옛 줄(2026-11-24). 같은 source 안에서는
        세트의 세 축이 id 순서로 붙어 있으므로(가져오기는 세트를 통째로 넣는다) seq 묶기가 그대로 맞다.
        """
        key = cursor_key("ADXL", mac, source)
        with self.lock:
            through = 0 if cursors is not None else self._stat_through(key)
        touched = False
        while True:
            with self.lock:
                rows = [dict(row) for row in self._conn.execute(
                    """
                    SELECT id, mac, axis, seq, sample_rate, sample_count, received_at, sensor_type, sensor_data_hex
                    FROM packet
                    WHERE mac=? AND sensor_type='ADXL' AND source=? AND id>?
                    ORDER BY id LIMIT ?
                    """,
                    (mac, source, through, STAT_CHUNK),
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
            self._write_hours(mac, profile_id, settings, buckets, settled, save_cursor=cursors is None, cursor=key)
            touched = touched or bool(buckets)
            totals["hours"] += len(buckets)
            through = settled
            if cursors is not None:
                cursors[key] = through
            if not full:
                break
            time.sleep(0.01)
        return touched

    def _compute_piezo(self, mac, profile_id, settings, cursors, totals):
        """PIEZO 한 MAC: 읽기 위치 뒤 패킷을 STAT_CHUNK줄씩 읽어 패킷마다 piezo_stats → 시간 줄. 줄을 썼으면 True.

        ADXL과 달리 묶을 축이 없어, 읽은 조각의 마지막 id까지가 곧 다 본 위치다. 잠금 규칙은 ADXL과 같다.
        """
        key = cursor_key("PIEZO", mac)
        with self.lock:
            through = 0 if cursors is not None else self._stat_through(key)
        touched = False
        while True:
            with self.lock:
                rows = [dict(row) for row in self._conn.execute(
                    """
                    SELECT id, mac, axis, seq, sample_rate, sample_count, received_at, sensor_type, sensor_data_hex
                    FROM packet
                    WHERE mac=? AND sensor_type='PIEZO' AND id>?
                    ORDER BY id LIMIT ?
                    """,
                    (mac, through, STAT_CHUNK),
                ).fetchall()]
            if not rows:
                break
            buckets = {}
            for row in rows:
                try:
                    stats = sp.piezo_stats(packet_to_template(row), settings)
                    when = str(row["received_at"])
                    hour = sp.hour_bucket(when)
                except (ValueError, TypeError, OverflowError, ZeroDivisionError, KeyError):
                    totals["failed_sets"] += 1
                    continue
                buckets.setdefault(hour, []).append((stats, when, int(row["id"]), int(row["id"])))
                totals["sets"] += 1
                totals["piezo_packets"] += 1
                for text in stats["notes"]:
                    if text not in totals["notes"]:
                        totals["notes"].append(text)
            settled = max(int(row["id"]) for row in rows)
            self._write_hours(mac, profile_id, settings, buckets, settled, save_cursor=cursors is None, sensor="PIEZO")
            touched = touched or bool(buckets)
            totals["hours"] += len(buckets)
            through = settled
            if cursors is not None:
                cursors[key] = through
            if len(rows) < STAT_CHUNK:
                break
            time.sleep(0.01)
        return touched

    def _write_hours(self, mac, profile_id, settings, buckets, settled, save_cursor, sensor="ADXL", cursor=None):
        """시간 묶음들을 measurement에 넣거나 합친다(한 commit). save_cursor면 읽기 위치(cursor 키, 없으면
        cursor_key(sensor, mac))도 같은 commit에."""
        now = _iso()
        with self.lock:
            for hour, items in sorted(buckets.items()):
                new = sp.row_from_sets([(stats, when, first_id) for stats, when, first_id, _last in items])
                last_id = max(item[3] for item in items)
                key = hour_key("H", sensor, profile_id, mac, hour)
                old = self._conn.execute("SELECT * FROM measurement WHERE set_key=?", (key,)).fetchone()
                if old is not None:
                    previous = dict(old)
                    try:
                        previous["notes"] = json.loads(previous.get("notes") or "[]")
                    except ValueError:
                        previous["notes"] = []
                    new = sp.merge_rows(previous, new)
                    last_id = max(last_id, int(previous.get("through_id") or 0))
                self._upsert_hour(mac, profile_id, hour, key, new, scale_of(settings, sensor), last_id, now, sensor)
            if save_cursor:
                self._save_stat_through(cursor or cursor_key(sensor, mac), settled)
            self._conn.commit()

    def _upsert_hour(self, mac, profile_id, hour, key, row, g_per_lsb, through_id, now, sensor="ADXL"):
        """lock을 가진 상태에서 시간 줄 하나를 넣거나 같은 set_key 줄을 덮어쓴다. commit은 부르는 쪽.

        g_per_lsb 칸: ADXL은 g/LSB, PIEZO는 g/Count(= 1000×Vmax/Amax/감도). 둘 다 'Count 하나가 몇 g'이다.
        """
        values = (
            mac, row["last_at"], sensor, row["sample_rate"], row["sample_count"],
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

    def packet_counts(self):
        """설정 'CSV 데이터 가져오기' 안내용: packet 줄 수 {'live': 실시간 수신, 'csv': 가져온 줄}.

        idx_packet_stream(mac, sensor_type, source, id)만 훑어 센다(큰 HEX 칸이 있는 표 본문은 읽지 않음).
        """
        with self.lock:
            rows = self._conn.execute(
                "SELECT source, COUNT(*) AS n FROM packet INDEXED BY idx_packet_stream GROUP BY source"
            ).fetchall()
        # 'fill'(2026-11-24): 실측이 빈 시간을 실측 파형으로 채운 줄. 따로 센다.
        result = {"live": 0, "csv": 0, "fill": 0}
        for row in rows:
            result[row["source"] if row["source"] in ("csv", "fill") else "live"] += int(row["n"] or 0)
        return result

    def _stat_through(self, mac):
        """lock을 가진 상태에서, 그 읽기 위치 키(cursor_key: MAC 또는 'PIEZO|MAC')가 이미 본 마지막 packet id."""
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

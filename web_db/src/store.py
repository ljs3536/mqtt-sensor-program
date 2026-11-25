"""SQLite 저장소.

역할:
- device: MAC 명부와 표시 이름
- packet: CSV와 같은 수신 원본. 통계 계산은 하지 않는다
- sensor_config: 설정 적용 때 보낸 JSON, 브로커 retain으로 받은 S/{MAC} 설정
- app_setting, floor_plan, sensor_placement: 앱 설정(MQTT 자동 연결)과 대문 평면도 → store_site.SiteMixin
- measurement 조회와 최신화는 store_query.QueryMixin
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path

from web_db.src.db_util import _float, _int, _iso, _rate
from web_db.src import kind_view
from web_db.src.grouping import assign_set
from web_db.src.limits import zone_bounds
from web_db.src.store_query import QueryMixin
from web_db.src.store_site import SiteMixin

from web_db.src.paths import DB_FILE

# 기본 DB 파일. SENSOR_DATA_DIR가 있으면 그 폴더(Docker 볼륨), 없으면 web_db/sensor.sqlite(paths.py).
DB_PATH = DB_FILE


class Store(QueryMixin, SiteMixin):
    """sensor.sqlite 하나를 프로세스 전체가 같이 쓰는 저장소.

    연결은 하나다(check_same_thread=False). MQTT 수신 스레드, HTTP 요청 스레드,
    매시간 통계·재계산 스레드가 모두 이 연결을 쓰므로, 쿼리와 commit은 반드시 self.lock 안에서 한다.
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
        - app_setting: 키별 JSON 설정(지금은 'mqtt' 자동 연결 값)
        - site_state, site_state_history: 센서별 지금 상태와 시작 시각, 상태가 바뀐 기록(site_watch가 1분마다)
        - floor_plan, sensor_placement: 대문 평면도(여러 장, 탭 순서 sort_order)와 센서 아이콘 위치(비율 0~1).
          평면도 한 장에 센서 여러 개, 센서 하나는 평면도 한 장에만
        """
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS device (
                mac TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                visible INTEGER NOT NULL DEFAULT 1,
                kind_view TEXT NOT NULL DEFAULT '',
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
            CREATE TABLE IF NOT EXISTS app_setting (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS floor_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '평면도',
                file TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sensor_placement (
                plan_id INTEGER NOT NULL REFERENCES floor_plan(id) ON DELETE CASCADE,
                mac TEXT NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (plan_id, mac)
            );
            CREATE TABLE IF NOT EXISTS site_state (
                mac TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                link TEXT NOT NULL DEFAULT '',
                alarm TEXT NOT NULL DEFAULT '',
                since TEXT NOT NULL,
                checked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS site_state_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac TEXT NOT NULL,
                prev_state TEXT NOT NULL,
                state TEXT NOT NULL,
                link TEXT NOT NULL DEFAULT '',
                alarm TEXT NOT NULL DEFAULT '',
                changed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_state_history_time ON site_state_history(changed_at);
            CREATE INDEX IF NOT EXISTS idx_state_history_mac ON site_state_history(mac, changed_at);
            CREATE INDEX IF NOT EXISTS idx_measurement_mac_time ON measurement(mac, last_at);
            DROP TABLE IF EXISTS daily_stat;
            """
        )
        # 평면도 여러 장(탭 순서). 한 장만 쓰던 때 만든 floor_plan에는 sort_order가 없어서 붙인다.
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(floor_plan)")}
        if "sort_order" not in columns:
            self._conn.execute("ALTER TABLE floor_plan ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        # 최근 알림 '확인'(2026-11-19). 확인한 시각이 있으면 대문 알림 목록에서 숨긴다(기록은 남긴다).
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(site_state_history)")}
        if "acked_at" not in columns:
            self._conn.execute("ALTER TABLE site_state_history ADD COLUMN acked_at TEXT NOT NULL DEFAULT ''")
        # 통계 계산 기준 버전(2026-11-23, stat_profile.py). 버전은 지우지 않고 쌓는다. 지금 쓰는 버전은
        # app_setting 'stat_profile_active'({"id": n}). 없으면(업그레이드 직후) 옛 하루 통계(profile_id 없음)를 보인다.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stat_profile (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                settings TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        # 한 시간 한 줄 통계(2026-11-23)에 쓰는 칸. 옛 하루 통계 줄은 이 칸이 비어 있다(profile_id NULL).
        # - profile_id: 계산에 쓴 stat_profile.id. 화면·경보는 지금 버전 줄만 본다.
        # - hour: 묶음 키 'YYYY-MM-DDTHH:00:00'(세트 마지막 축 수신 시각의 시).
        # - x_mms/y_mms/z_mms: 축별 속도 RMS 평균, srss_mms_max: 그 시간 세트 중 SRSS 최대.
        # - first_id: 그 시간 첫 세트의 가장 작은 packet id(through_id와 함께 원본 범위).
        # - notes: 그 Fs에서 쓸 수 없어 끈 필터 등 계산 메모(JSON 목록).
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(measurement)")}
        for name, kind in (("profile_id", "INTEGER"), ("hour", "TEXT"), ("x_mms", "REAL"), ("y_mms", "REAL"),
                           ("z_mms", "REAL"), ("srss_mms_max", "REAL"), ("first_id", "INTEGER"),
                           ("notes", "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE measurement ADD COLUMN {name} {kind}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_measurement_profile ON measurement(profile_id, mac, hour)"
        )
        # 진동 평가 구간(2026-11-23, limits.zone_bounds): MAC마다 양호 상한·주의 상한. 비어 있으면 한계의 ISO 비율.
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(srss_limit)")}
        for name in ("good_mm_s", "caution_mm_s"):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE srss_limit ADD COLUMN {name} REAL")
        # 지금 코드가 읽지도 쓰지도 않는 옛 열은 지운다(2026-11-23). device·measurement는 작은 표라 바로 끝난다.
        # 다른 프로그램이 DB를 잡고 있는 등으로 실패하면 그대로 두고 서버는 뜬다(값이 비어 있을 뿐 동작 영향 없음).
        # device.location(설치 위치)은 2026-11-23에 지웠다가 2026-11-24에 다시 쓴다(설정 > 장치 '위치' 칸).
        # 그래서 지우는 목록에서 빼고, 없으면(이미 지운 DB) 빈 값으로 다시 만든다. 지워졌던 값은 돌아오지 않는다.
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(device)")}
        if "location" not in columns:
            self._conn.execute("ALTER TABLE device ADD COLUMN location TEXT NOT NULL DEFAULT ''")
        # device.visible(2026-11-24): 설정 > 장치 '표시' 체크. 0이면 이름이 있어도 센서 현황·분석·통계에서 숨긴다.
        # 옛 줄은 모두 보이게(1). 수신 저장·통계 계산과는 상관없다.
        if "visible" not in columns:
            self._conn.execute("ALTER TABLE device ADD COLUMN visible INTEGER NOT NULL DEFAULT 1")
        # device.kind_view(2026-11-25): 종류(PIEZO·MEMS·초음파)별 화면 표시. 규칙은 kind_view.py.
        # 옛 DB에 처음 만들 때는 packet에서 MAC마다 처음 받은 종류만 보임, 그 뒤에 들어온 종류는 자동 숨김(-1)으로 채운다.
        # (packet 인덱스 idx_packet_stream만 훑으므로 한 번만 잠깐 걸린다.)
        if "kind_view" not in columns:
            self._conn.execute("ALTER TABLE device ADD COLUMN kind_view TEXT NOT NULL DEFAULT ''")
            if self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='packet'").fetchone():
                filled = kind_view.backfill(self._conn.execute(kind_view.backfill_sql()).fetchall())
                self._conn.executemany("UPDATE device SET kind_view=? WHERE mac=?", [(v, m) for m, v in filled.items()])
        for table, names in (("device", ("asset_no",)),
                             ("measurement", ("packet_id", "packet_x_id", "packet_y_id", "packet_z_id"))):
            columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for name in names:
                if name in columns:
                    try:
                        self._conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
                    except sqlite3.DatabaseError:
                        pass
        # packet.decoded_samples(HEX를 푼 샘플 글자): 2026-11-23부터 읽는 곳이 없다(통계도 HEX를 읽음).
        # DB 크기의 절반쯤이라 새 DB에는 만들지 않는다. 옛 DB는 열이 남아 있으면 빈 글자로만 채운다
        # (NOT NULL). 이미 쌓인 값을 지우고 파일을 줄이는 것은 packet 표 전체를 다시 쓰는 일이라
        # 서버 시작 때 하지 않고, 서버를 멈춘 뒤 `python -m web_db.src.compact_db`로 한다.
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(packet)")}
        self._packet_has_decoded = "decoded_samples" in columns
        # packet.source(2026-11-24): 이 줄이 어디서 왔는지. '' = 실시간 수신(save_packet),
        # 'csv' = 설정 > 통계 데이터 'CSV 데이터 가져오기'로 넣은 옛 CSV 줄(import_packets, csv_import.py).
        # 가져오기는 수신 중에도 돌아서 옛 줄과 실시간 줄이 id 순서로 섞인다. 그래서
        # - 실시간 세트 묶기(grouping.assign_set)는 source=''인 직전 패킷만 보고,
        # - 매시간 통계·재계산은 source별로 따로 읽는다(store_query._compute_adxl, 읽기 위치 키도 따로).
        # 옛 DB의 줄은 모두 실시간 수신이라 기본값 ''가 맞다.
        if "source" not in columns:
            self._conn.execute("ALTER TABLE packet ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        # 세트 묶기·통계가 'MAC·종류·source 안에서 id 순'으로 읽는 길(가져온 줄이 많아도 실시간 저장이 느려지지 않게).
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_packet_stream ON packet(mac, sensor_type, source, id)")
        # 옛 스키마에서 남은 MAC 빈 device 줄(mac이 NULL)은 화면 목록을 어지럽히므로 지운다.
        # 있을 때만 지운다. 늘 DELETE를 하면 다른 프로그램(DB Browser 등)이 쓰기 잠금을 잡고 있을 때
        # 서버가 시작조차 못 한다(읽기는 WAL이라 잠금과 상관없이 된다).
        empty = self._conn.execute("SELECT COUNT(*) FROM device WHERE mac IS NULL OR TRIM(mac)=''").fetchone()[0]
        if empty:
            self._conn.execute("DELETE FROM device WHERE mac IS NULL OR TRIM(mac)=''")
        self._conn.commit()

    def save_display_name(self, mac, name, location=None):
        """MAC의 표시 이름(과 설치 위치)을 device에 저장한다.

        location이 None이면 위치는 그대로 둔다(이름만 저장하는 수신 모니터용). 새 MAC이면 빈 위치로 만든다.
        """
        mac = str(mac or "").strip()
        if not mac:
            raise ValueError("MAC이 필요합니다")
        now = _iso()
        label = str(name or "").strip()
        place = None if location is None else str(location).strip()[:100]
        with self.lock:
            self._conn.execute(
                """
                INSERT INTO device(
                    mac, display_name, location, created_at, updated_at, last_gw, last_battery, last_sensor, last_at
                ) VALUES (?, ?, COALESCE(?, ''), ?, ?, '', '', '', '')
                ON CONFLICT(mac) DO UPDATE SET
                    display_name=excluded.display_name,
                    location=COALESCE(?, device.location),
                    updated_at=excluded.updated_at
                """,
                (mac, label, place, now, now, place),
            )
            place = self._conn.execute("SELECT location FROM device WHERE mac=?", (mac,)).fetchone()["location"]
            self._conn.commit()
        return {"mac": mac, "display_name": label, "location": place}

    def set_device_visible(self, mac, visible):
        """설정 > 장치 '표시' 체크를 저장한다. 없는 MAC이면 ValueError."""
        mac = str(mac or "").strip()
        with self.lock:
            changed = self._conn.execute(
                "UPDATE device SET visible=?, updated_at=? WHERE mac=?", (1 if visible else 0, _iso(), mac)).rowcount
            self._conn.commit()
        if not changed:
            raise ValueError("등록되지 않은 MAC입니다")
        return {"mac": mac, "visible": bool(visible)}

    def set_device_kind(self, mac, kind, shown):
        """설정 > 장치 종류 칸(PIEZO·MEMS·초음파) 저장(2026-11-25). shown=False면 그 종류를 화면에서만 뺀다.

        돌려줌: {"mac", "kind", "shown", "kind_view": {종류: 값}}. 없는 MAC·모르는 종류면 ValueError.
        """
        mac = str(mac or "").strip()
        kind = kind_view.to_kind(kind)
        if not kind:
            raise ValueError("종류는 PIEZO, MEMS(ADXL), 초음파 중 하나여야 합니다")
        with self.lock:
            row = self._conn.execute("SELECT kind_view FROM device WHERE mac=?", (mac,)).fetchone()
            if row is None:
                raise ValueError("등록되지 않은 MAC입니다")
            view = kind_view.parse(row["kind_view"])
            view[kind] = kind_view.SHOWN if shown else kind_view.HIDDEN
            text = json.dumps(view, ensure_ascii=False)
            self._conn.execute("UPDATE device SET kind_view=?, updated_at=? WHERE mac=?", (text, _iso(), mac))
            self._conn.commit()
        return {"mac": mac, "kind": kind, "shown": bool(shown), "kind_view": view}

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

    def _read_zones(self, mac):
        """lock을 가진 상태에서 진동 평가 구간 {good, caution, limit, custom}(limits.zone_bounds)."""
        key = self.limit_key(mac)
        row = self._conn.execute(
            "SELECT limit_mm_s, good_mm_s, caution_mm_s FROM srss_limit WHERE mac=?", (key,)
        ).fetchone() if key else None
        if row is None:
            return zone_bounds(4.5)
        return zone_bounds(self._limit_number(row["limit_mm_s"]), row["good_mm_s"], row["caution_mm_s"])

    def get_zones(self, mac):
        """진동 평가 구간 {good, caution, limit, custom}. 양호 < good ≤ 주의 < caution ≤ 경고 < limit ≤ 위험."""
        with self.lock:
            return self._read_zones(mac)

    def save_zones(self, mac, good, caution, limit):
        """세 경계를 저장한다. good·caution이 둘 다 비어 있으면(None·'') 자동(ISO 비율)으로 되돌린다.

        0 < 양호 상한 < 주의 상한 < 위험 기준이어야 한다. 위험 기준은 예전 SRSS 한계와 같은 칸이다.
        """
        key = self.limit_key(mac)
        number = float(limit)
        if not key or not math.isfinite(number) or number <= 0:
            raise ValueError("장치 ID와 0보다 큰 위험 기준이 필요합니다")
        blank = lambda value: value is None or str(value).strip() == ""
        if blank(good) and blank(caution):
            good_value = caution_value = None
        else:
            try:
                good_value, caution_value = float(good), float(caution)
            except (TypeError, ValueError):
                raise ValueError("양호 상한과 주의 상한은 둘 다 숫자여야 합니다(둘 다 비우면 자동)") from None
            if not (0 < good_value < caution_value < number):
                raise ValueError("0 < 양호 상한 < 주의 상한 < 위험 기준 순서여야 합니다")
        with self.lock:
            self._conn.execute(
                """
                INSERT INTO srss_limit(mac, limit_mm_s, good_mm_s, caution_mm_s, updated_at) VALUES (?,?,?,?,?)
                ON CONFLICT(mac) DO UPDATE SET
                    limit_mm_s=excluded.limit_mm_s, good_mm_s=excluded.good_mm_s,
                    caution_mm_s=excluded.caution_mm_s, updated_at=excluded.updated_at
                """,
                (key, number, good_value, caution_value, _iso()),
            )
            self._conn.commit()
            return self._read_zones(key)

    def save_srss_limit(self, mac, value):
        """장치 SRSS 한계(= 위험 기준)를 저장한다. 분석 화면의 '한계 적용'이 부른다.

        저장해 둔 양호·주의 상한이 새 한계보다 크거나 같아지면 그 둘은 비워 자동(ISO 비율)으로 되돌린다.
        """
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
            self._conn.execute(
                "UPDATE srss_limit SET good_mm_s=NULL, caution_mm_s=NULL "
                "WHERE mac=? AND (caution_mm_s IS NOT NULL AND caution_mm_s>=?)",
                (key, number),
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

    def save_sensor_config(self, mac, payload, action, only_if_changed=False):
        """설정 JSON을 그 MAC의 sensor_config에 남긴다(MAC당 마지막 하나).

        - 웹이 설정 적용·START·STOP으로 보낸 JSON(action = 버튼 이름).
        - 구독 중 S/{MAC}로 받은 설정(action = 'retain 수신'). 브로커가 구독 순간 주는 retain 값이라
          데스크톱 수신기가 보낸 설정도 여기 남는다. 대문 연결 판정이 SENSING_PERIOD와 STOP(SENSORTYPE 0)을 읽는다.
        only_if_changed=True면 저장된 JSON과 같을 때 쓰지 않는다(자기가 보낸 retain이 되돌아올 때 이름을 덮지 않게).
        """
        mac = str(mac or "").strip()
        if not mac:
            return
        now = _iso()
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            if only_if_changed:
                row = self._conn.execute("SELECT payload FROM sensor_config WHERE mac=?", (mac,)).fetchone()
                if row is not None and row["payload"] == text:
                    return False
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
        return True

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
          Hz로 읽는 것은 쓰는 쪽(통계 stat_profile, 그래프)이 normalize_sample_rate로 한다.
        - decoded_samples는 2026-11-23부터 쓰지 않는다. 옛 DB에 열이 남아 있으면 빈 글자를 넣는다(_migrate 설명).
        - set_key / set_no: assign_set이 직전 패킷과 seq를 비교해 정한다. 같은 lock 안에서
          '직전 패킷 조회 → INSERT'를 하므로, 두 패킷이 같은 직전 값을 보고 번호가 꼬이지 않는다.
        한 패킷마다 commit한다. 서버가 꺼져도 받은 패킷까지는 남는다.
        """
        mac, sensor_type, axis, seq, received, names, values = self._packet_values(item)
        with self.lock:
            key, set_no = assign_set(self._conn, mac, seq, sensor_type, axis)
            self._touch_device(mac, received, str(item.get("gw") or ""), str(item.get("battery") or ""), sensor_type)
            cursor = self._conn.execute(
                f"INSERT INTO packet({', '.join(names + ['set_key', 'set_no'])}) "
                f"VALUES ({', '.join('?' for _ in range(len(names) + 2))})",
                values + [key, set_no],
            )
            packet_id = int(cursor.lastrowid)
            self._conn.commit()
            return packet_id

    def _packet_values(self, item):
        """save_packet·import_packets 공용: item → (mac, 종류, 축, seq, 수신 시각, packet 칸 이름, 값).

        set_key·set_no·source는 넣지 않는다(부르는 쪽이 붙인다).
        """
        mac = str(item.get("device") or "UNKNOWN").strip() or "UNKNOWN"
        kind = str(item.get("kind") or "")
        sensor_type = "ULTRASONIC" if kind == "ULTRASONIC" else ("ADXL" if kind.startswith("ADXL") else "PIEZO")
        axis = "H" if sensor_type == "ULTRASONIC" else str(item.get("axis") or "")
        seq = str(item.get("seq") or "")
        tick = str(item.get("tick") or "")
        received = _iso(item.get("received_at"))
        meta = item.get("meta") or {}
        names = ["mac", "received_at", "gw_mac", "sensor_type", "axis", "seq", "tick", "battery",
                 "sample_rate", "sample_count", "payload_bytes", "sensor_data_hex",
                 "min_khz", "interval_hz", "freq_start_khz", "freq_end_khz"]
        values = [
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
            _float(meta.get("minKhz")) if meta else None,
            _float(meta.get("intervalHz")) if meta else None,
            _float(meta.get("freq_start_khz")) if meta else None,
            _float(meta.get("freq_end_khz")) if meta else None,
        ]
        if self._packet_has_decoded:
            names.append("decoded_samples")
            values.append("")
        return mac, sensor_type, axis, seq, received, names, values

    def import_packets(self, sets):
        """옛 CSV 패킷 묶음을 packet에 넣는다(설정 'CSV 데이터 가져오기', csv_import.py).

        sets: [[item, ...], ...]. 묶음 하나 = ADXL X/Y/Z 한 세트(축이 빠질 수 있음) 또는 PIEZO·초음파 패킷 하나.
              item은 save_packet과 같은 모양(import_csv._item).
        돌려줌: {"saved": 넣은 패킷 수, "duplicates": 이미 있어 건너뛴 패킷 수, "lock_ms": 잠금을 잡은 시간}
        - source='csv'로 넣는다. 실시간 세트 묶기와 통계는 source별로 따로 보므로 수신 중에 넣어도 섞이지 않는다.
        - 중복: 같은 MAC·종류·축·수신 시각이 이미 packet에 있으면(실시간으로 받았거나 전에 가져옴) 그 묶음을
          통째로 건너뛴다. 세트 일부만 들어가 반쪽 세트가 생기지 않게 한다. 버튼을 여러 번 눌러도 안전하다.
        - set_key 'CSV|종류|MAC|n'(실시간 '종류|MAC|n'과 겹치지 않음), set_no n은 MAC·종류마다 가져온 줄의 최대+1부터.
        - device: 없는 MAC은 등록한다. 마지막 수신(last_at 등)은 이 묶음의 가장 늦은 시각이 지금 값보다 늦을 때만
          바꾼다(실시간 수신 값을 옛 값으로 덮지 않는다).
        잠금은 이 함수 안에서만 잡고 한 commit으로 끝낸다. 부르는 쪽이 HEX 약 1MB씩 나눠 불러,
        실시간 수신 저장이 기다리는 시간은 한 번에 그 commit 시간 정도다(결과 lock_ms).
        """
        prepared = []
        for group in sets:
            rows = [self._packet_values(item) + (item,) for item in group]
            if rows:
                prepared.append(rows)
        began = time.perf_counter()
        saved = duplicates = 0
        with self.lock:
            numbers = {}
            latest = {}
            first_kind = {}  # (mac, 종류) → 이 묶음에서 가장 이른 수신 시각(kind_view 기록 순서)
            # 중복 확인: MAC마다 이 묶음의 수신 시각 범위를 (mac, received_at) 인덱스로 한 번에 읽어 둔다.
            # 줄마다 따로 물으면 SQLite가 다른 인덱스를 골라 표 본문을 읽느라 묶음당 100ms 넘게 걸렸다(2026-11-24 측정).
            span = {}
            for rows in prepared:
                for mac, _type, _axis, _seq, received, _n, _v, _i in rows:
                    low, high = span.get(mac, (received, received))
                    span[mac] = (min(low, received), max(high, received))
            seen = set()
            for mac, (low, high) in span.items():
                for row in self._conn.execute(
                    "SELECT sensor_type, axis, received_at FROM packet INDEXED BY idx_packet_mac_time "
                    "WHERE mac=? AND received_at BETWEEN ? AND ?",
                    (mac, low, high),
                ):
                    seen.add((mac, row["sensor_type"], row["axis"], row["received_at"]))
            for rows in prepared:
                keys = [(mac, sensor_type, axis, received) for mac, sensor_type, axis, _seq, received, _n, _v, _i in rows]
                exists = any(key in seen for key in keys)
                seen.update(keys)  # 같은 묶음 안에서 두 폴더의 같은 줄도 한 번만
                if exists:
                    duplicates += len(rows)
                    continue
                mac, sensor_type = rows[0][0], rows[0][1]
                if (mac, sensor_type) not in numbers:
                    # 번호는 늘 최대+1로 매기므로 가장 큰 id 줄의 set_no가 최대다. MAX(set_no)로 찾으면 set_no가 큰 HEX 칸
                    # 뒤에 있어 그 MAC의 가져온 줄을 전부(넘침 페이지까지) 읽게 되어 잠금이 길어진다(2026-11-24 측정 0.7초).
                    row = self._conn.execute(
                        "SELECT set_no FROM packet WHERE mac=? AND sensor_type=? AND source='csv' ORDER BY id DESC LIMIT 1",
                        (mac, sensor_type),
                    ).fetchone()
                    numbers[(mac, sensor_type)] = int(row["set_no"] or 0) if row is not None else 0
                numbers[(mac, sensor_type)] += 1
                number = numbers[(mac, sensor_type)]
                key = f"CSV|{sensor_type}|{mac}|{number}"
                for _mac, _type, _axis, _seq, received, names, values, item in rows:
                    self._conn.execute(
                        f"INSERT INTO packet({', '.join(names + ['set_key', 'set_no', 'source'])}) "
                        f"VALUES ({', '.join('?' for _ in range(len(names) + 3))})",
                        values + [key, number, "csv"],
                    )
                    saved += 1
                    if received and ((mac, sensor_type) not in first_kind or received < first_kind[(mac, sensor_type)]):
                        first_kind[(mac, sensor_type)] = received
                    last = latest.get(mac)
                    if last is None or received > last[0]:
                        latest[mac] = (received, str(item.get("gw") or ""), str(item.get("battery") or ""), sensor_type)
            now = _iso()
            # 마지막 수신 정보는 이 묶음의 가장 늦은 시각이 지금 값보다 늦을 때만 바꾼다(빈 값 ''은 어떤 시각보다 작다).
            # 2026-11-24 고침: 전에는 'last_at이 비어 있을 때만' 채워서, 새 DB에 가져오면 첫 묶음(가장 옛 시각)에서 멈췄다
            # → 실시간 수신 전까지 센서 현황이 옛 시각 기준 '미수신'. 실시간 값은 늘 CSV보다 늦어 덮이지 않는다.
            for mac, (received, gw, battery, sensor_type) in latest.items():
                self._conn.execute(
                    """
                    INSERT INTO device(mac, created_at, updated_at, last_gw, last_battery, last_sensor, last_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(mac) DO UPDATE SET
                        last_gw=CASE WHEN excluded.last_at>device.last_at THEN excluded.last_gw ELSE device.last_gw END,
                        last_battery=CASE WHEN excluded.last_at>device.last_at THEN excluded.last_battery ELSE device.last_battery END,
                        last_sensor=CASE WHEN excluded.last_at>device.last_at THEN excluded.last_sensor ELSE device.last_sensor END,
                        last_at=CASE WHEN excluded.last_at>device.last_at THEN excluded.last_at ELSE device.last_at END
                    """,
                    (mac, now, now, gw, battery, sensor_type, received),
                )
            # 이 묶음에서 처음 본 종류를 kind_view에 기록한다(MAC마다 가장 이른 수신 순 → 빈 MAC은 첫 종류만 보임).
            for (mac, _kind), _first in sorted(first_kind.items(), key=lambda pair: (pair[0][0], pair[1])):
                kind_view.note_kind(self._conn, mac, _kind)
            self._conn.commit()
        return {"saved": saved, "duplicates": duplicates, "lock_ms": (time.perf_counter() - began) * 1000}

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
        # 이 MAC이 이 종류를 처음 받았으면 kind_view에 기록(첫 종류는 보임, 그 뒤 새 종류는 자동 숨김).
        kind_view.note_kind(self._conn, mac, sensor_type)


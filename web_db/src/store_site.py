"""대문(평면도) 대시보드와 앱 설정 저장. Store가 이 Mixin을 함께 상속한다.

테이블(스키마는 store._migrate)
- app_setting: 키 하나에 JSON 하나. 지금은 'mqtt'(설정 화면의 수신 MQTT 값) 하나.
- floor_plan: 평면도 이미지 정보. 여러 장(층·구역마다 한 장)이고 화면은 탭으로 고른다.
  이미지 파일은 DB가 아니라 site_dir 폴더에 floor_plan_{id}{확장자}로 둔다. 탭 순서는 sort_order.
- sensor_placement: MAC이 어느 평면도의 어디(x, y)에 있는지. x·y는 이미지 폭·높이에 대한
  비율(0~1)이라 화면 크기가 달라도 같은 자리에 그려진다. MAC 하나는 평면도 한 장에만 놓는다
  (replace_placements가 지킨다. 테이블 키는 (plan_id, mac)이라 DB만으로는 막지 않는다).

잠금 규칙은 store.Store와 같다. 모든 쿼리와 commit은 self.lock 안에서 한다.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from web_db.src.db_util import _iso

# 평면도로 받을 이미지 형식. 확장자 → 브라우저에 보낼 media type.
PLAN_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".svg": "image/svg+xml"}
PLAN_MAX_BYTES = 15 * 1024 * 1024


class SiteMixin:
    """app_setting, floor_plan, sensor_placement 읽기·쓰기."""

    @property
    def site_dir(self):
        """평면도 이미지 폴더. DB 파일 옆 site_files. 시험용 DB를 쓰면 그 옆에 생긴다."""
        path = Path(self.path).parent / "site_files"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---------- 앱 설정 ----------
    def get_setting(self, key, default=None):
        """app_setting[key]의 JSON 값. 없거나 깨졌으면 default."""
        with self.lock:
            row = self._conn.execute("SELECT value FROM app_setting WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def save_setting(self, key, value):
        """app_setting[key]에 JSON으로 저장한다(덮어쓰기)."""
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            self._conn.execute(
                """
                INSERT INTO app_setting(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, text, _iso()),
            )
            self._conn.commit()

    # ---------- 평면도 ----------
    # 평면도는 여러 장이다(층·구역마다 한 장). 화면 탭 순서는 sort_order → id.
    def floor_plans(self):
        """모든 평면도 [{id, name, file, width, height, sort_order, created_at, updated_at}] (탭 순서)."""
        with self.lock:
            rows = self._conn.execute("SELECT * FROM floor_plan ORDER BY sort_order, id").fetchall()
        return [dict(row) for row in rows]

    def floor_plan(self, plan_id):
        """평면도 한 장. 없으면 None."""
        with self.lock:
            row = self._conn.execute("SELECT * FROM floor_plan WHERE id=?", (int(plan_id),)).fetchone()
        return None if row is None else dict(row)

    @staticmethod
    def _check_plan_image(filename, data, width, height):
        """올린 이미지 확인. (확장자, 폭, 높이)를 돌려준다. 형식·크기가 틀리면 ValueError.

        filename은 확장자 확인에만 쓴다(사용자 파일 이름을 저장 경로에 쓰지 않는다).
        width, height는 브라우저가 읽은 원본 픽셀 크기다. 화면 비율을 맞추는 데 쓴다.
        """
        suffix = Path(str(filename or "")).suffix.lower()
        if suffix not in PLAN_TYPES:
            raise ValueError("평면도는 PNG, JPG, WEBP, SVG 이미지만 올릴 수 있습니다")
        if not data:
            raise ValueError("빈 파일입니다")
        if len(data) > PLAN_MAX_BYTES:
            raise ValueError("평면도 이미지는 15MB 이하여야 합니다")
        width = int(width or 0)
        height = int(height or 0)
        if width <= 0 or height <= 0:
            raise ValueError("이미지 크기를 읽지 못했습니다")
        return suffix, width, height

    @staticmethod
    def _plan_name(name):
        """평면도 이름(탭 글자) 정리. 공백을 하나로 줄이고 40자까지. 비면 ValueError."""
        text = " ".join(str(name or "").split())[:40]
        if not text:
            raise ValueError("평면도 이름이 필요합니다")
        return text

    def create_floor_plan(self, name, filename, data, width, height):
        """새 평면도를 맨 뒤 탭으로 추가한다. 이미지 저장 이름은 floor_plan_{id}{확장자}."""
        suffix, width, height = self._check_plan_image(filename, data, width, height)
        name = self._plan_name(name)
        now = _iso()
        with self.lock:
            order = self._conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM floor_plan").fetchone()[0]
            cursor = self._conn.execute(
                "INSERT INTO floor_plan(name, file, width, height, sort_order, created_at, updated_at)"
                " VALUES (?, '', ?, ?, ?, ?, ?)",
                (name, width, height, order, now, now),
            )
            plan_id = int(cursor.lastrowid)
            file_name = f"floor_plan_{plan_id}{suffix}"
            (self.site_dir / file_name).write_bytes(data)
            self._conn.execute("UPDATE floor_plan SET file=? WHERE id=?", (file_name, plan_id))
            self._conn.commit()
        return self.floor_plan(plan_id)

    def replace_floor_plan_image(self, plan_id, filename, data, width, height):
        """그 평면도의 이미지만 바꾼다. 이름과 센서 위치(비율)는 남는다. 없는 id면 KeyError."""
        suffix, width, height = self._check_plan_image(filename, data, width, height)
        current = self.floor_plan(plan_id)
        if current is None:
            raise KeyError(plan_id)
        file_name = f"floor_plan_{current['id']}{suffix}"
        with self.lock:
            # 새 파일을 먼저 쓰고 DB를 바꾼 뒤, 확장자가 달라 남은 옛 파일만 지운다.
            (self.site_dir / file_name).write_bytes(data)
            self._conn.execute(
                "UPDATE floor_plan SET file=?, width=?, height=?, updated_at=? WHERE id=?",
                (file_name, width, height, _iso(), current["id"]),
            )
            self._conn.commit()
        if current["file"] and current["file"] != file_name:
            (self.site_dir / current["file"]).unlink(missing_ok=True)
        return self.floor_plan(plan_id)

    def rename_floor_plan(self, plan_id, name):
        """평면도 이름(탭 글자)을 바꾼다. 없는 id면 KeyError."""
        name = self._plan_name(name)
        with self.lock:
            changed = self._conn.execute(
                "UPDATE floor_plan SET name=?, updated_at=? WHERE id=?", (name, _iso(), int(plan_id))
            ).rowcount
            self._conn.commit()
        if not changed:
            raise KeyError(plan_id)
        return self.floor_plan(plan_id)

    def move_floor_plan(self, plan_id, step):
        """탭 순서를 한 칸 앞(step<0)·뒤(step>0)로 옮긴다. 없는 id면 KeyError."""
        ids = [p["id"] for p in self.floor_plans()]
        if int(plan_id) not in ids:
            raise KeyError(plan_id)
        index = ids.index(int(plan_id))
        other = index + (1 if int(step) > 0 else -1)
        if 0 <= other < len(ids):
            ids[index], ids[other] = ids[other], ids[index]
        with self.lock:
            # 순서를 1, 2, 3…으로 다시 매긴다(옛 평면도의 sort_order가 모두 0이어도 맞게 된다).
            self._conn.executemany(
                "UPDATE floor_plan SET sort_order=? WHERE id=?", [(n, pid) for n, pid in enumerate(ids, 1)]
            )
            self._conn.commit()
        return self.floor_plans()

    def delete_floor_plan(self, plan_id):
        """평면도 한 장과 그 위의 센서 위치를 지운다. 그 센서들은 '배치 안 됨'으로 돌아간다."""
        current = self.floor_plan(plan_id)
        if current is None:
            return False
        with self.lock:
            self._conn.execute("DELETE FROM sensor_placement WHERE plan_id=?", (current["id"],))
            self._conn.execute("DELETE FROM floor_plan WHERE id=?", (current["id"],))
            self._conn.commit()
        if current["file"]:
            (self.site_dir / current["file"]).unlink(missing_ok=True)
        return True

    def floor_plan_file(self, plan_id):
        """(파일 경로, media type). 평면도가 없거나 파일이 사라졌으면 None."""
        current = self.floor_plan(plan_id)
        if current is None or not current["file"]:
            return None
        path = self.site_dir / current["file"]
        if not path.exists():
            return None
        return path, PLAN_TYPES.get(path.suffix.lower(), "application/octet-stream")

    # ---------- 센서 위치 ----------
    def placements(self):
        """모든 평면도의 센서 위치 [{plan_id, mac, x, y}]. MAC 하나는 평면도 한 장에만 있다."""
        with self.lock:
            rows = self._conn.execute(
                "SELECT plan_id, mac, x, y FROM sensor_placement ORDER BY plan_id, mac"
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_placements(self, items):
        """편집 화면의 '저장'. 모든 평면도의 위치를 받은 목록으로 통째로 바꾼다.

        items: [{plan_id, mac, x, y}]. x·y는 0~1로 자른다.
        - 센서 하나는 평면도 한 장에만 놓인다. 같은 MAC이 두 번 오면 뒤의 것(마지막으로 놓은 곳)을 쓴다.
        - 목록에 없는 MAC은 모든 평면도에서 빠진다('배치 안 됨'으로 돌아감).
        - 없는 평면도 id가 오면 ValueError(편집 중 다른 창에서 그 평면도를 지운 경우).
        """
        with self.lock:
            plan_ids = {row[0] for row in self._conn.execute("SELECT id FROM floor_plan")}
        clean = {}
        for item in items or []:
            item = item or {}
            mac = str(item.get("mac") or "").strip()
            try:
                plan_id = int(item.get("plan_id"))
                x = float(item.get("x"))
                y = float(item.get("y"))
            except (TypeError, ValueError):
                raise ValueError(f"{mac or 'MAC'} 위치가 숫자가 아닙니다") from None
            if not mac or not (math.isfinite(x) and math.isfinite(y)):
                raise ValueError("MAC과 위치가 필요합니다")
            if plan_id not in plan_ids:
                raise ValueError(f"평면도 {plan_id}번이 없습니다. 새로고침 후 다시 배치해 주세요")
            clean.pop(mac, None)  # 뒤에 온 값이 '마지막으로 놓은 곳'이 되게 순서도 뒤로.
            clean[mac] = (plan_id, min(1.0, max(0.0, x)), min(1.0, max(0.0, y)))
        now = _iso()
        with self.lock:
            self._conn.execute("DELETE FROM sensor_placement")
            self._conn.executemany(
                "INSERT INTO sensor_placement(plan_id, mac, x, y, updated_at) VALUES (?, ?, ?, ?, ?)",
                [(plan_id, mac, x, y, now) for mac, (plan_id, x, y) in clean.items()],
            )
            self._conn.commit()
        return self.placements()

    # ---------- 상태 판정용 읽기 ----------
    def site_rows(self):
        """대문 상태 API가 쓰는 MAC별 원자료를 한 번에 읽는다.

        - device: 이름, 마지막 수신 시각·센서 종류·배터리
        - sensor_config: 마지막 설정 JSON(웹이 보낸 것 + 브로커 retain으로 받은 것)
        - measurement(ADXL): 통계 화면과 같은 SRSS 전체 평균(세트 수 가중), 마지막 통계 시각
        - packet: 최근 패킷 120개의 (수신 시각, 종류, 축)(연결 판정용 수신 간격). 측정으로 묶는 일은 site_status가 한다.
        """
        with self.lock:
            devices = self._conn.execute(
                """
                SELECT d.mac, d.display_name, d.last_at, d.last_sensor, d.last_battery,
                       c.payload AS config, c.updated_at AS config_at, c.action AS config_action,
                       SUM(m.set_count) AS set_count,
                       SUM(m.srss_mms * m.set_count) / SUM(m.set_count) AS srss_mms,
                       MAX(m.last_at) AS stat_last_at
                FROM device d
                LEFT JOIN sensor_config c ON c.mac=d.mac
                LEFT JOIN measurement m ON m.mac=d.mac AND m.sensor_type='ADXL'
                GROUP BY d.mac
                ORDER BY d.created_at, d.mac
                """
            ).fetchall()
            result = []
            for row in devices:
                item = dict(row)
                item["limit"] = self._read_srss_limit(item["mac"])
                # 최근 패킷 120개의 (시각, 종류, 축). 측정 한 번 묶기(ADXL X→Y→Z)는 site_status가 한다.
                # sensor_type·axis는 행 앞쪽 열이라 뒤쪽의 큰 HEX·샘플 열(overflow 페이지)은 읽지 않는다.
                # 15초마다 MAC 수만큼 부르므로 이렇게 가볍게 둔다.
                times = self._conn.execute(
                    "SELECT received_at AS t, sensor_type, axis FROM packet WHERE mac=? ORDER BY received_at DESC LIMIT 120",
                    (item["mac"],),
                ).fetchall()
                item["recent_times"] = [(r["t"], r["sensor_type"], r["axis"]) for r in reversed(times) if r["t"]]
                result.append(item)
        return result

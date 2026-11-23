"""data_csv_web의 CSV로 시간별 통계(measurement) 다시 만들기(2026-11-23 전에는 일자별).

왜: 운영 서버에는 DB 없이 수신 CSV만 쌓여 있을 수 있다. 그 누적 CSV로 통계 화면·대문 알람을
채우려면 CSV를 직접 읽어 통계를 내야 한다. 설정 화면 '통계 데이터' 탭의 버튼이 부른다.

무엇을 읽나
- data_csv_web 아래(하위 폴더 포함)의 `{MAC}_ADXL-X/Y/Z_sensorData_hex.csv` 세 파일 묶음. 폴더·MAC마다 한 묶음.
  다른 PC에서 가져온 CSV 폴더도 data_csv_web 안에 넣으면 같이 읽는다.
- X/Y/Z 묶기는 CSV 분석 화면과 같은 load_adxl_csv_xyz(packet_no 세트 번호 → 안 되면 seq).
  깨진 HEX는 복구하지 않고 그 축을 뺀다(세 축이 다 있는 세트만 통계에 들어간다).
- 세트 계산은 매시간 통계와 같은 stat_profile.set_stats이고, 기준은 DB의 가장 최근 통계 기준 버전이다.
  그래서 값은 DB 패킷으로 낸 통계와 같다(필터 HPF/LPF, MEMS 보정, g/LSB, Welch PSD).
- 같은 CSV가 두 폴더에 복사돼 있어도 한 번만 센다(MAC + 세 축 수신 시각이 같으면 같은 세트).

어떻게 넣나 (store.replace_measurements_from_csv)
- 세트의 시 = 세 축 중 마지막 수신 시각의 시(YYYY-MM-DDTHH:00:00). MAC·시간마다 measurement 한 줄
  (set_key = `CSV|{버전}|{MAC}|{시}`). 통계 화면 작은 그래프의 점 하나 = 한 시간.
- 끝나면 그 기준 버전이 '지금 버전'이 된다.
- measurement를 **전부 지우고** CSV 결과로 바꾼다. CSV가 원본 전체라는 전제다
  (수신 중 CSV 저장을 끈 기간이 있으면 그 기간은 통계에서 빠진다).
- SQLite packet에 이미 있는 패킷은 CSV에도 있으므로, 통계 읽기 위치(stat_cursor)를 시작 순간의
  마지막 packet id로 옮긴다. 그래서 매시간 스케줄러가 같은 패킷을 두 번 세지 않고, 그 뒤 수신분만 더한다.
- CSV에서 세트를 하나도 못 찾으면 아무것도 지우지 않는다.
- device에 없는 MAC은 등록한다(통계 화면 목록은 device 기준이다).

한 번에 하나만 돈다(Store.stats_lock을 잡으므로 매시간 스케줄러·재계산과도 겹치지 않는다).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from pathlib import Path

from web_db.src import stat_profile as sp
from web_db.src.db_util import _iso
from web_db.src.load_file import load_adxl_csv_xyz
from web_db.src.paths import DATA_CSV



def find_groups(folder=DATA_CSV):
    """[(폴더, MAC, {"X": 경로, "Y": 경로, "Z": 경로})]. Y·Z 파일이 없는 묶음은 뺀다.

    파일 이름은 대소문자를 가리지 않는다. Windows는 원래 안 가리지만 Linux(Docker)는 가려서,
    '.CSV'나 'adxl-x'처럼 저장된 파일을 놓치지 않게 이름을 소문자로 바꿔 비교한다.
    """
    suffix = {axis: f"_adxl-{axis.lower()}_sensordata_hex.csv" for axis in ("X", "Y", "Z")}
    found = {}  # (폴더, MAC 소문자) → {축: 경로}
    names = {}  # (폴더, MAC 소문자) → 원래 MAC 글자
    for path in Path(folder).rglob("*"):
        if not path.is_file():
            continue
        lower = path.name.lower()
        for axis, end in suffix.items():
            if lower.endswith(end):
                mac = path.name[: -len(end)]
                key = (path.parent, mac.lower())
                found.setdefault(key, {})[axis] = str(path)
                names.setdefault(key, mac)
    groups = []
    for key in sorted(found, key=lambda k: (str(k[0]), k[1])):
        if len(found[key]) == 3:
            groups.append((key[0], names[key], found[key]))
    return groups


def compute(folder=DATA_CSV, settings=None, progress=None):
    """CSV 전체를 읽어 MAC·시간별 세트 통계를 모은다. DB는 건드리지 않는다.

    settings: 통계 기준(stat_profile.validate 결과). 없으면 첫 버전 기본값.
    돌려주는 dict
      hours: {(mac, 시): [(세트 통계, 세트 시각, 0)...]}, groups, sets, failed, duplicates, skipped_groups
    progress(done, total)가 있으면 묶음 하나 끝날 때마다 부른다.
    """
    settings = settings or sp.default_settings()
    groups = find_groups(folder)
    hours = defaultdict(list)
    seen = set()
    result = {"groups": len(groups), "sets": 0, "failed": 0, "duplicates": 0, "skipped_groups": []}
    for index, (parent, mac, paths) in enumerate(groups, 1):
        try:
            sets = load_adxl_csv_xyz(paths, recover_partial=False)
        except Exception as exc:  # 형식이 다른 CSV, 세트가 하나도 없는 묶음 등
            result["skipped_groups"].append(f"{parent.name}/{mac}: {exc}")
            sets = []
        for found in sets:
            if not all(axis in found for axis in ("X", "Y", "Z")):
                result["failed"] += 1
                continue
            stamps = tuple(str(found[axis].get("_received_at") or "") for axis in ("X", "Y", "Z"))
            when = max(stamps)
            if not when:
                result["failed"] += 1
                continue
            key = (mac, stamps)
            if key in seen:
                result["duplicates"] += 1
                continue
            seen.add(key)
            try:
                stats = sp.set_stats({axis: found[axis] for axis in ("X", "Y", "Z")}, settings)
                hour = sp.hour_bucket(when)
            except (ValueError, TypeError, OverflowError, ZeroDivisionError, KeyError):
                result["failed"] += 1
                continue
            hours[(mac, hour)].append((stats, when, 0))
            result["sets"] += 1
        if progress:
            progress(index, len(groups))
    result["hours"] = hours
    return result


def rows_for_store(computed):
    """compute 결과 → measurement에 넣을 줄 목록(MAC, 시 순). CSV에는 packet id가 없어 first_id는 0."""
    rows = []
    for (mac, hour) in sorted(computed["hours"]):
        row = sp.row_from_sets(computed["hours"][(mac, hour)])
        row.update({"mac": mac, "hour": hour})
        rows.append(row)
    return rows


class CsvStatsJob:
    """설정 화면 버튼이 시작하는 백그라운드 작업. 프로세스에 하나. state()로 진행을 본다."""

    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._thread = None
        self._state = {"running": False, "phase": "", "done": 0, "total": 0, "result": None, "error": "",
                       "started_at": "", "finished_at": ""}

    def state(self):
        with self._lock:
            return dict(self._state)

    def _set(self, **values):
        with self._lock:
            self._state.update(values)

    def start(self, folder=DATA_CSV):
        """작업을 시작한다. 이미 돌고 있으면 False."""
        with self._lock:
            if self._state["running"]:
                return False
            self._state = {"running": True, "phase": "CSV 찾는 중", "done": 0, "total": 0, "result": None,
                           "error": "", "started_at": _iso(), "finished_at": ""}
        self._thread = threading.Thread(target=self._run, args=(folder,), name="csv-stats", daemon=True)
        self._thread.start()
        return True

    def _run(self, folder):
        began = time.monotonic()
        try:
            # 하루 스케줄러와 겹치지 않게 통계 잠금을 처음부터 끝까지 잡는다.
            with self.store.stats_lock:
                cursor = self.store.adxl_packet_marks()  # 시작 순간 MAC별 마지막 packet id
                profile_id = self.store.ensure_stat_profile()  # 가장 최근 기준 버전
                profile = self.store.stat_profile(profile_id)
                self._set(phase=f"CSV 읽고 계산하는 중 (기준 v{profile_id})")
                computed = compute(folder, profile["settings"],
                                   progress=lambda done, total: self._set(done=done, total=total))
                rows = rows_for_store(computed)
                summary = {
                    "groups": computed["groups"],
                    "sets": computed["sets"],
                    "failed": computed["failed"],
                    "duplicates": computed["duplicates"],
                    "skipped_groups": computed["skipped_groups"][:20],
                    "hours": len(rows),
                    "macs": len({row["mac"] for row in rows}),
                    "first_hour": min((row["hour"] for row in rows), default=""),
                    "last_hour": max((row["hour"] for row in rows), default=""),
                    "profile_id": profile_id,
                }
                if not rows:
                    summary["replaced"] = False
                    summary["message"] = "CSV에서 통계를 낼 X/Y/Z 세트를 찾지 못해 기존 통계를 그대로 두었습니다."
                else:
                    self._set(phase="통계 저장 중")
                    self.store.replace_measurements_from_csv(rows, cursor, profile_id, profile["settings"]["g_per_lsb"])
                    summary["replaced"] = True
                    summary["message"] = (f"{summary['macs']}개 MAC, {summary['hours']}시간치 통계를 "
                                          f"기준 v{profile_id}로 만들었습니다.")
            summary["seconds"] = round(time.monotonic() - began, 1)
            self._set(running=False, phase="완료", result=summary, finished_at=_iso())
        except Exception as exc:
            self._set(running=False, phase="실패", error=str(exc), finished_at=_iso())

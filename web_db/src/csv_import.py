"""data_csv_web의 누적 수신 CSV를 SQLite packet으로 가져오기(2026-11-24, 설정 > 통계 데이터 'CSV 데이터 가져오기').

왜: 이전 버전 웹은 SQLite를 쓰지 않아 운영 서버에는 수신 CSV만 한 달치쯤 쌓여 있다. 그 CSV를 packet에 넣어 두면
- 통계는 '지금 기준으로 다시 계산' 한 번으로 모든 기간이 같은 길(packet → 시간별 통계)로 계산되고,
- 센서 데이터 분석(DB) 화면에서도 옛 기간을 열 수 있다.
(2026-11-23까지 있던 'CSV로 시간별 통계 다시 만들기'(csv_stats.py)는 이 기능으로 바뀌었다. 그 방식은 packet 밖 통계라
 '다시 계산'을 누르면 CSV 기간 통계가 지워지는 문제가 있었다.)

무엇을 읽나
- data_csv_web 아래(하위 폴더 포함) 모든 `{MAC}_{종류}_sensorData_hex.csv`(ADXL-X/Y/Z, PIEZO, ULTRASONIC). 대소문자 무시.
  `_decoded_samples.csv`는 HEX를 푼 결과라 읽지 않는다(DB에는 HEX만 둔다).
- 한 줄 → 패킷 하나. 디코드·칸 해석은 명령줄 import_csv._item과 같다(실시간 수신과 같은 디코드 함수로 HEX 확인).
  디코드가 안 되는 줄은 건너뛰고 결과에 몇 개인지 남긴다.

수신을 막지 않게(한 달치를 넣는 동안에도 실시간 수신·화면은 그대로)
- 파일은 한 줄씩 흘려 읽는다(파일 전체를 메모리에 올리지 않는다). 폴더마다 그 폴더 파일들을 수신 시각 순으로 합쳐 읽는다.
- CSV 읽기·HEX 확인은 DB 잠금 없이 하고, 약 CHUNK_BYTES(HEX 1MB, ADXL 약 10세트)씩 모아 Store.import_packets로 넣는다.
  잠금은 그 한 번(중복 확인 + INSERT + commit)만 잡는다. 실시간 저장(save_packet)은 길어야 그 시간만 기다린다.
  결과의 max_lock_ms가 그 최댓값이다. 묶음 사이에 잠깐 쉬어 수신 스레드가 먼저 잡을 수 있게 한다.
- CSV 저장(CsvWriter 스레드)은 DB와 상관없어 가져오기 중에도 그대로 쌓인다.

세트·통계가 실시간 줄과 섞이지 않게
- 가져온 줄은 packet.source='csv', set_key 'CSV|종류|MAC|n'. ADXL은 실시간 수신과 같은 규칙(grouping.continues_set,
  seq 하위 4비트가 X→Y→Z로 이어짐)으로 MAC마다 세트를 묶고, 세트를 통째로 한 번에 넣는다.
- 실시간 세트 묶기와 매시간 통계·재계산은 source별로 따로 읽는다(grouping.assign_set, store_query._compute_adxl).
- 중복: 같은 MAC·종류·축·수신 시각이 이미 있으면(실시간으로 받은 줄, 전에 가져온 줄, 다른 폴더의 같은 CSV) 건너뛴다.
  그래서 여러 번 눌러도, 새 웹이 이미 DB에 넣은 기간이 CSV에 같이 있어도 두 번 들어가지 않는다.
  막지 못하는 것: 재송신. 옛 파형을 다시 보내면 수신기가 새 수신 시각으로 CSV에 남겨 실측처럼 들어간다(로컬
  data_csv_web 5,303줄 중 2,192줄, 2026-11-24). 재송신·시험 폴더는 가져오기 전에 data_csv_web 밖으로 옮긴다.

통계는 여기서 계산하지 않는다. 끝나면 '지금 기준으로 다시 계산'을 누른다(그 전에도 매시간 통계가 가져온 줄을 새 줄로
보고 시간 줄에 더하지만, 2026-11-24 전 CSV 통계 줄이 남아 있으면 겹쳐 보이므로 다시 계산으로 정리한다).
재계산·기준 저장과는 동시에 돌지 않는다(routes/db.py가 서로 409로 막는다).
"""

from __future__ import annotations

import csv
import heapq
import threading
import time
from pathlib import Path

from web_db.src.db_util import _iso
from web_db.src.grouping import continues_set, seq_low
from web_db.src.import_csv import _item
from web_db.src.paths import DATA_CSV

# HEX 한 칸이 수십 KB라 csv 모듈 기본 한계(128KB)를 넘을 수 있다.
csv.field_size_limit(2**31 - 1)

# 한 번에 넣는 양. 잠금 시간은 거의 commit(디스크에 쓰기)이라 HEX 글자 수로 끊는다(세트 경계에서 끊으므로 조금 넘을 수 있다).
# 2026-11-24 측정(로컬 SSD): 200패킷(약 7MB)씩이면 묶음 잠금 최대 0.47초 → 1MB(ADXL 약 30패킷 = 10세트)로 줄였다.
CHUNK_BYTES = 1_000_000
CHUNK = 200  # 작은 패킷(초음파 등)이 많을 때의 패킷 수 상한
SUFFIX = "_sensordata_hex.csv"
AXIS_ORDER = {"X": 0, "Y": 1, "Z": 2}


def find_files(folder=DATA_CSV):
    """{폴더: [경로...]} — 폴더 아래(하위 폴더 포함) 모든 *_sensorData_hex.csv. 폴더·파일 이름 순."""
    found = {}
    root = Path(folder)
    if not root.exists():
        return {}
    for path in root.rglob("*"):
        if path.is_file() and path.name.lower().endswith(SUFFIX):
            found.setdefault(path.parent, []).append(path)
    return {parent: sorted(found[parent], key=lambda p: p.name.lower()) for parent in sorted(found, key=str)}


def scan(folder=DATA_CSV):
    """버튼 옆 안내: {'files': 파일 수, 'folders': 폴더 수, 'mb': 합친 크기(MB)}."""
    files = find_files(folder)
    paths = [path for group in files.values() for path in group]
    return {"files": len(paths), "folders": len(files),
            "mb": round(sum(path.stat().st_size for path in paths) / 1e6, 1)}


def _read(path, index, sizes):
    """한 파일을 한 줄씩: ((수신 시각, 축 순서, 파일 번호, 줄 번호), 줄 dict). sizes[0]에 읽은 대략 바이트를 더한다."""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for order, row in enumerate(csv.DictReader(handle)):
            sizes[0] += len(row.get("sensorData_hex") or "") + 200
            kind = str(row.get("sensor_type") or "")
            axis = kind.split("-")[-1] if kind.startswith("ADXL-") else ""
            yield (str(row.get("received_at") or ""), AXIS_ORDER.get(axis, 9), index, order), row


class _Grouper:
    """흘러오는 패킷을 넣을 묶음으로 만든다. ADXL은 MAC마다 세트(continues_set), 나머지는 패킷 하나가 한 묶음."""

    def __init__(self):
        self.open = {}  # MAC → (세트 item 목록, 마지막 축, 마지막 seq 하위값)
        self.ready = []  # 다 묶인 묶음
        self.count = 0  # ready 안의 패킷 수
        self.bytes = 0  # ready 안의 HEX 글자 수

    def add(self, item):
        if not str(item["kind"]).startswith("ADXL-"):
            self._close_group([item])
            return
        mac, axis, low = item["device"], item["axis"], seq_low(item["seq"])
        current = self.open.get(mac)
        if current and continues_set(current[1], current[2], axis, low):
            current[0].append(item)
            self.open[mac] = (current[0], axis, low)
            return
        if current:
            self._close_group(current[0])
        self.open[mac] = ([item], axis, low)

    def close_all(self):
        for group, _axis, _low in self.open.values():
            self._close_group(group)
        self.open.clear()

    def _close_group(self, group):
        self.ready.append(group)
        self.count += len(group)
        self.bytes += sum(len(item["sensor_data"]) for item in group)

    def take(self):
        groups, self.ready, self.count, self.bytes = self.ready, [], 0, 0
        return groups


def import_folder(store, folder=DATA_CSV, progress=None, log_limit=20):
    """CSV 전체를 packet에 넣는다. 결과 dict(saved, duplicates, skipped, ...). progress(done, total, 폴더 이름)."""
    files = find_files(folder)
    total_bytes = sum(path.stat().st_size for group in files.values() for path in group) or 1
    sizes = [0]
    result = {"folders": len(files), "files": sum(len(group) for group in files.values()), "rows": 0, "saved": 0,
              "duplicates": 0, "skipped": 0, "skipped_examples": [], "by_type": {}, "first_at": "", "last_at": "",
              "max_lock_ms": 0.0, "chunks": 0}

    def flush(grouper):
        groups = grouper.take()
        if not groups:
            return
        done = store.import_packets(groups)
        result["saved"] += done["saved"]
        result["duplicates"] += done["duplicates"]
        result["max_lock_ms"] = max(result["max_lock_ms"], done["lock_ms"])
        result["chunks"] += 1
        time.sleep(0.005)  # 수신 스레드가 잠금을 먼저 잡을 기회

    for number, (parent, paths) in enumerate(files.items(), 1):
        grouper = _Grouper()
        streams = [_read(path, index, sizes) for index, path in enumerate(paths)]
        for _key, row in heapq.merge(*streams, key=lambda pair: pair[0]):
            result["rows"] += 1
            try:
                item = _item(row)
                if not item["device"] or not item["received_at"]:
                    raise ValueError("MAC 또는 수신 시각이 비어 있음")
            except Exception as exc:  # 디코드 실패, 모르는 종류 등: 실시간 수신도 이런 패킷은 저장하지 않는다
                result["skipped"] += 1
                if len(result["skipped_examples"]) < log_limit:
                    result["skipped_examples"].append(f"{parent.name} packet_no={row.get('packet_no')}: {exc}")
                continue
            kind = "ADXL" if item["kind"].startswith("ADXL-") else item["kind"]
            result["by_type"][kind] = result["by_type"].get(kind, 0) + 1
            when = item["received_at"]
            result["first_at"] = min(result["first_at"] or when, when)
            result["last_at"] = max(result["last_at"], when)
            grouper.add(item)
            if grouper.count >= CHUNK or grouper.bytes >= CHUNK_BYTES:
                flush(grouper)
                if progress:
                    progress(min(sizes[0], total_bytes), total_bytes, parent.name, number)
        grouper.close_all()
        flush(grouper)
        if progress:
            progress(min(sizes[0], total_bytes), total_bytes, parent.name, number)
    return result


class CsvImportJob:
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
        self._thread = threading.Thread(target=self._run, args=(folder,), name="csv-import", daemon=True)
        self._thread.start()
        return True

    def _run(self, folder):
        began = time.monotonic()
        try:
            folders = len(find_files(folder))

            def progress(done, total, name, number):
                self._set(done=done, total=total, phase=f"가져오는 중 ({number}/{folders} 폴더, {name})")

            result = import_folder(self.store, folder, progress=progress)
            result["seconds"] = round(time.monotonic() - began, 1)
            result["max_lock_ms"] = round(result["max_lock_ms"], 1)
            if result["saved"]:
                result["message"] = (f"{result['saved']}개 패킷을 DB에 넣었습니다. "
                                     "'지금 기준으로 다시 계산'을 눌러 통계를 만드세요.")
            elif result["rows"]:
                result["message"] = "새로 넣을 패킷이 없습니다(모두 이미 DB에 있음)."
            else:
                result["message"] = "가져올 CSV 줄을 찾지 못했습니다."
            self._set(running=False, phase="완료", result=result, finished_at=_iso())
        except Exception as exc:
            self._set(running=False, phase="실패", error=str(exc), finished_at=_iso())

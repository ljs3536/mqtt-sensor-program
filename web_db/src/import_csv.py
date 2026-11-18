"""CSV 폴더 → SQLite packet 다시 채우기.

데스크톱·웹 수신기가 남긴 CSV 폴더(예: D:\\20261106_100046)를 실시간 수신과 같은 길로 DB에 넣는다.

실행 (8081 서버를 끈 상태에서):
    python -m web_db.src.import_csv D:\\20261106_100046 --reset
    python -m web_db.src.import_csv D:\\20261106_100046            # 지우지 않고 뒤에 더 넣기

읽는 파일: 폴더 안의 `{MAC}_{종류}_sensorData_hex.csv`만 읽는다.
- HEX가 수신 원본이다. `_decoded_samples.csv`는 HEX를 푼 결과라서 읽지 않고,
  실시간 수신(MonitorService._remember)과 같은 디코드 함수로 HEX를 다시 푼다.
  그래야 DB의 decoded_samples가 실시간으로 받은 것과 똑같아진다.
- received_at, seq, tick, 배터리, samplerate, numofsample, payload_bytes는 CSV 값을 그대로 쓴다.

넣는 순서: 모든 파일의 줄을 received_at 순으로 섞어서 넣는다.
- store.save_packet → assign_set이 "그 MAC의 직전 패킷"과 seq를 비교해 X/Y/Z 세트 번호를 정하므로,
  MAC 안에서는 받은 순서대로 들어가야 세트가 실시간과 똑같이 묶인다.
- 시각이 같으면 파일 안 순서(packet_no) → 축(X, Y, Z) 순.

--reset (지울 때만):
- 먼저 DB 파일을 `sensor.sqlite.bak-YYYYMMDD-HHMMSS`로 복사해 둔다(WAL을 합친 뒤 복사).
- 지우는 것: packet, measurement, stat_cursor, stat_meta(수신 원본과 거기서 계산된 통계).
  device의 마지막 수신 정보(last_gw, last_battery, last_sensor, last_at)는 비운 뒤 가져오면서 다시 채운다.
- 남기는 것: device 표시 이름, srss_limit 한계값, sensor_config 설정 JSON, app_setting(MQTT),
  floor_plan·sensor_placement(평면도와 배치). 사용자가 직접 정한 값이라 데이터와 따로 둔다.
- 그 밖에 MAC이 비어 있는 device 줄(옛 스키마에서 남은 빈 줄)은 지운다.

--no-stats: 가져온 뒤 통계 최신화(update_statistics)를 하지 않는다. 기본은 한 번 돌려서
  대문·통계 화면의 SRSS 알람 값이 바로 보이게 한다.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from web_db.src import core as mon
from web_db.src.store import DB_PATH, Store

# HEX 한 칸이 수십 KB라 csv 모듈 기본 한계(128KB)를 넘을 수 있다.
csv.field_size_limit(2**31 - 1)

AXIS_ORDER = {"X": 0, "Y": 1, "Z": 2}


def _rows(folder):
    """폴더의 모든 *_sensorData_hex.csv 줄을 (정렬 키, 파일 이름, 줄 dict)로 모은다."""
    result = []
    for path in sorted(Path(folder).glob("*_sensorData_hex.csv")):
        with open(path, encoding="utf-8-sig", newline="") as handle:
            for order, row in enumerate(csv.DictReader(handle)):
                axis = str(row.get("axis") or "")
                key = (str(row.get("received_at") or ""), order, AXIS_ORDER.get(axis, 9))
                result.append((key, path.name, row))
    result.sort(key=lambda item: item[0])
    return result


def _item(row):
    """CSV 한 줄 → store.save_packet이 받는 item. 디코드는 MonitorService._remember와 같은 함수.

    실패하면 ValueError(이유). 그 줄은 넣지 않고 건너뛴다(실시간 수신도 디코드 실패 패킷은 저장하지 않는다).
    """
    kind = str(row.get("sensor_type") or "").strip()
    sensor_hex = "".join(str(row.get("sensorData_hex") or "").split())
    count = row.get("numofsample") or None
    meta = None
    if kind == "ULTRASONIC":
        samples, _freq, meta = mon.decode_ultrasonic_samples(sensor_hex)
        decoded = np.asarray(samples, dtype=np.float64)
        axis = "H"
    elif kind == "PIEZO":
        decoded = mon.decode_piezo_samples(sensor_hex, count)
        axis = str(row.get("axis") or "Z")
    elif kind.startswith("ADXL-"):
        decoded = mon.decode_vibration_samples(sensor_hex, count)
        axis = kind.split("-")[-1]
    else:
        raise ValueError(f"모르는 sensor_type {kind!r}")
    if len(decoded) == 0:
        raise ValueError("샘플 0개")
    return {
        "device": str(row.get("dvicMacAddr") or "").strip(),
        "kind": kind,
        "axis": axis,
        "seq": str(row.get("seq") or ""),
        "tick": str(row.get("tick") or ""),
        "battery": str(row.get("batteryRmin") or ""),
        "gw": str(row.get("gwMacAddr") or ""),
        "sample_rate": row.get("samplerate") or "",
        "num_samples": row.get("numofsample") or "",
        "payload_bytes": row.get("payload_bytes") or "",
        "sensor_data": sensor_hex,
        "decoded": np.asarray(decoded),
        "meta": meta,
        "received_at": str(row.get("received_at") or ""),
    }


def backup(path=DB_PATH):
    """WAL을 본 파일에 합친 뒤 DB 파일을 같은 폴더에 복사한다. 복사본 경로를 돌려준다."""
    path = Path(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    target = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, target)
    return target


def reset_data(store):
    """수신 원본과 통계만 지운다. 표시 이름·한계값·설정·평면도는 남긴다(모듈 설명 참고)."""
    with store.lock:
        conn = store._conn
        counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                  for name in ("packet", "measurement", "stat_cursor", "stat_meta")}
        for name in counts:
            conn.execute(f"DELETE FROM {name}")
        # AUTOINCREMENT 번호도 처음부터(통계 읽기 위치 through_id가 id를 기준으로 한다).
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('packet', 'measurement')")
        conn.execute("UPDATE device SET last_gw='', last_battery='', last_sensor='', last_at=''")
        counts["device(빈 MAC)"] = conn.execute("DELETE FROM device WHERE mac IS NULL OR TRIM(mac)=''").rowcount
        conn.commit()
    return counts


def import_folder(store, folder, log=print):
    """폴더의 CSV를 받은 순서대로 넣는다. {'saved': n, 'skipped': [...], 'by_mac': {...}}."""
    rows = _rows(folder)
    saved = 0
    skipped = []
    by_mac = {}
    for index, (_key, name, row) in enumerate(rows, 1):
        try:
            item = _item(row)
        except Exception as exc:
            skipped.append(f"{name} packet_no={row.get('packet_no')}: {exc}")
            continue
        store.save_packet(item)
        saved += 1
        by_mac[item["device"]] = by_mac.get(item["device"], 0) + 1
        if index % 200 == 0:
            log(f"  {index}/{len(rows)}")
    return {"total": len(rows), "saved": saved, "skipped": skipped, "by_mac": by_mac}


def main(argv=None):
    """명령줄 진입점."""
    parser = argparse.ArgumentParser(description="CSV 폴더를 SQLite packet에 넣는다")
    parser.add_argument("folder", help="*_sensorData_hex.csv가 있는 폴더")
    parser.add_argument("--reset", action="store_true", help="넣기 전에 packet·통계를 지운다(백업 후)")
    parser.add_argument("--no-stats", action="store_true", help="넣은 뒤 통계 최신화를 하지 않는다")
    parser.add_argument("--db", default=str(DB_PATH), help="DB 파일(기본 web_db/sensor.sqlite)")
    args = parser.parse_args(argv)

    folder = Path(args.folder)
    if not any(folder.glob("*_sensorData_hex.csv")):
        print(f"{folder}에 *_sensorData_hex.csv가 없습니다")
        return 1
    if args.reset:
        print(f"백업: {backup(args.db)}")
    store = Store(args.db)
    try:
        if args.reset:
            print(f"지움: {reset_data(store)}")
        result = import_folder(store, folder)
        print(f"넣음 {result['saved']}/{result['total']}줄")
        for mac, count in sorted(result["by_mac"].items()):
            print(f"  {mac}: {count}")
        for text in result["skipped"]:
            print(f"  건너뜀 {text}")
        if not args.no_stats:
            print(f"통계 최신화: {store.update_statistics()}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

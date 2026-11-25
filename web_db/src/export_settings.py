"""설정만 담은 새 SQLite 만들기(2026-11-24, 운영 서버 첫 배포용).

왜: 운영 서버는 SQLite를 새로 만들고, 수신 원본(packet)은 운영 서버에 쌓인 CSV를 'CSV 데이터 가져오기'로 넣는다.
    그런데 새 DB에는 표시 이름이 없어서, 이름이 없는 센서를 숨기는 규칙(2026-11-24) 때문에 센서 현황·통계·분석에
    아무것도 보이지 않는다. 로컬에서 정해 둔 이름·위치·구간·평면도·통계 기준만 따로 옮기면 그 문제가 없다.

실행 (원본 DB는 서버가 켜져 있어도 읽기만 하므로 괜찮다. 새 파일은 아직 없는 경로여야 한다):
    python -m web_db.src.export_settings D:\\deploy\\sensor.sqlite
    python -m web_db.src.export_settings D:\\deploy\\sensor.sqlite --from web_db\\sensor.sqlite --with-mqtt
  만든 폴더(sensor.sqlite + site_files)를 운영 서버 데이터 폴더(Docker면 SENSOR_DATA_DIR=/data, 아니면 web_db/)에
  서버를 켜기 전에 넣는다. 그다음 설정 > 통계 데이터 'CSV 데이터 가져오기' → '지금 기준으로 다시 계산'.

옮기는 것 (사용자가 정한 값)
| 표·파일 | 내용 | 비고 |
| --- | --- | --- |
| device | 표시 이름, 위치, 표시(visible), 처음 본 시각 | 마지막 수신 정보(last_*)는 비운다. CSV 가져오기·실시간 수신이 다시 채운다 |
| srss_limit | MAC별 위험 기준·양호/주의 상한 | 옮기는 장치의 줄만(device에서 지운 MAC의 줄은 빼고, 2026-11-25) |
| stat_profile + app_setting 'stat_profile_active' | 통계 계산 기준 버전(같은 id) | 운영 서버의 그래프 설정 파일이 달라도 통계 기준이 같다 |
| floor_plan, sensor_placement, site_files/ | 평면도(이미지)와 센서 위치, 센서 사진 | |
| app_setting 'mqtt' | 브로커 주소·자동 연결 | --with-mqtt일 때만. 로컬 주소(127.0.0.1)를 운영에 옮기지 않게 기본은 뺀다 |
| sensor_config | MAC별 마지막 설정 JSON | --with-sensor-config일 때만. 운영 브로커의 retain(S/MAC)이 구독 순간 다시 채운다 |

옮기지 않는 것: packet(수신 원본), measurement·stat_cursor·stat_meta(통계), site_state·site_state_history(상태 기록).
device는 사용자 값이 있는 MAC만 옮긴다(이름·위치가 있거나, 표시를 껐거나, 구간·평면도 위치를 정한 MAC).
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from web_db.src.paths import DB_FILE

# 옮기는 표 순서(외래 키: sensor_placement → floor_plan).
TABLES = ("device", "srss_limit", "stat_profile", "app_setting", "floor_plan", "sensor_placement", "sensor_config")
# device에서 비우는 칸(수신해야 생기는 값).
DEVICE_BLANK = {"last_gw": "", "last_battery": "", "last_sensor": "", "last_at": ""}


def _columns(conn, schema, table):
    """그 표의 열 이름 목록. 표가 없으면 []."""
    return [row[1] for row in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def export(source, target, with_mqtt=False, with_sensor_config=False):
    """source DB의 설정만 담은 새 DB를 target에 만든다. {표: 옮긴 줄 수, 'files': 복사한 파일 수}."""
    source, target = Path(source), Path(target)
    if not source.exists():
        raise SystemExit(f"원본 DB가 없습니다: {source}")
    if target.exists():
        raise SystemExit(f"새 DB 경로에 파일이 이미 있습니다: {target} (덮어쓰지 않습니다. 다른 경로를 주거나 지운 뒤 다시)")
    if source.resolve() == target.resolve():
        raise SystemExit("원본과 새 DB가 같은 파일입니다")

    # 1. 새 DB에 지금 코드의 스키마를 만든다(서버가 켜질 때와 같은 _migrate).
    from web_db.src.store import Store  # paths 확인(쓰기 가능 폴더)을 거치므로 여기서 읽는다

    Store(target).close()

    # 2. 원본을 읽기 전용으로 붙여 표마다 같은 이름의 열만 옮긴다(원본이 옛 스키마여도 된다).
    conn = sqlite3.connect(target)
    src =sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    counts = {}
    try:
        src_tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # 사용자 값이 있는 MAC(device 줄을 옮길 MAC).
        keep = set()
        if "device" in src_tables:
            device_cols = _columns(src, "main", "device")
            visible = "visible=0" if "visible" in device_cols else "0"
            location = "TRIM(location)<>''" if "location" in device_cols else "0"
            keep |= {row[0] for row in src.execute(
                f"SELECT mac FROM device WHERE TRIM(display_name)<>'' OR {location} OR {visible}")}
        if "srss_limit" in src_tables:
            limits = {row[0] for row in src.execute("SELECT mac FROM srss_limit")}
            # srss_limit 키는 대문자·구분자 없는 MAC(store.limit_key). device.mac과 같은 모양이면 그대로 맞는다.
            keep |= {row[0] for row in src.execute("SELECT mac FROM device")
                     if str(row[0] or "").upper().replace(":", "").replace("-", "") in limits}
        if "sensor_placement" in src_tables:
            keep |= {row[0] for row in src.execute("SELECT mac FROM sensor_placement")}

        for table in TABLES:
            if table not in src_tables:
                continue
            if table == "sensor_config" and not with_sensor_config:
                continue
            target_cols = _columns(conn, "main", table)
            cols = [c for c in _columns(src, "main", table) if c in target_cols]
            rows = [dict(row) for row in src.execute(f"SELECT {', '.join(cols)} FROM {table}")]
            if table == "device":
                rows = [dict(row, **{k: v for k, v in DEVICE_BLANK.items() if k in cols}) for row in rows
                        if row["mac"] in keep]
            elif table == "sensor_config":
                rows = [row for row in rows if row["mac"] in keep]
            elif table == "srss_limit":
                # 옮기는 장치의 진동 구간만(2026-11-25). device에서 지운 시험용 MAC(예: TEST00000001)의 줄이 따라가지 않게.
                # srss_limit 키는 대문자·구분자 없는 MAC이라 같은 모양으로 맞춘다.
                kept = {str(mac or "").upper().replace(":", "").replace("-", "") for mac in keep}
                rows = [row for row in rows if str(row["mac"] or "").upper() in kept]
            elif table == "app_setting" and not with_mqtt:
                rows = [row for row in rows if row["key"] != "mqtt"]
            conn.execute(f"DELETE FROM {table}")  # 새 DB라 비어 있지만, 스키마가 기본 줄을 넣어도 원본 값으로
            conn.executemany(
                f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [[row[c] for c in cols] for row in rows],
            )
            counts[table] = len(rows)
        conn.commit()
        # 평면도에 배치된 센서인데 device 줄이 없는 MAC은 없어야 한다(keep에 넣었으므로). 확인만 한다.
        missing = conn.execute(
            "SELECT COUNT(*) FROM sensor_placement p LEFT JOIN device d ON d.mac=p.mac WHERE d.mac IS NULL").fetchone()[0]
        if missing:
            counts["경고: device 없는 배치"] = missing
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        src.close()
        conn.close()

    # 3. 평면도 이미지·센서 사진(DB 옆 site_files, store_site.site_dir).
    files = 0
    src_dir, dst_dir = source.parent / "site_files", target.parent / "site_files"
    if src_dir.exists():
        dst_dir.mkdir(parents=True, exist_ok=True)
        for path in src_dir.rglob("*"):
            if path.is_file():
                out = dst_dir / path.relative_to(src_dir)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, out)
                files += 1
    counts["files"] = files
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description="설정(이름·위치·구간·평면도·통계 기준)만 담은 새 SQLite를 만든다.")
    parser.add_argument("target", help="새로 만들 DB 파일(없는 경로). 옆에 site_files 폴더도 만든다")
    parser.add_argument("--from", dest="source", default=str(DB_FILE), help="원본 DB(기본: 서버가 쓰는 DB)")
    parser.add_argument("--with-mqtt", action="store_true", help="MQTT 연결 설정(app_setting 'mqtt')도 옮긴다")
    parser.add_argument("--with-sensor-config", action="store_true", help="MAC별 마지막 설정 JSON(sensor_config)도 옮긴다")
    args = parser.parse_args(argv)
    counts = export(args.source, args.target, args.with_mqtt, args.with_sensor_config)
    print(f"만듦: {args.target}")
    for name, count in counts.items():
        print(f"  {name}: {count}")
    if not args.with_mqtt:
        print("  (MQTT 설정은 옮기지 않았습니다. 운영 서버 설정 화면에서 브로커 주소와 자동 연결을 정하세요.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

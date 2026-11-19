"""프로젝트 경로.

코드(읽기 전용)와 데이터(쓰기)를 나눈다.
- ROOT, FRONT: 소스와 화면 파일. Docker 이미지 안에서는 바뀌지 않는다.
- 데이터: SQLite(평면도 이미지 site_files는 DB 옆), 수신 CSV(data_csv_web), MEMS 보정 JSON.
  환경변수 SENSOR_DATA_DIR가 있으면 모두 그 폴더 하나에 둔다(Docker 볼륨 /data).
  없으면 지금까지처럼 저장소 안 위치를 쓴다(Windows에서 바로 실행할 때).

  | 데이터 | SENSOR_DATA_DIR 없음 | SENSOR_DATA_DIR=/data |
  | --- | --- | --- |
  | SQLite | web_db/sensor.sqlite | /data/sensor.sqlite |
  | 평면도 이미지 | web_db/site_files/ | /data/site_files/ |
  | 수신 CSV | data_csv_web/ | /data/data_csv_web/ |
  | MEMS 보정 JSON | 저장소 루트 | /data (없으면 저장소 루트 기본 파일을 읽음) |
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONT = Path(__file__).resolve().parents[1] / "front"

_DATA_ENV = os.environ.get("SENSOR_DATA_DIR", "").strip()
DATA_DIR = Path(_DATA_ENV).resolve() if _DATA_ENV else None

DB_FILE = (DATA_DIR / "sensor.sqlite") if DATA_DIR else (ROOT / "web_db" / "sensor.sqlite")
SETTINGS_DIR = DATA_DIR or ROOT
# 수신 CSV 폴더 이름은 로컬·개발 서버·운영 서버 모두 data_csv_web 하나로 쓴다(2026-11-19).
# 운영 서버(testSensor)에 이미 있는 폴더 이름이라, Docker로 바꿔도 그 폴더를 그대로 읽고 쓴다.
DATA_CSV = SETTINGS_DIR / "data_csv_web"


def _check_writable(folder):
    """폴더를 만들고 실제로 써 본다. 못 쓰면 원인을 한 줄로 알리고 서버를 멈춘다.

    Linux(Docker)에서 자주 나는 경우: 서버 PC의 ./sensor_web_db, ./data_csv_web를 Docker가 대신 만들면 root 소유가 되어
    컨테이너 사용자(uid 1000)가 쓰지 못한다. 그때 긴 Traceback 대신 해결 방법을 보여 준다.
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise SystemExit(
            f"[sensor-web] 데이터 폴더에 쓸 수 없습니다: {folder} ({exc.strerror}). "
            f"서버 PC에서 그 폴더의 주인·권한을 확인하세요(예: sudo chown -R $(id -u):$(id -g) sensor_web_db data_csv_web)."
        ) from None


if DATA_DIR is not None:
    _check_writable(DATA_DIR)
_check_writable(DATA_CSV)

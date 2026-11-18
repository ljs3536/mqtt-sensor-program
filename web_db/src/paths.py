"""프로젝트 경로.

코드(읽기 전용)와 데이터(쓰기)를 나눈다.
- ROOT, FRONT: 소스와 화면 파일. Docker 이미지 안에서는 바뀌지 않는다.
- 데이터: SQLite(평면도 이미지 site_files는 DB 옆), 수신 CSV(data_csv), MEMS 보정 JSON.
  환경변수 SENSOR_DATA_DIR가 있으면 모두 그 폴더 하나에 둔다(Docker 볼륨 /data).
  없으면 지금까지처럼 저장소 안 위치를 쓴다(Windows에서 바로 실행할 때).

  | 데이터 | SENSOR_DATA_DIR 없음 | SENSOR_DATA_DIR=/data |
  | --- | --- | --- |
  | SQLite | web_db/sensor.sqlite | /data/sensor.sqlite |
  | 평면도 이미지 | web_db/site_files/ | /data/site_files/ |
  | 수신 CSV | data_csv/ | /data/data_csv/ |
  | MEMS 보정 JSON | 저장소 루트 | /data (없으면 저장소 루트 기본 파일을 읽음) |
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONT = Path(__file__).resolve().parents[1] / "front"

_DATA_ENV = os.environ.get("SENSOR_DATA_DIR", "").strip()
DATA_DIR = Path(_DATA_ENV).resolve() if _DATA_ENV else None
if DATA_DIR is not None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE = (DATA_DIR / "sensor.sqlite") if DATA_DIR else (ROOT / "web_db" / "sensor.sqlite")
SETTINGS_DIR = DATA_DIR or ROOT
DATA_CSV = SETTINGS_DIR / "data_csv"
DATA_CSV.mkdir(exist_ok=True)

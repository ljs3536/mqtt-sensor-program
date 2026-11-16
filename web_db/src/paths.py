"""프로젝트 경로. web 패키지를 쓰지 않는다."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONT = Path(__file__).resolve().parents[1] / "front"
DATA_CSV = ROOT / "data_csv"
DATA_CSV.mkdir(exist_ok=True)

"""서버에 올릴 두 파일 만들기: sensor-web.tar(이미지) + docker-compose.yml.

    python docker/make_bundle.py              # 이미지 빌드 → docker-bundle/ 에 두 파일
    python docker/make_bundle.py --no-build   # 이미 빌드한 sensor-web:latest 를 저장만

서버(로컬·개발·운영 공통):
    두 파일을 data_csv_web 이 있는(또는 생길) 폴더에 넣고
    docker load -i sensor-web.tar && docker compose up -d --force-recreate

폴더(./data_csv_web, ./sensor_web_db)와 실행 사용자는 docker-compose.yml 과 이미지 안 시작 스크립트
(docker/entrypoint.sh)가 정한다. 설치 스크립트나 .env 는 없다.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docker-bundle"
IMAGE = "sensor-web:latest"


def run(*args):
    """명령을 실행하고 실패하면 멈춘다."""
    print(">", " ".join(args), flush=True)
    subprocess.run(args, check=True, cwd=ROOT)


def main(argv=None):
    parser = argparse.ArgumentParser(description="sensor-web.tar + docker-compose.yml 만들기")
    parser.add_argument("--no-build", action="store_true", help="빌드하지 않고 이미 있는 sensor-web:latest 를 저장")
    args = parser.parse_args(argv)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    if not args.no_build:
        run("docker", "build", "-f", "web_db/Dockerfile", "-t", IMAGE, ".")  # Dockerfile은 web_db/ 안(2026-11-27), 기준 폴더는 루트
    run("docker", "save", "-o", str(OUT / "sensor-web.tar"), IMAGE)
    shutil.copy2(ROOT / "docker-compose.yml", OUT / "docker-compose.yml")
    size = (OUT / "sensor-web.tar").stat().st_size / 1e6
    print(f"완료: {OUT}\\sensor-web.tar ({size:.0f}MB), docker-compose.yml")
    print("서버: 두 파일을 같은 폴더에 두고  docker load -i sensor-web.tar && docker compose up -d --force-recreate")
    return 0


if __name__ == "__main__":
    sys.exit(main())

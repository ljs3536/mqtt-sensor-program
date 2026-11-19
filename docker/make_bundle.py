"""다른 PC로 옮길 Docker 묶음 만들기.

소스가 있는 이 PC에서 실행한다(Docker와 Python 필요):
    python docker/make_bundle.py              # 이미지 빌드 + DB 등 데이터 포함
    python docker/make_bundle.py --no-data    # 이미지와 compose만(데이터 없이 새로 시작)

만들어지는 docker-bundle/ (git 제외)
    sensor-web.tar        docker save 결과(이미지)
    docker-compose.yml    실행 파일(빌드 없이 이미지만 씀)
    data/sensor.sqlite    지금 DB 복사본(서버가 켜져 있어도 안전하게 복사: SQLite backup API)
    data/site_files/      평면도 이미지
    data/*.json           MEMS 보정 JSON(있는 것만)
    data_csv/             빈 폴더(수신 CSV가 여기 쌓인다)
    설치방법.txt

옮긴 PC에서는 폴더째 복사한 뒤:
    docker load -i sensor-web.tar
    docker compose up -d
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docker-bundle"
IMAGE = "sensor-web:latest"
JSON_FILES = ("mems_lsb_scale.json", "mems_frequency_sensitivity.json")

GUIDE = """센서 웹 Docker 설치

1. 이 폴더(docker-bundle)를 통째로 설치할 PC에 복사한다.
2. 이미지 넣기:            docker load -i sensor-web.tar
3. (Linux만) 폴더 권한:     sudo chown -R 1000:1000 data data_csv
   컨테이너는 uid 1000 사용자로 돈다. 이 권한이 없으면 DB·CSV를 쓰지 못한다.
4. 실행:                   docker compose up -d
5. 브라우저:               http://<이 PC 주소>:8090  (포트를 바꾸려면 .env 에 WEB_PORT=포트)
6. 설정 화면 '수신 MQTT'에 브로커 주소를 넣고 저장한다(같은 PC 브로커면 host.docker.internal).
   CSV 저장을 켜면 수신 CSV가 이 폴더의 data_csv/ 에 쌓인다.

데이터 위치
  data/sensor.sqlite   DB (옮길 때 만든 복사본에서 시작한다)
  data/site_files/     평면도 이미지
  data_csv/            수신 CSV

새 버전으로 바꿀 때: 새 sensor-web.tar만 다시 load 하고 docker compose up -d (data, data_csv는 그대로 둔다).
멈추기: docker compose down   로그: docker compose logs -f
"""


def run(*args):
    """명령을 실행하고 실패하면 멈춘다."""
    print(">", " ".join(args), flush=True)
    subprocess.run(args, check=True, cwd=ROOT)


def copy_db(source: Path, target: Path):
    """SQLite backup API로 복사한다. 서버가 켜져 있어도 커밋된 내용까지 한 파일로 안전하게 복사된다."""
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")  # -wal 파일 없이 한 파일로 남긴다(컨테이너가 다시 WAL로 연다).
    finally:
        dst.close()
        src.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="다른 PC로 옮길 Docker 묶음(docker-bundle/) 만들기")
    parser.add_argument("--no-data", action="store_true", help="DB·평면도·보정 JSON을 넣지 않는다")
    parser.add_argument("--no-build", action="store_true", help="이미지를 다시 빌드하지 않는다(이미 있는 sensor-web:latest 저장)")
    args = parser.parse_args(argv)

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "data").mkdir(parents=True)
    (OUT / "data_csv").mkdir()

    if not args.no_build:
        run("docker", "build", "-t", IMAGE, ".")
    run("docker", "save", "-o", str(OUT / "sensor-web.tar"), IMAGE)
    shutil.copy2(ROOT / "docker-compose.yml", OUT / "docker-compose.yml")
    (OUT / "설치방법.txt").write_text(GUIDE, encoding="utf-8")

    if not args.no_data:
        db = ROOT / "web_db" / "sensor.sqlite"
        if db.exists():
            copy_db(db, OUT / "data" / "sensor.sqlite")
            print("DB 복사:", db)
        plans = ROOT / "web_db" / "site_files"
        if plans.exists():
            shutil.copytree(plans, OUT / "data" / "site_files")
            print("평면도 복사:", plans)
        for name in JSON_FILES:
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, OUT / "data" / name)
                print("보정 JSON 복사:", name)

    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()) / 1e6
    print(f"완료: {OUT} ({size:.0f}MB). 폴더째 옮긴 뒤 설치방법.txt 순서대로.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

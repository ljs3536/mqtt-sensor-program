"""DB 정리: 쓰지 않는 packet.decoded_samples 열을 지우고 파일을 줄인다(2026-11-23).

왜: decoded_samples(HEX를 푼 샘플 글자)는 DB 크기의 절반쯤인데, 이제 읽는 곳이 없다.
    통계·분석 화면 모두 sensor_data_hex를 직접 푼다. 서버는 새 줄에 빈 글자만 넣는다(store.save_packet).
    이미 쌓인 값을 지우려면 packet 표를 통째로 다시 써야 해서, 서버 시작 때 자동으로 하지 않고 이 명령으로 한다.

실행: 서버를 멈춘 뒤 (DB를 다른 프로그램이 열고 있으면 안 된다)
    python -m web_db.src.compact_db                 # 기본 DB(SENSOR_DATA_DIR 또는 web_db/sensor.sqlite)
    python -m web_db.src.compact_db D:\\path\\sensor.sqlite
  Docker:
    docker compose stop web
    docker compose run --rm web python -m web_db.src.compact_db
    docker compose up -d

하는 일
1. 먼저 같은 폴더에 `sensor.sqlite.bak-날짜-시각` 백업을 만든다(--no-backup으로 끔).
2. ALTER TABLE packet DROP COLUMN decoded_samples (열이 없으면 건너뜀)
3. VACUUM: 비운 자리를 실제로 돌려줘 파일이 작아진다. 잠깐 DB 크기만큼 빈 디스크가 더 필요하다.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from web_db.src.paths import DB_FILE


def _size(path):
    """DB 본 파일과 WAL을 더한 크기(MB)."""
    total = 0
    for suffix in ("", "-wal"):
        item = Path(str(path) + suffix)
        if item.exists():
            total += item.stat().st_size
    return total / 1e6


def main(argv=None):
    parser = argparse.ArgumentParser(description="쓰지 않는 packet.decoded_samples 열을 지우고 DB 파일을 줄인다.")
    parser.add_argument("db", nargs="?", default=str(DB_FILE), help="SQLite 파일(기본: 서버가 쓰는 DB)")
    parser.add_argument("--no-backup", action="store_true", help="백업 파일을 만들지 않는다")
    args = parser.parse_args(argv)
    path = Path(args.db)
    if not path.exists():
        print(f"DB 파일이 없습니다: {path}")
        return 1
    before = _size(path)
    conn = sqlite3.connect(path, timeout=5)
    try:
        # 다른 프로세스(서버)가 쓰고 있으면 여기서 멈춘다.
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        print("DB를 다른 프로그램(서버 등)이 쓰고 있습니다. 서버를 멈춘 뒤 다시 실행하세요.")
        conn.close()
        return 1
    columns = {row[1] for row in conn.execute("PRAGMA table_info(packet)")}
    if "decoded_samples" not in columns:
        print(f"이미 정리된 DB입니다(decoded_samples 없음). 크기 {before:.1f} MB")
        conn.close()
        return 0
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    if not args.no_backup:
        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        print(f"백업: {backup}")
    conn = sqlite3.connect(path)
    print("decoded_samples 열을 지우는 중… (packet 표를 다시 씁니다)")
    conn.execute("ALTER TABLE packet DROP COLUMN decoded_samples")
    conn.commit()
    print("VACUUM 중… (빈 자리를 돌려줍니다)")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    print(f"끝: {before:.1f} MB → {_size(path):.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

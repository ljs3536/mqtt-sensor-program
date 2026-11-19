#!/bin/sh
# 컨테이너 시작 스크립트(이미지 안). 서버에서는 tar와 docker-compose.yml만 있으면 되게,
# 예전 install.sh가 하던 "폴더 주인과 같은 사용자로 실행"을 여기서 한다.
#
# 1. 수신 CSV 폴더(/data/data_csv_web = 서버의 ./data_csv_web)의 주인 uid/gid를 읽는다.
#    운영 서버면 infob, 로컬 Windows Docker면 보통 root(0).
# 2. 그 주인이 root가 아니면 DB 폴더(/data = ./sensor_web_db)도 그 주인으로 맞춘 뒤
#    그 사용자로 서버를 실행한다. 새로 쓰는 CSV·DB 파일이 기존 파일과 같은 주인이 된다
#    (root 파일이 섞이면 나중에 infob가 지우거나 옮기지 못한다).
#    CSV 폴더 안의 기존 파일 주인은 바꾸지 않는다.
# 3. 주인이 root(폴더를 Docker가 대신 만든 경우 포함)면 root로 그대로 실행한다.
set -e

CSV_DIR=/data/data_csv_web
mkdir -p /data "$CSV_DIR"

uid=$(stat -c %u "$CSV_DIR")
gid=$(stat -c %g "$CSV_DIR")

if [ "$(id -u)" = "0" ] && [ "$uid" != "0" ]; then
  # DB 폴더 자체와 그 안의 파일(CSV 폴더는 빼고)을 같은 주인으로.
  chown "$uid:$gid" /data
  for item in /data/* /data/.[!.]*; do
    [ -e "$item" ] || continue
    [ "$item" = "$CSV_DIR" ] && continue
    chown -R "$uid:$gid" "$item"
  done
  echo "[sensor-web] 실행 사용자 uid=$uid gid=$gid (수신 CSV 폴더 주인)"
  exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "$@"
fi

echo "[sensor-web] 실행 사용자 uid=$(id -u) (수신 CSV 폴더 주인이 root라 그대로)"
exec "$@"

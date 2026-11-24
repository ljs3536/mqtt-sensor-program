# web_db(센서 웹) 이미지. 데스크톱 프로그램(PySide6 등)은 넣지 않는다.
#
# 빌드:  docker build -t sensor-web:latest .
# 저장:  docker save -o sensor-web.tar sensor-web:latest      (python docker/make_bundle.py 가 둘 다 한다)
# 서버:  sensor-web.tar 와 docker-compose.yml 을 같은 폴더에 두고
#        docker load -i sensor-web.tar && docker compose up -d --force-recreate
#
# 데이터는 /data 아래(SENSOR_DATA_DIR, web_db/src/paths.py). compose가 서버 폴더를 연결한다.
#   /data              = ./sensor_web_db  (sensor.sqlite, site_files/ 평면도, MEMS 보정 JSON)
#   /data/data_csv_web = ./data_csv_web   (수신 CSV. 로컬·개발·운영 모두 같은 이름)
# 시작할 때 docker/entrypoint.sh 가 CSV 폴더 주인과 같은 사용자로 바꿔 실행한다(설치 스크립트 불필요).
#
# 서버는 반드시 프로세스 하나(uvicorn worker 1개)로 돈다. MQTT 수신 상태, SQLite 잠금,
# 매시간 통계 스레드가 모두 한 프로세스 메모리에 있기 때문이다(--workers를 늘리지 않는다).

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SENSOR_DATA_DIR=/data \
    TZ=Asia/Seoul

WORKDIR /app

# 시각대: 매시간 통계의 '시' 묶음과 화면 시각을 한국 시각으로 맞춘다. slim 이미지에는 tzdata가 없다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

# 소스. .dockerignore가 DB·CSV·평면도·가상환경·데스크톱 파일을 뺀다.
# 기본 MEMS JSON 두 개(1 LSB당 g, 주파수·감도 보정). /data에 같은 이름 파일이 없으면 이것을 읽는다.
# 통계 기준 첫 버전도 이 값으로 만든다(2026-11-23). 보정 JSON이 빠져 있으면 코드 기본값(나눗수 1.014)이 쓰였다.
COPY mems_lsb_scale.json mems_frequency_sensitivity.json ./
COPY web_db ./web_db

# 시작 스크립트: 수신 CSV 폴더 주인으로 사용자를 바꿔 실행한다(setpriv는 기본 이미지에 있다).
# Windows에서 받아도 되게 줄 끝 CR을 지운다.
COPY docker/entrypoint.sh /usr/local/bin/sensor-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/sensor-entrypoint \
    && chmod 755 /usr/local/bin/sensor-entrypoint \
    && mkdir -p /data/data_csv_web
# 사용자를 바꿔도 홈 폴더가 없어 생기는 문제가 없게.
ENV HOME=/tmp
VOLUME ["/data"]
ENTRYPOINT ["/usr/local/bin/sensor-entrypoint"]

EXPOSE 8081

# 대문 상태 API가 응답하면 정상. 수신(MQTT) 연결 여부와는 상관없다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/api/site/status', timeout=4)"

CMD ["python", "-m", "uvicorn", "web_db.server:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]

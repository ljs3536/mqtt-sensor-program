# web_db(센서 웹) 이미지. 데스크톱 프로그램(PySide6 등)은 넣지 않는다.
#
# 빌드:  docker build -t sensor-web .
# 실행:  docker run -d --name sensor-web -p 8081:8081 -v sensor-data:/data sensor-web
#
# 데이터는 모두 /data 볼륨에 남는다(SENSOR_DATA_DIR, web_db/src/paths.py).
#   /data/sensor.sqlite, /data/site_files/(평면도), /data/data_csv/(수신 CSV), MEMS 보정 JSON
# 컨테이너를 지우거나 이미지를 새로 받아도 볼륨만 그대로면 데이터는 남는다.
#
# 서버는 반드시 프로세스 하나(uvicorn worker 1개)로 돈다. MQTT 수신 상태, SQLite 잠금,
# 하루 통계 스레드가 모두 한 프로세스 메모리에 있기 때문이다(--workers를 늘리지 않는다).

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SENSOR_DATA_DIR=/data \
    TZ=Asia/Seoul

WORKDIR /app

# 시각대: 하루 통계(자정 뒤)와 화면 시각을 한국 시각으로 맞춘다. slim 이미지에는 tzdata가 없다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

# 소스. .dockerignore가 DB·CSV·평면도·가상환경·데스크톱 파일을 뺀다.
COPY mems_lsb_scale.json ./
COPY web_db ./web_db

# root가 아닌 사용자로 실행. /data는 이 사용자가 쓸 수 있게 만든다.
RUN useradd --create-home --uid 1000 sensor \
    && mkdir -p /data \
    && chown -R sensor:sensor /data
USER sensor
VOLUME ["/data"]

EXPOSE 8081

# 대문 상태 API가 응답하면 정상. 수신(MQTT) 연결 여부와는 상관없다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/api/site/status', timeout=4)"

CMD ["python", "-m", "uvicorn", "web_db.server:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]

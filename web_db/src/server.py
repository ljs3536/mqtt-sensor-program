"""web_db HTTP 입구. 화면은 front, 구현은 역할별 모듈이다.

실행: .venv\\Scripts\\python.exe -m uvicorn web_db.server:app --host 127.0.0.1 --port 8081
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_db.src.paths import FRONT
from web_db.src.routes.analysis import router as analysis_router
from web_db.src.routes.db import router as db_router
from web_db.src.routes.db import start_daily_stats
from web_db.src.routes.generator import router as generator_router
from web_db.src.routes.monitor import router as monitor_router
from web_db.src.routes.pages import router as pages_router
from web_db.src.routes.site import router as site_router
from web_db.src.runtime import monitor, store
from web_db.src.site_watch import start_site_watch

app = FastAPI(title="sensor-program-db")
# 응답 압축(gzip). 2026-11-26: 운영 서버를 LTE로 밖에서 볼 때 데이터 사용량을 줄이려고 켰다.
# 브라우저가 'Accept-Encoding: gzip'을 보내면 서버가 압축해 보내고 브라우저가 알아서 푼다(화면 코드 변경 없음).
# 그래프 JSON(숫자 글자)은 약 1/4.5로 줄어든다(수신 모니터 417KB → 약 91KB). 1KB 미만 응답은 그대로 보낸다.
# 이 서버에는 스트리밍(SSE) 응답이 없어 압축이 실시간 전달을 막는 곳은 없다.
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory=FRONT / "static"), name="static")
app.include_router(pages_router)
app.include_router(generator_router)
app.include_router(monitor_router)
app.include_router(analysis_router)
app.include_router(db_router)
app.include_router(site_router)

start_daily_stats()
# 센서 상태가 바뀔 때마다 기록(대문 '최근 알림', 카드의 '3시간째 끊김'). 1분마다, 수신 중일 때만.
start_site_watch(store, monitor)
# 설정 화면에서 '서버 시작 때 자동 연결'을 켜 두었으면 그 MQTT 값으로 수신을 시작한다.
# 백그라운드 연결이라 브로커가 꺼져 있어도 서버는 뜨고, paho가 계속 다시 붙기를 시도한다.
try:
    monitor.auto_connect()
except Exception as exc:  # 설정이 깨져 있어도 서버는 켜져야 한다
    monitor.log(f"MQTT 자동 연결 실패: {exc}")

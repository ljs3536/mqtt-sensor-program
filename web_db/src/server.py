"""web_db HTTP 입구. 화면은 front, 구현은 역할별 모듈이다.

실행: .venv\\Scripts\\python.exe -m uvicorn web_db.server:app --host 127.0.0.1 --port 8081
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
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
from web_db.src.runtime import monitor

app = FastAPI(title="sensor-program-db")
app.mount("/static", StaticFiles(directory=FRONT / "static"), name="static")
app.include_router(pages_router)
app.include_router(generator_router)
app.include_router(monitor_router)
app.include_router(analysis_router)
app.include_router(db_router)
app.include_router(site_router)

start_daily_stats()
# 설정 화면에서 '서버 시작 때 자동 연결'을 켜 두었으면 그 MQTT 값으로 수신을 시작한다.
# 백그라운드 연결이라 브로커가 꺼져 있어도 서버는 뜨고, paho가 계속 다시 붙기를 시도한다.
try:
    monitor.auto_connect()
except Exception as exc:  # 설정이 깨져 있어도 서버는 켜져야 한다
    monitor.log(f"MQTT 자동 연결 실패: {exc}")

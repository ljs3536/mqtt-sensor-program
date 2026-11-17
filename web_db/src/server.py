"""web_db HTTP 입구. 화면은 front, 구현은 역할별 모듈이다.

실행: .venv\\Scripts\\python.exe -m uvicorn web_db.server:app --host 127.0.0.1 --port 8081
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_db.src.paths import FRONT
from web_db.src.routes.analysis import router as analysis_router
from web_db.src.routes.db import _daily_stats
from web_db.src.routes.db import router as db_router
from web_db.src.routes.generator import router as generator_router
from web_db.src.routes.monitor import router as monitor_router
from web_db.src.routes.pages import router as pages_router

app = FastAPI(title="sensor-program-db")
app.mount("/static", StaticFiles(directory=FRONT / "static"), name="static")
app.include_router(pages_router)
app.include_router(generator_router)
app.include_router(monitor_router)
app.include_router(analysis_router)
app.include_router(db_router)

threading.Thread(target=_daily_stats, name="daily-stats", daemon=True).start()

"""8081 진입점. 구현은 src, 화면은 front. web 패키지를 쓰지 않는다.

실행: .venv\\Scripts\\python.exe -m uvicorn web_db.server:app --host 127.0.0.1 --port 8081
"""

from web_db.src.server import app

__all__ = ["app"]

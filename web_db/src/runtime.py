"""프로세스 안에서 하나씩만 만드는 수신·송신·DB 객체."""

from web_db.src.monitor_service import MonitorService
from web_db.src.replay import ReplayService
from web_db.src.store import Store

store = Store()
replay = ReplayService()
monitor = MonitorService(store)

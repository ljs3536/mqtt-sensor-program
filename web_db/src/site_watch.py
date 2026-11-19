"""센서 상태 기록 스레드. 1분마다 대문과 같은 판정을 하고, 바뀐 것만 site_state_history에 남긴다.

왜 서버 스레드인가: 화면(대문)이 열려 있지 않아도 "언제 끊겼는지, 언제 경보가 났다가 풀렸는지"를
남겨야 하기 때문이다. 판정은 site_status.build_status 그대로라 화면에 보이는 상태와 같다.

수신(MQTT)이 끊겨 있으면 기록하지 않는다. 그때는 모든 센서가 '대기'로 보이는데, 이것은 센서가
아니라 서버 쪽 사정이라 기록하면 서버를 켜고 끌 때마다 가짜 변화가 쌓인다.

서버 프로세스에 하나만 돈다(start_site_watch가 두 번 불려도 새로 만들지 않는다).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from web_db.src import site_status

CHECK_SECONDS = 60
_thread = None


def receiver_info(monitor):
    """수신 연결 상태 요약. 대문 위쪽 점·안내와 상태 판정('수신 꺼짐')에 쓴다."""
    return {
        "connected": bool(monitor.connected),
        "host": monitor.host,
        "port": monitor.port,
        "origin": monitor.origin,
        "active": monitor.client is not None,
    }


def check_once(store, monitor, now=None):
    """한 번 판정해 기록한다. 수신이 꺼져 있으면 아무것도 하지 않고 []를 돌려준다."""
    receiver = receiver_info(monitor)
    if not receiver["connected"]:
        return []
    now = now or datetime.now()
    status = site_status.build_status(store.site_rows(), receiver, now=now)
    return store.record_states(status["sensors"], now.isoformat(timespec="milliseconds"))


def _loop(store, monitor):
    while True:
        try:
            check_once(store, monitor)
        except Exception:
            pass  # 한 번 실패해도 스레드는 살려 두고 다음 확인을 기다린다.
        time.sleep(CHECK_SECONDS)


def start_site_watch(store, monitor):
    """상태 기록 스레드를 하나만 켠다."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _thread = threading.Thread(target=_loop, args=(store, monitor), name="site-watch", daemon=True)
    _thread.start()
    return _thread

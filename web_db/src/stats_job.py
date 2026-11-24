"""통계 재계산 백그라운드 작업(2026-11-23).

언제 도나
- 설정 화면에서 통계 기준을 저장하면(새 버전) 그 버전으로 packet 전체를 다시 계산한다.
- 설정 화면 '지금 기준으로 다시 계산' 버튼.
- 서버 시작 때 지금 버전이 없으면(하루 통계에서 업그레이드한 직후) 첫 버전으로 한 번.

SQLite를 붙잡지 않는다: 계산은 store.rebuild_statistics가 300줄씩 짧게 읽고 잠금 밖에서 계산한다.
수신 저장은 그동안에도 그대로 된다. 끝나기 전까지 화면·경보는 이전 통계를 본다.
한 번에 하나만 돈다(Store.stats_lock을 잡으므로 매시간 최신화와도 겹치지 않는다).
"""

from __future__ import annotations

import threading
import time

from web_db.src.db_util import _iso


class StatRebuildJob:
    """프로세스에 하나. state()로 진행을 본다."""

    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._thread = None
        self._state = {"running": False, "phase": "", "done": 0, "total": 0, "result": None, "error": "",
                       "profile_id": 0, "started_at": "", "finished_at": ""}

    def state(self):
        with self._lock:
            return dict(self._state)

    def _set(self, **values):
        with self._lock:
            self._state.update(values)

    def start(self, profile_id):
        """profile_id 버전으로 재계산을 시작한다. 이미 돌고 있으면 False."""
        with self._lock:
            if self._state["running"]:
                return False
            self._state = {"running": True, "phase": "다른 통계 작업이 끝나기를 기다리는 중", "done": 0, "total": 0,
                           "result": None, "error": "", "profile_id": int(profile_id),
                           "started_at": _iso(), "finished_at": ""}
        self._thread = threading.Thread(target=self._run, args=(int(profile_id),), name="stat-rebuild", daemon=True)
        self._thread.start()
        return True

    def _run(self, profile_id):
        began = time.monotonic()
        try:
            with self.store.stats_lock:
                self._set(phase=f"기준 v{profile_id}로 다시 계산하는 중")
                result = self.store.rebuild_statistics(
                    profile_id, progress=lambda done, total: self._set(done=done, total=total)
                )
            result["seconds"] = round(time.monotonic() - began, 1)
            result["notes"] = result.get("notes", [])[:10]
            result["message"] = (f"기준 v{profile_id}: {result['macs']}개 MAC, {result['sets']}세트, "
                                 f"{result['hours']}시간치 통계를 만들었습니다.")
            self._set(running=False, phase="완료", result=result, finished_at=_iso())
        except Exception as exc:
            self._set(running=False, phase="실패", error=str(exc), finished_at=_iso())

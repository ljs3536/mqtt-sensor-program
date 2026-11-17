"""/api/db. SQLite 조회와 통계 최신화."""

import threading
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from web_db.src.grouping import group_adxl, packet_to_template
from web_db.src.runtime import replay, store

router = APIRouter()


class DbLoad(BaseModel):
    """DB 분석 조회. recent는 최근 세트 수, range는 시작~끝 시각."""

    mac: str
    sensor: str
    mode: str = "recent"
    limit: int = 50
    start: str = ""
    end: str = ""


class DeviceName(BaseModel):
    """표시 이름 저장 요청."""
    mac: str
    display_name: str = ""


def _query_time(value, end):
    """datetime-local 값을 packet.received_at 문자열 비교에 맞춘다."""
    text = str(value or "").strip()
    if len(text) == 16:
        return text + (":59.999" if end else ":00")
    return text


def _load_items(mac, sensor, mode="recent", limit=50, start="", end=""):
    """DB 패킷을 발생기 미리보기 형식으로 고른다. ADXL은 완성된 세트만."""
    sensor_type = {"PIEZO": "PIEZO", "ADXL": "ADXL", "초음파": "ULTRASONIC"}.get(sensor)
    if sensor_type is None:
        raise ValueError("센서는 PIEZO, ADXL, 초음파 중 하나여야 합니다")
    if mode not in ("recent", "range"):
        raise ValueError("조회는 최근 또는 기간이어야 합니다")
    if mode == "range" and (not start or not end):
        raise ValueError("기간 조회는 시작과 끝이 필요합니다")
    rows = store.packets_for(mac, sensor_type, mode, limit, start, end)
    if sensor == "ADXL":
        items = group_adxl(rows)
        if not items:
            raise ValueError("이 조건의 ADXL X/Y/Z 세트가 SQLite에 없습니다")
        return items
    if not rows:
        raise ValueError("이 조건의 센서 패킷이 SQLite에 없습니다")
    return [packet_to_template(row) for row in rows]


def _daily_stats():
    """날짜가 바뀌었고 오늘 통계가 없으면 한 번 최신화한다."""
    while True:
        time.sleep(600)
        try:
            last = store.stats_updated_at()
            today = datetime.now().date().isoformat()
            if last is None or str(last)[:10] < today:
                store.update_statistics()
        except Exception:
            continue


threading.Thread(target=_daily_stats, name="daily-stats", daemon=True).start()


@router.get("/api/db/dashboard")
def db_dashboard():
    """저장된 통계만 조회한다. 계산은 하지 않는다."""
    return store.dashboard()


@router.post("/api/db/update-stats")
def db_update_stats():
    """확인된 새로고침 또는 호출 시 새 측정만 통계로 만든다."""
    return store.update_statistics()


@router.get("/api/db/devices")
def db_devices():
    """저장된 MAC 목록."""
    return store.devices()


@router.get("/api/db/stats")
def db_stats(mac: str):
    """한 MAC 통계 요약."""
    mac = mac.strip()
    if not mac:
        raise HTTPException(400, "MAC이 필요합니다")
    return store.stats(mac)


@router.post("/api/db/refresh")
def db_refresh():
    """날짜별 통계 표를 measurement에서 다시 만든다."""
    count = store.rebuild_daily()
    return {"days": count}


@router.post("/api/db/load")
def db_load(body: DbLoad):
    """고른 조건의 DB 패킷을 분석 그래프에 올린다."""
    mac = body.mac.strip()
    sensor = body.sensor.strip()
    start = _query_time(body.start, end=False)
    end = _query_time(body.end, end=True)
    try:
        items = _load_items(mac, sensor, body.mode, body.limit, start, end)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with replay.lock:
        replay.templates[sensor] = items
        replay.index[sensor] = 0
        replay.paths[sensor] = f"DB {mac}"
        replay.log(f"[{sensor}] SQLite {mac} {len(items)}개 로드")
    try:
        preview = replay.preview(sensor, 0)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"sensor": sensor, "mac": mac, "count": len(items), "preview": preview, "stats": store.stats(mac)}


@router.get("/api/db/profile")
def db_profile(mac: str):
    """이름과 마지막 설정 JSON."""
    mac = mac.strip()
    if not mac:
        raise HTTPException(400, "MAC이 필요합니다")
    return store.device_profile(mac)


@router.post("/api/db/device-name")
def db_device_name(body: DeviceName):
    """표시 이름을 저장한다."""
    try:
        return store.save_display_name(body.mac, body.display_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/api/db/device-name")
def db_clear_device_name(mac: str):
    """표시 이름을 지운다. 패킷이 없는 MAC은 목록에서 제거한다."""
    try:
        return store.clear_display_name(mac)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc



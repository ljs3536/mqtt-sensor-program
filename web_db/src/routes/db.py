"""/api/db. SQLite 조회와 하루 한 번 통계 스케줄러.

통계 계산은 화면에서 시키지 않는다(최신화 버튼·API 없음, 2026-11-18). 스케줄러와
명령줄 도구(import_csv)만 store.update_statistics를 부른다.

예외: 설정 화면 '통계 데이터' 탭의 "CSV로 일자별 통계 다시 만들기"(관리자용).
- POST /api/db/csv-stats  백그라운드로 시작(이미 돌고 있으면 409)
- GET  /api/db/csv-stats  진행 상황과 마지막 결과
data_csv 전체로 measurement를 일자별로 다시 만든다(csv_stats.py). 모든 사람이 같은 결과를 본다.
"""

import threading
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from web_db.src.csv_stats import CsvStatsJob, find_groups
from web_db.src.grouping import group_adxl, packet_to_template
from web_db.src.paths import DATA_CSV
from web_db.src.runtime import replay, store

router = APIRouter()
csv_job = CsvStatsJob(store)


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
    """datetime-local 값을 packet.received_at 문자열 비교에 맞춘다.

    브라우저는 "2026-11-17T14:00"처럼 분까지 보낸다. received_at은 밀리초까지 있는
    ISO 문자열이라, 시작은 ":00", 끝은 ":59.999"를 붙여 그 분 전체가 들어가게 한다.
    """
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
    """날짜가 바뀌었고 오늘 통계가 없으면 한 번 최신화한다.

    10분마다 깨어나 stat_meta의 마지막 최신화 날짜를 본다. 오늘보다 이전이면 최신화한다.
    그래서 자정 뒤 최대 10분 안에 하루 한 번 돈다. 서버가 꺼져 있던 날은 건너뛰고,
    다시 켜진 뒤 첫 확인 때 그동안 쌓인 패킷을 한 구간으로 넣는다.
    예외는 삼키고 다음 확인을 기다린다(스레드가 죽지 않게).
    """
    while True:
        time.sleep(600)
        try:
            last = store.stats_updated_at()
            today = datetime.now().date().isoformat()
            if last is None or str(last)[:10] < today:
                store.update_statistics()
        except Exception:
            continue


_daily_thread = None


def start_daily_stats():
    """하루 통계 스레드를 프로세스에 하나만 켠다. 두 번 불려도 새로 만들지 않는다."""
    global _daily_thread
    if _daily_thread is not None and _daily_thread.is_alive():
        return _daily_thread
    _daily_thread = threading.Thread(target=_daily_stats, name="daily-stats", daemon=True)
    _daily_thread.start()
    return _daily_thread


@router.get("/api/db/dashboard")
def db_dashboard():
    """저장된 통계만 조회한다. 계산은 하지 않는다."""
    return store.dashboard()


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


@router.get("/api/db/csv-stats")
def csv_stats_state():
    """CSV 일자별 통계 작업 상태와, 지금 data_csv에서 찾은 X/Y/Z CSV 묶음 수(버튼 옆 안내용)."""
    return dict(csv_job.state(), folder=str(DATA_CSV), groups_found=len(find_groups(DATA_CSV)))


@router.post("/api/db/csv-stats")
def csv_stats_start():
    """data_csv 전체로 일자별 통계를 다시 만든다. 백그라운드로 돌고 GET으로 진행을 본다."""
    if not csv_job.start(DATA_CSV):
        raise HTTPException(409, "이미 통계를 만드는 중입니다")
    return csv_job.state()

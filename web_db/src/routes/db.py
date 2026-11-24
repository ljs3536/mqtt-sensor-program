"""/api/db. SQLite 조회와 매시간 통계 스케줄러(2026-11-23 전에는 하루 한 번).

통계 최신화는 화면에서 시키지 않는다(2026-11-18). 매시간 스케줄러와 명령줄 도구(import_csv)만
store.update_statistics를 부른다. 통계는 MAC마다 한 시간 한 줄이고 계산 기준은 DB stat_profile 버전이다.

설정 화면 '통계 데이터' 탭(관리자용, 모든 사람이 같은 결과를 본다)
- GET  /api/db/stat-profile   기준 버전 목록·지금 버전·그래프 설정 파일 값·재계산 진행
- POST /api/db/stat-profile   새 기준 버전 저장 + 그 버전으로 재계산 시작(돌고 있으면 409)
- POST /api/db/stat-rebuild   지금(또는 가장 최근) 버전으로 재계산 시작
- GET/POST /api/db/csv-import data_csv_web의 누적 CSV를 packet으로 가져오기(csv_import.py, 2026-11-24).
  끝나면 '지금 기준으로 다시 계산'으로 통계를 만든다. 가져오기와 재계산·기준 저장은 서로 409로 막는다.
"""

import threading
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from web_db.src import core as gen
from web_db.src import stat_profile as sp
from web_db.src.csv_import import CsvImportJob, scan
from web_db.src.grouping import group_adxl, packet_to_template
from web_db.src.paths import DATA_CSV
from web_db.src.runtime import replay, store

from web_db.src.stats_job import StatRebuildJob

router = APIRouter()
csv_job = CsvImportJob(store)
rebuild_job = StatRebuildJob(store)


class StatProfileBody(BaseModel):
    """통계 기준 저장 요청. settings 모양은 stat_profile.py 설명."""
    settings: dict
    note: str = ""


class DbLoad(BaseModel):
    """DB 분석 조회. recent는 최근 세트 수, range는 시작~끝 시각."""

    mac: str
    sensor: str
    mode: str = "recent"
    limit: int = 50
    start: str = ""
    end: str = ""


class DeviceName(BaseModel):
    """표시 이름 저장 요청. location(설치 위치, 2026-11-24)은 보낼 때만 바꾼다(None이면 그대로).

    수신 모니터의 이름 저장(monitor-db.js)은 이름만 보내므로 위치가 지워지지 않는다."""
    mac: str
    display_name: str = ""
    location: str | None = None


class DeviceVisible(BaseModel):
    """설정 > 장치 '표시' 체크 저장 요청."""
    mac: str
    visible: bool = True


class Zones(BaseModel):
    """진동 구간 저장 요청(mm/s RMS). good·caution을 둘 다 비우면 자동(위험 기준의 ISO 비율)."""
    mac: str
    good: float | str | None = None
    caution: float | str | None = None
    limit: float


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


def _hourly_stats():
    """매시간 한 번 통계를 최신화한다(2026-11-23, 전에는 하루 한 번).

    1분마다 깨어나 stat_meta의 마지막 계산 시각을 본다. 그 '시'가 지금 시보다 이전이면 최신화한다.
    그래서 매시 정각 뒤 1분 안에 돈다. 서버가 꺼져 있던 동안 쌓인 패킷은 켜진 뒤 첫 확인 때
    세트 시각에 맞는 시간 줄로 들어간다(한 덩어리로 뭉치지 않는다).
    재계산이 돌고 있으면 stats_lock에서 기다렸다가 이어서 한다.
    예외는 삼키고 다음 확인을 기다린다(스레드가 죽지 않게).
    """
    while True:
        time.sleep(60)
        try:
            last = str(store.stats_updated_at() or "")
            hour = datetime.now().strftime("%Y-%m-%dT%H")
            if last.replace(" ", "T")[:13] < hour:
                store.update_statistics()
        except Exception:
            continue


_hourly_thread = None


def start_daily_stats():
    """매시간 통계 스레드를 프로세스에 하나만 켠다(함수 이름은 예전 그대로, server.py가 부른다).

    시작할 때 통계 기준 버전이 없으면 첫 버전을 만들고, 지금 버전이 없으면(하루 통계에서 업그레이드한 직후)
    그 버전으로 packet 전체 재계산을 백그라운드로 시작한다. 끝날 때까지 화면은 옛 하루 통계를 보인다.
    """
    global _hourly_thread
    if _hourly_thread is not None and _hourly_thread.is_alive():
        return _hourly_thread
    try:
        latest = store.ensure_stat_profile()
        if not store.stat_profiles()["active"]:
            rebuild_job.start(latest)
    except Exception:
        pass  # DB를 다른 프로그램이 잡고 있어도 서버는 뜬다. 다음 시작 때 다시 시도한다.
    _hourly_thread = threading.Thread(target=_hourly_stats, name="hourly-stats", daemon=True)
    _hourly_thread.start()
    return _hourly_thread


@router.get("/api/db/dashboard")
def db_dashboard(period: str = "24h", mac: str = "", bucket: str = ""):
    """저장된 통계만 조회한다. 계산은 하지 않는다. period: 24h(기본)·7d·30d·all(store.DASHBOARD_PERIODS).

    mac·bucket(hour/day)은 통계2 화면의 자세히 보기가 한 MAC만, 원하는 점 묶음으로 읽을 때 쓴다(2026-11-24).
    """
    return store.dashboard(period, mac or None, bucket or None)


@router.get("/api/db/devices")
def db_devices(named: bool = False):
    """저장된 MAC 목록. 설정 '장치' 표의 사진 칸을 위해 photo(주소 또는 "")를 붙인다.

    named=true면 이름이 있는 MAC만(센서 데이터 분석, 2026-11-24). 설정 > 장치 표는 전부(이름을 붙일 곳).
    """
    items = store.devices(named)
    photos = store.sensor_photos()
    for item in items:
        version = photos.get(str(item.get("mac") or "").upper())
        item["photo"] = f"/api/site/sensors/{item['mac']}/photo?v={version}" if version else ""
    return items


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
        return store.save_display_name(body.mac, body.display_name, body.location)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/db/device-visible")
def db_device_visible(body: DeviceVisible):
    """'표시'를 끄면 이름이 있어도 센서 현황·분석·통계에서 숨긴다(2026-11-24). 수신 저장·통계 계산은 그대로."""
    try:
        return store.set_device_visible(body.mac, body.visible)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/db/zones")
def db_zones(body: Zones):
    """진동 구간(양호 상한·주의 상한·위험 기준)을 저장한다. 위험 기준은 예전 SRSS 한계와 같은 칸이다."""
    try:
        return store.save_zones(body.mac, body.good, body.caution, body.limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/api/db/device-name")
def db_clear_device_name(mac: str):
    """표시 이름을 지운다. 패킷이 없는 MAC은 목록에서 제거한다."""
    try:
        return store.clear_display_name(mac)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/db/csv-import")
def csv_import_state():
    """CSV 가져오기 진행 상태 + 버튼 옆 안내(data_csv_web에서 찾은 CSV 파일 수·크기, DB packet 줄 수)."""
    state = csv_job.state()
    if state["running"]:
        return state  # 도는 동안(1초마다 부름)은 진행만. 폴더 훑기·줄 세기는 하지 않는다
    return dict(state, folder=str(DATA_CSV), found=scan(DATA_CSV), packets=store.packet_counts())


@router.post("/api/db/csv-import")
def csv_import_start():
    """data_csv_web의 CSV를 packet으로 가져온다. 백그라운드로 돌고 GET으로 진행을 본다. 수신은 그대로 계속된다."""
    if rebuild_job.state()["running"]:
        raise HTTPException(409, "통계를 다시 계산하는 중입니다. 끝난 뒤 하세요.")
    if not csv_job.start(DATA_CSV):
        raise HTTPException(409, "이미 CSV를 가져오는 중입니다")
    return csv_job.state()


# ---------- 통계 계산 기준(버전) ----------
def _file_settings():
    """그래프가 쓰는 설정 파일 값(g/LSB, MEMS 보정). 설정 화면 '그래프 설정 파일 값 가져오기'용."""
    return {"g_per_lsb": gen.read_mems_g_per_lsb(), "correction": gen.load_mems_correction()}


@router.get("/api/db/stat-profile")
def stat_profile_state():
    """기준 버전 목록(최근 50), 지금·최근 버전 id, 기본값, 그래프 설정 파일 값, 재계산 진행."""
    data = store.stat_profiles()
    data.update(defaults=sp.default_settings(), files=_file_settings(), job=rebuild_job.state(),
                windows=list(gen.FFT_WINDOWS), segments=list(sp.PSD_SEGMENTS))
    return data


@router.post("/api/db/stat-profile")
def stat_profile_save(body: StatProfileBody):
    """새 기준 버전을 저장하고 그 버전으로 재계산을 시작한다. 끝나면 그 버전이 지금 버전이 된다."""
    if rebuild_job.state()["running"] or csv_job.state()["running"]:
        raise HTTPException(409, "통계를 다시 계산하거나 CSV를 가져오는 중입니다. 끝난 뒤 저장하세요.")
    try:
        profile_id = store.add_stat_profile(body.settings, body.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    rebuild_job.start(profile_id)
    return stat_profile_state()


@router.post("/api/db/stat-rebuild")
def stat_rebuild():
    """지금 버전(없으면 가장 최근 버전)으로 packet 전체를 다시 계산한다."""
    if rebuild_job.state()["running"] or csv_job.state()["running"]:
        raise HTTPException(409, "이미 통계를 다시 계산하거나 CSV를 가져오는 중입니다")
    data = store.stat_profiles()
    profile_id = data["active"] or data["latest"] or store.ensure_stat_profile()
    rebuild_job.start(profile_id)
    return stat_profile_state()

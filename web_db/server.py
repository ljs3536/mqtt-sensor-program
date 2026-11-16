"""8081 프로세스. 계산과 MQTT, CSV는 web 패키지를 그대로 쓰고 DB만 여기서 붙인다.

역할:
- web.core: 디코드, 파일 로드, FFT
- web.analysis: 그래프 수식
- web.server: 수신 모니터, CSV, 파형 발생
- web_db.grouping: ADXL set_no
- web_db.stats_calc: 한 세트의 SRSS, RMS, PEAK
- web_db.store: SQLite 읽기 쓰기
- 이 파일: 수신 저장 연결, 화면, 통계 API, 하루 한 번 최신화

실행: .venv\\Scripts\\python.exe -m uvicorn web_db.server:app --host 127.0.0.1 --port 8081
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from web import server as web_server
from web_db.grouping import group_adxl, packet_to_template
from web_db.store import Store

HERE = web_server.Path(__file__).resolve().parent
store = Store()
monitor = web_server.monitor
replay = web_server.replay
app = web_server.app

if not any(getattr(route, "path", None) == "/db-static" for route in app.router.routes):
    app.mount("/db-static", StaticFiles(directory=HERE / "static"), name="db-static")


def _drop(path):
    """이미 등록된 같은 경로의 페이지 라우트를 빼 웹 DB 화면으로 바꾼다."""
    app.router.routes[:] = [
        route for route in app.router.routes if getattr(route, "path", None) != path
    ]


_drop("/studio")
_drop("/")
_drop("/generator")


@app.get("/generator")
def generator_page():
    """데이터 분석 CSV. 분석 화면과 같은 배치이고 파일만 연다."""
    return FileResponse(HERE / "generator.html")


@app.get("/")
def home_page():
    """센서 수신 모니터. 설정 JSON과 표시 이름을 SQLite에 남긴다."""
    return FileResponse(HERE / "index.html")


@app.get("/dashboard")
def dashboard_page():
    """MAC별 최신 SRSS, RMS, PEAK."""
    return FileResponse(HERE / "dashboard.html")


@app.get("/api/db/dashboard")
def db_dashboard():
    """저장된 통계만 조회한다. 계산은 하지 않는다."""
    return store.dashboard()


@app.post("/api/db/update-stats")
def db_update_stats():
    """확인된 새로고침 또는 호출 시 새 측정만 통계로 만든다."""
    return store.update_statistics()


@app.get("/studio")
def studio_page():
    """센서 데이터 분석. CSV 파일과 SQLite 수신 기록을 모두 연다."""
    return FileResponse(HERE / "studio.html")


class DbLoad(BaseModel):
    """DB 분석 조회. recent는 최근 세트 수, range는 시작~끝 시각."""

    mac: str
    sensor: str
    mode: str = "recent"
    limit: int = 50
    start: str = ""
    end: str = ""


def _save_csv(self, obj, kind, axis, sensor_hex, decoded, meta):
    """기존 CSV 저장 뒤에 같은 패킷을 SQLite packet에 넣는다."""
    web_server.MonitorService._save_csv(self, obj, kind, axis, sensor_hex, decoded, meta)
    try:
        store.save_packet(
            {
                "device": str(obj.get("dvicMacAddr") or obj.get("deviceId") or "UNKNOWN").strip(),
                "kind": kind,
                "axis": axis if kind != "ULTRASONIC" else "H",
                "seq": obj.get("seq", ""),
                "tick": obj.get("tick", ""),
                "battery": obj.get("batteryRmin", ""),
                "gw": obj.get("gwMacAddr", ""),
                "sample_rate": obj.get("samplerate", ""),
                "num_samples": obj.get("numofsample", ""),
                "payload_bytes": len(
                    web_server.json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                ),
                "sensor_data": sensor_hex,
                "decoded": decoded,
                "meta": meta,
                "received_at": web_server.datetime.now().isoformat(timespec="milliseconds"),
            }
        )
    except Exception as exc:
        self.log(f"SQLite 저장 실패: {exc}")


monitor._save_csv = _save_csv.__get__(monitor, web_server.MonitorService)


def _publish_config(self, macs, payload, label, retain=True):
    """센서 설정 JSON을 브로커로 보낸 뒤 retain 명령이면 DB에 남긴다."""
    text = web_server.MonitorService.publish_config(self, macs, payload, label, retain)
    if retain:
        for mac in macs:
            try:
                store.save_sensor_config(mac, payload, label)
            except Exception as exc:
                self.log(f"설정 JSON 저장 실패 {mac}: {exc}")
    return text


monitor.publish_config = _publish_config.__get__(monitor, web_server.MonitorService)


def _daily_stats():
    """날짜가 바뀌었고 오늘 통계가 없으면 한 번 최신화한다."""
    while True:
        web_server.time.sleep(600)
        try:
            last = store.stats_updated_at()
            today = web_server.datetime.now().date().isoformat()
            if last is None or str(last)[:10] < today:
                store.update_statistics()
        except Exception:
            continue


web_server.threading.Thread(target=_daily_stats, name="daily-stats", daemon=True).start()


class DeviceName(BaseModel):
    mac: str
    display_name: str = ""


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


@app.get("/api/db/devices")
def db_devices():
    """저장된 MAC 목록."""
    return store.devices()


@app.get("/api/db/stats")
def db_stats(mac: str):
    """한 MAC 통계 요약."""
    mac = mac.strip()
    if not mac:
        raise HTTPException(400, "MAC이 필요합니다")
    return store.stats(mac)


@app.post("/api/db/refresh")
def db_refresh():
    """날짜별 통계 표를 measurement에서 다시 만든다."""
    count = store.rebuild_daily()
    return {"days": count}


@app.post("/api/db/load")
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


def _query_time(value, end):
    """datetime-local 값을 packet.received_at 문자열 비교에 맞춘다."""
    text = str(value or "").strip()
    if len(text) == 16:
        return text + (":59.999" if end else ":00")
    return text


@app.get("/api/db/profile")
def db_profile(mac: str):
    """이름과 마지막 설정 JSON."""
    mac = mac.strip()
    if not mac:
        raise HTTPException(400, "MAC이 필요합니다")
    return store.device_profile(mac)


@app.post("/api/db/device-name")
def db_device_name(body: DeviceName):
    """표시 이름을 저장한다."""
    try:
        return store.save_display_name(body.mac, body.display_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

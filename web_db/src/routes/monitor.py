"""/api/monitor. 수신 연결, 최신 그래프, 센서 명령."""

from fastapi import APIRouter, HTTPException

from web_db.src import core as mon
from web_db.src.monitor_service import _sensor_config
from web_db.src.runtime import monitor

router = APIRouter()


@router.post("/api/monitor/connect")
async def monitor_connect(body: dict):
    """수신 모니터를 브로커에 연결한다."""
    try:
        monitor.connect_mqtt(
            body.get("host") or "127.0.0.1",
            int(body.get("port") or 1883),
            body.get("topic") or "V/#,D/#",
            csv_enabled=body.get("csv_enabled", True),
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return monitor.state()


@router.post("/api/monitor/disconnect")
def monitor_disconnect():
    """수신 MQTT와 CSV 세션을 끊는다."""
    monitor.disconnect()
    monitor.log("연결 해제")
    return monitor.state()


@router.get("/api/monitor/state")
def monitor_state(device: str = ""):
    """지금 수신 상태와 최신 그래프를 돌려준다."""
    return monitor.state(device)


@router.post("/api/monitor/fft-check")
async def monitor_fft_check(body: dict):
    """수신 중인 RAW의 FFT 왕복 검사를 돌려준다."""
    try:
        return monitor.fft_check(str(body.get("device") or ""))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/monitor/view")
async def monitor_view(body: dict):
    """그래프 옵션과 창 길이를 바꾸고 최신 그래프를 다시 준다."""
    monitor.view_options = body.get("options") or {}
    if body.get("window") is not None:
        monitor.window = str(body.get("window") or "전체")
    return monitor.state(str(body.get("device") or ""))


@router.post("/api/monitor/window")
async def monitor_window(body: dict):
    """모니터 그래프의 창 길이만 바꾼다."""
    monitor.window = str(body.get("window") or "전체")
    return {"window": monitor.window}


@router.post("/api/monitor/command")
async def monitor_command(body: dict):
    """고른 MAC에 START, STOP, 설정 적용, 설정 조회를 보낸다."""
    action = str(body.get("action") or "")
    macs = [str(item).strip() for item in body.get("macs") or [] if str(item).strip()]
    sensor = str(body.get("sensor") or "ADXL").upper()
    period = body.get("period") or 1
    labels = mon.ADXL_PROFILE_LABELS if sensor == "ADXL" else mon.PIEZO_PROFILE_LABELS
    profile = body.get("profile") or labels[-1]
    try:
        if action == "query":
            if len(macs) != 1:
                raise ValueError("현재 설정 조회는 센서 MAC 1개만 선택하세요")
            payload = monitor.publish_config(macs, {"CONFIG": "?"}, "현재 설정 조회", retain=False)
        elif action == "stop":
            payload = monitor.publish_config(
                macs, _sensor_config(sensor, period, profile, stop=True), "STOP"
            )
        elif action in ("start", "apply"):
            label = "START" if action == "start" else "설정 적용"
            payload = monitor.publish_config(
                macs,
                _sensor_config(
                    sensor,
                    period,
                    profile,
                    sensor_type=body.get("sensor_type"),
                    g_range=body.get("g_range") or "16",
                    piezo_gain=body.get("piezo_gain") if body.get("piezo_gain") is not None else "30",
                ),
                label,
            )
        else:
            raise ValueError("명령은 start, stop, apply, query 중 하나여야 합니다")
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"payload": payload, "state": monitor.state()}



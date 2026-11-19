"""/api/monitor. 수신 연결, 최신 그래프, 센서 명령."""

from fastapi import APIRouter, HTTPException

from web_db.src import core as mon
from web_db.src.monitor_service import _sensor_config
from web_db.src.runtime import monitor

router = APIRouter()


@router.post("/api/monitor/connect")
async def monitor_connect(body: dict):
    """수신 모니터를 브로커에 연결한다."""
    host = str(body.get("host") or "127.0.0.1").strip()
    port = int(body.get("port") or 1883)
    topic = str(body.get("topic") or "V/#,D/#").strip()
    csv_enabled = bool(body.get("csv_enabled", True))
    try:
        monitor.connect_mqtt(host, port, topic, csv_enabled=csv_enabled)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    # 이 연결(CSV 체크 포함)을 기억한다. 서버가 다시 켜지면 '연결 해제'를 하기 전까지 같은 값으로 다시 연결한다.
    # 연결 자체는 이미 됐으므로, 기억(DB 쓰기)이 실패해도 500을 내지 않고 경고만 붙인다.
    return dict(monitor.state(), warning=_remember(host, port, topic, True, csv_enabled))


def _remember(host, port, topic, on, csv=None):
    """연결 값을 DB에 남긴다. 실패하면(다른 프로그램이 DB 쓰기 잠금을 잡은 경우 등) 경고 문장을 돌려준다."""
    try:
        monitor.remember_connection(host, port, topic, on=on, csv=csv)
        return ""
    except Exception as exc:
        message = (
            f"연결 값을 DB에 저장하지 못했습니다({exc}). 서버가 다시 켜질 때 자동 연결이 이 값을 따르지 않을 수 있습니다. "
            "DB Browser 같은 프로그램이 DB를 쓰기 중으로 잡고 있으면 닫거나 변경사항을 쓰기/취소하세요(그동안 수신 데이터도 DB에 저장되지 않습니다)."
        )
        monitor.log(message)
        return message


@router.post("/api/monitor/disconnect")
def monitor_disconnect():
    """수신 MQTT와 CSV 세션을 끊는다."""
    monitor.disconnect()
    # 사람이 끊은 것이므로 서버가 다시 켜져도 다시 연결하지 않는다(저장 실패는 경고만).
    warning = _remember(monitor.host, monitor.port, monitor.topic, False)
    monitor.log("연결 해제")
    return dict(monitor.state(), warning=warning)


@router.post("/api/monitor/logs/clear")
def monitor_clear_logs():
    """수신 화면 로그를 비우고 한 줄 안내만 남긴다."""
    monitor.clear_logs()
    monitor.log("로그를 지웠습니다")
    return {"ok": True}


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



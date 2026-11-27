"""/api/monitor. 수신 연결, 최신 그래프, 센서 명령."""

import json

from fastapi import APIRouter, HTTPException

from web_db.src import core as mon
from web_db.src.monitor_service import _sensor_config, command_text, custom_profile, subscribe_qos
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
        qos = subscribe_qos(body.get("qos"))
        monitor.connect_mqtt(host, port, topic, csv_enabled=csv_enabled, qos=qos)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    # 이 연결(CSV 체크·QoS 포함)을 기억한다. 서버가 다시 켜지면 '연결 해제'를 하기 전까지 같은 값으로 다시 연결한다.
    # 연결 자체는 이미 됐으므로, 기억(DB 쓰기)이 실패해도 500을 내지 않고 경고만 붙인다.
    return dict(monitor.state(), warning=_remember(host, port, topic, True, csv_enabled, qos))


def _remember(host, port, topic, on, csv=None, qos=None):
    """연결 값을 DB에 남긴다. 실패하면(다른 프로그램이 DB 쓰기 잠금을 잡은 경우 등) 경고 문장을 돌려준다."""
    try:
        monitor.remember_connection(host, port, topic, on=on, csv=csv, qos=qos)
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
def monitor_state(device: str = "", have: str = ""):
    """지금 수신 상태와 최신 그래프를 돌려준다.

    have = 화면이 가진 그래프 지문. 같으면 그래프(latest)를 빼고 보낸다(MonitorService.state 참고).
    """
    return monitor.state(device, have)


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


def _command_fields(body):
    """화면 칸 값(종류·주기·Fs+N·G Range·게인)을 명령에 쓰는 값으로 읽는다. 명령·미리보기가 같이 쓴다.

    돌려주는 것: (sensor, period, profile, g_range, piezo_gain). Fs+N 직접 입력이 잘못되면 ValueError.
    """
    sensor = str(body.get("sensor") or "ADXL").upper()
    # 0도 올바른 주기다. 값이 아예 없을 때만 1을 쓴다(예전 `or 1`은 0을 1로 바꿨다).
    period = body.get("period")
    if period is None:
        period = 1
    labels = mon.ADXL_PROFILE_LABELS if sensor == "ADXL" else mon.PIEZO_PROFILE_LABELS
    profile = body.get("profile") or labels[-1]
    g_range = body.get("g_range") or "16"
    piezo_gain = body.get("piezo_gain") if body.get("piezo_gain") is not None else "30"
    # 직접 입력(2026-11-24): custom이 참이면 목록 대신 custom_rate·custom_count를 검사해 쓴다(STOP의 normal에도).
    if body.get("custom"):
        profile = custom_profile(sensor, body.get("custom_rate"), body.get("custom_count"))
    return sensor, period, profile, g_range, piezo_gain


def _raw_payload(text):
    """'JSON 그대로 보내기'(2026-11-27)의 본문을 검사해 dict로 돌려준다. 잘못되면 ValueError(이유).

    - JSON 객체({ … })여야 하고 비어 있으면 안 된다. 키 순서는 적힌 그대로 보낸다.
    - 크기(CR 포함 1024바이트)는 command_text가 검사한다(다른 명령과 같다).
    - 값의 종류(글자 "4" / 숫자 4)는 고치지 않는다. 보낸 사람이 적은 그대로 나간다.
    """
    try:
        payload = json.loads(str(text or ""))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 형식이 아닙니다: {exc.msg} (줄 {exc.lineno}, 칸 {exc.colno})") from None
    if not isinstance(payload, dict) or not payload:
        raise ValueError("JSON 객체({ … })를 넣어 주세요. 비어 있으면 보내지 않습니다")
    command_text(payload)
    return payload


@router.post("/api/monitor/command/preview")
def monitor_command_preview(body: dict):
    """화면 칸 값으로 '설정 적용'이 보낼 JSON을 만들어 돌려준다(2026-11-27, 보내지 않음).

    화면의 '보낼 JSON' 칸이 칸을 바꿀 때마다 부른다. 실제 전송과 같은 함수(_sensor_config)로 만들어
    화면 미리보기와 실제로 나가는 내용이 어긋나지 않는다.
    - 센서 타입 0이면 {"COMMPATH":"0","SENSORTYPE":"0"}과 함께, '설정 적용'으로 누르면 STOP 3단계로 간다는 note.
    - 칸 값이 잘못되면 400(이유).
    """
    try:
        sensor, period, profile, g_range, piezo_gain = _command_fields(body)
        payload = _sensor_config(sensor, period, profile, sensor_type=body.get("sensor_type"),
                                 g_range=g_range, piezo_gain=piezo_gain)
        text = command_text(payload)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    stopping = str(body.get("sensor_type") or "").strip() == "0"
    return {
        "payload": payload,
        "bytes": len(text.encode("utf-8")) + 1,
        "note": "센서 타입 0: '설정 적용'은 STOP 3단계(STOP → retain 삭제 → normal)로 보냅니다" if stopping else "",
    }


@router.post("/api/monitor/command")
def monitor_command(body: dict):
    """고른 MAC에 START, STOP, 설정 적용, 설정 조회, JSON 그대로 보내기를 한다.

    async가 아니라 def다. STOP은 단계마다 PUBACK을 최대 10초 기다리므로, 스레드 풀에서 돌아야
    그동안 다른 화면 요청(상태 조회 등)이 막히지 않는다.
    action "raw"(2026-11-27): body["raw"]의 JSON을 그대로 S/{MAC}에 retain으로 보낸다(설정 적용과 같은 QoS·retain·DB 기록).
    SENSORTYPE 0이 들어 있어도 STOP 3단계로 바꾸지 않고 한 번만 보낸다(화면이 보내기 전에 경고한다).
    """
    action = str(body.get("action") or "")
    macs = [str(item).strip() for item in body.get("macs") or [] if str(item).strip()]
    try:
        if action == "raw":
            payload = monitor.publish_config(macs, _raw_payload(body.get("raw")), "JSON 직접 전송")
            return {"payload": payload, "state": monitor.state()}
        sensor, period, profile, g_range, piezo_gain = _command_fields(body)
        if action == "query":
            if len(macs) != 1:
                raise ValueError("현재 설정 조회는 센서 MAC 1개만 선택하세요")
            payload = monitor.publish_config(macs, {"CONFIG": "?"}, "현재 설정 조회", retain=False)
        elif action == "stop" or (action == "apply" and str(body.get("sensor_type") or "").strip() == "0"):
            # 수신기 V063: '설정 적용'에서 센서 타입 0을 고르면 STOP과 같은 절차를 밟는다.
            # 수신기 V057 STOP: SENSORTYPE 0 → retain 삭제 → normal(종류가 PIEZO면 1, 아니면 2).
            # normal 설정은 지금 화면 칸(주기, Fs+N, G Range, 게인)으로 만든다. 칸 값이 잘못되면 아무것도 보내지 않는다.
            normal = _sensor_config(
                sensor,
                period,
                profile,
                sensor_type="1" if sensor == "PIEZO" else "2",
                g_range=g_range,
                piezo_gain=piezo_gain,
            )
            payload = monitor.stop_to_normal(macs, normal, "STOP")
        elif action in ("start", "apply"):
            label = "START" if action == "start" else "설정 적용"
            # 수신기 V063(2026-11-23 결정): START는 센서 타입 칸과 상관없이 collection으로 보낸다(PIEZO 3, MEMS 4).
            # 설정 적용은 고른 값(1~4) 그대로. 0은 위 STOP 절차로 갔다.
            sensor_type = ("3" if sensor == "PIEZO" else "4") if action == "start" else body.get("sensor_type")
            payload = monitor.publish_config(
                macs,
                _sensor_config(
                    sensor,
                    period,
                    profile,
                    sensor_type=sensor_type,
                    g_range=g_range,
                    piezo_gain=piezo_gain,
                ),
                label,
            )
        else:
            raise ValueError("명령은 start, stop, apply, query, raw 중 하나여야 합니다")
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"payload": payload, "state": monitor.state()}



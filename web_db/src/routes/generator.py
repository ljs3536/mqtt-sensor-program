"""/api/generator. 파일·CSV를 열고 MQTT로 다시 보낸다."""

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from web_db.src.runtime import replay

router = APIRouter()


def _with_upload(filename, data, load):
    """바이트를 임시 파일로만 넘기고, 파싱이 끝나면 바로 지운다.

    재생에 쓰는 패킷은 메모리에 남는다. uploads 폴더는 만들지 않는다.
    """
    suffix = Path(filename or "data.txt").suffix or ".txt"
    handle = tempfile.NamedTemporaryFile(prefix="sensor-upload-", suffix=suffix, delete=False)
    path = Path(handle.name)
    try:
        handle.write(data)
        handle.close()
        return load(path)
    finally:
        path.unlink(missing_ok=True)


@router.post("/api/generator/upload")
async def upload_sensor(sensor: str = Form(...), file: UploadFile = File(...)):
    """PIEZO, ADXL, 초음파 파일 하나를 전송 목록에 올린다."""
    if sensor not in ("PIEZO", "ADXL", "초음파"):
        raise HTTPException(400, "센서는 PIEZO, ADXL, 초음파 중 하나여야 합니다")
    original = Path(file.filename or "data.txt").name
    try:
        payload = await file.read()
        count = _with_upload(
            original,
            payload,
            lambda path: replay.store(sensor, str(path), label=original),
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"sensor": sensor, "count": count, "preview": replay.preview(sensor, 0)}


def _form_flag(value, default=True):
    """폼 문자열을 켜짐 또는 꺼짐으로 읽는다."""
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() not in {"0", "false", "off", "no"}


@router.post("/api/generator/upload-adxl-csv")
async def upload_adxl_csv(
    x: UploadFile = File(...),
    y: UploadFile = File(...),
    z: UploadFile = File(...),
    recover_partial: str = Form("true"),
):
    """ADXL X/Y/Z CSV 세 파일을 묶어 전송 목록에 올린다."""
    files = {}
    try:
        for axis, item in (("X", x), ("Y", y), ("Z", z)):
            files[axis] = (Path(item.filename or f"{axis}.csv").name, await item.read())
    except Exception as exc:
        raise HTTPException(400, f"CSV 읽기 실패: {exc}") from exc
    recover = _form_flag(recover_partial, default=True)
    handles = []
    try:
        paths = {}
        for axis, (name, payload) in files.items():
            suffix = Path(name).suffix or ".csv"
            handle = tempfile.NamedTemporaryFile(prefix=f"sensor-adxl-{axis}-", suffix=suffix, delete=False)
            handle.write(payload)
            handle.close()
            handles.append(Path(handle.name))
            paths[axis] = handle.name
        count = replay.store_adxl_csv(paths, recover_partial=recover)
        preview = replay.preview("ADXL", 0)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        for path in handles:
            path.unlink(missing_ok=True)
    return {"count": count, "sensor": "ADXL", "recover_partial": recover, "preview": preview}


@router.get("/api/generator/preview")
def generator_preview(sensor: str, index: int = 1):
    """파일 패킷의 그래프 미리보기를 돌려준다."""
    try:
        return replay.preview(sensor, index - 1)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/generator/fft-check")
async def generator_fft_check(body: dict):
    """파일에서 고른 패킷의 FFT 왕복 검사를 돌려준다."""
    try:
        return replay.fft_check(body.get("sensor"))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/generator/preview")
async def generator_preview_post(body: dict):
    """옵션을 반영한 파일 패킷 그래프를 돌려준다."""
    try:
        return replay.preview(body.get("sensor"), int(body.get("index") or 1) - 1, body.get("options"))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/generator/test")
async def generator_test(body: dict):
    """파형 송신 전에 브로커 연결만 시험한다."""
    try:
        return replay.test_broker(body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/generator/start")
async def generator_start(body: dict):
    """골라 둔 파일 패킷의 MQTT 반복 송신을 시작한다."""
    try:
        replay.start(body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return replay.status()


@router.post("/api/generator/stop")
def generator_stop():
    """파일 패킷 반복 송신을 멈춘다."""
    replay.stop()
    return replay.status()


@router.get("/api/generator/status")
def generator_status():
    """송신 횟수와 로그만 돌려준다. 브로커에 다시 접속하지 않는다."""
    # 화면의 Publish/PUBACK/Drop 갱신용. 이 요청은 브로커에 접속하지 않는다.
    return replay.status()



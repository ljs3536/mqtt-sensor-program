"""/api/site. 대문(평면도) 대시보드와 설정 화면의 MQTT 자동 연결.

- GET  /api/site/status        센서별 연결·알람 상태와 요약(대문이 15초마다 부른다). 가볍게 유지한다.
평면도는 여러 장(층·구역마다 한 장, 화면은 탭). 평면도 한 장에 센서 여러 개, 센서 하나는 한 장에만.
- GET    /api/site/plans               평면도 목록(탭 순서)과 모든 센서 위치.
- POST   /api/site/plans               평면도 추가(multipart: file, width, height, name).
- GET    /api/site/plans/{id}/image    평면도 이미지 파일.
- POST   /api/site/plans/{id}/image    그 평면도 이미지 교체(위치는 남음).
- PATCH  /api/site/plans/{id}          이름 바꾸기 {"name"} / 탭 순서 {"move": -1|1}.
- DELETE /api/site/plans/{id}          평면도 한 장과 그 위치 삭제.
- POST   /api/site/placements          편집 화면 '저장'. 모든 평면도의 위치 통째로 바꾸기 [{plan_id, mac, x, y}].
- GET·POST /api/site/mqtt      설정 화면의 수신 MQTT 값(자동 연결 포함).
"""

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from web_db.src import site_status
from web_db.src.runtime import monitor, store

router = APIRouter()


def _receiver():
    """수신 연결 상태 요약. 대문 위쪽 줄과 상태 판정('수신 꺼짐')에 쓴다."""
    return {
        "connected": bool(monitor.connected),
        "host": monitor.host,
        "port": monitor.port,
        "origin": monitor.origin,
        "active": monitor.client is not None,
    }


@router.get("/api/site/status")
def site_status_get():
    """MAC별 연결(마지막 수신 vs 원래 간격)과 알람(쌓인 통계 SRSS vs 한계). 최신화는 하지 않는다."""
    return site_status.build_status(store.site_rows(), _receiver())


def _plan_info(plan):
    """화면에 보낼 평면도 정보. 이미지 주소에는 updated_at을 붙여 교체 때 브라우저 캐시를 피한다."""
    return {
        "id": plan["id"],
        "name": plan["name"],
        "width": plan["width"],
        "height": plan["height"],
        "updated_at": plan["updated_at"],
        "image": f"/api/site/plans/{plan['id']}/image?v={plan['updated_at']}",
    }


def _plans_body():
    """평면도 전체(탭 순서)와 모든 센서 위치. 대문은 이것 하나로 탭과 아이콘을 다 그린다."""
    return {"plans": [_plan_info(p) for p in store.floor_plans()], "placements": store.placements()}


@router.get("/api/site/plans")
def site_plans():
    """평면도 목록과 위치 [{plan_id, mac, x, y}]. 평면도 한 장에 센서 여러 개, 센서 하나는 한 장에만."""
    return _plans_body()


@router.get("/api/site/plans/{plan_id}/image")
def site_plan_image(plan_id: int):
    """평면도 이미지. 없으면 404."""
    found = store.floor_plan_file(plan_id)
    if found is None:
        raise HTTPException(404, "평면도가 없습니다")
    path, media = found
    return FileResponse(path, media_type=media)


@router.post("/api/site/plans")
async def site_plan_create(
    file: UploadFile = File(...),
    width: int = Form(...),
    height: int = Form(...),
    name: str = Form(...),
):
    """평면도를 새로 추가한다(맨 뒤 탭). 응답에 새 id(created)를 넣어 화면이 그 탭을 연다."""
    try:
        data = await file.read()
        plan = store.create_floor_plan(name, file.filename, data, width, height)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return dict(_plans_body(), created=plan["id"])


@router.post("/api/site/plans/{plan_id}/image")
async def site_plan_image_replace(
    plan_id: int,
    file: UploadFile = File(...),
    width: int = Form(...),
    height: int = Form(...),
):
    """그 평면도의 이미지만 바꾼다. 이름과 센서 위치(비율)는 그대로."""
    try:
        data = await file.read()
        store.replace_floor_plan_image(plan_id, file.filename, data, width, height)
    except KeyError:
        raise HTTPException(404, "평면도가 없습니다") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _plans_body()


@router.patch("/api/site/plans/{plan_id}")
def site_plan_update(plan_id: int, body: dict):
    """본문 {"name": 새 이름} 또는 {"move": -1|1}(탭 순서 한 칸 앞·뒤)."""
    try:
        if "name" in body:
            store.rename_floor_plan(plan_id, body.get("name"))
        if "move" in body:
            store.move_floor_plan(plan_id, int(body.get("move") or 0))
    except KeyError:
        raise HTTPException(404, "평면도가 없습니다") from None
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return _plans_body()


@router.delete("/api/site/plans/{plan_id}")
def site_plan_delete(plan_id: int):
    """평면도 한 장과 그 위 센서 위치를 지운다. 그 센서들은 '배치 안 됨'이 된다."""
    removed = store.delete_floor_plan(plan_id)
    return dict(_plans_body(), removed=removed)


@router.post("/api/site/placements")
def site_placements(body: dict):
    """본문 {"items": [{plan_id, mac, x, y}]}로 모든 평면도의 위치를 통째로 바꾼다."""
    try:
        items = store.replace_placements(body.get("items") or [])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"placements": items}


DEFAULT_MQTT = {"host": "127.0.0.1", "port": 1883, "topic": "V/#,D/#", "csv": False, "auto": False}


@router.get("/api/site/mqtt")
def site_mqtt_get():
    """저장된 수신 MQTT 값과 지금 연결 상태."""
    cfg = dict(DEFAULT_MQTT, **(store.get_setting("mqtt") or {}))
    return {"settings": cfg, "receiver": _receiver()}


@router.post("/api/site/mqtt")
def site_mqtt_save(body: dict):
    """수신 MQTT 값을 저장한다. 자동 연결이 켜져 있으면 저장한 값으로 바로 다시 붙는다.

    본문: host, port, topic, csv(자동 연결 때 CSV 저장), auto(서버 시작 때 자동 연결).
    자동 연결을 끄면 저장만 하고 지금 연결은 건드리지 않는다(끊으려면 수신 모니터에서).
    """
    host = str(body.get("host") or "").strip()
    topic = str(body.get("topic") or "").strip()
    try:
        port = int(body.get("port"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Port는 숫자여야 합니다") from None
    if not host or not topic or not 0 < port < 65536:
        raise HTTPException(400, "Broker, Port, 구독 Topic을 확인해 주세요")
    cfg = {"host": host, "port": port, "topic": topic, "csv": bool(body.get("csv")), "auto": bool(body.get("auto"))}
    store.save_setting("mqtt", cfg)
    started = False
    if cfg["auto"]:
        try:
            started = monitor.auto_connect()
        except Exception as exc:
            raise HTTPException(400, f"저장했지만 연결 시작 실패: {exc}") from exc
    return {"settings": cfg, "started": started, "receiver": _receiver()}

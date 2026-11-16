"""/api/analysis. 그래프 기본값, g/LSB, SRSS 한계."""

from fastapi import APIRouter, HTTPException

from web_db.src import analysis as curves
from web_db.src import core as gen

router = APIRouter()


@router.get("/api/analysis/srss-limit")
def read_srss_limit(device: str = ""):
    """장치 SRSS 한계선을 읽는다."""
    return {"device": gen.srss_limit_key(device), "value": gen.srss_limit_get(device)}


@router.post("/api/analysis/srss-limit")
async def write_srss_limit(body: dict):
    """장치 SRSS 한계선을 저장한다."""
    try:
        value = gen.srss_limit_save(body.get("device"), body.get("value"))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"device": gen.srss_limit_key(body.get("device")), "value": value}


@router.get("/api/analysis/defaults")
def analysis_defaults():
    """그래프 옵션 기본값을 돌려준다."""
    return curves.default_options()


@router.post("/api/analysis/mems-scale")
async def save_mems_scale(body: dict):
    """화면에서 바꾼 g/LSB를 저장한다."""
    try:
        value = float(body.get("g_per_lsb"))
        gen.save_mems_g_per_lsb(value)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"g_per_lsb": value}


@router.post("/api/analysis/mems-correction")
async def save_mems_correction(body: dict):
    """화면에서 바꾼 MEMS 주파수·감도 보정을 저장한다."""
    try:
        saved = gen.save_mems_correction(body.get("correction") or body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"correction": saved}



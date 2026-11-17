"""화면 HTML. /, /generator, /studio, /dashboard."""

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

from web_db.src.paths import FRONT

router = APIRouter()


@router.get("/")
def home():
    """센서 수신 모니터. CSV와 SQLite에 수신 원본을 남긴다."""
    return FileResponse(FRONT / "index.html")


@router.get("/generator")
def generator_page():
    """데이터 분석 CSV. 분석 화면과 같은 배치이고 파일만 연다."""
    return FileResponse(FRONT / "generator.html")


@router.get("/studio")
def studio_page():
    """센서 데이터 분석. SQLite 수신 기록만 연다."""
    return FileResponse(FRONT / "studio.html")


@router.get("/dashboard")
def dashboard_page():
    """MAC별 최신 SRSS, RMS, PEAK."""
    return FileResponse(FRONT / "dashboard.html")


@router.get("/monitor")
def monitor_page():
    """수신 모니터 화면을 연다. 주소만 /monitor다."""
    return FileResponse(FRONT / "index.html")


@router.get("/archive")
def archive_page():
    """예전 보관 주소를 CSV 분석 화면으로 보낸다."""
    return RedirectResponse("/generator")



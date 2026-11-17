"""화면 HTML. /, /generator, /studio, /dashboard, /settings.

/generator와 /studio는 설정 서랍이 같다. 서랍은 front/partials/drawer.html 하나에 두고,
페이지를 보낼 때 `<!-- @include partials/drawer.html -->` 자리에 넣어 한 장의 HTML로 보낸다.
브라우저에서 따로 불러오지 않으므로, 스크립트가 처음 실행될 때 서랍의 입력 칸이 이미 있다.
"""

import re

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

from web_db.src.paths import FRONT

router = APIRouter()

_INCLUDE = re.compile(r"<!--\s*@include\s+([\w./-]+)\s*-->")


def _with_includes(name):
    """front/{name}을 읽고 @include 자리를 front 아래 파일 내용으로 바꾼다.

    요청마다 파일을 다시 읽는다. 서랍 HTML을 고치면 서버를 다시 켜지 않아도 새로고침에 반영된다.
    포함 경로는 front 폴더 밖으로 나갈 수 없다. 넣는 파일 안의 @include는 다시 풀지 않는다.
    """
    root = FRONT.resolve()

    def load(match):
        path = (FRONT / match.group(1)).resolve()
        if root not in path.parents:
            raise ValueError(f"front 밖의 파일은 넣을 수 없습니다: {match.group(1)}")
        return path.read_text(encoding="utf-8")

    html = (FRONT / name).read_text(encoding="utf-8")
    return HTMLResponse(_INCLUDE.sub(load, html))


@router.get("/")
def home():
    """센서 수신 모니터. CSV와 SQLite에 수신 원본을 남긴다."""
    return FileResponse(FRONT / "index.html")


@router.get("/generator")
def generator_page():
    """데이터 분석 CSV. 분석 화면과 같은 배치이고 파일만 연다. 서랍은 partials/drawer.html."""
    return _with_includes("generator.html")


@router.get("/studio")
def studio_page():
    """센서 데이터 분석. SQLite 수신 기록만 연다. 서랍은 partials/drawer.html."""
    return _with_includes("studio.html")


@router.get("/dashboard")
def dashboard_page():
    """MAC별 최신 SRSS, RMS, PEAK."""
    return FileResponse(FRONT / "dashboard.html")


@router.get("/settings")
def settings_page():
    """장치 표시 이름과 통계 SRSS 한계."""
    return FileResponse(FRONT / "settings.html")

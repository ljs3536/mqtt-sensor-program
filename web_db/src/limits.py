"""장치별 SRSS 한계. 데스크톱과 같은 사용자 홈 파일을 쓴다.

web 패키지를 import하지 않는다.
"""

from __future__ import annotations

import json
import math
from pathlib import Path


def srss_limit_path():
    """장치별 속도 SRSS 한계. 데스크톱과 같은 사용자 홈 파일을 쓴다."""
    return Path.home() / ".sensor-program" / "srss_limits.json"


def srss_limit_key(device):
    """장치 이름을 한계 파일의 키로 맞춘다."""
    return str(device or "").strip().upper().replace(":", "").replace("-", "")


def srss_limit_read():
    """사용자 홈의 SRSS 한계 JSON을 읽는다. 없으면 빈 목록이다."""
    path = srss_limit_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("SRSS 설정 JSON은 객체여야 합니다")
    return data


def srss_limit_get(device):
    """저장된 값이 없으면 4.5 mm/s RMS. 경보 판정이 아니라 그래프 기준선이다."""
    try:
        value = float(srss_limit_read().get(srss_limit_key(device), 4.5))
        return value if math.isfinite(value) and value > 0 else 4.5
    except (OSError, ValueError, TypeError):
        return 4.5


def srss_limit_save(device, value):
    """장치 SRSS 한계를 사용자 홈 JSON에 저장한다."""
    key = srss_limit_key(device)
    value = float(value)
    if not key or not math.isfinite(value) or value <= 0:
        raise ValueError("장치 ID와 0보다 큰 유한한 한계값이 필요합니다")
    path = srss_limit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = srss_limit_read()
    data[key] = value
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)
    return value


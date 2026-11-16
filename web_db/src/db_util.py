"""SQLite에 넣기 전의 작은 값 변환."""

from __future__ import annotations

from datetime import datetime

import numpy as np


def _iso(value=None):
    """시각 문자. 값이 없으면 지금 시각."""
    if value:
        return str(value)
    return datetime.now().isoformat(timespec="milliseconds")


def _samples_text(values, kind):
    """샘플 배열을 packet.decoded_samples 칸에 넣을 문자열로 만든다."""
    data = np.asarray(values, dtype=np.float64).reshape(-1)
    if kind == "ULTRASONIC":
        return ",".join(format(float(item), ".9g") for item in data)
    return ",".join(str(int(item)) for item in data)


def _float(value):
    """숫자가 아니면 None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _int(value):
    """정수가 아니면 None."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

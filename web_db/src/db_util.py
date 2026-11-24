"""SQLite에 넣기 전의 작은 값 변환."""

from __future__ import annotations

from datetime import datetime

import numpy as np


def _iso(value=None):
    """시각 문자. 값이 없으면 지금 시각."""
    if value:
        return str(value)
    return datetime.now().isoformat(timespec="milliseconds")


def _float(value):
    """숫자가 아니면 None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _rate(value):
    """samplerate 원본. 숫자면 숫자로, `0x0F`처럼 숫자가 아니면 글자 그대로 둔다. 비면 None."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    number = _float(text)
    return number if number is not None else text


def _int(value):
    """정수가 아니면 None."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

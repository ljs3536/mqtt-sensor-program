"""브라우저로 보내기 전에 그래프 점을 줄인다.

web 패키지를 import하지 않는다.
"""

from __future__ import annotations

import numpy as np


def _clean(values):
    """숫자가 아닌 값을 0으로 바꿔 그래프 계산에 쓴다."""
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return arr
    return np.where(np.isfinite(arr), arr, 0.0)


def _rounded(values):
    """화면으로 보낼 값을 소수 6자리로 줄인다."""
    return [round(float(value), 6) for value in values]


def _envelope_index(values, limit=16384):
    """Keep each bucket's min and max so peaks survive display thinning."""
    data = _clean(values)
    count = data.size
    if count <= limit:
        return np.arange(count)
    buckets = max(1, limit // 2)
    edges = np.linspace(0, count, buckets + 1).astype(int)
    chosen = []
    for start, stop in zip(edges[:-1], edges[1:]):
        if stop <= start:
            continue
        segment = data[start:stop]
        low = start + int(np.argmin(segment))
        high = start + int(np.argmax(segment))
        if low > high:
            low, high = high, low
        chosen.append(low)
        if high != low:
            chosen.append(high)
    return np.asarray(chosen, dtype=int)


def _take(values, index):
    """고른 인덱스의 값을 화면용 소수로 꺼낸다."""
    data = _clean(values)
    if data.size == 0:
        return []
    return _rounded(data[index])


def _pack_pair(x_values, y_values):
    """X·Y 쌍을 화면 점 수에 맞게 줄인다. 봉우리는 남긴다."""
    y_data = _clean(y_values)
    x_data = _clean(x_values)
    count = min(x_data.size, y_data.size)
    if count == 0:
        return [], []
    index = _envelope_index(y_data[:count])
    return _take(x_data[:count], index), _take(y_data[:count], index)


def _pack_curves(curves):
    """곡선 계산 결과를 브라우저 그래프 JSON으로 맞춘다."""
    packed = {
        "kind": curves["kind"],
        "x_label": curves.get("x_label") or "",
        "y_label": curves.get("y_label") or "",
        "fft_x_label": curves.get("fft_x_label") or "",
        "fft_label": curves.get("fft_label") or "",
        "info": curves.get("info") or "",
        "formula": curves.get("formula") or "",
        "avg_status": curves.get("avg_status") or "",
        "rotation": curves.get("rotation") or "",
        "fft_log_x": bool(curves.get("fft_log_x")),
        "fft_log_y": bool(curves.get("fft_log_y")),
        "series": {},
        "fft": {},
    }
    def _pack_trace(item):
        if not item:
            return None
        freq, amp = _pack_pair(item.get("f"), item.get("y"))
        return {
            "ok": bool(item.get("ok")),
            "text": item.get("text") or "",
            "unit": item.get("unit") or "",
            "device": item.get("device") or "",
            "level": None if item.get("level") is None else float(item["level"]),
            "value": None if item.get("value") is None else float(item["value"]),
            "f": freq,
            "y": amp,
        }
    for key in ("srss", "gn", "srss_limit"):
        packed_trace = _pack_trace(curves.get(key))
        if packed_trace:
            packed[key] = packed_trace
    if curves["kind"] == "spectrum":
        packed["x"], packed["y"] = _pack_pair(curves.get("x"), curves.get("y"))
        return packed
    for axis, series in (curves.get("series") or {}).items():
        t_values, y_values = _pack_pair(series["t"], series["y"])
        packed["series"][axis] = {"t": t_values, "y": y_values}
    for axis, series in (curves.get("fft") or {}).items():
        f_values, y_values = _pack_pair(series["f"], series["y"])
        packed["fft"][axis] = {"f": f_values, "y": y_values}
    return packed

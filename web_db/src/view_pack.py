"""브라우저로 보내기 전에 그래프 점을 줄인다. 계산이 끝난 배열만 대상으로 한다."""

from __future__ import annotations

import math

import numpy as np


def _clean(values):
    """숫자가 아닌 값을 0으로 바꿔 그래프 계산에 쓴다."""
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return arr
    return np.where(np.isfinite(arr), arr, 0.0)


def _rounded(values):
    """화면으로 보낼 Y값을 줄인다: 소수 6자리와 유효숫자 7자리 중 더 자세한 쪽.

    목적은 JSON 크기(브라우저로 보내는 글자 수)를 줄이는 것이다. 화면 속도와는 거의 관계없다.
    예전에는 소수 6자리 고정이라 0.0000005보다 작은 값(PSD (g)²/Hz 등)이 0이 됐다
    (2026-11-23 확인: PSD 257점 중 10점이 0, Y 로그 눈금에서 점이 빠짐). 이제 1.556919e-07처럼 남는다.
    큰 값은 지금처럼 소수 6자리까지(41.849123).
    """
    out = []
    for value in values:
        value = float(value)
        if value == 0.0:
            out.append(0.0)
            continue
        digits = max(6, 6 - int(math.floor(math.log10(abs(value)))))
        out.append(round(value, digits))
    return out


def _rounded_x(values):
    """화면으로 보낼 X값(시간 ms, 주파수 kHz/Hz): 유효숫자 12자리.

    X는 점 위치라서 반올림하면 툴팁·고정 커서에 실제 값이 안 보인다(0.012890625 kHz가 0.012891).
    12자리는 부동소수 꼬리(0.012890625000000001)만 지운다.
    """
    return [float(f"{float(value):.12g}") for value in values]


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


def _take(values, index, x_axis=False):
    """고른 인덱스의 값을 화면용 소수로 꺼낸다. x_axis면 X 규칙(_rounded_x)."""
    data = _clean(values)
    if data.size == 0:
        return []
    return _rounded_x(data[index]) if x_axis else _rounded(data[index])


def _pack_pair(x_values, y_values):
    """X·Y 쌍을 화면 점 수에 맞게 줄인다. 봉우리는 남긴다."""
    y_data = _clean(y_values)
    x_data = _clean(x_values)
    count = min(x_data.size, y_data.size)
    if count == 0:
        return [], []
    index = _envelope_index(y_data[:count])
    return _take(x_data[:count], index, x_axis=True), _take(y_data[:count], index)


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
        # Window 보정 모드와 실제 곱해진 보정계수 문구(서랍의 선택 상자 옆에 보인다).
        "window_correction": curves.get("window_correction") or "",
        "window_factor": curves.get("window_factor") or "",
        "avg_status": curves.get("avg_status") or "",
        "rotation": curves.get("rotation") or "",
        "fft_log_x": bool(curves.get("fft_log_x")),
        "fft_log_y": bool(curves.get("fft_log_y")),
        "series": {},
        "fft": {},
    }
    def _pack_trace(item):
        """SRSS·gN·한계선 곡선 하나의 점을 줄여 JSON에 넣을 수 있게 만든다. 곡선이 없으면 None."""
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
    # PSD 모드 결과 칸(합성 SRSS, 한계값, 판정, 연산 단계). 곡선은 위 fft/gn에 들어 있다.
    psd = curves.get("psd")
    if psd:
        packed["psd"] = {
            "on": True,
            "srss": psd.get("srss") or "",
            "limit": psd.get("limit") or "",
            "judgement": psd.get("judgement") or "",
            "state": psd.get("state") or "none",
            "steps": psd.get("steps") or "",
            "title": psd.get("title") or "",
            "error": psd.get("error") or "",
            "unit": psd.get("unit") or "",
            "level": psd.get("level"),
            "axis_rms": {k: float(v) for k, v in (psd.get("axis_rms") or {}).items()},
        }
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

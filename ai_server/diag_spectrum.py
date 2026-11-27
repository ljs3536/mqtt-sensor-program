"""CNN, AE 진단모델용 스펙트럼.

그래프 Window와 분리한다. 학습 때 고른 Window를 체크포인트에 저장하고, 추론은 그 값을 사용한다.
"""

import numpy as np

WINDOWS = (
    "Hann",
    "Hamming",
    "Blackman",
    "Blackman-Harris",
    "Flat Top",
    "Bartlett",
    "Kaiser",
    "Rectangular",
)


def normalize_window(name):
    key = str(name or "Hann").strip().lower()
    for item in WINDOWS:
        if item.lower() == key:
            return item
    raise ValueError("지원하지 않는 Window입니다. " + ", ".join(WINDOWS))


def _bessel_i0(value):
    total = 1.0
    term = 1.0
    square = (value * value) / 4.0
    for index in range(1, 50):
        term = (term * square) / (index * index)
        total += term
        if term < 1e-15:
            break
    return total


def window_values(length, window_type):
    window_type = normalize_window(window_type)
    if length <= 1 or window_type == "Rectangular":
        return np.ones((length,), dtype=np.float64)
    index = np.arange(length, dtype=np.float64)
    span = float(length - 1)
    a1 = (2.0 * np.pi * index) / span
    a2 = (4.0 * np.pi * index) / span
    a3 = (6.0 * np.pi * index) / span
    a4 = (8.0 * np.pi * index) / span
    if window_type == "Hann":
        return 0.5 - 0.5 * np.cos(a1)
    if window_type == "Hamming":
        return 0.54 - 0.46 * np.cos(a1)
    if window_type == "Blackman":
        return 0.42 - 0.5 * np.cos(a1) + 0.08 * np.cos(a2)
    if window_type == "Blackman-Harris":
        return 0.35875 - 0.48829 * np.cos(a1) + 0.14128 * np.cos(a2) - 0.01168 * np.cos(a3)
    if window_type == "Flat Top":
        return 0.21557895 - 0.41663158 * np.cos(a1) + 0.277263158 * np.cos(a2) - 0.083578947 * np.cos(a3) + 0.006947368 * np.cos(a4)
    if window_type == "Bartlett":
        return 1.0 - np.abs((index - span / 2.0) / (span / 2.0))
    beta = 8.6
    alpha = span / 2.0
    ratio = (index - alpha) / alpha
    return _bessel_i0(beta * np.sqrt(np.maximum(0.0, 1.0 - ratio * ratio))) / _bessel_i0(beta)


def fft_magnitude(signal, window_type="Hann"):
    wave = np.asarray(signal, dtype=np.float64)
    if wave.size < 8:
        raise ValueError("FFT에 넣을 파형이 너무 짧습니다. 길이 %d" % wave.size)
    centered = wave - float(wave.mean())
    window = window_values(centered.size, window_type)
    window_sum = float(window.sum()) or float(centered.size)
    spectrum = np.fft.rfft(centered * window)
    magnitude = np.abs(spectrum[: centered.size // 2]) * (2.0 / window_sum)
    return magnitude.astype(np.float32)


def fft_magnitudes(signals, window_type="Hann"):
    window_type = normalize_window(window_type)
    rows = [fft_magnitude(row, window_type) for row in signals]
    widths = {row.size for row in rows}
    if len(widths) != 1:
        raise ValueError("FFT 변환 후 스펙트럼 길이가 서로 다릅니다.")
    return np.asarray(rows, dtype=np.float32)

"""완성된 ADXL X/Y/Z 한 세트의 SRSS, RMS, PEAK.

수신 콜백에서는 호출하지 않는다. 통계 최신화 때만 쓴다.
SRSS g는 가속도 스펙트럼이고, srss_mms는 같은 스펙트럼을 mm/s RMS로 바꾼 값이다.
"""

from __future__ import annotations

import numpy as np

from web import core as gen


def _parse_samples(text):
    """쉼표로 저장된 샘플 문자열을 배열로 읽는다."""
    if not str(text or "").strip():
        return np.array([], dtype=np.float64)
    return np.fromstring(str(text), sep=",", dtype=np.float64)


def adxl_set_stats(found):
    """X/Y/Z 행 dict에서 RMS, PEAK, SRSS(g RMS), SRSS(mm/s RMS)를 계산한다."""
    g_per_lsb = float(gen.read_mems_g_per_lsb())
    rates = []
    counts = []
    rms_parts = []
    powers = []
    velocity_powers = []
    peak = 0.0
    for axis in ("X", "Y", "Z"):
        row = found[axis]
        samples = _parse_samples(row["decoded_samples"])
        rate = float(row["sample_rate"] or 0)
        count = int(samples.size)
        if count < 2 or rate <= 0:
            raise ValueError("ADXL 통계에는 샘플 2개 이상과 샘플링 속도가 필요합니다")
        rates.append(rate)
        counts.append(count)
        grams = (samples - np.mean(samples)) * g_per_lsb
        rms_parts.append(float(np.sqrt(np.mean(np.square(grams)))))
        peak = max(peak, float(np.max(np.abs(grams))))
        freq, mag = gen.fft_peak(samples, rate, "Blackman")
        grams_peak = mag * g_per_lsb
        window = gen.fft_window(count, "Blackman")
        powers.append(gen.srss_axis_power(grams_peak, count, window))
        velocity = np.zeros_like(grams_peak)
        positive = freq > 0
        velocity[positive] = grams_peak[positive] * 9806.65 / (2.0 * np.pi * freq[positive])
        velocity_powers.append(gen.srss_axis_power(velocity, count, window))
    if len(set(rates)) != 1 or len(set(counts)) != 1:
        raise ValueError("ADXL X/Y/Z 샘플 수 또는 샘플링 속도가 다릅니다")
    return {
        "sample_rate": rates[0],
        "sample_count": counts[0],
        "rms": float(np.sqrt(np.sum(np.square(rms_parts)))),
        "peak": peak,
        "srss": float(np.sqrt(np.sum(powers))),
        "srss_mms": float(np.sqrt(np.sum(velocity_powers))),
        "g_per_lsb": g_per_lsb,
    }

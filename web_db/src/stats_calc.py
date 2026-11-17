"""완성된 ADXL X/Y/Z 한 세트의 SRSS, RMS, PEAK.

수신 콜백에서는 호출하지 않는다. 통계 최신화 때만 쓴다.
SRSS g는 가속도 스펙트럼이고, srss_mms는 같은 스펙트럼을 mm/s RMS로 바꾼 값이다.
"""

from __future__ import annotations

import numpy as np

from web_db.src import core as gen


def _parse_samples(text):
    """쉼표로 저장된 샘플 문자열을 배열로 읽는다."""
    if not str(text or "").strip():
        return np.array([], dtype=np.float64)
    return np.fromstring(str(text), sep=",", dtype=np.float64)


def adxl_set_stats(found):
    """X/Y/Z 행 dict에서 RMS, PEAK, SRSS(g RMS), SRSS(mm/s RMS)를 계산한다.

    found는 {"X": row, "Y": row, "Z": row}이고 row는 packet 줄이다.
    샘플 단위는 LSB이고, 지금 저장된 1 LSB(g)를 곱해 g로 바꾼다.
    다음 경우는 ValueError로 이 세트를 버린다(최신화는 failed_sets로 센다).
    - 축 하나라도 샘플이 2개 미만이거나 샘플링 속도를 Hz로 읽을 수 없을 때
    - 세 축의 샘플 수 또는 샘플링 속도가 서로 다를 때
    그래프의 필터, Window N, 주파수·감도 보정은 넣지 않는다. 창은 Blackman 고정이다.
    """
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
        # packet에는 수신 값 그대로 있다. 그래프와 같은 규칙으로 장비 코드(10~15, 0x0A~0x0F)를 Hz로 읽는다.
        raw_rate = row["sample_rate"]
        rate = float(gen.normalize_sample_rate("ADXL", raw_rate, 0)) if raw_rate not in (None, "") else 0.0
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


def summarize_mac(set_stats):
    """완성 세트들의 RMS 평균, PEAK 최댓값, SRSS 평균을 MAC 한 줄로 모은다."""
    if not set_stats:
        raise ValueError("완성 세트가 없습니다")
    rates = {item["sample_rate"] for item in set_stats}
    counts = {item["sample_count"] for item in set_stats}
    return {
        "sample_rate": next(iter(rates)) if len(rates) == 1 else None,
        "sample_count": next(iter(counts)) if len(counts) == 1 else None,
        "rms": float(np.mean([item["rms"] for item in set_stats])),
        "peak": float(max(item["peak"] for item in set_stats)),
        "srss": float(np.mean([item["srss"] for item in set_stats])),
        "srss_mms": float(np.mean([item["srss_mms"] for item in set_stats])),
        "g_per_lsb": set_stats[-1]["g_per_lsb"],
        "set_count": len(set_stats),
    }

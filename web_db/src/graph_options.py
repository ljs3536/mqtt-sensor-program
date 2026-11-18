"""그래프 옵션 기본값과 파형 HEX 해석."""

from __future__ import annotations

import numpy as np

from web_db.src import core as gen
from web_db.src import core as mon


PIEZO_UNITS = ("Count", "mV", "g RMS", "mm/s RMS")
PIEZO_WINDOWS = ("Blackman", "Hann", "Hamming", "Rectangular")
SRSS_MISSING = "SRSS: 완전한 XYZ 데이터 없음"
ADXL_RAW_UNITS = ("자동 (g/mg/µg)", "Count", "g", "mg", "µg")
ADXL_FFT_UNITS = ("자동 (g RMS)", "Count Peak", "g Peak", "g RMS", "mm/s RMS")


def _flag(value, default=False):
    """화면 값을 켜짐 또는 꺼짐으로 읽는다."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "off", "no", ""}


def _num(value, default):
    """화면 값을 유한한 수로 읽는다. 아니면 기본값이다."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not np.isfinite(number):
        return float(default)
    return number


def _merge(base, raw):
    """화면에서 온 옵션을 기본 옵션 위에 덮어쓴다."""
    if not isinstance(raw, dict):
        return base
    merged = dict(base)
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _filter_block(hpf_hz, lpf_hz):
    """HPF, LPF, Notch의 기본 칸을 만든다."""
    return {
        "HPF": {"raw": False, "fft": False, "freq": hpf_hz, "extra": 4},
        # auto: '자동 Fs/2'(수신기 V057·발생기 v070 기본값). 켜져 있으면 LPF를 걸지 않는다.
        "LPF": {"raw": False, "fft": False, "freq": lpf_hz, "extra": 4, "auto": True},
        "Notch": {"raw": False, "fft": False, "freq": 975, "extra": 20},
    }


def default_options():
    """그래프 옵션의 기본값을 만든다."""
    correction = gen.load_mems_correction()
    return {
        "fft_log_x": False,
        "fft_log_y": False,
        "fft_avg": False,
        "fft_avg_count": "16",
        # PSD(Welch) 모드. 켜면 FFT 칸이 PSD로 바뀌고 FFT 평균은 하지 않는다(graph_psd.py).
        # segment: 구간 N(전체/8192…128), overlap: %, target: 축마다 평균할 목표 구간 수.
        "psd": {"on": False, "segment": "전체", "overlap": 50, "target": 30},
        "piezo": {
            "unit": "mV",
            "vmax": 3.89,
            "amax": 4096,
            "sensitivity": 100,
            "window": "Blackman",
            # Window 보정: 보정 없음 / 진폭 보정 / 에너지 보정. signal.WINDOW_CORRECTIONS 참고.
            "window_correction": gen.DEFAULT_WINDOW_CORRECTION,
            "window_n": "전체",
            "filters": _filter_block(10, 3000),
        },
        "adxl": {
            "axes": {"X": True, "Y": True, "Z": True, "gN": True, "SRSS": True},
            "raw_unit": "자동 (g/mg/µg)",
            "fft_unit": "자동 (g RMS)",
            "g_per_lsb": gen.read_mems_g_per_lsb(),
            "window": "Blackman",
            "window_correction": gen.DEFAULT_WINDOW_CORRECTION,
            "window_n": "전체",
            "beta": 8.6,
            "filters": _filter_block(10, 1000),
            "correction": correction,
            "rpm": 1800,
            "rot_axis": "X",
            "rot_type": "미지정",
        },
    }


def resolve_options(raw):
    """요청 옵션과 기본 옵션을 합친다."""
    return _merge(default_options(), raw or {})


def _filter_kwargs(filters, kind):
    """필터 칸을 필터 함수 인자로 바꾼다."""
    filters = filters or {}
    hpf = filters.get("HPF") or {}
    lpf = filters.get("LPF") or {}
    notch = filters.get("Notch") or {}
    field = "raw" if kind == "RAW" else "fft"
    return dict(
        hpf=_flag(hpf.get(field)),
        hp_cutoff=_num(hpf.get("freq"), 10),
        hp_order=max(1, int(_num(hpf.get("extra"), 4))),
        lpf=_flag(lpf.get(field)),
        # 자동 Fs/2면 None → spectral_filter가 LPF를 건너뛴다(차단 주파수 = Nyquist = 전 대역 통과).
        lp_cutoff=None if _flag(lpf.get("auto")) else _num(lpf.get("freq"), 3000),
        lp_order=max(1, int(_num(lpf.get("extra"), 4))),
        notch=_flag(notch.get(field)),
        notch_center=_num(notch.get("freq"), 975),
        notch_width=_num(notch.get("extra"), 20),
    )


def _window_n(value):
    """FFT 창 길이. 전체면 길이를 제한하지 않는다."""
    text = str(value or "전체").strip()
    if text in {"", "전체", "0"}:
        return None
    count = int(float(text))
    if count < 2:
        raise ValueError("FFT Window N은 2 이상이어야 합니다")
    return count


def decode_wave(sensor, obj):
    """sensorData HEX → 부호 있는 16비트 Count. 시간축은 n / Fs 이고 Fs는 Hz다."""
    raw_hex = "".join(str(obj.get("sensorData") or "").split())
    raw = bytes.fromhex(raw_hex)
    if len(raw) % 2:
        raise ValueError(f"{sensor} bytes 길이는 짝수여야 합니다")
    samples = np.frombuffer(raw, dtype=">i2").astype(np.float64)
    default = 3200.0 if sensor == "ADXL" else 25600.0
    rate = mon.normalize_sample_rate(sensor, obj.get("samplerate"), default)
    return samples, float(rate)


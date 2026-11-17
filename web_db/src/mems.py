"""MEMS g/LSB와 주파수·감도 보정 파일."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

APP_DIR = Path(__file__).resolve().parents[2]

MEMS_SCALE_FILENAME = "mems_lsb_scale.json"
MEMS_DEFAULT_G_PER_LSB = 1.0 / 256.0

def read_mems_g_per_lsb():
    """프로젝트 루트 JSON에서 1 LSB당 g를 읽는다. 없으면 1/256이다."""
    try:
        path = APP_DIR / MEMS_SCALE_FILENAME
        value = float(json.loads(path.read_text(encoding="utf-8"))["g_per_lsb"])
        if math.isfinite(value) and 0 < value <= 10:
            return value
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return MEMS_DEFAULT_G_PER_LSB

def save_mems_g_per_lsb(value):
    """1 LSB당 g를 프로젝트 루트 JSON에 저장한다."""
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 10:
        raise ValueError("1 LSB당 g 값은 0보다 크고 10 이하여야 합니다.")
    path = APP_DIR / MEMS_SCALE_FILENAME
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps({"g_per_lsb": value}, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)



# V022/V027: MEMS FFT display calibration; never changes raw packets or MQTT.
MEMS_CORRECTION_FILENAME = "mems_frequency_sensitivity.json"
MEMS_CORRECTION_DEFAULTS = {
    "frequency_enabled": True,
    "sensitivity_enabled": True,
    "frequency_divisor": 1.014,
    "low_hz": 60.0,
    "high_hz": 360.0,
    "low_sensitivity": 0.99,
    "mid_intercept": 1.07,
    "mid_slope": 0.0005,
    "high_sensitivity": 0.89,
}

def validate_mems_correction(values):
    """주파수·감도 보정 값이 범위 안인지 확인하고 기본값을 채운다."""
    result = dict(MEMS_CORRECTION_DEFAULTS)
    result.update(values)
    for name in ("frequency_divisor", "low_hz", "high_hz", "low_sensitivity",
                 "mid_intercept", "mid_slope", "high_sensitivity"):
        result[name] = float(result[name])
        if not math.isfinite(result[name]):
            raise ValueError(f"MEMS 보정 {name}: 유효한 숫자가 필요합니다.")
    if result["frequency_divisor"] <= 0:
        raise ValueError("주파수 나눗수는 0보다 커야 합니다.")
    if not 0 <= result["low_hz"] < result["high_hz"]:
        raise ValueError("주파수 경계는 0 <= 하한 < 상한이어야 합니다.")
    if result["low_sensitivity"] <= 0 or result["high_sensitivity"] <= 0:
        raise ValueError("저주파/고주파 감도는 0보다 커야 합니다.")
    for f in (result["low_hz"], result["high_hz"]):
        if result["mid_intercept"] - f * result["mid_slope"] <= 0:
            raise ValueError("중간 구간 전체에서 감도가 양수여야 합니다.")
    result["frequency_enabled"] = bool(result["frequency_enabled"])
    result["sensitivity_enabled"] = bool(result["sensitivity_enabled"])
    return result

def load_mems_correction():
    """보정 JSON을 읽는다. 없으면 기본 보정이다."""
    try:
        path = APP_DIR / MEMS_CORRECTION_FILENAME
        return validate_mems_correction(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError):
        return dict(MEMS_CORRECTION_DEFAULTS)

def save_mems_correction(values):
    """화면에서 바꾼 MEMS 주파수·감도 보정을 저장한다."""
    values = validate_mems_correction(values)
    path = APP_DIR / MEMS_CORRECTION_FILENAME
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(values, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)
    return values

def mems_sensitivity(measured_hz, config):
    """측정 주파수에서 감도 S(f)를 계산한다."""
    f = np.asarray(measured_hz, dtype=np.float64)
    result = np.where(f <= config["low_hz"], config["low_sensitivity"],
                      np.where(f <= config["high_hz"],
                               config["mid_intercept"] - f * config["mid_slope"],
                               config["high_sensitivity"]))
    if np.any(result <= 0):
        raise ValueError("MEMS 보정 감도가 0 이하입니다.")
    return result

def corrected_mems_fft(measured_hz, g_amplitude, config, *, apply_sensitivity=True):
    """FFT 주파수와 진폭에 주파수 나눗수와 감도 보정을 적용한다."""
    frequency = np.asarray(measured_hz, dtype=np.float64)
    amplitude = np.asarray(g_amplitude, dtype=np.float64)
    if config["sensitivity_enabled"] and apply_sensitivity:
        amplitude = amplitude / mems_sensitivity(frequency, config)
    if config["frequency_enabled"]:
        frequency = frequency / config["frequency_divisor"]
    return frequency, amplitude

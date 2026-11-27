"""MEMS g/LSB와 주파수·감도 보정 파일."""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

import numpy as np

from web_db.src.paths import MEMS_DEFAULT_DIR, MEMS_DIR

# 저장 위치. Docker에서는 SENSOR_DATA_DIR(볼륨)이고, 그냥 실행하면 web_db/다(paths.py, 2026-11-27 전에는 저장소 루트).
APP_DIR = MEMS_DIR


def _settings_file(name):
    """읽을 보정 파일. 데이터 폴더에 아직 없으면(Docker 첫 실행) web_db/의 기본 파일을 읽는다."""
    path = APP_DIR / name
    return path if path.exists() else MEMS_DEFAULT_DIR / name


# ---------- 설정 파일 읽기 캐시 (2026-11-23) ----------
# FFT 평균 380세트를 그리면 패킷·축마다 g/LSB와 보정 파일을 다시 읽어 디스크 읽기만 약 2초가 걸렸다.
# 같은 파일은 1초 안에는 다시 읽지 않고, 1초가 지나면 수정 시각(mtime)이 바뀌었을 때만 다시 읽는다.
# 이 서버의 저장 함수(save_*)는 저장 직후 캐시를 비우므로 화면에서 바꾼 값은 바로 반영된다.
# 파일을 손으로 고친 경우도 1초 안에 반영된다.
_READ_TTL = 1.0
_read_cache = {}
_read_lock = threading.Lock()


def _cached_read(name, loader):
    """설정 파일 name을 loader(path)로 읽되, 위 규칙으로 캐시한다."""
    now = time.monotonic()
    with _read_lock:
        hit = _read_cache.get(name)
    if hit and now - hit["checked"] < _READ_TTL:
        return hit["value"]
    path = _settings_file(name)
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        stamp = (str(path), None)
    value = hit["value"] if hit and hit["stamp"] == stamp else loader(path)
    with _read_lock:
        _read_cache[name] = {"checked": now, "stamp": stamp, "value": value}
    return value


def _forget(name):
    """저장 직후 캐시를 비워 다음 읽기가 새 파일을 보게 한다."""
    with _read_lock:
        _read_cache.pop(name, None)


MEMS_SCALE_FILENAME = "mems_lsb_scale.json"
MEMS_DEFAULT_G_PER_LSB = 1.0 / 256.0

def read_mems_g_per_lsb():
    """데이터 폴더(없으면 저장소 루트) JSON에서 1 LSB당 g를 읽는다. 없으면 1/256이다. 1초 캐시(_cached_read)."""
    return _cached_read(MEMS_SCALE_FILENAME, _load_g_per_lsb)


def _load_g_per_lsb(path):
    """g/LSB 파일 하나를 실제로 읽는다."""
    try:
        value = float(json.loads(path.read_text(encoding="utf-8"))["g_per_lsb"])
        if math.isfinite(value) and 0 < value <= 10:
            return value
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return MEMS_DEFAULT_G_PER_LSB

def save_mems_g_per_lsb(value):
    """1 LSB당 g를 데이터 폴더 JSON에 저장한다(그냥 실행하면 저장소 루트)."""
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 10:
        raise ValueError("1 LSB당 g 값은 0보다 크고 10 이하여야 합니다.")
    path = APP_DIR / MEMS_SCALE_FILENAME
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps({"g_per_lsb": value}, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    _forget(MEMS_SCALE_FILENAME)



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
    """보정 JSON을 읽는다. 없으면 기본 보정이다. 1초 캐시(_cached_read). 호출 쪽이 고쳐도 캐시가 안 바뀌게 복사해 준다."""
    return dict(_cached_read(MEMS_CORRECTION_FILENAME, _load_correction))


def _load_correction(path):
    """보정 파일 하나를 실제로 읽는다."""
    try:
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
    _forget(MEMS_CORRECTION_FILENAME)
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

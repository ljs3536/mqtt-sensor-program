# SRSS: overall AC RMS; FFT display only.
# V038: add FFT-only MEMS gN norm (purple #9467BD) to all post-processing displays.
# V037: MEMS FFT velocity RMS display option (mm/s); preserves acceleration, RAW, and MQTT.
# V034: IFB-VIBR-HALOW-04 mode A command protocol compatibility.
# V026: MEMS 1 LSB(g) user scale with persistence; RAW/FFT/RMS/statistics use same conversion.
# V021: Raw/FFT and ultrasonic viewers: left+wheel zoom X, right+wheel zoom Y; default wheel unchanged.
# V018: isolate PIEZO per-device voltage persistence from graph store registration; report viewer errors.
# V020: click-to-pin RAW/FFT data cursor, tracking the same X coordinate across live updates; preserves V019 behavior.
# V016: ADXL ONLY - temporarily disable 7D5E/7D5D unescape. PIEZO/ULTRASONIC unchanged.
# V015: show detailed RAW/FFT formulas immediately for selected PIEZO display unit.
# V014: auto-register new PIEZO sensor voltage on first valid packet; graph still edits per sensor.
# NEW PIEZO ADC V014 - user-editable effective Vmax (default 3.890V), consistent P-P display scale.
# Based on V011; originals preserved. RAW centering affects display only.
# V011 change from V010: ADC Vmax 3.82 -> 3.89 V ONLY; Amax=4096 and other logic unchanged.
# IMPORTANT: signed incoming counts remain 16-bit and are NOT valid signed 12-bit codes.
# 12-bit voltage scaling here is an experimental scale applied to original 16-bit words.
# NEW PIEZO ADC V008 (derived from V007; originals preserved)
# V008 historical note: prior 16bit/3.3V calculation below was replaced with V009 test scale,
# g=v/100 -> g_mean -> sqrt(mean((g-g_mean)**2)),
# per-bin velocity_rms=FFT_bin_g_rms*9806.65/(2*pi*f).
# IMPORTANT: +32768 is present in ADC absolute voltage, but cancels in AC RMS.
# This does not by itself resolve the observed ~19x shaker discrepancy.
# NEW PIEZO ADC V006 (derived from V005; originals preserved)
# Fix: g_spec FFT hover label follows plotted g RMS, not count Peak.
# Fix: manual Y-axis labels show g/g RMS for sensor-spec acceleration mode.
# NEW PIEZO ADC V003 (derived from V002; previous versions untouched)
# ADC: signed 16-bit -32768..32767; voltage=(ADC+32768)*3.3/65536.
# Auto zero: mean of all samples in the received data block, NOT necessarily one vibration cycle.
# V_AC[n]=(ADC[n]-mean(ADC))*3.3/65536.
# V_RMS = sqrt(mean(V_AC**2)), g_RMS = V_RMS/0.1 only if ADC voltage follows sensor voltage 1:1.
# FFT peak g RMS is single component; not total g RMS. No historical 12mV/1g or -14400 calibration.

import csv
import json
import math
import multiprocessing as mp
import queue
import re
import sys
import threading
import time
import zlib
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox

import paho.mqtt.client as mqtt
import numpy as np

try:
    import serial
    from serial.tools import list_ports
    SERIAL_AVAILABLE = True
except Exception:
    serial = None
    list_ports = None
    SERIAL_AVAILABLE = False


APP_TITLE = '수신기 V052 — gN / SRSS 한계값'
PIEZO_DEFAULT_ZERO_COUNT = 0.0  # V001: 이전 -14400 보정 비활성화. 자동 0점=수신 샘플 평균.
PIEZO_ADC_COUNTS = 4096.0
PIEZO_ADC_VMAX = 3.89
PIEZO_SENSOR_MV_PER_G = 100.0  # 공칭 감도; 아날로그 회로의 이득/감쇠는 미검증
PIEZO_SENSOR_MAX_G = 50.0  # 센서 정격: 50 g -> 5000 mV (100 mV/g)
PIEZO_ACCEL_MM_PER_S2_PER_G = 9806.65


def piezo_hw_adc_to_mv(adc_counts, vmax_v=PIEZO_ADC_VMAX, amax=PIEZO_ADC_COUNTS):
    """시험 환산: v(t)[mV] = 1000*Vmax/Amax * (C(t)+Amax/2).

    V011은 16bit 수신 코드에 12bit/3.89V 계수를 시험 적용하므로
    산출된 절대전압은 물리적으로 유효하지 않을 수 있다. RMS/FFT AC 진폭 검증용.
    """
    adc = np.asarray(adc_counts, dtype=np.float64)
    return (1000.0 * vmax_v / amax) * (adc + amax / 2.0)


def piezo_hw_metrics(adc_counts, vmax_v=PIEZO_ADC_VMAX, amax=PIEZO_ADC_COUNTS):
    """하드웨어 문서의 계산 순서: C -> v(t) -> g(t) -> g_mean -> g_RMS.

    g(t)=v(t)[mV]/(100 mV/g), g_RMS=sqrt(mean((g-g_mean)**2)).
    원본 ADC의 DC 바이어스는 g_mean 제거 시 함께 소거된다.
    """
    volt_mv = piezo_hw_adc_to_mv(adc_counts, vmax_v, amax)
    if volt_mv.size == 0:
        return {"voltage_mv": volt_mv, "voltage_mean_mv": 0.0,
                "g": volt_mv.copy(), "g_mean": 0.0, "g_ac": volt_mv.copy(),
                "g_rms": 0.0, "voltage_ac_rms_mv": 0.0}
    g = volt_mv / PIEZO_SENSOR_MV_PER_G
    g_mean = float(np.mean(g))
    g_ac = g - g_mean
    g_rms = float(np.sqrt(np.mean(np.square(g_ac))))
    return {"voltage_mv": volt_mv, "voltage_mean_mv": float(np.mean(volt_mv)),
            "g": g, "g_mean": g_mean, "g_ac": g_ac,
            "g_rms": g_rms, "voltage_ac_rms_mv": g_rms * PIEZO_SENSOR_MV_PER_G}


def piezo_hw_fft_grms(fft_peak_counts, vmax_v=PIEZO_ADC_VMAX, amax=PIEZO_ADC_COUNTS):
    """정규화된 1-sided FFT peak Count -> 해당 bin 정현파 가속도 g RMS."""
    return (np.asarray(fft_peak_counts, dtype=np.float64)
            * (vmax_v * 1000.0 / amax)
            / PIEZO_SENSOR_MV_PER_G / np.sqrt(2.0))


def piezo_hw_velocity_rms_mms(grms, frequency_hz):
    """단일 FFT 주파수 성분만: v_RMS[mm/s] = G_RMS*9806.65/(2*pi*f)."""
    f = np.asarray(frequency_hz, dtype=np.float64)
    a = np.asarray(grms, dtype=np.float64)
    result = np.zeros(np.broadcast_shapes(f.shape, a.shape), dtype=np.float64)
    np.divide(a * PIEZO_ACCEL_MM_PER_S2_PER_G, 2 * np.pi * f,
              out=result, where=f > 0)
    return result

DEFAULT_BROKER = "192.168.1.62"
DEFAULT_PORT = 1883
DEFAULT_TOPIC = "V/#,D/#"
DEFAULT_QOS = 1
DEFAULT_SERIAL_BAUD = 230400

ADXL_RATE_TO_CODE = {
    "25": "0x08",
    "50": "0x09",
    "100": "0x0A",
    "200": "0x0B",
    "400": "0x0C",
    "800": "0x0D",
    "1600": "0x0E",
    "3200": "0x0F",
}

# FFT X축 기준표에 정의된 사용자 선택값.
# 마지막 조합이 장비 기본값이다.
PIEZO_SAMPLE_RATES = ("512", "1280", "2560", "5120", "12800", "25600")
ADXL_SAMPLE_RATES = ("100", "200", "400", "800", "1600", "3200")
SAMPLE_COUNTS = ("1024", "2048", "4096", "8192")

# UI에서는 Sample Rate와 Sample Count를 따로 고르지 않고
# "Fs + N" 하나의 세트로 선택한다.
PIEZO_SAMPLE_PROFILES = tuple(
    (rate, count, f"{rate} Hz + {count}")
    for rate in PIEZO_SAMPLE_RATES
    for count in SAMPLE_COUNTS
)
ADXL_SAMPLE_PROFILES = tuple(
    (rate, count, f"{rate} Hz + {count}")
    for rate in ADXL_SAMPLE_RATES
    for count in SAMPLE_COUNTS
)

PIEZO_PROFILE_LABELS = tuple(item[2] for item in PIEZO_SAMPLE_PROFILES)
ADXL_PROFILE_LABELS = tuple(item[2] for item in ADXL_SAMPLE_PROFILES)

PIEZO_PROFILE_MAP = {
    label: (rate, count)
    for rate, count, label in PIEZO_SAMPLE_PROFILES
}
ADXL_PROFILE_MAP = {
    label: (rate, count)
    for rate, count, label in ADXL_SAMPLE_PROFILES
}

# ADXL 수신 payload의 samplerate가 0x0A~0x0F 코드로 올 수도 있고
# 실제 Hz(100~3200)로 올 수도 있으므로 양쪽을 모두 처리한다.
# ADXL343 Full Resolution mode.
# Data sheet nominal sensitivity:
#   256 LSB/g (about 3.9 mg/LSB) for all g ranges in Full Resolution.
ADXL_FULL_RES_LSB_PER_G = 256.0
# V026: User-adjustable MEMS physical scale (display/calculation only).
# ADXL343 full-resolution nominal: 1 / 256 g per signed count.
MEMS_SCALE_FILENAME = "mems_lsb_scale.json"
MEMS_DEFAULT_G_PER_LSB = 1.0 / 256.0

def read_mems_g_per_lsb():
    try:
        path = Path(__file__).resolve().parent / MEMS_SCALE_FILENAME
        value = float(json.loads(path.read_text(encoding="utf-8"))["g_per_lsb"])
        if math.isfinite(value) and 0 < value <= 10:
            return value
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return MEMS_DEFAULT_G_PER_LSB

def save_mems_g_per_lsb(value):
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 10:
        raise ValueError("1 LSB당 g 값은 0보다 크고 10 이하여야 합니다.")
    path = Path(__file__).resolve().parent / MEMS_SCALE_FILENAME
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps({"g_per_lsb": value}, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)

def get_mems_lsb_per_g():
    return 1.0 / read_mems_g_per_lsb()


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
    try:
        path = Path(__file__).resolve().parent / MEMS_CORRECTION_FILENAME
        return validate_mems_correction(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError):
        return dict(MEMS_CORRECTION_DEFAULTS)

def save_mems_correction(values):
    values = validate_mems_correction(values)
    path = Path(__file__).resolve().parent / MEMS_CORRECTION_FILENAME
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(values, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)
    return values

def mems_sensitivity(measured_hz, config):
    f = np.asarray(measured_hz, dtype=np.float64)
    result = np.where(f <= config["low_hz"], config["low_sensitivity"],
                      np.where(f <= config["high_hz"],
                               config["mid_intercept"] - f * config["mid_slope"],
                               config["high_sensitivity"]))
    if np.any(result <= 0):
        raise ValueError("MEMS 보정 감도가 0 이하입니다.")
    return result


def format_mems_correction_formula(config):
    """Human-readable displayed formulas using the currently selected parameters."""
    fmt=lambda v: f"{float(v):.9g}"
    lo, hi = fmt(config['low_hz']), fmt(config['high_hz'])
    div=fmt(config['frequency_divisor'])
    sl=fmt(config['low_sensitivity'])
    sm=fmt(config['mid_intercept'])
    slope=fmt(config['mid_slope'])
    sh=fmt(config['high_sensitivity'])
    return (
        f"주파수: f_real = f_measure / {div} "
        f"({'ON' if config['frequency_enabled'] else 'OFF'})\n"
        f"Sensitivity S(f_measure): f ≤ {lo} Hz → {sl}; "
        f"{lo} < f ≤ {hi} Hz → {sm} − f × {slope}; "
        f"f > {hi} Hz → {sh}\n"
        f"가속도: G_real(f) = G_measure(f) / S(f_measure) "
        f"({'ON' if config['sensitivity_enabled'] else 'OFF'}; FFT g Peak/g RMS만 해당)"
    )

def corrected_mems_fft(measured_hz, g_amplitude, config, *, apply_sensitivity=True):
    frequency = np.asarray(measured_hz, dtype=np.float64)
    amplitude = np.asarray(g_amplitude, dtype=np.float64)
    if config["sensitivity_enabled"] and apply_sensitivity:
        amplitude = amplitude / mems_sensitivity(frequency, config)
    if config["frequency_enabled"]:
        frequency = frequency / config["frequency_divisor"]
    return frequency, amplitude

ADXL_VALID_G_RANGES = (2, 4, 8, 16)


def normalize_adxl_g_range(value):
    """
    g_range payload를 2/4/8/16 정수로 정규화한다.
    알 수 없거나 잘못된 값이면 None.
    """
    try:
        if value in (None, ""):
            return None

        g_range = int(float(str(value).strip()))

        if g_range in ADXL_VALID_G_RANGES:
            return g_range

    except Exception:
        pass

    return None


ADXL_CODE_TO_RATE = {
    int(code, 16): float(rate)
    for rate, code in ADXL_RATE_TO_CODE.items()
}

UI_REFRESH_MS = 250
GRAPH_REFRESH_MS = 250
MAX_GRAPH_POINTS = 1200


def get_app_dir():
    """
    PyInstaller onefile 배포 시에는 EXE가 있는 폴더를 사용한다.
    일반 Python 실행 시에는 .py 파일 폴더를 사용한다.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# V013: PIEZO 장치별 환산 전압 설정 파일 (실행 파일/소스와 같은 위치).
PIEZO_DEVICE_VOLTAGE_FILENAME = "piezo_device_voltages.json"


def load_piezo_device_voltages(path=None):
    """저장된 {device_id: volts}를 읽는다. 파일이 없으면 빈 설정."""
    path = Path(path) if path is not None else get_app_dir() / PIEZO_DEVICE_VOLTAGE_FILENAME
    if not path.exists():
        return {}
    try:
        with path.open('r', encoding='utf-8') as fh:
            obj = json.load(fh)
        if not isinstance(obj, dict):
            raise ValueError('장치별 전압 설정은 JSON 객체여야 합니다.')
        result = {}
        for device_id, voltage in obj.items():
            val = float(voltage)
            if isinstance(device_id, str) and device_id.strip() and math.isfinite(val) and 0 < val <= 100000:
                result[device_id] = val
        return result
    except (OSError, ValueError, TypeError) as exc:
        print(f"[V013] PIEZO 전압 설정 읽기 실패: {path} ({exc})", file=sys.stderr)
        return {}


def save_piezo_device_voltages(values, path=None):
    """설정 저장 시 임시 파일에 먼저 기록한 후 교체한다."""
    path = Path(path) if path is not None else get_app_dir() / PIEZO_DEVICE_VOLTAGE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    try:
        with temp.open('w', encoding='utf-8', newline='\n') as fh:
            json.dump(values, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write('\n')
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def safe_filename(value):
    text = str(value or "UNKNOWN").strip()
    text = re.sub(r'[^0-9A-Za-z._-]+', '_', text)
    return text or "UNKNOWN"


def parse_mqtt_topics(value):
    """
    MQTT 구독 Topic을 콤마/세미콜론/개행으로 여러 개 지정할 수 있게 한다.

    예:
      V/#,D/#
      V/#;D/#
    """
    raw = str(value or "")
    topics = []
    for part in re.split(r"[,;\\n\\r]+", raw):
        topic = part.strip()
        if topic and topic not in topics:
            topics.append(topic)
    return topics


class SensorRegistry:
    def __init__(self):
        self.lock = threading.Lock()
        self.devices = {}

    def reset(self):
        with self.lock:
            self.devices = {}

    def update(self, device_id, kind, source):
        now_text = datetime.now().strftime("%H:%M:%S")
        device_id = str(device_id or "UNKNOWN").strip()

        if kind.startswith("ADXL-"):
            sensor_type = "ADXL"
        else:
            sensor_type = kind

        with self.lock:
            rec = self.devices.setdefault(
                device_id,
                {
                    "device_id": device_id,
                    "type": sensor_type,
                    "source": source,
                    "count": 0,
                    "last": now_text,
                    "axes": set(),
                },
            )

            rec["count"] += 1
            rec["last"] = now_text
            rec["source"] = source

            if kind.startswith("ADXL-"):
                rec["type"] = "ADXL"
                rec["axes"].add(kind[-1])
            else:
                rec["type"] = kind

    def snapshot(self):
        with self.lock:
            result = []
            for device_id in sorted(self.devices):
                rec = self.devices[device_id]
                result.append(
                    {
                        "device_id": rec["device_id"],
                        "type": rec["type"],
                        "source": rec["source"],
                        "count": rec["count"],
                        "last": rec["last"],
                        "axes": "".join(sorted(rec["axes"])) if rec["axes"] else "-",
                    }
                )
            return result


@dataclass
class CSVItem:
    received_at: str
    source: str
    device_id: str
    gw_mac: str
    kind: str
    axis: str
    seq: str
    tick: str
    battery: str
    sample_rate: str
    num_samples: str
    payload_bytes: int
    sensor_data: str
    acq_status: str = ""
    ap_ssid: str = ""
    ap_rssi: str = ""


def parse_seq(seq_value):
    """
    seq의 상위 4bit로 센서/축을 구분한다.

    JSON 숫자로 들어온 경우:
      seq: 28
      -> 이미 정수값 28(0x1C)이므로 그대로 사용

    JSON 문자열로 들어온 경우:
      seq: "1C"
      -> HEX 문자열로 해석

    반환:
      (상위 nibble, 하위 nibble)
    """
    if isinstance(seq_value, bool):
        raise ValueError("seq bool 값은 허용하지 않습니다.")

    if isinstance(seq_value, int):
        seq_int = seq_value
    else:
        s = str(seq_value).strip()

        if not s:
            raise ValueError("seq 값이 비어 있습니다.")

        if s.lower().startswith("0x"):
            s = s[2:]

        seq_int = int(s, 16)

    if seq_int < 0:
        raise ValueError("seq는 음수일 수 없습니다.")

    return (seq_int >> 4) & 0x0F, seq_int & 0x0F


def classify_vibration(obj):
    # 초음파 H 제외:
    # 현재 진동 실제 포맷의 samplerate / numofsample 필드를 1차 기준으로 사용.
    if not isinstance(obj, dict):
        return None
    if "samplerate" not in obj or "numofsample" not in obj:
        return None

    try:
        sensor_type, frame = parse_seq(obj.get("seq"))
    except Exception:
        return None

    names = {
        0: "PIEZO",
        1: "ADXL-X",
        2: "ADXL-Y",
        3: "ADXL-Z",
    }
    name = names.get(sensor_type)
    if name is None:
        return None
    return name, frame


def classify_ultrasonic(obj, topic=""):
    """
    초음파(H) 판별.

    기존 수신기 규칙:
    - sensorData는 있으나 samplerate / numofsample이 없는 형식
    - 초음파 Topic 문자열(ultrasonic/ultra)이면 우선 H로 간주

    현재 실제 H payload는:
      12-byte header + 320 * float32 = 1292 bytes
    """
    if not isinstance(obj, dict):
        return False

    sensor_data = obj.get("sensorData")
    if not isinstance(sensor_data, str) or not sensor_data.strip():
        return False

    topic_lower = str(topic or "").lower()
    if "ultrasonic" in topic_lower or "/ultra" in topic_lower:
        return True

    # Legacy H frames omit both fields. A frame with only one of the two
    # vibration fields is incomplete, not automatically an H frame.
    has_rate = obj.get("samplerate") not in (None, "")
    has_count = obj.get("numofsample") not in (None, "")
    return not has_rate and not has_count


def unescape_bytes(data: bytes):
    """
    확인된 escape 규칙:
      7D 5E -> 7E
      7D 5D -> 7D

    7D 뒤가 5E/5D가 아니면 정상 데이터로 보고 7D 자체를 보존한다.
    """
    result = bytearray()
    i = 0

    while i < len(data):
        if data[i] == 0x7D and i + 1 < len(data):
            nxt = data[i + 1]
            if nxt == 0x5E:
                result.append(0x7E)
                i += 2
                continue
            if nxt == 0x5D:
                result.append(0x7D)
                i += 2
                continue

        result.append(data[i])
        i += 1

    return bytes(result)


def normalize_sample_rate(sensor_kind, value, default_rate):
    """
    수신 JSON의 samplerate를 실제 Hz로 정규화한다.

    PIEZO:
      512 / 1280 / ... / 25600 같은 실제 Hz 값을 사용.

    ADXL:
      실제 Hz(100~3200) 또는 장비 코드(0x0A~0x0F, 10~15)를 모두 허용.
    """
    try:
        if value in (None, ""):
            return float(default_rate)

        s = str(value).strip()
        if not s:
            return float(default_rate)

        if str(sensor_kind).upper() == "ADXL":
            # 0x0F 형태
            if s.lower().startswith("0x"):
                code = int(s, 16)
                if code in ADXL_CODE_TO_RATE:
                    return ADXL_CODE_TO_RATE[code]

            # 0F처럼 0x 없이 오는 경우도 허용
            if re.fullmatch(r"0[89A-Fa-f]", s):
                code = int(s, 16)
                if code in ADXL_CODE_TO_RATE:
                    return ADXL_CODE_TO_RATE[code]

            numeric = float(s)

            # 정수 코드 10~15로 들어오는 경우
            code = int(numeric)
            if numeric == code and code in ADXL_CODE_TO_RATE:
                return ADXL_CODE_TO_RATE[code]

            # 실제 Hz로 들어오는 경우
            return numeric

        return float(s)

    except Exception:
        return float(default_rate)


def _decode_sensor_words_without_byte_swap(sensor_hex, numofsample=None, label="SENSOR", restore_escape=True):
    """
    센서 측에서 endian 처리가 완료된 sensorData를 디코딩한다.

    기존 로직은 유지하되 PC에서 수행하던 2-byte Little Endian
    byte swap/reorder 단계만 제거한다.

    PIEZO는 기존대로 통신 escape(7D5E/7D5D)를 복원한다.
    V016 시험: ADXL은 escape 복원을 하지 않고 원본 bytes를 그대로 사용한다.
    4 HEX(16-bit) word 순서는 그대로 signed int16 big-endian으로 해석한다.
    """
    if not isinstance(sensor_hex, str):
        raise ValueError(f"{label} sensorData가 문자열이 아닙니다.")

    sensor_hex = sensor_hex.strip()

    if len(sensor_hex) % 2:
        raise ValueError(f"{label} sensorData HEX 길이가 홀수입니다.")

    raw = bytes.fromhex(sensor_hex)

    # V016: ADXL에 한해 escape 복원 임시 중단 (원상복구하려면 아래 주석 해제).
    if restore_escape:
        restored = unescape_bytes(raw)  # PIEZO의 기존 처리 유지
    else:
        # restored = unescape_bytes(raw)  # ADXL 시험용으로 주석 처리
        restored = raw

    if len(restored) % 2:
        raise ValueError(
            f"{label} {'escape 복원 후' if restore_escape else '원본'} byte 길이가 홀수입니다: {len(restored)}"
        )

    if numofsample not in (None, "", 0, "0"):
        expected_bytes = int(numofsample) * 2
        if len(restored) != expected_bytes:
            raise ValueError(
                f"{label} 길이 불일치: restored={len(restored)} "
                f"expected={expected_bytes}"
            )

    # 센서에서 endian 처리가 끝났으므로 PC에서는 byte swap/reorder하지 않는다.
    # 복원된 4 HEX word 순서를 그대로 signed int16로 해석한다.
    return np.frombuffer(restored, dtype=">i2").astype(np.float64)


def decode_vibration_samples(sensor_hex, numofsample=None):
    """V016 ADXL 시험: 7D5D/7D5E escape 복원 없이 수신된 HEX 그대로 디코딩."""
    return _decode_sensor_words_without_byte_swap(
        sensor_hex,
        numofsample,
        label="ADXL",
        restore_escape=False,
    )


def decode_piezo_samples(sensor_hex, numofsample=None):
    """PIEZO: 기존 로직 유지, PC Little Endian reorder만 제거."""
    return _decode_sensor_words_without_byte_swap(
        sensor_hex,
        numofsample,
        label="PIEZO",
    )


def decode_ultrasonic_samples(sensor_hex):
    """
    초음파 H sensorData 디코딩.

    포맷:
      header 12 bytes + 320 * IEEE754 float32 little-endian
      = 1292 bytes

    header:
      restored_hex[16:18] = minKhz
      restored_hex[18:20] = intervalHz
      restored_hex[24:]   = 320 float32 payload

    입력이 정확히 1292 bytes이면 이미 escape 복원이 끝난 것으로 보고
    그대로 사용한다. 길이가 다르면 7D5E/7D5D 복원을 시도한다.
    """
    if not isinstance(sensor_hex, str):
        raise ValueError("ULTRASONIC sensorData가 문자열이 아닙니다.")

    sensor_hex = sensor_hex.strip()
    if len(sensor_hex) % 2:
        raise ValueError("ULTRASONIC sensorData HEX 길이가 홀수입니다.")

    raw_input = bytes.fromhex(sensor_hex)
    expected_bytes = 12 + (320 * 4)  # 1292

    if len(raw_input) == expected_bytes:
        raw = raw_input
        escape_mode = "already_unescaped_by_length"
    else:
        raw = unescape_bytes(raw_input)
        escape_mode = "fallback_unescape"

    if len(raw) != expected_bytes:
        raise ValueError(
            "ULTRASONIC payload 길이 불일치: "
            f"input={len(raw_input)} bytes, "
            f"restored={len(raw)} bytes, "
            f"expected={expected_bytes} bytes"
        )

    restored_hex = raw.hex().upper()
    min_khz = int(restored_hex[16:18], 16)
    interval_hz = int(restored_hex[18:20], 16)

    payload = raw[12:]
    samples = np.frombuffer(
        payload,
        dtype="<f4",
        count=320,
    ).astype(np.float64, copy=True)

    if len(samples) != 320:
        raise ValueError(
            f"ULTRASONIC float 개수 불일치: {len(samples)} / expected 320"
        )

    freq_khz = (
        float(min_khz)
        + np.arange(len(samples), dtype=np.float64)
        * (float(interval_hz) / 1000.0)
    )

    meta = {
        "input_bytes": len(raw_input),
        "restored_bytes": len(raw),
        "escape_mode": escape_mode,
        "minKhz": int(min_khz),
        "intervalHz": int(interval_hz),
        "sample_count": len(samples),
        "freq_start_khz": float(freq_khz[0]) if len(freq_khz) else float(min_khz),
        "freq_end_khz": float(freq_khz[-1]) if len(freq_khz) else float(min_khz),
    }
    return samples, freq_khz, meta


FFT_WINDOW_NAMES = (
    "Rectangular",
    "Hann",
    "Hamming",
    "Blackman",
    "Blackman-Harris",
    "Flat Top",
    "Bartlett",
    "Kaiser",
)

# 현재 센서에서 지원하는 Sample Count 최소값.
MIN_FFT_WINDOW_SIZE = int(SAMPLE_COUNTS[0])


def allowed_fft_window_sizes(sample_count):
    """
    한 번의 연속 측정 Sample Count 안에서 허용되는 FFT Window Size.

    기본 원칙:
      MIN_FFT_WINDOW_SIZE <= Window Size <= Sample Count

    센서는 측정 -> 전송 -> Sleep 방식이므로 서로 다른 패킷을
    이어 붙여 Window Size를 키우지 않는다.
    """
    try:
        sample_count = int(sample_count)
    except Exception:
        return []

    if sample_count <= 0:
        return []

    values = []

    for value in SAMPLE_COUNTS:
        n = int(value)
        if MIN_FFT_WINDOW_SIZE <= n <= sample_count:
            values.append(n)

    # 향후 표준 목록에 없는 Sample Count가 오더라도
    # 실제 Sample Count 자체는 최대값으로 허용.
    if (
        sample_count >= MIN_FFT_WINDOW_SIZE
        and sample_count not in values
    ):
        values.append(sample_count)

    # 예상 밖으로 최소값보다 작은 패킷이 들어오는 경우에는
    # 실제 크기를 단독 허용해 그래프가 완전히 막히지 않게 한다.
    if not values:
        values.append(sample_count)

    return sorted(set(values))


def select_fft_segment(samples, window_size=None):
    """
    한 패킷 내부의 연속 Raw 데이터에서 FFT용 구간을 선택한다.

    Window Size < Sample Count이면 중앙의 연속 구간을 사용한다.
    패킷 간 Raw 데이터는 절대 누적/연결하지 않는다.

    반환:
      (fft_samples, start_index)
    """
    data = np.asarray(samples, dtype=np.float64)
    total = len(data)

    if total <= 0:
        return data, 0

    try:
        requested = int(window_size)
    except Exception:
        requested = total

    requested = max(1, min(requested, total))

    start = (total - requested) // 2
    end = start + requested

    return (
        np.array(
            data[start:end],
            dtype=np.float64,
            copy=True,
        ),
        int(start),
    )


def normalize_fft_window_name(window_name):
    name = str(window_name or "Blackman").strip().lower()

    aliases = {
        "none": "Rectangular",
        "rect": "Rectangular",
        "rectangle": "Rectangular",
        "rectangular": "Rectangular",
        "boxcar": "Rectangular",

        "hann": "Hann",
        "hanning": "Hann",

        "hamming": "Hamming",

        "blackman": "Blackman",

        "blackman-harris": "Blackman-Harris",
        "blackman harris": "Blackman-Harris",
        "blackmanharris": "Blackman-Harris",
        "bh": "Blackman-Harris",

        "flat top": "Flat Top",
        "flattop": "Flat Top",
        "flat-top": "Flat Top",

        "bartlett": "Bartlett",
        "triangle": "Bartlett",
        "triangular": "Bartlett",

        "kaiser": "Kaiser",
    }

    return aliases.get(name, "Hann")


def _blackman_harris_window(n):
    """
    4-term Blackman-Harris window.
    계수는 널리 사용되는 92 dB Blackman-Harris 계열 값.
    """
    n = int(n)

    if n <= 0:
        return np.array([], dtype=np.float64)
    if n == 1:
        return np.ones(1, dtype=np.float64)

    idx = np.arange(n, dtype=np.float64)
    phase = (2.0 * np.pi * idx) / float(n - 1)

    a0 = 0.35875
    a1 = 0.48829
    a2 = 0.14128
    a3 = 0.01168

    return (
        a0
        - a1 * np.cos(phase)
        + a2 * np.cos(2.0 * phase)
        - a3 * np.cos(3.0 * phase)
    )


def _flat_top_window(n):
    """
    5-term Flat Top window.

    진폭 정확도를 우선하는 FFT 측정에 유용하다.
    대신 메인 로브가 넓어져 가까운 주파수 분리 성능은 떨어질 수 있다.
    """
    n = int(n)

    if n <= 0:
        return np.array([], dtype=np.float64)
    if n == 1:
        return np.ones(1, dtype=np.float64)

    idx = np.arange(n, dtype=np.float64)
    phase = (2.0 * np.pi * idx) / float(n - 1)

    a0 = 0.21557895
    a1 = 0.41663158
    a2 = 0.277263158
    a3 = 0.083578947
    a4 = 0.006947368

    return (
        a0
        - a1 * np.cos(phase)
        + a2 * np.cos(2.0 * phase)
        - a3 * np.cos(3.0 * phase)
        + a4 * np.cos(4.0 * phase)
    )


def build_fft_window(
    n,
    window_name="Blackman",
    kaiser_beta=8.6,
):
    """
    FFT에 사용할 Window 생성.

    Rectangular:
        Window 미적용과 동일.
    Hann:
        기존 기본값. 범용 진동/연속 신호 분석에 사용.
    Hamming:
        Hann과 유사한 고정형 Window.
    Blackman:
        누설 억제를 더 강화.
    Blackman-Harris:
        매우 낮은 Side Lobe가 필요한 경우.
    Flat Top:
        FFT 진폭 측정 정확도를 우선할 때 유용.
    Bartlett:
        삼각형 Window.
    Kaiser:
        beta 값으로 Main Lobe / Side Lobe 특성을 조절.

    모든 Window의 진폭은 calculate_fft()에서
    coherent gain(sum(window)) 기준으로 보정한다.
    """
    n = int(n)
    normalized = normalize_fft_window_name(window_name)

    if n <= 0:
        return np.array([], dtype=np.float64)

    if normalized == "Rectangular":
        return np.ones(n, dtype=np.float64)

    if normalized == "Hamming":
        return np.hamming(n)

    if normalized == "Blackman":
        return np.blackman(n)

    if normalized == "Blackman-Harris":
        return _blackman_harris_window(n)

    if normalized == "Flat Top":
        return _flat_top_window(n)

    if normalized == "Bartlett":
        return np.bartlett(n)

    if normalized == "Kaiser":
        try:
            beta = float(kaiser_beta)
        except Exception:
            beta = 8.6

        if not np.isfinite(beta):
            beta = 8.6

        beta = max(0.0, beta)
        return np.kaiser(n, beta)

    # default: Hann
    return np.hanning(n)


def butterworth_highpass_zero_phase(samples, sample_rate, cutoff_hz, order):
    """FFT-domain zero-phase Butterworth-amplitude HPF for one independent packet.

    No cross-packet state or SciPy dependency. Each packet is treated as periodic,
    so a discontinuity at the boundaries may create edge-related spectral leakage.
    The cutoff is the -3 dB amplitude point, not a hard frequency mask.
    """
    data = np.asarray(samples, dtype=np.float64)
    if len(data) < 2:
        return data.copy()
    fs = float(sample_rate)
    fc = float(cutoff_hz)
    degree = int(order)
    if not (np.isfinite(fs) and fs > 0 and np.isfinite(fc)
            and 0 < fc < fs / 2 and 1 <= degree <= 12):
        raise ValueError("HPF: 0 < cutoff < Fs/2, 차수 1~12 범위여야 합니다.")
    # Center first; suppress DC explicitly so it does not reappear in the IFFT.
    centered = data - np.mean(data)
    bins = np.fft.rfftfreq(len(centered), d=1.0 / fs)
    response = np.zeros(len(bins), dtype=np.float64)
    positive = bins > 0
    response[positive] = 1.0 / np.sqrt(
        1.0 + np.power(fc / bins[positive], 2 * degree)
    )
    return np.fft.irfft(np.fft.rfft(centered) * response, n=len(centered))



def apply_spectral_filters(samples, sample_rate, *, hpf=False, hp_cutoff=10.0,
                           hp_order=4, lpf=False, lp_cutoff=3000.0, lp_order=4,
                           notch=False, notch_center=975.0, notch_width=20.0):
    """Packet-local zero-phase frequency-domain filters, no extra dependencies.

    Butterworth HPF/LPF magnitude responses (-3 dB cutoff); Gaussian
    notch magnitude response with full-width-at-half-maximum of rejection
    profile given by notch_width. Filters multiply when simultaneously on.
    Packet boundaries are periodic for this FFT method; edge artifacts
    are possible. Never modifies input array or saved raw payloads.
    """
    data = np.asarray(samples, dtype=np.float64)
    if len(data) < 2 or not (hpf or lpf or notch):
        return data.copy()
    fs = float(sample_rate)
    if not np.isfinite(fs) or fs <= 0:
        raise ValueError("필터: Fs는 0보다 커야 합니다.")
    nyquist = fs / 2.0
    bins = np.fft.rfftfreq(len(data), d=1.0 / fs)
    response = np.ones_like(bins)
    if hpf:
        fc, degree = float(hp_cutoff), int(hp_order)
        if not (0 < fc < nyquist and 1 <= degree <= 12):
            raise ValueError("HPF: 0 < 차단 주파수 < Fs/2, 차수 1~12")
        passband = np.zeros_like(bins)
        mask = bins > 0
        passband[mask] = 1.0 / np.sqrt(1.0 + (fc / bins[mask]) ** (2 * degree))
        response *= passband
    if lpf:
        fc, degree = float(lp_cutoff), int(lp_order)
        if not (0 < fc < nyquist and 1 <= degree <= 12):
            raise ValueError("LPF: 0 < 차단 주파수 < Fs/2, 차수 1~12")
        response *= 1.0 / np.sqrt(1.0 + (bins / fc) ** (2 * degree))
    if notch:
        center, width = float(notch_center), float(notch_width)
        if not (0 < center < nyquist and 0 < width < nyquist):
            raise ValueError("Notch: 0 < 중심 주파수 및 대역폭 < Fs/2")
        # Gaussian rejection: at center response=0, at +/- width/2 ~=0.5.
        sigma = width / (2.0 * np.sqrt(2.0 * np.log(2.0)))
        response *= 1.0 - np.exp(-0.5 * ((bins - center) / sigma) ** 2)
    return np.fft.irfft(np.fft.rfft(data) * response, n=len(data))


def scipy_filter_signal(samples, sample_rate, *, hpf=False, hp_cutoff=10.0,
                        hp_order=4, lpf=False, lp_cutoff=3000.0, lp_order=4,
                        notch=False, notch_center=975.0, notch_width=20.0):
    """SciPy comparison only. Forward/backward filters are zero-phase,
    but their magnitude response differs from the original FFT filter.
    Never changes saved raw/count or MQTT payloads.
    """
    from scipy import signal
    data = np.asarray(samples, dtype=np.float64).copy()
    if data.size < 2 or not (hpf or lpf or notch):
        return data
    fs = float(sample_rate)
    if not (np.isfinite(fs) and fs > 0):
        raise ValueError("SciPy Fs must be positive")
    ny = fs / 2.0
    for enabled, cutoff, order, btype in (
        (hpf, hp_cutoff, hp_order, 'highpass'),
        (lpf, lp_cutoff, lp_order, 'lowpass'),
    ):
        if not enabled:
            continue
        fc, degree = float(cutoff), int(order)
        if not (0 < fc < ny and 1 <= degree <= 12):
            raise ValueError(f"SciPy {btype}: invalid cutoff/order")
        sos = signal.butter(degree, fc, btype=btype, fs=fs, output='sos')
        data = signal.sosfiltfilt(sos, data)
    if notch:
        center, width = float(notch_center), float(notch_width)
        if not (0 < center < ny and 0 < width < ny):
            raise ValueError("SciPy notch: invalid center/width")
        # iirnotch bandwidth is defined by f0 / Q, unlike Gaussian notch FWHM.
        b, a = signal.iirnotch(center, center / width, fs=fs)
        data = signal.filtfilt(b, a, data)
    return data


def scipy_fft_comparison(samples, sample_rate, window_name="Blackman", kaiser_beta=8.6,
                         hpf_enabled=False, hpf_cutoff_hz=10.0, hpf_order=4,
                         lpf_enabled=False, lpf_cutoff_hz=3000.0, lpf_order=4,
                         notch_enabled=False, notch_center_hz=975.0, notch_width_hz=20.0):
    """SciPy-only viewer spectrum: SciPy detrend, filters, windows and fft.
    Uses same one-sided peak amplitude normalization as the original viewer.
    """
    from scipy import signal, fft
    a = np.asarray(samples, dtype=np.float64)
    n = len(a)
    if n < 2:
        return np.array([]), np.array([])
    centered = signal.detrend(a, type='constant')
    if hpf_enabled or lpf_enabled or notch_enabled:
        centered = scipy_filter_signal(
            centered, sample_rate,
            hpf=hpf_enabled, hp_cutoff=hpf_cutoff_hz, hp_order=hpf_order,
            lpf=lpf_enabled, lp_cutoff=lpf_cutoff_hz, lp_order=lpf_order,
            notch=notch_enabled, notch_center=notch_center_hz,
            notch_width=notch_width_hz,
        )
    names = {
        'Rectangular': 'boxcar', 'Hann': 'hann', 'Hamming': 'hamming',
        'Blackman': 'blackman', 'Blackman-Harris': 'blackmanharris',
        'Flat Top': 'flattop', 'Bartlett': 'bartlett',
    }
    name = normalize_fft_window_name(window_name)
    if name == 'Kaiser':
        w = signal.windows.kaiser(n, beta=max(0, float(kaiser_beta)), sym=True)
    else:
        w = signal.windows.get_window(names.get(name, 'hann'), n, fftbins=False)
    spec = fft.rfft(centered * w)
    gain = float(np.sum(w))
    magnitude = 2.0 * np.abs(spec) / gain if gain > 0 else np.zeros_like(spec.real)
    magnitude[0] *= 0.5
    if n % 2 == 0:
        magnitude[-1] *= 0.5
    return fft.rfftfreq(n, d=1.0 / float(sample_rate)), magnitude


def calculate_fft(
    samples,
    sample_rate,
    window_name="Blackman",
    kaiser_beta=8.6,
    hpf_enabled=False,
    hpf_cutoff_hz=10.0,
    hpf_order=4,
    lpf_enabled=False, lpf_cutoff_hz=3000.0, lpf_order=4,
    notch_enabled=False, notch_center_hz=975.0, notch_width_hz=20.0,
):
    samples = np.asarray(samples, dtype=np.float64)
    n = len(samples)

    if n < 2:
        return np.array([]), np.array([])

    # DC 제거
    centered = samples - np.mean(samples)
    if hpf_enabled or lpf_enabled or notch_enabled:
        centered = apply_spectral_filters(
            centered, sample_rate, hpf=hpf_enabled,
            hp_cutoff=hpf_cutoff_hz, hp_order=hpf_order,
            lpf=lpf_enabled, lp_cutoff=lpf_cutoff_hz, lp_order=lpf_order,
            notch=notch_enabled, notch_center=notch_center_hz,
            notch_width=notch_width_hz,
        )

    window_name = normalize_fft_window_name(window_name)
    window = build_fft_window(
        n,
        window_name,
        kaiser_beta=kaiser_beta,
    )
    windowed = centered * window

    # 실수 입력이므로 single-sided FFT
    spectrum = np.fft.rfft(windowed)
    magnitude = np.abs(spectrum)

    # Window의 coherent gain 보정.
    # 정현파 Peak amplitude를 복원하기 위한 보정이다.
    gain = np.sum(window)

    if gain > 0:
        magnitude = (2.0 / gain) * magnitude

    # DC / Nyquist bin은 single-sided FFT에서 2배 대상이 아니다.
    if len(magnitude):
        magnitude[0] *= 0.5

    if n % 2 == 0 and len(magnitude) > 1:
        magnitude[-1] *= 0.5

    freqs = np.fft.rfftfreq(
        n,
        d=1.0 / float(sample_rate),
    )

    return freqs, magnitude


# ---------------------------------------------------------------------------
# 회전 부품 개수 탐색: FFT 피크와 RPM의 정수 차수(order) 일치도 기반.
# 결과는 부품 개수 후보일 뿐, 물리적인 부품 종류/개수 확정이 아니다.
# ---------------------------------------------------------------------------


# Shared per-device velocity SRSS reference limits (not alarm decisions).
def srss_limit_path():
    return Path.home() / '.sensor-program' / 'srss_limits.json'

def srss_limit_key(device):
    return str(device or '').strip().upper().replace(':','').replace('-','')

def srss_limit_read():
    path = srss_limit_path()
    if not path.exists():
        return {}
    with path.open('r', encoding='utf-8') as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError('SRSS 설정 JSON은 객체여야 합니다')
    return data

def srss_limit_get(device):
    try:
        value = float(srss_limit_read().get(srss_limit_key(device), 4.5))
        return value if np.isfinite(value) and value > 0 else 4.5
    except (OSError, ValueError, TypeError):
        return 4.5

def srss_limit_save(device, value):
    import os, tempfile
    key = srss_limit_key(device)
    value = float(value)
    if not key or not np.isfinite(value) or value <= 0:
        raise ValueError('장치 ID와 0보다 큰 유한한 한계값이 필요합니다')
    path = srss_limit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = srss_limit_read()
    data[key] = value
    fd, name = tempfile.mkstemp(prefix='srss_', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

def srss_axis_power(peak, n, window):
    """One-sided coherent-gain peak spectrum -> window-power-corrected AC mean square.
    DC excluded; Nyquist is not a paired bin. Units follow the input peak amplitudes.
    """
    a=np.asarray(peak,dtype=float)
    w=np.asarray(window,dtype=float)
    if n<2 or len(w)!=n or len(a)!=n//2+1 or not np.all(np.isfinite(a)):
        raise ValueError('SRSS: FFT N/Window mismatch')
    den=n*float(np.sum(w*w))
    if den<=0: raise ValueError('SRSS: invalid window')
    weights=np.full(len(a),0.5); weights[0]=0.0
    if n%2==0: weights[-1]=1.0
    return float(np.sum(weights*a*a)*float(np.sum(w))**2/den)

def estimate_rotating_counts(freqs, magnitude, rpm, min_count=2, max_count=48,
                             max_harmonics=5, top_k=5):
    freqs = np.asarray(freqs, dtype=np.float64)
    magnitude = np.asarray(magnitude, dtype=np.float64)
    if (rpm <= 0 or len(freqs) < 5 or len(freqs) != len(magnitude)
            or not np.all(np.isfinite(freqs)) or not np.all(np.isfinite(magnitude))):
        return []
    rotation_hz = float(rpm) / 60.0
    if rotation_hz <= 0:
        return []
    df = float(np.median(np.diff(freqs)))
    if df <= 0:
        return []
    nyquist = float(freqs[-1])
    power = np.maximum(magnitude, 0.0)
    # DC, 극저주파는 후보 계산에서 제외한다.
    power = power.copy()
    power[freqs < max(2 * df, 1.0)] = 0
    if not np.any(power > 0):
        return []
    # 국부 최댓값만 사용하여 넓은 스펙트럼/노이즈의 영향을 줄인다.
    indices = np.where((power[1:-1] > power[:-2]) &
                       (power[1:-1] >= power[2:]))[0] + 1
    if len(indices) == 0:
        return []
    noise_floor = float(np.median(power[power > 0]))
    threshold = max(noise_floor * 3.0, float(np.max(power)) * 0.015)
    indices = indices[power[indices] >= threshold]
    if not len(indices):
        return []
    # 너무 많은 피크를 쫓지 않는다.
    indices = indices[np.argsort(power[indices])[-120:]]
    peak_f = freqs[indices]
    peak_a = power[indices]
    results = []
    for count in range(int(min_count), int(max_count) + 1):
        fundamental = rotation_hz * count
        if fundamental > nyquist - df:
            continue
        n_harm = min(max_harmonics, int(nyquist // fundamental))
        if n_harm < 1:
            continue
        matched = []
        score = 0.0
        for harmonic in range(1, n_harm + 1):
            target = fundamental * harmonic
            # RPM 편차와 FFT 빈 폭 고려. 지나친 넓이는 다른 차수 오인 위험.
            tolerance = max(1.5 * df, target * 0.015)
            mask = np.abs(peak_f - target) <= tolerance
            if not np.any(mask):
                continue
            possible = np.where(mask)[0]
            k = possible[np.argmax(peak_a[possible])]
            peak_ratio = float(peak_a[k] / max(float(np.max(power)), 1e-12))
            proximity = max(0.0, 1.0 - abs(float(peak_f[k]) - target) / tolerance)
            contribution = (peak_ratio ** 0.5) * (0.5 + 0.5 * proximity) / (harmonic ** 0.6)
            score += contribution
            matched.append({'harmonic': harmonic, 'expected_hz': round(target, 3),
                            'observed_hz': round(float(peak_f[k]), 3),
                            'amplitude': round(float(peak_a[k]), 6)})
        if not matched:
            continue
        # 동일 피크가 큰 개수와 작은 개수의 고조파에 동시에 들어갈 수 있으므로
        # 기본파 일치 및 2개 이상의 독립적인 피크에 보너스를 준다.
        base_matched = any(v['harmonic'] == 1 for v in matched)
        score *= (1.0 if base_matched else 0.35)
        score *= (1.0 + 0.18 * min(len(matched) - 1, 3))
        results.append({'count': count, 'base_hz': round(fundamental, 3),
                        'score': round(score, 5), 'matched': matched})
    results.sort(key=lambda r: (-r['score'], -len(r['matched']), r['count']))
    return results[:top_k]


class ADXLLatestStore:
    """
    장치/축별 최신 ADXL 원데이터 + FFT만 저장한다.
    오래된 Graph 데이터는 누적하지 않는다.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self.devices = set()
        self.version = 0
        # PIEZO: 장치/축별 패킷 Peak-to-Peak 누적 통계. 원본 Count 기반.
        self.piezo_pp_totals = {}

    def update(
        self,
        device_id,
        axis,
        samples,
        sample_rate,
        recv_ns,
        seq="",
        numofsample=None,
        packet_sample_rate="",
        packet_numofsample="",
        fft_window="Blackman",
        fft_window_beta=8.6,
        fft_window_size=None,
        g_range="",
        gain="",
        accumulate_piezo_pp=False,
    ):
        # 축마다 독립 배열을 보관해 참조 공유 가능성도 제거한다.
        samples = np.array(samples, dtype=np.float64, copy=True)

        # G RMS 보정 및 진단용 Raw 통계.
        if len(samples):
            raw_mean = float(np.mean(samples))
            raw_min = float(np.min(samples))
            raw_max = float(np.max(samples))
            centered_raw = samples - raw_mean
            raw_rms = float(
                np.sqrt(np.mean(centered_raw ** 2))
            )
        else:
            raw_mean = 0.0
            raw_min = 0.0
            raw_max = 0.0
            raw_rms = 0.0

        fft_window = normalize_fft_window_name(fft_window)

        try:
            fft_window_beta = float(fft_window_beta)
        except Exception:
            fft_window_beta = 8.6

        fft_samples, fft_segment_start = select_fft_segment(
            samples,
            fft_window_size,
        )

        freqs, magnitude = calculate_fft(
            fft_samples,
            sample_rate,
            fft_window,
            kaiser_beta=fft_window_beta,
        )

        # 실제 축 데이터가 동일한지 확인하기 위한 fingerprint.
        # decoded int16 기준 CRC32를 사용한다.
        sample_i16 = np.asarray(samples, dtype=np.int16)
        crc32 = zlib.crc32(sample_i16.tobytes()) & 0xFFFFFFFF

        peak_bin = -1
        peak_hz = 0.0
        peak_magnitude = 0.0

        # DC(0 Hz)는 제외하고 가장 큰 FFT Peak를 진단값으로 사용.
        if len(magnitude) > 1 and len(freqs) == len(magnitude):
            local_index = int(np.argmax(magnitude[1:])) + 1
            peak_bin = local_index
            peak_hz = float(freqs[local_index])
            peak_magnitude = float(magnitude[local_index])

        record = {
            "samples": samples,
            "sample_rate": float(sample_rate),
            "numofsample": int(numofsample) if numofsample not in (None, "", 0, "0") else len(samples),
            "packet_sample_rate": str(packet_sample_rate),
            "packet_numofsample": str(packet_numofsample),
            "g_range": str(g_range),
            "gain": str(gain),
            "fft_n": int(len(fft_samples)),
            "fft_window_size": int(len(fft_samples)),
            "fft_segment_start": int(fft_segment_start),
            "fft_window": str(fft_window),
            "fft_window_beta": float(fft_window_beta),
            "freqs": freqs,
            "magnitude": magnitude,
            "peak_bin": int(peak_bin),
            "peak_hz": float(peak_hz),
            "peak_magnitude": float(peak_magnitude),
            "raw_mean": float(raw_mean),
            "raw_min": float(raw_min),
            "raw_max": float(raw_max),
            "raw_rms": float(raw_rms),
            "recv_ns": int(recv_ns),
            "seq": str(seq),
            "crc32": f"{crc32:08X}",
        }

        with self.lock:
            if accumulate_piezo_pp and len(samples):
                key = (str(device_id), str(axis))
                totals = self.piezo_pp_totals.setdefault(
                    key, {"packet_count": 0, "sum_max_count": 0.0, "sum_min_count": 0.0}
                )
                totals["packet_count"] += 1
                totals["sum_max_count"] += raw_max
                totals["sum_min_count"] += raw_min
                # P-P 통계의 기준 전압도 공통 기본 환산계수를 따른다.
                # 화면에서는 사용자가 지정한 Vmax를 적용하여 재계산한다.
                mv_per_count = PIEZO_ADC_VMAX * 1000.0 / PIEZO_ADC_COUNTS
                record["piezo_pp_sum_max_count"] = totals["sum_max_count"]
                record["piezo_pp_sum_min_count"] = totals["sum_min_count"]
                record["piezo_pp_packet_mv"] = (raw_max - raw_min) * mv_per_count
                record["piezo_pp_sum_max_mv"] = totals["sum_max_count"] * mv_per_count
                record["piezo_pp_sum_min_mv"] = totals["sum_min_count"] * mv_per_count
                record["piezo_pp_total_mv"] = (
                    totals["sum_max_count"] - totals["sum_min_count"]
                ) * mv_per_count
                record["piezo_pp_packet_count"] = totals["packet_count"]
            self.devices.add(str(device_id))
            dev = self.data.setdefault(str(device_id), {})
            dev[str(axis)] = record
            self.version += 1

        return record

    def device_list(self):
        with self.lock:
            return sorted(self.devices)

    def snapshot(self, device_id):
        with self.lock:
            axes = self.data.get(str(device_id), {})
            return self.version, dict(axes)


class UltrasonicLatestStore:
    """
    장치별 최신 초음파 H 320개 주파수 구간 값만 보관한다.
    오래된 패킷은 누적하지 않는다.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self.devices = set()
        self.version = 0

    def update(
        self,
        device_id,
        samples,
        freq_khz,
        min_khz,
        interval_hz,
        recv_ns,
        seq="",
    ):
        samples = np.array(samples, dtype=np.float64, copy=True)
        freq_khz = np.array(freq_khz, dtype=np.float64, copy=True)

        if len(samples):
            value_min = float(np.min(samples))
            value_max = float(np.max(samples))
            value_mean = float(np.mean(samples))
            peak_index = int(np.argmax(samples))
            peak_value = float(samples[peak_index])
            peak_khz = (
                float(freq_khz[peak_index])
                if len(freq_khz) == len(samples)
                else 0.0
            )
        else:
            value_min = value_max = value_mean = 0.0
            peak_index = -1
            peak_value = peak_khz = 0.0

        record = {
            "samples": samples,
            "freq_khz": freq_khz,
            "minKhz": int(min_khz),
            "intervalHz": int(interval_hz),
            "recv_ns": int(recv_ns),
            "seq": str(seq),
            "sample_count": int(len(samples)),
            "value_min": value_min,
            "value_max": value_max,
            "value_mean": value_mean,
            "peak_index": peak_index,
            "peak_value": peak_value,
            "peak_khz": peak_khz,
        }

        with self.lock:
            device_id = str(device_id)
            self.devices.add(device_id)
            self.data[device_id] = record
            self.version += 1

        return record

    def device_list(self):
        with self.lock:
            return sorted(self.devices)

    def snapshot(self, device_id):
        with self.lock:
            record = self.data.get(str(device_id))
            return self.version, (dict(record) if record is not None else None)


@dataclass
class UltrasonicGraphItem:
    device_id: str
    sensor_data: str
    recv_ns: int
    seq: str = ""


@dataclass
class GraphItem:
    device_id: str
    sensor_kind: str
    axis: str
    sensor_data: str
    numofsample: int
    sample_rate: float
    recv_ns: int
    seq: str

    # FFT 진단용 원본 JSON 값.
    # packet_sample_rate가 없으면 빈 문자열.
    packet_sample_rate: str = ""
    packet_numofsample: str = ""

    # 새 센서 metadata
    g_range: str = ""
    gain: str = ""


@dataclass
class Bucket:
    messages: int = 0
    payload_bytes: int = 0
    sensor_hex_chars: int = 0
    last_ns: int | None = None
    interval_sum_ms: float = 0.0
    interval_count: int = 0
    interval_min_ms: float | None = None
    interval_max_ms: float = 0.0

    def add(self, payload_len, sensor_hex_chars, recv_ns):
        self.messages += 1
        self.payload_bytes += payload_len
        self.sensor_hex_chars += sensor_hex_chars

        if self.last_ns is not None:
            dt = (recv_ns - self.last_ns) / 1_000_000.0
            self.interval_sum_ms += dt
            self.interval_count += 1
            if self.interval_min_ms is None or dt < self.interval_min_ms:
                self.interval_min_ms = dt
            self.interval_max_ms = max(self.interval_max_ms, dt)

        self.last_ns = recv_ns

    def avg_interval(self):
        if self.interval_count == 0:
            return 0.0
        return self.interval_sum_ms / self.interval_count


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.Lock()):
            self.started_ns = None
            self.stopped_elapsed_sec = 0.0
            self.rate_mark_ns = None
            self.total_messages = 0
            self.total_bytes = 0
            self.ignored = 0
            self.json_errors = 0
            self.mark_messages = 0
            self.mark_bytes = 0
            self.current_msg_rate = 0.0
            self.current_byte_rate = 0.0
            self.buckets = {
                "PIEZO": Bucket(),
                "ADXL-X": Bucket(),
                "ADXL-Y": Bucket(),
                "ADXL-Z": Bucket(),
                "ULTRASONIC": Bucket(),
            }

    def start(self):
        with self.lock:
            now = time.monotonic_ns()
            self.started_ns = now
            self.stopped_elapsed_sec = 0.0
            self.rate_mark_ns = now
            self.mark_messages = self.total_messages
            self.mark_bytes = self.total_bytes
            self.current_msg_rate = 0.0
            self.current_byte_rate = 0.0

    def stop(self):
        with self.lock:
            if self.started_ns is not None:
                now = time.monotonic_ns()
                self.stopped_elapsed_sec = (
                    now - self.started_ns
                ) / 1_000_000_000.0
                self.started_ns = None
                self.rate_mark_ns = None
                self.current_msg_rate = 0.0
                self.current_byte_rate = 0.0

    def add(self, kind, payload_len, sensor_hex_chars, recv_ns):
        with self.lock:
            self.total_messages += 1
            self.total_bytes += payload_len
            self.buckets[kind].add(payload_len, sensor_hex_chars, recv_ns)

    def add_ignored(self):
        with self.lock:
            self.ignored += 1

    def add_json_error(self):
        with self.lock:
            self.json_errors += 1

    def snapshot(self):
        now = time.monotonic_ns()
        with self.lock:
            if self.started_ns is not None:
                if self.rate_mark_ns is None:
                    self.rate_mark_ns = now

                rate_sec = (
                    now - self.rate_mark_ns
                ) / 1_000_000_000.0

                if rate_sec >= 0.2:
                    self.current_msg_rate = (
                        self.total_messages - self.mark_messages
                    ) / rate_sec
                    self.current_byte_rate = (
                        self.total_bytes - self.mark_bytes
                    ) / rate_sec
                    self.rate_mark_ns = now
                    self.mark_messages = self.total_messages
                    self.mark_bytes = self.total_bytes

                elapsed = (
                    now - self.started_ns
                ) / 1_000_000_000.0
            else:
                elapsed = self.stopped_elapsed_sec

            safe_elapsed = max(elapsed, 1e-9)

            bucket_copy = {}
            for key, b in self.buckets.items():
                bucket_copy[key] = {
                    "messages": b.messages,
                    "payload_bytes": b.payload_bytes,
                    "sensor_hex_chars": b.sensor_hex_chars,
                    "avg_interval": b.avg_interval(),
                    "min_interval": b.interval_min_ms or 0.0,
                    "max_interval": b.interval_max_ms,
                }

            return {
                "elapsed": elapsed,
                "messages": self.total_messages,
                "bytes": self.total_bytes,
                "ignored": self.ignored,
                "json_errors": self.json_errors,
                "current_msg_rate": self.current_msg_rate,
                "current_byte_rate": self.current_byte_rate,
                "avg_msg_rate": (
                    self.total_messages / safe_elapsed
                    if elapsed > 0 else 0.0
                ),
                "avg_byte_rate": (
                    self.total_bytes / safe_elapsed
                    if elapsed > 0 else 0.0
                ),
                "buckets": bucket_copy,
            }



def _pyqtgraph_ultrasonic_viewer_process(data_queue, command_queue):
    """
    초음파 H 전용 PyQtGraph Viewer.

    H의 320개 값은 이미 주파수 영역 데이터이므로 추가 FFT를 하지 않고,
    x=minKhz + index*intervalHz/1000 [kHz] 축으로 그대로 표시한다.
    """
    try:
        import pyqtgraph as pg
        from pyqtgraph.Qt import QtCore, QtWidgets
    except Exception as e:
        try:
            command_queue.put_nowait({
                "type": "error",
                "message": (
                    f"PyQtGraph 실행 실패: {e}\\n"
                    "python -m pip install pyqtgraph PySide6"
                ),
            })
        except Exception:
            pass
        return

    class AxisHoldWheelViewBox(pg.ViewBox):
        """발생기와 동일: 왼쪽 버튼+휠은 X, 오른쪽 버튼+휠은 Y.

        Right-button motion must NOT zoom Y: only the mouse wheel is allowed to
        start Y zoom. The wheel also cancels the active drag until mouse release.
        """
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._wheel_gesture_buttons = QtCore.Qt.MouseButton.NoButton

        def wheelEvent(self, ev, axis=None):
            # QApplication tracks pressed buttons even if GraphicsScene delivers
            # wheel events without reliable mouse-button flags on some platforms.
            buttons = ev.buttons() | QtWidgets.QApplication.mouseButtons()
            if buttons & QtCore.Qt.MouseButton.RightButton:
                axis = 1
                self._wheel_gesture_buttons |= QtCore.Qt.MouseButton.RightButton
                if not self.state['mouseEnabled'][1]:
                    ev.accept()  # Keep the explicit Y-axis lock.
                    return
            elif buttons & QtCore.Qt.MouseButton.LeftButton:
                axis = 0
                self._wheel_gesture_buttons |= QtCore.Qt.MouseButton.LeftButton
            super().wheelEvent(ev, axis=axis)

        def mouseDragEvent(self, ev, axis=None):
            # PyQtGraph's default right-drag scales the Y axis without using
            # the wheel. Disable that conflicting gesture so both GUIs agree.
            if ev.button() == QtCore.Qt.MouseButton.RightButton:
                if ev.isFinish():
                    self._wheel_gesture_buttons = QtCore.Qt.MouseButton.NoButton
                ev.accept()
                return
            if self._wheel_gesture_buttons:
                if ev.isFinish():
                    self._wheel_gesture_buttons = QtCore.Qt.MouseButton.NoButton
                ev.accept()
                return
            super().mouseDragEvent(ev, axis=axis)

        def mouseClickEvent(self, ev):
            self._wheel_gesture_buttons = QtCore.Qt.MouseButton.NoButton
            super().mouseClickEvent(ev)  # Keep ordinary right-click menu.

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])

    class HWindow(QtWidgets.QMainWindow):
        def closeEvent(self, event):
            try:
                command_queue.put_nowait({"type": "closed"})
            except Exception:
                pass
            super().closeEvent(event)

    win = HWindow()
    win.setWindowTitle("ULTRASONIC H - 320 Frequency Values")
    win.resize(1050, 760)

    central = QtWidgets.QWidget()
    win.setCentralWidget(central)
    layout = QtWidgets.QVBoxLayout(central)

    top = QtWidgets.QHBoxLayout()
    layout.addLayout(top)

    top.addWidget(QtWidgets.QLabel("ULTRASONIC Device"))
    device_combo = QtWidgets.QComboBox()
    device_combo.setMinimumWidth(220)
    top.addWidget(device_combo)

    top.addSpacing(12)
    top.addWidget(QtWidgets.QLabel("Y Min"))
    y_min_edit = QtWidgets.QLineEdit()
    y_min_edit.setMaximumWidth(100)
    top.addWidget(y_min_edit)

    top.addWidget(QtWidgets.QLabel("Y Max"))
    y_max_edit = QtWidgets.QLineEdit()
    y_max_edit.setMaximumWidth(100)
    top.addWidget(y_max_edit)

    apply_y_btn = QtWidgets.QPushButton("Y축 적용/고정")
    auto_y_btn = QtWidgets.QPushButton("Y축 자동")
    view_all_btn = QtWidgets.QPushButton("전체 보기")
    top.addWidget(apply_y_btn)
    top.addWidget(auto_y_btn)
    top.addWidget(view_all_btn)
    top.addStretch(1)

    nav_label = QtWidgets.QLabel(
        "휠: 기존 확대 | 좌클릭 유지+휠: X만 | 우클릭 유지+휠: Y만 (Y고정 제외) | 우클릭 이동: 확대 안 함 | 우클릭: 메뉴"
    )
    layout.addWidget(nav_label)

    info = QtWidgets.QLabel("초음파 데이터 수신 대기 중...")
    layout.addWidget(info)

    plot = pg.PlotWidget(viewBox=AxisHoldWheelViewBox())
    plot.showGrid(x=True, y=True, alpha=0.25)
    plot.setLabel("bottom", "Frequency", units="kHz")
    plot.setLabel("left", "Value")
    curve = plot.plot(pen=pg.mkPen(width=1.2))
    layout.addWidget(plot, 1)
    ultrasonic_cursor = pg.TextItem(anchor=(0, 1),
        border=pg.mkPen(80, 80, 80), fill=pg.mkBrush(255, 255, 235, 235),
        color=(30, 30, 30))
    ultrasonic_cursor.hide()
    plot.addItem(ultrasonic_cursor, ignoreBounds=True)
    ultrasonic_dot = pg.ScatterPlotItem(size=9,
        brush=pg.mkBrush(255, 135, 0), pen=pg.mkPen(60, 60, 60))
    ultrasonic_dot.hide()
    plot.addItem(ultrasonic_dot, ignoreBounds=True)
    cursor_freq = np.array([], dtype=np.float64)
    cursor_values = np.array([], dtype=np.float64)

    def ultrasonic_hover(event):
        scene_pos = event[0] if isinstance(event, tuple) else event
        if not plot.sceneBoundingRect().contains(scene_pos) or not len(cursor_freq):
            ultrasonic_cursor.hide(); ultrasonic_dot.hide()
            return
        pos = plot.getViewBox().mapSceneToView(scene_pos)
        i = int(np.argmin(np.abs(cursor_freq - pos.x())))
        x, y = float(cursor_freq[i]), float(cursor_values[i])
        scene_pt = plot.getViewBox().mapViewToScene(QtCore.QPointF(x, y))
        if ((scene_pt.x() - scene_pos.x())**2
                + (scene_pt.y() - scene_pos.y())**2) > 35**2:
            ultrasonic_cursor.hide(); ultrasonic_dot.hide()
            return
        ultrasonic_cursor.setText(f"X={x:.6g} kHz / Y={y:.6g} (원본값)")
        ultrasonic_cursor.setPos(x, y)
        ultrasonic_dot.setData([x], [y])
        ultrasonic_cursor.show(); ultrasonic_dot.show()

    ultrasonic_hover_proxy = pg.SignalProxy(plot.scene().sigMouseMoved,
                                             rateLimit=40, slot=ultrasonic_hover)

    devices = []
    selected = ""
    current_device = ""
    current_record = None
    fixed_y = None
    suppress_combo = False

    def put_command(payload):
        try:
            command_queue.put_nowait(payload)
        except Exception:
            pass

    def set_devices(new_devices):
        nonlocal devices, selected, suppress_combo
        new_devices = [str(v) for v in (new_devices or [])]
        old = device_combo.currentText().strip()

        if new_devices == devices:
            return

        devices = new_devices
        suppress_combo = True
        device_combo.clear()
        device_combo.addItems(devices)

        target = old if old in devices else (selected if selected in devices else "")
        if not target and devices:
            target = devices[0]

        if target:
            idx = device_combo.findText(target)
            if idx >= 0:
                device_combo.setCurrentIndex(idx)

        selected = target
        suppress_combo = False

        if target:
            put_command({"type": "select", "device_id": target})

    def combo_changed(_index):
        nonlocal selected
        if suppress_combo:
            return
        selected = device_combo.currentText().strip()
        if selected:
            put_command({"type": "select", "device_id": selected})

    device_combo.currentIndexChanged.connect(combo_changed)

    def apply_fixed_y():
        nonlocal fixed_y
        a = y_min_edit.text().strip()
        b = y_max_edit.text().strip()
        try:
            y0 = float(a)
            y1 = float(b)
        except Exception:
            QtWidgets.QMessageBox.warning(
                win, "Y축", "Y Min / Y Max를 숫자로 입력하세요."
            )
            return

        if not np.isfinite(y0) or not np.isfinite(y1) or y0 >= y1:
            QtWidgets.QMessageBox.warning(
                win, "Y축", "Y Min은 Y Max보다 작아야 합니다."
            )
            return

        fixed_y = (y0, y1)
        plot.setMouseEnabled(x=True, y=False)
        plot.setYRange(y0, y1, padding=0)

    def auto_y():
        nonlocal fixed_y
        fixed_y = None
        y_min_edit.clear()
        y_max_edit.clear()
        plot.setMouseEnabled(x=True, y=True)
        plot.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)

    def view_all():
        plot.enableAutoRange(axis=pg.ViewBox.XAxis, enable=True)
        if fixed_y is None:
            plot.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
            plot.autoRange()
        else:
            plot.autoRange()
            plot.setYRange(fixed_y[0], fixed_y[1], padding=0)
            plot.setMouseEnabled(x=True, y=False)

    apply_y_btn.clicked.connect(apply_fixed_y)
    auto_y_btn.clicked.connect(auto_y)
    view_all_btn.clicked.connect(view_all)

    def apply_snapshot(msg):
        nonlocal current_device, current_record, selected, cursor_freq, cursor_values

        device_id = str(msg.get("device_id", ""))
        samples = np.asarray(msg.get("samples", []), dtype=np.float64)
        freq_khz = np.asarray(msg.get("freq_khz", []), dtype=np.float64)

        if not len(samples) or len(samples) != len(freq_khz):
            return

        device_changed = device_id != current_device
        current_device = device_id
        current_record = msg
        selected = device_id

        curve.setData(freq_khz, samples)
        cursor_freq, cursor_values = freq_khz, samples

        if device_changed:
            idx = device_combo.findText(device_id)
            if idx >= 0 and device_combo.currentIndex() != idx:
                device_combo.blockSignals(True)
                device_combo.setCurrentIndex(idx)
                device_combo.blockSignals(False)
            view_all()
        elif fixed_y is not None:
            plot.setYRange(fixed_y[0], fixed_y[1], padding=0)

        min_khz = float(msg.get("minKhz", 0))
        interval_hz = float(msg.get("intervalHz", 0))
        peak_khz = float(msg.get("peak_khz", 0))
        peak_value = float(msg.get("peak_value", 0))
        value_min = float(msg.get("value_min", 0))
        value_max = float(msg.get("value_max", 0))
        seq = str(msg.get("seq", ""))

        info.setText(
            f"{device_id} | 320 values | "
            f"minKhz={min_khz:g} | intervalHz={interval_hz:g} | "
            f"{freq_khz[0]:.3f} ~ {freq_khz[-1]:.3f} kHz | "
            f"min={value_min:.9g} | max={value_max:.9g} | "
            f"peak={peak_value:.9g} @ {peak_khz:.3f} kHz | seq={seq}"
        )

    def poll():
        try:
            while True:
                msg = data_queue.get_nowait()
                typ = msg.get("type")
                if typ == "devices":
                    set_devices(msg.get("devices", []))
                elif typ == "snapshot":
                    apply_snapshot(msg)
                elif typ == "close":
                    win.close()
                    return
        except queue.Empty:
            pass
        except Exception as e:
            put_command({"type": "error", "message": f"ULTRASONIC graph error: {e}"})

    timer = QtCore.QTimer()
    timer.timeout.connect(poll)
    timer.start(50)

    win.show()
    app.exec()



# Two-sided FFT / IFFT verification. Original complex coefficients are never
# replaced by normalized magnitude, RMS, gN, or averaged magnitude.
def fft_roundtrip_diagnostic(samples, fs, gain=None):
    x = np.asarray(samples, dtype=np.float64)
    fs = float(fs)
    if x.ndim != 1 or len(x) < 2 or fs <= 0 or not np.isfinite(fs) or not np.all(np.isfinite(x)):
        raise ValueError('유효한 RAW 샘플과 Fs가 필요합니다.')
    n = len(x)
    spectrum = np.fft.fft(x)
    reconstructed = np.fft.ifft(spectrum)
    f = np.fft.fftshift(np.fft.fftfreq(n, d=1.0/fs))
    shifted = np.fft.fftshift(spectrum)
    gain = float(n if gain is None else gain)
    if not np.isfinite(gain) or gain <= 0:
        raise ValueError('FFT 정규화 분모 오류')
    # Single-sided amplitude derived from the same complex FFT used for IFFT.
    # DC and (for even N) Nyquist are unique bins and are not doubled.
    single_f = np.fft.rfftfreq(n, d=1.0/fs)
    single_magnitude = np.abs(spectrum[:n//2+1])/gain
    if n % 2 == 0:
        single_magnitude[1:-1] *= 2.0
    else:
        single_magnitude[1:] *= 2.0
    error = reconstructed.real - x
    return dict(t=np.arange(n)/fs, x=x, f=f, magnitude=np.abs(shifted)/gain,
                single_f=single_f, single_magnitude=single_magnitude,
                real=shifted.real/gain, imag=shifted.imag/gain,
                restored=reconstructed.real, error=error,
                max_error=float(np.max(np.abs(error))),
                rms_error=float(np.sqrt(np.mean(error**2))),
                imag_error=float(np.max(np.abs(reconstructed.imag))),
                passed=bool(np.allclose(reconstructed.real,x,rtol=1e-10,atol=1e-8)),
                gain=gain, fs=fs, n=n)


def fft_diagnostic_text(d, label):
    return (f'{label} | N={d["n"]:,} / Fs={d["fs"]:g} Hz | '
            f'IFFT {"PASS" if d["passed"] else "FAIL"} | '
            f'최대 오차={d["max_error"]:.3e} Count | RMS 오차={d["rms_error"]:.3e} | '
            f'잔류 허수={d["imag_error"]:.3e} | 양측=|FFT|/{d["gain"]:g} | 단측=2×양측 (DC/Nyquist 제외)')


def show_fft_diagnostic_qt(parent, records, title):
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtWidgets
    if not records:
        QtWidgets.QMessageBox.information(parent, 'FFT/IFFT', '선택된 RAW 데이터가 없습니다. 초음파 H는 위상이 없는 스펙트럼입니다.')
        return
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle('FFT 과정 / IFFT 검증 · ' + title)
    dialog.resize(1100, 850)
    layout = QtWidgets.QVBoxLayout(dialog)
    bar = QtWidgets.QHBoxLayout(); layout.addLayout(bar)
    bar.addWidget(QtWidgets.QLabel('축'))
    axis = QtWidgets.QComboBox(); axis.addItems(list(records)); bar.addWidget(axis)
    bar.addWidget(QtWidgets.QLabel('FFT 입력'))
    stage = QtWidgets.QComboBox(); stage.addItems(['원본 RAW (복원 검증)', '현재 FFT 전처리 적용']); bar.addWidget(stage)
    parts = QtWidgets.QCheckBox('실수부 / 허수부'); bar.addWidget(parts)
    bar.addStretch(1)
    note = QtWidgets.QLabel('① 입력 → ② 양측 FFT → ③ 단측 FFT | ② 복소수 FFT → ④ IFFT 복원 · 평균/SRSS/물리단위 보정은 검증 제외'); note.setWordWrap(True); layout.addWidget(note)
    status = QtWidgets.QLabel(); status.setWordWrap(True); layout.addWidget(status)
    graphics = pg.GraphicsLayoutWidget(); layout.addWidget(graphics, 1)
    plots = [graphics.addPlot(row=i//2,col=i%2) for i in range(4)]
    def draw(*_):
        rec = records[axis.currentText()]
        processed = stage.currentIndex() == 1
        x = rec['processed'] if processed else rec['raw']
        d = fft_roundtrip_diagnostic(x,rec['fs'],rec['gain'] if processed else None)
        for plot in plots: plot.clear(); plot.showGrid(x=True,y=True,alpha=.2)
        plots[0].setTitle('① FFT 입력 (전처리)' if processed else '① 원본 RAW Count (DC 포함)')
        plots[0].setLabel('bottom','Time',units='s'); plots[0].setLabel('left','Count')
        plots[0].plot(d['t'],d['x'],pen=pg.mkPen('#1f77b4',width=1.3))
        plots[1].setTitle('② 양측 FFT · 음수/양수 주파수')
        plots[1].setLabel('bottom','Frequency',units='Hz'); plots[1].setLabel('left','Count')
        plots[1].plot(d['f'],d['magnitude'],pen=pg.mkPen('#9467BD',width=1.5))
        if parts.isChecked():
            plots[1].plot(d['f'],d['real'],pen=pg.mkPen('#1f77b4',width=1))
            plots[1].plot(d['f'],d['imag'],pen=pg.mkPen('#2ca02c',width=1))
        plots[2].setTitle('③ 단측 FFT · 양/음 주파수 진폭 합성 (Peak)')
        plots[2].setLabel('bottom','Frequency',units='Hz'); plots[2].setLabel('left','Count Peak')
        plots[2].plot(d['single_f'],d['single_magnitude'],pen=pg.mkPen('#d62728',width=1.5))
        plots[3].setTitle('④ IFFT 복원 (빨강) / FFT 입력 (파랑)')
        plots[3].setLabel('bottom','Time',units='s'); plots[3].setLabel('left','Count')
        plots[3].plot(d['t'],d['x'],pen=pg.mkPen('#1f77b4',width=2))
        plots[3].plot(d['t'],d['restored'],pen=pg.mkPen('#d62728',width=1))
        status.setText(fft_diagnostic_text(d, '전처리 입력 복원 (원본 아님)' if processed else '원본 RAW 복원'))
        for plot in plots: plot.enableAutoRange()
    axis.currentTextChanged.connect(draw); stage.currentIndexChanged.connect(draw); parts.toggled.connect(draw)
    draw(); dialog.exec() if hasattr(dialog,'exec') else dialog.exec_()



def _pyqtgraph_viewer_process(mode, data_queue, command_queue, engine="original"):
    """PyQtGraph viewer in a separate Qt process.

    Tkinter와 Qt의 event loop 충돌을 피하기 위해 그래프만 별도 process로 실행한다.
    Raw는 Time(s), FFT는 Frequency(Hz) 축을 사용한다.
    PyQtGraph의 clipToView + peak downsampling으로 전체 8192 sample 표시 시
    Canvas에서 보이던 고주파 모아레/표시 alias를 줄인다.
    """
    try:
        import pyqtgraph as pg
        from pyqtgraph.Qt import QtCore, QtWidgets
    except Exception as e:
        try:
            command_queue.put_nowait({
                "type": "error",
                "message": (
                    "PyQtGraph/Qt를 불러올 수 없습니다.\n\n"
                    f"오류: {e}\n\n"
                    "설치 예:\n"
                    "python -m pip install pyqtgraph PySide6"
                ),
            })
        except Exception:
            pass
        return

    mode = str(mode).upper()
    scipy_mode = engine == "scipy"
    if scipy_mode:
        try:
            from scipy import signal, fft
        except ImportError as e:
            command_queue.put({"type": "error", "message":
                               "SciPy 비교 창은 scipy 설치가 필요합니다.\n"
                               "python -m pip install scipy\n" + str(e)})
            return
    fft_calculator = scipy_fft_comparison if scipy_mode else calculate_fft
    spectral_filter = scipy_filter_signal if scipy_mode else apply_spectral_filters
    class AxisHoldWheelViewBox(pg.ViewBox):
        """발생기와 동일: 왼쪽 버튼+휠은 X, 오른쪽 버튼+휠은 Y.

        Right-button motion must NOT zoom Y: only the mouse wheel is allowed to
        start Y zoom. The wheel also cancels the active drag until mouse release.
        """
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._wheel_gesture_buttons = QtCore.Qt.MouseButton.NoButton

        def wheelEvent(self, ev, axis=None):
            # QApplication tracks pressed buttons even if GraphicsScene delivers
            # wheel events without reliable mouse-button flags on some platforms.
            buttons = ev.buttons() | QtWidgets.QApplication.mouseButtons()
            if buttons & QtCore.Qt.MouseButton.RightButton:
                axis = 1
                self._wheel_gesture_buttons |= QtCore.Qt.MouseButton.RightButton
                if not self.state['mouseEnabled'][1]:
                    ev.accept()  # Keep the explicit Y-axis lock.
                    return
            elif buttons & QtCore.Qt.MouseButton.LeftButton:
                axis = 0
                self._wheel_gesture_buttons |= QtCore.Qt.MouseButton.LeftButton
            super().wheelEvent(ev, axis=axis)

        def mouseDragEvent(self, ev, axis=None):
            # PyQtGraph's default right-drag scales the Y axis without using
            # the wheel. Disable that conflicting gesture so both GUIs agree.
            if ev.button() == QtCore.Qt.MouseButton.RightButton:
                if ev.isFinish():
                    self._wheel_gesture_buttons = QtCore.Qt.MouseButton.NoButton
                ev.accept()
                return
            if self._wheel_gesture_buttons:
                if ev.isFinish():
                    self._wheel_gesture_buttons = QtCore.Qt.MouseButton.NoButton
                ev.accept()
                return
            super().mouseDragEvent(ev, axis=axis)

        def mouseClickEvent(self, ev):
            self._wheel_gesture_buttons = QtCore.Qt.MouseButton.NoButton
            super().mouseClickEvent(ev)  # Keep ordinary right-click menu.

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv)

    # 밝은 배경. 실제 curve 색은 축 구분을 위해 고정.
    pg.setConfigOption("background", "w")
    pg.setConfigOption("foreground", "k")
    pg.setConfigOptions(antialias=False)

    window = QtWidgets.QMainWindow()
    window.setWindowTitle(f"{mode} Raw + FFT - {'SciPy 비교' if scipy_mode else '기존 계산'}")
    window.resize(1050, 860)  # V046: compact initial size; freely resizable

    central = QtWidgets.QWidget()
    window.setCentralWidget(central)
    layout = QtWidgets.QVBoxLayout(central)

    top = QtWidgets.QHBoxLayout()
    layout.addLayout(top)
    top.addWidget(QtWidgets.QLabel(f"{mode} Device"))

    combo = QtWidgets.QComboBox()
    combo.setMinimumWidth(230)
    top.addWidget(combo)

    full_btn = QtWidgets.QPushButton("전체 보기")
    top.addSpacing(8)
    top.addWidget(full_btn)
    diagnostic_btn = QtWidgets.QPushButton("FFT 과정 / IFFT 검증")
    top.addWidget(diagnostic_btn)

    axis_checks = {}
    mems_raw_display_unit = "g"
    if mode == "ADXL":
        top.addSpacing(12)
        top.addWidget(QtWidgets.QLabel("표시 축"))
        for axis in ("X", "Y", "Z", "gN", "SRSS"):
            cb = QtWidgets.QCheckBox(axis)
            cb.setChecked(True)
            axis_checks[axis] = cb
            top.addWidget(cb)

    if mode == "ADXL":
        top.addWidget(QtWidgets.QLabel("한계값 (mm/s RMS)"))
        srss_limit_entry = QtWidgets.QLineEdit("4.5")
        srss_limit_entry.setMaximumWidth(85)
        top.addWidget(srss_limit_entry)
        srss_limit_btn = QtWidgets.QPushButton("적용")
        top.addWidget(srss_limit_btn)
    top.addStretch(1)

    # PIEZO / MEMS 공통 FFT Window 선택.
    fft_top = QtWidgets.QHBoxLayout()
    fft_top.addWidget(QtWidgets.QLabel("FFT Window"))

    fft_window_combo = QtWidgets.QComboBox()
    fft_window_combo.addItem(
        "Rectangular (None)",
        "Rectangular",
    )
    fft_window_combo.addItem("Hann", "Hann")
    fft_window_combo.addItem("Hamming", "Hamming")
    fft_window_combo.addItem("Blackman", "Blackman")
    fft_window_combo.addItem(
        "Blackman-Harris",
        "Blackman-Harris",
    )
    fft_window_combo.addItem("Flat Top", "Flat Top")
    fft_window_combo.addItem("Bartlett", "Bartlett")
    fft_window_combo.addItem("Kaiser", "Kaiser")
    fft_window_combo.setCurrentIndex(3)  # Blackman 기본값
    fft_window_combo.setMinimumWidth(165)
    fft_top.addWidget(fft_window_combo)

    fft_top.addSpacing(12)
    fft_top.addWidget(QtWidgets.QLabel("Window Size"))

    fft_window_size_combo = QtWidgets.QComboBox()
    fft_window_size_combo.setMinimumWidth(100)
    fft_top.addWidget(fft_window_size_combo)

    # 메인 설정이 아직 전달되지 않았을 때의 초기값.
    configured_sample_rate = 0.0
    configured_sample_count = int(SAMPLE_COUNTS[-1])

    kaiser_beta_label = QtWidgets.QLabel("Kaiser β")
    fft_top.addWidget(kaiser_beta_label)

    kaiser_beta_spin = QtWidgets.QDoubleSpinBox()
    kaiser_beta_spin.setDecimals(2)
    kaiser_beta_spin.setRange(0.0, 30.0)
    kaiser_beta_spin.setSingleStep(0.5)
    kaiser_beta_spin.setValue(8.6)
    kaiser_beta_spin.setMaximumWidth(90)
    fft_top.addWidget(kaiser_beta_spin)

    # Kaiser 선택 시에만 Beta가 의미가 있다.
    kaiser_beta_label.setEnabled(False)
    kaiser_beta_spin.setEnabled(False)

    fft_top.addStretch(1)

    info = QtWidgets.QLabel(f"{mode} 데이터 수신 대기 중...")
    info.setWordWrap(True)
    layout.addWidget(info)

    # V032: PIEZO/MEMS 그래프 상단 설정 영역만 스크롤되도록 분리한다.
    # 메인 장치 선택/전체 보기와 RAW/FFT 그래프는 스크롤 영역 밖에 유지한다.
    graph_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
    graph_splitter.setChildrenCollapsible(False)
    layout.addWidget(graph_splitter, 1)

    options_scroll = QtWidgets.QScrollArea()
    options_scroll.setWidgetResizable(True)
    options_scroll.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
    options_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    options_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    options_scroll.setMinimumHeight(100)
    options_container = QtWidgets.QWidget()
    options_layout = QtWidgets.QVBoxLayout(options_container)
    options_layout.setContentsMargins(8, 6, 8, 6)
    options_layout.setSpacing(6)
    options_layout.addLayout(fft_top)
    options_scroll.setWidget(options_container)
    graph_splitter.addWidget(options_scroll)
    piezo_pp_info = None
    if mode == "PIEZO":
        piezo_pp_info = QtWidgets.QLabel(
            "패킷 P-P: 수신 대기 중 | 누적 ΣMax−ΣMin: 수신 대기 중"
        )
        piezo_pp_info.setToolTip(
            "원본 PIEZO Count의 패킷별 최대·최소 차이를 mV로 변환합니다. "
            "누적은 수신 처리한 패킷마다 최대값 합계 - 최소값 합계입니다. "
            "필터·0점 보정과 독립적이며, 장치별로 누적됩니다."
        )
        options_layout.addWidget(piezo_pp_info)

    # v0.63: 같은 수신 패킷에 대한 서로 다른 두 가지 g RMS 추정값 비교.
    # 원본 P-P와 현재 FFT 피크를 이용하며, 데이터를 재가공해 저장하지 않는다.
    piezo_grms_compare_info = None
    if mode == "PIEZO":
        piezo_grms_compare_info = QtWidgets.QLabel(
            "선택 단위 측정값: 수신 대기 중"
        )
        piezo_grms_compare_info.setWordWrap(True)
        piezo_grms_compare_info.setToolTip("선택한 단위의 결과만 표시합니다. 다른 단위는 PIEZO 표시 단위 목록에서 선택하세요.")
        options_layout.addWidget(piezo_grms_compare_info)
        piezo_formula_info = QtWidgets.QLabel("선택 단위 계산식: 데이터 수신 대기 중")
        piezo_formula_info.setWordWrap(True)
        piezo_formula_info.setToolTip("현재 선택한 단위에 적용된 계산식만 표시합니다. 속도 RMS는 FFT의 개별 주파수 성분 값입니다.")
        options_layout.addWidget(piezo_formula_info)
        piezo_formula_info.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        piezo_formula_info.setStyleSheet(
            "QLabel { background: #eef4fa; color: #183450; "
            "border: 1px solid #b7cddd; border-radius: 5px; padding: 8px; }"
        )

        piezo_cycle_info = QtWidgets.QLabel("FFT 피크 기준 1주기: 수신 대기 중")
        piezo_cycle_info.setWordWrap(True)
        options_layout.addWidget(piezo_cycle_info)

    if mode == "ADXL":
        # FFT Window/Window Size 컨트롤과 겹치지 않도록 별도 행에 배치.
        unit_row = QtWidgets.QHBoxLayout()
        options_layout.addLayout(unit_row)
        unit_row.addWidget(QtWidgets.QLabel("MEMS 표시 단위"))

        unit_row.addWidget(QtWidgets.QLabel("RAW Y축"))
        mems_raw_unit_combo = QtWidgets.QComboBox()
        for label, key in (("자동 가속도 (g/mg/µg)", "auto"), ("ADC (Count)", "count"),
                           ("가속도 (g)", "g"), ("가속도 (mg)", "mg"), ("가속도 (µg)", "µg")):
            mems_raw_unit_combo.addItem(label, key)
        mems_raw_unit_combo.setCurrentIndex(2)  # 기존 수신기 기본 g 유지
        mems_raw_unit_combo.setToolTip("RAW 단위는 FFT와 독립적입니다. 수동 선택은 수신 중 유지됩니다.")
        unit_row.addWidget(mems_raw_unit_combo)
        mems_raw_display_unit = "g"
        unit_row.addSpacing(8)
        unit_row.addWidget(QtWidgets.QLabel("FFT Y축"))
        mems_fft_unit_combo = QtWidgets.QComboBox()
        for label, key in (("가속도 (g RMS)", "g_rms"), ("ADC (Count Peak)", "count_peak"),
                           ("가속도 (g Peak)", "g_peak"), ("속도 (mm/s RMS)", "velocity_rms")):
            mems_fft_unit_combo.addItem(label, key)
        unit_row.addWidget(mems_fft_unit_combo)
        mems_fft_formula_inline = QtWidgets.QLabel()
        mems_fft_formula_inline.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        mems_fft_formula_inline.setWordWrap(True)
        mems_fft_formula_inline.setStyleSheet("color: #365a78;")
        def refresh_mems_fft_formula_inline(_index=None):
            formulas = {"velocity_rms": "V RMS[mm/s] = FFT g RMS × 9806.65/(2πf), f>0 Hz",
                        "g_rms": "FFT g RMS = FFT Count Peak × (g/LSB)/√2",
                        "g_peak": "FFT g Peak = FFT Count Peak × (g/LSB)",
                        "count_peak": "FFT Count Peak = 2|rFFT(C×w)|/Σw"}
            mems_fft_formula_inline.setText(formulas[mems_fft_unit_combo.currentData()])
        unit_row.addWidget(mems_fft_formula_inline)
        mems_fft_unit_combo.currentIndexChanged.connect(refresh_mems_fft_formula_inline)
        refresh_mems_fft_formula_inline()
        unit_row.addSpacing(12)
        unit_row.addWidget(QtWidgets.QLabel("1 LSB ="))
        mems_g_per_lsb_spin = QtWidgets.QDoubleSpinBox()
        mems_g_per_lsb_spin.setDecimals(9)
        mems_g_per_lsb_spin.setRange(0.000000001, 10.0)
        mems_g_per_lsb_spin.setSingleStep(0.0001)
        mems_g_per_lsb_spin.setValue(read_mems_g_per_lsb())
        mems_g_per_lsb_spin.setSuffix(" g")
        mems_g_per_lsb_spin.setMinimumWidth(155)
        mems_g_per_lsb_spin.setToolTip("ADXL343 Full Resolution 공칭값: 1/256 g = 0.00390625 g/LSB. 원본 Count 및 MQTT 내용 변경 없음.")
        unit_row.addWidget(mems_g_per_lsb_spin)
        mems_scale_label = QtWidgets.QLabel(f"= {1.0 / mems_g_per_lsb_spin.value():.7g} LSB/g")
        unit_row.addWidget(mems_scale_label)
        unit_row.addStretch(1)
        mems_correction_controls = {}
        mems_correction_values = load_mems_correction()
        correction_row = QtWidgets.QHBoxLayout()
        correction_row2 = QtWidgets.QHBoxLayout()
        options_layout.addLayout(correction_row)
        options_layout.addLayout(correction_row2)
        for key, title in (('frequency_enabled', '주파수 보정'),('sensitivity_enabled','감도 보정')):
            check = QtWidgets.QCheckBox(title)
            check.setChecked(mems_correction_values[key])
            mems_correction_controls[key] = check
            correction_row.addWidget(check)
        # 보정 버튼은 체크박스 바로 옆에 배치하여 화면 우측으로 밀리지 않도록 한다.
        correction_btn = QtWidgets.QPushButton('보정 적용/저장')
        correction_row.addWidget(correction_btn)
        correction_row.addSpacing(12)
        for i, (key, title) in enumerate((
            ('frequency_divisor','f 나눗수'), ('low_hz','하한 Hz'), ('high_hz','상한 Hz'),
            ('low_sensitivity','낮은 S'), ('mid_intercept','중간 상수'),
            ('mid_slope','중간 기울기'), ('high_sensitivity','높은 S'))):
            parent_row = correction_row if i < 3 else correction_row2
            parent_row.addWidget(QtWidgets.QLabel(title))
            spin = QtWidgets.QDoubleSpinBox()
            spin.setDecimals(7)
            spin.setRange(-1000000.0, 1000000.0)
            spin.setSingleStep(0.001 if key != 'mid_slope' else 0.0001)
            spin.setValue(mems_correction_values[key])
            spin.setMinimumWidth(90)
            mems_correction_controls[key] = spin
            parent_row.addWidget(spin)
        correction_row.addStretch(1)
        correction_row2.addStretch(1)
        correction_hint = QtWidgets.QLabel(format_mems_correction_formula(mems_correction_values))
        correction_hint.setWordWrap(True)
        correction_hint.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        correction_hint.setStyleSheet('QLabel { background: #eef4fa; color: #183450; border: 1px solid #b7cddd; border-radius: 5px; padding: 6px; }')
        options_layout.addWidget(correction_hint)
        def update_mems_correction_formula(*_):
            try:
                values = {key: (control.isChecked() if key.endswith('_enabled') else control.value())
                          for key, control in mems_correction_controls.items()}
                correction_hint.setText(format_mems_correction_formula(validate_mems_correction(values)))
            except (ValueError, TypeError):
                correction_hint.setText('보정 수식: 입력값을 확인해 주세요. 유효한 값 입력 시 자동 표시됩니다.')
        for key, control in mems_correction_controls.items():
            if key.endswith('_enabled'):
                control.stateChanged.connect(update_mems_correction_formula)
            else:
                control.valueChanged.connect(update_mems_correction_formula)


    # PIEZO의 ADC 스케일은 하드웨어 확인 전 임시 가정이다.
    # signed int16 RAW를 그대로 보존하고, 평균(또는 수동 0점)을 뺀
    # AC 성분만 V로 환산한다. 절대 ADC 입력전압/센서 출력전압은 아니다.
    piezo_unit_combo = None
    # V013: PIEZO 그래프 프로세스에서 디바이스별 전압을 관리.
    device_voltage_path = get_app_dir() / PIEZO_DEVICE_VOLTAGE_FILENAME
    device_voltages = load_piezo_device_voltages(device_voltage_path) if mode == "PIEZO" else {}
    piezo_ref_voltage = None
    piezo_adc_denominator = None
    if mode == "PIEZO":
        voltage_row = QtWidgets.QHBoxLayout()
        options_layout.addLayout(voltage_row)
        voltage_row.addWidget(QtWidgets.QLabel("PIEZO 표시 단위"))
        piezo_unit_combo = QtWidgets.QComboBox()
        piezo_unit_combo.addItem("전압 (mV; RAW 평균=0 표시)", "voltage")
        piezo_unit_combo.addItem("Count (RAW 그래프 평균=0; 실제 원본 별도)", "count")
        piezo_unit_combo.addItem("가속도 g / FFT g RMS (RAW 평균=0)", "g_spec")
        piezo_unit_combo.addItem("진동 속도 RMS (FFT mm/s, RAW 가속도 g)", "velocity_rms")
        voltage_row.addWidget(piezo_unit_combo)
        piezo_fft_formula_inline = QtWidgets.QLabel()
        piezo_fft_formula_inline.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        piezo_fft_formula_inline.setWordWrap(True)
        piezo_fft_formula_inline.setStyleSheet("color: #365a78;")
        def refresh_piezo_fft_formula_inline(_index=None):
            texts = {
                "voltage": "FFT mV Peak = FFT Count Peak × K",
                "count": "FFT Count Peak = 2|rFFT(C×w)|/Σw",
                "g_spec": "FFT g RMS = FFT mV Peak/(감도×√2)",
                "velocity_rms": "V RMS[mm/s] = FFT g RMS × 9806.65/(2πf), f>0 Hz",
                "g_pp": "FFT g RMS = FFT mV Peak × (기준 g Peak/기준 mV P-P)/√2",
            }
            piezo_fft_formula_inline.setText(texts.get(piezo_unit_combo.currentData(), ""))
        voltage_row.addWidget(piezo_fft_formula_inline)
        piezo_unit_combo.currentIndexChanged.connect(refresh_piezo_fft_formula_inline)
        refresh_piezo_fft_formula_inline()
        voltage_row.addWidget(QtWidgets.QLabel("ADC 전압 범위"))
        piezo_ref_voltage = QtWidgets.QDoubleSpinBox()
        piezo_ref_voltage.setDecimals(6)
        piezo_ref_voltage.setRange(0.000001, 100000.0)
        piezo_ref_voltage.setValue(PIEZO_ADC_VMAX)
        piezo_ref_voltage.setSuffix(" V")
        voltage_row.addWidget(piezo_ref_voltage)
        voltage_row.addWidget(QtWidgets.QLabel("ADC 분모"))
        piezo_adc_denominator = QtWidgets.QDoubleSpinBox()
        piezo_adc_denominator.setDecimals(2)
        piezo_adc_denominator.setRange(1, 1000000000)
        piezo_adc_denominator.setValue(4096)
        piezo_adc_denominator.setEnabled(False)
        piezo_ref_voltage.setEnabled(True)  # V013: 사용자가 실행 중 변경 가능
        piezo_ref_voltage.setToolTip("장치별 유효 환산 전압(V). 신규 장치는 3.890V, 저장된 장치는 고유 설정을 자동으로 불러옵니다. 변경 즉시 파일에 저장되며 그래프를 다시 계산합니다.")
        voltage_row.addWidget(piezo_adc_denominator)
        voltage_row.addWidget(QtWidgets.QLabel("수신=16bit signed | 시험 환산 분모=4096 | Vmax 변경 가능 | 센서=100mV/g"))
        voltage_row.addStretch(1)
        voltage_path_label = QtWidgets.QLabel(f"장치별 전압 설정 파일: {device_voltage_path}")
        voltage_path_label.setToolTip("센서가 처음 수신되면 기본 3.890V로 자동 등록됩니다. 변경된 전압도 같은 파일에 저장됩니다.")
        options_layout.addWidget(voltage_path_label)

        # v0.56: 실측 정현파 교정. 예: 12 mV P-P = 1 g Peak.
        # QSettings에 사용자 입력값을 보관하며, 수신 CSV는 변경하지 않는다.
        pp_settings = QtCore.QSettings("LDMS", "VibrationFFT")
        pp_row = QtWidgets.QHBoxLayout()
        options_layout.addLayout(pp_row)
        pp_calibration_check = QtWidgets.QCheckBox("실측 교정 ON")
        pp_calibration_check.setToolTip("ON: 입력한 mV P-P/g Peak 기준으로 RAW는 g, FFT는 g RMS 표시. OFF: 전압(mV) 표시")
        pp_calibration_check.setChecked(False)
        pp_row.addWidget(pp_calibration_check)
        pp_row.addWidget(QtWidgets.QLabel("기준 (정현파)"))
        piezo_pp_mv = QtWidgets.QDoubleSpinBox()
        piezo_pp_mv.setDecimals(4)
        piezo_pp_mv.setRange(0.0001, 1000000.0)
        piezo_pp_mv.setValue(float(pp_settings.value("piezo_pp_mv", 12.0)))
        piezo_pp_mv.setSuffix(" mV P-P")
        piezo_pp_mv.setToolTip("RAW에서 최대값 - 최소값을 측정한 전체 높이")
        pp_row.addWidget(piezo_pp_mv)
        pp_row.addWidget(QtWidgets.QLabel("="))
        piezo_pp_g = QtWidgets.QDoubleSpinBox()
        piezo_pp_g.setDecimals(4)
        piezo_pp_g.setRange(0.0001, 100000.0)
        piezo_pp_g.setValue(float(pp_settings.value("piezo_pp_g", 1.0)))
        piezo_pp_g.setSuffix(" g Peak")
        pp_row.addWidget(piezo_pp_g)
        pp_reset = QtWidgets.QPushButton("12mV / 1g 초기화")
        pp_row.addWidget(pp_reset)
        pp_status = QtWidgets.QLabel("50Hz 포함 실측 범위에서만 검증된 추정 교정")
        pp_row.addWidget(pp_status)
        pp_row.addStretch(1)
        # V001: 12mV P-P calibration is retired; no widget or saved setting can activate it.
        for obsolete in (pp_calibration_check, piezo_pp_mv, piezo_pp_g, pp_reset, pp_status):
            obsolete.setVisible(False)
        for idx in range(pp_row.count()):
            item = pp_row.itemAt(idx)
            if item and item.widget():
                item.widget().setVisible(False)

    # PIEZO / ADXL의 RAW 표시용 HPF와 FFT HPF는 서로 독립적으로 ON/OFF 한다.
    hpf_row = QtWidgets.QHBoxLayout()
    options_layout.addLayout(hpf_row)
    raw_hpf_enabled_check = QtWidgets.QCheckBox("RAW HPF ON")
    raw_hpf_enabled_check.setChecked(False)
    hpf_row.addWidget(raw_hpf_enabled_check)
    hpf_enabled_check = QtWidgets.QCheckBox("FFT HPF ON")
    hpf_enabled_check.setChecked(False)
    hpf_row.addWidget(hpf_enabled_check)
    hpf_row.addWidget(QtWidgets.QLabel("차단 주파수"))
    hpf_cutoff_spin = QtWidgets.QDoubleSpinBox()
    hpf_cutoff_spin.setDecimals(2)
    hpf_cutoff_spin.setRange(0.01, 1000000.0)
    hpf_cutoff_spin.setValue(10.0)
    hpf_cutoff_spin.setSuffix(" Hz")
    hpf_cutoff_spin.setMaximumWidth(120)
    hpf_row.addWidget(hpf_cutoff_spin)
    hpf_row.addWidget(QtWidgets.QLabel("필터 차수"))
    hpf_order_spin = QtWidgets.QSpinBox()
    hpf_order_spin.setRange(1, 12)
    hpf_order_spin.setValue(4)
    hpf_order_spin.setMaximumWidth(75)
    hpf_row.addWidget(hpf_order_spin)
    hpf_info_label = QtWidgets.QLabel(("SciPy Butterworth(sosfiltfilt)" if scipy_mode else "FFT 진폭응답") + " · RAW/FFT 독립 ON/OFF")
    hpf_row.addWidget(hpf_info_label)
    hpf_row.addStretch(1)
    # LPF: frequency-domain Butterworth, independent RAW/FFT toggles.
    lpf_row = QtWidgets.QHBoxLayout()
    options_layout.addLayout(lpf_row)
    raw_lpf_check = QtWidgets.QCheckBox("RAW LPF ON")
    fft_lpf_check = QtWidgets.QCheckBox("FFT LPF ON")
    lpf_row.addWidget(raw_lpf_check)
    lpf_row.addWidget(fft_lpf_check)
    lpf_row.addWidget(QtWidgets.QLabel("LPF 차단 주파수"))
    lpf_cutoff_spin = QtWidgets.QDoubleSpinBox()
    lpf_cutoff_spin.setRange(0.01, 1000000.0)
    lpf_cutoff_spin.setDecimals(2)
    lpf_cutoff_spin.setValue(1600.0)
    lpf_auto_check = QtWidgets.QCheckBox("자동 Fs/2")
    lpf_auto_check.setChecked(True)
    lpf_row.addWidget(lpf_auto_check)
    lpf_cutoff_spin.setSuffix(" Hz")
    lpf_row.addWidget(lpf_cutoff_spin)
    lpf_row.addWidget(QtWidgets.QLabel("차수"))
    lpf_order_spin = QtWidgets.QSpinBox()
    lpf_order_spin.setRange(1, 12)
    lpf_order_spin.setValue(4)
    lpf_row.addWidget(lpf_order_spin)
    lpf_info = QtWidgets.QLabel("Butterworth · RAW/FFT 독립")
    lpf_row.addWidget(lpf_info)
    lpf_row.addStretch(1)

    # Notch: Gaussian-shaped rejection around the user-specified frequency.
    notch_row = QtWidgets.QHBoxLayout()
    options_layout.addLayout(notch_row)
    raw_notch_check = QtWidgets.QCheckBox("RAW Notch ON")
    fft_notch_check = QtWidgets.QCheckBox("FFT Notch ON")
    notch_row.addWidget(raw_notch_check)
    notch_row.addWidget(fft_notch_check)
    notch_row.addWidget(QtWidgets.QLabel("중심 주파수"))
    notch_center_spin = QtWidgets.QDoubleSpinBox()
    notch_center_spin.setRange(0.01, 1000000.0)
    notch_center_spin.setDecimals(2)
    notch_center_spin.setValue(975.0)
    notch_center_spin.setSuffix(" Hz")
    notch_row.addWidget(notch_center_spin)
    notch_row.addWidget(QtWidgets.QLabel("제거 폭"))
    notch_width_spin = QtWidgets.QDoubleSpinBox()
    notch_width_spin.setRange(0.01, 1000000.0)
    notch_width_spin.setDecimals(2)
    notch_width_spin.setValue(20.0)
    notch_width_spin.setSuffix(" Hz")
    notch_row.addWidget(notch_width_spin)
    notch_info = QtWidgets.QLabel("Gaussian Notch · 중심에서 최대 감쇠")
    notch_row.addWidget(notch_info)
    notch_row.addStretch(1)

    # 회전부품 개수 역추정 컨트롤 (ADXL/PIEZO 공통).
    infer_row = QtWidgets.QHBoxLayout()
    options_layout.addLayout(infer_row)
    infer_row.addWidget(QtWidgets.QLabel("회전 부품 역추정 | RPM"))
    infer_rpm = QtWidgets.QDoubleSpinBox()
    infer_rpm.setRange(0.0, 100000.0)
    infer_rpm.setDecimals(1)
    infer_rpm.setValue(1800.0)
    infer_rpm.setSingleStep(100.0)
    infer_rpm.setMaximumWidth(120)
    infer_row.addWidget(infer_rpm)
    infer_row.addWidget(QtWidgets.QLabel("유형"))
    infer_type = QtWidgets.QComboBox()
    infer_type.addItems(["미지정", "블레이드", "기어", "플런저", "DC 정류자"])
    infer_row.addWidget(infer_type)
    infer_row.addWidget(QtWidgets.QLabel("분석 축"))
    infer_axis = QtWidgets.QComboBox()
    infer_axis.addItems(["X", "Y", "Z"] if mode == "ADXL" else ["Z"])
    infer_row.addWidget(infer_axis)
    infer_btn = QtWidgets.QPushButton("개수 추정")
    infer_row.addWidget(infer_btn)
    infer_marks = QtWidgets.QCheckBox("후보 주파수 표시")
    infer_marks.setChecked(True)
    infer_row.addWidget(infer_marks)
    infer_row.addStretch(1)
    infer_result = QtWidgets.QLabel("RPM을 입력하고 데이터 수신 후 '개수 추정'을 누르세요. (부품 수 후보)")
    infer_result.setWordWrap(True)
    options_layout.addWidget(infer_result)
    infer_current = {'candidates': [], 'axis': None, 'device': None, 'nyquist': 0.0}
    infer_markers = []

    # PIEZO 수동 G RMS 기준 보정.
    #
    # ADXL343(MEMS)는 Full Resolution 256 LSB/g를 사용하여
    # 자동으로 Raw=g / FFT=g RMS로 변환하므로 수동 보정 UI를 사용하지 않는다.
    calibration_status = None
    calibration_btn = None
    calibration_reset_btn = None
    calibration_reset_all_btn = None
    calibration_axis_combo = None
    reference_g_rms_edit = None

    if mode == "PIEZO":
        cal_row = QtWidgets.QHBoxLayout()
        options_layout.addLayout(cal_row)

        cal_row.addWidget(
            QtWidgets.QLabel("시험기 기준 진동 (RMS)")
        )

        reference_g_rms_edit = QtWidgets.QDoubleSpinBox()
        reference_g_rms_edit.setDecimals(4)
        reference_g_rms_edit.setRange(0.0001, 1000.0)
        reference_g_rms_edit.setSingleStep(0.1)
        reference_g_rms_edit.setValue(1.0)
        reference_g_rms_edit.setSuffix(" g")
        reference_g_rms_edit.setMaximumWidth(110)
        cal_row.addWidget(reference_g_rms_edit)

        calibration_btn = QtWidgets.QPushButton(
            "현재 패킷 기준으로 보정"
        )

        calibration_reset_btn = QtWidgets.QPushButton(
            "보정 해제"
        )

        calibration_status = QtWidgets.QLabel(
            "보정: 미적용 (Raw=count / FFT=count Peak)"
        )

        cal_row.addWidget(calibration_btn)
        cal_row.addWidget(calibration_reset_btn)
        if calibration_reset_all_btn is not None:
            cal_row.addWidget(calibration_reset_all_btn)
        cal_row.addSpacing(10)
        cal_row.addWidget(calibration_status)
        cal_row.addStretch(1)

    # PIEZO Raw 표시용 사용자 0점 보정.
    #
    # 예:
    #   입력값 -18500 count
    #   원본 -18500 ± 진동값
    #   표시      0 ± 진동값
    #
    # 이것은 물리단위(g) 보정이 아니며 FFT 계산에도 적용하지 않는다.
    piezo_zero_mode_combo = None
    piezo_zero_edit = None
    piezo_zero_apply_btn = None
    piezo_zero_reset_btn = None
    piezo_zero_status = None
    piezo_auto_mean_label = None
    piezo_copy_auto_btn = None

    if mode == "PIEZO":
        zero_row = QtWidgets.QHBoxLayout()
        options_layout.addLayout(zero_row)

        zero_row.addWidget(QtWidgets.QLabel("PIEZO RAW 0점 모드"))
        piezo_zero_mode_combo = QtWidgets.QComboBox()
        piezo_zero_mode_combo.addItem("OFF (자동 0점 제거 안 함)", "off")
        # 초기 내부 기본 모드(auto)와 화면 선택이 일치하도록 설정.
        piezo_zero_mode_combo.setCurrentIndex(piezo_zero_mode_combo.findData("off"))
        zero_row.addWidget(piezo_zero_mode_combo)
        zero_row.addWidget(QtWidgets.QLabel("수동 기준 Count"))

        piezo_zero_edit = QtWidgets.QDoubleSpinBox()
        piezo_zero_edit.setDecimals(3)
        piezo_zero_edit.setRange(-1000000000.0, 1000000000.0)
        piezo_zero_edit.setSingleStep(1.0)
        piezo_zero_edit.setValue(PIEZO_DEFAULT_ZERO_COUNT)
        piezo_zero_edit.setMaximumWidth(150)
        zero_row.addWidget(piezo_zero_edit)

        piezo_zero_apply_btn = QtWidgets.QPushButton(
            "0점 적용"
        )
        piezo_zero_reset_btn = QtWidgets.QPushButton(
            "0점 제거 OFF"
        )
        piezo_zero_status = QtWidgets.QLabel(
            "RAW 그래프만 평균=0, 수신/저장 데이터 무변경"
        )

        zero_row.addWidget(piezo_zero_apply_btn)
        zero_row.addWidget(piezo_zero_reset_btn)
        zero_row.addSpacing(10)
        zero_row.addWidget(piezo_zero_status)
        zero_row.addStretch(1)
        for obsolete in (piezo_zero_mode_combo, piezo_zero_edit, piezo_zero_apply_btn, piezo_zero_reset_btn):
            obsolete.setVisible(False)

        # 실제로 수신한 최신 패킷의 평균 Count를 0점 모드와 관계없이 표시.
        auto_mean_row = QtWidgets.QHBoxLayout()
        options_layout.addLayout(auto_mean_row)
        auto_mean_row.addWidget(QtWidgets.QLabel("원본 ADC 평균(그래프 표시에서만 차감)"))
        piezo_auto_mean_label = QtWidgets.QLabel("수신 대기 중")
        piezo_auto_mean_label.setMinimumWidth(200)
        auto_mean_row.addWidget(piezo_auto_mean_label)
        piezo_copy_auto_btn = QtWidgets.QPushButton("자동값 → 수동 입력")
        piezo_copy_auto_btn.setToolTip("현재 패킷 평균 Count를 수동 입력란에 복사합니다. 고정하려면 '0점 적용'을 누르세요.")
        piezo_copy_auto_btn.setEnabled(False)
        auto_mean_row.addWidget(piezo_copy_auto_btn)
        piezo_copy_auto_btn.setVisible(False)
        auto_mean_row.addWidget(QtWidgets.QLabel("RAW 표시 평균 제거 / RMS 평균 제거는 별도 계산"))
        auto_mean_row.addStretch(1)

    # Y range control
    yrow = QtWidgets.QHBoxLayout()
    options_layout.addLayout(yrow)

    def make_edit():
        e = QtWidgets.QLineEdit()
        e.setMaximumWidth(90)
        return e

    raw_y_label = QtWidgets.QLabel("Raw Y")
    yrow.addWidget(raw_y_label)
    raw_min = make_edit(); raw_min.setPlaceholderText("min")
    raw_max = make_edit(); raw_max.setPlaceholderText("max")
    yrow.addWidget(raw_min); yrow.addWidget(QtWidgets.QLabel("~")); yrow.addWidget(raw_max)
    yrow.addSpacing(12)
    fft_y_label = QtWidgets.QLabel("FFT Y")
    yrow.addWidget(fft_y_label)
    fft_min = make_edit(); fft_min.setPlaceholderText("min")
    fft_max = make_edit(); fft_max.setPlaceholderText("max")
    yrow.addWidget(fft_min); yrow.addWidget(QtWidgets.QLabel("~")); yrow.addWidget(fft_max)
    apply_y_btn = QtWidgets.QPushButton("Y축 적용/고정")
    auto_y_btn = QtWidgets.QPushButton("Y축 자동")
    yrow.addWidget(apply_y_btn); yrow.addWidget(auto_y_btn)
    yrow.addStretch(1)

    options_layout.addStretch(1)
    graphics = pg.GraphicsLayoutWidget()
    graphics.setMinimumHeight(220)
    graph_splitter.addWidget(graphics)
    graph_splitter.setStretchFactor(0, 0)
    graph_splitter.setStretchFactor(1, 1)
    graph_splitter.setSizes([270, 560])

    # RAW symmetric logarithmic display. This transformation never touches sensor arrays.
    raw_symlog_state = {'enabled': False, 'threshold': 1.0, 'manual': False}

    def raw_symlog_y(values):
        data = np.asarray(values, dtype=np.float64)
        threshold = raw_symlog_state['threshold']
        return np.sign(data) * np.log10(1.0 + np.abs(data) / threshold)

    def raw_symlog_inverse(values):
        data = np.asarray(values, dtype=np.float64)
        threshold = raw_symlog_state['threshold']
        return np.sign(data) * threshold * (np.power(10.0, np.abs(data)) - 1.0)

    class RawSymLogAxis(pg.AxisItem):
        def tickStrings(self, values, scale, spacing):
            if not raw_symlog_state['enabled']:
                return super().tickStrings(values, scale, spacing)
            originals = raw_symlog_inverse(values)
            return [f'{x:.4g}' if np.isfinite(x) else '' for x in originals]

    raw_plot = graphics.addPlot(row=0, col=0, viewBox=AxisHoldWheelViewBox(),
                                axisItems={'left': RawSymLogAxis(orientation='left')})
    fft_plot = graphics.addPlot(row=1, col=0, viewBox=AxisHoldWheelViewBox())

    # FFT axis modes are display-only (the original arrays and calculations are untouched).
    raw_symlog_check = QtWidgets.QCheckBox('RAW Y 대칭로그 (SymLog)')
    raw_symlog_threshold = QtWidgets.QLineEdit()
    raw_symlog_threshold.setMaximumWidth(90)
    raw_symlog_threshold.setPlaceholderText('자동')
    raw_symlog_threshold.setToolTip('빈칸: 데이터 최대 절댓값의 1% (자동). 양수 입력: 선형 전환 기준값')
    fft_log_x_check = QtWidgets.QCheckBox('FFT X 로그')
    fft_log_y_check = QtWidgets.QCheckBox('FFT Y 로그')
    fft_log_x_check.setToolTip('0 Hz는 로그 X축에서 표시되지 않습니다. FFT 데이터 자체는 유지됩니다.')
    fft_log_y_check.setToolTip('0 이하 진폭은 로그 Y축에서 표시되지 않습니다. FFT/RMS 계산은 그대로 유지됩니다.')
    fft_log_row = QtWidgets.QHBoxLayout()
    layout.insertLayout(layout.indexOf(graphics), fft_log_row)
    fft_log_row.addWidget(raw_symlog_check)
    fft_log_row.addWidget(QtWidgets.QLabel('기준값'))
    fft_log_row.addWidget(raw_symlog_threshold)
    fft_log_row.addSpacing(15)
    fft_log_row.addWidget(QtWidgets.QLabel('FFT 축 스케일'))
    fft_log_row.addWidget(fft_log_x_check)
    fft_log_row.addWidget(fft_log_y_check)
    fft_log_row.addWidget(QtWidgets.QLabel('(그래프 표시만 변경 · 원본/FFT/RMS 불변)'))
    fft_log_row.addStretch(1)

    raw_plot.setLabel("bottom", "Time", units="s")
    raw_plot.setLabel("left", "int16")
    fft_plot.setLabel("bottom", "Frequency", units="Hz")
    fft_plot.setLabel("left", "Magnitude")
    raw_plot.showGrid(x=True, y=True, alpha=0.25)
    fft_plot.showGrid(x=True, y=True, alpha=0.25)

    # Dominant FFT-bin period is an estimate, not a measured phase-aligned cycle.
    cycle_region = None
    if mode == "PIEZO":
        cycle_region = pg.LinearRegionItem(
            values=(0, 0), movable=False,
            brush=pg.mkBrush(80, 140, 220, 38),
            pen=pg.mkPen(75, 125, 205, 130),
        )
        cycle_region.setZValue(-10)
        raw_plot.addItem(cycle_region, ignoreBounds=True)
        cycle_region.hide()

    # 핵심: 화면 밖 sample은 그리지 않고, 많은 sample은 min/max peak 방식으로 축소.
    raw_plot.setClipToView(True)
    fft_plot.setClipToView(True)
    raw_plot.setDownsampling(auto=True, mode="peak")
    fft_plot.setDownsampling(auto=True, mode="peak")

    # mouse drag = pan / wheel = zoom (PyQtGraph 기본 ViewBox 동작)
    raw_plot.getViewBox().setMouseEnabled(x=True, y=True)
    fft_plot.getViewBox().setMouseEnabled(x=True, y=True)

    colors = {
        "X": (31, 119, 180),
        "Y": (44, 160, 44),
        "Z": (214, 39, 40),
        "PIEZO": (214, 39, 40),
        "SRSS": (148, 103, 189),  # #9467BD
    }

    if mode == "ADXL":
        raw_plot.addLegend()
        fft_plot.addLegend()

    axes = ("X", "Y", "Z") if mode == "ADXL" else ("Z",)
    raw_curves = {}
    fft_curves = {}
    for axis in axes:
        color = colors[axis] if mode == "ADXL" else colors["PIEZO"]
        pen = pg.mkPen(color=color, width=1)
        raw_curves[axis] = raw_plot.plot([], [], pen=pen, name=axis)
        fft_curves[axis] = fft_plot.plot([], [], pen=pen, name=axis)

    # SRSS is an overall RMS reference line, not an independent RAW series.
    if mode == "ADXL":
        fft_curves["gN"] = fft_plot.plot([], [], pen=pg.mkPen(color=(23,162,184), width=1.4), name="gN")
        srss_pen=pg.mkPen(color=colors["SRSS"], width=1.7)
        srss_pen.setDashPattern([5.0,3.0])
        fft_curves["SRSS"] = fft_plot.plot([], [], pen=srss_pen, name="SRSS")
        limit_pen=pg.mkPen(color=(180,83,9),width=1.5)
        limit_pen.setDashPattern([5.0,3.0])
        srss_limit_curve=fft_plot.plot([], [], pen=limit_pen, name="SRSS 한계 (mm/s RMS)")

    # 마우스 좌클릭: 값 고정, 다른 점 클릭: 이동, 빈 곳 클릭: 고정 해제.
    # 커서가 가리키는 실제 원본 샘플/FFT bin의 값을 툴팁으로 표시한다.
    cursor_arrays = {"raw": {}, "fft": {}}
    cursor_piezo_conversion = {"raw": {}, "fft": {}}
    cursor_tip_items = {}
    cursor_markers = {}
    for cursor_kind, cursor_plot in (("raw", raw_plot), ("fft", fft_plot)):
        tip = pg.TextItem(anchor=(0, 1), border=pg.mkPen(80, 80, 80),
                          fill=pg.mkBrush(255, 255, 235, 235), color=(30, 30, 30))
        tip.setZValue(1000)
        tip.hide()
        cursor_plot.addItem(tip, ignoreBounds=True)
        dot = pg.ScatterPlotItem(size=9, brush=pg.mkBrush(255, 135, 0),
                                 pen=pg.mkPen(60, 60, 60))
        dot.setZValue(1001)
        dot.hide()
        cursor_plot.addItem(dot, ignoreBounds=True)
        cursor_tip_items[cursor_kind] = tip
        cursor_markers[cursor_kind] = dot

    def cursor_units(cursor_kind):
        if mode == "ADXL":
            if cursor_kind == "raw":
                return mems_raw_display_unit
            return {"g_rms": "g RMS", "g_peak": "g Peak", "count_peak": "Count Peak",
                    "velocity_rms": "mm/s RMS"}[mems_fft_unit_combo.currentData()]
        display_mode = str(piezo_unit_combo.currentData())
        if display_mode == "voltage":
            return "mV" if cursor_kind == "raw" else "mV Peak"
        if display_mode == "velocity_rms":
            return "g" if cursor_kind == "raw" else "mm/s RMS"
        if display_mode in ("g_spec", "g_pp"):
            return "g" if cursor_kind == "raw" else "g RMS"
        if display_mode == "g" and is_calibration_complete(current_device):
            return "g" if cursor_kind == "raw" else "g RMS"
        return "count (평균제거)" if cursor_kind == "raw" else "count Peak"

    # V020: 클릭한 주파수/시간의 위치를 고정한다. 수신 때마다 같은 축/동일 X의 최신 Y를 추적.
    cursor_pins = {"raw": None, "fft": None}  # {axis, x} / None

    def find_graph_point(kind, chart, scene_pos, radius_pixels=35):
        if not chart.sceneBoundingRect().contains(scene_pos):
            return None
        view_pos = chart.vb.mapSceneToView(scene_pos)
        raw_symlog = kind == 'raw' and raw_symlog_state['enabled']
        is_fft_log = kind == 'fft'
        log_x = is_fft_log and fft_log_x_check.isChecked()
        log_y = is_fft_log and fft_log_y_check.isChecked()
        # PyQtGraph PlotItem.setLogMode transforms the *display coordinates*
        # into log10 space; cursor arrays remain in original physical units.
        probe_x = 10.0 ** view_pos.x() if log_x else view_pos.x()
        best = None
        for axis, (xs, ys) in cursor_arrays[kind].items():
            if mode == "ADXL" and not axis_checks[axis].isChecked():
                continue
            if len(xs) == 0 or len(ys) == 0:
                continue
            i = int(np.clip(np.searchsorted(xs, probe_x), 0, len(xs) - 1))
            for j in ((i - 1, i) if i > 0 else (i,)):
                sx, sy = float(xs[j]), float(ys[j])
                if not (np.isfinite(sx) and np.isfinite(sy)):
                    continue
                if (log_x and sx <= 0) or (log_y and sy <= 0):
                    continue
                visual_x = np.log10(sx) if log_x else sx
                visual_y = float(raw_symlog_y(sy)) if raw_symlog else (np.log10(sy) if log_y else sy)
                spot = chart.vb.mapViewToScene(QtCore.QPointF(visual_x, visual_y))
                dist = (spot.x() - scene_pos.x()) ** 2 + (spot.y() - scene_pos.y()) ** 2
                if best is None or dist < best[0]:
                    best = (dist, axis, sx, sy)
        return best if best is not None and best[0] <= radius_pixels ** 2 else None

    def show_graph_cursor(kind, axis, x, y, pinned=False):
        tip = cursor_tip_items[kind]
        dot = cursor_markers[kind]
        yunit = cursor_units(kind)
        if mode == "ADXL" and kind == "fft" and axis == "SRSS":
            yunit = {"count_peak":"Count RMS", "velocity_rms":"mm/s RMS"}.get(mems_fft_unit_combo.currentData(),"g RMS")
        shown_y = y * 1000.0 if yunit.startswith("mV") else y
        axis_text = f"{axis} | " if mode == "ADXL" else ""
        xunit = "s" if kind == "raw" else "Hz"
        prefix = "[고정] " if pinned else ""
        tip.setText(f"{prefix}{axis_text}X={x:.6g} {xunit} / Y={shown_y:.6g} {yunit}")
        log_x = kind == 'fft' and fft_log_x_check.isChecked()
        log_y = kind == 'fft' and fft_log_y_check.isChecked()
        if (log_x and x<=0) or (log_y and y<=0):
            tip.hide(); dot.hide(); return
        visual_x = np.log10(x) if log_x else x
        visual_y = float(raw_symlog_y(y)) if kind == 'raw' and raw_symlog_state['enabled'] else (np.log10(y) if log_y else y)
        tip.setPos(visual_x, visual_y)
        dot.setData([visual_x], [visual_y])
        tip.show()
        dot.show()

    def refresh_pinned_cursors():
        for kind in ("raw", "fft"):
            pin = cursor_pins[kind]
            if pin is None:
                continue
            axis = pin["axis"]
            data = cursor_arrays[kind].get(axis)
            if data is None or not len(data[0]) or not len(data[1]):
                cursor_tip_items[kind].hide()
                cursor_markers[kind].hide()
                continue
            if mode == "ADXL" and not axis_checks[axis].isChecked():
                cursor_tip_items[kind].hide()
                cursor_markers[kind].hide()
                continue
            xs, ys = data
            idx = int(np.argmin(np.abs(xs - pin["x"])))
            x, y = float(xs[idx]), float(ys[idx])
            if np.isfinite(x) and np.isfinite(y):
                show_graph_cursor(kind, axis, x, y, pinned=True)

    def hover_graph(event):
        scene_pos = event[0] if isinstance(event, tuple) else event
        for kind, chart in (("raw", raw_plot), ("fft", fft_plot)):
            if cursor_pins[kind] is not None:
                continue  # 선택 고정 중에는 마우스를 움직여도 값이 사라지지 않는다.
            best = find_graph_point(kind, chart, scene_pos)
            if best is None:
                cursor_tip_items[kind].hide()
                cursor_markers[kind].hide()
            else:
                _, axis, x, y = best
                show_graph_cursor(kind, axis, x, y)

    def click_graph(event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        scene_pos = event.scenePos()
        for kind, chart in (("raw", raw_plot), ("fft", fft_plot)):
            if not chart.sceneBoundingRect().contains(scene_pos):
                continue
            best = find_graph_point(kind, chart, scene_pos, radius_pixels=15)
            if best is None:
                cursor_pins[kind] = None
                cursor_tip_items[kind].hide()
                cursor_markers[kind].hide()
            else:
                _, axis, x, y = best
                cursor_pins[kind] = {"axis": axis, "x": x}
                show_graph_cursor(kind, axis, x, y, pinned=True)
            break

    hover_proxy = pg.SignalProxy(graphics.scene().sigMouseMoved, rateLimit=40,
                                 slot=hover_graph)
    graphics.scene().sigMouseClicked.connect(click_graph)

    def apply_fft_log_axis():
        xlog=fft_log_x_check.isChecked()
        ylog=fft_log_y_check.isChecked()
        # A saved manual Y range can be invalid in log mode; release only
        # the FFT Y lock rather than modifying any data or RAW settings.
        nonlocal fft_fixed
        if ylog and fft_fixed is not None and fft_fixed[0] <= 0:
            fft_fixed = None
            fft_min.clear(); fft_max.clear()
            fft_plot.getViewBox().setMouseEnabled(x=True,y=True)
            fft_plot.enableAutoRange(axis='y',enable=True)
        fft_plot.setLogMode(x=xlog,y=ylog)
        if xlog:
            # setXRange uses ViewBox coordinates (log10 in this mode).
            # Positive FFT frequencies are necessarily >= one FFT bin.
            visible_freqs=[]
            for xs,ys in cursor_arrays['fft'].values():
                xs=np.asarray(xs,dtype=float)
                positive=xs[np.isfinite(xs)&(xs>0)]
                if positive.size:visible_freqs.append((float(np.min(positive)),float(np.max(positive))))
            if visible_freqs:
                low=min(pair[0] for pair in visible_freqs)
                high=max(pair[1] for pair in visible_freqs)
                if high>low:fft_plot.setXRange(np.log10(low),np.log10(high),padding=0)
        else:
            # Restore the complete frequency axis in linear mode.
            if cursor_arrays['fft']:
                high=max(float(np.max(xs)) for xs,ys in cursor_arrays['fft'].values() if len(xs))
                fft_plot.setXRange(0,high,padding=0)
        if fft_fixed is None:
            fft_plot.enableAutoRange(axis='y',enable=True)
        elif ylog and fft_fixed[0]>0:
            fft_plot.setYRange(np.log10(fft_fixed[0]),np.log10(fft_fixed[1]),padding=0)
        elif not ylog:
            fft_plot.setYRange(*fft_fixed,padding=0)
        refresh_pinned_cursors()

    fft_log_x_check.toggled.connect(lambda _checked:apply_fft_log_axis())
    fft_log_y_check.toggled.connect(lambda _checked:apply_fft_log_axis())

    def apply_raw_symlog_axis():
        # Preserve original-unit cursor arrays, transform only data passed to the plot.
        raw_symlog_state['enabled'] = raw_symlog_check.isChecked()
        manual_text = raw_symlog_threshold.text().strip()
        if manual_text:
            try:
                threshold = float(manual_text)
                if not np.isfinite(threshold) or threshold <= 0:
                    raise ValueError('양수만 허용')
            except ValueError:
                QtWidgets.QMessageBox.warning(window, 'RAW SymLog', '기준값에는 0보다 큰 숫자를 입력하거나 자동(빈칸)으로 두세요.')
                return
            raw_symlog_state['threshold'] = threshold
            raw_symlog_state['manual'] = True
        else:
            raw_symlog_state['manual'] = False
            available = [np.asarray(ys, dtype=np.float64) for xs, ys in cursor_arrays['raw'].values() if len(ys)]
            peak = max((float(np.max(np.abs(v[np.isfinite(v)]))) for v in available if np.any(np.isfinite(v))), default=0.0)
            raw_symlog_state['threshold'] = max(peak / 100.0, 1e-12)
        for axis, curve in raw_curves.items():
            values = cursor_arrays['raw'].get(axis)
            if values is None:
                continue
            xs, ys = values
            curve.setData(xs, raw_symlog_y(ys) if raw_symlog_state['enabled'] else ys)
        if raw_fixed is not None:
            bounds = raw_symlog_y(raw_fixed) if raw_symlog_state['enabled'] else raw_fixed
            raw_plot.setYRange(float(bounds[0]), float(bounds[1]), padding=0)
        else:
            raw_plot.enableAutoRange(axis='y', enable=True)
        raw_plot.getAxis('left').picture = None
        raw_plot.getAxis('left').update()
        refresh_pinned_cursors()

    raw_symlog_check.toggled.connect(lambda _: apply_raw_symlog_axis())
    raw_symlog_threshold.editingFinished.connect(apply_raw_symlog_axis)

    current_device = ""
    current_snapshot = None
    current_signature = None
    raw_fixed = None
    fft_fixed = None

    # key=(device_id, axis)
    # value={
    #   "counts_per_g": ...,
    #   "raw_mean_ref": ...,
    #   "ref_g_rms": ...
    # }
    #
    # PIEZO: Z
    # MEMS(ADXL): X / Y / Z 각각 별도 보정
    calibrations = {}

    # PIEZO 표시용 수동 0점.
    # key=device_id, value=raw count offset
    #
    # FFT에는 사용하지 않는다.
    piezo_zero_offsets = {}
    # 기본은 자동 0점. 수동 선택 시에만 -14400 또는 사용자가 입력한 값 사용.
    piezo_zero_mode_by_device = {}
    # device_id -> 가장 최근 정상 PIEZO 패킷의 원본 평균 Count
    piezo_latest_packet_mean_by_device = {}

    def refresh_piezo_auto_mean(device_id=None):
        if mode != "PIEZO" or piezo_auto_mean_label is None:
            return
        device_id = str(device_id or current_device or "").strip()
        mean = piezo_latest_packet_mean_by_device.get(device_id)
        valid = mean is not None and np.isfinite(mean)
        piezo_auto_mean_label.setText(
            f"{mean:.3f} Count (최신 패킷 평균)" if valid else "수신 대기 중"
        )
        piezo_copy_auto_btn.setEnabled(bool(valid))

    def get_piezo_zero_mode(device_id):
        return piezo_zero_mode_by_device.get(str(device_id or "").strip(), "off")


    def get_piezo_zero_offset(device_id):
        if mode != "PIEZO":
            return None

        device_id = str(device_id or "").strip()
        if not device_id:
            return None

        zero_mode = get_piezo_zero_mode(device_id)
        if zero_mode == "auto":
            return None  # 패킷 평균을 RAW 표시용 0점으로 사용
        if zero_mode == "off":
            return 0.0  # 보정 없음: ADC Count에서 어떤 기준값도 빼지 않음

        value = piezo_zero_offsets.get(device_id, PIEZO_DEFAULT_ZERO_COUNT)

        try:
            value = float(value)
        except Exception:
            return None

        return value if np.isfinite(value) else None

    def refresh_piezo_zero_status(device_id=None):
        if mode != "PIEZO" or piezo_zero_status is None:
            return

        device_id = str(
            device_id
            or current_device
            or (
                current_snapshot.get("device_id", "")
                if current_snapshot
                else ""
            )
        ).strip()

        zero_mode = get_piezo_zero_mode(device_id)
        if zero_mode == "auto":
            piezo_zero_status.setText("자동 0점: 현재 패킷 평균 (원본 Count에는 미적용)")
        elif zero_mode == "off":
            piezo_zero_status.setText("원본 ADC 불변 / RAW 그래프만 패킷 평균 제거")
        else:
            offset = get_piezo_zero_offset(device_id)
            piezo_zero_status.setText(
                f"수동 0점: {offset:.6g} count → 표시 0 (원본 Count에는 미적용)"
            )

    def calibration_axes():
        return ("Z",)

    def is_axis_calibrated(device_id, axis):
        cal = calibrations.get((str(device_id), str(axis)))
        if cal is None:
            return False
        try:
            value = float(cal.get("counts_per_g", 0.0))
            return np.isfinite(value) and value > 0
        except Exception:
            return False

    def is_calibration_complete(device_id, records=None):
        if not device_id:
            return False

        required = list(calibration_axes())

        # 실제 snapshot에 없는 축은 완료 판정에서 제외할 수 있지만,
        # ADXL은 통상 X/Y/Z 세 축 모두가 와야 하므로 세 축 기준으로 유지한다.
        if mode == "PIEZO":
            required = ["Z"]

        return all(
            is_axis_calibrated(device_id, axis)
            for axis in required
        )

    def refresh_calibration_status(device_id=None):
        if calibration_status is None:
            return

        device_id = str(
            device_id
            or current_device
            or (
                current_snapshot.get("device_id", "")
                if current_snapshot
                else ""
            )
        )

        if not device_id:
            calibration_status.setText(
                "보정: 미적용 (Raw=count / FFT=count Peak)"
            )
            return

        if mode == "PIEZO":
            cal = calibrations.get((device_id, "Z"))
            if not is_axis_calibrated(device_id, "Z"):
                calibration_status.setText(
                    "PIEZO Z: 미보정 (Raw=count / FFT=count Peak)"
                )
                return

            calibration_status.setText(
                "PIEZO Z 보정 완료 | "
                f"Ref={float(cal['ref_g_rms']):.6g} g RMS | "
                f"Scale={float(cal['counts_per_g']):.6g} count/g"
            )
            return

        return

    def parse_pair(a, b, label):
        ta = a.text().strip(); tb = b.text().strip()
        if not ta and not tb:
            return None
        if not ta or not tb:
            raise ValueError(f"{label}: 최소/최대를 모두 입력하거나 모두 비워주세요.")
        lo = float(ta); hi = float(tb)
        if not np.isfinite(lo) or not np.isfinite(hi) or lo >= hi:
            raise ValueError(f"{label}: 올바른 최소/최대값을 입력하세요.")
        return lo, hi

    def update_y_range_unit_labels():
        # PyQtGraph 전압 축은 자동으로 mV로 표시될 수 있으므로
        # PIEZO 전압 모드의 수동 Y축 입력 역시 mV 단위로 받는다.
        is_mv = (mode == "PIEZO" and piezo_unit_combo is not None
                 and piezo_unit_combo.currentData() == "voltage")
        velocity = (mode == "PIEZO" and piezo_unit_combo is not None
                    and piezo_unit_combo.currentData() == "velocity_rms")
        acceleration = (mode == "PIEZO" and piezo_unit_combo is not None
                        and piezo_unit_combo.currentData() == "g_spec")
        raw_y_label.setText("Raw Y (mV)" if is_mv else "Raw Y (g)" if (velocity or acceleration) else "Raw Y")
        mems_velocity = (mode == "ADXL"
                         and mems_fft_unit_combo.currentData() == "velocity_rms")
        fft_y_label.setText("FFT Y (mV Peak)" if is_mv else "FFT Y (mm/s RMS)" if (velocity or mems_velocity) else "FFT Y (g RMS)" if acceleration else "FFT Y")
        if mode == "ADXL":
            raw_y_label.setText("Raw Y (" + mems_raw_display_unit + ")")
            fft_y_label.setText("FFT Y (" + cursor_units("fft") + ")")
        hint = "mV 입력" if is_mv else "min"
        raw_min.setPlaceholderText(hint)
        fft_min.setPlaceholderText(hint)
        raw_max.setPlaceholderText("mV 입력" if is_mv else "max")
        fft_max.setPlaceholderText("mV 입력" if is_mv else "max")

    update_y_range_unit_labels()

    def apply_y():
        nonlocal raw_fixed, fft_fixed
        try:
            raw_fixed = parse_pair(raw_min, raw_max, "Raw Y축")
            fft_fixed = parse_pair(fft_min, fft_max, "FFT Y축")
            if mode == "PIEZO" and piezo_unit_combo is not None and piezo_unit_combo.currentData() == "voltage":
                # 입력: mV, 내부 PyQtGraph 데이터 및 setYRange: V
                if raw_fixed is not None:
                    raw_fixed = (raw_fixed[0] / 1000.0, raw_fixed[1] / 1000.0)
                if fft_fixed is not None:
                    fft_fixed = (fft_fixed[0] / 1000.0, fft_fixed[1] / 1000.0)

            # Raw Y축
            if raw_fixed is None:
                raw_plot.enableAutoRange(axis="y", enable=True)
                # 자동 Y축이면 기존 PyQtGraph 동작처럼 Y 확대/축소 허용
                raw_plot.getViewBox().setMouseEnabled(x=True, y=True)
            else:
                raw_plot.enableAutoRange(axis="y", enable=False)
                bounds = raw_symlog_y(raw_fixed) if raw_symlog_state['enabled'] else raw_fixed
                raw_plot.setYRange(float(bounds[0]), float(bounds[1]), padding=0)
                # 수동 Y축 적용 시 Y 범위를 완전히 잠근다.
                # 마우스 휠/드래그는 X축에만 적용된다.
                raw_plot.getViewBox().setMouseEnabled(x=True, y=False)

            # FFT Y축
            if fft_fixed is None:
                fft_plot.enableAutoRange(axis="y", enable=True)
                fft_plot.getViewBox().setMouseEnabled(x=True, y=True)
            else:
                fft_plot.enableAutoRange(axis="y", enable=False)
                if fft_log_y_check.isChecked():
                    if fft_fixed[0] <= 0:
                        raise ValueError('FFT 로그 Y축은 0보다 큰 min/max가 필요합니다.')
                    fft_plot.setYRange(np.log10(fft_fixed[0]),np.log10(fft_fixed[1]),padding=0)
                else:
                    fft_plot.setYRange(*fft_fixed, padding=0)
                fft_plot.getViewBox().setMouseEnabled(x=True, y=False)

        except Exception as e:
            QtWidgets.QMessageBox.warning(window, "Y축 설정", str(e))

    def auto_y():
        nonlocal raw_fixed, fft_fixed
        raw_fixed = None
        fft_fixed = None

        for e in (raw_min, raw_max, fft_min, fft_max):
            e.clear()

        # 자동 모드로 복귀하면 Y축 확대/이동도 다시 허용
        raw_plot.getViewBox().setMouseEnabled(x=True, y=True)
        fft_plot.getViewBox().setMouseEnabled(x=True, y=True)

        raw_plot.enableAutoRange(axis="y", enable=True)
        fft_plot.enableAutoRange(axis="y", enable=True)

    apply_y_btn.clicked.connect(apply_y)
    auto_y_btn.clicked.connect(auto_y)

    def load_selected_device_voltage(device_id):
        if mode != "PIEZO" or piezo_ref_voltage is None:
            return
        device_id = str(device_id or "").strip()
        if not device_id:
            return
        # 다른 프로세스에서 신규 장치가 등록됐을 수 있으므로 항상 디스크를 다시 확인.
        device_voltages.update(load_piezo_device_voltages(device_voltage_path))
        if device_id not in device_voltages:
            device_voltages[device_id] = float(PIEZO_ADC_VMAX)
            try:
                save_piezo_device_voltages(device_voltages, device_voltage_path)
            except OSError as exc:
                QtWidgets.QMessageBox.warning(window, "전압 설정 저장 실패",
                    f"저장 경로: {device_voltage_path}\n\n{exc}")
        target = float(device_voltages.get(device_id, PIEZO_ADC_VMAX))
        # 디바이스 전환 시 기존 디바이스의 전압으로 다시 저장되는 것을 방지한다.
        piezo_ref_voltage.blockSignals(True)
        try:
            piezo_ref_voltage.setValue(target)
        finally:
            piezo_ref_voltage.blockSignals(False)
        # 이전 디바이스에서 고정한 Y축 범위는 새 장치의 단위에 맞지 않을 수 있다.
        auto_y()
        if current_snapshot is not None and str(current_snapshot.get('device_id', '')) == str(device_id):
            apply_snapshot(current_snapshot, force_full=False)

    def device_voltage_changed(_value=None):
        if mode != "PIEZO" or piezo_ref_voltage is None:
            return
        device_id = combo.currentText().strip()
        if not device_id:
            return
        # 다른 프로세스에서 추가한 센서 설정을 덮어쓰지 않도록 병합.
        device_voltages.update(load_piezo_device_voltages(device_voltage_path))
        device_voltages[device_id] = float(piezo_ref_voltage.value())
        try:
            save_piezo_device_voltages(device_voltages, device_voltage_path)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(
                window, "디바이스별 전압 저장 실패",
                f"설정 파일을 저장할 수 없습니다:\n{device_voltage_path}\n\n{exc}")

    def send_selection():
        nonlocal current_device
        previous_device = current_device
        current_device = combo.currentText().strip()
        if mode == "ADXL" and previous_device != current_device:
            srss_limit_entry.setText(f"{srss_limit_get(current_device):.9g}")
        if previous_device != current_device:
            for cursor_kind in cursor_pins:
                cursor_pins[cursor_kind] = None
                cursor_tip_items[cursor_kind].hide()
                cursor_markers[cursor_kind].hide()
        try:
            load_selected_device_voltage(current_device)
        except Exception as exc:
            # 전압 설정 I/O 문제로 선택/그래프 콤보를 막지 않는다.
            print(f"[V018] PIEZO 전압 설정 선택 오류(그래프 진행): {exc}", file=sys.stderr)
        refresh_calibration_status(current_device)
        refresh_piezo_zero_status(current_device)
        refresh_piezo_auto_mean(current_device)

        if (
            mode == "PIEZO"
            and piezo_zero_edit is not None
        ):
            offset = piezo_zero_offsets.get(current_device, PIEZO_DEFAULT_ZERO_COUNT)
            piezo_zero_edit.setValue(offset)
            piezo_zero_mode_combo.blockSignals(True)
            chosen_mode = get_piezo_zero_mode(current_device)
            idx = piezo_zero_mode_combo.findData(chosen_mode)
            piezo_zero_mode_combo.setCurrentIndex(max(0, idx))
            piezo_zero_mode_combo.blockSignals(False)

        if current_device:
            try:
                command_queue.put_nowait(
                    {
                        "type": "select",
                        "device_id": current_device,
                    }
                )
            except Exception:
                pass

    combo.currentTextChanged.connect(lambda _t: send_selection())

    def selected_fft_window_size():
        try:
            data = fft_window_size_combo.currentData()
            if data is None:
                data = fft_window_size_combo.currentText()
            return int(data)
        except Exception:
            return int(configured_sample_count)

    def set_fft_window_size_options(
        sample_count,
        reset_to_sample_count=False,
    ):
        """
        메인 Sample Count에 맞춰 Window Size 선택지를 갱신한다.

        기본:
          Sample Count = Window Size = FFT N

        수동 선택:
          MIN_FFT_WINDOW_SIZE <= Window Size <= Sample Count
        """
        nonlocal configured_sample_count

        try:
            sample_count = int(sample_count)
        except Exception:
            return

        if sample_count <= 0:
            return

        configured_sample_count = sample_count
        options = allowed_fft_window_sizes(sample_count)

        old_value = selected_fft_window_size()

        fft_window_size_combo.blockSignals(True)
        fft_window_size_combo.clear()

        for value in options:
            fft_window_size_combo.addItem(
                str(value),
                int(value),
            )

        if (
            reset_to_sample_count
            or old_value not in options
        ):
            target = (
                sample_count
                if sample_count in options
                else max(options)
            )
        else:
            target = old_value

        index = fft_window_size_combo.findData(int(target))
        if index >= 0:
            fft_window_size_combo.setCurrentIndex(index)

        fft_window_size_combo.setEnabled(
            len(options) > 1
        )
        fft_window_size_combo.blockSignals(False)

    # 초기에는 기본 Sample Count = Window Size = FFT N.
    set_fft_window_size_options(
        configured_sample_count,
        reset_to_sample_count=True,
    )

    def edit_srss_limit():
        device=combo.currentText().strip()
        if not device:
            QtWidgets.QMessageBox.information(window,"SRSS 한계값","장치를 선택하세요.")
            return
        try:
            value=float(srss_limit_entry.text())
            srss_limit_save(device,value)
        except (OSError,ValueError,TypeError) as exc:
            QtWidgets.QMessageBox.warning(window,"SRSS 저장 실패",str(exc));return
        if current_snapshot is not None:
            apply_snapshot(current_snapshot,force_full=False)

    if mode == "ADXL":
        srss_limit_btn.clicked.connect(edit_srss_limit)
        srss_limit_entry.returnPressed.connect(edit_srss_limit)

    def apply_visibility():
        if mode != "ADXL":
            return
        for axis in ("X", "Y", "Z"):
            visible = axis_checks[axis].isChecked()
            raw_curves[axis].setVisible(visible)
            fft_curves[axis].setVisible(visible)
        fft_curves["gN"].setVisible(axis_checks["gN"].isChecked())
        fft_curves["SRSS"].setVisible(axis_checks["SRSS"].isChecked())

    for cb in axis_checks.values():
        cb.stateChanged.connect(lambda _state: apply_visibility())

    def open_fft_diagnostic():
        try:
            snapshot=current_snapshot or {}; result={}
            for axis, rec in snapshot.get('axes', {}).items():
                raw=np.asarray(rec.get('samples',[]),dtype=np.float64); fs=float(rec.get('sample_rate',0))
                if len(raw)<2 or fs<=0:continue
                segment,_=select_fft_segment(raw,selected_fft_window_size()); centered=segment-segment.mean()
                hp=float(hpf_cutoff_spin.value()); lp=fs/2 if lpf_auto_check.isChecked() else float(lpf_cutoff_spin.value()); nc=float(notch_center_spin.value()); nw=float(notch_width_spin.value())
                centered=spectral_filter(centered,fs,hpf=hpf_enabled_check.isChecked() and 0<hp<fs/2,hp_cutoff=hp,hp_order=int(hpf_order_spin.value()),lpf=fft_lpf_check.isChecked() and 0<lp<fs/2,lp_cutoff=lp,lp_order=int(lpf_order_spin.value()),notch=fft_notch_check.isChecked() and 0<nc<fs/2 and 0<nw<fs/2,notch_center=nc,notch_width=nw)
                name=normalize_fft_window_name(fft_window_combo.currentData() or fft_window_combo.currentText()); beta=float(kaiser_beta_spin.value())
                if scipy_mode:
                    from scipy import signal
                    names={'Rectangular':'boxcar','Hann':'hann','Hamming':'hamming','Blackman':'blackman','Blackman-Harris':'blackmanharris','Flat Top':'flattop','Bartlett':'bartlett'}
                    w=signal.windows.kaiser(len(segment),beta,sym=True) if name=='Kaiser' else signal.windows.get_window(names.get(name,'hann'),len(segment),fftbins=False)
                else:w=build_fft_window(len(segment),name,kaiser_beta=beta)
                result[axis]=dict(raw=raw.copy(),fs=fs,processed=centered*w,gain=float(w.sum()))
            show_fft_diagnostic_qt(window,result,str(snapshot.get('device_id',''))+' '+mode)
        except Exception as exc:QtWidgets.QMessageBox.warning(window,'FFT/IFFT 검증',str(exc))
    diagnostic_btn.clicked.connect(open_fft_diagnostic)

    def apply_snapshot(snapshot, force_full=False):
        nonlocal current_snapshot, current_signature, mems_raw_display_unit, raw_fixed
        if not snapshot:
            return
        current_snapshot = snapshot
        records = snapshot.get("axes", {})
        first = None
        first_axis_used = None
        display_fft_by_axis = {}
        mems_raw_counts = {}

        selected_fft_window = normalize_fft_window_name(
            fft_window_combo.currentData()
            or fft_window_combo.currentText()
            or "Blackman"
        )

        selected_kaiser_beta = float(
            kaiser_beta_spin.value()
        )

        selected_window_size = selected_fft_window_size()
        hp_on = hpf_enabled_check.isChecked()
        raw_hp_on = raw_hpf_enabled_check.isChecked()
        hp_cutoff = float(hpf_cutoff_spin.value())
        hp_order = int(hpf_order_spin.value())
        lp_on = fft_lpf_check.isChecked()
        raw_lp_on = raw_lpf_check.isChecked()
        lp_cutoff = float(lpf_cutoff_spin.value())
        lp_order = int(lpf_order_spin.value())
        nt_on = fft_notch_check.isChecked()
        raw_nt_on = raw_notch_check.isChecked()
        nt_center = float(notch_center_spin.value())
        nt_width = float(notch_width_spin.value())
        active_fs = next((float(r.get("sample_rate", 0)) for r in records.values() if float(r.get("sample_rate", 0)) > 0), float(configured_sample_rate))
        if lpf_auto_check.isChecked():
            if active_fs > 0:
                lpf_cutoff_spin.blockSignals(True)
                lpf_cutoff_spin.setValue(active_fs / 2)
                lpf_cutoff_spin.blockSignals(False)
                lpf_info.setText(f"자동 Fs/2 = {active_fs/2:g} Hz · 전 대역 통과")
            else:
                lpf_info.setText("자동 Fs/2 · 수신 Fs 대기")
        else:
            # Refresh status on every update, including valid manual input.
            # Otherwise the last invalid-cutoff message persists indefinitely.
            if not (lp_on or raw_lp_on):
                lpf_info.setText("LPF OFF · RAW/FFT 독립")
            elif active_fs <= 0:
                lpf_info.setText("LPF · 수신 Fs 대기")
            elif 0 < lp_cutoff < active_fs / 2:
                lpf_info.setText(f"LPF {lp_cutoff:g} Hz / Fs/2={active_fs/2:g} Hz | RAW={'ON' if raw_lp_on else 'OFF'} / FFT={'ON' if lp_on else 'OFF'}")
            else:
                lpf_info.setText(f"LPF 오류: 0 < 차단 주파수 < {active_fs/2:g} Hz (Fs={active_fs:g} Hz)")
        hpf_valid = (not (hp_on or raw_hp_on)
                     or (active_fs > 0 and hp_cutoff < active_fs / 2))
        hpf_info_label.setText(
            f"Butterworth {hp_order}차 / {hp_cutoff:g} Hz | "
            f"RAW={'ON' if raw_hp_on else 'OFF'} / FFT={'ON' if hp_on else 'OFF'}"
            + ("" if hpf_valid else " | 차단 주파수는 Fs/2 미만이어야 함")
        )

        snapshot_device = str(
            snapshot.get("device_id", current_device)
        )
        if infer_current['device'] and infer_current['device'] != snapshot_device:
            infer_current.update(candidates=[], axis=None, device=None, nyquist=0.0)
            for old_marker in list(infer_markers):
                fft_plot.removeItem(old_marker)
            infer_markers.clear()
            infer_result.setText("센서가 변경되었습니다. 개수 추정을 다시 실행하세요.")

        mems_correction = mems_correction_values if mode == "ADXL" else None
        # ADXL343:
        #   Full Resolution 고정으로 256 LSB/g 자동 변환.
        # PIEZO:
        #   사용자가 실제 기준값을 알고 수동 G 보정을 완료한 경우에만 g 변환.
        piezo_calibration_complete = (
            mode == "PIEZO"
            and is_calibration_complete(
                snapshot_device,
                records,
            )
        )

        # g 환산은 사용자가 MEMS 그래프에서 언제든 ON/OFF 가능.
        # 체크 ON : Raw count / 256 -> g, FFT count Peak / 256 / sqrt(2) -> g RMS
        # 체크 OFF: Raw count 그대로, FFT count Peak 그대로.
        mems_convert_to_g = (
            mode == "ADXL"
            and mems_fft_unit_combo.currentData() != "count_peak"
        )
        piezo_display_mode = (
            str(piezo_unit_combo.currentData()) if mode == "PIEZO" else ""
        )
        piezo_voltage_scale = (
            float(piezo_ref_voltage.value()) / float(piezo_adc_denominator.value())
            if mode == "PIEZO" else 1.0
        )
        physical_units_enabled = (
            mems_convert_to_g
            or (mode == "PIEZO" and piezo_display_mode == "g" and piezo_calibration_complete)
            or (mode == "PIEZO" and piezo_display_mode in ("g_spec", "velocity_rms"))
        )

        for axis in axes:
            rec = records.get(axis)
            if rec is None:
                raw_curves[axis].setData([], [])
                fft_curves[axis].setData([], [])
                continue

            samples = np.asarray(
                rec.get("samples", []),
                dtype=np.float64,
            )
            if mode == "PIEZO" and samples.size and np.all(np.isfinite(samples)):
                piezo_latest_packet_mean_by_device[snapshot_device] = float(np.mean(samples))
                if snapshot_device == str(current_device or snapshot_device).strip():
                    refresh_piezo_auto_mean(snapshot_device)
            fs = float(rec.get("sample_rate", 0.0))

            fft_samples, fft_segment_start = select_fft_segment(
                samples,
                selected_window_size,
            )

            if fs > 0 and len(fft_samples) >= 2:
                valid_hp = hp_on and 0 < hp_cutoff < fs / 2
                if hp_on and not valid_hp:
                    hpf_info_label.setText("HPF 오류: 차단 주파수는 수신 Fs/2 미만이어야 합니다")
                valid_lp = lp_on and not lpf_auto_check.isChecked() and 0 < lp_cutoff < fs / 2
                valid_nt = nt_on and 0 < nt_center < fs / 2 and 0 < nt_width < fs / 2
                if lp_on and not lpf_auto_check.isChecked() and not valid_lp:
                    lpf_info.setText(f"LPF 오류 ({axis}): 0 < 차단 주파수 < {fs/2:g} Hz (수신 Fs={fs:g} Hz)")
                if nt_on and not valid_nt:
                    notch_info.setText("Notch 오류: 중심 및 제거 폭은 Fs/2 미만이어야 합니다")
                display_freqs, display_magnitude = fft_calculator(
                    fft_samples,
                    fs,
                    selected_fft_window,
                    kaiser_beta=selected_kaiser_beta,
                    hpf_enabled=valid_hp,
                    hpf_cutoff_hz=hp_cutoff,
                    hpf_order=hp_order,
                    lpf_enabled=valid_lp, lpf_cutoff_hz=lp_cutoff, lpf_order=lp_order,
                    notch_enabled=valid_nt, notch_center_hz=nt_center,
                    notch_width_hz=nt_width,
                )
            else:
                display_freqs = np.array([], dtype=np.float64)
                display_magnitude = np.array([], dtype=np.float64)

            display_fft_by_axis[axis] = {
                "freqs": display_freqs,
                "magnitude": display_magnitude,
                "fft_n": int(len(fft_samples)),
                "segment_start": int(fft_segment_start),
            }

            if fs > 0:
                raw_x = (
                    np.arange(
                        len(samples),
                        dtype=np.float64,
                    )
                    / fs
                )
            else:
                raw_x = np.arange(
                    len(samples),
                    dtype=np.float64,
                )

            calibration = calibrations.get(
                (snapshot_device, axis)
            )

            if mode == "ADXL" and mems_convert_to_g:
                # [MEMS g 환산 공식 - ADXL343 Full Resolution 고정]
                # ADXL343 공칭 감도 = 256 LSB/g, g_range=2/4/8/16에서도 동일.
                # 1 g = 9.80665 m/s^2 (여기서는 g 단위로 표시).
                #
                # Raw(g) = signed_raw(count) / 256
                # FFT(g RMS) = FFT_count_peak / 256 / sqrt(2)
                #
                # Raw는 시간영역 가속도(정적 중력 + 동적 성분 포함).
                # FFT는 원본 count 데이터의 평균을 제거하고 구한
                # 1-sided Peak 진폭을 RMS로 바꾼 것으로, 주파수축은 불변.
                raw_display = samples
                fft_display = (
                    np.asarray(display_magnitude, dtype=np.float64)
                    / (1.0 / mems_g_per_lsb_spin.value())
                    / (1.0 if mems_fft_unit_combo.currentData() == "g_peak" else np.sqrt(2.0))
                )

            elif mode == "ADXL":
                # g 변환 체크 해제 시, 원본 데이터를 그대로 표시.
                # FFT의 Window/N/Fs 등 계산 방식은 변하지 않는다.
                raw_display = samples
                fft_display = np.asarray(
                    display_magnitude, dtype=np.float64
                )

            elif mode == "PIEZO" and piezo_display_mode in ("g_spec", "velocity_rms"):
                # Signed ADC codes -> AC voltage after removing received packet mean.
                # 100 mV/g nominal sensor sensitivity (assumes analog gain = 1).
                # 문서 순서대로 signed ADC -> (ADC+Amax/2) 전압 -> g(t)
                # -> g_mean 제거. 샘플 수는 수신 데이터 길이 그대로 사용한다.
                hw = piezo_hw_metrics(samples, float(piezo_ref_voltage.value()),
                                      float(piezo_adc_denominator.value()))
                raw_display = hw["g"]  # V011: mean removed only in display buffer below
                fft_display = piezo_hw_fft_grms(
                    display_magnitude, float(piezo_ref_voltage.value()),
                    float(piezo_adc_denominator.value()))
                if piezo_display_mode == "velocity_rms":
                    # Velocity spectrum only: FFT magnitude is one-sided peak acceleration.
                    # DC has no physical velocity conversion; mark it as 0 for visualization.
                    freqs_for_velocity = np.asarray(display_freqs, dtype=np.float64)
                    fft_velocity = np.zeros_like(fft_display)
                    valid = freqs_for_velocity > 0
                    fft_velocity[valid] = piezo_hw_velocity_rms_mms(
                        fft_display[valid], freqs_for_velocity[valid])
                    fft_display = fft_velocity

            elif mode == "PIEZO" and piezo_display_mode == "g_pp":
                # 12mV P-P = 1g Peak -> 6mV Peak = 1g Peak.
                # RAW: g instantaneously; FFT: each sinusoidal bin in g RMS.
                # Voltage is still provisional: physical calibration must be verified.
                pp_mv = float(piezo_pp_mv.value())
                pp_g = float(piezo_pp_g.value())
                g_per_volt = (2000.0 * pp_g / pp_mv)  # mV P-P to volts peak
                manual_zero = get_piezo_zero_offset(snapshot_device)
                raw_zero = manual_zero if manual_zero is not None else (float(np.mean(samples)) if len(samples) else 0.0)
                raw_display = (samples - raw_zero) * piezo_voltage_scale * g_per_volt
                fft_display = (np.asarray(display_magnitude, dtype=np.float64)
                               * piezo_voltage_scale * g_per_volt / np.sqrt(2.0))

            elif mode == "PIEZO" and piezo_display_mode == "voltage":
                # 잠정 전압: signed count의 변화량에 선형 스케일만 적용.
                # offset은 현재 패킷 평균 또는 사용자가 설정한 RAW 0점.
                # 절대 전압 계산에는 ADC coding / 아날로그 회로 정보가 필요.
                manual_zero = get_piezo_zero_offset(snapshot_device)
                raw_zero = (
                    manual_zero if manual_zero is not None
                    else (float(np.mean(samples)) if len(samples) else 0.0)
                )
                # V008: 하드웨어 원식 그대로 RAW 전압 표시, 평균/수동 0점 제거 없음.
                # 내부 전압 표시 단위는 V (Y축 V), 커서는 mV로 변환한다.
                raw_display = piezo_hw_adc_to_mv(
                    samples, float(piezo_ref_voltage.value()),
                    float(piezo_adc_denominator.value())) / 1000.0  # display mean removed below
                fft_display = np.asarray(display_magnitude, dtype=np.float64) * piezo_voltage_scale

            elif (
                piezo_display_mode == "g"
                and piezo_calibration_complete
                and calibration is not None
                and float(
                    calibration.get("counts_per_g", 0.0)
                ) > 0
            ):
                counts_per_g = float(
                    calibration["counts_per_g"]
                )

                # Raw:
                # 기본은 기존처럼 현재 패킷 평균을 DC offset으로 사용.
                # PIEZO에서 사용자가 수동 0점을 지정한 경우에는
                # 그 raw count 값을 0점으로 사용한다.
                current_mean = (
                    float(np.mean(samples))
                    if len(samples)
                    else 0.0
                )

                manual_zero = get_piezo_zero_offset(
                    snapshot_device
                )

                raw_zero = (
                    manual_zero
                    if manual_zero is not None
                    else current_mean
                )

                raw_display = (
                    (samples - raw_zero)
                    / counts_per_g
                )

                # calculate_fft()의 magnitude는 Peak amplitude(count).
                # 기준이 RMS이므로:
                #
                # g RMS = count Peak / counts_per_g / sqrt(2)
                fft_display = (
                    np.asarray(
                        display_magnitude,
                        dtype=np.float64,
                    )
                    / counts_per_g
                    / np.sqrt(2.0)
                )
            else:
                # PIEZO는 물리단위 보정이 없어도 사용자가 지정한
                # count 값을 표시상 0으로 이동할 수 있다.
                #
                # 주의: FFT 계산은 원본 samples를 이용하며
                # calculate_fft() 내부에서 평균 제거를 수행하므로
                # 이 표시용 0점 값은 FFT 결과에 영향을 주지 않는다.
                manual_zero = get_piezo_zero_offset(
                    snapshot_device
                )

                # 원본 Count는 모드와 무관하게 ADC 수신값 그대로 표시.
                raw_display = samples

                fft_display = np.asarray(
                    display_magnitude,
                    dtype=np.float64,
                )

            if mode == "ADXL":
                # S(f) uses the measured (uncorrected) frequency; FFT power only.
                display_freqs, fft_display = corrected_mems_fft(
                    display_freqs, fft_display, mems_correction,
                    apply_sensitivity=mems_convert_to_g)
                display_fft_by_axis[axis]["freqs"] = display_freqs
                if mems_convert_to_g and mems_fft_unit_combo.currentData() == "velocity_rms":
                    # Convert calibrated acceleration g RMS/bin to velocity mm/s RMS/bin.
                    # Corrected f from the same calibration is used in the denominator.
                    fft_display = piezo_hw_velocity_rms_mms(fft_display, display_freqs)
                display_fft_by_axis[axis]["corrected_amplitude"] = fft_display

            # RAW 표시용 HPF: 원본 samples/CSV는 변경하지 않는다.
            # AC 성분이므로 DC/사용자 0점은 제거된 0 중심 파형을 표시한다.
            # 물리 단위 환산은 기존 표시 모드와 일치하도록 적용한다.
            if raw_hp_on and not (mode == "PIEZO" and piezo_display_mode == "count") and fs > 0 and len(samples) >= 2:
                if 0 < hp_cutoff < fs / 2:
                    raw_filtered_counts = (
                        scipy_filter_signal(samples - np.mean(samples), fs, hpf=True,
                            hp_cutoff=hp_cutoff, hp_order=hp_order)
                        if scipy_mode else
                        butterworth_highpass_zero_phase(samples, fs, hp_cutoff, hp_order)
                    )
                    if mode == "ADXL":
                        raw_display = raw_filtered_counts
                    elif mode == "PIEZO" and piezo_display_mode == "g_pp":
                        raw_display = raw_filtered_counts * piezo_voltage_scale * (2000.0 * piezo_pp_g.value() / piezo_pp_mv.value())
                    elif mode == "PIEZO" and piezo_display_mode in ("g_spec", "velocity_rms"):
                        raw_display = raw_filtered_counts * piezo_voltage_scale / (PIEZO_SENSOR_MV_PER_G / 1000.0)
                    elif mode == "PIEZO" and piezo_display_mode == "voltage":
                        raw_display = raw_filtered_counts * piezo_voltage_scale
                    elif (mode == "PIEZO" and piezo_display_mode == "g"
                          and piezo_calibration_complete and calibration is not None
                          and float(calibration.get("counts_per_g", 0)) > 0):
                        raw_display = raw_filtered_counts / float(calibration["counts_per_g"])
                    else:
                        raw_display = raw_filtered_counts
                else:
                    hpf_info_label.setText("RAW HPF 오류: 차단 주파수는 수신 Fs/2 미만이어야 합니다")

            # Apply LPF / Notch to the display signal only (never to CSV).
            if (raw_lp_on or raw_nt_on) and not (mode == "PIEZO" and piezo_display_mode == "count") and fs > 0 and len(samples) >= 2:
                valid_lp_raw = raw_lp_on and not lpf_auto_check.isChecked() and 0 < lp_cutoff < fs / 2
                valid_nt_raw = raw_nt_on and 0 < nt_center < fs / 2 and 0 < nt_width < fs / 2
                if raw_lp_on and not lpf_auto_check.isChecked() and not valid_lp_raw:
                    lpf_info.setText(f"RAW LPF 오류 ({axis}): 0 < 차단 주파수 < {fs/2:g} Hz (수신 Fs={fs:g} Hz)")
                if raw_nt_on and not valid_nt_raw:
                    notch_info.setText("RAW Notch 오류: 중심 및 제거 폭은 수신 Fs/2 미만이어야 합니다")
                if valid_lp_raw or valid_nt_raw:
                    # Work in count space, applying all enabled RAW filters
                    # together to avoid filtering an already unit-converted display.
                    # V008: RAW 자동 0점 제거 사용하지 않음.
                    # LPF/Notch에는 원본 ADC 코드가 그대로 입력된다.
                    # HPF가 켜지면 필터 고유 특성상 DC는 제거됨.
                    if mode == "PIEZO":
                        raw_filter_input = np.asarray(samples, dtype=np.float64)
                    else:
                        raw_filter_input = samples
                    raw_all = spectral_filter(
                        raw_filter_input, fs, hpf=raw_hp_on and 0 < hp_cutoff < fs / 2,
                        hp_cutoff=hp_cutoff, hp_order=hp_order,
                        lpf=valid_lp_raw, lp_cutoff=lp_cutoff, lp_order=lp_order,
                        notch=valid_nt_raw, notch_center=nt_center, notch_width=nt_width,
                    )
                    if mode == "ADXL":
                        raw_display = raw_all
                    elif mode == "PIEZO" and piezo_display_mode == "g_pp":
                        raw_display = raw_all * piezo_voltage_scale * (2000.0 * piezo_pp_g.value() / piezo_pp_mv.value())
                    elif mode == "PIEZO" and piezo_display_mode in ("g_spec", "velocity_rms"):
                        # Keep ADC offset for LPF/Notch; HPF removes DC by definition.
                        v_raw = raw_all + (0.0 if raw_hp_on else float(piezo_adc_denominator.value()) / 2.0)
                        raw_display = v_raw * piezo_voltage_scale / (PIEZO_SENSOR_MV_PER_G / 1000.0)
                    elif mode == "PIEZO" and piezo_display_mode == "voltage":
                        v_raw = raw_all + (0.0 if raw_hp_on else float(piezo_adc_denominator.value()) / 2.0)
                        raw_display = v_raw * piezo_voltage_scale
                    elif (mode == "PIEZO" and piezo_display_mode == "g"
                          and piezo_calibration_complete and calibration is not None
                          and float(calibration.get("counts_per_g", 0)) > 0):
                        raw_display = raw_all / float(calibration["counts_per_g"])
                    else:
                        raw_display = raw_all

            # V011: ONLY plot/cursor buffer is mean-centered.
            # Original samples, CSV, FFT and RMS are unchanged.
            # Mean is computed from the exact post-filter display vector,
            # so the plotted Y-axis center is zero in every PIEZO RAW unit.
            if mode == "PIEZO" and len(raw_display):
                raw_display = np.asarray(raw_display, dtype=np.float64)
                raw_display = raw_display - np.mean(raw_display)

            if mode == "ADXL":
                mems_raw_counts[axis] = (np.asarray(raw_x), np.asarray(raw_display))
            else:
                raw_curves[axis].setData(raw_x, raw_symlog_y(raw_display) if raw_symlog_state['enabled'] else raw_display)
            fft_curves[axis].setData(
                np.asarray(
                    display_freqs,
                    dtype=np.float64,
                ),
                fft_display,
            )
            cursor_arrays["raw"][axis] = (np.asarray(raw_x), np.asarray(raw_display))
            cursor_arrays["fft"][axis] = (np.asarray(display_freqs), np.asarray(fft_display))
            if mode == "PIEZO":
                cursor_piezo_conversion["raw"][axis] = {
                    "x": np.asarray(raw_x), "count": np.asarray(samples),
                    "zero": float(np.mean(samples)) if len(samples) else 0.0,
                }
                cursor_piezo_conversion["fft"][axis] = {
                    "x": np.asarray(display_freqs),
                    "count_peak": np.asarray(display_magnitude),
                }

            if first is None:
                first = rec
                first_axis_used = axis

        if mode == "ADXL":
            # One common RAW unit across XYZ, based on post-filter displayed samples.
            selected_raw = mems_raw_unit_combo.currentData()
            g_per_lsb = float(mems_g_per_lsb_spin.value())
            if selected_raw == "auto":
                peak_g = max((float(np.max(np.abs(y))) * g_per_lsb
                              for x, y in mems_raw_counts.values() if y.size), default=0.0)
                selected_raw = "µg" if peak_g < 0.001 else "mg" if peak_g < 1.0 else "g"
            if selected_raw != mems_raw_display_unit:
                raw_fixed = None
                raw_min.clear(); raw_max.clear()
                raw_plot.getViewBox().setMouseEnabled(x=True, y=True)
                raw_plot.enableAutoRange(axis="y", enable=True)
            mems_raw_display_unit = selected_raw
            raw_factor = {"count": 1.0, "g": g_per_lsb, "mg": g_per_lsb * 1000.0,
                          "µg": g_per_lsb * 1e6}[selected_raw]
            raw_plot.getAxis("left").enableAutoSIPrefix(False)
            raw_plot.setLabel("left", "ADC" if selected_raw == "count" else "Acceleration", units=selected_raw)
            for axis, (xs, counts) in mems_raw_counts.items():
                ys = counts * raw_factor
                raw_curves[axis].setData(xs, raw_symlog_y(ys) if raw_symlog_state['enabled'] else ys)
                cursor_arrays["raw"][axis] = (xs, ys)
            update_y_range_unit_labels()

        # SRSS: one scalar overall AC RMS, independent of axis visibility.
        if mode == "ADXL":
            complete=all(a in display_fft_by_axis and "corrected_amplitude" in display_fft_by_axis[a] for a in ("X","Y","Z"))
            fft_curves["gN"].setData([],[])
            cursor_arrays["fft"].pop("gN",None)
            fft_curves["SRSS"].setData([],[])
            srss_limit_curve.setData([],[])
            cursor_arrays["fft"].pop("SRSS",None)
            fft_plot.setTitle("SRSS: 완전한 XYZ 데이터 없음")
            if complete:
                source=[display_fft_by_axis[a] for a in ("X","Y","Z")]
                base_f=np.asarray(source[0]["freqs"],dtype=float)
                nn=source[0]["fft_n"]
                matched=bool(len(base_f)) and all(v["fft_n"]==nn and np.array_equal(base_f,v["freqs"]) for v in source)
                if matched:
                    try:
                        gn=np.sqrt(sum(np.square(np.asarray(v["corrected_amplitude"],dtype=float)) for v in source))
                        fft_curves["gN"].setData(base_f,gn)
                        cursor_arrays["fft"]["gN"]=(base_f,gn)
                        w=build_fft_window(nn,selected_fft_window,kaiser_beta=selected_kaiser_beta)
                        key=mems_fft_unit_combo.currentData()
                        # Displayed RMS bins use peak/sqrt(2), including endpoints.
                        # Recover peak before the correct energy/endpoint normalization.
                        peak_factor=np.sqrt(2.0) if key in ("g_rms","velocity_rms") else 1.0
                        level=float(np.sqrt(sum(srss_axis_power(np.asarray(v["corrected_amplitude"])*peak_factor,nn,w) for v in source)))
                        unit="Count RMS" if key=="count_peak" else "mm/s RMS" if key=="velocity_rms" else "g RMS"
                        fft_curves["SRSS"].setData(base_f,np.full(len(base_f),level))
                        cursor_arrays["fft"]["SRSS"]=(base_f,np.full(len(base_f),level))
                        fft_plot.setTitle("SRSS = {:.6g} {}".format(level,unit))
                        if key=="velocity_rms":
                            limit=srss_limit_get(current_device)
                            srss_limit_curve.setData(base_f,np.full(len(base_f),limit))
                            fft_plot.setTitle("SRSS = {:.6g} {} | 한계 {:.6g} mm/s RMS".format(level,unit,limit))
                    except (ValueError,TypeError,OverflowError): pass

        apply_visibility()
        refresh_pinned_cursors()

        if first is None:
            info.setText(f"{current_device or '-'} | 데이터 대기 중")
            if piezo_pp_info is not None:
                piezo_pp_info.setText("패킷 P-P: 수신 대기 중 | 누적 ΣMax−ΣMin: 수신 대기 중")
            if piezo_grms_compare_info is not None:
                piezo_grms_compare_info.setText("선택 단위 측정값: 수신 대기 중")
            if mode == "PIEZO":
                piezo_cycle_info.setText("FFT 피크 기준 1주기: 수신 대기 중")
                if cycle_region is not None:
                    cycle_region.hide()
            return

        samples0 = np.asarray(
            first.get("samples", []),
            dtype=np.float64,
        )
        n = len(samples0)
        fs = float(first.get("sample_rate", 0.0))
        signature = (n, fs)

        # 물리단위 변환 여부에 따라 Y축 단위를 명확하게 바꾼다.
        if mode == "PIEZO" and piezo_display_mode == "velocity_rms":
            raw_plot.setLabel("left", "Instant acceleration", units="g")
            fft_plot.setLabel("left", "Velocity RMS per FFT bin", units="mm/s")
        elif mode == "ADXL":
            fft_plot.getAxis("left").enableAutoSIPrefix(False)
            fft_plot.setLabel("left", "Velocity" if mems_fft_unit_combo.currentData() == "velocity_rms" else "Amplitude", units=cursor_units("fft"))
        elif physical_units_enabled:
            raw_plot.setLabel(
                "left",
                "Acceleration",
                units="g",
            )
            fft_plot.setLabel(
                "left",
                "Amplitude",
                units="g RMS",
            )
        elif mode == "ADXL":
            raw_plot.setLabel("left", "Raw minus packet mean", units="count")
            fft_plot.setLabel("left", "Amplitude", units="count Peak")
        elif piezo_display_mode == "voltage":
            raw_plot.setLabel("left", "AC voltage (RAW display centered)", units="V")
            fft_plot.setLabel("left", "AC Amplitude (provisional)", units="V Peak")
        else:
            raw_plot.setLabel("left", "Raw minus packet mean", units="count")
            fft_plot.setLabel("left", "Amplitude", units="count Peak")

        if force_full or signature != current_signature:
            current_signature = signature
            time_end = ((n - 1) / fs) if n > 0 and fs > 0 else max(n - 1, 1)
            raw_plot.setXRange(0, max(time_end, 1e-9), padding=0)
            if fft_log_x_check.isChecked():
                # Frequencies are sampled at Fs/N; 0 Hz is undefined in log.
                f0 = fs / max(n, 1) if fs > 0 else 1.0
                fhi = max(fs / 2.0, f0 * 1.01)
                fft_plot.setXRange(np.log10(f0), np.log10(fhi), padding=0)
            else:
                fft_plot.setXRange(0, max(fs / 2.0, 1.0), padding=0)
            if raw_fixed is None:
                raw_plot.enableAutoRange(axis="y", enable=True)
            if fft_fixed is None:
                fft_plot.enableAutoRange(axis="y", enable=True)

        packet_fs = (
            first.get("packet_sample_rate", "")
            or "(없음)"
        )
        packet_n = (
            first.get("packet_numofsample", "")
            or "(없음)"
        )
        first_fft_display = display_fft_by_axis.get(
            first_axis_used,
            {},
        )

        first_display_freqs = np.asarray(
            first_fft_display.get(
                "freqs",
                np.array([], dtype=np.float64),
            ),
            dtype=np.float64,
        )
        first_display_magnitude = np.asarray(
            first_fft_display.get(
                "magnitude",
                np.array([], dtype=np.float64),
            ),
            dtype=np.float64,
        )
        first_fft_n = int(
            first_fft_display.get(
                "fft_n",
                len(samples0),
            )
        )
        first_fft_segment_start = int(
            first_fft_display.get(
                "segment_start",
                0,
            )
        )

        n_cfg = max(1, int(first_fft_n))

        peak_bin = -1
        peak_hz = 0.0
        peak_mag_count = 0.0

        if (
            len(first_display_magnitude) > 1
            and len(first_display_freqs)
            == len(first_display_magnitude)
        ):
            peak_bin = (
                int(
                    np.argmax(
                        first_display_magnitude[1:]
                    )
                )
                + 1
            )
            peak_hz = float(
                first_display_freqs[peak_bin]
            )
            peak_mag_count = float(
                first_display_magnitude[peak_bin]
            )

        raw_mean = (
            float(np.mean(samples0))
            if len(samples0)
            else 0.0
        )
        raw_min_v = (
            float(np.min(samples0))
            if len(samples0)
            else 0.0
        )
        raw_max_v = (
            float(np.max(samples0))
            if len(samples0)
            else 0.0
        )
        centered0 = samples0 - raw_mean
        raw_rms_count = (
            float(
                np.sqrt(
                    np.mean(centered0 ** 2)
                )
            )
            if len(samples0)
            else 0.0
        )

        first_axis = first_axis_used or axes[0]
        calibration = calibrations.get(
            (snapshot_device, first_axis)
        )

        if mode == "ADXL":
            counts_per_g = (1.0 / mems_g_per_lsb_spin.value())
            g_range = normalize_adxl_g_range(
                first.get("g_range", "")
            )
            range_text = (
                f"±{g_range} g"
                if g_range is not None
                else "unknown"
            )

            if mems_convert_to_g:
                raw_rms_g = raw_rms_count / counts_per_g
                peak_mag_display = (
                    peak_mag_count / counts_per_g / np.sqrt(2.0)
                )
                if mems_correction["sensitivity_enabled"]:
                    original_peak_hz = peak_hz * (mems_correction["frequency_divisor"] if mems_correction["frequency_enabled"] else 1.0)
                    peak_mag_display /= float(mems_sensitivity(original_peak_hz, mems_correction))
                if mems_fft_unit_combo.currentData() == "g_peak":
                    peak_mag_display *= np.sqrt(2.0)
                elif mems_fft_unit_combo.currentData() == "velocity_rms":
                    peak_mag_display = float(piezo_hw_velocity_rms_mms(np.asarray([peak_mag_display]), np.asarray([peak_hz]))[0])
                amp_text = (
                    f"Peak Mag={peak_mag_display:.6g} {cursor_units('fft')} | "
                    f"Raw AC RMS={raw_rms_g:.6g} g | "
                    f"Range={range_text} | "
                    f"Scale={counts_per_g:.6g} LSB/g (Full Res) | "
                    f"g 변환=ON"
                )
            else:
                amp_text = (
                    f"Peak Mag={peak_mag_count:.6g} count Peak | "
                    f"Raw AC RMS={raw_rms_count:.6g} count | "
                    f"Range={range_text} | "
                    f"g 변환=OFF (원본 count)"
                )

        elif (
            piezo_display_mode == "g"
            and piezo_calibration_complete
            and calibration is not None
            and float(
                calibration.get("counts_per_g", 0.0)
            ) > 0
        ):
            counts_per_g = float(
                calibration["counts_per_g"]
            )
            raw_rms_g = (
                raw_rms_count / counts_per_g
            )
            peak_mag_display = (
                peak_mag_count
                / counts_per_g
                / np.sqrt(2.0)
            )
            amp_text = (
                f"Peak Mag={peak_mag_display:.6g} g RMS | "
                f"Raw RMS={raw_rms_g:.6g} g | "
                f"Scale={counts_per_g:.6g} count/g"
            )
        elif mode == "PIEZO" and piezo_display_mode in ("g_spec", "velocity_rms"):
            spec_v_per_g = PIEZO_SENSOR_MV_PER_G / 1000.0
            amp_text = (
                f"FFT Peak={peak_mag_count * piezo_voltage_scale / spec_v_per_g / np.sqrt(2.0):.6g} g RMS (단일 피크) | "
                f"Raw AC RMS={raw_rms_count * piezo_voltage_scale:.6g} V RMS "
                f"= {raw_rms_count * piezo_voltage_scale / spec_v_per_g:.6g} g RMS | "
                f"RAW 표시 평균=0 | RMS 평균 제거=ON | 센서 감도={PIEZO_SENSOR_MV_PER_G:g}mV/g (Amax={PIEZO_ADC_COUNTS:g}, Vmax={piezo_ref_voltage.value():.6g}V 시험 환산)"
            )
        elif mode == "PIEZO" and piezo_display_mode == "g_pp":
            scale_pp = 2000.0 * piezo_pp_g.value() / piezo_pp_mv.value()
            amp_text = (
                f"Peak Mag={peak_mag_count * piezo_voltage_scale * scale_pp / np.sqrt(2.0):.6g} g RMS | "
                f"실측 교정={piezo_pp_mv.value():g}mV P-P / {piezo_pp_g.value():g}g Peak (추정)"
            )
        elif mode == "PIEZO" and piezo_display_mode == "voltage":
            amp_text = (
                f"FFT Peak={peak_mag_count * piezo_voltage_scale:.6g} V Peak | "
                f"Raw AC RMS(평균 제거 후)={raw_rms_count * piezo_voltage_scale:.6g} V | "
                f"Scale={piezo_voltage_scale:.9g} V/count (assumed)"
            )
        else:
            amp_text = (
                f"Peak Mag={peak_mag_count:.6g} count Peak | "
                f"Raw RMS={raw_rms_count:.6g} count"
            )

        time_end = (
            ((n - 1) / fs)
            if n > 0 and fs > 0
            else 0.0
        )

        window_info_text = f"Window={selected_fft_window}"

        zero_info_text = ""
        if mode == "PIEZO":
            manual_zero = get_piezo_zero_offset(
                snapshot_device
            )

            if piezo_display_mode == "count":
                zero_info_text = " | RAW 그래프는 Count−패킷 평균 / 원본은 수신값 그대로"
            elif get_piezo_zero_mode(snapshot_device) == "off":
                zero_info_text = " | RAW 그래프만 패킷 평균 제거 (원본/FFT/RMS 입력 불변)"
            elif manual_zero is not None:
                zero_info_text = f" | 수동 Raw Zero={manual_zero:.6g} count"
            else:
                zero_info_text = " | 자동 Raw Zero=패킷 평균"

        if selected_fft_window == "Kaiser":
            window_info_text += (
                f"(β={selected_kaiser_beta:.2f})"
            )

        if mode == "ADXL":
            raw_stats_text = (f"Raw Mean={raw_mean * raw_factor:.6g} {mems_raw_display_unit} / "
                              f"Min={raw_min_v * raw_factor:.6g} / Max={raw_max_v * raw_factor:.6g}")
        else:
            raw_stats_text = (
                f"Raw Mean={raw_mean:.6g} / "
                f"Min={raw_min_v:.6g} / Max={raw_max_v:.6g}"
            )

        info.setText(
            f"{snapshot_device} | "
            f"Packet Fs={packet_fs} / FFT Fs={fs:.6g} Hz | "
            f"Packet N={packet_n} / Raw N={n} | "
            f"Window N={first_fft_n} / FFT N={first_fft_n} | "
            f"{window_info_text} | "
            f"Peak Bin={peak_bin} / Peak={peak_hz:.6g} Hz | "
            f"{amp_text}"
            f"{zero_info_text} | "
            f"{raw_stats_text} | "
            f"Time X=0~{time_end:.6g} s | "
            f"FFT Segment={first_fft_segment_start}~"
            f"{first_fft_segment_start + max(first_fft_n - 1, 0)} | "
            f"FFT X=0~{fs/2.0:g} Hz | "
            f"Δf={fs/max(n_cfg,1):.6g} Hz"
        )

        if piezo_pp_info is not None:
            # Display P-P must follow the same user-editable voltage as FFT and RMS.
            mv_per_count = float(piezo_ref_voltage.value()) * 1000.0 / float(piezo_adc_denominator.value())
            pp_mv = (raw_max_v - raw_min_v) * mv_per_count
            sum_max = float(first.get("piezo_pp_sum_max_count", 0.0)) * mv_per_count
            sum_min = float(first.get("piezo_pp_sum_min_count", 0.0)) * mv_per_count
            pp_total = sum_max - sum_min
            packets = int(first.get("piezo_pp_packet_count", 0))
            piezo_pp_info.setText(
                f"현재 패킷 P-P = {pp_mv:,.4f} mV  |  "
                f"누적 (ΣMax - ΣMin) = {pp_total:,.4f} mV  "
                f"[ΣMax={sum_max:,.4f} mV, ΣMin={sum_min:,.4f} mV, "
                f"패킷 {packets:,}건]"
            )
        if mode == "PIEZO":
            # Show the *estimated* one-period length of the strongest non-DC FFT bin.
            # No assertion that the entire signal is a pure single-frequency wave.
            if peak_bin >= 1 and peak_hz > 0 and fs > 0 and n > 1:
                one_cycle_seconds = 1.0 / peak_hz
                one_cycle_samples = fs / peak_hz
                # Select a centered interval inside the received packet; no phase detection.
                interval = min(one_cycle_seconds, n / fs)
                middle = (n - 1) / (2.0 * fs)
                cycle_begin = max(0.0, middle - interval / 2.0)
                cycle_end = min((n - 1) / fs, cycle_begin + interval)
                start_index = max(0, min(n - 1, int(np.floor(cycle_begin * fs))))
                end_index = max(start_index + 1, min(n, int(np.ceil(cycle_end * fs))))
                cycle_counts = samples0[start_index:end_index]
                cycle_min = float(np.min(cycle_counts))
                cycle_max = float(np.max(cycle_counts))
                cycle_pp = (cycle_max - cycle_min) * piezo_voltage_scale * 1000.0
                piezo_cycle_info.setText(
                    f"FFT 피크 기준 추정 1주기: f={peak_hz:.4f} Hz | "
                    f"T={one_cycle_seconds * 1000:.4f} ms | "
                    f"Fs={fs:g} Hz | Fs/f={one_cycle_samples:.2f} 샘플 | "
                    f"RAW 음영 구간={cycle_begin*1000:.3f}~{cycle_end*1000:.3f} ms | "
                    f"범위: {start_index}~{end_index - 1}번 샘플, "
                    f"Count 최소={cycle_min:.2f}, 최대={cycle_max:.2f}, "
                    f"P-P={cycle_pp:.4f} mV "
                    "(패킷 중앙 임의 구간, 실제 주기 경계/위상 검출 아님)"
                )
                if cycle_region is not None:
                    cycle_region.setRegion((cycle_begin, cycle_end))
                    cycle_region.show()
            else:
                piezo_cycle_info.setText("FFT 피크 기준 추정 1주기: 주파수 미검출")
                if cycle_region is not None:
                    cycle_region.hide()

        if piezo_grms_compare_info is not None:
            # Spec based metrics. No 12mV P-P or -14400 Count anywhere in calculation.
            scale_mv = float(piezo_ref_voltage.value()) * 1000.0 / float(piezo_adc_denominator.value())
            pp_mv_spec = (raw_max_v - raw_min_v) * scale_mv
            hw_metrics = piezo_hw_metrics(samples0, float(piezo_ref_voltage.value()), float(piezo_adc_denominator.value()))
            ac_rms_mv = hw_metrics["voltage_ac_rms_mv"]
            g_rms_spec = hw_metrics["g_rms"]
            fft_peak_grms = (float(piezo_hw_fft_grms(peak_mag_count, float(piezo_ref_voltage.value()), float(piezo_adc_denominator.value())))
                             if peak_bin >= 1 else float('nan'))
            fft_velocity_mms = (float(piezo_hw_velocity_rms_mms(fft_peak_grms, peak_hz))
                                if peak_bin >= 1 and peak_hz > 0 else float('nan'))
            selected_mode = str(piezo_unit_combo.currentData())
            if selected_mode == "velocity_rms":
                if np.isfinite(fft_velocity_mms):
                    text = (f"선택: 진동 속도 RMS = {fft_velocity_mms:.5f} mm/s "
                            f"@ FFT 피크 {peak_hz:.3f} Hz (단일 주파수 성분)")
                else:
                    text = "선택: 진동 속도 RMS (mm/s) | 유효한 FFT 피크 없음"
                formula = "FFT 속도 RMS(mm/s) = 해당 주파수 FFT g RMS × 9806.65 / (2πf)"
            elif selected_mode == "g_spec":
                text = (f"선택: 가속도 RMS = {g_rms_spec:.6f} g (원본 전체 AC) | "
                        f"FFT 피크 = {fft_peak_grms:.6f} g RMS @ {peak_hz:.3f} Hz"
                        if np.isfinite(fft_peak_grms) else
                        f"선택: 가속도 RMS = {g_rms_spec:.6f} g (원본 전체 AC)")
                formula = (f"시험 ADC전압(mV)={piezo_ref_voltage.value()*1000:.6g}×(C+{piezo_adc_denominator.value()/2:g})/{piezo_adc_denominator.value():g} "
                           "→ g(t)=v(t)/100 → gmean=Σg/N → g RMS=√[Σ(g−gmean)²/N]; FFT bin=Peak/√2; RAW 그래프만 평균 제거")
            elif selected_mode == "voltage":
                text = (f"선택: RAW 표시만 평균=0 (원본 ADC 유지) | "
                        f"ADC 평균 절대전압 = {hw_metrics['voltage_mean_mv']:.5f} mV | "
                        f"AC RMS(별도 평균 제거)={ac_rms_mv:.5f} mV")
                formula = (f"시험 ADC전압(mV)={piezo_ref_voltage.value()*1000:.6g}×(C+{piezo_adc_denominator.value()/2:g})/{piezo_adc_denominator.value():g} "
                           "(16비트 원본 Count의 12비트 절대전압 해석 금지); RAW 그래프=전압−평균전압")
            else:
                text = (f"선택: ADC 원본 Count | 평균(0점) = {raw_mean:.3f} Count | "
                        f"P-P = {raw_max_v-raw_min_v:.3f} Count")
                formula = "0점(Count) = ΣADC / N; P-P(Count) = Max−Min"
            piezo_grms_compare_info.setText(text)
            refresh_piezo_formula()
        refresh_calibration_status(snapshot_device)
        refresh_piezo_zero_status(snapshot_device)

    def clear_infer_markers():
        while infer_markers:
            item = infer_markers.pop()
            try:
                fft_plot.removeItem(item)
            except Exception:
                pass

    def refresh_infer_markers():
        clear_infer_markers()
        if not infer_marks.isChecked() or not infer_current['candidates']:
            return
        candidate = infer_current['candidates'][0]
        axis = infer_current['axis']
        if not axis or infer_current['nyquist'] <= 0:
            return
        for harmonic in range(1, 6):
            f = candidate['base_hz'] * harmonic
            if f > infer_current['nyquist']:
                break
            line = pg.InfiniteLine(pos=f, angle=90,
                                   pen=pg.mkPen((120, 40, 175, 150), width=1,
                                                style=QtCore.Qt.PenStyle.DashLine))
            fft_plot.addItem(line)
            infer_markers.append(line)

    def run_rotating_inference():
        if not current_snapshot:
            infer_result.setText("아직 수신된 FFT 데이터가 없습니다.")
            return
        rpm_value = float(infer_rpm.value())
        if rpm_value <= 0:
            infer_result.setText("RPM을 0보다 크게 입력하세요.")
            return
        axis = infer_axis.currentText()
        rec = (current_snapshot.get('axes') or {}).get(axis)
        if rec is None:
            infer_result.setText(f"{axis}축 데이터가 없습니다.")
            return
        samples = np.asarray(rec.get('samples', []), dtype=np.float64)
        fs = float(rec.get('sample_rate', 0))
        if fs <= 0 or len(samples) < 4:
            infer_result.setText("FFT에 필요한 데이터가 부족합니다.")
            return
        segment, _ = select_fft_segment(samples, selected_fft_window_size())
        window, beta = current_fft_window_settings()
        hp_on = hpf_enabled_check.isChecked()
        hp_cutoff = float(hpf_cutoff_spin.value())
        if hp_on and not (0 < hp_cutoff < fs / 2):
            infer_result.setText("HPF 차단 주파수는 Fs/2 미만이어야 합니다.")
            return
        lp_on = fft_lpf_check.isChecked() and not lpf_auto_check.isChecked()
        nt_on = fft_notch_check.isChecked()
        if lp_on and not (0 < lpf_cutoff_spin.value() < fs / 2):
            infer_result.setText("LPF 차단 주파수는 Fs/2 미만이어야 합니다.")
            return
        if nt_on and not (0 < notch_center_spin.value() < fs / 2
                          and 0 < notch_width_spin.value() < fs / 2):
            infer_result.setText("Notch 중심 주파수와 제거 폭은 Fs/2 미만이어야 합니다.")
            return
        f, mag = fft_calculator(
            segment, fs, window, beta,
            hpf_enabled=hp_on, hpf_cutoff_hz=hp_cutoff,
            hpf_order=int(hpf_order_spin.value()),
            lpf_enabled=lp_on, lpf_cutoff_hz=float(lpf_cutoff_spin.value()),
            lpf_order=int(lpf_order_spin.value()),
            notch_enabled=nt_on, notch_center_hz=float(notch_center_spin.value()),
            notch_width_hz=float(notch_width_spin.value()),
        )
        candidates = estimate_rotating_counts(f, mag, rpm_value)
        infer_current.update(candidates=candidates, axis=axis, nyquist=float(f[-1]),
                             device=current_snapshot.get('device_id'))
        if not candidates:
            infer_result.setText("일치하는 후보가 없습니다. RPM/샘플링 설정 또는 신호를 확인하세요.")
            clear_infer_markers()
            return
        entries = []
        for row in candidates:
            entries.append(f"{row['count']}개 ({row['base_hz']:g}Hz, 일치 {len(row['matched'])}개, 점수 {row['score']:.2f})")
        infer_result.setText(f"[{infer_type.currentText()}] 후보: " + " | ".join(entries) +
                             "  ※ 점수는 상대 지표, 부품 수 확정/고장 판정 아님")
        refresh_infer_markers()

    def hpf_settings_changed(_value=None):
        enabled = hpf_enabled_check.isChecked() or raw_hpf_enabled_check.isChecked()
        hpf_cutoff_spin.setEnabled(enabled)
        hpf_order_spin.setEnabled(enabled)
        if infer_current['candidates']:
            infer_current.update(candidates=[], axis=None, device=None, nyquist=0.0)
            clear_infer_markers()
            infer_result.setText("HPF 설정이 변경되었습니다. 개수를 다시 추정하세요.")
        if current_snapshot:
            apply_snapshot(current_snapshot, force_full=False)

    raw_hpf_enabled_check.stateChanged.connect(hpf_settings_changed)
    hpf_enabled_check.stateChanged.connect(hpf_settings_changed)
    hpf_cutoff_spin.valueChanged.connect(hpf_settings_changed)
    hpf_order_spin.valueChanged.connect(hpf_settings_changed)
    hpf_settings_changed()

    def extra_filter_settings_changed(_value=None):
        lpf_active = raw_lpf_check.isChecked() or fft_lpf_check.isChecked()
        notch_active = raw_notch_check.isChecked() or fft_notch_check.isChecked()
        lpf_cutoff_spin.setEnabled(lpf_active and not lpf_auto_check.isChecked())
        lpf_order_spin.setEnabled(lpf_active)
        notch_center_spin.setEnabled(notch_active)
        notch_width_spin.setEnabled(notch_active)
        if infer_current['candidates']:
            infer_current.update(candidates=[], axis=None, device=None, nyquist=0.0)
            clear_infer_markers()
            infer_result.setText("필터 설정이 변경되었습니다. 개수를 다시 추정하세요.")
        if current_snapshot:
            apply_snapshot(current_snapshot, force_full=False)

    for control in (raw_lpf_check, fft_lpf_check, raw_notch_check, fft_notch_check, lpf_auto_check):
        control.stateChanged.connect(extra_filter_settings_changed)
    for control in (lpf_cutoff_spin, notch_center_spin, notch_width_spin):
        control.valueChanged.connect(extra_filter_settings_changed)
    lpf_order_spin.valueChanged.connect(extra_filter_settings_changed)
    extra_filter_settings_changed()

    infer_btn.clicked.connect(run_rotating_inference)
    infer_marks.stateChanged.connect(lambda _state: refresh_infer_markers())

    def refresh_piezo_formula():
        """V015: current mode and device voltage; does not modify measurement data."""
        if mode != "PIEZO" or piezo_unit_combo is None or piezo_formula_info is None:
            return
        unit = str(piezo_unit_combo.currentData())
        vmax = float(piezo_ref_voltage.value())
        amax = float(piezo_adc_denominator.value())
        mv_count = 1000.0 * vmax / amax
        preface = (f"[선택한 디바이스의 계산식]  Vmax={vmax:.6f} V, "
                   f"Amax={amax:g}, 감도=100 mV/g\n"
                   f"변환계수 K = (1000 × {vmax:.6f}) / {amax:g} = {mv_count:.9f} mV/Count\n")
        if unit == "count":
            body = (
                "RAW [Count] : C[n] = 수신 HEX의 signed int16 원본\n"
                "그래프 표시 : C_display[n] = C[n] − mean(C)  (표시용 0점)\n"
                "FFT [Count Peak] : X[k] = rFFT((C − mean(C)) × w[n])\n"
                "FFT_peak[k] = 2 × |X[k]| / Σw[n]  (DC/Nyquist 예외)\n"
                "※ 원본 Count는 변경하거나 저장값에서 평균을 빼지 않음"
            )
        elif unit == "voltage":
            body = (
                "원래 ADC 공식 : V_abs[n] = (C[n] + Amax/2) × K  [mV]\n"
                "RAW [mV] : V_display[n] = (C[n] − mean(C)) × K\n"
                "FFT [mV Peak] : FFT_voltage_peak[k] = FFT_count_peak[k] × K\n"
                "※ 수신은 signed 16bit: 위 12bit V_abs는 실제 절대전압으로 해석 금지"
            )
        elif unit == "g_spec":
            body = (
                "원래 ADC 공식 : V_abs[n] = (C[n] + Amax/2) × K [mV]\n"
                "① g[n] = V_abs[n] / 100  →  ② g_mean = Σg[n] / N\n"
                "③ 전체 g_RMS = √(Σ(g[n] − g_mean)² / N)\n"
                "RAW [g] : g_display[n] = (C[n] − mean(C)) × K / 100\n"
                "FFT [g RMS] : (FFT_count_peak[k] × K / 100) / √2\n"
                "FFT_count_peak[k] = 2 × |rFFT((C−mean(C))×w)[k]| / Σw\n"
                "※ FFT는 주파수별 RMS, 전체 g_RMS와는 다른 결과일 수 있음"
            )
        elif unit == "velocity_rms":
            body = (
                "RAW [g] : (C[n] − mean(C)) × K / 100  (RAW는 속도 그래프가 아님)\n"
                "① FFT_count_peak[k] = 2 × |rFFT((C−mean(C))×w)[k]| / Σw\n"
                "② FFT_g_RMS[k] = FFT_count_peak[k] × K / (100 × √2)\n"
                "③ FFT_velocity_RMS[k] = FFT_g_RMS[k] × 9806.65 / (2π × f[k]) [mm/s]\n"
                "※ f=0Hz(DC)는 속도 환산에서 제외; 각 주파수 성분별 값"
            )
        else:
            body = "선택한 표시 단위에 대한 계산식이 없습니다."
        piezo_formula_info.setText(preface + body)

    def mems_g_mode_changed(_state=None):
        refresh_piezo_formula()
        # count / g 전환 시 기존 Y축 고정 범위는 서로 단위가 다르므로 초기화.
        # 같은 snapshot을 즉시 다시 그려서 새 MQTT 수신을 기다리지 않아도 된다.
        auto_y()
        update_y_range_unit_labels()
        if current_snapshot:
            apply_snapshot(current_snapshot, force_full=False)

    if mode == "PIEZO":
        # ON/OFF 스위치는 기존 표시 단위 콤보와 동기화한다.
        # 사용자가 기존 단위 선택을 바꾸더라도 스위치 표시가 일치한다.
        def pp_calibration_toggled(checked):
            pp_settings.setValue("piezo_pp_enabled", bool(checked))
            target = "g_pp" if checked else "voltage"
            if piezo_unit_combo.currentData() != target:
                idx = piezo_unit_combo.findData(target)
                if idx >= 0:
                    piezo_unit_combo.setCurrentIndex(idx)
            pp_status.setText(
                ("ON | " if checked else "OFF | ")
                + f"기준: {piezo_pp_mv.value():g} mV P-P = {piezo_pp_g.value():g} g Peak"
            )
            mems_g_mode_changed()

        def pp_unit_combo_sync(_idx=None):
            active = piezo_unit_combo.currentData() == "g_pp"
            if pp_calibration_check.isChecked() != active:
                pp_calibration_check.blockSignals(True)
                pp_calibration_check.setChecked(active)
                pp_calibration_check.blockSignals(False)
            pp_settings.setValue("piezo_pp_enabled", bool(active))
            pp_status.setText(
                ("ON | " if active else "OFF | ")
                + f"기준: {piezo_pp_mv.value():g} mV P-P = {piezo_pp_g.value():g} g Peak"
            )

        pp_calibration_check.toggled.connect(pp_calibration_toggled)
        piezo_unit_combo.currentIndexChanged.connect(pp_unit_combo_sync)
        saved_unit_index = piezo_unit_combo.findData(
            "voltage"
        )
        if saved_unit_index >= 0:
            piezo_unit_combo.setCurrentIndex(saved_unit_index)

        def pp_settings_changed(_value=None):
            pp_settings.setValue("piezo_pp_mv", float(piezo_pp_mv.value()))
            pp_settings.setValue("piezo_pp_g", float(piezo_pp_g.value()))
            pp_status.setText(
                f"{'ON' if pp_calibration_check.isChecked() else 'OFF'} | "
                f"기준: {piezo_pp_mv.value():g} mV P-P = "
                f"{piezo_pp_g.value():g} g Peak | 정현파 가정"
            )
            if piezo_unit_combo.currentData() == "g_pp":
                mems_g_mode_changed()
        piezo_pp_mv.valueChanged.connect(pp_settings_changed)
        piezo_pp_g.valueChanged.connect(pp_settings_changed)
        def pp_reset_defaults():
            piezo_pp_mv.setValue(12.0)
            piezo_pp_g.setValue(1.0)
        pp_reset.clicked.connect(pp_reset_defaults)
        pp_settings_changed()

    if mode == "PIEZO":
        refresh_piezo_formula()

    if mode == "ADXL":
        mems_raw_unit_combo.currentIndexChanged.connect(mems_g_mode_changed)
        mems_fft_unit_combo.currentIndexChanged.connect(mems_g_mode_changed)
        def mems_scale_changed(value):
            try:
                save_mems_g_per_lsb(value)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(window, "MEMS 환산", str(exc))
                return
            mems_scale_label.setText(f"= {1.0 / value:.7g} LSB/g")
            mems_g_mode_changed()
        mems_g_per_lsb_spin.valueChanged.connect(mems_scale_changed)
    if mode == "ADXL":
        def apply_mems_correction_settings():
            nonlocal mems_correction_values
            try:
                values = {}
                for key, control in mems_correction_controls.items():
                    values[key] = control.isChecked() if key.endswith("_enabled") else control.value()
                mems_correction_values = save_mems_correction(values)
            except (ValueError, OSError) as exc:
                QtWidgets.QMessageBox.warning(window, "MEMS FFT 보정", str(exc))
                return
            mems_g_mode_changed()
        correction_btn.clicked.connect(apply_mems_correction_settings)
    if piezo_unit_combo is not None:
        piezo_unit_combo.currentIndexChanged.connect(mems_g_mode_changed)
        piezo_ref_voltage.valueChanged.connect(device_voltage_changed)
        piezo_ref_voltage.valueChanged.connect(mems_g_mode_changed)
        piezo_adc_denominator.valueChanged.connect(mems_g_mode_changed)

    full_btn.clicked.connect(
        lambda: apply_snapshot(
            current_snapshot,
            force_full=True,
        )
    )

    def current_fft_window_settings():
        selected = normalize_fft_window_name(
            fft_window_combo.currentData()
            or fft_window_combo.currentText()
            or "Blackman"
        )

        beta = float(kaiser_beta_spin.value())
        return selected, beta

    def send_fft_window_settings_to_parent():
        selected, beta = current_fft_window_settings()

        try:
            command_queue.put_nowait(
                {
                    "type": "fft_window",
                    "mode": mode,
                    "window": selected,
                    "beta": beta,
                }
            )
        except Exception:
            pass

    def fft_window_changed(_index=None):
        selected, _beta = current_fft_window_settings()

        is_kaiser = selected == "Kaiser"
        kaiser_beta_label.setEnabled(is_kaiser)
        kaiser_beta_spin.setEnabled(is_kaiser)

        # 이후 새 패킷에도 같은 Window/Beta 적용.
        send_fft_window_settings_to_parent()

        # 이미 수신된 Raw sample도 즉시 다시 FFT 계산.
        if current_snapshot:
            apply_snapshot(
                current_snapshot,
                force_full=False,
            )

    def kaiser_beta_changed(_value=None):
        selected, _beta = current_fft_window_settings()

        # Kaiser가 아닌 경우 Beta 변경은 FFT에 영향 없음.
        if selected != "Kaiser":
            return

        send_fft_window_settings_to_parent()

        if current_snapshot:
            apply_snapshot(
                current_snapshot,
                force_full=False,
            )

    fft_window_combo.currentIndexChanged.connect(
        fft_window_changed
    )

    kaiser_beta_spin.valueChanged.connect(
        kaiser_beta_changed
    )

    def fft_window_size_changed(_index=None):
        size = selected_fft_window_size()

        try:
            command_queue.put_nowait(
                {
                    "type": "fft_window_size",
                    "mode": mode,
                    "window_size": int(size),
                    "sample_count": int(configured_sample_count),
                }
            )
        except Exception:
            pass

        if current_snapshot:
            apply_snapshot(
                current_snapshot,
                force_full=False,
            )

    fft_window_size_combo.currentIndexChanged.connect(
        fft_window_size_changed
    )


    def copy_piezo_auto_mean_to_manual():
        if mode != "PIEZO":
            return
        device_id = str((current_snapshot or {}).get("device_id", current_device)).strip()
        mean = piezo_latest_packet_mean_by_device.get(device_id)
        if mean is None or not np.isfinite(mean):
            QtWidgets.QMessageBox.information(window, "PIEZO 자동 0점", "먼저 해당 센서의 RAW 패킷을 수신해야 합니다.")
            return
        piezo_zero_edit.setValue(float(mean))
        piezo_zero_status.setText("자동 평균값을 입력란에 복사했습니다. '0점 적용'을 누르면 수동 고정됩니다.")

    if piezo_copy_auto_btn is not None:
        piezo_copy_auto_btn.clicked.connect(copy_piezo_auto_mean_to_manual)

    def apply_piezo_zero_offset():
        if mode != "PIEZO":
            return

        device_id = str(
            (
                current_snapshot or {}
            ).get(
                "device_id",
                current_device,
            )
        ).strip()

        if not device_id:
            QtWidgets.QMessageBox.warning(
                window,
                "PIEZO 0점",
                "선택된 PIEZO 센서가 없습니다.",
            )
            return

        try:
            offset = float(piezo_zero_edit.value())
        except Exception:
            QtWidgets.QMessageBox.warning(
                window,
                "PIEZO 0점",
                "올바른 Raw count 값을 입력하세요.",
            )
            return

        if not np.isfinite(offset):
            QtWidgets.QMessageBox.warning(
                window,
                "PIEZO 0점",
                "올바른 Raw count 값을 입력하세요.",
            )
            return

        piezo_zero_offsets[device_id] = offset
        piezo_zero_mode_by_device[device_id] = "manual"
        piezo_zero_mode_combo.blockSignals(True)
        piezo_zero_mode_combo.setCurrentIndex(piezo_zero_mode_combo.findData("manual"))
        piezo_zero_mode_combo.blockSignals(False)

        auto_y()
        refresh_piezo_zero_status(device_id)

        try:
            command_queue.put_nowait(
                {
                    "type": "piezo_zero_offset",
                    "device_id": device_id,
                    "offset": offset,
                }
            )
        except Exception:
            pass

        if current_snapshot:
            apply_snapshot(
                current_snapshot,
                force_full=False,
            )

    def reset_piezo_zero_offset():
        if mode != "PIEZO":
            return

        device_id = str(
            (
                current_snapshot or {}
            ).get(
                "device_id",
                current_device,
            )
        ).strip()

        piezo_zero_offsets[device_id] = PIEZO_DEFAULT_ZERO_COUNT
        piezo_zero_mode_by_device[device_id] = "manual"
        piezo_zero_mode_combo.blockSignals(True)
        piezo_zero_mode_combo.setCurrentIndex(piezo_zero_mode_combo.findData("manual"))
        piezo_zero_mode_combo.blockSignals(False)
        piezo_zero_edit.setValue(PIEZO_DEFAULT_ZERO_COUNT)

        auto_y()
        refresh_piezo_zero_status(device_id)

        try:
            command_queue.put_nowait(
                {
                    "type": "piezo_zero_reset",
                    "device_id": device_id,
                }
            )
        except Exception:
            pass

        if current_snapshot:
            apply_snapshot(
                current_snapshot,
                force_full=False,
            )

    def change_piezo_zero_mode(_idx=None):
        if mode != "PIEZO":
            return
        device_id = str((current_snapshot or {}).get("device_id", current_device)).strip()
        if not device_id:
            refresh_piezo_zero_status()
            return
        selected = str(piezo_zero_mode_combo.currentData())
        piezo_zero_mode_by_device[device_id] = selected
        if selected == "manual" and device_id not in piezo_zero_offsets:
            piezo_zero_offsets[device_id] = PIEZO_DEFAULT_ZERO_COUNT
        auto_y()
        refresh_piezo_zero_status(device_id)
        if current_snapshot:
            apply_snapshot(current_snapshot, force_full=False)

    if piezo_zero_mode_combo is not None:
        piezo_zero_mode_combo.currentIndexChanged.connect(change_piezo_zero_mode)

    if piezo_zero_apply_btn is not None:
        piezo_zero_apply_btn.clicked.connect(
            apply_piezo_zero_offset
        )

    if piezo_zero_reset_btn is not None:
        piezo_zero_reset_btn.clicked.connect(
            reset_piezo_zero_offset
        )

    def selected_calibration_axis():
        return "Z"

    def calibrate_current_g_rms():
        """
        현재 선택 축의 AC Raw RMS를 사용자가 입력한 기준 진동(g RMS)과
        대응시켜 count/g 보정계수를 구한다.

        counts_per_g = Raw_RMS_count / Reference_G_RMS

        PIEZO: Z
        MEMS : X / Y / Z 각각 별도 보정
        """
        if mode != "PIEZO":
            return

        sensor_name = "PIEZO"

        if not current_snapshot:
            QtWidgets.QMessageBox.warning(
                window,
                "G RMS 보정",
                f"아직 수신된 {sensor_name} 데이터가 없습니다.",
            )
            return

        device_id = str(
            current_snapshot.get(
                "device_id",
                current_device,
            )
        )
        axis = selected_calibration_axis()

        rec = current_snapshot.get(
            "axes",
            {},
        ).get(axis)

        if rec is None:
            QtWidgets.QMessageBox.warning(
                window,
                "G RMS 보정",
                f"{sensor_name} {axis}축 데이터가 없습니다.",
            )
            return

        samples = np.asarray(
            rec.get("samples", []),
            dtype=np.float64,
        )

        if len(samples) < 2:
            QtWidgets.QMessageBox.warning(
                window,
                "G RMS 보정",
                "보정할 샘플이 부족합니다.",
            )
            return

        try:
            ref_g_rms = float(
                reference_g_rms_edit.value()
                if reference_g_rms_edit is not None
                else 1.0
            )
        except Exception:
            ref_g_rms = 1.0

        if (
            not np.isfinite(ref_g_rms)
            or ref_g_rms <= 0
        ):
            QtWidgets.QMessageBox.warning(
                window,
                "G RMS 보정",
                "시험기 기준 진동(RMS)은 0보다 큰 값이어야 합니다.",
            )
            return

        raw_mean = float(np.mean(samples))
        centered = samples - raw_mean
        raw_rms = float(
            np.sqrt(np.mean(centered ** 2))
        )

        if (
            not np.isfinite(raw_rms)
            or raw_rms <= 0
        ):
            QtWidgets.QMessageBox.warning(
                window,
                "G RMS 보정",
                "Raw RMS가 0이거나 유효하지 않습니다.",
            )
            return

        counts_per_g = raw_rms / ref_g_rms

        calibrations[(device_id, axis)] = {
            "counts_per_g": counts_per_g,
            "raw_mean_ref": raw_mean,
            "ref_g_rms": ref_g_rms,
        }
        if piezo_unit_combo is not None:
            piezo_unit_combo.setCurrentIndex(piezo_unit_combo.findData("g_spec"))

        # count 기준 Y축 값을 고정해 둔 상태라면 g 변환 후 범위가 맞지 않으므로
        # 보정 변경 시 자동 Y축으로 복귀.
        auto_y()
        refresh_calibration_status(device_id)

        try:
            command_queue.put_nowait(
                {
                    "type": "calibration",
                    "sensor_mode": sensor_name,
                    "device_id": device_id,
                    "axis": axis,
                    "ref_g_rms": ref_g_rms,
                    "counts_per_g": counts_per_g,
                    "raw_rms_count": raw_rms,
                    "raw_mean_ref": raw_mean,
                    "calibration_complete": is_calibration_complete(
                        device_id
                    ),
                }
            )
        except Exception:
            pass

        apply_snapshot(
            current_snapshot,
            force_full=False,
        )

    def reset_selected_calibration():
        if mode != "PIEZO":
            return

        device_id = str(
            (
                current_snapshot or {}
            ).get(
                "device_id",
                current_device,
            )
        )
        axis = selected_calibration_axis()

        calibrations.pop(
            (device_id, axis),
            None,
        )

        auto_y()
        refresh_calibration_status(device_id)

        try:
            command_queue.put_nowait(
                {
                    "type": "calibration_reset",
                    "sensor_mode": (
                        "MEMS"
                        if mode == "ADXL"
                        else "PIEZO"
                    ),
                    "device_id": device_id,
                    "axis": axis,
                }
            )
        except Exception:
            pass

        if current_snapshot:
            apply_snapshot(
                current_snapshot,
                force_full=False,
            )

    def reset_all_calibrations():
        reset_selected_calibration()

    if calibration_btn is not None:
        calibration_btn.clicked.connect(
            calibrate_current_g_rms
        )

    if calibration_reset_btn is not None:
        calibration_reset_btn.clicked.connect(
            reset_selected_calibration
        )

    if calibration_reset_all_btn is not None:
        calibration_reset_all_btn.clicked.connect(
            reset_all_calibrations
        )

    if calibration_axis_combo is not None:
        calibration_axis_combo.currentTextChanged.connect(
            lambda _t: refresh_calibration_status(
                (
                    current_snapshot or {}
                ).get(
                    "device_id",
                    current_device,
                )
            )
        )

    def handle_devices(devices):
        devices = [str(x) for x in devices]
        # V017: 주기적 목록 재전송 시 이미 같은 목록이면 선택/화면을 초기화하지 않는다.
        existing = [combo.itemText(i) for i in range(combo.count())]
        if existing == devices:
            return
        old = combo.currentText().strip()
        combo.blockSignals(True)
        combo.clear(); combo.addItems(devices)
        if old and old in devices:
            combo.setCurrentText(old)
        elif devices:
            combo.setCurrentIndex(0)
        combo.blockSignals(False)
        send_selection()

    def poll():
        nonlocal configured_sample_rate
        latest = None
        try:
            while True:
                msg = data_queue.get_nowait()
                typ = msg.get("type")
                if typ == "devices":
                    handle_devices(list(msg.get("devices", [])))

                elif typ == "sensor_config":
                    try:
                        configured_sample_rate = float(
                            msg.get(
                                "sample_rate",
                                configured_sample_rate,
                            )
                        )
                    except Exception:
                        pass

                    try:
                        new_count = int(
                            msg.get(
                                "sample_count",
                                configured_sample_count,
                            )
                        )
                    except Exception:
                        new_count = configured_sample_count

                    set_fft_window_size_options(
                        new_count,
                        reset_to_sample_count=True,
                    )

                    if current_snapshot:
                        apply_snapshot(
                            current_snapshot,
                            force_full=False,
                        )

                elif typ == "snapshot":
                    latest = msg
                elif typ == "raise":
                    window.showNormal(); window.raise_(); window.activateWindow()
                elif typ == "close":
                    window.close(); return
        except queue.Empty:
            pass
        except Exception as exc:
            print(f"[V018] 그래프 목록/큐 갱신 오류: {type(exc).__name__}: {exc}", file=sys.stderr)
        if latest is not None:
            try:
                apply_snapshot(latest)
            except Exception as exc:
                print(f"[V018] 그래프 렌더링 오류: {type(exc).__name__}: {exc}", file=sys.stderr)

    timer = QtCore.QTimer()
    timer.timeout.connect(poll)
    timer.start(50)

    def close_event(event):
        try:
            command_queue.put_nowait({"type": "closed"})
        except Exception:
            pass
        event.accept()

    window.closeEvent = close_event
    window.show()
    try:
        app.exec()
    except AttributeError:
        app.exec_()


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1200x760")
        self.root.minsize(940, 600)

        self.client = None
        self.serial_conn = None
        self.serial_thread = None
        self.serial_stop = threading.Event()
        self.connected = False
        self.active_transport = None
        self.measuring = False
        self.stats = Stats()
        self.ui_queue = queue.Queue()

        # 자동 감지 센서 목록
        self.sensor_registry = SensorRegistry()

        # CSV 저장은 통신 callback과 분리
        self.csv_root = get_app_dir() / "data_csv"
        self.csv_session_dir = None
        self.csv_enabled = tk.BooleanVar(value=True)
        self.csv_folder_var = tk.StringVar(value=str(self.csv_root))
        self.csv_queue = queue.Queue()
        self.csv_stop = threading.Event()
        self.csv_counter_lock = threading.Lock()
        self.csv_saved_count = 0
        self.csv_error_count = 0

        # ADXL CSV는 X/Y/Z 3축이 모두 들어온 경우에만 저장한다.
        # key: (source, device_id)
        # value: {"X": CSVItem, "Y": CSVItem}
        #
        # X 수신: 새 세트 시작. 기존 미완성 세트는 폐기.
        # Y 수신: X가 있을 때만 보관.
        # Z 수신: X/Y가 모두 있을 때만 X,Y,Z를 저장 Queue에 넣는다.
        self.adxl_csv_pending = {}
        self.adxl_csv_pending_lock = threading.Lock()

        self.csv_thread = threading.Thread(
            target=self._csv_writer_loop,
            daemon=True,
            name="vibration-csv-writer",
        )
        self.csv_thread.start()

        # ADXL Graph/FFT는 MQTT callback과 분리
        self.graph_queue = queue.Queue()
        self.graph_stop = threading.Event()
        self.graph_thread = threading.Thread(
            target=self._graph_decode_loop,
            daemon=True,
            name="adxl-graph-decode",
        )
        self.adxl_store = ADXLLatestStore()
        self.piezo_store = ADXLLatestStore()
        self.ultrasonic_store = UltrasonicLatestStore()
        # V014: 그래프를 열지 않아도 최초 정상 PIEZO 수신 시 기본 전압을 저장.
        self.piezo_device_voltage_path = get_app_dir() / PIEZO_DEVICE_VOLTAGE_FILENAME
        self.piezo_device_voltage_known = set(load_piezo_device_voltages(self.piezo_device_voltage_path))

        # PIEZO / MEMS 각각 독립적인 FFT Window 선택값.
        # PyQtGraph 창에서 변경하면 _pg_poll_commands()를 통해 갱신된다.
        self.fft_window_by_mode = {
            "ADXL": "Blackman",
            "PIEZO": "Blackman",
        }

        self.fft_window_beta_by_mode = {
            "ADXL": 8.6,
            "PIEZO": 8.6,
        }

        # 실제 FFT에 적용 중인 Fs / Sample Count / Window Size.
        # START 또는 설정 적용 성공 시:
        #   Sample Count = Window Size = FFT N
        self.fft_sample_rate_by_mode = {
            "PIEZO": float(PIEZO_SAMPLE_RATES[-1]),
            "ADXL": float(ADXL_SAMPLE_RATES[-1]),
        }
        self.fft_sample_count_by_mode = {
            "PIEZO": int(SAMPLE_COUNTS[-1]),
            "ADXL": int(SAMPLE_COUNTS[-1]),
        }
        self.fft_window_size_by_mode = {
            "PIEZO": int(SAMPLE_COUNTS[-1]),
            "ADXL": int(SAMPLE_COUNTS[-1]),
        }

        self.graph_thread.start()

        self.graph_window = None
        self.piezo_graph_window = None
        self.piezo_graph_device_var = None
        self.piezo_graph_canvas_raw = None
        self.piezo_graph_canvas_fft = None
        self.piezo_graph_info_var = None
        self.piezo_graph_last_version = -1

        # ULTRASONIC H PyQtGraph viewer state
        self.pg_ultrasonic_process = None
        self.pg_ultrasonic_data_queue = None
        self.pg_ultrasonic_command_queue = None
        self.pg_ultrasonic_selected = ""
        self.pg_ultrasonic_last_devices = []
        self.pg_ultrasonic_last_version = -1
        self.pg_ultrasonic_error_shown = False

        # 그래프 확대/축소/이동 상태
        # key: adxl_raw / adxl_fft / piezo_raw / piezo_fft
        self.graph_view_states = {}
        self.graph_device_var = None
        self.graph_canvases = {}
        self.graph_info_var = None
        self.graph_last_version = -1

        # FFT 진단 로그는 센서/축마다 너무 많이 쌓이지 않도록
        # 약 1초 간격으로 시스템 로그에 출력한다.
        self.fft_diag_log_interval_sec = 1.0
        self.fft_diag_last_log = {}

        # 그래프 Y축 사용자 고정값.
        # Raw/FFT는 단위가 다르므로 각각 별도 범위를 사용한다.
        # 최소/최대가 모두 빈칸이면 자동 스케일.
        self.adxl_raw_y_min_var = tk.StringVar(value="")
        self.adxl_raw_y_max_var = tk.StringVar(value="")
        self.adxl_fft_y_min_var = tk.StringVar(value="")
        self.adxl_fft_y_max_var = tk.StringVar(value="")
        self.piezo_raw_y_min_var = tk.StringVar(value="")
        self.piezo_raw_y_max_var = tk.StringVar(value="")
        self.piezo_fft_y_min_var = tk.StringVar(value="")
        self.piezo_fft_y_max_var = tk.StringVar(value="")

        self.config_window = None
        self.config_target_listbox = None
        self.config_target_summary_var = None
        self.command_target_ids = set()

        # 센서 설정값.
        # 각 센서의 마지막 프로필이 기본값:
        # PIEZO 25600 Hz + 8192
        # ADXL  3200 Hz + 8192
        self.cfg_period = tk.StringVar(value="10")
        # 실제 ADXL 센서에 전송할 측정 범위 (표시용 LSB/g 보정과 별개)
        self.cfg_mems_g_range = tk.StringVar(value="16")
        self.cfg_piezo_gain = tk.StringVar(value="30")

        self.sample_profile_state = {
            "PIEZO": PIEZO_PROFILE_LABELS[-1],
            "ADXL": ADXL_PROFILE_LABELS[-1],
        }
        self.cfg_sample_profile = tk.StringVar(
            value=self.sample_profile_state["ADXL"]
        )
        self._last_sensor_type = "ADXL"

        # 수신 Thread에서는 Tk StringVar를 직접 읽지 않고
        # 이 일반 Python 값으로 현재 설정 Fs/N을 참조한다.
        self.graph_sample_defaults = {
            "PIEZO": (
                float(PIEZO_SAMPLE_RATES[-1]),
                int(SAMPLE_COUNTS[-1]),
            ),
            "ADXL": (
                float(ADXL_SAMPLE_RATES[-1]),
                int(SAMPLE_COUNTS[-1]),
            ),
        }

        self.config_axis_info_var = tk.StringVar(value="")

        # 현재 센서 설정 조회 상태(MQTT S/{MAC} + {"CONFIG":"?"})
        self.pending_config_query_device = None
        self.config_read_status_var = tk.StringVar(
            value="현재 설정 조회 대기"
        )

        self._build_ui()
        self.root.after(UI_REFRESH_MS, self._refresh)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self):
        conn = ttk.LabelFrame(self.root, text="통신 연결")
        conn.pack(fill="x", padx=10, pady=(10, 5))

        self.mode = tk.StringVar(value="MQTT")
        self.host = tk.StringVar(value=DEFAULT_BROKER)
        self.port = tk.StringVar(value=str(DEFAULT_PORT))
        self.topic = tk.StringVar(value=DEFAULT_TOPIC)
        self.qos = tk.IntVar(value=DEFAULT_QOS)
        self.serial_port = tk.StringVar(value="")
        self.serial_baud = tk.StringVar(value=str(DEFAULT_SERIAL_BAUD))

        # 센서 동작 선택은 통신 방식과 완전히 별개.
        self.run_sensor_type = tk.StringVar(value="ADXL")
        self.cfg_sensor_type_code = tk.StringVar(value="2")

        # -------------------------------------------------------------
        # 1. 통신 방식 선택
        # -------------------------------------------------------------
        mode_frame = ttk.Frame(conn)
        mode_frame.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Label(mode_frame, text="통신 방식").pack(side="left", padx=(0, 6))

        self.mode_combo = ttk.Combobox(
            mode_frame,
            textvariable=self.mode,
            values=["MQTT", "SERIAL"],
            width=12,
            state="readonly",
        )
        self.mode_combo.pack(side="left", padx=4)
        self.mode_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._update_transport_ui(),
        )

        self.btn_connect = ttk.Button(
            mode_frame,
            text="연결",
            command=self.connect,
        )
        self.btn_connect.pack(side="left", padx=(18, 5))

        self.btn_disconnect = ttk.Button(
            mode_frame,
            text="연결 해제",
            command=self.disconnect,
            state="disabled",
        )
        self.btn_disconnect.pack(side="left", padx=5)

        ttk.Label(mode_frame, text="상태").pack(side="left", padx=(20, 5))
        self.status = tk.StringVar(value="DISCONNECTED")
        ttk.Label(
            mode_frame,
            textvariable=self.status,
            width=20,
        ).pack(side="left", padx=5)

        ttk.Label(
            mode_frame,
            text="선택한 통신 방식의 설정 영역만 편집 가능",
        ).pack(side="right", padx=8)

        # -------------------------------------------------------------
        # 2. MQTT 설정 + 센서 설정/동작
        # -------------------------------------------------------------
        self.mqtt_frame = ttk.LabelFrame(
            conn,
            text="MQTT 설정 / 센서 설정 · 동작",
        )
        self.mqtt_frame.pack(fill="x", padx=8, pady=4)

        # MQTT 접속 정보
        ttk.Label(self.mqtt_frame, text="Broker").grid(
            row=0, column=0, padx=(8, 4), pady=3, sticky="e"
        )
        self.host_entry = ttk.Entry(
            self.mqtt_frame,
            textvariable=self.host,
            width=20,
        )
        self.host_entry.grid(
            row=0, column=1, padx=4, pady=3, sticky="w"
        )

        ttk.Label(self.mqtt_frame, text="Port").grid(
            row=0, column=2, padx=(12, 4), pady=3, sticky="e"
        )
        self.port_entry = ttk.Entry(
            self.mqtt_frame,
            textvariable=self.port,
            width=8,
        )
        self.port_entry.grid(
            row=0, column=3, padx=4, pady=3, sticky="w"
        )

        ttk.Label(self.mqtt_frame, text="구독 Topic(s)").grid(
            row=0, column=4, padx=(12, 4), pady=3, sticky="e"
        )
        self.topic_entry = ttk.Entry(
            self.mqtt_frame,
            textvariable=self.topic,
            width=30,
        )
        self.topic_entry.grid(
            row=0, column=5, padx=4, pady=3, sticky="w"
        )

        # 구독 및 일반 송신 QoS 선택 (기본값 1).
        # 모드 A 설정 명령의 QoS는 규격에 따라 별도로 1로 고정한다.
        ttk.Label(self.mqtt_frame, text="QoS").grid(
            row=0, column=6, padx=(12, 4), pady=3, sticky="e"
        )
        self.qos_combo = ttk.Combobox(
            self.mqtt_frame,
            textvariable=self.qos,
            values=[0, 1, 2],
            width=6,
            state="readonly",
        )
        self.qos_combo.grid(row=0, column=7, padx=4, pady=3, sticky="w")

        ttk.Label(
            self.mqtt_frame,
            text=(
                "기본 구독 Topic = V/#,D/#  (콤마로 복수 지정) |  "
                "MQTT Retained 설정 Topic = S/{선택한 센서 MAC}"
            ),
        ).grid(
            row=1,
            column=0,
            columnspan=8,
            padx=8,
            pady=(0, 6),
            sticky="w",
        )

        # ---------------------------------------------------------
        # MQTT 센서 MAC + 센서 설정/동작
        # ---------------------------------------------------------
        inline = ttk.Frame(self.mqtt_frame)
        inline.grid(
            row=2,
            column=0,
            columnspan=8,
            sticky="nsew",
            padx=6,
            pady=(1, 4),
        )
        inline.columnconfigure(0, weight=1)
        inline.columnconfigure(1, weight=1)

        # 왼쪽: 수신된 센서 MAC 목록
        target = ttk.LabelFrame(
            inline,
            text="설정 대상 센서 MAC (dvicMacAddr)",
        )
        target.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(0, 6),
        )

        list_frame = ttk.Frame(target)
        list_frame.pack(
            fill="both",
            expand=True,
            padx=5,
            pady=(4, 2),
        )

        target_scroll = ttk.Scrollbar(
            list_frame,
            orient="vertical",
        )
        target_scroll.pack(side="right", fill="y")

        self.config_target_listbox = tk.Listbox(
            list_frame,
            selectmode="extended",
            height=4,
            exportselection=False,
            yscrollcommand=target_scroll.set,
        )
        self.config_target_listbox.pack(
            side="left",
            fill="both",
            expand=True,
        )
        target_scroll.configure(
            command=self.config_target_listbox.yview
        )

        self.config_target_listbox.bind(
            "<<ListboxSelect>>",
            lambda _e: self._sync_config_target_selection(),
        )

        target_buttons = ttk.Frame(target)
        target_buttons.pack(
            fill="x",
            padx=5,
            pady=(0, 3),
        )

        self.btn_config_refresh = ttk.Button(
            target_buttons,
            text="목록 새로고침",
            command=self._refresh_config_devices,
        )
        self.btn_config_refresh.pack(
            side="left",
            padx=(0, 4),
        )

        self.btn_config_select_all = ttk.Button(
            target_buttons,
            text="전체 선택",
            command=self._select_all_config_devices,
        )
        self.btn_config_select_all.pack(
            side="left",
            padx=4,
        )

        self.btn_config_clear = ttk.Button(
            target_buttons,
            text="선택 해제",
            command=self._clear_config_devices,
        )
        self.btn_config_clear.pack(
            side="left",
            padx=4,
        )

        self.config_target_summary_var = tk.StringVar(
            value="0개 선택"
        )
        ttk.Label(
            target_buttons,
            textvariable=self.config_target_summary_var,
        ).pack(side="right", padx=4)

        ttk.Label(
            target,
            text=(
                "MQTT: 선택한 각 MAC의 S/{MAC} retained 설정을 갱신  |  "
                "Serial: MAC 선택 미사용"
            ),
        ).pack(
            fill="x",
            padx=5,
            pady=(0, 3),
        )

        # 오른쪽: 센서 종류 / 측정주기 / START·STOP / 설정 적용
        sensor_cfg = ttk.LabelFrame(
            inline,
            text="센서 설정 / 동작",
        )
        sensor_cfg.grid(
            row=0,
            column=1,
            sticky="nsew",
            padx=(6, 0),
        )

        # 센서 설정을 한 화면에서 모두 볼 수 있도록 세로 공간을 압축.
        ttk.Label(sensor_cfg, text="종류").grid(
            row=0, column=0, padx=(6, 3), pady=3, sticky="e"
        )

        self.run_sensor_combo = ttk.Combobox(
            sensor_cfg,
            textvariable=self.run_sensor_type,
            values=["PIEZO", "ADXL"],
            width=10,
            state="readonly",
        )
        self.run_sensor_combo.grid(
            row=0, column=1, padx=3, pady=3, sticky="w"
        )
        self.run_sensor_combo.bind(
            "<<ComboboxSelected>>",
            self._on_sensor_type_changed,
        )

        ttk.Label(sensor_cfg, text="주기(s)").grid(
            row=0, column=2, padx=(8, 3), pady=3, sticky="e"
        )

        self.cfg_period_entry = ttk.Entry(
            sensor_cfg,
            textvariable=self.cfg_period,
            width=8,
        )
        self.cfg_period_entry.grid(
            row=0, column=3, padx=3, pady=3, sticky="w"
        )

        ttk.Label(sensor_cfg, text="Fs + N").grid(
            row=0, column=4, padx=(8, 3), pady=3, sticky="e"
        )

        self.cfg_sample_profile_combo = ttk.Combobox(
            sensor_cfg,
            textvariable=self.cfg_sample_profile,
            values=ADXL_PROFILE_LABELS,
            width=21,
            state="readonly",
        )
        self.cfg_sample_profile_combo.grid(
            row=0, column=5, padx=(3, 6), pady=3, sticky="w"
        )

        # 2행: 센서 타입 / G Range / PIEZO Gain
        settings_row = ttk.Frame(sensor_cfg)
        settings_row.grid(
            row=1, column=0, columnspan=6,
            padx=6, pady=(2, 3), sticky="w",
        )

        ttk.Label(settings_row, text="센서 타입").pack(side="left", padx=(0, 4))
        self.cfg_sensortype_combo = ttk.Combobox(
            settings_row,
            textvariable=self.cfg_sensor_type_code,
            values=("0", "1", "2", "3", "4"),
            width=5,
            state="readonly",
        )
        self.cfg_sensortype_combo.pack(side="left", padx=(0, 14))
        self.cfg_sensortype_combo.bind(
            "<<ComboboxSelected>>", self._on_sensortype_code_changed
        )
        self.cfg_sensortype_desc = tk.StringVar(value="2: MEMS normal")

        ttk.Label(settings_row, text="G Range").pack(side="left", padx=(0, 4))
        self.cfg_mems_g_range_combo = ttk.Combobox(
            settings_row,
            textvariable=self.cfg_mems_g_range,
            values=("2", "4", "8", "16"),
            width=6,
            state="readonly",
        )
        self.cfg_mems_g_range_combo.pack(side="left", padx=(0, 14))

        ttk.Label(settings_row, text="게인").pack(side="left", padx=(0, 4))
        self.cfg_piezo_gain_entry = ttk.Entry(
            settings_row, textvariable=self.cfg_piezo_gain, width=8
        )
        self.cfg_piezo_gain_entry.pack(side="left", padx=(0, 10))
        self.cfg_piezo_gain_entry.configure(state="disabled")

        # 3행: 동작 버튼만 모아 배치
        action_row = ttk.Frame(sensor_cfg)
        action_row.grid(
            row=2, column=0, columnspan=6,
            padx=6, pady=(1, 3), sticky="w",
        )
        self.btn_sensor_start = ttk.Button(
            action_row,
            text="START",
            command=self.send_selected_sensor_start,
            state="disabled",
        )
        self.btn_sensor_start.pack(side="left", padx=(0, 5))

        self.btn_sensor_stop = ttk.Button(
            action_row,
            text="STOP",
            command=lambda: self.send_start_command("0"),
            state="disabled",
        )
        self.btn_sensor_stop.pack(side="left", padx=(0, 5))

        self.btn_config_apply = ttk.Button(
            action_row,
            text="설정 적용",
            command=self.send_ap_config,
            state="disabled",
        )
        self.btn_config_apply.pack(side="left", padx=(0, 5))

        # 현재 설정 조회는 UI에서 숨김. 다른 코드에서 참조하므로 객체는 유지.
        # 필요 시 아래 버튼의 pack()을 다시 활성화할 수 있음.
        self.btn_config_read = ttk.Button(
            action_row,
            text="현재 설정 조회",
            command=self.request_current_sensor_config,
            state="disabled",
        )
        # self.btn_config_read.pack(side="left", padx=4)

        # 4~5행: 설명 문구 2줄 (현재 모드 / 선택 프로필의 분석 기준)
        ttk.Label(
            sensor_cfg,
            textvariable=self.cfg_sensortype_desc,
            justify="left",
        ).grid(
            row=3, column=0, columnspan=6,
            padx=6, pady=(1, 1), sticky="w",
        )
        ttk.Label(
            sensor_cfg,
            textvariable=self.config_axis_info_var,
            wraplength=720,
            justify="left",
        ).grid(
            row=4, column=0, columnspan=6,
            padx=6, pady=(0, 3), sticky="w",
        )

        # 현재 설정 조회 기능을 숨겨 조회 상태 설명 줄도 표시하지 않음.
        # 필요 시 self.config_read_status_var Label을 다시 활성화.
        preview_frame = ttk.LabelFrame(
            sensor_cfg,
            text="전송 JSON",
        )
        preview_frame.grid(
            row=5, column=0, columnspan=6,
            padx=6, pady=(2, 5), sticky="nsew",
        )

        self.config_preview = tk.Text(
            preview_frame,
            height=3,
            width=58,
            wrap="word",
        )
        self.config_preview.pack(
            fill="both",
            expand=True,
            padx=4,
            pady=3,
        )

        self.cfg_period.trace_add(
            "write",
            lambda *_args: self._update_config_preview(),
        )
        self.cfg_mems_g_range.trace_add(
            "write",
            lambda *_args: self._update_config_preview(),
        )
        self.cfg_piezo_gain.trace_add(
            "write",
            lambda *_args: self._update_config_preview(),
        )
        self.cfg_sample_profile.trace_add(
            "write",
            lambda *_args: self._on_sample_profile_changed(),
        )

        # -------------------------------------------------------------
        # 3. Serial 설정
        # -------------------------------------------------------------
        self.serial_frame = ttk.LabelFrame(conn, text="Serial 설정")
        self.serial_frame.pack(fill="x", padx=8, pady=4)

        ttk.Label(self.serial_frame, text="COM Port").grid(
            row=0, column=0, padx=(8, 4), pady=3, sticky="e"
        )
        self.serial_port_combo = ttk.Combobox(
            self.serial_frame,
            textvariable=self.serial_port,
            values=[],
            width=20,
            state="readonly",
        )
        self.serial_port_combo.grid(
            row=0, column=1, padx=4, pady=3, sticky="w"
        )

        self.btn_refresh_serial = ttk.Button(
            self.serial_frame,
            text="포트 새로고침",
            command=self.refresh_serial_ports,
        )
        self.btn_refresh_serial.grid(
            row=0, column=2, padx=8, pady=3, sticky="w"
        )

        ttk.Label(self.serial_frame, text="Baud").grid(
            row=0, column=3, padx=(14, 4), pady=3, sticky="e"
        )
        self.serial_baud_combo = ttk.Combobox(
            self.serial_frame,
            textvariable=self.serial_baud,
            values=[
                "9600",
                "19200",
                "38400",
                "57600",
                "115200",
                "230400",
                "460800",
                "921600",
            ],
            width=14,
            state="readonly",
        )
        self.serial_baud_combo.grid(
            row=0, column=4, padx=4, pady=3, sticky="w"
        )

        ttk.Label(
            self.serial_frame,
            text="Serial 설정 송신/수신은 현재 선택된 COM Port를 사용",
        ).grid(
            row=1,
            column=0,
            columnspan=5,
            padx=8,
            pady=(0, 6),
            sticky="w",
        )

        self.refresh_serial_ports()
        self._update_transport_ui()

        ctl = ttk.LabelFrame(self.root, text="그래프 / 저장")
        ctl.pack(fill="x", padx=10, pady=5)

        # v0.17:
        # 성능 측정용 START/STOP/통계 초기화 UI는 일단 화면에서 숨긴다.
        # 기존 내부 코드와의 호환성을 위해 버튼 객체만 생성하고 pack하지 않는다.
        self.btn_start = ttk.Button(
            ctl,
            text="측정 시작",
            command=self.start_measure,
            state="disabled",
        )
        self.btn_stop = ttk.Button(
            ctl,
            text="측정 중지",
            command=self.stop_measure,
            state="disabled",
        )

        ttk.Button(
            ctl,
            text="ADXL X/Y/Z + FFT 그래프",
            command=self.open_adxl_graph,
        ).pack(side="left", padx=(8, 6), pady=7)

        ttk.Button(
            ctl,
            text="PIEZO + FFT 그래프",
            command=self.open_piezo_graph,
        ).pack(side="left", padx=6, pady=7)

        ttk.Button(
            ctl,
            text="PIEZO + FFT 그래프 (SciPy 비교)",
            command=self.open_piezo_scipy_graph,
        ).pack(side="left", padx=6, pady=7)

        ttk.Button(
            ctl,
            text="초음파 H 그래프",
            command=self.open_ultrasonic_graph,
        ).pack(side="left", padx=6, pady=7)

        ttk.Checkbutton(
            ctl,
            text="CSV 저장 (HEX + Decoded 분리)",
            variable=self.csv_enabled,
            command=self._on_csv_toggle,
        ).pack(side="left", padx=(16, 4))

        ttk.Label(
            ctl,
            text=(
                "CSV 체크 시 연결 중 수신 데이터를 바로 저장 "
                "(측정 시작 버튼과 분리)"
            ),
        ).pack(side="left", padx=10)

        csv_frame = ttk.LabelFrame(self.root, text="CSV 저장")
        csv_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(csv_frame, text="저장 폴더").pack(
            side="left", padx=(8, 5), pady=6
        )
        ttk.Entry(
            csv_frame,
            textvariable=self.csv_folder_var,
            state="readonly",
        ).pack(side="left", fill="x", expand=True, padx=5, pady=6)

        self.csv_status_var = tk.StringVar(value="저장 0 | Queue 0 | 오류 0")
        ttk.Label(
            csv_frame,
            textvariable=self.csv_status_var,
            width=30,
        ).pack(side="right", padx=8)

        total = ttk.LabelFrame(self.root, text="전체 진동 수신 통계")
        # v0.17 UI 임시 숨김: 전체 진동 수신 통계
        # total.pack(fill="x", padx=10, pady=5)

        self.vars = {
            "elapsed": tk.StringVar(value="0.0 s"),
            "messages": tk.StringVar(value="0"),
            "data": tk.StringVar(value="0 B"),
            "cur_msg": tk.StringVar(value="0 msg/s"),
            "avg_msg": tk.StringVar(value="0 msg/s"),
            "cur_rate": tk.StringVar(value="0 KB/s"),
            "avg_rate": tk.StringVar(value="0 KB/s"),
            "ignored": tk.StringVar(value="0"),
            "errors": tk.StringVar(value="0"),
        }

        fields = [
            ("측정시간", "elapsed"),
            ("진동 메시지", "messages"),
            ("누적 데이터", "data"),
            ("현재 수신속도", "cur_msg"),
            ("평균 수신속도", "avg_msg"),
            ("현재 데이터율", "cur_rate"),
            ("평균 데이터율", "avg_rate"),
            ("제외(H/기타)", "ignored"),
            ("JSON 오류", "errors"),
        ]

        for i, (label, key) in enumerate(fields):
            row = i // 5
            col = (i % 5) * 2
            ttk.Label(total, text=label).grid(
                row=row, column=col, padx=(10, 4), pady=3, sticky="e"
            )
            ttk.Label(total, textvariable=self.vars[key], width=18).grid(
                row=row, column=col + 1, padx=(0, 10), pady=3, sticky="w"
            )

        sensor_frame = ttk.LabelFrame(self.root, text="센서별 수신 통계")
        # v0.17 UI 임시 숨김: 센서별 수신 통계
        # sensor_frame.pack(fill="x", padx=10, pady=5)

        cols = (
            "sensor", "messages", "payload", "hex",
            "avg_interval", "min_interval", "max_interval"
        )
        self.tree = ttk.Treeview(
            sensor_frame, columns=cols, show="headings", height=4
        )

        headers = [
            ("sensor", "구분", 110),
            ("messages", "메시지 수", 110),
            ("payload", "Payload 누적", 140),
            ("hex", "sensorData HEX", 140),
            ("avg_interval", "평균 간격(ms)", 130),
            ("min_interval", "최소 간격(ms)", 130),
            ("max_interval", "최대 간격(ms)", 130),
        ]
        for key, title, width in headers:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="center")

        self.tree.pack(fill="x", padx=8, pady=8)

        for name in ("PIEZO", "ADXL-X", "ADXL-Y", "ADXL-Z", "ULTRASONIC"):
            self.tree.insert(
                "", "end", iid=name,
                values=(name, 0, "0 B", 0, "0.000", "0.000", "0.000")
            )

        detected_frame = ttk.LabelFrame(self.root, text="자동 감지 센서 (dvicMacAddr) / MQTT 명령 대상은 Ctrl·Shift로 다중 선택")
        # v0.17 UI 임시 숨김: 자동 감지 센서 통계 표
        # detected_frame.pack(fill="x", padx=10, pady=5)

        detected_cols = ("device", "type", "axes", "source", "count", "last")
        self.detected_tree = ttk.Treeview(
            detected_frame,
            columns=detected_cols,
            show="headings",
            height=4,
            selectmode="extended",
        )

        detected_headers = [
            ("device", "Device ID", 220),
            ("type", "종류", 90),
            ("axes", "축", 70),
            ("source", "수신", 90),
            ("count", "수신 건수", 110),
            ("last", "마지막 수신", 110),
        ]

        for key, title, width in detected_headers:
            self.detected_tree.heading(key, text=title)
            self.detected_tree.column(key, width=width, anchor="center")

        self.detected_tree.pack(fill="x", padx=8, pady=(8, 3))

        ttk.Label(
            detected_frame,
            text=(
                "MQTT: 선택한 센서 MAC 각각에 설정/START/STOP 개별 publish  |  "
                "Serial: 센서 선택과 무관하게 현재 COM으로 1회 전송"
            ),
        ).pack(fill="x", padx=8, pady=(0, 7))

        # V030: place communication/system logs side by side in a resizable pane.
        # Keep both Text widgets and their existing logging/clear callbacks unchanged.
        log_panes = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        log_panes.pack(fill="both", expand=True, padx=10, pady=(5, 10))

        sensor_log_frame = ttk.LabelFrame(
            log_panes,
            text="센서 통신 로그 (TX / RX)",
        )
        log_panes.add(sensor_log_frame, weight=1)

        sensor_log_toolbar = ttk.Frame(sensor_log_frame)
        sensor_log_toolbar.pack(fill="x", padx=5, pady=(5, 0))

        ttk.Button(
            sensor_log_toolbar,
            text="통신 로그 지우기",
            command=self._clear_sensor_log,
        ).pack(side="right", padx=3)

        ttk.Label(
            sensor_log_toolbar,
            text=(
                "TX: 설정 JSON 전체 | "
                "RX: MAC/seq/종류/sample/payload + sensorData 앞부분"
            ),
            wraplength=410,
            justify="left",
        ).pack(side="left", fill="x", expand=True, padx=3)

        self.sensor_log = tk.Text(
            sensor_log_frame,
            height=7,
            wrap="none",
        )
        self.sensor_log.pack(
            fill="both",
            expand=True,
            padx=5,
            pady=5,
        )

        self.sensor_log_max_lines = 1200

        log_frame = ttk.LabelFrame(log_panes, text="시스템 로그")
        log_panes.add(log_frame, weight=1)

        self.log = tk.Text(log_frame, height=7, wrap="none")
        self.log.pack(fill="both", expand=True, padx=5, pady=5)

    def _log(self, text):
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.log.see("end")

    def _sensor_log(self, direction, text):
        """메인 Thread에서 센서 TX/RX 통신 로그를 표시한다."""
        if not hasattr(self, "sensor_log"):
            return

        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.sensor_log.insert(
            "end",
            f"[{stamp}] {direction:<2} {text}\n",
        )
        self.sensor_log.see("end")

        # 장시간 수신 시 Text 위젯이 무한히 커지지 않도록 최근 로그만 유지
        try:
            line_count = int(
                self.sensor_log.index("end-1c").split(".")[0]
            )
            if line_count > self.sensor_log_max_lines:
                delete_to = line_count - self.sensor_log_max_lines + 100
                self.sensor_log.delete("1.0", f"{delete_to}.0")
        except Exception:
            pass

    def _clear_sensor_log(self):
        if hasattr(self, "sensor_log"):
            self.sensor_log.delete("1.0", "end")

    def _queue_rx_log(
        self,
        source,
        topic,
        obj,
        kind,
        payload_bytes,
        mqtt_meta=None,
        payload_crc32=None,
    ):
        device_id = str(
            obj.get("dvicMacAddr")
            or obj.get("deviceId")
            or obj.get("macAddress")
            or "UNKNOWN"
        ).strip()

        seq = str(obj.get("seq", ""))
        tick = str(obj.get("tick", ""))
        sample_rate = str(obj.get("samplerate", ""))
        sample_count = str(obj.get("numofsample", ""))
        g_range = str(obj.get("g_range", ""))
        gain = str(obj.get("gain", ""))
        sensor_data = obj.get("sensorData", "")
        acq_status = str(obj.get("acqStatus", ""))
        ap_ssid = str(obj.get("apSsid", ""))
        ap_rssi = str(obj.get("apRssi", ""))

        # 전체 sensorData가 완전히 같은지 빠르게 비교하기 위한 CRC32.
        # HEX 문자열 자체를 UTF-8 bytes로 계산하므로, 동일 문자열이면 동일 CRC가 나온다.
        sensor_crc32 = None
        try:
            if isinstance(sensor_data, str):
                sensor_crc32 = zlib.crc32(
                    sensor_data.encode("utf-8")
                ) & 0xFFFFFFFF
            else:
                sensor_crc32 = zlib.crc32(
                    str(sensor_data).encode("utf-8")
                ) & 0xFFFFFFFF
        except Exception:
            sensor_crc32 = None

        if isinstance(sensor_data, str):
            preview = sensor_data[:80]
            if len(sensor_data) > 80:
                preview += "..."
        else:
            preview = str(sensor_data)[:80]

        location = (
            f" topic={topic}"
            if topic
            else ""
        )

        mqtt_diag = ""
        if source == "MQTT" and mqtt_meta:
            mqtt_diag = (
                f" mid={mqtt_meta.get('mid', '')}"
                f" qos={mqtt_meta.get('qos', '')}"
                f" dup={mqtt_meta.get('dup', '')}"
                f" retain={mqtt_meta.get('retain', '')}"
            )

        crc_diag = ""
        if payload_crc32 is not None:
            crc_diag += f" payloadCRC={payload_crc32:08X}"
        if sensor_crc32 is not None:
            crc_diag += f" dataCRC={sensor_crc32:08X}"

        self.ui_queue.put(
            (
                "sensor_log",
                "RX",
                (
                    f"[{source}]{location} "
                    f"mac={device_id} "
                    f"type={kind} "
                    f"seq={seq} "
                    f"tick={tick} "
                    f"rate={sample_rate} "
                    f"samples={sample_count} "
                    f"{('acqStatus=' + acq_status + ' ') if acq_status else ''}"
                    f"{('apSsid=' + ap_ssid + ' ') if ap_ssid else ''}"
                    f"{('apRssi=' + ap_rssi + ' ') if ap_rssi else ''}"
                    f"{('g_range=' + g_range + ' ') if g_range else ''}"
                    f"{('gain=' + gain + ' ') if gain else ''}"
                    f"payload={payload_bytes}B"
                    f"{mqtt_diag}"
                    f"{crc_diag} "
                    f"sensorData={preview}"
                ),
            )
        )

    @staticmethod
    def fmt_bytes(value):
        value = float(value)
        if value >= 1024 ** 3:
            return f"{value / 1024**3:.3f} GB"
        if value >= 1024 ** 2:
            return f"{value / 1024**2:.3f} MB"
        if value >= 1024:
            return f"{value / 1024:.3f} KB"
        return f"{int(value)} B"

    @staticmethod
    def fmt_rate(value):
        if value >= 1024 ** 2:
            return f"{value / 1024**2:.3f} MB/s"
        return f"{value / 1024:.3f} KB/s"

    def _update_transport_ui(self):
        """
        MQTT/SERIAL 선택에 따라 접속 설정만 전환한다.

        센서 종류/SENSING_PERIOD는 두 통신 모두 사용하므로
        항상 수정 가능하다.

        MQTT일 때만 MAC 대상 목록을 사용한다.
        """
        mode = self.mode.get().strip().upper()

        if self.connected:
            self.mode_combo.configure(state="disabled")

            self.host_entry.configure(state="disabled")
            self.port_entry.configure(state="disabled")
            self.topic_entry.configure(state="disabled")
            self.qos_combo.configure(state="disabled")

            self.serial_port_combo.configure(state="disabled")
            self.serial_baud_combo.configure(state="disabled")
            self.btn_refresh_serial.configure(state="disabled")
        else:
            self.mode_combo.configure(state="readonly")

            if mode == "MQTT":
                self.host_entry.configure(state="normal")
                self.port_entry.configure(state="normal")
                self.topic_entry.configure(state="normal")
                self.qos_combo.configure(state="readonly")

                self.serial_port_combo.configure(state="disabled")
                self.serial_baud_combo.configure(state="disabled")
                self.btn_refresh_serial.configure(state="disabled")

            elif mode == "SERIAL":
                self.host_entry.configure(state="disabled")
                self.port_entry.configure(state="disabled")
                self.topic_entry.configure(state="disabled")
                self.qos_combo.configure(state="disabled")

                self.serial_port_combo.configure(state="readonly")
                self.serial_baud_combo.configure(state="readonly")
                self.btn_refresh_serial.configure(state="normal")

        # 센서 설정 자체는 MQTT/Serial 공통
        self.run_sensor_combo.configure(state="readonly")
        self.cfg_period_entry.configure(state="normal")
        self.cfg_sample_profile_combo.configure(state="readonly")
        self.cfg_sensortype_combo.configure(state="readonly")
        self.cfg_mems_g_range_combo.configure(
            state="readonly" if self.run_sensor_type.get().strip().upper() == "ADXL"
            else "disabled"
        )
        self.cfg_piezo_gain_entry.configure(
            state="normal" if self.run_sensor_type.get().strip().upper() == "PIEZO"
            else "disabled"
        )

        # MAC 대상은 MQTT에서만 선택
        if mode == "MQTT":
            self.config_target_listbox.configure(state="normal")
            self.btn_config_refresh.configure(state="normal")
            self.btn_config_select_all.configure(state="normal")
            self.btn_config_clear.configure(state="normal")
        else:
            self.config_target_listbox.configure(state="disabled")
            self.btn_config_refresh.configure(state="disabled")
            self.btn_config_select_all.configure(state="disabled")
            self.btn_config_clear.configure(state="disabled")

        # 전송 버튼은 연결 상태에서만 사용
        send_state = "normal" if self.connected else "disabled"
        self.btn_sensor_start.configure(state=send_state)
        self.btn_sensor_stop.configure(state=send_state)
        self.btn_config_apply.configure(state=send_state)

        read_state = (
            "normal"
            if self.connected and mode == "MQTT"
            else "disabled"
        )
        self.btn_config_read.configure(state=read_state)

        self._update_config_target_summary()
        self._update_config_preview()

    def _on_csv_toggle(self):
        """
        CSV 저장은 성능 측정 START/STOP과 독립적으로 동작한다.
        연결 중 CSV를 켜면 새로운 저장 Session을 시작한다.
        토글 전의 ADXL 미완성 X/Y/Z 세트는 이어서 사용하지 않는다.
        """
        self._clear_adxl_csv_pending()

        if self.csv_enabled.get():
            if self.connected:
                self._start_csv_session()
            else:
                self.csv_folder_var.set("연결 후 CSV 저장 폴더 생성")
        else:
            self.csv_session_dir = None
            self.csv_folder_var.set("CSV 저장 비활성")
            self._log("CSV 저장 비활성")

    def refresh_serial_ports(self):
        if not SERIAL_AVAILABLE:
            if hasattr(self, "serial_port_combo"):
                self.serial_port_combo["values"] = []
            return

        ports = [p.device for p in list_ports.comports()]

        if hasattr(self, "serial_port_combo"):
            self.serial_port_combo["values"] = ports

        if ports and not self.serial_port.get():
            self.serial_port.set(ports[0])
        elif self.serial_port.get() not in ports and ports:
            self.serial_port.set(ports[0])

    def make_client(self):
        client_id = f"vibration_speed_{int(time.time())}"
        try:
            c = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id
            )
        except Exception:
            c = mqtt.Client(client_id=client_id)

        c.on_connect = self.on_connect
        c.on_disconnect = self.on_disconnect
        c.on_message = self.on_message
        return c

    def connect(self):
        if self.connected:
            return

        mode = self.mode.get().strip().upper()

        if mode == "MQTT":
            self._connect_mqtt()
        elif mode == "SERIAL":
            self._connect_serial()
        else:
            messagebox.showwarning(
                "통신 방식",
                "MQTT 또는 SERIAL을 선택하세요.",
            )

    def _connect_mqtt(self):
        try:
            self.client = self.make_client()
            self.status.set("CONNECTING MQTT...")
            self._log(f"MQTT 연결 시도 {self.host.get()}:{self.port.get()}")
            self.client.connect(
                self.host.get().strip(),
                int(self.port.get().strip()),
                60
            )
            self.client.loop_start()
            self.active_transport = "MQTT"
        except Exception as e:
            self.client = None
            self.active_transport = None
            self.status.set("DISCONNECTED")
            messagebox.showerror("MQTT 연결 오류", str(e))

    def _connect_serial(self):
        if not SERIAL_AVAILABLE:
            messagebox.showerror(
                "Serial",
                "pyserial이 설치되어 있지 않습니다.\\n"
                "python -m pip install pyserial"
            )
            return

        port_name = self.serial_port.get().strip()
        if not port_name:
            messagebox.showwarning("Serial", "Serial Port를 선택하세요.")
            return

        try:
            baud = int(self.serial_baud.get())
            self.serial_conn = serial.Serial(
                port=port_name,
                baudrate=baud,
                timeout=0.1,
            )

            self.serial_stop.clear()
            self.serial_thread = threading.Thread(
                target=self._serial_read_loop,
                daemon=True,
                name="vibration-serial-reader",
            )
            self.serial_thread.start()

            self.connected = True
            self.active_transport = "SERIAL"
            self.status.set("CONNECTED")
            self.btn_connect.configure(state="disabled")
            self.btn_disconnect.configure(state="normal")
            self.btn_start.configure(state="normal")
            self.btn_sensor_start.configure(state="normal")
            self.btn_sensor_stop.configure(state="normal")
            self.btn_config_apply.configure(state="normal")
            self._update_transport_ui()

            if self.csv_enabled.get():
                self._start_csv_session()

            self._log(
                f"Serial 연결 성공 {port_name} / {baud} bps"
            )

        except Exception as e:
            self.serial_conn = None
            self.active_transport = None
            self.status.set("DISCONNECTED")
            messagebox.showerror("Serial 연결 오류", str(e))

    def disconnect(self):
        self.measuring = False
        self.stats.stop()

        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
            try:
                self.client.loop_stop()
            except Exception:
                pass
        self.client = None

        self.serial_stop.set()
        if self.serial_conn is not None:
            try:
                self.serial_conn.close()
            except Exception:
                pass
        self.serial_conn = None

        self.connected = False
        previous = self.active_transport
        self.active_transport = None

        self.status.set("DISCONNECTED")
        self.btn_connect.configure(state="normal")
        self.btn_disconnect.configure(state="disabled")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="disabled")
        self.btn_sensor_start.configure(state="disabled")
        self.btn_sensor_stop.configure(state="disabled")
        self.btn_config_apply.configure(state="disabled")
        self._update_transport_ui()

        if self.csv_enabled.get():
            self.csv_session_dir = None
            self.csv_folder_var.set("연결 후 CSV 저장 폴더 생성")

        self._log(f"{previous or '통신'} 연결 해제")

    def _serial_read_loop(self):
        buffer = ""

        while not self.serial_stop.is_set():
            conn = self.serial_conn
            if conn is None:
                break

            try:
                waiting = conn.in_waiting
                data = conn.read(waiting if waiting > 0 else 1)

                if not data:
                    continue

                decoded_chunk = data.decode("utf-8", errors="ignore")

                # Serial CONFIG READY 진단 로그.
                # 일반 센서 데이터는 매우 크므로 모든 raw chunk를 출력하지 않고,
                # CONFIG/READY 문구가 포함된 chunk만 repr() 형태로 남긴다.
                # 이렇게 하면 \r\n, escape 문자까지 실제 수신 형태를 확인할 수 있다.
                upper_chunk = decoded_chunk.upper()
                if "CONFIG" in upper_chunk or "READY" in upper_chunk:
                    self.ui_queue.put((
                        "log",
                        f"Serial RAW RX: {decoded_chunk!r}"
                    ))
                    self.ui_queue.put((
                        "sensor_log",
                        "RX",
                        f"[SERIAL RAW] {decoded_chunk!r}"
                    ))

                buffer += decoded_chunk
                objects, buffer = self._extract_json_objects(buffer)

                for json_text in objects:
                    self._process_payload(
                        json_text.encode("utf-8"),
                        source="SERIAL",
                        topic=f"COM:{self.serial_port.get().strip()}",
                    )

                # 노이즈/불완전 데이터 무한 누적 방지
                if len(buffer) > 2_000_000:
                    last_open = buffer.rfind("{")
                    buffer = buffer[last_open:] if last_open >= 0 else ""

            except Exception as e:
                if not self.serial_stop.is_set():
                    self.ui_queue.put(("log", f"Serial 수신 오류: {e}"))
                    self.ui_queue.put(("status", "DISCONNECTED"))
                break

    @staticmethod
    def _extract_json_objects(buffer):
        objects = []
        start = None
        depth = 0
        in_string = False
        escaped = False
        consumed_to = 0

        for i, ch in enumerate(buffer):
            if start is None:
                if ch == "{":
                    start = i
                    depth = 1
                    in_string = False
                    escaped = False
                continue

            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    objects.append(buffer[start:i + 1])
                    consumed_to = i + 1
                    start = None

        if start is not None:
            remainder = buffer[start:]
        else:
            remainder = buffer[consumed_to:] if consumed_to else ""
            # 완성 JSON이 없고 시작 brace도 없으면 노이즈 버림
            if not objects:
                remainder = ""

        return objects, remainder

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        try:
            rc = int(reason_code)
        except Exception:
            rc = int(getattr(reason_code, "value", -1))

        if rc != 0:
            self.ui_queue.put(("status", "CONNECT FAILED"))
            self.ui_queue.put(("log", f"연결 실패 rc={reason_code}"))
            return

        topics = parse_mqtt_topics(self.topic.get())
        if not topics:
            topics = [DEFAULT_TOPIC]

        subscribe_logs = []
        for sub_topic in topics:
            result, mid = client.subscribe(
                sub_topic,
                qos=int(self.qos.get())
            )
            subscribe_logs.append(
                f"{sub_topic} result={result} mid={mid}"
            )

        self.connected = True
        self.active_transport = "MQTT"
        self.ui_queue.put(("status", "CONNECTED"))
        self.ui_queue.put((
            "log",
            f"SUBSCRIBE {' | '.join(subscribe_logs)} QoS={self.qos.get()}"
        ))
        self.ui_queue.put(("csv_session_on_connect", None))

    def on_disconnect(
        self, client, userdata, disconnect_flags=None,
        reason_code=None, properties=None
    ):
        self.connected = False
        self.ui_queue.put(("status", "DISCONNECTED"))
        self.ui_queue.put(("log", f"연결 끊김 reason={reason_code}"))

    def on_message(self, client, userdata, msg):
        # 진동은 V/#, 초음파는 D/# 등 복수 Topic을 구독할 수 있다.
        # 설정 retained 메시지는 S/{MAC}에 저장하므로 수신 Topic과 분리된다.
        # MQTT 중복 진단을 위해 Paho MQTTMessage의 mid/qos/dup/retain과
        # 전체 payload CRC32를 _process_payload()까지 전달한다.
        mqtt_meta = {
            "mid": getattr(msg, "mid", ""),
            "qos": getattr(msg, "qos", ""),
            "dup": bool(getattr(msg, "dup", False)),
            "retain": bool(getattr(msg, "retain", False)),
        }
        try:
            payload_crc32 = zlib.crc32(msg.payload) & 0xFFFFFFFF
        except Exception:
            payload_crc32 = None

        try:
            obj = json.loads(msg.payload.decode("utf-8"))

            if isinstance(obj, dict) and "sensorData" not in obj:
                # 우리가 보낸 조회 요청은 V/# 구독 때문에 자기 자신에게도
                # 다시 들어오므로 응답으로 처리하지 않는다.
                if str(obj.get("CONFIG", "")).strip() == "?":
                    return

                # 현재 설정 조회 응답:
                # 예전 프로토콜은 V/{MAC}에 {"CONFIG":"?"}를 보내면
                # 같은 V/{MAC}으로 현재 설정 JSON을 응답한다.
                if (
                    "SENSORTYPE" in obj
                    and (
                        "PIEZO_SAMPLE_RATE" in obj
                        or "MEMS_SAMPLE_RATE" in obj
                        or "ADXL_SAMPLE_RATE" in obj
                    )
                ):
                    topic = str(getattr(msg, "topic", ""))
                    topic_device = (
                        topic.split("/", 1)[1]
                        if "/" in topic
                        else ""
                    )
                    device_id = str(
                        obj.get("dvicMacAddr")
                        or obj.get("deviceId")
                        or obj.get("macAddress")
                        or topic_device
                        or ""
                    ).strip()

                    pending = str(
                        self.pending_config_query_device or ""
                    ).strip()

                    # 명시적으로 조회 중인 센서의 응답만 UI 설정에 반영.
                    if pending and device_id == pending:
                        self.ui_queue.put(
                            (
                                "sensor_config_response",
                                {
                                    "device_id": device_id,
                                    "topic": topic,
                                    "config": obj,
                                },
                            )
                        )
                        self.pending_config_query_device = None

                    return

                control_keys = {
                    "START",
                    "CONFIG",
                    "COMMPATH",
                    "SENSORTYPE",
                    "PIEZO_GAIN",
                    "PIEZO_SAMPLE_RATE",
                    "PIEZO_SAMPLE_COUNT",
                    "MEMS_SAMPLE_RATE",
                    "MEMS_SAMPLE_COUNT",
                    "MEMS_G_RANGE",
                    "ADXL_SAMPLE_RATE",
                    "ADXL_SAMPLE_COUNT",
                    "ADXL_G_RANGE",
                    "SENSING_PERIOD",
                }

                if any(key in obj for key in control_keys):
                    return

        except Exception:
            pass

        self._process_payload(
            msg.payload,
            source="MQTT",
            topic=str(getattr(msg, "topic", "")),
            mqtt_meta=mqtt_meta,
            payload_crc32=payload_crc32,
        )

    def _process_payload(
        self,
        raw,
        source="UNKNOWN",
        topic="",
        mqtt_meta=None,
        payload_crc32=None,
    ):
        """
        MQTT/Serial 공통 처리 진입점.

        연결만 되어 있어도:
        - JSON 파싱
        - 진동 분류
        - dvicMacAddr 자동 감지
        - ADXL Graph/FFT 최신값 갱신

        CSV 체크 상태에서는 측정 START/STOP과 무관하게:
        - HEX 원본 저장
        - decoded sample 저장

        기존 성능 통계 로직은 내부에 유지하지만 UI에서는 숨김.
        """
        recv_ns = time.monotonic_ns()

        try:
            if isinstance(raw, bytes):
                raw_bytes = raw
                text_payload = raw.decode("utf-8")
            else:
                text_payload = str(raw)
                raw_bytes = text_payload.encode("utf-8")

            obj = json.loads(text_payload)

        except Exception:
            if self.measuring:
                self.stats.add_json_error()
            return

        # Serial 센서는 Sleep에서 깨어난 직후 설정 요청 신호를 보낸다.
        # 실제 센서 메시지는 {"CONFIG":"READY"} 이므로 key/value 모두
        # 대소문자와 관계없이 인식한다.
        # 이 메시지는 진동 데이터가 아니므로 classify_vibration() 전에 처리한다.
        normalized_obj = (
            {str(k).strip().lower(): v for k, v in obj.items()}
            if isinstance(obj, dict)
            else {}
        )

        if (
            source == "SERIAL"
            and str(normalized_obj.get("config", "")).strip().lower() == "ready"
        ):
            self.ui_queue.put(
                (
                    "sensor_log",
                    "RX",
                    (
                        f"[SERIAL] topic={topic} "
                        f"CONFIG READY {text_payload.strip()}"
                    ),
                )
            )
            self.ui_queue.put(
                (
                    "log",
                    (
                        "Serial CONFIG READY 감지: "
                        f"{text_payload.strip()} -> 현재 설정 자동 전송"
                    ),
                )
            )
            # Tk StringVar 접근과 실제 설정 생성은 UI Thread에서 수행한다.
            self.ui_queue.put(("serial_config_ready", None))
            return

        result = classify_vibration(obj)
        if result is None and classify_ultrasonic(obj, topic):
            result = ("ULTRASONIC", None)

        if result is None:
            if self.measuring:
                self.stats.add_ignored()
            return

        kind, _frame = result
        sensor_data = obj.get("sensorData", "")
        sensor_hex_chars = (
            len(sensor_data) if isinstance(sensor_data, str) else 0
        )

        device_id = str(
            obj.get("dvicMacAddr")
            or obj.get("deviceId")
            or obj.get("macAddress")
            or "UNKNOWN"
        ).strip()

        # 센서 통신 RX 로그
        self._queue_rx_log(
            source=source,
            topic=topic,
            obj=obj,
            kind=kind,
            payload_bytes=len(raw_bytes),
            mqtt_meta=mqtt_meta,
            payload_crc32=payload_crc32,
        )

        # 연결만 되어 있어도 센서 목록은 자동으로 갱신
        self.sensor_registry.update(
            device_id=device_id,
            kind=kind,
            source=source,
        )

        # ADXL / PIEZO 그래프/FFT는 측정 여부와 무관하게 최신값 갱신
        if kind.startswith("ADXL-") or kind == "PIEZO":
            axis = kind[-1] if kind.startswith("ADXL-") else "Z"

            try:
                is_adxl = kind.startswith("ADXL-")
                sensor_kind = "ADXL" if is_adxl else "PIEZO"

                default_rate, default_count = (
                    self.graph_sample_defaults.get(
                        sensor_kind,
                        (3200.0, 8192)
                        if is_adxl
                        else (25600.0, 8192),
                    )
                )

                packet_sample_rate = obj.get("samplerate", "")
                packet_numofsample = obj.get("numofsample", "")

                sample_rate = normalize_sample_rate(
                    sensor_kind,
                    (
                        packet_sample_rate
                        if packet_sample_rate not in (None, "")
                        else default_rate
                    ),
                    default_rate,
                )

                raw_count = (
                    packet_numofsample
                    if packet_numofsample not in (None, "")
                    else default_count
                )
                numofsample = int(
                    raw_count
                    if raw_count not in (None, "", 0, "0")
                    else default_count
                )

            except Exception:
                fallback = self.graph_sample_defaults.get(
                    "ADXL"
                    if kind.startswith("ADXL-")
                    else "PIEZO",
                    (3200.0, 8192)
                    if kind.startswith("ADXL-")
                    else (25600.0, 8192),
                )
                sample_rate = float(fallback[0])
                numofsample = int(fallback[1])
                packet_sample_rate = str(obj.get("samplerate", ""))
                packet_numofsample = str(obj.get("numofsample", ""))

            try:
                self.graph_queue.put_nowait(
                    GraphItem(
                        device_id=device_id,
                        sensor_kind=("ADXL" if kind.startswith("ADXL-") else "PIEZO"),
                        axis=axis,
                        sensor_data=sensor_data,
                        numofsample=numofsample,
                        sample_rate=sample_rate,
                        recv_ns=recv_ns,
                        seq=str(obj.get("seq", "")),
                        packet_sample_rate=str(packet_sample_rate),
                        packet_numofsample=str(packet_numofsample),
                        g_range=str(obj.get("g_range", "")),
                        gain=str(obj.get("gain", "")),
                    )
                )
            except Exception:
                pass

        # ULTRASONIC H 그래프는 320개 주파수영역 값을 그대로 표시한다.
        # 추가 FFT를 수행하지 않는다.
        if kind == "ULTRASONIC":
            try:
                self.graph_queue.put_nowait(
                    UltrasonicGraphItem(
                        device_id=device_id,
                        sensor_data=sensor_data,
                        recv_ns=recv_ns,
                        seq=str(obj.get("seq", "")),
                    )
                )
            except Exception:
                pass

        # 기존 성능 통계는 UI에서 숨겼지만,
        # 내부 측정 모드를 수동으로 사용할 경우에만 계속 집계한다.
        if self.measuring:
            self.stats.add(
                kind,
                len(raw_bytes),
                sensor_hex_chars,
                recv_ns,
            )

        # CSV는 측정 START/STOP과 완전히 분리.
        # PIEZO는 패킷 단위로 즉시 저장 Queue에 넣는다.
        # ADXL은 반드시 X -> Y -> Z 3축이 완성된 경우에만 저장한다.
        if self.csv_enabled.get():
            try:
                if kind.startswith("ADXL-"):
                    axis = kind[-1]
                elif kind == "ULTRASONIC":
                    axis = "H"
                else:
                    axis = "Z"

                csv_item = CSVItem(
                    received_at=datetime.now().isoformat(
                        timespec="milliseconds"
                    ),
                    source=source,
                    device_id=device_id,
                    gw_mac=str(obj.get("gwMacAddr", "")),
                    kind=kind,
                    axis=axis,
                    seq=str(obj.get("seq", "")),
                    tick=str(obj.get("tick", "")),
                    battery=str(obj.get("batteryRmin", "")),
                    sample_rate=str(obj.get("samplerate", "")),
                    num_samples=(
                        "320"
                        if kind == "ULTRASONIC"
                        else str(obj.get("numofsample", ""))
                    ),
                    payload_bytes=len(raw_bytes),
                    sensor_data=sensor_data,
                    acq_status=str(obj.get("acqStatus", "") if obj.get("acqStatus") is not None else ""),
                    ap_ssid=str(obj.get("apSsid", "") if obj.get("apSsid") is not None else ""),
                    ap_rssi=str(obj.get("apRssi", "") if obj.get("apRssi") is not None else ""),
                )

                if kind.startswith("ADXL-"):
                    self._queue_adxl_csv_item(csv_item)
                else:
                    self.csv_queue.put_nowait(csv_item)

            except Exception as e:
                with self.csv_counter_lock:
                    self.csv_error_count += 1
                self.ui_queue.put(("log", f"CSV Queue 오류: {e}"))

    def _clear_adxl_csv_pending(self):
        """현재 미완성 ADXL X/Y/Z 세트를 모두 폐기한다."""
        with self.adxl_csv_pending_lock:
            self.adxl_csv_pending.clear()

    @staticmethod
    def _adxl_csv_items_compatible(first_item, next_item):
        """
        같은 ADXL 세트로 묶을 수 있는지 최소 조건을 확인한다.

        source/device_id는 pending key에서 동일함을 보장한다.
        samplerate/numofsample 값이 양쪽 모두 존재하는 경우에는
        서로 같아야 같은 세트로 인정한다.
        """
        for attr in ("sample_rate", "num_samples"):
            a = str(getattr(first_item, attr, "") or "").strip()
            b = str(getattr(next_item, attr, "") or "").strip()
            if a and b and a != b:
                return False
        return True

    def _queue_adxl_csv_item(self, item):
        """
        ADXL CSV 저장용 X/Y/Z 세트 조립.

        반드시 X -> Y -> Z 순서가 완성되어야 실제 csv_queue에 들어간다.

        예:
          X, X, X, Y, Z
          -> 앞의 X 두 개는 폐기
          -> 마지막 X + Y + Z만 저장

          X, Y, X
          -> 첫 X/Y 미완성 세트 폐기
          -> 마지막 X부터 새 세트 시작

          Y 또는 Z가 먼저 도착
          -> 저장하지 않음
        """
        key = (str(item.source), str(item.device_id))
        axis = str(item.axis).upper()
        completed = None

        with self.adxl_csv_pending_lock:
            pending = self.adxl_csv_pending.get(key)

            if axis == "X":
                self.adxl_csv_pending[key] = {"X": item}
                return

            if axis == "Y":
                if (
                    pending is None
                    or set(pending.keys()) != {"X"}
                    or not self._adxl_csv_items_compatible(pending["X"], item)
                ):
                    self.adxl_csv_pending.pop(key, None)
                    return

                pending["Y"] = item
                return

            if axis == "Z":
                if (
                    pending is None
                    or set(pending.keys()) != {"X", "Y"}
                    or not self._adxl_csv_items_compatible(pending["X"], item)
                    or not self._adxl_csv_items_compatible(pending["Y"], item)
                ):
                    self.adxl_csv_pending.pop(key, None)
                    return

                completed = (
                    pending["X"],
                    pending["Y"],
                    item,
                )
                self.adxl_csv_pending.pop(key, None)

            else:
                return

        if completed is not None:
            # X/Y/Z 3축이 완성된 경우에만 세 축을 연속으로 저장 Queue에 등록.
            for completed_item in completed:
                self.csv_queue.put_nowait(completed_item)

    def _detected_device_ids(self):
        return [rec["device_id"] for rec in self.sensor_registry.snapshot() if rec.get("device_id")]

    def open_ap_config(self):
        """v0.13부터 별도 설정창을 사용하지 않는다."""
        self._refresh_config_devices()
        self._update_config_preview()

    def _current_commpath(self):
        mode = self.mode.get().strip().upper()

        if mode == "MQTT":
            return "0"
        if mode == "SERIAL":
            return "1"

        raise ValueError(
            "메인 화면의 통신 방식을 MQTT 또는 SERIAL로 선택하세요."
        )

    def _profile_labels_for_sensor(self, sensor_type):
        sensor_type = str(sensor_type).strip().upper()

        if sensor_type == "PIEZO":
            return PIEZO_PROFILE_LABELS
        if sensor_type == "ADXL":
            return ADXL_PROFILE_LABELS

        return ()

    def _profile_map_for_sensor(self, sensor_type):
        sensor_type = str(sensor_type).strip().upper()

        if sensor_type == "PIEZO":
            return PIEZO_PROFILE_MAP
        if sensor_type == "ADXL":
            return ADXL_PROFILE_MAP

        return {}

    def _profile_values_for_sensor(self, sensor_type):
        sensor_type = str(sensor_type).strip().upper()
        profile_map = self._profile_map_for_sensor(sensor_type)

        label = self.sample_profile_state.get(sensor_type, "")

        if (
            self.run_sensor_type.get().strip().upper() == sensor_type
            and self.cfg_sample_profile.get().strip()
        ):
            label = self.cfg_sample_profile.get().strip()

        if label not in profile_map:
            labels = self._profile_labels_for_sensor(sensor_type)
            if not labels:
                raise ValueError(
                    f"지원하지 않는 센서 종류: {sensor_type}"
                )
            label = labels[-1]

        rate, count = profile_map[label]
        return str(rate), str(count), label

    def _on_sensor_type_changed(self, _event=None):
        old_type = str(self._last_sensor_type).strip().upper()
        current_profile = self.cfg_sample_profile.get().strip()

        if (
            old_type in ("PIEZO", "ADXL")
            and current_profile
            and current_profile
            in self._profile_map_for_sensor(old_type)
        ):
            self.sample_profile_state[old_type] = current_profile

        new_type = self.run_sensor_type.get().strip().upper()
        self._last_sensor_type = new_type
        current_code = self.cfg_sensor_type_code.get().strip()
        # 종류 변경 시 물리 센서 종류만 변경, 기존 normal/collection 모드는 유지.
        collection = current_code in ("3", "4")
        self.cfg_sensor_type_code.set(
            "3" if collection and new_type == "PIEZO" else
            "4" if collection else
            "1" if new_type == "PIEZO" else "2"
        )
        self._update_sensortype_description()
        self.cfg_mems_g_range_combo.configure(
            state="readonly" if new_type == "ADXL" else "disabled"
        )
        self.cfg_piezo_gain_entry.configure(
            state="normal" if new_type == "PIEZO" else "disabled"
        )

        labels = self._profile_labels_for_sensor(new_type)
        self.cfg_sample_profile_combo["values"] = labels

        selected = self.sample_profile_state.get(new_type, "")

        if selected not in labels and labels:
            selected = labels[-1]
            self.sample_profile_state[new_type] = selected

        self.cfg_sample_profile.set(selected)
        self._on_sample_profile_changed()

    def _on_sample_profile_changed(self):
        sensor_type = self.run_sensor_type.get().strip().upper()

        if sensor_type not in ("PIEZO", "ADXL"):
            self._update_config_preview()
            return

        label = self.cfg_sample_profile.get().strip()
        profile_map = self._profile_map_for_sensor(sensor_type)

        if label in profile_map:
            self.sample_profile_state[sensor_type] = label
            rate, count = profile_map[label]
            self.graph_sample_defaults[sensor_type] = (
                float(rate),
                int(count),
            )

        self._update_config_preview()

    def _update_sensortype_description(self):
        code = self.cfg_sensor_type_code.get().strip()
        descriptions = {
            "0": "0: collection 종료 / normal 복귀",
            "1": "1: PIEZO normal",
            "2": "2: MEMS normal",
            "3": "3: PIEZO collection",
            "4": "4: MEMS collection",
        }
        self.cfg_sensortype_desc.set(descriptions.get(code, "SENSORTYPE 오류"))

    def _on_sensortype_code_changed(self, _event=None):
        code = self.cfg_sensor_type_code.get().strip()
        if code not in ("0", "1", "2", "3", "4"):
            return
        self._update_sensortype_description()
        sensor = "PIEZO" if code in ("1", "3") else (
            "ADXL" if code in ("2", "4") else None
        )
        if sensor is not None and self.run_sensor_type.get() != sensor:
            # 센서 종류 변경 시 보정/샘플 프로필 UI만 갱신한다.
            self.run_sensor_type.set(sensor)
            self._on_sensor_type_changed()
            # _on_sensor_type_changed의 기본 모드 보정이 명시적 선택을 덮지 않게 복구.
            self.cfg_sensor_type_code.set(code)
            self._update_sensortype_description()
        self._update_config_preview()

    def _current_sensortype(self, override=None):
        if override is not None:
            value = str(override)

            if value not in ("0", "1", "2", "3", "4"):
                raise ValueError(
                    "SENSORTYPE은 0(STOP), 1/2(normal), 3/4(collection)만 가능합니다."
                )
            return value

        selected = self.cfg_sensor_type_code.get().strip()
        if selected not in ("0", "1", "2", "3", "4"):
            raise ValueError("SENSORTYPE은 0~4 중 선택하세요.")
        return selected

    def _build_config_dict(self, sensor_type_override=None):
        try:
            period = int(self.cfg_period.get().strip())
        except Exception:
            raise ValueError(
                "SENSING_PERIOD는 0 이상의 정수(초)여야 합니다."
            )

        if period < 0:
            raise ValueError(
                "SENSING_PERIOD는 0 이상의 정수(초)여야 합니다."
            )

        sensor_type_code = self._current_sensortype(
            sensor_type_override
        )

        payload = {
            "COMMPATH": self._current_commpath(),
            "SENSORTYPE": sensor_type_code,
        }
        if sensor_type_code != "0":
            payload["SENSING_PERIOD"] = str(period)

        # Normal(1/2)과 Collection(3/4)은 같은 센서 설정을 사용한다.
        if sensor_type_code in ("1", "3"):
            piezo_rate, piezo_count, _label = (
                self._profile_values_for_sensor("PIEZO")
            )
            payload["PIEZO_SAMPLE_RATE"] = piezo_rate
            payload["PIEZO_SAMPLE_COUNT"] = piezo_count

            gain = self.cfg_piezo_gain.get().strip()
            if not gain.isdigit():
                raise ValueError("PIEZO_GAIN은 0 이상의 정수여야 합니다.")
            payload["PIEZO_GAIN"] = gain

        # ADXL 선택 시 ADXL 설정만 전송
        elif sensor_type_code in ("2", "4"):
            adxl_rate, adxl_count, _label = (
                self._profile_values_for_sensor("ADXL")
            )

            adxl_code = ADXL_RATE_TO_CODE.get(adxl_rate)
            if not adxl_code:
                raise ValueError(
                    f"ADXL Sample Rate 코드 변환 실패: {adxl_rate}"
                )

            # 모드 A command: 레지스터 코드는 0x prefix 없는 HEX 문자열(예: 0F).
            payload["MEMS_SAMPLE_RATE"] = (
                adxl_code[2:] if adxl_code.lower().startswith("0x") else adxl_code
            )
            payload["MEMS_SAMPLE_COUNT"] = adxl_count

            # 사용자가 선택한 ADXL 측정 범위를 실제 센서 설정으로 송신.
            # RAW/FFT 화면의 LSB/g 환산값과는 독립적이다.
            g_range = self.cfg_mems_g_range.get().strip()
            if g_range not in ("2", "4", "8", "16"):
                raise ValueError("MEMS G Range는 2, 4, 8, 16 중 선택하세요.")
            # ADXL343 RANGE 코드: ±2g=0, ±4g=1, ±8g=2, ±16g=3
            payload["MEMS_G_RANGE"] = {
                "2": "0", "4": "1", "8": "2", "16": "3"
            }[g_range]

        # STOP(SENSORTYPE=0)은 샘플 설정을 별도로 보내지 않는다.
        # 공통으로 필요할 경우 다시 활성화
        # payload["SLEEP_SEC"] = "0"

        return payload


    def _update_config_preview(self):
        if not hasattr(self, "config_preview"):
            return

        try:
            payload = self._build_config_dict()
            preview = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            )

            sensor_type = (
                self.run_sensor_type.get().strip().upper()
            )
            rate, count, label = (
                self._profile_values_for_sensor(sensor_type)
            )

            fs = float(rate)
            n = int(count)

            self.config_axis_info_var.set(
                f"{sensor_type} | {label} | "
                f"Raw X=0~{n - 1:,} | "
                f"FFT X=0~{fs / 2.0:g} Hz | "
                f"Δf={fs / n:g} Hz"
            )

        except Exception as e:
            preview = f"[설정 오류]\n{e}"
            self.config_axis_info_var.set("")

        self.config_preview.delete("1.0", "end")
        self.config_preview.insert("1.0", preview)


    def _refresh_config_devices(self):
        if self.config_target_listbox is None:
            return

        # 현재 선택 상태 먼저 보존
        self._sync_config_target_selection()

        devices = self._detected_device_ids()

        self.config_target_listbox.delete(0, "end")

        for device_id in devices:
            self.config_target_listbox.insert("end", device_id)

        for index, device_id in enumerate(devices):
            if device_id in self.command_target_ids:
                self.config_target_listbox.selection_set(index)

        self._update_config_target_summary()
        self._update_config_preview()

    def _sync_config_target_selection(self):
        if self.config_target_listbox is None:
            return

        selected = {
            str(self.config_target_listbox.get(i)).strip()
            for i in self.config_target_listbox.curselection()
            if str(self.config_target_listbox.get(i)).strip()
        }

        self.command_target_ids = selected
        self._update_config_target_summary()

    def _update_config_target_summary(self):
        if self.config_target_summary_var is None:
            return

        if self.mode.get().strip().upper() == "SERIAL":
            self.config_target_summary_var.set(
                "Serial: MAC 선택 미사용"
            )
            return

        count = len(self.command_target_ids)

        if count == 0:
            self.config_target_summary_var.set("0개 선택")
        else:
            self.config_target_summary_var.set(
                f"{count}개 선택"
            )

    def _select_all_config_devices(self):
        if self.config_target_listbox is None:
            return

        self.config_target_listbox.selection_set(0, "end")
        self._sync_config_target_selection()

    def _clear_config_devices(self):
        if self.config_target_listbox is None:
            return

        self.config_target_listbox.selection_clear(0, "end")
        self._sync_config_target_selection()

    def _selected_command_devices(self):
        # 설정창이 열려 있으면 현재 화면 선택을 즉시 반영
        self._sync_config_target_selection()
        return sorted(self.command_target_ids)

    def _send_serial_config_on_ready(self):
        """
        Serial 센서가 {"CONFIG":"READY"}를 보낸 순간 호출된다.

        현재 UI의 COMMPATH / SENSORTYPE / Sample Rate / Sample Count / SENSING_PERIOD 값을 읽어
        Serial로 설정 JSON을 1회 전송한다.

        센서가 Sleep -> Wake 할 때마다 ready를 다시 보낸다면
        그때마다 가장 최신 UI 설정이 자동으로 전달된다.
        """
        if not self.connected or self.active_transport != "SERIAL":
            self._log(
                "Serial CONFIG READY 처리 생략: 현재 Serial 연결 상태가 아닙니다."
            )
            return False

        if self.serial_conn is None or not self.serial_conn.is_open:
            self._log(
                "Serial CONFIG READY 처리 생략: Serial Port가 열려 있지 않습니다."
            )
            return False

        try:
            payload = self._build_config_dict()
        except Exception as e:
            self._log(
                f"Serial CONFIG READY 설정 생성 오류: {e}"
            )
            self._sensor_log(
                "TX",
                f"[SERIAL] CONFIG READY RESPONSE 실패: 설정값 오류 {e}",
            )
            return False

        return self._send_control_payload(
            payload,
            "CONFIG READY RESPONSE",
        )

    def _send_control_payload(self, payload_obj, label):
        """
        Serial:
          현재 COM Port로 JSON + CRLF를 1회 전송.
          센서가 {"CONFIG":"READY"}를 보내면 _send_serial_config_on_ready()가
          현재 설정을 만들어 이 함수를 통해 자동 1회 전송한다.

        MQTT:
          선택한 각 dvicMacAddr의 S/{MAC} Topic에
          현재 설정 JSON을 retain=True로 publish하여
          Broker의 retained 설정값을 갱신한다.
        """
        if not self.connected:
            messagebox.showwarning(
                "센서 설정",
                "먼저 MQTT 또는 Serial 연결을 해주세요.",
            )
            return False

        payload_json = json.dumps(
            payload_obj, ensure_ascii=False, separators=(",", ":")
        )
        if len(payload_json.encode("utf-8")) + 1 > 1024:
            raise ValueError("모드 A command는 CR 포함 1024바이트 이하여야 합니다.")
        # 모드 A Serial command 종료는 '}' 다음 CR 1바이트.
        # MQTT command payload는 JSON 본문만 전송한다.
        payload_text = payload_json + ("\r" if self.active_transport == "SERIAL" else "")

        try:
            if self.active_transport == "SERIAL":
                if (
                    self.serial_conn is None
                    or not self.serial_conn.is_open
                ):
                    raise RuntimeError(
                        "Serial Port가 열려 있지 않습니다."
                    )

                data = payload_text.encode("utf-8")
                written = self.serial_conn.write(data)
                self.serial_conn.flush()

                self._log(
                    f"{label} Serial 전송 완료 "
                    f"({written} bytes): "
                    f"{payload_text.strip()}"
                )
                self._sensor_log(
                    "TX",
                    (
                        f"[SERIAL] COM={self.serial_port.get().strip()} "
                        f"{label} {payload_text.strip()}"
                    ),
                )
                return True

            if self.active_transport == "MQTT":
                if self.client is None:
                    raise RuntimeError(
                        "MQTT Client가 없습니다."
                    )

                devices = self._selected_command_devices()

                if not devices:
                    raise ValueError(
                        "MQTT 설정 대상 센서 MAC이 선택되지 않았습니다.\n"
                        "MQTT 설정의 센서 MAC 목록에서 한 개 이상 선택하세요."
                    )

                qos = 1  # 모드 A command QoS 고정
                success_count = 0
                failures = []

                for device_id in devices:
                    # 센서 설정은 Broker의 S/{MAC} retained message로 관리
                    topic = f"S/{device_id}"

                    try:
                        info = self.client.publish(
                            topic,
                            payload=payload_text,
                            qos=qos,
                            retain=True,
                        )

                        rc = getattr(info, "rc", None)

                        self._log(
                            f"{label} MQTT publish "
                            f"target={device_id} "
                            f"topic={topic} "
                            f"QoS={qos} retain=True rc={rc}: "
                            f"{payload_text.strip()}"
                        )
                        self._sensor_log(
                            "TX",
                            (
                                f"[MQTT] topic={topic} "
                                f"target={device_id} "
                                f"QoS={qos} retain=True rc={rc} "
                                f"{label} {payload_text.strip()}"
                            ),
                        )

                        if rc not in (
                            None,
                            mqtt.MQTT_ERR_SUCCESS,
                        ):
                            failures.append(
                                f"{device_id}(rc={rc})"
                            )
                        else:
                            success_count += 1

                    except Exception as e:
                        failures.append(
                            f"{device_id}({e})"
                        )

                if failures:
                    raise RuntimeError(
                        f"{label} 일부/전체 전송 실패\n"
                        f"성공 {success_count}/{len(devices)}\n"
                        f"실패: {', '.join(failures)}"
                    )

                self._log(
                    f"{label} MQTT retained 설정 갱신 완료: "
                    f"{success_count}개 센서"
                )
                return True

            raise RuntimeError(
                f"지원하지 않는 통신 방식: "
                f"{self.active_transport}"
            )

        except Exception as e:
            self._log(
                f"{label} 전송 오류: {e}"
            )
            messagebox.showerror(
                "센서 설정 전송 오류",
                str(e),
            )
            return False

    def request_current_sensor_config(self):
        """
        선택한 MQTT 센서 1대의 현재 설정을 조회한다.

        확인된 설정 조회 프로토콜:
          Request Topic : S/{MAC}
          Payload       : {"CONFIG":"?"}
          Retain        : False

        설정 응답은 수신 MQTT 메시지에서 처리한다.
        """
        if (
            not self.connected
            or self.active_transport != "MQTT"
            or self.client is None
        ):
            messagebox.showwarning(
                "현재 설정 조회",
                "MQTT에 먼저 연결하세요.",
            )
            return

        devices = self._selected_command_devices()

        if len(devices) != 1:
            messagebox.showwarning(
                "현재 설정 조회",
                "현재 설정 조회는 센서 1대만 선택해서 실행하세요.",
            )
            return

        device_id = devices[0]
        topic = f"S/{device_id}"
        payload_text = json.dumps(
            {"CONFIG": "?"},
            ensure_ascii=False,
            separators=(",", ":"),
        )

        try:
            qos = int(self.qos.get())
            info = self.client.publish(
                topic,
                payload=payload_text,
                qos=qos,
                retain=False,
            )
            rc = getattr(info, "rc", None)

            if rc not in (None, mqtt.MQTT_ERR_SUCCESS):
                raise RuntimeError(
                    f"MQTT publish 실패 rc={rc}"
                )

            self.pending_config_query_device = device_id
            self.config_read_status_var.set(
                f"{device_id} 현재 설정 응답 대기 중..."
            )

            self._log(
                f"CONFIG QUERY MQTT publish "
                f"target={device_id} topic={topic} "
                f"QoS={qos} retain=False rc={rc}: "
                f"{payload_text}"
            )
            self._sensor_log(
                "TX",
                (
                    f"[MQTT] topic={topic} target={device_id} "
                    f"QoS={qos} retain=False "
                    f"CONFIG QUERY {payload_text}"
                ),
            )

        except Exception as e:
            self.pending_config_query_device = None
            self.config_read_status_var.set(
                f"현재 설정 조회 실패: {e}"
            )
            self._log(f"CONFIG QUERY 오류: {e}")
            messagebox.showerror(
                "현재 설정 조회 오류",
                str(e),
            )

    @staticmethod
    def _adxl_rate_value_to_hz(value):
        """
        센서 설정 응답의 ADXL/MEMS Sample Rate를 UI의 Hz 문자열로 변환.
        0x0A~0x0F 코드 또는 실제 Hz 문자열을 모두 허용한다.
        """
        s = str(value).strip()

        if not s:
            return ""

        try:
            if s.lower().startswith("0x"):
                code = int(s, 16)
                hz = ADXL_CODE_TO_RATE.get(code)
                return str(int(hz)) if hz is not None else ""

            # 0x prefix 없는 레지스터 HEX 코드(예: "0F")도 지원.
            if len(s) == 2 and all(ch in "0123456789abcdefABCDEF" for ch in s):
                hz = ADXL_CODE_TO_RATE.get(int(s, 16))
                if hz is not None:
                    return str(int(hz))

            # "3200", "3200.0" 형태
            numeric = float(s)
            if numeric.is_integer():
                return str(int(numeric))
            return str(numeric)

        except Exception:
            return ""

    def _apply_current_sensor_config(self, device_id, cfg):
        """
        센서가 응답한 현재 설정을 UI와 그래프 기본 Fs/N에 반영한다.
        Tk StringVar는 반드시 UI Thread에서만 변경한다.
        """
        sensor_type_code = str(
            cfg.get("SENSORTYPE", "")
        ).strip()

        if "SENSING_PERIOD" in cfg:
            self.cfg_period.set(
                str(cfg.get("SENSING_PERIOD", "")).strip()
            )

        applied_sensor = None
        rate = ""
        count = ""

        if sensor_type_code in ("1", "3"):
            applied_sensor = "PIEZO"
            if "PIEZO_GAIN" in cfg:
                self.cfg_piezo_gain.set(str(cfg["PIEZO_GAIN"]))
            rate = str(
                cfg.get("PIEZO_SAMPLE_RATE", "")
            ).strip()
            count = str(
                cfg.get("PIEZO_SAMPLE_COUNT", "")
            ).strip()

        elif sensor_type_code in ("2", "4"):
            applied_sensor = "ADXL"
            gcode = str(cfg.get("MEMS_G_RANGE", "")).strip()
            if gcode in ("0", "1", "2", "3"):
                self.cfg_mems_g_range.set({"0": "2", "1": "4", "2": "8", "3": "16"}[gcode])
            rate = self._adxl_rate_value_to_hz(
                cfg.get(
                    "MEMS_SAMPLE_RATE",
                    cfg.get("ADXL_SAMPLE_RATE", ""),
                )
            )
            count = str(
                cfg.get(
                    "MEMS_SAMPLE_COUNT",
                    cfg.get("ADXL_SAMPLE_COUNT", ""),
                )
            ).strip()

        # STOP은 어떤 센서 설정을 마지막으로 사용했는지 SENSORTYPE만으로는
        # 알 수 없으므로 센서 종류/프로필 선택은 건드리지 않는다.
        if sensor_type_code in ("1", "2", "3", "4"):
            self.cfg_sensor_type_code.set(sensor_type_code)
            self._update_sensortype_description()
        if applied_sensor is not None:
            profile_map = self._profile_map_for_sensor(
                applied_sensor
            )
            label = f"{rate} Hz + {count}"

            self.run_sensor_type.set(applied_sensor)
            self._last_sensor_type = applied_sensor

            labels = self._profile_labels_for_sensor(
                applied_sensor
            )
            self.cfg_sample_profile_combo["values"] = labels

            if label in profile_map:
                self.sample_profile_state[
                    applied_sensor
                ] = label
                self.cfg_sample_profile.set(label)

                self.graph_sample_defaults[
                    applied_sensor
                ] = (
                    float(rate),
                    int(count),
                )
            else:
                # 현재 센서값이 UI 기준표에 없는 경우 값을 임의 변경하지 않는다.
                self._log(
                    f"CONFIG RESPONSE {device_id}: "
                    f"지원 목록에 없는 {applied_sensor} Fs/N "
                    f"{rate} Hz + {count}"
                )

        self._update_config_preview()

        # 현재 설정 조회 응답은 센서의 실제 Fs/N이므로 FFT에도 반영.
        if applied_sensor is not None and rate and count:
            try:
                self._sync_fft_graph_config(
                    applied_sensor,
                    float(rate),
                    int(count),
                    reason="현재 설정 조회",
                )
            except Exception:
                pass

        mode_text = {
            "0": "STOP",
            "1": "PIEZO",
            "2": "ADXL",
        }.get(sensor_type_code, sensor_type_code or "UNKNOWN")

        profile_text = (
            f"{rate} Hz + {count}"
            if rate and count
            else "-"
        )

        self.config_read_status_var.set(
            f"조회 완료: {device_id} | "
            f"SENSORTYPE={mode_text} | "
            f"Fs+N={profile_text} | "
            f"주기={cfg.get('SENSING_PERIOD', '-')}"
        )

        self._log(
            f"CONFIG RESPONSE 적용 {device_id}: "
            f"{json.dumps(cfg, ensure_ascii=False)}"
        )
        self._sensor_log(
            "RX",
            (
                f"[MQTT] CONFIG RESPONSE "
                f"device={device_id} "
                f"{json.dumps(cfg, ensure_ascii=False)}"
            ),
        )

    def send_ap_config(self):
        # 설정 적용은 SENSORTYPE 드롭다운에서 선택한 모드 그대로 실행한다.
        # 0은 retained STOP/삭제/normal 복귀 전체 절차를 거친다.
        if self.cfg_sensor_type_code.get().strip() == "0":
            self.send_start_command("0")
            return
        try:
            payload = self._build_config_dict()
        except Exception as e:
            messagebox.showerror(
                "설정값 오류",
                str(e),
            )
            return

        sent = self._send_control_payload(
            payload,
            "RETAIN CONFIG",
        )

        if sent:
            self._sync_fft_graph_config_from_payload(
                payload,
                reason="설정 적용",
            )

    def send_selected_sensor_start(self):
        """모드 A collection START: PIEZO=3, ADXL=4."""
        try:
            mode = self.run_sensor_type.get().strip().upper()
            code = "3" if mode == "PIEZO" else "4" if mode == "ADXL" else None
            if code is None:
                raise ValueError("PIEZO 또는 ADXL을 선택하세요.")
            self.cfg_sensor_type_code.set(code)
            self._update_sensortype_description()
            payload = self._build_config_dict(sensor_type_override=code)
        except Exception as e:
            messagebox.showerror("설정값 오류", str(e))
            return

        sent = self._send_control_payload(
            payload,
            "RETAIN COLLECTION START",
        )

        if sent:
            self._sync_fft_graph_config_from_payload(
                payload,
                reason="START",
            )

    def _mqtt_collection_stop(self, stop_payload, normal_payload):
        """모드 A STOP: retained STOP -> PUBACK -> retained 삭제 -> PUBACK -> normal.

        모든 단계에 QoS 1을 강제하며 실패 시 다음 단계로 진행하지 않는다.
        """
        if not self.connected or self.active_transport != "MQTT" or self.client is None:
            raise RuntimeError("MQTT 연결 상태가 아닙니다.")
        devices = self._selected_command_devices()
        if not devices:
            raise RuntimeError("MQTT 대상 MAC을 선택하세요.")
        stop_bytes = json.dumps(stop_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        normal_bytes = json.dumps(normal_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(stop_bytes) + 1 > 1024 or len(normal_bytes) + 1 > 1024:
            raise ValueError("모드 A command 길이가 1024바이트를 초과했습니다.")
        for device_id in devices:
            topic = "S/" + device_id
            for step, body in (
                ("STOP retained", stop_bytes),
                ("retained 삭제", b""),
                ("normal retained", normal_bytes),
            ):
                info = self.client.publish(topic, payload=body, qos=1, retain=True)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError("{} {} publish 오류 rc={}".format(device_id, step, info.rc))
                # Paho MQTT network loop의 PUBACK를 기다린 뒤 다음 단계 발행.
                info.wait_for_publish(timeout=10)
                if not info.is_published():
                    raise RuntimeError("{} {} PUBACK 시간 초과".format(device_id, step))
                self._log("[MODE A] {} {} QoS=1 retain=True PUBACK 확인".format(topic, step))
                self._sensor_log("TX", "[MQTT] {} {} PUBACK 확인".format(topic, step))
        return True

    def send_start_command(self, start_value):
        """모드 A collection STOP(0) + MQTT retained 삭제 + normal 복귀."""
        if str(start_value) != "0":
            try:
                payload = self._build_config_dict(sensor_type_override=str(start_value))
            except Exception as e:
                messagebox.showerror("설정값 오류", str(e))
                return
            self._send_control_payload(payload, "RETAIN CONFIG")
            return
        try:
            stop_payload = {"COMMPATH": self._current_commpath(), "SENSORTYPE": "0"}
            if self.active_transport == "MQTT":
                normal_code = "1" if self.run_sensor_type.get().strip().upper() == "PIEZO" else "2"
                normal_payload = self._build_config_dict(sensor_type_override=normal_code)
                self._mqtt_collection_stop(stop_payload, normal_payload)
                self._sync_fft_graph_config_from_payload(normal_payload, reason="STOP normal 복귀")
            else:
                self._send_control_payload(stop_payload, "COLLECTION STOP")
            self.cfg_sensor_type_code.set(
                "1" if self.run_sensor_type.get().strip().upper() == "PIEZO" else "2"
            )
            self._update_sensortype_description()
            self._update_config_preview()
        except Exception as e:
            self._log("COLLECTION STOP 실패: {}".format(e))
            messagebox.showerror("COLLECTION STOP 오류", str(e))

    def _csv_writer_loop(self):
        """
        CSV 전용 Writer Thread.

        중요:
        - PIEZO / ULTRASONIC은 패킷 단위로 저장한다.
        - ADXL은 _queue_adxl_csv_item()에서 X/Y/Z 한 세트가 완성된
          경우에만 X/Y/Z 세 패킷이 이 Queue에 들어온다.
        - HEX 원본 행은 쓰는 즉시 flush 한다.
        - decoded 행도 쓰는 즉시 flush 한다.
        - 따라서 프로그램 실행 중 파일을 열어도 실제 데이터가 보인다.
        - decode 실패 시에도 해당 패킷의 HEX 원본은 반드시 남는다.
        """
        files = {}
        writers = {}

        common_header = [
            "packet_no",
            "received_at",
            "source",
            "dvicMacAddr",
            "gwMacAddr",
            "sensor_type",
            "axis",
            "seq",
            "tick",
            "batteryRmin",
            "samplerate",
            "numofsample",
            "payload_bytes",
            "acqStatus",
            "apSsid",
            "apRssi",
        ]

        hex_header = common_header + [
            "sensorData_bytes",
            "sensorData_hex",
        ]

        decoded_header = common_header + [
            "decoded_sample_count",
            "decoded_samples",
        ]

        ultrasonic_decoded_header = common_header + [
            "minKhz",
            "intervalHz",
            "freq_start_kHz",
            "freq_end_kHz",
            "decoded_sample_count",
            "decoded_samples",
        ]

        packet_counters = {}

        def get_writer(path, header):
            key = str(path)

            if key not in files:
                # A legacy CSV in the same session may have an older header.
                # Keep it intact; write the new schema to a versioned sibling.
                if path.exists() and path.stat().st_size > 0:
                    try:
                        with path.open("r", encoding="utf-8-sig", newline="") as old_f:
                            old_header = next(csv.reader(old_f), [])
                        if old_header != header:
                            path = path.with_name(path.stem + "_v040" + path.suffix)
                            key = str(path)
                    except (OSError, UnicodeError, csv.Error):
                        path = path.with_name(path.stem + "_v040" + path.suffix)
                        key = str(path)
                is_new = (
                    not path.exists()
                    or path.stat().st_size == 0
                )

                f = path.open(
                    "a",
                    newline="",
                    encoding="utf-8-sig",
                    buffering=1024 * 1024,
                )
                writer = csv.writer(f)

                files[key] = f
                writers[key] = writer

                if is_new:
                    writer.writerow(header)
                    # 헤더도 즉시 저장
                    f.flush()

            return key, writers[key], files[key]

        try:
            while (
                not self.csv_stop.is_set()
                or not self.csv_queue.empty()
            ):
                try:
                    item = self.csv_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                try:
                    session_dir = self.csv_session_dir

                    if session_dir is None:
                        session_dir = (
                            self.csv_root
                            / "unspecified_session"
                        )

                    session_dir.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    device = safe_filename(item.device_id)
                    sensor_name = safe_filename(item.kind)

                    packet_key = (
                        item.device_id,
                        item.kind,
                    )
                    packet_no = (
                        packet_counters.get(packet_key, 0)
                        + 1
                    )
                    packet_counters[packet_key] = packet_no

                    base_row = [
                        packet_no,
                        item.received_at,
                        item.source,
                        item.device_id,
                        item.gw_mac,
                        item.kind,
                        item.axis,
                        item.seq,
                        item.tick,
                        item.battery,
                        item.sample_rate,
                        item.num_samples,
                        item.payload_bytes,
                        item.acq_status,
                        item.ap_ssid,
                        item.ap_rssi,
                    ]

                    # ---------------------------------------------
                    # 1. HEX 원본: 무조건 먼저 쓰고 즉시 flush
                    # ---------------------------------------------
                    hex_path = (
                        session_dir
                        / f"{device}_{sensor_name}_sensorData_hex.csv"
                    )

                    _hex_key, hex_writer, hex_file = get_writer(
                        hex_path,
                        hex_header,
                    )

                    sensor_data_bytes = (
                        len(item.sensor_data) // 2
                        if isinstance(item.sensor_data, str)
                        else 0
                    )

                    hex_writer.writerow(
                        base_row
                        + [
                            sensor_data_bytes,
                            item.sensor_data,
                        ]
                    )

                    # 테스트 중 파일을 열어도 바로 보이게 즉시 flush
                    hex_file.flush()

                    # ---------------------------------------------
                    # 2. Decode
                    # 성공한 경우에만 decoded 파일을 생성/기록
                    # ---------------------------------------------
                    try:
                        ultrasonic_meta = None
                        if item.kind == "PIEZO":
                            decoded = decode_piezo_samples(
                                item.sensor_data,
                                item.num_samples,
                            )
                        elif item.kind == "ULTRASONIC":
                            decoded, _h_freq_khz, ultrasonic_meta = (
                                decode_ultrasonic_samples(
                                    item.sensor_data
                                )
                            )
                        else:
                            decoded = decode_vibration_samples(
                                item.sensor_data,
                                item.num_samples,
                            )
                    except Exception as decode_error:
                        raise RuntimeError(
                            f"HEX 저장 완료 / Decode 실패 "
                            f"{item.device_id}/{item.kind} "
                            f"seq={item.seq}: {decode_error}"
                        )

                    decoded_path = (
                        session_dir
                        / f"{device}_{sensor_name}_decoded_samples.csv"
                    )

                    if item.kind == "ULTRASONIC":
                        decoded_file_header = ultrasonic_decoded_header
                        decoded_text = ",".join(
                            format(float(value), ".9g")
                            for value in decoded
                        )
                    else:
                        decoded_file_header = decoded_header
                        decoded_text = ",".join(
                            str(int(value))
                            for value in decoded
                        )

                    _dec_key, decoded_writer, decoded_file = get_writer(
                        decoded_path,
                        decoded_file_header,
                    )

                    if item.kind == "ULTRASONIC":
                        decoded_writer.writerow(
                            base_row
                            + [
                                ultrasonic_meta["minKhz"],
                                ultrasonic_meta["intervalHz"],
                                f"{ultrasonic_meta['freq_start_khz']:.9g}",
                                f"{ultrasonic_meta['freq_end_khz']:.9g}",
                                len(decoded),
                                decoded_text,
                            ]
                        )
                    else:
                        decoded_writer.writerow(
                            base_row
                            + [
                                len(decoded),
                                decoded_text,
                            ]
                        )

                    # decoded 역시 즉시 flush
                    decoded_file.flush()

                    with self.csv_counter_lock:
                        self.csv_saved_count += 1

                except Exception as e:
                    with self.csv_counter_lock:
                        self.csv_error_count += 1

                    self.ui_queue.put(
                        (
                            "log",
                            f"CSV 저장/Decode 오류: {e}",
                        )
                    )

                finally:
                    self.csv_queue.task_done()

        finally:
            for f in files.values():
                try:
                    f.flush()
                    f.close()
                except Exception:
                    pass

    def _start_csv_session(self):
        # 이전 세션의 ADXL 미완성 축이 새 세션과 섞이지 않게 초기화.
        self._clear_adxl_csv_pending()

        if not self.csv_enabled.get():
            self.csv_session_dir = None
            self.csv_folder_var.set("CSV 저장 비활성")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_session_dir = self.csv_root / stamp
        self.csv_session_dir.mkdir(parents=True, exist_ok=True)
        self.csv_folder_var.set(str(self.csv_session_dir))

        with self.csv_counter_lock:
            self.csv_saved_count = 0
            self.csv_error_count = 0

        self._log(f"CSV 저장 폴더: {self.csv_session_dir}")

    def _graph_decode_loop(self):
        while not self.graph_stop.is_set():
            try:
                item = self.graph_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                if isinstance(item, UltrasonicGraphItem):
                    samples, freq_khz, h_meta = decode_ultrasonic_samples(
                        item.sensor_data
                    )
                    record = self.ultrasonic_store.update(
                        device_id=item.device_id,
                        samples=samples,
                        freq_khz=freq_khz,
                        min_khz=h_meta["minKhz"],
                        interval_hz=h_meta["intervalHz"],
                        recv_ns=item.recv_ns,
                        seq=item.seq,
                    )

                    diag_key = (
                        item.device_id,
                        "ULTRASONIC",
                        "H",
                    )
                    now = time.monotonic()
                    last = self.fft_diag_last_log.get(diag_key, 0.0)
                    if now - last >= self.fft_diag_log_interval_sec:
                        self.fft_diag_last_log[diag_key] = now
                        self.ui_queue.put((
                            "log",
                            (
                                f"[H CHECK] {item.device_id}/ULTRASONIC | "
                                f"N={record['sample_count']} | "
                                f"minKhz={record['minKhz']} | "
                                f"intervalHz={record['intervalHz']} | "
                                f"Range={record['freq_khz'][0]:.3f}~"
                                f"{record['freq_khz'][-1]:.3f} kHz | "
                                f"Peak={record['peak_value']:.9g} @ "
                                f"{record['peak_khz']:.3f} kHz"
                            ),
                        ))
                    continue

                if item.sensor_kind == "PIEZO":
                    samples = decode_piezo_samples(
                        item.sensor_data,
                        item.numofsample,
                    )
                else:
                    samples = decode_vibration_samples(
                        item.sensor_data,
                        item.numofsample,
                    )

                target_store = (
                    self.adxl_store
                    if item.sensor_kind == "ADXL"
                    else self.piezo_store
                )

                first_graph_packet = str(item.device_id) not in target_store.device_list()
                record = target_store.update(
                    device_id=item.device_id,
                    axis=item.axis,
                    samples=samples,
                    sample_rate=item.sample_rate,
                    recv_ns=item.recv_ns,
                    seq=item.seq,
                    numofsample=item.numofsample,
                    packet_sample_rate=item.packet_sample_rate,
                    packet_numofsample=item.packet_numofsample,
                    fft_window=self.fft_window_by_mode.get(
                        item.sensor_kind,
                        "Blackman",
                    ),
                    fft_window_beta=self.fft_window_beta_by_mode.get(
                        item.sensor_kind,
                        8.6,
                    ),
                    fft_window_size=self.fft_window_size_by_mode.get(
                        item.sensor_kind,
                        item.numofsample,
                    ),
                    g_range=item.g_range,
                    gain=item.gain,
                    accumulate_piezo_pp=(item.sensor_kind == "PIEZO"),
                )

                if first_graph_packet:
                    self.ui_queue.put((
                        "log",
                        f"[GRAPH STORE] {item.sensor_kind} 등록: {item.device_id} | "
                        f"목록={target_store.device_list()} | N={len(samples)}"
                    ))

                # V018: 그래프 저장소 등록 및 목록 갱신은 설정 파일 I/O와 독립적으로 수행한다.
                # 전압 설정 등록에 실패하더라도 수신 그래프 데이터는 이미 저장된 상태이다.
                if item.sensor_kind == "PIEZO":
                    device_key = str(item.device_id or "").strip()
                    if device_key and device_key not in self.piezo_device_voltage_known:
                        try:
                            current_voltages = load_piezo_device_voltages(self.piezo_device_voltage_path)
                            if device_key not in current_voltages:
                                current_voltages[device_key] = float(PIEZO_ADC_VMAX)
                                save_piezo_device_voltages(current_voltages, self.piezo_device_voltage_path)
                                self.ui_queue.put(("log", f"[V018] PIEZO 전압 설정 자동 등록: {device_key}={PIEZO_ADC_VMAX:.3f}V | {self.piezo_device_voltage_path}"))
                            self.piezo_device_voltage_known.add(device_key)
                        except Exception as exc:
                            self.ui_queue.put(("log", f"[V018] PIEZO 전압 파일 저장 실패(그래프는 유지): {self.piezo_device_voltage_path} ({exc})"))

                diag_key = (
                    item.device_id,
                    item.sensor_kind,
                    item.axis,
                )
                now = time.monotonic()
                last = self.fft_diag_last_log.get(diag_key, 0.0)

                if now - last >= self.fft_diag_log_interval_sec:
                    self.fft_diag_last_log[diag_key] = now

                    packet_fs = (
                        item.packet_sample_rate
                        if str(item.packet_sample_rate).strip()
                        else "(없음)"
                    )
                    packet_n = (
                        item.packet_numofsample
                        if str(item.packet_numofsample).strip()
                        else "(없음)"
                    )

                    if item.sensor_kind == "ADXL":
                        g_range = normalize_adxl_g_range(
                            item.g_range
                        )
                        range_text = (
                            f"±{g_range}g"
                            if g_range is not None
                            else "unknown"
                        )

                        peak_text = (
                            f"{record['peak_magnitude'] / get_mems_lsb_per_g() / np.sqrt(2.0):.6g} g RMS"
                        )
                        raw_mean_text = (
                            f"{record['raw_mean'] / get_mems_lsb_per_g():.6g} g"
                        )
                        raw_min_text = (
                            f"{record['raw_min'] / get_mems_lsb_per_g():.6g} g"
                        )
                        raw_max_text = (
                            f"{record['raw_max'] / get_mems_lsb_per_g():.6g} g"
                        )
                        raw_rms_text = (
                            f"{record['raw_rms'] / get_mems_lsb_per_g():.6g} g"
                        )
                        scale_text = (
                            f"Range={range_text} | "
                            f"Scale={get_mems_lsb_per_g():g} LSB/g FullRes | "
                        )
                    else:
                        peak_text = (
                            f"{record['peak_magnitude']:.6g} count Peak"
                        )
                        raw_mean_text = (
                            f"{record['raw_mean']:.6g} count"
                        )
                        raw_min_text = (
                            f"{record['raw_min']:.6g} count"
                        )
                        raw_max_text = (
                            f"{record['raw_max']:.6g} count"
                        )
                        raw_rms_text = (
                            f"{record['raw_rms']:.6g} count"
                        )
                        scale_text = ""

                    self.ui_queue.put(
                        (
                            "log",
                            (
                                f"[FFT CHECK] "
                                f"{item.device_id}/{item.sensor_kind}-{item.axis} | "
                                f"Packet Fs={packet_fs} | "
                                f"FFT Fs={record['sample_rate']:.6g} Hz | "
                                f"Packet N={packet_n} | "
                                f"Window N={record.get('fft_window_size', record['fft_n'])} | "
                                f"FFT N={record['fft_n']} | "
                                f"Window={record.get('fft_window','Hann')}"
                                f"{('(β=' + format(float(record.get('fft_window_beta',8.6)), '.2f') + ')') if record.get('fft_window') == 'Kaiser' else ''} | "
                                f"{scale_text}"
                                f"Peak Bin={record['peak_bin']} | "
                                f"Peak Hz={record['peak_hz']:.6g} | "
                                f"Peak Mag={peak_text} | "
                                f"Raw Mean={raw_mean_text} | "
                                f"Raw Min={raw_min_text} | "
                                f"Raw Max={raw_max_text} | "
                                f"Raw AC RMS={raw_rms_text}"
                            ),
                        )
                    )

            except Exception as e:
                # UltrasonicGraphItem has no sensor_kind or axis attributes.
                # A malformed/unsupported frame must never stop this worker.
                if isinstance(item, UltrasonicGraphItem):
                    kind, axis = "ULTRASONIC", "H"
                else:
                    kind = getattr(item, "sensor_kind", "UNKNOWN")
                    axis = getattr(item, "axis", "?")
                self.ui_queue.put((
                    "log",
                    f"{kind} Graph Decode 오류 "
                    f"{getattr(item, 'device_id', '?')}/{axis} "
                    f"seq={getattr(item, 'seq', '')}: {e} "
                    "(해당 프레임 제외, 그래프 처리 계속)"
                ))

            finally:
                self.graph_queue.task_done()

    @staticmethod
    def _parse_y_limit_pair(min_var, max_var, label="Y축"):
        """
        Y축 입력값을 (min, max)로 반환.
        둘 다 빈칸이면 None(자동), 하나만 입력하면 오류.
        """
        min_text = min_var.get().strip()
        max_text = max_var.get().strip()

        if not min_text and not max_text:
            return None

        if not min_text or not max_text:
            raise ValueError(
                f"{label}: 최소값과 최대값을 모두 입력하거나 둘 다 비워주세요."
            )

        try:
            y_min = float(min_text)
            y_max = float(max_text)
        except Exception:
            raise ValueError(f"{label}: 숫자만 입력할 수 있습니다.")

        if not np.isfinite(y_min) or not np.isfinite(y_max):
            raise ValueError(f"{label}: 유한한 숫자를 입력하세요.")

        if y_min >= y_max:
            raise ValueError(f"{label}: 최소값은 최대값보다 작아야 합니다.")

        return y_min, y_max

    def _validate_and_apply_y_limits(self, sensor_kind):
        try:
            if sensor_kind == "ADXL":
                self._parse_y_limit_pair(
                    self.adxl_raw_y_min_var,
                    self.adxl_raw_y_max_var,
                    "ADXL Raw Y축",
                )
                self._parse_y_limit_pair(
                    self.adxl_fft_y_min_var,
                    self.adxl_fft_y_max_var,
                    "ADXL FFT Y축",
                )
                self._force_graph_refresh()
            else:
                self._parse_y_limit_pair(
                    self.piezo_raw_y_min_var,
                    self.piezo_raw_y_max_var,
                    "PIEZO Raw Y축",
                )
                self._parse_y_limit_pair(
                    self.piezo_fft_y_min_var,
                    self.piezo_fft_y_max_var,
                    "PIEZO FFT Y축",
                )
                self._force_piezo_graph_refresh()
        except ValueError as e:
            messagebox.showwarning("Y축 범위", str(e))

    def _build_y_limit_bar(
        self,
        parent,
        raw_min_var,
        raw_max_var,
        fft_min_var,
        fft_max_var,
        apply_callback,
    ):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=10, pady=(0, 6))

        ttk.Label(bar, text="Y축 고정").pack(side="left", padx=(0, 8))

        ttk.Label(bar, text="Raw 최소").pack(side="left")
        ttk.Entry(
            bar, textvariable=raw_min_var, width=9
        ).pack(side="left", padx=(3, 6))

        ttk.Label(bar, text="최대").pack(side="left")
        ttk.Entry(
            bar, textvariable=raw_max_var, width=9
        ).pack(side="left", padx=(3, 12))

        ttk.Label(bar, text="FFT 최소").pack(side="left")
        ttk.Entry(
            bar, textvariable=fft_min_var, width=9
        ).pack(side="left", padx=(3, 6))

        ttk.Label(bar, text="최대").pack(side="left")
        ttk.Entry(
            bar, textvariable=fft_max_var, width=9
        ).pack(side="left", padx=(3, 8))

        ttk.Button(
            bar,
            text="Y축 적용",
            command=apply_callback,
        ).pack(side="left", padx=5)

        ttk.Label(
            bar,
            text="(최소/최대 모두 빈칸 = 자동)",
        ).pack(side="left", padx=8)

        return bar

    def open_piezo_graph(self):
        if (
            self.piezo_graph_window is not None
            and self.piezo_graph_window.winfo_exists()
        ):
            self.piezo_graph_window.deiconify()
            self.piezo_graph_window.lift()
            return

        win = tk.Toplevel(self.root)
        self.piezo_graph_window = win
        win.title("PIEZO Raw + FFT")
        win.geometry("1100x760")
        win.minsize(820, 600)

        top = ttk.Frame(win)
        top.pack(fill="x", padx=10, pady=(10, 5))

        ttk.Label(top, text="PIEZO Device").pack(side="left", padx=(0, 5))

        devices = self.piezo_store.device_list()
        initial = devices[0] if devices else ""

        self.piezo_graph_device_var = tk.StringVar(value=initial)
        self.piezo_graph_device_combo = ttk.Combobox(
            top,
            textvariable=self.piezo_graph_device_var,
            values=devices,
            state="readonly",
            width=24,
        )
        self.piezo_graph_device_combo.pack(side="left", padx=5)
        self.piezo_graph_device_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._reset_graph_views(
                "piezo_raw",
                "piezo_fft",
            ),
        )

        ttk.Button(
            top,
            text="센서 목록 새로고침",
            command=self._refresh_piezo_graph_device_list,
        ).pack(side="left", padx=8)

        ttk.Label(
            top,
            text="휠: 확대/축소 | 드래그: 이동 | 더블클릭: 초기화",
        ).pack(side="left", padx=15)

        self._build_y_limit_bar(
            win,
            self.piezo_raw_y_min_var,
            self.piezo_raw_y_max_var,
            self.piezo_fft_y_min_var,
            self.piezo_fft_y_max_var,
            lambda: self._validate_and_apply_y_limits("PIEZO"),
        )

        self.piezo_graph_info_var = tk.StringVar(
            value="PIEZO 데이터 수신 대기 중..."
        )
        ttk.Label(
            win,
            textvariable=self.piezo_graph_info_var,
        ).pack(fill="x", padx=12, pady=(0, 5))

        grid = ttk.Frame(win)
        grid.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        grid.columnconfigure(0, weight=1)
        grid.rowconfigure(0, weight=1)
        grid.rowconfigure(1, weight=1)

        raw_frame = ttk.LabelFrame(grid, text="PIEZO Z Raw")
        raw_frame.grid(
            row=0, column=0, sticky="nsew",
            padx=4, pady=4,
        )
        self.piezo_graph_canvas_raw = tk.Canvas(
            raw_frame, highlightthickness=0
        )
        self.piezo_graph_canvas_raw.pack(fill="both", expand=True)
        self._bind_graph_navigation(
            self.piezo_graph_canvas_raw,
            "piezo_raw",
            self._force_piezo_graph_refresh,
        )

        fft_frame = ttk.LabelFrame(grid, text="PIEZO Z FFT")
        fft_frame.grid(
            row=1, column=0, sticky="nsew",
            padx=4, pady=4,
        )
        self.piezo_graph_canvas_fft = tk.Canvas(
            fft_frame, highlightthickness=0
        )
        self.piezo_graph_canvas_fft.pack(fill="both", expand=True)
        self._bind_graph_navigation(
            self.piezo_graph_canvas_fft,
            "piezo_fft",
            self._force_piezo_graph_refresh,
        )

        self.piezo_graph_last_version = -1

        def on_close():
            try:
                win.destroy()
            except Exception:
                pass

            self.piezo_graph_window = None
            self.piezo_graph_device_var = None
            self.piezo_graph_canvas_raw = None
            self.piezo_graph_canvas_fft = None
            self.piezo_graph_info_var = None
            for key in ("piezo_raw", "piezo_fft"):
                self.graph_view_states.pop(key, None)

        win.protocol("WM_DELETE_WINDOW", on_close)
        win.after(
            GRAPH_REFRESH_MS,
            self._refresh_piezo_graph,
        )

    def _refresh_piezo_graph_device_list(self):
        devices = self.piezo_store.device_list()

        if not hasattr(self, "piezo_graph_device_combo"):
            return

        self.piezo_graph_device_combo["values"] = devices

        if (
            self.piezo_graph_device_var is not None
            and not self.piezo_graph_device_var.get()
            and devices
        ):
            self.piezo_graph_device_var.set(devices[0])

        self._force_piezo_graph_refresh()

    def _force_piezo_graph_refresh(self):
        self.piezo_graph_last_version = -1

    def _refresh_piezo_graph(self):
        win = self.piezo_graph_window
        if win is None:
            return

        try:
            if not win.winfo_exists():
                return
        except Exception:
            return

        devices = self.piezo_store.device_list()

        if hasattr(self, "piezo_graph_device_combo"):
            current_values = list(
                self.piezo_graph_device_combo["values"]
            )

            if current_values != devices:
                self.piezo_graph_device_combo["values"] = devices
                if (
                    self.piezo_graph_device_var is not None
                    and not self.piezo_graph_device_var.get()
                    and devices
                ):
                    self.piezo_graph_device_var.set(devices[0])

        device_id = (
            self.piezo_graph_device_var.get()
            if self.piezo_graph_device_var is not None
            else ""
        )

        if not device_id:
            if self.piezo_graph_info_var is not None:
                self.piezo_graph_info_var.set(
                    "PIEZO 센서 검색 중... 데이터가 들어오면 목록에 자동 추가됩니다."
                )

            win.after(
                GRAPH_REFRESH_MS,
                self._refresh_piezo_graph,
            )
            return

        version, axes = self.piezo_store.snapshot(device_id)

        if version != self.piezo_graph_last_version:
            self.piezo_graph_last_version = version
            record = axes.get("Z")

            if record is None:
                if self.piezo_graph_canvas_raw is not None:
                    self.piezo_graph_canvas_raw.delete("all")
                if self.piezo_graph_canvas_fft is not None:
                    self.piezo_graph_canvas_fft.delete("all")

                if self.piezo_graph_info_var is not None:
                    self.piezo_graph_info_var.set(
                        f"{device_id} | PIEZO Z 데이터 대기 중"
                    )
            else:
                samples = record["samples"]
                sample_rate = record["sample_rate"]
                freqs = record["freqs"]
                magnitude = record["magnitude"]

                # Raw(Time Data) X축은 Sample Index가 아니라
                # 패킷 시작 시점 기준 경과 시간(sec)으로 표시한다.
                #
                #   t[i] = i / Fs
                #
                # 예) Fs=25600 Hz:
                #     0, 0.0000390625, 0.000078125, ...
                if float(sample_rate) > 0:
                    time_axis = (
                        np.arange(
                            len(samples),
                            dtype=np.float64,
                        )
                        / float(sample_rate)
                    )
                else:
                    time_axis = np.arange(
                        len(samples),
                        dtype=np.float64,
                    )

                try:
                    piezo_raw_y_range = self._parse_y_limit_pair(
                        self.piezo_raw_y_min_var,
                        self.piezo_raw_y_max_var,
                        "PIEZO Raw Y축",
                    )
                except ValueError:
                    piezo_raw_y_range = None

                try:
                    piezo_fft_y_range = self._parse_y_limit_pair(
                        self.piezo_fft_y_min_var,
                        self.piezo_fft_y_max_var,
                        "PIEZO FFT Y축",
                    )
                except ValueError:
                    piezo_fft_y_range = None

                self._draw_canvas_graph(
                    self.piezo_graph_canvas_raw,
                    time_axis,
                    samples,
                    "Time (s)",
                    "int16",
                    view_key="piezo_raw",
                    line_color="#d62728",
                    fixed_y_range=piezo_raw_y_range,
                )

                self._draw_canvas_graph(
                    self.piezo_graph_canvas_fft,
                    freqs,
                    magnitude,
                    "Frequency (Hz)",
                    "Magnitude",
                    view_key="piezo_fft",
                    line_color="#d62728",
                    fixed_y_range=piezo_fft_y_range,
                )

                if self.piezo_graph_info_var is not None:
                    resolution = (
                        sample_rate / len(samples)
                        if len(samples)
                        else 0.0
                    )

                    packet_fs = (
                        record.get("packet_sample_rate", "")
                        or "(없음)"
                    )
                    packet_n = (
                        record.get("packet_numofsample", "")
                        or "(없음)"
                    )

                    self.piezo_graph_info_var.set(
                        f"{device_id} | "
                        f"Packet Fs={packet_fs} / FFT Fs={sample_rate:.6g} Hz | "
                        f"Packet N={packet_n} / FFT N={record.get('fft_n', len(samples))} | "
                        f"Peak Bin={record.get('peak_bin', -1)} / "
                        f"Peak={record.get('peak_hz', 0.0):.6g} Hz / "
                        f"Mag={record.get('peak_magnitude', 0.0):.6g} | "
                        f"Time X=0~{((len(samples) - 1) / sample_rate if len(samples) and sample_rate > 0 else 0.0):.6g} s | "
                        f"FFT X=0~{sample_rate / 2.0:g} Hz | "
                        f"Δf={resolution:.6g} Hz"
                    )

        win.after(
            GRAPH_REFRESH_MS,
            self._refresh_piezo_graph,
        )

    def open_adxl_graph(self):
        if (
            self.graph_window is not None
            and self.graph_window.winfo_exists()
        ):
            self.graph_window.deiconify()
            self.graph_window.lift()
            return

        win = tk.Toplevel(self.root)
        self.graph_window = win
        win.title("ADXL X/Y/Z Combined Raw + FFT")
        win.geometry("1260x860")
        win.minsize(940, 650)

        top = ttk.Frame(win)
        top.pack(fill="x", padx=10, pady=(10, 5))

        ttk.Label(top, text="ADXL Device").pack(
            side="left",
            padx=(0, 5),
        )

        devices = self.adxl_store.device_list()
        initial = devices[0] if devices else ""

        self.graph_device_var = tk.StringVar(value=initial)
        self.graph_device_combo = ttk.Combobox(
            top,
            textvariable=self.graph_device_var,
            values=devices,
            state="readonly",
            width=22,
        )
        self.graph_device_combo.pack(side="left", padx=5)
        self.graph_device_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: self._reset_graph_views(
                "adxl_raw",
                "adxl_fft",
            ),
        )

        ttk.Button(
            top,
            text="센서 목록 새로고침",
            command=self._refresh_graph_device_list,
        ).pack(side="left", padx=8)

        # 축 표시 체크박스
        self.graph_axis_vars = {
            "X": tk.BooleanVar(value=True),
            "Y": tk.BooleanVar(value=True),
            "Z": tk.BooleanVar(value=True),
        }

        axis_frame = ttk.LabelFrame(top, text="표시 축")
        axis_frame.pack(side="left", padx=14)

        axis_colors = {
            "X": "#1f77b4",  # blue
            "Y": "#2ca02c",  # green
            "Z": "#d62728",  # red
        }

        for axis in ("X", "Y", "Z"):
            cb = ttk.Checkbutton(
                axis_frame,
                text=axis,
                variable=self.graph_axis_vars[axis],
                command=self._force_graph_refresh,
            )
            cb.pack(side="left", padx=(6, 2), pady=3)

            # 색상 범례
            swatch = tk.Canvas(
                axis_frame,
                width=16,
                height=10,
                highlightthickness=0,
            )
            swatch.create_rectangle(
                1,
                1,
                15,
                9,
                fill=axis_colors[axis],
                outline=axis_colors[axis],
            )
            swatch.pack(side="left", padx=(0, 6))

        ttk.Label(
            top,
            text="휠: 확대/축소 | 드래그: 이동 | 더블클릭: 초기화",
        ).pack(side="left", padx=12)

        self._build_y_limit_bar(
            win,
            self.adxl_raw_y_min_var,
            self.adxl_raw_y_max_var,
            self.adxl_fft_y_min_var,
            self.adxl_fft_y_max_var,
            lambda: self._validate_and_apply_y_limits("ADXL"),
        )

        self.graph_info_var = tk.StringVar(
            value="ADXL X/Y/Z 데이터 수신 대기 중..."
        )
        ttk.Label(
            win,
            textvariable=self.graph_info_var,
        ).pack(fill="x", padx=12, pady=(0, 5))

        grid = ttk.Frame(win)
        grid.pack(
            fill="both",
            expand=True,
            padx=8,
            pady=(0, 8),
        )
        grid.columnconfigure(0, weight=1)
        grid.rowconfigure(0, weight=1)
        grid.rowconfigure(1, weight=1)

        self.graph_canvases = {}

        raw_frame = ttk.LabelFrame(
            grid,
            text="ADXL X/Y/Z Raw",
        )
        raw_frame.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=4,
            pady=4,
        )
        raw_canvas = tk.Canvas(
            raw_frame,
            highlightthickness=0,
        )
        raw_canvas.pack(fill="both", expand=True)
        self.graph_canvases["raw"] = raw_canvas
        self._bind_graph_navigation(
            raw_canvas,
            "adxl_raw",
            self._force_graph_refresh,
        )

        fft_frame = ttk.LabelFrame(
            grid,
            text="ADXL X/Y/Z FFT",
        )
        fft_frame.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=4,
            pady=4,
        )
        fft_canvas = tk.Canvas(
            fft_frame,
            highlightthickness=0,
        )
        fft_canvas.pack(fill="both", expand=True)
        self.graph_canvases["fft"] = fft_canvas
        self._bind_graph_navigation(
            fft_canvas,
            "adxl_fft",
            self._force_graph_refresh,
        )

        self.graph_last_version = -1

        def on_close():
            try:
                win.destroy()
            except Exception:
                pass

            self.graph_window = None
            self.graph_canvases = {}
            self.graph_device_var = None
            self.graph_info_var = None
            self.graph_axis_vars = {}
            for key in ("adxl_raw", "adxl_fft"):
                self.graph_view_states.pop(key, None)

        win.protocol("WM_DELETE_WINDOW", on_close)
        win.after(
            GRAPH_REFRESH_MS,
            self._refresh_adxl_graph,
        )

    def _refresh_graph_device_list(self):
        devices = self.adxl_store.device_list()

        if not hasattr(self, "graph_device_combo"):
            return

        self.graph_device_combo["values"] = devices

        if (
            self.graph_device_var is not None
            and not self.graph_device_var.get()
            and devices
        ):
            self.graph_device_var.set(devices[0])

        self._force_graph_refresh()

    def _force_graph_refresh(self):
        self.graph_last_version = -1

    @staticmethod
    def _decimate_xy(x, y, max_points=MAX_GRAPH_POINTS):
        x = np.asarray(x)
        y = np.asarray(y)

        n = len(y)
        if n <= max_points:
            return x, y

        step = int(np.ceil(n / max_points))
        return x[::step], y[::step]

    @staticmethod
    def _fmt_tick(value):
        av = abs(float(value))
        if av >= 1000:
            return f"{value:.0f}"
        if av >= 1:
            return f"{value:.1f}"
        return f"{value:.3f}"

    @staticmethod
    def _nice_x_tick_step(span, plot_width, raw_sample_axis=False):
        """
        현재 화면 폭과 X축 범위에 따라 숫자 라벨 간격을 결정한다.

        Raw Time / FFT:
          - 현재 화면 폭과 X축 범위에 맞춰 보기 좋은 간격을 자동 선택한다.
          - Time 축은 초(sec) 단위로 1/2/2.5/5 x 10^n 간격을 사용한다.

        FFT:
          - 1/2/2.5/5 x 10^n 계열의 보기 좋은 간격 사용.
        """
        span = max(float(span), 1e-12)
        plot_width = max(float(plot_width), 1.0)

        target_ticks = max(4.0, min(24.0, plot_width / 55.0))
        approx = span / target_ticks

        if raw_sample_axis:
            raw_steps = (
                1, 2, 5, 10, 20, 25, 50, 100, 200, 250,
                500, 1000, 2000, 2500, 5000, 10000,
            )
            return float(
                min(raw_steps, key=lambda v: abs(v - approx))
            )

        if approx <= 0:
            return 1.0

        exponent = math.floor(math.log10(approx))
        base = 10.0 ** exponent
        fraction = approx / base
        nice_fractions = (1.0, 2.0, 2.5, 5.0, 10.0)
        nice_fraction = min(
            nice_fractions,
            key=lambda v: abs(v - fraction),
        )
        return nice_fraction * base

    def _draw_detailed_x_ticks(
        self,
        canvas,
        x_min,
        x_max,
        left,
        plot_w,
        axis_y,
        fg,
        x_title,
    ):
        """
        X축 상세 눈금.

        Raw Time:
          - 초(sec) 단위의 자동 눈금.
          - 확대/축소에 따라 major/minor 간격이 자동으로 변한다.

        FFT:
          - 현재 주파수 범위에 맞춰 자동으로 상세 눈금 생성.
        """
        span = max(float(x_max) - float(x_min), 1e-12)
        raw_axis = "sample" in str(x_title).lower()

        major_step = self._nice_x_tick_step(
            span,
            plot_w,
            raw_sample_axis=raw_axis,
        )

        if raw_axis:
            # 원래 요청 기준: 50 sample 보조 눈금.
            # 50보다 더 확대된 경우에는 major보다 작은 적절한 간격 사용.
            if major_step >= 50:
                minor_step = 50.0
            elif major_step >= 10:
                minor_step = 10.0
            elif major_step >= 5:
                minor_step = 5.0
            else:
                minor_step = 1.0
        else:
            minor_step = major_step / 5.0

        def iter_ticks(step):
            if step <= 0:
                return

            first = math.ceil(float(x_min) / step) * step
            value = first
            # 비정상적으로 많은 tick 생성을 방지
            count = 0
            while value <= float(x_max) + step * 1e-9 and count < 1000:
                yield value
                value += step
                count += 1

        # 보조 눈금
        if minor_step > 0:
            for value in iter_ticks(minor_step):
                # major와 겹치는 위치는 major에서 다시 그림
                ratio = value / major_step
                if abs(ratio - round(ratio)) < 1e-7:
                    continue

                xx = left + (
                    (float(value) - float(x_min)) / span
                ) * plot_w

                canvas.create_line(
                    xx,
                    axis_y,
                    xx,
                    axis_y + 3,
                    fill=fg,
                )

        # major 눈금 + 숫자
        for value in iter_ticks(major_step):
            xx = left + (
                (float(value) - float(x_min)) / span
            ) * plot_w

            canvas.create_line(
                xx,
                axis_y,
                xx,
                axis_y + 6,
                fill=fg,
            )
            canvas.create_text(
                xx,
                axis_y + 16,
                text=self._fmt_tick(value),
                anchor="n",
                fill=fg,
            )

    def _graph_view_state(self, key):
        state = self.graph_view_states.get(key)

        if state is None:
            state = {
                "manual": False,
                "limits": None,
                "current_limits": None,
                "geometry": None,
                "drag": None,
                "refresh": None,
            }
            self.graph_view_states[key] = state

        return state

    def _bind_graph_navigation(self, canvas, key, refresh_callback):
        """
        그래프 조작:
        - 마우스 휠: 커서 위치 기준 확대/축소
        - 왼쪽 드래그: 현재 확대 범위 이동
        - 더블클릭: 전체 범위 초기화
        """
        state = self._graph_view_state(key)
        state["refresh"] = refresh_callback

        canvas.bind(
            "<MouseWheel>",
            lambda e, k=key: self._on_graph_mousewheel(e, k),
        )

        # Linux/X11 호환
        canvas.bind(
            "<Button-4>",
            lambda e, k=key: self._on_graph_linux_wheel(e, k, 1),
        )
        canvas.bind(
            "<Button-5>",
            lambda e, k=key: self._on_graph_linux_wheel(e, k, -1),
        )

        canvas.bind(
            "<ButtonPress-1>",
            lambda e, k=key: self._on_graph_drag_start(e, k),
        )
        canvas.bind(
            "<B1-Motion>",
            lambda e, k=key: self._on_graph_drag_move(e, k),
        )
        canvas.bind(
            "<ButtonRelease-1>",
            lambda e, k=key: self._on_graph_drag_end(e, k),
        )
        canvas.bind(
            "<Double-Button-1>",
            lambda e, k=key: self._reset_graph_view(k),
        )

        canvas.configure(cursor="crosshair")

    def _request_graph_redraw(self, key):
        state = self._graph_view_state(key)
        callback = state.get("refresh")

        if callback is not None:
            callback()

    def _on_graph_linux_wheel(self, event, key, direction):
        event.delta = 120 if direction > 0 else -120
        self._on_graph_mousewheel(event, key)

    def _on_graph_mousewheel(self, event, key):
        state = self._graph_view_state(key)
        limits = state.get("current_limits")
        geometry = state.get("geometry")

        if limits is None or geometry is None:
            return "break"

        x_min, x_max, y_lo, y_hi = limits
        left, right, top, bottom, width, height = geometry

        plot_w = max(width - left - right, 1)
        plot_h = max(height - top - bottom, 1)

        # plot 영역 밖에서도 가장 가까운 경계점 기준으로 확대
        px = min(max(event.x, left), width - right)
        py = min(max(event.y, top), height - bottom)

        x_ratio = (px - left) / plot_w
        y_ratio = (py - top) / plot_h

        x_center = x_min + x_ratio * (x_max - x_min)
        y_center = y_hi - y_ratio * (y_hi - y_lo)

        # wheel up = 확대 / down = 축소
        zoom_factor = 0.80 if event.delta > 0 else 1.25

        new_x_span = max((x_max - x_min) * zoom_factor, 1e-12)
        new_y_span = max((y_hi - y_lo) * zoom_factor, 1e-12)

        new_x_min = x_center - x_ratio * new_x_span
        new_x_max = new_x_min + new_x_span

        # y_ratio는 화면 위->아래이므로 y축 계산 반전
        new_y_hi = y_center + y_ratio * new_y_span
        new_y_lo = new_y_hi - new_y_span

        state["manual"] = True
        state["limits"] = (
            new_x_min,
            new_x_max,
            new_y_lo,
            new_y_hi,
        )

        self._request_graph_redraw(key)
        return "break"

    def _on_graph_drag_start(self, event, key):
        state = self._graph_view_state(key)
        limits = state.get("current_limits")

        if limits is None:
            return

        state["drag"] = {
            "x": event.x,
            "y": event.y,
            "limits": tuple(limits),
        }

    def _on_graph_drag_move(self, event, key):
        state = self._graph_view_state(key)
        drag = state.get("drag")
        geometry = state.get("geometry")

        if drag is None or geometry is None:
            return

        left, right, top, bottom, width, height = geometry
        plot_w = max(width - left - right, 1)
        plot_h = max(height - top - bottom, 1)

        x_min, x_max, y_lo, y_hi = drag["limits"]
        x_span = x_max - x_min
        y_span = y_hi - y_lo

        dx = event.x - drag["x"]
        dy = event.y - drag["y"]

        shift_x = -(dx / plot_w) * x_span
        shift_y = (dy / plot_h) * y_span

        state["manual"] = True
        state["limits"] = (
            x_min + shift_x,
            x_max + shift_x,
            y_lo + shift_y,
            y_hi + shift_y,
        )

        self._request_graph_redraw(key)

    def _on_graph_drag_end(self, event, key):
        state = self._graph_view_state(key)
        state["drag"] = None

    def _reset_graph_view(self, key):
        state = self._graph_view_state(key)
        state["manual"] = False
        state["limits"] = None
        state["drag"] = None
        self._request_graph_redraw(key)
        return "break"

    def _reset_graph_views(self, *keys):
        for key in keys:
            state = self._graph_view_state(key)
            state["manual"] = False
            state["limits"] = None
            state["drag"] = None

        if keys:
            self._request_graph_redraw(keys[0])

    def _draw_canvas_graph(
        self,
        canvas,
        x_values,
        y_values,
        x_title,
        y_title,
        view_key=None,
        line_color=None,
        fixed_y_range=None,
    ):
        if canvas is None:
            return

        canvas.delete("all")

        if len(y_values) < 2:
            return

        x_values = np.asarray(x_values)
        y_values = np.asarray(y_values)

        auto_x_min = float(np.min(x_values))
        auto_x_max = float(np.max(x_values))
        auto_y_min = float(np.min(y_values))
        auto_y_max = float(np.max(y_values))

        if auto_y_max == auto_y_min:
            pad = max(abs(auto_y_max) * 0.05, 1.0)
        else:
            pad = (auto_y_max - auto_y_min) * 0.05

        auto_y_lo = auto_y_min - pad
        auto_y_hi = auto_y_max + pad

        state = (
            self._graph_view_state(view_key)
            if view_key
            else None
        )

        if (
            state is not None
            and state.get("manual")
            and state.get("limits") is not None
        ):
            x_min, x_max, manual_y_lo, manual_y_hi = state["limits"]
            if fixed_y_range is None:
                y_lo, y_hi = manual_y_lo, manual_y_hi
            else:
                y_lo, y_hi = fixed_y_range
        else:
            x_min = auto_x_min
            x_max = auto_x_max
            if fixed_y_range is None:
                y_lo = auto_y_lo
                y_hi = auto_y_hi
            else:
                y_lo, y_hi = fixed_y_range

        x_span = max(x_max - x_min, 1e-12)
        y_span = max(y_hi - y_lo, 1e-12)

        # 현재 X 범위 주변 데이터만 그려 성능과 clipping을 개선
        mask = (x_values >= x_min) & (x_values <= x_max)

        if np.any(mask):
            visible_indices = np.flatnonzero(mask)
            i0 = max(int(visible_indices[0]) - 1, 0)
            i1 = min(int(visible_indices[-1]) + 2, len(x_values))
            draw_x = x_values[i0:i1]
            draw_y = y_values[i0:i1]
        else:
            draw_x = np.asarray([])
            draw_y = np.asarray([])

        if len(draw_y) >= 2:
            draw_x, draw_y = self._decimate_xy(
                draw_x,
                draw_y,
                MAX_GRAPH_POINTS,
            )

        width = max(canvas.winfo_width(), 250)
        height = max(canvas.winfo_height(), 180)

        # 축 제목과 눈금 숫자가 겹치지 않도록 여백 확보
        left = 78
        right = 18
        top = 18
        bottom = 66

        plot_w = max(width - left - right, 1)
        plot_h = max(height - top - bottom, 1)

        if state is not None:
            state["current_limits"] = (
                x_min,
                x_max,
                y_lo,
                y_hi,
            )
            state["geometry"] = (
                left,
                right,
                top,
                bottom,
                width,
                height,
            )

        style = ttk.Style()
        fg = (
            style.lookup("TLabel", "foreground")
            or style.lookup(".", "foreground")
            or "black"
        )

        canvas.create_line(
            left,
            top,
            left,
            height - bottom,
            width - right,
            height - bottom,
            fill=fg,
        )

        for i in range(5):
            frac = i / 4
            yy = top + frac * plot_h
            value = y_hi - frac * y_span

            canvas.create_line(
                left - 3,
                yy,
                left,
                yy,
                fill=fg,
            )
            canvas.create_text(
                left - 6,
                yy,
                text=self._fmt_tick(value),
                anchor="e",
                fill=fg,
            )

        self._draw_detailed_x_ticks(
            canvas,
            x_min,
            x_max,
            left,
            plot_w,
            height - bottom,
            fg,
            x_title,
        )

        points = []

        for xv, yv in zip(draw_x, draw_y):
            px = left + ((float(xv) - x_min) / x_span) * plot_w
            py = top + ((y_hi - float(yv)) / y_span) * plot_h

            # plot 영역 바깥으로 선이 라벨까지 침범하지 않도록 clamp
            px = min(max(px, left), width - right)
            py = min(max(py, top), height - bottom)

            points.extend((px, py))

        if len(points) >= 4:
            canvas.create_line(
                *points,
                fill=(line_color or fg),
                width=1.2,
            )

        # X축 제목: 눈금 숫자 아래 별도 공간
        canvas.create_text(
            left + plot_w / 2,
            height - 8,
            text=x_title,
            anchor="s",
            fill=fg,
        )

        # Y축 제목: 왼쪽 여백 중앙에 세로 표시
        canvas.create_text(
            16,
            top + plot_h / 2,
            text=y_title,
            anchor="center",
            angle=90,
            fill=fg,
        )

        if state is not None and state.get("manual"):
            canvas.create_text(
                width - right - 5,
                top + 5,
                text="ZOOM",
                anchor="ne",
                fill=fg,
            )

    def _draw_canvas_multiseries(
        self,
        canvas,
        series_dict,
        x_title,
        y_title,
        view_key=None,
        fixed_y_range=None,
    ):
        """
        X/Y/Z 공통 스케일 통합 그래프.
        view_key가 있으면 확대/축소/이동 범위를 유지한다.
        """
        if canvas is None:
            return

        canvas.delete("all")

        prepared_full = {}

        for axis, value in series_dict.items():
            if value is None:
                continue

            x_values, y_values, color = value

            if len(y_values) < 2:
                continue

            prepared_full[axis] = (
                np.asarray(x_values),
                np.asarray(y_values),
                color,
            )

        if not prepared_full:
            width = max(canvas.winfo_width(), 250)
            height = max(canvas.winfo_height(), 180)

            style = ttk.Style()
            fg = (
                style.lookup("TLabel", "foreground")
                or style.lookup(".", "foreground")
                or "black"
            )

            canvas.create_text(
                width / 2,
                height / 2,
                text="표시할 축이 없습니다.",
                fill=fg,
                anchor="center",
            )
            return

        auto_x_min = min(
            float(np.min(x))
            for x, _y, _c in prepared_full.values()
        )
        auto_x_max = max(
            float(np.max(x))
            for x, _y, _c in prepared_full.values()
        )
        auto_y_min = min(
            float(np.min(y))
            for _x, y, _c in prepared_full.values()
        )
        auto_y_max = max(
            float(np.max(y))
            for _x, y, _c in prepared_full.values()
        )

        if auto_y_max == auto_y_min:
            pad = max(abs(auto_y_max) * 0.05, 1.0)
        else:
            pad = (auto_y_max - auto_y_min) * 0.05

        auto_y_lo = auto_y_min - pad
        auto_y_hi = auto_y_max + pad

        state = (
            self._graph_view_state(view_key)
            if view_key
            else None
        )

        if (
            state is not None
            and state.get("manual")
            and state.get("limits") is not None
        ):
            x_min, x_max, manual_y_lo, manual_y_hi = state["limits"]
            if fixed_y_range is None:
                y_lo, y_hi = manual_y_lo, manual_y_hi
            else:
                y_lo, y_hi = fixed_y_range
        else:
            x_min = auto_x_min
            x_max = auto_x_max
            if fixed_y_range is None:
                y_lo = auto_y_lo
                y_hi = auto_y_hi
            else:
                y_lo, y_hi = fixed_y_range

        x_span = max(x_max - x_min, 1e-12)
        y_span = max(y_hi - y_lo, 1e-12)

        prepared = {}

        for axis, (x_values, y_values, color) in prepared_full.items():
            mask = (x_values >= x_min) & (x_values <= x_max)

            if not np.any(mask):
                continue

            idxs = np.flatnonzero(mask)
            i0 = max(int(idxs[0]) - 1, 0)
            i1 = min(int(idxs[-1]) + 2, len(x_values))

            draw_x = x_values[i0:i1]
            draw_y = y_values[i0:i1]

            if len(draw_y) >= 2:
                draw_x, draw_y = self._decimate_xy(
                    draw_x,
                    draw_y,
                    MAX_GRAPH_POINTS,
                )

                prepared[axis] = (
                    np.asarray(draw_x),
                    np.asarray(draw_y),
                    color,
                )

        width = max(canvas.winfo_width(), 250)
        height = max(canvas.winfo_height(), 180)

        # 축 제목과 눈금 숫자가 겹치지 않도록 여백 확보
        left = 82
        right = 20
        top = 18
        bottom = 68

        plot_w = max(width - left - right, 1)
        plot_h = max(height - top - bottom, 1)

        if state is not None:
            state["current_limits"] = (
                x_min,
                x_max,
                y_lo,
                y_hi,
            )
            state["geometry"] = (
                left,
                right,
                top,
                bottom,
                width,
                height,
            )

        style = ttk.Style()
        fg = (
            style.lookup("TLabel", "foreground")
            or style.lookup(".", "foreground")
            or "black"
        )

        canvas.create_line(
            left,
            top,
            left,
            height - bottom,
            width - right,
            height - bottom,
            fill=fg,
        )

        for i in range(5):
            frac = i / 4
            yy = top + frac * plot_h
            value = y_hi - frac * y_span

            canvas.create_line(
                left - 3,
                yy,
                left,
                yy,
                fill=fg,
            )
            canvas.create_text(
                left - 6,
                yy,
                text=self._fmt_tick(value),
                anchor="e",
                fill=fg,
            )

        self._draw_detailed_x_ticks(
            canvas,
            x_min,
            x_max,
            left,
            plot_w,
            height - bottom,
            fg,
            x_title,
        )

        for axis in ("X", "Y", "Z"):
            item = prepared.get(axis)

            if item is None:
                continue

            x_values, y_values, color = item
            points = []

            for xv, yv in zip(x_values, y_values):
                px = (
                    left
                    + ((float(xv) - x_min) / x_span)
                    * plot_w
                )
                py = (
                    top
                    + ((y_hi - float(yv)) / y_span)
                    * plot_h
                )

                px = min(max(px, left), width - right)
                py = min(max(py, top), height - bottom)
                points.extend((px, py))

            if len(points) >= 4:
                canvas.create_line(
                    *points,
                    fill=color,
                    width=1.4,
                )

        # X축 제목: 눈금 숫자 아래 별도 공간
        canvas.create_text(
            left + plot_w / 2,
            height - 8,
            text=x_title,
            anchor="s",
            fill=fg,
        )

        # Y축 제목: 왼쪽 여백 중앙에 세로 표시
        canvas.create_text(
            16,
            top + plot_h / 2,
            text=y_title,
            anchor="center",
            angle=90,
            fill=fg,
        )

        if state is not None and state.get("manual"):
            canvas.create_text(
                width - right - 5,
                top + 5,
                text="ZOOM",
                anchor="ne",
                fill=fg,
            )

    def _refresh_adxl_graph(self):
        win = self.graph_window
        if win is None:
            return

        try:
            if not win.winfo_exists():
                return
        except Exception:
            return

        devices = self.adxl_store.device_list()

        if hasattr(self, "graph_device_combo"):
            current_values = list(
                self.graph_device_combo["values"]
            )

            if current_values != devices:
                self.graph_device_combo["values"] = devices

                if (
                    self.graph_device_var is not None
                    and not self.graph_device_var.get()
                    and devices
                ):
                    self.graph_device_var.set(devices[0])

        device_id = (
            self.graph_device_var.get()
            if self.graph_device_var is not None
            else ""
        )

        if not device_id:
            if self.graph_info_var is not None:
                self.graph_info_var.set(
                    "ADXL 센서 검색 중... "
                    "데이터가 들어오면 목록에 자동 추가됩니다."
                )

            win.after(
                GRAPH_REFRESH_MS,
                self._refresh_adxl_graph,
            )
            return

        version, axes = self.adxl_store.snapshot(device_id)

        # 축 체크박스를 바꾸면 _force_graph_refresh가 -1로 바꾸므로
        # 수신 버전이 같아도 즉시 다시 그린다.
        if version != self.graph_last_version:
            self.graph_last_version = version

            axis_colors = {
                "X": "#1f77b4",
                "Y": "#2ca02c",
                "Z": "#d62728",
            }

            enabled_axes = []

            for axis in ("X", "Y", "Z"):
                var = (
                    self.graph_axis_vars.get(axis)
                    if hasattr(self, "graph_axis_vars")
                    else None
                )

                if var is not None and var.get():
                    enabled_axes.append(axis)

            raw_series = {}
            fft_series = {}
            received_axes = []

            for axis in ("X", "Y", "Z"):
                record = axes.get(axis)

                if record is None:
                    continue

                received_axes.append(axis)

                if axis not in enabled_axes:
                    continue

                samples = record["samples"]
                sample_rate = record["sample_rate"]
                freqs = record["freqs"]
                magnitude = record["magnitude"]

                # 각 축의 Raw X축은 해당 축 패킷의 Fs를 기준으로 한
                # 경과 시간(sec)이다.
                if float(sample_rate) > 0:
                    time_axis = (
                        np.arange(
                            len(samples),
                            dtype=np.float64,
                        )
                        / float(sample_rate)
                    )
                else:
                    time_axis = np.arange(
                        len(samples),
                        dtype=np.float64,
                    )

                raw_series[axis] = (
                    time_axis,
                    samples,
                    axis_colors[axis],
                )
                fft_series[axis] = (
                    freqs,
                    magnitude,
                    axis_colors[axis],
                )

            try:
                adxl_raw_y_range = self._parse_y_limit_pair(
                    self.adxl_raw_y_min_var,
                    self.adxl_raw_y_max_var,
                    "ADXL Raw Y축",
                )
            except ValueError:
                adxl_raw_y_range = None

            try:
                adxl_fft_y_range = self._parse_y_limit_pair(
                    self.adxl_fft_y_min_var,
                    self.adxl_fft_y_max_var,
                    "ADXL FFT Y축",
                )
            except ValueError:
                adxl_fft_y_range = None

            self._draw_canvas_multiseries(
                self.graph_canvases.get("raw"),
                raw_series,
                "Time (s)",
                "int16",
                view_key="adxl_raw",
                fixed_y_range=adxl_raw_y_range,
            )

            self._draw_canvas_multiseries(
                self.graph_canvases.get("fft"),
                fft_series,
                "Frequency (Hz)",
                "Magnitude",
                view_key="adxl_fft",
                fixed_y_range=adxl_fft_y_range,
            )

            if self.graph_info_var is not None:
                sample_info = ""
                diagnostics = []

                for axis in ("X", "Y", "Z"):
                    if axis not in axes:
                        continue

                    r = axes[axis]
                    samples = r["samples"]

                    if not sample_info:
                        n_cfg = int(r.get("numofsample", len(samples)))
                        fs_cfg = float(r["sample_rate"])
                        sample_info = (
                            f"N={n_cfg} / Fs={fs_cfg:.0f} Hz / "
                            f"Time X=0~{((len(samples) - 1) / fs_cfg if len(samples) and fs_cfg > 0 else 0.0):.6g} s / "
                            f"FFT X=0~{fs_cfg / 2.0:g} Hz / "
                            f"Δf={fs_cfg / max(n_cfg, 1):.6g} Hz"
                        )

                    if len(samples):
                        head = ",".join(
                            str(int(v))
                            for v in samples[:4]
                        )
                        min_v = int(np.min(samples))
                        max_v = int(np.max(samples))
                    else:
                        head = "-"
                        min_v = 0
                        max_v = 0

                    packet_fs = (
                        r.get("packet_sample_rate", "")
                        or "(없음)"
                    )

                    diagnostics.append(
                        f"{axis}:seq={r.get('seq','')} "
                        f"PacketFs={packet_fs} "
                        f"FFTFs={float(r.get('sample_rate', 0.0)):.6g} "
                        f"N={r.get('fft_n', len(samples))} "
                        f"PeakBin={r.get('peak_bin', -1)} "
                        f"PeakHz={float(r.get('peak_hz', 0.0)):.6g} "
                        f"Mag={float(r.get('peak_magnitude', 0.0)):.6g} "
                        f"crc={r.get('crc32','')} "
                        f"min/max={min_v}/{max_v}"
                    )

                # 실제 decoded 배열이 완전히 같은지 검사
                identical_pairs = []

                for a, b in (("X", "Y"), ("X", "Z"), ("Y", "Z")):
                    if a in axes and b in axes:
                        sa = axes[a]["samples"]
                        sb = axes[b]["samples"]

                        if (
                            len(sa) == len(sb)
                            and np.array_equal(sa, sb)
                        ):
                            identical_pairs.append(f"{a}={b}")

                warning = (
                    " | ⚠ 동일 데이터: "
                    + ",".join(identical_pairs)
                    if identical_pairs
                    else ""
                )

                self.graph_info_var.set(
                    f"{device_id} | "
                    f"수신 축={','.join(received_axes) or '-'} | "
                    f"표시 축={','.join(enabled_axes) or '-'} | "
                    f"{sample_info} | "
                    + " / ".join(diagnostics)
                    + warning
                )

        win.after(
            GRAPH_REFRESH_MS,
            self._refresh_adxl_graph,
        )

    def start_measure(self):
        if not self.connected:
            messagebox.showwarning("측정", "MQTT 또는 Serial을 먼저 연결하세요.")
            return

        self.stats.reset()
        self._start_csv_session()
        self.stats.start()
        self.measuring = True
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._log(f"측정 시작 [{self.active_transport}]: PIEZO + ADXL X/Y/Z")

    def stop_measure(self):
        self.measuring = False
        self.stats.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        s = self.stats.snapshot()
        self._log(
            f"측정 중지 | {s['messages']:,} msg | "
            f"{self.fmt_bytes(s['bytes'])} | "
            f"평균 {s['avg_msg_rate']:.2f} msg/s | "
            f"{self.fmt_rate(s['avg_byte_rate'])}"
        )

    def reset_stats(self):
        self.stats.reset()
        self._log("통계 초기화 (자동 감지 센서 목록은 유지)")


    # =========================================================
    # PyQtGraph renderer (v0.39)
    # =========================================================
    @staticmethod
    def _pg_put_latest(target_queue, message):
        try:
            target_queue.put_nowait(message)
            return
        except queue.Full:
            pass
        except Exception:
            return
        try:
            target_queue.get_nowait()
        except Exception:
            pass
        try:
            target_queue.put_nowait(message)
        except Exception:
            pass

    def _pg_start_viewer(self, mode):
        mode = str(mode).upper()
        prefix = "adxl" if mode == "ADXL" else ("piezo_scipy" if mode == "PIEZO_SCIPY" else "piezo")
        process = getattr(self, f"pg_{prefix}_process", None)
        if process is not None and process.is_alive():
            q = getattr(self, f"pg_{prefix}_data_queue", None)
            if q is not None:
                self._pg_put_latest(q, {"type": "raise"})
            return False

        ctx = mp.get_context("spawn")
        data_queue = ctx.Queue(maxsize=6)
        command_queue = ctx.Queue(maxsize=20)
        process = ctx.Process(
            target=_pyqtgraph_viewer_process,
            args=("PIEZO" if mode == "PIEZO_SCIPY" else mode, data_queue, command_queue,
                  "scipy" if mode == "PIEZO_SCIPY" else "original"),
            daemon=True,
            name=f"pyqtgraph-{prefix}-viewer",
        )
        process.start()

        setattr(self, f"pg_{prefix}_process", process)
        setattr(self, f"pg_{prefix}_data_queue", data_queue)
        setattr(self, f"pg_{prefix}_command_queue", command_queue)
        setattr(self, f"pg_{prefix}_selected", "")
        setattr(self, f"pg_{prefix}_last_version", -1)
        setattr(self, f"pg_{prefix}_last_devices", [])
        setattr(self, f"pg_{prefix}_last_device_send_at", 0.0)
        setattr(self, f"pg_{prefix}_last_snapshot_send_at", 0.0)
        setattr(self, f"pg_{prefix}_error_shown", False)

        # 창을 나중에 열더라도 현재 적용 중인 Fs/N을 즉시 전달한다.
        try:
            self._pg_put_latest(
                data_queue,
                {
                    "type": "sensor_config",
                    "mode": mode,
                    "sample_rate": float(
                        self.fft_sample_rate_by_mode.get(
                            "PIEZO" if mode == "PIEZO_SCIPY" else mode,
                            self.graph_sample_defaults.get(
                                "PIEZO" if mode == "PIEZO_SCIPY" else mode,
                                (0.0, SAMPLE_COUNTS[-1]),
                            )[0],
                        )
                    ),
                    "sample_count": int(
                        self.fft_sample_count_by_mode.get(
                            "PIEZO" if mode == "PIEZO_SCIPY" else mode,
                            self.graph_sample_defaults.get(
                                "PIEZO" if mode == "PIEZO_SCIPY" else mode,
                                (0.0, SAMPLE_COUNTS[-1]),
                            )[1],
                        )
                    ),
                    "reason": "VIEWER_OPEN",
                },
            )
        except Exception:
            pass

        return True

    def _pg_poll_commands(self, mode):
        mode = str(mode).upper()
        prefix = "adxl" if mode == "ADXL" else ("piezo_scipy" if mode == "PIEZO_SCIPY" else "piezo")
        q = getattr(self, f"pg_{prefix}_command_queue", None)
        if q is None:
            return
        try:
            while True:
                msg = q.get_nowait()
                typ = msg.get("type")
                if typ == "select":
                    setattr(
                        self,
                        f"pg_{prefix}_selected",
                        str(msg.get("device_id", "")).strip(),
                    )
                    setattr(
                        self,
                        f"pg_{prefix}_last_version",
                        -1,
                    )
                elif typ == "fft_window":
                    selected_window = normalize_fft_window_name(
                        msg.get("window", "Hann")
                    )

                    try:
                        selected_beta = float(
                            msg.get("beta", 8.6)
                        )
                    except Exception:
                        selected_beta = 8.6

                    if not np.isfinite(selected_beta):
                        selected_beta = 8.6

                    self.fft_window_by_mode[mode] = selected_window
                    self.fft_window_beta_by_mode[mode] = selected_beta

                    setattr(
                        self,
                        f"pg_{prefix}_last_version",
                        -1,
                    )

                    beta_text = (
                        f" β={selected_beta:.2f}"
                        if selected_window == "Kaiser"
                        else ""
                    )

                    self.ui_queue.put(
                        (
                            "log",
                            (
                                f"[FFT WINDOW] "
                                f"{mode} -> "
                                f"{selected_window}{beta_text}"
                            ),
                        )
                    )
                elif typ == "piezo_zero_offset":
                    self.ui_queue.put(
                        (
                            "log",
                            (
                                f"[PIEZO ZERO] "
                                f"{msg.get('device_id','')} | "
                                f"Raw Zero="
                                f"{float(msg.get('offset',0.0)):.6g} count "
                                f"(display only, FFT unchanged)"
                            ),
                        )
                    )

                elif typ == "piezo_zero_reset":
                    self.ui_queue.put(
                        (
                            "log",
                            (
                                f"[PIEZO ZERO] "
                                f"{msg.get('device_id','')} | "
                                f"기본 0점 복원: {PIEZO_DEFAULT_ZERO_COUNT:g} count"
                            ),
                        )
                    )

                elif typ == "fft_window_size":
                    try:
                        requested_size = int(
                            msg.get("window_size", 0)
                        )
                    except Exception:
                        requested_size = 0

                    sample_count = int(
                        self.fft_sample_count_by_mode.get(
                            "PIEZO" if mode == "PIEZO_SCIPY" else mode,
                            SAMPLE_COUNTS[-1],
                        )
                    )

                    allowed = allowed_fft_window_sizes(
                        sample_count
                    )

                    if requested_size in allowed:
                        applied_size = requested_size
                    else:
                        applied_size = (
                            sample_count
                            if sample_count in allowed
                            else max(allowed)
                        )

                    self.fft_window_size_by_mode[
                        mode
                    ] = int(applied_size)

                    setattr(
                        self,
                        f"pg_{prefix}_last_version",
                        -1,
                    )

                    self.ui_queue.put(
                        (
                            "log",
                            (
                                f"[FFT WINDOW SIZE] "
                                f"{mode} | "
                                f"Sample Count={sample_count} | "
                                f"Window N={applied_size} | "
                                f"FFT N={applied_size}"
                            ),
                        )
                    )

                elif typ == "closed":
                    p = getattr(self, f"pg_{prefix}_process", None)
                    if p is not None:
                        try: p.join(timeout=0.2)
                        except Exception: pass
                    setattr(self, f"pg_{prefix}_process", None)
                    return
                elif typ == "calibration":
                    self.ui_queue.put(
                        (
                            "log",
                            (
                                f"[G RMS CAL] "
                                f"{msg.get('sensor_mode','')}/"
                                f"{msg.get('device_id','')}/"
                                f"{msg.get('axis','')} | "
                                f"Reference={float(msg.get('ref_g_rms',1.0)):.6g} g RMS | "
                                f"Raw RMS={float(msg.get('raw_rms_count',0.0)):.6g} count | "
                                f"Scale={float(msg.get('counts_per_g',0.0)):.6g} count/g | "
                                f"OffsetRef={float(msg.get('raw_mean_ref',0.0)):.6g} | "
                                f"Complete={bool(msg.get('calibration_complete',False))}"
                            ),
                        )
                    )
                elif typ == "calibration_reset":
                    self.ui_queue.put(
                        (
                            "log",
                            (
                                f"[G RMS CAL] "
                                f"{msg.get('sensor_mode','')}/"
                                f"{msg.get('device_id','')}/"
                                f"{msg.get('axis','')} 보정 해제"
                            ),
                        )
                    )
                elif typ == "calibration_reset_all":
                    self.ui_queue.put(
                        (
                            "log",
                            (
                                f"[G RMS CAL] "
                                f"{msg.get('sensor_mode','')}/"
                                f"{msg.get('device_id','')} "
                                f"전체 축 보정 해제"
                            ),
                        )
                    )
                elif typ == "error":
                    if not getattr(self, f"pg_{prefix}_error_shown", False):
                        setattr(self, f"pg_{prefix}_error_shown", True)
                        messagebox.showerror("PyQtGraph", str(msg.get("message", "PyQtGraph 실행 오류")))
        except queue.Empty:
            pass
        except Exception:
            pass

    def _sync_fft_graph_config(
        self,
        mode,
        sample_rate,
        sample_count,
        reason="CONFIG",
    ):
        """
        실제 센서 설정 적용 시 그래프 FFT 기준을 동기화한다.

        기본:
          Sample Count = Window Size = FFT N

        이후 그래프 창에서 Window Size를 Sample Count 이하로
        줄여 사용할 수 있다.
        """
        mode = str(mode).upper()

        if mode not in ("PIEZO", "ADXL"):
            return

        try:
            sample_rate = float(sample_rate)
            sample_count = int(sample_count)
        except Exception:
            return

        if sample_rate <= 0 or sample_count <= 0:
            return

        self.fft_sample_rate_by_mode[mode] = sample_rate
        self.fft_sample_count_by_mode[mode] = sample_count
        self.fft_window_size_by_mode[mode] = sample_count

        # 수신 데이터에 Fs/N이 빠진 경우 사용하는 fallback도
        # 실제 적용값과 맞춘다.
        self.graph_sample_defaults[mode] = (
            sample_rate,
            sample_count,
        )

        prefix = "adxl" if mode == "ADXL" else ("piezo_scipy" if mode == "PIEZO_SCIPY" else "piezo")
        q = getattr(
            self,
            f"pg_{prefix}_data_queue",
            None,
        )
        process = getattr(
            self,
            f"pg_{prefix}_process",
            None,
        )

        if (
            q is not None
            and process is not None
            and process.is_alive()
        ):
            self._pg_put_latest(
                q,
                {
                    "type": "sensor_config",
                    "mode": mode,
                    "sample_rate": sample_rate,
                    "sample_count": sample_count,
                    "reason": str(reason),
                },
            )

        setattr(
            self,
            f"pg_{prefix}_last_version",
            -1,
        )

        self._log(
            f"[FFT CONFIG SYNC] {mode} | "
            f"Fs={sample_rate:g} Hz | "
            f"Sample Count={sample_count} | "
            f"Window N={sample_count} | "
            f"FFT N={sample_count} | "
            f"reason={reason}"
        )

    def _sync_fft_graph_config_from_payload(
        self,
        payload,
        reason="CONFIG",
    ):
        sensor_type_code = str(
            payload.get("SENSORTYPE", "")
        ).strip()

        try:
            if sensor_type_code in ("1", "3"):
                self._sync_fft_graph_config(
                    "PIEZO",
                    float(payload["PIEZO_SAMPLE_RATE"]),
                    int(payload["PIEZO_SAMPLE_COUNT"]),
                    reason=reason,
                )

            elif sensor_type_code in ("2", "4"):
                rate_value = payload.get(
                    "MEMS_SAMPLE_RATE",
                    payload.get("ADXL_SAMPLE_RATE", ""),
                )

                rate_text = self._adxl_rate_value_to_hz(
                    rate_value
                )

                count_value = payload.get(
                    "MEMS_SAMPLE_COUNT",
                    payload.get("ADXL_SAMPLE_COUNT", ""),
                )

                if rate_text and str(count_value).strip():
                    self._sync_fft_graph_config(
                        "ADXL",
                        float(rate_text),
                        int(count_value),
                        reason=reason,
                    )

        except Exception as e:
            self._log(
                f"[FFT CONFIG SYNC] payload 적용 실패: {e}"
            )

    def _pg_send_snapshot(self, mode):
        mode = str(mode).upper()
        prefix = "adxl" if mode == "ADXL" else ("piezo_scipy" if mode == "PIEZO_SCIPY" else "piezo")
        self._pg_poll_commands(mode)
        process = getattr(self, f"pg_{prefix}_process", None)
        if process is None or not process.is_alive():
            return False

        store = self.adxl_store if mode == "ADXL" else self.piezo_store
        devices = store.device_list()
        last_devices = getattr(self, f"pg_{prefix}_last_devices", [])
        # V017: 프로세스간 큐가 가득 차면 최초 devices 메시지가 유실될 수 있다.
        # 목록에 변화가 없어도 2초마다 다시 전달해 콤보박스 공백 상태를 복구한다.
        now = time.monotonic()
        last_send = float(getattr(self, f"pg_{prefix}_last_device_send_at", 0.0))
        if list(devices) != list(last_devices) or now - last_send >= 2.0:
            setattr(self, f"pg_{prefix}_last_devices", list(devices))
            setattr(self, f"pg_{prefix}_last_device_send_at", now)
            q = getattr(self, f"pg_{prefix}_data_queue", None)
            if q is not None:
                self._pg_put_latest(q, {"type": "devices", "devices": list(devices)})

        selected = getattr(self, f"pg_{prefix}_selected", "")
        if selected not in devices:
            if not devices:
                return True
            selected = devices[0]
            setattr(self, f"pg_{prefix}_selected", selected)
            setattr(self, f"pg_{prefix}_last_version", -1)

        version, axes = store.snapshot(selected)
        # V019: 같은 버전이라도 2초마다 최신 스냅샷을 재전송한다.
        # IPC 큐에서 한 번 유실된 그래프 갱신 메시지를 복구할 수 있다.
        last_version = getattr(self, f"pg_{prefix}_last_version", -1)
        last_snapshot_at = float(getattr(self, f"pg_{prefix}_last_snapshot_send_at", 0.0))
        if version == last_version and now - last_snapshot_at < 2.0:
            return True
        setattr(self, f"pg_{prefix}_last_version", version)
        setattr(self, f"pg_{prefix}_last_snapshot_send_at", now)

        payload_axes = {}
        axes_to_send = ("X", "Y", "Z") if mode == "ADXL" else ("Z",)
        for axis in axes_to_send:
            rec = axes.get(axis)
            if rec is None:
                continue
            payload_axes[axis] = {
                "samples": np.array(rec.get("samples", []), dtype=np.float64, copy=True),
                "freqs": np.array(rec.get("freqs", []), dtype=np.float64, copy=True),
                "magnitude": np.array(rec.get("magnitude", []), dtype=np.float64, copy=True),
                "sample_rate": float(rec.get("sample_rate", 0.0)),
                "numofsample": int(rec.get("numofsample", len(rec.get("samples", [])))),
                "packet_sample_rate": str(rec.get("packet_sample_rate", "")),
                "packet_numofsample": str(rec.get("packet_numofsample", "")),
                "g_range": str(rec.get("g_range", "")),
                "gain": str(rec.get("gain", "")),
                "fft_n": int(
                    rec.get(
                        "fft_n",
                        len(rec.get("samples", [])),
                    )
                ),
                "fft_window_size": int(
                    rec.get(
                        "fft_window_size",
                        rec.get(
                            "fft_n",
                            len(rec.get("samples", [])),
                        ),
                    )
                ),
                "fft_segment_start": int(
                    rec.get("fft_segment_start", 0)
                ),
                "fft_window": str(
                    rec.get("fft_window", "Hann")
                ),
                "fft_window_beta": float(
                    rec.get("fft_window_beta", 8.6)
                ),
                "peak_bin": int(rec.get("peak_bin", -1)),
                "peak_hz": float(rec.get("peak_hz", 0.0)),
                "peak_magnitude": float(rec.get("peak_magnitude", 0.0)),
                "raw_mean": float(rec.get("raw_mean", 0.0)),
                "raw_min": float(rec.get("raw_min", 0.0)),
                "raw_max": float(rec.get("raw_max", 0.0)),
                "raw_rms": float(rec.get("raw_rms", 0.0)),
                "piezo_pp_packet_mv": float(rec.get("piezo_pp_packet_mv", 0.0)),
                "piezo_pp_sum_max_count": float(rec.get("piezo_pp_sum_max_count", 0.0)),
                "piezo_pp_sum_min_count": float(rec.get("piezo_pp_sum_min_count", 0.0)),
                "piezo_pp_sum_max_mv": float(rec.get("piezo_pp_sum_max_mv", 0.0)),
                "piezo_pp_sum_min_mv": float(rec.get("piezo_pp_sum_min_mv", 0.0)),
                "piezo_pp_total_mv": float(rec.get("piezo_pp_total_mv", 0.0)),
                "piezo_pp_packet_count": int(rec.get("piezo_pp_packet_count", 0)),
                "seq": str(rec.get("seq", "")),
            }

        q = getattr(self, f"pg_{prefix}_data_queue", None)
        if q is not None:
            self._pg_put_latest(q, {"type": "snapshot", "device_id": selected, "axes": payload_axes})
        return True

    def open_adxl_graph(self):
        started = self._pg_start_viewer("ADXL")
        if started:
            self.root.after(100, self._refresh_adxl_graph)

    def _refresh_adxl_graph(self):
        if self._pg_send_snapshot("ADXL"):
            self.root.after(GRAPH_REFRESH_MS, self._refresh_adxl_graph)

    def open_piezo_graph(self):
        self._pg_start_viewer("PIEZO")
        # V019: 이미 열린 창이어도 갱신 루프가 중단되어 있으면 복구.
        if not getattr(self, "_piezo_graph_refresh_running", False):
            self._piezo_graph_refresh_running = True
            self.root.after(100, self._refresh_piezo_graph)

    def _refresh_piezo_graph(self):
        try:
            if self._pg_send_snapshot("PIEZO"):
                self.root.after(GRAPH_REFRESH_MS, self._refresh_piezo_graph)
            else:
                self._piezo_graph_refresh_running = False
        except Exception as exc:
            self._piezo_graph_refresh_running = False
            self._log(f"[V019] PIEZO 그래프 갱신 실패: {type(exc).__name__}: {exc}")

    def open_piezo_scipy_graph(self):
        """Open a second independent viewer for the *same* PIEZO store data."""
        try:
            from scipy import signal, fft  # noqa: F401
        except ImportError:
            messagebox.showerror("SciPy 필요", "비교 기능을 사용하려면 설치하세요:\npython -m pip install scipy")
            return
        started = self._pg_start_viewer("PIEZO_SCIPY")
        if started:
            self.root.after(100, self._refresh_piezo_scipy_graph)

    def _refresh_piezo_scipy_graph(self):
        if self._pg_send_snapshot("PIEZO_SCIPY"):
            self.root.after(GRAPH_REFRESH_MS, self._refresh_piezo_scipy_graph)

    def _pg_start_ultrasonic_viewer(self):
        process = self.pg_ultrasonic_process
        if process is not None and process.is_alive():
            return True

        ctx = mp.get_context("spawn")
        data_queue = ctx.Queue(maxsize=6)
        command_queue = ctx.Queue(maxsize=20)
        process = ctx.Process(
            target=_pyqtgraph_ultrasonic_viewer_process,
            args=(data_queue, command_queue),
            daemon=True,
            name="ultrasonic-pyqtgraph",
        )
        process.start()

        self.pg_ultrasonic_process = process
        self.pg_ultrasonic_data_queue = data_queue
        self.pg_ultrasonic_command_queue = command_queue
        self.pg_ultrasonic_selected = ""
        self.pg_ultrasonic_last_devices = []
        self.pg_ultrasonic_last_version = -1
        self.pg_ultrasonic_error_shown = False

        self._pg_send_ultrasonic_snapshot()
        return True

    def _pg_poll_ultrasonic_commands(self):
        q = self.pg_ultrasonic_command_queue
        if q is None:
            return

        try:
            while True:
                msg = q.get_nowait()
                typ = msg.get("type")

                if typ == "select":
                    self.pg_ultrasonic_selected = str(
                        msg.get("device_id", "")
                    )
                    self.pg_ultrasonic_last_version = -1

                elif typ == "closed":
                    self.pg_ultrasonic_process = None
                    self.pg_ultrasonic_data_queue = None
                    self.pg_ultrasonic_command_queue = None
                    return

                elif typ == "error":
                    if not self.pg_ultrasonic_error_shown:
                        self.pg_ultrasonic_error_shown = True
                        messagebox.showerror(
                            "PyQtGraph",
                            str(msg.get(
                                "message",
                                "ULTRASONIC PyQtGraph 실행 오류",
                            )),
                        )
        except queue.Empty:
            pass
        except Exception:
            pass

    def _pg_send_ultrasonic_snapshot(self):
        self._pg_poll_ultrasonic_commands()

        process = self.pg_ultrasonic_process
        q = self.pg_ultrasonic_data_queue
        if process is None or not process.is_alive() or q is None:
            return False

        devices = self.ultrasonic_store.device_list()
        if devices != self.pg_ultrasonic_last_devices:
            self.pg_ultrasonic_last_devices = list(devices)
            self._pg_put_latest(
                q,
                {"type": "devices", "devices": list(devices)},
            )

        selected = self.pg_ultrasonic_selected
        if selected not in devices:
            if not devices:
                return True
            selected = devices[0]
            self.pg_ultrasonic_selected = selected
            self.pg_ultrasonic_last_version = -1

        version, record = self.ultrasonic_store.snapshot(selected)
        if record is None:
            return True

        if version == self.pg_ultrasonic_last_version:
            return True

        self.pg_ultrasonic_last_version = version

        self._pg_put_latest(
            q,
            {
                "type": "snapshot",
                "device_id": selected,
                "samples": np.array(
                    record.get("samples", []),
                    dtype=np.float64,
                    copy=True,
                ),
                "freq_khz": np.array(
                    record.get("freq_khz", []),
                    dtype=np.float64,
                    copy=True,
                ),
                "minKhz": int(record.get("minKhz", 0)),
                "intervalHz": int(record.get("intervalHz", 0)),
                "seq": str(record.get("seq", "")),
                "value_min": float(record.get("value_min", 0.0)),
                "value_max": float(record.get("value_max", 0.0)),
                "peak_value": float(record.get("peak_value", 0.0)),
                "peak_khz": float(record.get("peak_khz", 0.0)),
            },
        )
        return True

    def open_ultrasonic_graph(self):
        started = self._pg_start_ultrasonic_viewer()
        if started:
            self.root.after(
                100,
                self._refresh_ultrasonic_graph,
            )

    def _refresh_ultrasonic_graph(self):
        if self._pg_send_ultrasonic_snapshot():
            self.root.after(
                GRAPH_REFRESH_MS,
                self._refresh_ultrasonic_graph,
            )

    def _stop_ultrasonic_viewer(self):
        q = self.pg_ultrasonic_data_queue
        p = self.pg_ultrasonic_process

        if q is not None:
            self._pg_put_latest(q, {"type": "close"})

        if p is not None:
            try:
                p.join(timeout=1.0)
            except Exception:
                pass

            if p.is_alive():
                try:
                    p.terminate()
                except Exception:
                    pass
                try:
                    p.join(timeout=0.5)
                except Exception:
                    pass

        self.pg_ultrasonic_process = None
        self.pg_ultrasonic_data_queue = None
        self.pg_ultrasonic_command_queue = None

    def _stop_pg_viewer(self, mode):
        mode = str(mode).upper()
        prefix = "adxl" if mode == "ADXL" else ("piezo_scipy" if mode == "PIEZO_SCIPY" else "piezo")
        q = getattr(self, f"pg_{prefix}_data_queue", None)
        p = getattr(self, f"pg_{prefix}_process", None)
        if q is not None:
            self._pg_put_latest(q, {"type": "close"})
        if p is not None:
            try: p.join(timeout=1.0)
            except Exception: pass
            if p.is_alive():
                try: p.terminate()
                except Exception: pass
                try: p.join(timeout=0.5)
                except Exception: pass
        setattr(self, f"pg_{prefix}_process", None)

    def _refresh(self):
        while True:
            try:
                item = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if item[0] == "log":
                self._log(item[1])
            elif item[0] == "sensor_log":
                self._sensor_log(item[1], item[2])
            elif item[0] == "csv_session_on_connect":
                if self.connected and self.csv_enabled.get():
                    self._start_csv_session()
            elif item[0] == "serial_config_ready":
                # Serial Read Thread에서 감지한 ready를 UI Thread에서 처리.
                self._send_serial_config_on_ready()
            elif item[0] == "sensor_config_response":
                response = item[1]
                self._apply_current_sensor_config(
                    response.get("device_id", ""),
                    response.get("config", {}),
                )
            elif item[0] == "status":
                status = item[1]
                self.status.set(status)

                if status == "CONNECTED":
                    self.btn_connect.configure(state="disabled")
                    self.btn_disconnect.configure(state="normal")
                    self.btn_start.configure(state="normal")
                    self.btn_sensor_start.configure(state="normal")
                    self.btn_sensor_stop.configure(state="normal")
                    self.btn_config_apply.configure(state="normal")
                    self._update_transport_ui()
                else:
                    self.connected = False
                    self.btn_connect.configure(state="normal")
                    self.btn_disconnect.configure(state="disabled")
                    self.btn_start.configure(state="disabled")
                    self.btn_stop.configure(state="disabled")
                    self.btn_sensor_start.configure(state="disabled")
                    self.btn_sensor_stop.configure(state="disabled")
                    self.btn_config_apply.configure(state="disabled")
                    self.btn_config_read.configure(state="disabled")
                    self.pending_config_query_device = None
                    self._update_transport_ui()

        s = self.stats.snapshot()
        self.vars["elapsed"].set(f"{s['elapsed']:.1f} s")
        self.vars["messages"].set(f"{s['messages']:,}")
        self.vars["data"].set(self.fmt_bytes(s["bytes"]))
        self.vars["cur_msg"].set(f"{s['current_msg_rate']:,.2f} msg/s")
        self.vars["avg_msg"].set(f"{s['avg_msg_rate']:,.2f} msg/s")
        self.vars["cur_rate"].set(self.fmt_rate(s["current_byte_rate"]))
        self.vars["avg_rate"].set(self.fmt_rate(s["avg_byte_rate"]))
        self.vars["ignored"].set(f"{s['ignored']:,}")
        self.vars["errors"].set(f"{s['json_errors']:,}")

        for name, b in s["buckets"].items():
            self.tree.item(
                name,
                values=(
                    name,
                    f"{b['messages']:,}",
                    self.fmt_bytes(b["payload_bytes"]),
                    f"{b['sensor_hex_chars']:,}",
                    f"{b['avg_interval']:.3f}",
                    f"{b['min_interval']:.3f}",
                    f"{b['max_interval']:.3f}",
                )
            )

        # dvicMacAddr 자동 감지 목록 갱신
        registry = self.sensor_registry.snapshot()
        current_ids = set(self.detected_tree.get_children())
        wanted_ids = {rec["device_id"] for rec in registry}

        for iid in current_ids - wanted_ids:
            self.detected_tree.delete(iid)

        for rec in registry:
            iid = rec["device_id"]
            values = (
                rec["device_id"],
                rec["type"],
                rec["axes"],
                rec["source"],
                f"{rec['count']:,}",
                rec["last"],
            )

            if iid in current_ids:
                self.detected_tree.item(iid, values=values)
            else:
                self.detected_tree.insert("", "end", iid=iid, values=values)

        if self.config_window is not None and self.config_window.winfo_exists():
            self._refresh_config_devices()

        with self.csv_counter_lock:
            csv_saved = self.csv_saved_count
            csv_errors = self.csv_error_count

        self.csv_status_var.set(
            f"저장 {csv_saved:,} | Queue {self.csv_queue.qsize():,} | 오류 {csv_errors:,}"
        )

        self.root.after(UI_REFRESH_MS, self._refresh)

    def _close(self):
        try:
            self._stop_pg_viewer("ADXL")
        except Exception:
            pass
        try:
            self._stop_pg_viewer("PIEZO")
        except Exception:
            pass
        try:
            self._stop_pg_viewer("PIEZO_SCIPY")
        except Exception:
            pass
        try:
            self._stop_ultrasonic_viewer()
        except Exception:
            pass

        self.graph_stop.set()
        self.csv_stop.set()

        try:
            self.disconnect()
        except Exception:
            pass

        # 남아있는 CSV Queue를 가능한 범위에서 정상적으로 비우고 파일 flush
        try:
            self.csv_thread.join(timeout=3.0)
        except Exception:
            pass

        if self.graph_window is not None:
            try:
                self.graph_window.destroy()
            except Exception:
                pass

        if self.piezo_graph_window is not None:
            try:
                self.piezo_graph_window.destroy()
            except Exception:
                pass

        if self.config_window is not None:
            try:
                self.config_window.destroy()
            except Exception:
                pass

        self.root.destroy()


def main():
    mp.freeze_support()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

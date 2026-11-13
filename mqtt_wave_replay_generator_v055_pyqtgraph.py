# SRSS: overall AC RMS, window-power correction; FFT display only.
# FFT roundtrip diagnostic: signed frequency, complex IFFT, input error verification.
# V042: descriptive unit labels; preserve canonical FFT unit values.
# Based on V041: sequenced packet navigation, stale-frame rejection, persistent Qt curves.
# V037-PG PREVIEW: PyQtGraph separate-process renderer; original V036 untouched.
# V036: MEMS FFT gN = sqrt(X^2+Y^2+Z^2), purple #9467BD; includes averaging and velocity.
# V035: MEMS FFT velocity RMS display option (mm/s); preserves acceleration, RAW, and MQTT.
# V032: ULTRASONIC 320-point frequency spectrum preview (no repeated FFT).
# V034: Optional status metadata defaults during MQTT replay, preserving old CSV.
# V031: ULTRASONIC receiver-exported sensorData_hex CSV import support.
# V020: MEMS 1 LSB(g) user scale with persistence; generator preview only, published data unchanged.
# -*- coding: utf-8 -*-
"""MQTT single-device waveform replay generator v0.01.
Use one input file per sensor; navigate a packet by packet preview; play from
selected packet to the last record and loop to the *selected packet*.
"""
import csv
import math
from datetime import datetime
import json
import os
import queue
import random
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
from matplotlib.backends.backend_agg import FigureCanvasAgg
import multiprocessing as mp
import sys
from matplotlib.figure import Figure
from matplotlib import font_manager, rcParams

# 한글 그래프 레이블용 글꼴: 설치된 글꼴을 우선 선택한다.
# Windows 기본 "맑은 고딕"을 우선하고, 다른 OS에서도 실행 가능하도록 대체 글꼴을 허용한다.
def configure_korean_plot_font():
    candidates = ('Malgun Gothic', '맑은 고딕', 'NanumGothic',
                  'NanumSquare', 'Noto Sans CJK KR', 'AppleGothic', 'UnDotum')
    for family in candidates:
        try:
            font_manager.findfont(family, fallback_to_default=False)
        except (ValueError, OSError):
            continue
        rcParams['font.family'] = [family]
        rcParams['axes.unicode_minus'] = False
        rcParams['pdf.fonttype'] = 42
        rcParams['ps.fonttype'] = 42
        return family
    rcParams['axes.unicode_minus'] = False
    return None

KOREAN_PLOT_FONT = configure_korean_plot_font()
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

APP_TITLE = '발생기 V055 — SRSS / FFT 과정 검증'
TYPE_NIBBLE = {'PIEZO': 0, 'X': 1, 'Y': 2, 'Z': 3}

def sensor_type_from_seq(seq: str) -> str:
    try:
        high = int(str(seq), 16) // 16
    except Exception:
        return "UNKNOWN"

    if high == 0:
        return "PIEZO"
    if high == 1:
        return "X"
    if high == 2:
        return "Y"
    if high == 3:
        return "Z"
    return "UNKNOWN"


def period_ms_from_payload(obj: dict) -> float:
    try:
        rate = float(obj.get("samplerate", 0))
        count = float(obj.get("numofsample", 0))
        if rate > 0 and count > 0:
            return count / rate * 1000.0
    except Exception:
        pass
    return 0.0


def parse_json_objects(text: str) -> list[dict]:
    """
    다음 입력 형태를 모두 처리:
    - JSONL
    - pretty JSON 1개
    - x / y / z 라벨 + JSON
    - 여러 JSON object가 연속으로 존재하는 TXT
    """
    decoder = json.JSONDecoder()
    objects = []
    pos = 0

    while pos < len(text):
        start = text.find("{", pos)
        if start < 0:
            break

        try:
            obj, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            pos = start + 1
            continue

        if isinstance(obj, dict):
            objects.append(obj)

        pos = start + end

    return objects


def load_json_objects(path: str) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8-sig", errors="strict")
    objects = parse_json_objects(text)

    if not objects:
        raise ValueError("JSON object를 찾지 못했습니다.")

    return objects


def load_adxl_templates(path: str) -> dict[str, list[dict]]:
    objects = load_json_objects(path)

    axes = {"X": [], "Y": [], "Z": []}
    for obj in objects:
        st = sensor_type_from_seq(obj.get("seq", ""))
        if st in axes:
            axes[st].append(obj)

    missing = [axis for axis in ("X", "Y", "Z") if not axes[axis]]
    if missing:
        raise ValueError(
            "ADXL 파일에서 다음 축 JSON을 찾지 못했습니다: " + ", ".join(missing)
        )

    return axes


def recover_adxl_hex_prefix(value, expected_samples):
    """Preserve only valid leading complete int16 words (four hex chars each).

    Never synthesize samples or try to guess beyond the first invalid character.
    Work on an in-memory copy of the CSV row; the source file is untouched.
    """
    value = ''.join(str(value or '').split())
    expected_samples = int(expected_samples)
    if expected_samples < 1:
        raise ValueError('numofsample은 1 이상이어야 합니다.')
    # For malformed source text, accept only the *contiguous* valid hex prefix.
    match = re.match(r'[0-9a-fA-F]*', value)
    prefix = match.group(0)
    # Each 16-bit signed sample occupies exactly four hexadecimal characters.
    prefix = prefix[:min(len(prefix)//4, expected_samples)*4]
    sample_count = len(prefix)//4
    if sample_count < 2:
        raise ValueError(f'복구 가능한 완성 16bit 샘플 부족: {sample_count}개')
    is_partial = (sample_count != expected_samples or len(value) != expected_samples*4 or
                  len(prefix) != len(value))
    return prefix.upper(), sample_count, is_partial


def _adxl_csv_events(paths, *, recover_partial=True):
    """Load XYZ CSV, preserving row markers for sequence alignment.

    Recover a valid *prefix* for graph preview only when requested. Bad or
    shortened source is never overwritten; recovered payloads are tagged so
    publishing can exclude them. Nothing is padded or interpolated.
    """
    events = []
    warnings = []
    counts = {}
    recovered_counts = {'X': 0, 'Y': 0, 'Z': 0}
    for axis in ('X', 'Y', 'Z'):
        path = str(paths.get(axis, '')).strip()
        if not path:
            raise ValueError(f'ADXL-{axis} CSV를 선택하세요.')
        with Path(path).open('r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            if not ('sensorData_hex' in fields or 'sensorData' in fields):
                raise ValueError(f'ADXL-{axis}: sensorData_hex 컬럼이 필요합니다.')
            count = 0
            for row_num, row in enumerate(reader, start=2):
                value = ''.join(str(row.get('sensorData_hex') or row.get('sensorData') or '').split())
                seq = str(row.get('seq') or '').strip()
                when = str(row.get('received_at') or '').strip()
                try:
                    timestamp = datetime.fromisoformat(when.replace('Z', '+00:00')).timestamp() if when else None
                except ValueError:
                    timestamp = None
                if not seq:
                    warnings.append(f'{axis} {row_num}행: seq 없음, 동기화 불가 → 제외')
                    continue
                try:
                    seq_number = int(seq.removeprefix('0x').removeprefix('0X'), 16)
                    if (seq_number >> 4) != TYPE_NIBBLE[axis]:
                        raise ValueError('seq 축 불일치')
                except ValueError:
                    warnings.append(f'{axis} {row_num}행: seq={seq!r} 축 정보 오류 → 제외')
                    continue
                obj = None
                try:
                    n = int(row.get('numofsample') or len(value)//4)
                    fs = float(row.get('samplerate') or 3200)
                    if n < 2 or not math.isfinite(fs) or fs <= 0:
                        raise ValueError(f'유효하지 않은 N/Fs: N={n}, Fs={fs}')
                    label = str(row.get('axis') or '').strip().upper()
                    if label and label not in (axis, 'ADXL-'+axis):
                        raise ValueError(f'축 라벨={label}')
                    if recover_partial:
                        repaired, valid_n, partial = recover_adxl_hex_prefix(value, n)
                    else:
                        raw = bytes.fromhex(value)
                        if len(raw) != n*2:
                            raise ValueError(f'HEX {len(raw)} bytes / N={n} / Fs={fs:g}')
                        repaired, valid_n, partial = value.upper(), n, False
                    obj = {
                        'gwMacAddr': str(row.get('gwMacAddr') or 'HALOW').strip(),
                        'dvicMacAddr': str(row.get('dvicMacAddr') or '').strip(),
                        'batteryRmin': str(row.get('batteryRmin') or '0').strip(),
                        'seq': seq, 'samplerate': str(row.get('samplerate') or '3200').strip(),
                        'numofsample': str(valid_n), 'sensorData': repaired,
                    }
                    for metadata_key in ('acqStatus', 'apSsid', 'apRssi'):
                        metadata_value = row.get(metadata_key)
                        if metadata_value is not None and str(metadata_value).strip():
                            if metadata_key == 'apRssi':
                                try:
                                    obj[metadata_key] = int(str(metadata_value).strip())
                                except ValueError:
                                    obj[metadata_key] = str(metadata_value).strip()
                            else:
                                obj[metadata_key] = str(metadata_value).strip()
                    if partial:
                        obj['_partial_recovered'] = True
                        obj['_original_numofsample'] = n
                        recovered_counts[axis] += 1
                        warnings.append(f'{axis} {row_num}행 seq={seq}: 부분 복구 {valid_n}/{n}샘플 (원본 HEX {len(value)}자; 미리보기 가능, 기본 송신 제외)')
                    if str(row.get('g_range') or '').strip():
                        obj['g_range'] = str(row['g_range']).strip()
                    if str(row.get('tick') or '').strip():
                        obj['tick'] = str(row['tick']).strip()
                except (ValueError, TypeError) as exc:
                    warnings.append(f'{axis} {row_num}행 seq={seq}: 복구 불가({exc}) → 해당 축 제외')
                events.append({'axis': axis, 'low': seq_number & 15, 'seq': seq,
                               'time': timestamp, 'row': row_num, 'obj': obj})
                count += 1
            counts[axis] = count
    return events, warnings, counts, recovered_counts


def load_adxl_csv_xyz(paths: dict[str, str], return_report=False, recover_partial=True):
    """Sequence-aware sparse XYZ grouping; no invented samples or padding.

    Sequence low nibble increments by 1 for each transmitted axis; the high
    nibble identifies X/Y/Z. A missing axis leaves a sparse set. Timestamp is
    only used to order records, never used to synthesize or replace seq.
    """
    events, warnings, counts, recovered_counts = _adxl_csv_events(paths, recover_partial=recover_partial)
    if not events:
        raise ValueError('ADXL CSV에서 seq를 가진 행을 찾지 못했습니다.')
    # With timestamps, merge chronological per-axis CSVs. If timestamps are
    # absent, use row-order/axis ordering as a conservative fallback.
    if any(e['time'] is not None for e in events):
        events.sort(key=lambda e: (e['time'] is None, e['time'] or 0, e['row'], ('X','Y','Z').index(e['axis'])))
    else:
        events.sort(key=lambda e: (e['row'], ('X','Y','Z').index(e['axis'])))
    sets = []
    current = {}
    last_axis = None
    last_low = None
    last_time = None
    for e in events:
        axis = e['axis']
        idx = ('X','Y','Z').index(axis)
        if current:
            prev_idx = ('X','Y','Z').index(last_axis)
            expected = (last_low + (idx - prev_idx)) & 15
            # If the stream order/rolling seq doesn't match, do not force the
            # packet into this set; start a new partial set instead.
            mismatch = idx <= prev_idx or expected != e['low']
            if mismatch:
                sets.append(current)
                current = {}
        if e['obj'] is not None:
            # A malformed axis is represented by its absence, not fake zeros.
            current[axis] = e['obj']
        last_axis, last_low, last_time = axis, e['low'], e['time']
    if current:
        sets.append(current)
    # A completely bad set has no drawable/transmittable axes.
    useful = [item for item in sets if item]
    skipped = len(sets) - len(useful)
    if skipped:
        warnings.append(f'유효한 축이 없는 세트 {skipped}건 제외')
    if not useful:
        raise ValueError('전송 가능한 ADXL 데이터가 없습니다. HEX와 N을 확인하세요.')
    report = {'rows': counts, 'set_count': len(useful),
              'partial': sum(len(x)<3 for x in useful), 'warnings': warnings,
              'axis_packets': {a:sum(a in x for x in useful) for a in ('X','Y','Z')},
              'recovered': recovered_counts}
    return (useful, report) if return_report else useful


def load_piezo_csv(path: str) -> list[dict]:
    """
    vibration_comm_speed_test 계열에서 저장한 PIEZO HEX CSV를
    MQTT 송신용 PIEZO JSON template 목록으로 변환한다.

    지원 컬럼:
      - sensorData_hex (필수, sensorData도 호환)
      - samplerate
      - numofsample
      - gwMacAddr / dvicMacAddr / batteryRmin / seq / tick

    CSV의 부가 컬럼(packet_no, received_at, payload_bytes 등)은
    MQTT payload에는 포함하지 않는다.
    """
    templates = []

    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise ValueError("PIEZO CSV 헤더를 찾지 못했습니다.")

        has_sensor_hex = (
            "sensorData_hex" in reader.fieldnames
            or "sensorData" in reader.fieldnames
        )
        if not has_sensor_hex:
            raise ValueError(
                "PIEZO CSV에 sensorData_hex 또는 sensorData 컬럼이 필요합니다."
            )

        for row_no, row in enumerate(reader, start=2):
            sensor_hex = str(
                row.get("sensorData_hex")
                or row.get("sensorData")
                or ""
            ).strip()

            if not sensor_hex:
                continue

            # 공백이 섞여 있어도 허용
            sensor_hex = "".join(sensor_hex.split())

            try:
                raw = bytes.fromhex(sensor_hex)
            except ValueError as e:
                raise ValueError(
                    f"PIEZO CSV {row_no}행 sensorData HEX 오류: {e}"
                ) from e

            if len(raw) % 2:
                raise ValueError(
                    f"PIEZO CSV {row_no}행 sensorData byte 길이가 홀수입니다: "
                    f"{len(raw)} bytes"
                )

            samplerate = str(row.get("samplerate") or "25600").strip()
            numofsample = str(row.get("numofsample") or "").strip()

            if not numofsample:
                numofsample = str(len(raw) // 2)

            try:
                expected_bytes = int(numofsample) * 2
            except ValueError as e:
                raise ValueError(
                    f"PIEZO CSV {row_no}행 numofsample 값 오류: {numofsample}"
                ) from e

            if expected_bytes != len(raw):
                raise ValueError(
                    f"PIEZO CSV {row_no}행 길이 불일치: "
                    f"sensorData={len(raw)} bytes, "
                    f"numofsample={numofsample} -> {expected_bytes} bytes"
                )

            seq = str(row.get("seq") or "00").strip()
            if sensor_type_from_seq(seq) != "PIEZO":
                raise ValueError(
                    f"PIEZO CSV {row_no}행 seq={seq}가 PIEZO 형식이 아닙니다."
                )

            obj = {
                "gwMacAddr": str(row.get("gwMacAddr") or "HALOW").strip(),
                "dvicMacAddr": str(row.get("dvicMacAddr") or "").strip(),
                "batteryRmin": str(row.get("batteryRmin") or "0").strip(),
                "seq": seq,
                "samplerate": samplerate,
                "numofsample": numofsample,
                "sensorData": sensor_hex.upper(),
            }

            tick = str(row.get("tick") or "").strip()
            if tick:
                obj["tick"] = tick
            for metadata_key in ('acqStatus', 'apSsid', 'apRssi'):
                metadata_value = row.get(metadata_key)
                if metadata_value is not None and str(metadata_value).strip():
                    if metadata_key == 'apRssi':
                        try:
                            obj[metadata_key] = int(str(metadata_value).strip())
                        except ValueError:
                            obj[metadata_key] = str(metadata_value).strip()
                    else:
                        obj[metadata_key] = str(metadata_value).strip()

            templates.append(obj)

    if not templates:
        raise ValueError("PIEZO CSV에서 전송 가능한 데이터 행을 찾지 못했습니다.")

    return templates


def load_piezo_templates(path: str) -> list[dict]:
    """
    PIEZO 입력은 기존 JSON/TXT/JSONL과
    vibration_comm_speed_test에서 저장한 HEX CSV를 모두 지원한다.
    """
    if Path(path).suffix.lower() == ".csv":
        return load_piezo_csv(path)

    objects = load_json_objects(path)

    piezo = [
        obj for obj in objects
        if sensor_type_from_seq(obj.get("seq", "")) == "PIEZO"
        and "samplerate" in obj
        and "numofsample" in obj
    ]

    if not piezo:
        raise ValueError(
            "PIEZO JSON을 찾지 못했습니다. "
            "seq high nibble=0이며 samplerate/numofsample 필드가 있어야 합니다."
        )

    return piezo


def load_ultrasonic_csv(path: str) -> list[dict]:
    """Import H packets from receiver-exported CSV without modifying sensorData.

    H payload: 12-byte header + 320 float32 readings (1292 bytes total).
    CSV metadata such as packet_no/received_at/source is not published.
    """
    templates = []
    errors = []
    with Path(path).open('r', encoding='utf-8-sig', newline='') as fh:
        reader = csv.DictReader(fh)
        names = reader.fieldnames or []
        hex_column = 'sensorData_hex' if 'sensorData_hex' in names else ('sensorData' if 'sensorData' in names else None)
        if not hex_column:
            raise ValueError('ULTRASONIC CSV에 sensorData_hex 또는 sensorData 컬럼이 필요합니다.')
        for line_no, row in enumerate(reader, start=2):
            value = ''.join(str(row.get(hex_column) or '').split())
            try:
                if not value:
                    raise ValueError('sensorData가 비어 있습니다')
                raw = bytes.fromhex(value)
                if len(raw) != 1292:
                    raise ValueError(f'HEX 데이터 {len(raw)}바이트 (정상: 1292바이트)')
                declared = str(row.get('sensorData_bytes') or '').strip()
                if declared and int(declared) != len(raw):
                    raise ValueError(f'sensorData_bytes={declared} / 실제={len(raw)}')
                n = str(row.get('numofsample') or '').strip()
                if n and int(n) != 320:
                    raise ValueError(f'numofsample={n} (정상: 320)')
                # Validate the expected float32 payload. Values are left untouched.
                floats = np.frombuffer(raw, dtype='<f4', offset=12, count=320)
                if not np.all(np.isfinite(floats)):
                    raise ValueError('float32 데이터에 NaN/Inf가 있습니다')
                obj = {
                    'gwMacAddr': str(row.get('gwMacAddr') or 'HALOW').strip(),
                    'dvicMacAddr': str(row.get('dvicMacAddr') or '').strip(),
                    'batteryRmin': str(row.get('batteryRmin') or '0').strip(),
                    'seq': str(row.get('seq') or '00').strip(),
                    'sensorData': value.upper(),
                }
                if str(row.get('tick') or '').strip():
                    obj['tick'] = str(row['tick']).strip()
                for metadata_key in ('acqStatus', 'apSsid', 'apRssi'):
                    metadata_value = row.get(metadata_key)
                    if metadata_value is not None and str(metadata_value).strip():
                        if metadata_key == 'apRssi':
                            try:
                                obj[metadata_key] = int(str(metadata_value).strip())
                            except ValueError:
                                obj[metadata_key] = str(metadata_value).strip()
                        else:
                            obj[metadata_key] = str(metadata_value).strip()
                templates.append(obj)
            except (ValueError, TypeError) as exc:
                errors.append(f'{line_no}행: {exc}')
    if errors:
        raise ValueError('초음파 CSV에 오류가 있어 불완전한 데이터 전송을 막았습니다. '
                         f'오류 {len(errors)}건. 예: ' + '; '.join(errors[:5]))
    if not templates:
        raise ValueError('ULTRASONIC CSV에서 유효한 데이터 행을 찾지 못했습니다.')
    return templates


def load_ultrasonic_templates(path: str) -> list[dict]:
    """Support original JSON/JSONL/TXT format and 320-point H HEX CSV export."""
    if Path(path).suffix.lower() == '.csv':
        return load_ultrasonic_csv(path)

    objects = load_json_objects(path)
    usable = [obj for obj in objects if 'sensorData' in obj]
    if not usable:
        raise ValueError('초음파 JSON에서 sensorData 필드를 찾지 못했습니다.')
    return usable


def with_replay_metadata(packet):
    """Return publish-only packet with optional Mode A status metadata filled.

    Missing/blank values from older CSV/JSON get synthetic replay defaults.
    Existing nonempty source fields are preserved, including numeric apRssi.
    This does not modify templates, CSV, or the sensor payload.
    """
    packet = dict(packet)
    defaults = {'acqStatus': 'OK', 'apSsid': 'REPLAY', 'apRssi': 0}
    for key, default in defaults.items():
        value = packet.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            packet[key] = default
    return packet


class SequenceState:
    """
    진동용 seq:
      high nibble = PIEZO/X/Y/Z 종류
      low nibble  = 0~F rolling counter
    """
    def __init__(self, start=0):
        self.value = start & 0xF
        self.lock = threading.Lock()

    def next_seq(self, sensor_type: str) -> str:
        with self.lock:
            low = self.value
            self.value = (self.value + 1) & 0xF

        high = TYPE_NIBBLE[sensor_type]
        return f"{high:X}{low:X}"



def decode_packet(sensor_kind, obj):
    """Decode a single selected packet for preview. Never rewrite MQTT data."""
    raw_hex = str(obj.get('sensorData','')).strip()
    raw = bytes.fromhex(''.join(raw_hex.split()))
    if sensor_kind == 'PIEZO':
        if len(raw) % 2: raise ValueError('PIEZO bytes 길이는 짝수여야 합니다')
        samples=np.frombuffer(raw,dtype='>i2').astype(np.float64)
        fs=float(obj.get('samplerate') or 25600)
        return samples, fs, 'Count'
    if sensor_kind == 'ADXL':
        if len(raw) % 2: raise ValueError('ADXL bytes 길이는 짝수여야 합니다')
        samples=np.frombuffer(raw,dtype='>i2').astype(np.float64)
        fs=float(obj.get('samplerate') or 3200)
        return samples, fs, 'Count'
    # Ultrasonic H data: source payload contains float32 samples after optional prefix.
    # Without knowing the concrete H binary envelope, preview must not invent a decoder.
    raise ValueError('초음파 H는 현재 RAW 바이너리 포맷 미확정: 송신은 지원, 그래프는 미지원')



def decode_ultrasonic_spectrum(packet):
    """Decode receiver-compatible H frame for plotting, without touching MQTT payload.

    12-byte header, then 320 little-endian float32 frequency-domain bins.
    Byte 8: starting frequency (kHz); byte 9: frequency step (Hz).
    If the input was still byte-stuffed, decode 7D5E/7D5D as the receiver does.
    """
    value = ''.join(str(packet.get('sensorData') or '').split())
    raw = bytes.fromhex(value)
    if len(raw) != 1292:
        recovered = bytearray()
        i = 0
        while i < len(raw):
            if raw[i] == 0x7D and i + 1 < len(raw) and raw[i + 1] in (0x5E, 0x5D):
                recovered.append(0x7E if raw[i + 1] == 0x5E else 0x7D)
                i += 2
            else:
                recovered.append(raw[i])
                i += 1
        raw = bytes(recovered)
    if len(raw) != 1292:
        raise ValueError(f'초음파 H 프레임 {len(raw)}바이트 (정상 1292바이트)')
    start_khz = int(raw[8])
    step_hz = int(raw[9])
    if step_hz <= 0:
        raise ValueError(f'초음파 주파수 간격 오류: {step_hz} Hz')
    amplitude = np.frombuffer(raw, dtype='<f4', count=320, offset=12).astype(np.float64)
    if not np.all(np.isfinite(amplitude)):
        raise ValueError('초음파 주파수 값에 NaN 또는 Inf가 있습니다')
    frequency_khz = start_khz + np.arange(320, dtype=np.float64) * step_hz / 1000.0
    return frequency_khz, amplitude, start_khz, step_hz


def spectral_filter(x, fs, *, hpf=False, hp_cutoff=10.0, hp_order=4,
                    lpf=False, lp_cutoff=3000.0, lp_order=4,
                    notch=False, notch_center=975.0, notch_width=20.0):
    data=np.asarray(x,dtype=np.float64)
    if data.size<2 or not (hpf or lpf or notch): return data.copy()
    bins=np.fft.rfftfreq(len(data), d=1/fs)
    resp=np.ones_like(bins)
    if hpf:
        if not 0 < hp_cutoff < fs/2: raise ValueError('HPF 컷오프는 Fs/2보다 작아야 합니다')
        p=np.zeros_like(bins); mask=bins>0
        p[mask]=1/np.sqrt(1+(hp_cutoff/bins[mask])**(2*hp_order))
        resp*=p
    # Auto / Fs/2 means full available bandwidth (no LPF attenuation).
    if lpf and lp_cutoff is not None:
        if not 0 < lp_cutoff < fs/2: raise ValueError('LPF 컷오프는 Fs/2보다 작아야 합니다')
        resp*=1/np.sqrt(1+(bins/lp_cutoff)**(2*lp_order))
    if notch:
        if not (0 < notch_center < fs/2 and 0 < notch_width < fs/2):
            raise ValueError('Notch 주파수/폭은 0~Fs/2 안으로 설정하세요')
        sigma=notch_width/(2*np.sqrt(2*np.log(2)))
        resp*=1-np.exp(-.5*((bins-notch_center)/sigma)**2)
    return np.fft.irfft(np.fft.rfft(data)*resp, n=len(data))


FFT_WINDOWS=('Rectangular','Hann','Hamming','Blackman','Blackman-Harris','Flat Top','Bartlett','Kaiser')

def fft_window(n, name='Blackman', beta=8.6):
    if name=='Rectangular': return np.ones(n)
    if name=='Hamming': return np.hamming(n)
    if name=='Blackman': return np.blackman(n)
    if name=='Bartlett': return np.bartlett(n)
    if name=='Kaiser': return np.kaiser(n,beta)
    if name=='Blackman-Harris':
        a=np.arange(n)*2*np.pi/max(1,n-1)
        return .35875-.48829*np.cos(a)+.14128*np.cos(2*a)-.01168*np.cos(3*a)
    if name=='Flat Top':
        a=np.arange(n)*2*np.pi/max(1,n-1)
        return .21557895-.41663158*np.cos(a)+.277263158*np.cos(2*a)-.083578947*np.cos(3*a)+.006947368*np.cos(4*a)
    return np.hanning(n)

def fft_peak(x,fs,window='Blackman',window_size=None,beta=8.6):
    x=np.asarray(x,dtype=np.float64)
    n=len(x) if window_size is None else min(len(x),max(2,int(window_size)))
    if n<2: return np.array([]),np.array([])
    offset=(len(x)-n)//2
    x=x[offset:offset+n]
    x=x-np.mean(x)
    win=fft_window(n,window,beta)
    scale=win.sum()
    if scale<=0: raise ValueError('FFT 창 함수 합계가 0입니다.')
    mag=2*np.abs(np.fft.rfft(x*win))/scale
    mag[0]/=2
    if n%2==0: mag[-1]/=2
    return np.fft.rfftfreq(n,d=1/fs),mag



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

def rotating_candidates(freqs, amps, rpm, min_count=2, max_count=48):
    """Receiver-style relative harmonic matching; exploratory, not a diagnosis."""
    f=np.asarray(freqs); a=np.asarray(amps)
    if rpm<=0 or len(f)<5: return []
    peaks=np.where((a[1:-1]>a[:-2])&(a[1:-1]>=a[2:]))[0]+1
    if len(peaks)==0: return []
    floor=np.median(a[a>0]) if np.any(a>0) else 0
    peaks=peaks[a[peaks]>=max(3*floor, .015*np.max(a))]
    peaks=peaks[np.argsort(a[peaks])[-120:]]
    df=float(np.median(np.diff(f))); rot=rpm/60
    results=[]
    for count in range(min_count,max_count+1):
        base=rot*count
        if base>=f[-1]-df: continue
        score=0.; matched=[]
        for h in range(1,min(5,int(f[-1]//base))+1):
            target=base*h; tol=max(1.5*df,.015*target)
            idx=peaks[np.abs(f[peaks]-target)<=tol]
            if len(idx)==0: continue
            j=idx[np.argmax(a[idx])]
            ratio=float(a[j]/max(np.max(a),1e-12))
            proximity=max(0,1-abs(f[j]-target)/tol)
            score+=(ratio**.5)*(.5+.5*proximity)/h**.6
            matched.append((h,round(float(f[j]),2)))
        if matched:
            score*=1 if any(h==1 for h,_ in matched) else .35
            score*=1+.18*min(len(matched)-1,3)
            results.append((score,count,base,matched))
    return sorted(results,reverse=True)[:5]



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


def show_fft_diagnostic_tk(parent, records, title):
    if not records:
        messagebox.showinfo('FFT/IFFT','선택된 RAW 데이터가 없습니다. 초음파 H는 위상이 없는 스펙트럼입니다.',parent=parent); return
    window=tk.Toplevel(parent); window.title('FFT 과정 / IFFT 검증 · '+title); window.geometry('1100x850')
    bar=ttk.Frame(window); bar.pack(fill='x',padx=6,pady=4)
    axis=tk.StringVar(value=next(iter(records))); stage=tk.StringVar(value='원본 RAW (복원 검증)'); parts=tk.BooleanVar(value=False)
    ttk.Label(bar,text='축').pack(side='left'); ttk.Combobox(bar,textvariable=axis,values=list(records),state='readonly',width=10).pack(side='left',padx=4)
    ttk.Label(bar,text='FFT 입력').pack(side='left'); ttk.Combobox(bar,textvariable=stage,values=['원본 RAW (복원 검증)','현재 FFT 전처리 적용'],state='readonly',width=24).pack(side='left',padx=4)
    ttk.Checkbutton(bar,text='실수부 / 허수부',variable=parts).pack(side='left',padx=4)
    status=tk.StringVar(); ttk.Label(window,textvariable=status,wraplength=1050).pack(fill='x',padx=8)
    ttk.Label(window,text='① 입력 → ② 양측 FFT → ③ 단측 FFT | ② 복소수 FFT → ④ IFFT 복원 · 평균/SRSS/물리단위 보정은 검증 제외').pack(fill='x',padx=8)
    fig=Figure(figsize=(10,7),dpi=100); plots=fig.subplots(2,2).ravel(); canvas=FigureCanvasTkAgg(fig,master=window);canvas.get_tk_widget().pack(fill='both',expand=True)
    def draw(*_):
        rec=records[axis.get()]; processed=stage.get().startswith('현재'); d=fft_roundtrip_diagnostic(rec['processed'] if processed else rec['raw'],rec['fs'],rec['gain'] if processed else None)
        for ax in plots: ax.clear(); ax.grid(alpha=.25)
        plots[0].plot(d['t'],d['x'],color='#1f77b4');plots[0].set_title('1. FFT input (processed)' if processed else '1. Original RAW (Count, DC included)')
        plots[1].plot(d['f'],d['magnitude'],color='#9467BD',label='Magnitude');plots[1].set_title('2. Two-sided FFT');plots[1].set_xlabel('Frequency (Hz)')
        if parts.get():
            plots[1].plot(d['f'],d['real'],color='#1f77b4',label='Real');plots[1].plot(d['f'],d['imag'],color='#2ca02c',label='Imag');plots[1].legend()
        plots[2].plot(d['single_f'],d['single_magnitude'],color='#d62728');plots[2].set_title('3. Single-sided FFT (Peak)');plots[2].set_xlabel('Frequency (Hz)');plots[2].set_ylabel('Count Peak')
        plots[3].plot(d['t'],d['x'],color='#1f77b4',lw=2,label='FFT input');plots[3].plot(d['t'],d['restored'],color='#d62728',lw=1,label='IFFT');plots[3].set_title('4. IFFT reconstruction');plots[3].legend()
        for ax in (plots[0],plots[3]):ax.set_xlabel('Time (s)');ax.set_ylabel('Count')
        plots[1].set_ylabel('Count');status.set(fft_diagnostic_text(d,'전처리 입력 복원 (원본 아님)' if processed else '원본 RAW 복원'));fig.tight_layout();canvas.draw_idle()
    for var in (axis,stage,parts):var.trace_add('write',draw)
    draw()


class Generator:
    def __init__(self,root):
        self.root=root; root.title(APP_TITLE); root.geometry('1280x660'); root.minsize(960,510)
        self.client=None; self.connected=False; self.connect_event=threading.Event()
        self.stop_event=threading.Event(); self.threads=[]; self.uiq=queue.Queue()
        self.templates={}; self.current_index={'PIEZO':0,'ADXL':0,'초음파':0}
        self.start_indices={}; self.seq_state=SequenceState()
        self.sent=0; self.acked=0; self.dropped=0; self.counter={'PIEZO':0,'ADXL':0,'초음파':0}
        self.counter_lock=threading.Lock(); self.status=tk.StringVar(value='대기')
        self._ui(); self.root.after(125,self._drain)
        root.protocol('WM_DELETE_WINDOW',self.close)

    def _ui(self):
        self.host=tk.StringVar(value='127.0.0.1'); self.port=tk.StringVar(value='1883')
        self.qos=tk.StringVar(value='1'); self.user=tk.StringVar(); self.password=tk.StringVar()
        self.device=tk.StringVar(value='S01'); self.mac=tk.StringVar(value='TEST00000001')
        self.rewrite_mac=tk.BooleanVar(value=False); self.seq_auto=tk.BooleanVar(value=True)
        self.jitter_on=tk.BooleanVar(value=False); self.loss_on=tk.BooleanVar(value=False)
        self.files={s:tk.StringVar() for s in ('PIEZO','ADXL','초음파')}
        self.adxl_csv_files={a:tk.StringVar() for a in ('X','Y','Z')}
        self.recover_partial_adxl=tk.BooleanVar(value=True)
        self.adxl_input_mode='json'
        self.enabled={s:tk.BooleanVar(value=(s=='PIEZO')) for s in self.files}
        self.topics={'PIEZO':tk.StringVar(value='V/{mac}'),
                     'ADXL':tk.StringVar(value='V/{mac}'),
                     '초음파':tk.StringVar(value='D/{mac}')}
        self.period={'PIEZO':tk.StringVar(value='320'),'ADXL':tk.StringVar(value='2560'),'초음파':tk.StringVar(value='10000')}
        self.auto_period={'PIEZO':tk.BooleanVar(value=True),'ADXL':tk.BooleanVar(value=True)}
        self.jitter={s:tk.StringVar(value='0') for s in self.files}
        self.loss={s:tk.StringVar(value='0') for s in self.files}
        self.xyz_gap=tk.StringVar(value='5'); self.tick_step=tk.StringVar(value='0')
        main=ttk.Frame(self.root,padding=8); main.pack(fill='both',expand=True)
        broker=ttk.LabelFrame(main,text='MQTT Broker / 장치 1개 고정'); broker.pack(fill='x',pady=3)
        specs=[('IP',self.host,16),('Port',self.port,6),('QoS',self.qos,4),('User',self.user,12),('Password',self.password,12),('Device ID',self.device,9),('MAC',self.mac,17)]
        for i,(label,var,width) in enumerate(specs):
            ttk.Label(broker,text=label).grid(row=0,column=i*2,padx=(5,2),pady=5)
            ttk.Entry(broker,textvariable=var,width=width,show='*' if label=='Password' else '').grid(row=0,column=i*2+1,padx=(0,5))
        opts=ttk.Frame(broker); opts.grid(row=1,column=0,columnspan=14,sticky='w')
        ttk.Checkbutton(opts,text='MAC 덮어쓰기',variable=self.rewrite_mac).pack(side='left',padx=6)
        ttk.Checkbutton(opts,text='진동 seq 자동',variable=self.seq_auto).pack(side='left',padx=6)
        ttk.Checkbutton(opts,text='Jitter',variable=self.jitter_on).pack(side='left',padx=6)
        ttk.Checkbutton(opts,text='패킷 손실',variable=self.loss_on).pack(side='left',padx=6)
        ttk.Button(opts,text='연결 테스트',command=self.test_connection).pack(side='left',padx=12)
        data=ttk.LabelFrame(main,text='센서별 파일 1개 선택 / 체크 해제 시 송신하지 않음'); data.pack(fill='x',pady=5)
        for i,s in enumerate(('PIEZO','ADXL','초음파')):
            ttk.Checkbutton(data,text=s,variable=self.enabled[s]).grid(row=i,column=0,padx=4,pady=3,sticky='w')
            ttk.Entry(data,textvariable=self.files[s],width=48).grid(row=i,column=1,sticky='ew',padx=4)
            ttk.Button(data,text='파일 선택',command=lambda s=s:self.choose_file(s)).grid(row=i,column=2,padx=2)
            ttk.Label(data,text='Topic').grid(row=i,column=3,padx=(12,2))
            ttk.Entry(data,textvariable=self.topics[s],width=27).grid(row=i,column=4,padx=2)
            ttk.Label(data,text='주기(ms)').grid(row=i,column=5,padx=2)
            ttk.Entry(data,textvariable=self.period[s],width=8).grid(row=i,column=6,padx=2)
            if s!='초음파':
                ttk.Checkbutton(data,text='자동주기',variable=self.auto_period[s]).grid(row=i,column=7)
            ttk.Label(data,text='±Jitter').grid(row=i,column=8)
            ttk.Entry(data,textvariable=self.jitter[s],width=5).grid(row=i,column=9)
            ttk.Label(data,text='Loss%').grid(row=i,column=10)
            ttk.Entry(data,textvariable=self.loss[s],width=5).grid(row=i,column=11)
        adxl_csv_row=ttk.Frame(data)
        adxl_csv_row.grid(row=4,column=0,columnspan=12,sticky='ew',pady=(2,5))
        ttk.Label(adxl_csv_row,text='ADXL CSV 3개 (기존 JSON/JSONL 선택도 유지)').pack(side='left',padx=4)
        for axis in ('X','Y','Z'):
            ttk.Label(adxl_csv_row,text=axis).pack(side='left',padx=(8,2))
            ttk.Entry(adxl_csv_row,textvariable=self.adxl_csv_files[axis],width=17).pack(side='left',padx=2)
            ttk.Button(adxl_csv_row,text='CSV',command=lambda a=axis:self.choose_adxl_csv(a)).pack(side='left',padx=2)
        ttk.Button(adxl_csv_row,text='XYZ CSV 로드',command=self.load_adxl_csv_selected).pack(side='left',padx=6)
        ttk.Checkbutton(adxl_csv_row,text='손상된 HEX 앞부분 자동 복구(미리보기)',variable=self.recover_partial_adxl).pack(side='left',padx=4)
        data.columnconfigure(1,weight=1)
        extras=ttk.Frame(data); extras.grid(row=3,column=0,columnspan=12,sticky='w',pady=3)
        ttk.Label(extras,text='ADXL XYZ gap(ms)').pack(side='left',padx=4)
        ttk.Entry(extras,textvariable=self.xyz_gap,width=7).pack(side='left')
        ttk.Label(extras,text='초음파 tick 증가').pack(side='left',padx=(18,4))
        ttk.Entry(extras,textvariable=self.tick_step,width=7).pack(side='left')
        ttk.Label(extras,text='(기본 Jitter / Loss OFF, 재생 위치는 센서별로 유지)').pack(side='left',padx=15)
        playback=ttk.LabelFrame(main,text='파일 패킷 미리보기 / 선택한 위치에서 재생'); playback.pack(fill='x',pady=3)
        self.sensor=tk.StringVar(value='PIEZO')
        ttk.Label(playback,text='그래프 센서').pack(side='left',padx=4)
        cmb=ttk.Combobox(playback,textvariable=self.sensor,values=('PIEZO','ADXL','초음파'),width=10,state='readonly')
        cmb.pack(side='left'); cmb.bind('<<ComboboxSelected>>',lambda e:self.preview())
        ttk.Button(playback,text='◀ 이전 패킷',command=lambda:self.navigate(-1)).pack(side='left',padx=8)
        ttk.Button(playback,text='다음 패킷 ▶',command=lambda:self.navigate(1)).pack(side='left',padx=4)
        self.index=tk.StringVar(value='1')
        main_index_entry=ttk.Entry(playback,textvariable=self.index,width=8)
        main_index_entry.pack(side='left',padx=4)
        main_index_entry.bind('<Return>',lambda event:self.goto_index())
        ttk.Button(playback,text='번호 이동',command=self.goto_index).pack(side='left')
        self.position=tk.StringVar(value='파일을 선택하세요')
        ttk.Label(playback,textvariable=self.position).pack(side='left',padx=12)
        ttk.Button(playback,text='📈 RAW / FFT 그래프 열기',command=self.show_graph_window).pack(side='right',padx=8)
        actions=ttk.Frame(main); actions.pack(fill='x',pady=3)
        self.start_btn=ttk.Button(actions,text='▶ 선택 위치부터 전송 시작',command=self.start)
        self.start_btn.pack(side='left',padx=4)
        self.stop_btn=ttk.Button(actions,text='■ 중지',command=self.stop,state='disabled')
        self.stop_btn.pack(side='left',padx=4)
        ttk.Label(actions,textvariable=self.status).pack(side='left',padx=18)
        self.stats=tk.StringVar(value='Publish 0 / PUBACK 0 / Drop 0')
        ttk.Label(actions,textvariable=self.stats).pack(side='right',padx=6)
        # V024: main window is a compact MQTT/file/replay controller.
        # All plot-related options live in a reusable independent Toplevel window.
        self.graph_window=tk.Toplevel(self.root)
        self.graph_window.title('발생기 그래프 분석 · PIEZO / MEMS / 초음파')
        self.graph_window.geometry('1120x670')
        self.graph_window.minsize(860,540)
        self.graph_window.protocol('WM_DELETE_WINDOW',self._hide_graph_options)
        self.graph_window.withdraw()

        graph_toolbar=ttk.Frame(self.graph_window,padding=(8,6))
        graph_toolbar.pack(fill='x')
        ttk.Label(graph_toolbar,text='센서').pack(side='left',padx=4)
        graph_sensor_combo=ttk.Combobox(graph_toolbar,textvariable=self.sensor,
            values=('PIEZO','ADXL','초음파'),width=10,state='readonly')
        graph_sensor_combo.pack(side='left',padx=4)
        graph_sensor_combo.bind('<<ComboboxSelected>>',lambda event:self.preview())
        ttk.Button(graph_toolbar,text='◀ 이전 패킷',command=lambda:self.navigate(-1)).pack(side='left',padx=5)
        ttk.Button(graph_toolbar,text='다음 패킷 ▶',command=lambda:self.navigate(1)).pack(side='left',padx=5)
        ttk.Label(graph_toolbar,text='패킷 번호').pack(side='left',padx=(10,2))
        graph_index_entry=ttk.Entry(graph_toolbar,textvariable=self.index,width=8)
        graph_index_entry.pack(side='left',padx=3)
        graph_index_entry.bind('<Return>',lambda event:self.goto_index())
        ttk.Button(graph_toolbar,text='번호 이동',command=self.goto_index).pack(side='left',padx=5)
        ttk.Button(graph_toolbar,text='그래프 전체 보기',command=self.reset_graph_view).pack(side='left',padx=(8,5))
        ttk.Label(graph_toolbar,textvariable=self.position).pack(side='left',padx=10)

        # Scrollable option region keeps the FFT plots visible even with many controls.
        controls_outer=ttk.LabelFrame(self.graph_window,text='그래프 설정 (이 영역에서 마우스 휠로 스크롤)')
        controls_outer.pack(fill='x',padx=8,pady=(0,5))
        controls_canvas=tk.Canvas(controls_outer,height=320,highlightthickness=0)
        options_scroll=ttk.Scrollbar(controls_outer,orient='vertical',command=controls_canvas.yview)
        controls_canvas.configure(yscrollcommand=options_scroll.set)
        options_scroll.pack(side='right',fill='y')
        controls_canvas.pack(side='left',fill='both',expand=True)
        self.preview_options=ttk.Frame(controls_canvas)
        options_id=controls_canvas.create_window((0,0),window=self.preview_options,anchor='nw')
        self.preview_options.bind('<Configure>',lambda e:controls_canvas.configure(scrollregion=controls_canvas.bbox('all')))
        controls_canvas.bind('<Configure>',lambda e:controls_canvas.itemconfigure(options_id,width=e.width))
        def scroll_options(event):
            if event.delta:
                controls_canvas.yview_scroll(-int(event.delta/120) or (-1 if event.delta>0 else 1),'units')
            return 'break'
        controls_canvas.bind('<Enter>',lambda event:controls_canvas.bind_all('<MouseWheel>',scroll_options))
        controls_canvas.bind('<Leave>',lambda event:controls_canvas.unbind_all('<MouseWheel>'))
        self._plot_controls(self.preview_options)
        graphframe=ttk.Frame(self.graph_window)
        graphframe.pack(fill='both',expand=True,padx=8,pady=4)
        ttk.Label(graphframe,text='PyQtGraph 그래프 창의 내부 계산용 영역입니다.\n'
            '실제 설정과 그래프는 Qt 창에 함께 표시됩니다.\n'
            'X/Y/Z/gN/SRSS · FFT 평균 · 보정 설정을 변경하면 그래프가 갱신됩니다.',
            justify='center',anchor='center').pack(fill='both',expand=True,pady=10)
        self._pg_process=None
        self._pg_queue=None
        self._pg_actions=None
        self._pg_widgets={}
        self._pg_revision=0
        self._pg_ack=0
        self.fig=Figure(figsize=(11,5.5),dpi=95)
        self.ax_raw=self.fig.add_subplot(211); self.ax_fft=self.fig.add_subplot(212)
        self.fig.tight_layout(pad=2.2)
        # Matplotlib is retained as a headless calculation adapter ONLY.
        # The actual visible charts are rendered by PyQtGraph in a child process.
        self.canvas=FigureCanvasAgg(self.fig)
        self._cursor_mark=None; self._cursor_text=None; self._hover_mark=None; self._hover_text=None; self._pins={}; self._pin_artists={}; self._drag=None
        self._held_graph_buttons=set()
        self._wheel_during_right_press=False
        self._wheel_during_left_press=False
        self._right_press_event=None
        self._last_spectrum={}; self._full_axes={}
        logframe=ttk.LabelFrame(main,text='송신 로그')
        logframe.pack(fill='both',expand=True,pady=3)
        self.log=tk.Text(logframe,height=5,wrap='none'); self.log.pack(fill='both',expand=True)
        self.preview()

    def reset_graph_view(self):
        """Restore RAW and FFT to data limits without modifying signal or plot settings.

        Preserve selected sensor, unit, logarithmic modes, filters, FFT averaging,
        and MEMS calibration; release manually locked Y ranges.
        """
        self.graph_fixed.set(False)
        for prefix in ('raw', 'fft'):
            for bound in ('ymin', 'ymax'):
                getattr(self, f'{prefix}_{bound}').set('')
        self._drag = None
        self._pins = {}; self._pin_artists = {}
        self._held_graph_buttons.clear()
        self.preview()
        for ax in (self.ax_raw, self.ax_fft):
            ax.relim()
            ax.autoscale(enable=True, axis='both', tight=False)
        self._send_pg_graph(reset=True)

    def _hide_graph_options(self):
        self.graph_window.withdraw()
        if self._pg_queue is not None:
            self._pg_queue.put({'command':'hide'})

    def _start_pg_window(self):
        if self._pg_process is not None and self._pg_process.is_alive():
            return True
        import importlib.util
        if importlib.util.find_spec('pyqtgraph') is None or not any(
                importlib.util.find_spec(name) for name in ('PySide6','PyQt6','PyQt5','PySide2')):
            messagebox.showerror('PyQtGraph 설치 필요',
                'PyQtGraph 그래프 창을 열려면 다음 명령으로 설치하세요:\n'
                'python -m pip install -r requirements_pyqtgraph.txt')
            return False
        # Two independent, unbounded queues: no read-back of one's own messages,
        # no silent loss when a plot is waiting in the feeder thread.
        self._pg_queue=mp.Queue()
        self._pg_actions=mp.Queue()
        self._pg_revision=0
        self._pg_ack=0
        self._pg_process=mp.Process(target=run_pyqtgraph_window,
            args=(self._pg_queue,self._pg_actions),daemon=True)
        self._pg_process.start()
        return True

    def _pg_options_snapshot(self):
        """Describe the original controls without creating a second visible Tk window.
        All state and calculations stay in the parent; Qt displays and edits them.
        """
        self._pg_widgets={}
        def describe(widget):
            kind=widget.winfo_class()
            if kind in ('TFrame','Frame','TLabelframe','Labelframe'):
                return {'kind':'group','text':str(widget.cget('text')) if kind in ('TLabelframe','Labelframe') else '',
                        'children':children(widget)}
            if kind not in ('TLabel','TEntry','TCombobox','TCheckbutton','TButton'):
                return None
            key=str(widget)
            self._pg_widgets[key]=widget
            d={'kind':kind,'id':key}
            if kind in ('TLabel','TCheckbutton','TButton'):
                d['text']=str(widget.cget('text'))
            if kind in ('TLabel','TEntry','TCombobox','TCheckbutton'):
                varopt='variable' if kind=='TCheckbutton' else 'textvariable'
                var=str(widget.cget(varopt))
                d['var']=var
                if var:
                    value=self.root.getvar(var)
                    d['value']=bool(self.root.tk.getboolean(value)) if kind=='TCheckbutton' else str(value)
            if kind=='TCombobox':d['values']=list(widget.cget('values'))
            if kind=='TEntry':
                d['width']=int(widget.cget('width'))
                if widget is getattr(self,'srss_limit_entry',None):d['srss_input']=True
                d['readonly']=str(widget.cget('state'))=='readonly'
            if widget.winfo_manager()=='grid':d['grid_column']=int(widget.grid_info()['column'])
            return d
        def children(parent):
            rows=[]; packed=[]; grid={}
            for w in parent.winfo_children():
                d=describe(w)
                if d is None:continue
                if w.winfo_manager()=='grid':
                    gi=w.grid_info();grid.setdefault(int(gi['row']),[]).append((int(gi['column']),d))
                else:packed.append(d)
            for row in sorted(grid):rows.append([d for _,d in sorted(grid[row],key=lambda x:x[0])])
            if packed:rows.append(packed)
            return rows
        groups=[describe(w) for w in self.preview_sections.get(self.sensor.get(),())]
        return groups

    def _send_pg_graph(self,reset=False,show=False):
        if self._pg_process is None or not self._pg_process.is_alive():return
        sections=[]
        for name,ax in (('RAW',self.ax_raw),('FFT',self.ax_fft)):
            if name=='FFT' and self.sensor.get()=='초음파':continue
            curves=[]
            for line in ax.lines:
                if line.get_label().startswith('_'):continue
                xx=np.asarray(line.get_xdata(),dtype=float)
                yy=np.asarray(line.get_ydata(),dtype=float)
                count=min(len(xx),len(yy))
                if count:curves.append({'name':line.get_label(),'x':xx[:count].copy(),
                    'y':yy[:count].copy(),'color':line.get_color(),'dashed':line.get_linestyle()=='--'})
            sections.append({'name':name if self.sensor.get()!='초음파' else 'ULTRASONIC',
                'title':ax.get_title(),'xlabel':ax.get_xlabel(),'ylabel':ax.get_ylabel(),
                'xlog':ax.get_xscale()=='log','ylog':ax.get_yscale()=='log',
                'symlog':ax.get_yscale()=='symlog',
                'linthresh':float(getattr(ax.yaxis._scale,'linthresh',1)) if ax.get_yscale()=='symlog' else 1.0,
                'ylimit':(float(ax.get_ylim()[0]),float(ax.get_ylim()[1])) if self.graph_fixed.get() else None,
                'notice':' / '.join(t.get_text() for t in ax.texts if t.get_text()) if not curves else '',
                'curves':curves})
        sensor=self.sensor.get()
        self._pg_revision += 1
        # The selected packet is owned by the parent, not the editable entry.
        selected=self.current_index[sensor]+1 if self.templates.get(sensor) else 1
        message={'command':'plot','sections':sections,'reset':reset,'show':show,
                 'revision':self._pg_revision,'ack':self._pg_ack,'srss_device':self._srss_input_device,
                 'sensor':sensor,'index':str(selected),'position':self.position.get(),
                 'total':len(self.templates.get(sensor) or []),'options':self._pg_options_snapshot()}
        try:self._pg_queue.put(message)
        except Exception as exc:self._log('PyQtGraph 갱신 전송 오류: '+str(exc))

    def _poll_pg_actions(self):
        if self._pg_actions is None:return
        # Qt commands are processed on Tk's main thread, never from a worker.
        while True:
            try:action=self._pg_actions.get_nowait()
            except queue.Empty:break
            except (EOFError,OSError):break
            try:
                cmd=action.get('command')
                request=int(action.get('request',0))
                if request and request<=self._pg_ack:continue
                if request:self._pg_ack=request
                if cmd in ('navigate','goto','sensor'):
                    # A navigation request must operate on the currently selected
                    # sensor in the graph, and reply even when no file is loaded.
                    target=action.get('sensor',self.sensor.get())
                    if target not in self.current_index:target=self.sensor.get()
                    self.sensor.set(target)
                    data=self.templates.get(target) or []
                    if data and cmd=='navigate':
                        self.current_index[target]=(self.current_index[target]+int(action['delta']))%len(data)
                    elif data and cmd=='goto':
                        self.current_index[target]=max(0,min(len(data)-1,int(action['index'])-1))
                    self.preview()
                    self._log(f'[그래프] {target} 패킷 {self.current_index[target]+1}/{len(data)} (요청 {request})')
                elif cmd=='diagnostic':
                    records,title=self._fft_diagnostic_records()
                    self._pg_queue.put(dict(command='diagnostic',records=records,title=title))
                elif cmd=='srss_limit':self._edit_srss_limit()
                elif cmd=='reset':self.reset_graph_view()
                elif cmd=='close':continue
                elif cmd=='error':self._log('PyQtGraph 오류: '+str(action.get('message','')));continue
                elif cmd=='widget':
                    w=self._pg_widgets.get(action.get('id'))
                    if w is None:continue
                    kind=w.winfo_class()
                    if kind=='TButton':w.invoke()
                    elif kind=='TCheckbutton':
                        var=str(w.cget('variable'))
                        if bool(self.root.tk.getboolean(self.root.getvar(var)))!=bool(action['value']):w.invoke()
                    elif kind in ('TEntry','TCombobox'):
                        if kind=='TEntry' and str(w.cget('state'))=='readonly':continue
                        var=str(w.cget('textvariable'))
                        self.root.setvar(var,str(action['value']))
                        if var==str(self.mems_g_per_lsb):self._apply_mems_scale()
                        # A preview is already traced for many dropdowns; explicitly
                        # refresh other options and numeric edits as well.
                        elif var not in {str(self.unit),str(self.window),str(self.mems_raw_unit),
                            str(self.mems_fft_unit),str(self.mems_window),str(self.mems_win_n),str(self.fft_avg_count)}:
                            self.preview()
                    # Buttons such as Y-axis apply/rotation may only change labels
                    # or limits, so send the state even when they did not preview.
                    self._send_pg_graph()
            except Exception as exc:
                self._log('PyQtGraph 조작 오류: '+str(exc))
                self._send_pg_graph()

    def show_graph_window(self):
        if not self._start_pg_window():return
        # Keep Tk option widgets alive as the calculation/state model, but never
        # display that intermediate window. The Qt window owns all visible controls.
        self.graph_window.withdraw()
        self.preview()
        self._send_pg_graph(show=True)

    def _build_filter_controls(self, parent, row):
        """Shared, labelled filter rows; canonical values stay unchanged."""
        panel = ttk.LabelFrame(parent, text='필터 설정 (미리보기 전용)')
        panel.grid(row=row, column=0, columnspan=16, sticky='ew', padx=5, pady=4)
        filters = {}
        for r, (name, freq, extra) in enumerate((('HPF','10','4'), ('LPF','1600','4'), ('Notch','975','20'))):
            d = {'RAW':tk.BooleanVar(value=False), 'FFT':tk.BooleanVar(value=False),
                 'freq':tk.StringVar(value=freq), 'extra':tk.StringVar(value=extra)}
            filters[name] = d
            if name=='LPF':
                d['auto']=tk.BooleanVar(value=True)
                d['freq'].set('')
                ttk.Checkbutton(panel,text='자동 Fs/2',variable=d['auto'],command=self.preview).grid(row=r,column=4,sticky='w',padx=3)
            title = {'HPF':'HPF (하이패스)', 'LPF':'LPF (로우패스)', 'Notch':'Notch (노치)'}[name]
            ttk.Label(panel, text=title).grid(row=r, column=0, sticky='w', padx=(5,8), pady=3)
            ttk.Checkbutton(panel, text='RAW ON', variable=d['RAW'], command=self.preview).grid(row=r,column=1,sticky='w',padx=3)
            ttk.Checkbutton(panel, text='FFT ON', variable=d['FFT'], command=self.preview).grid(row=r,column=2,sticky='w',padx=3)
            ttk.Label(panel, text='중심 주파수 (Hz)' if name=='Notch' else '차단 주파수 (Hz)').grid(row=r,column=3,sticky='w',padx=(10,3))
            freq_entry=ttk.Entry(panel,textvariable=d['freq'],width=10)
            freq_entry.grid(row=r,column=5,sticky='w',padx=3)
            if name=='LPF':
                d['entry']=freq_entry
                freq_entry.configure(state='readonly')
            ttk.Label(panel,text='제거 폭 (Hz)' if name=='Notch' else '필터 차수').grid(row=r,column=6,sticky='w',padx=(10,3))
            extra_entry=ttk.Entry(panel,textvariable=d['extra'],width=7)
            extra_entry.grid(row=r,column=7,sticky='w',padx=3)
            for entry in (freq_entry,extra_entry):
                entry.bind('<Return>',lambda _e:self.preview())
                entry.bind('<FocusOut>',lambda _e:self.preview())
        ttk.Label(panel,text='LPF: 자동 Fs/2 = 전 대역 통과. 수동 차단 주파수는 0 < Hz < Fs/2.').grid(row=3,column=0,columnspan=8,sticky='w',padx=5,pady=(2,4))
        return filters

    def _plot_controls(self,parent):
        p=ttk.LabelFrame(parent,text='PIEZO 그래프 옵션 (수신기 방식 · 미리보기만 변경)')
        p.pack(fill='x',pady=3)
        self.unit=tk.StringVar(value='mV')
        self.piezo_vmax=tk.StringVar(value='3.890000')
        self.piezo_amax=tk.StringVar(value='4096')
        self.piezo_sensitivity=tk.StringVar(value='100')
        self.window=tk.StringVar(value='Blackman')
        self.auto_zero_value=tk.StringVar(value='자동 Count: —')
        self.piezo_formula=tk.StringVar(value='PIEZO 계산식: 파일을 선택하세요')
        units=ttk.Frame(p);units.grid(row=0,column=0,columnspan=16,sticky='w',padx=4,pady=3)
        ttk.Label(units,text='표시 단위').pack(side='left',padx=3)
        ttk.Combobox(units,textvariable=self.unit,values=('Count','mV','g RMS','mm/s RMS'),width=12,state='readonly').pack(side='left',padx=3)
        self.piezo_fft_formula_inline=tk.StringVar()
        ttk.Label(units,textvariable=self.piezo_fft_formula_inline,foreground='#365a78').pack(side='left',padx=(8,3))
        def refresh_piezo_fft_formula(*_args):
            texts={
                'Count':'FFT Count Peak = 2|rFFT(C×w)|/Σw',
                'mV':'FFT mV Peak = FFT Count Peak × Vmax × 1000/Amax',
                'g RMS':'FFT g RMS = FFT mV Peak/(감도×√2)',
                'mm/s RMS':'V RMS[mm/s] = FFT g RMS × 9806.65/(2πf), f>0 Hz',
            }
            self.piezo_fft_formula_inline.set(texts.get(self.unit.get(),''))
        self.unit.trace_add('write',refresh_piezo_fft_formula)
        refresh_piezo_fft_formula()
        for label,var,width in (('Vmax (V)',self.piezo_vmax,10),('Amax',self.piezo_amax,7),('감도 (mV/g)',self.piezo_sensitivity,7)):
            ttk.Label(units,text=label).pack(side='left',padx=(11,3))
            entry=ttk.Entry(units,textvariable=var,width=width);entry.pack(side='left')
            entry.bind('<Return>',lambda _e:self.preview());entry.bind('<FocusOut>',lambda _e:self.preview())
        ttk.Label(units,text='FFT 창').pack(side='left',padx=(11,3))
        ttk.Combobox(units,textvariable=self.window,values=('Blackman','Hann','Hamming','Rectangular'),width=12,state='readonly').pack(side='left')
        ttk.Label(p,textvariable=self.auto_zero_value).grid(row=1,column=0,columnspan=9,sticky='w',padx=5)
        self.filters=self._build_filter_controls(p, 2)
        ttk.Button(p,text='그래프 갱신',command=self.preview).grid(row=3,column=0,sticky='w',padx=5,pady=3)
        formula_label=ttk.Label(p,textvariable=self.piezo_formula,justify='left',wraplength=1260)
        formula_label.grid(row=4,column=0,columnspan=16,sticky='w',padx=5,pady=4)
        for var in (self.unit,self.window):
            var.trace_add('write',lambda *_:self.preview())

        # MEMS controls only affect preview. MQTT payload remains unmodified.
        m=ttk.LabelFrame(parent,text='MEMS(ADXL) 그래프 옵션 — 미리보기만 변경 / 송신 원본 유지')
        m.pack(fill='x',pady=3)
        self.mems_axis={axis:tk.BooleanVar(value=True) for axis in ('X','Y','Z','SRSS','gN')}
        self.mems_raw_unit=tk.StringVar(value='자동 (g/mg/µg)')
        self.mems_fft_unit=tk.StringVar(value='자동 (g RMS)')
        self.mems_g_per_lsb=tk.StringVar(value=f'{read_mems_g_per_lsb():.9g}')
        self.mems_rms_summary=tk.StringVar(value='축별 g RMS: CSV 로드 후 표시 (평균/DC 제외)')
        ttk.Label(m,text='표시 축').grid(row=0,column=0,padx=(5,3),sticky='w')
        for j,axis in enumerate(('X','Y','Z','gN','SRSS'),start=1):
            ttk.Checkbutton(m,text=axis,variable=self.mems_axis[axis],command=self.preview).grid(row=0,column=j,padx=5,sticky='w')
        self.srss_limit_input=tk.StringVar(value='4.5')
        self._srss_input_device=None
        ttk.Label(m,text='한계값 (mm/s RMS)').grid(row=0,column=6,padx=(10,3),sticky='w')
        self.srss_limit_entry=ttk.Entry(m,textvariable=self.srss_limit_input,width=8)
        self.srss_limit_entry.grid(row=0,column=7,padx=3,sticky='w')
        self.srss_limit_entry.bind('<Return>',lambda event:self._edit_srss_limit())
        ttk.Button(m,text='적용',command=self._edit_srss_limit).grid(row=0,column=8,padx=3,sticky='w')
        ttk.Label(m,text='RAW 단위').grid(row=1,column=4,padx=(16,3))
        ttk.Combobox(m,textvariable=self.mems_raw_unit,values=('자동 (g/mg/µg)','Count','g','mg','µg'),width=9,state='readonly').grid(row=1,column=5,padx=3)
        ttk.Label(m,text='FFT 단위').grid(row=1,column=6,padx=(14,3))
        ttk.Combobox(m,textvariable=self.mems_fft_unit,values=('자동 (g RMS)','Count Peak','g Peak','g RMS','mm/s RMS'),width=12,state='readonly').grid(row=1,column=7,padx=3)
        self.mems_fft_formula_inline=tk.StringVar()
        ttk.Label(m,textvariable=self.mems_fft_formula_inline,foreground='#365a78').grid(row=4,column=0,columnspan=10,sticky='w',padx=4,pady=2)
        def refresh_mems_fft_formula(*_args):
            texts={
                '자동 (g RMS)':'FFT g RMS = FFT Count Peak × (g/LSB)/√2',
                'Count Peak':'FFT Count Peak = 2|rFFT(C×w)|/Σw',
                'g Peak':'FFT g Peak = FFT Count Peak × (g/LSB)',
                'g RMS':'FFT g RMS = FFT g Peak/√2',
                'mm/s RMS':'V RMS[mm/s] = FFT g RMS × 9806.65/(2πf), f>0 Hz',
            }
            self.mems_fft_formula_inline.set(texts.get(self.mems_fft_unit.get(),''))
        self.mems_fft_unit.trace_add('write',refresh_mems_fft_formula)
        refresh_mems_fft_formula()
        ttk.Label(m,text='1 LSB = (g)').grid(row=1,column=8,padx=(14,3))
        sens=ttk.Entry(m,textvariable=self.mems_g_per_lsb,width=12)
        sens.grid(row=1,column=9,padx=3)
        for event in ('<Return>','<FocusOut>'):
            sens.bind(event,lambda _event:self._apply_mems_scale())
        ttk.Label(m,textvariable=self.mems_rms_summary).grid(row=2,column=0,columnspan=10,sticky='w',padx=5,pady=3)
        ttk.Label(m,text='ADXL343 Full Resolution 공칭 기본값: 1 LSB = 0.00390625 g (256 LSB/g) · 자동 표시: µg/mg/g · 원본 전송 변경 없음').grid(row=3,column=0,columnspan=10,sticky='w',padx=5,pady=(0,3))
        # FFT display calibration controls (measured frequency is used for S(f)).
        self.mems_correction_vars = {}
        correction = load_mems_correction()
        cf = ttk.LabelFrame(parent, text='MEMS FFT 주파수 / Sensitivity 보정 (미리보기만)')
        cf.pack(fill='x', pady=3)
        numeric_fields = [
            ('frequency_divisor', 'f 나눗수'), ('low_hz', '저주파 경계 Hz'),
            ('high_hz', '고주파 경계 Hz'), ('low_sensitivity', '저주파 S'),
            ('mid_intercept', '중간 상수'), ('mid_slope', '중간 기울기'),
            ('high_sensitivity', '고주파 S'),
        ]
        for key, label in numeric_fields:
            self.mems_correction_vars[key] = tk.StringVar(value=str(correction[key]))
        for key, label in (('frequency_enabled','주파수 보정'),('sensitivity_enabled','감도 보정')):
            self.mems_correction_vars[key] = tk.BooleanVar(value=correction[key])
        for col, (key, label) in enumerate(numeric_fields):
            ttk.Label(cf, text=label).grid(row=0, column=col, padx=3)
            ttk.Entry(cf, textvariable=self.mems_correction_vars[key], width=11).grid(row=1, column=col, padx=3)
        for col, key in enumerate(('frequency_enabled','sensitivity_enabled')):
            ttk.Checkbutton(cf, text=('주파수 보정 ON' if col==0 else 'Sensitivity 보정 ON'),
                            variable=self.mems_correction_vars[key]).grid(row=2,column=col*2,columnspan=2,sticky='w',padx=4)
        ttk.Button(cf,text='보정 적용/저장',command=self._apply_mems_correction).grid(row=2,column=5,columnspan=2,padx=4)
        self.mems_correction_formula=tk.StringVar(value=format_mems_correction_formula(correction))
        ttk.Label(cf,textvariable=self.mems_correction_formula,justify='left',wraplength=1150).grid(row=3,column=0,columnspan=7,sticky='w',padx=4,pady=4)
        def update_mems_correction_formula(*_):
            try:
                self.mems_correction_formula.set(format_mems_correction_formula(self._mems_correction_config()))
            except (ValueError,TypeError):
                self.mems_correction_formula.set('보정 수식: 입력값을 확인해 주세요. 유효한 값 입력 시 자동 표시됩니다.')
        for variable in self.mems_correction_vars.values():
            variable.trace_add('write',update_mems_correction_formula)
        self.mems_raw_unit.trace_add('write',lambda *_:self.preview())
        self.mems_fft_unit.trace_add('write',lambda *_:self.preview())
        # Detailed MEMS preview analysis: no change to transmitted HEX/JSON.
        detail=ttk.LabelFrame(parent,text='MEMS 미리보기 상세 분석 (송신값 불변)')
        detail.pack(fill='x',pady=3)
        self.mems_window=tk.StringVar(value='Blackman')
        self.mems_win_n=tk.StringVar(value='전체')
        self.mems_beta=tk.StringVar(value='8.6')
        ttk.Label(detail,text='FFT Window').grid(row=0,column=0,padx=4)
        ttk.Combobox(detail,textvariable=self.mems_window,values=FFT_WINDOWS,width=17,state='readonly').grid(row=0,column=1)
        ttk.Label(detail,text='Window N').grid(row=0,column=2)
        ttk.Combobox(detail,textvariable=self.mems_win_n,values=('전체','1024','2048','4096','8192'),width=8,state='readonly').grid(row=0,column=3)
        ttk.Label(detail,text='Kaiser β').grid(row=0,column=4)
        ttk.Entry(detail,textvariable=self.mems_beta,width=8).grid(row=0,column=5)
        self.mems_filters=self._build_filter_controls(detail, 1)
        ttk.Button(detail,text='설정 적용',command=self.preview).grid(row=0,column=6,padx=6)
        self.mems_formula=tk.StringVar(value='계산식: MEMS 선택 시 표시')
        ttk.Label(detail,textvariable=self.mems_formula,wraplength=1280,justify='left').grid(row=2,column=0,columnspan=15,sticky='w',padx=5,pady=4)
        self.rpm=tk.StringVar(value='1800')
        self.rot_axis=tk.StringVar(value='X')
        self.rot_type=tk.StringVar(value='미지정')
        self.rot_result=tk.StringVar(value='회전부품 역추정: FFT 데이터 로드 후 실행 (상대 점수/후보)')
        rotating=ttk.Frame(detail); rotating.grid(row=3,column=0,columnspan=15,sticky='w')
        ttk.Label(rotating,text='RPM').pack(side='left',padx=3)
        ttk.Entry(rotating,textvariable=self.rpm,width=8).pack(side='left')
        ttk.Label(rotating,text='유형').pack(side='left',padx=3)
        ttk.Combobox(rotating,textvariable=self.rot_type,values=('미지정','블레이드','기어','플런저','DC 정류자'),width=11,state='readonly').pack(side='left')
        ttk.Label(rotating,text='축').pack(side='left',padx=3)
        ttk.Combobox(rotating,textvariable=self.rot_axis,values=('X','Y','Z'),width=4,state='readonly').pack(side='left')
        ttk.Button(rotating,text='개수 추정',command=self._estimate_rotation).pack(side='left',padx=7)
        ttk.Label(detail,textvariable=self.rot_result,wraplength=1300,justify='left').grid(row=4,column=0,columnspan=15,sticky='w',padx=5)
        # File-backed FFT averaging; selected RAW packet and MQTT payload stay unchanged.
        avg=ttk.LabelFrame(parent,text='FFT Linear Averaging — 파일 패킷별 진폭 산술평균 (RAW/송신 원본 유지)')
        avg.pack(fill='x',pady=3)
        self.fft_avg_on=tk.BooleanVar(value=False)
        self.fft_avg_count=tk.StringVar(value='16')
        self.fft_avg_status=tk.StringVar(value='평균 OFF — 현재 패킷 FFT 표시')
        ttk.Checkbutton(avg,text='FFT 평균 적용',variable=self.fft_avg_on,command=self.preview).pack(side='left',padx=5)
        ttk.Label(avg,text='시작: 현재 선택 패킷 | 평균 개수').pack(side='left',padx=5)
        ttk.Combobox(avg,textvariable=self.fft_avg_count,values=('2','4','8','16','32','64','128','전체'),width=7,state='readonly').pack(side='left')
        ttk.Button(avg,text='평균 갱신',command=self.preview).pack(side='left',padx=7)
        ttk.Label(avg,textvariable=self.fft_avg_status).pack(side='left',padx=7)
        # Linear averaging is arithmetic mean of one-sided magnitude spectra,
        # NOT time-sample averaging or RMS/power averaging.
        self.fft_avg_formula=tk.StringVar(value='FFT Linear Averaging: A_avg(f_k) = (1/M) × Σ A_i(f_k), i=1..M  |  M=실제 유효 패킷 수  |  OFF: 선택 패킷 FFT')
        ttk.Label(avg,textvariable=self.fft_avg_formula,justify='left',wraplength=1280).pack(fill='x',padx=9,pady=(2,5))
        self.fft_avg_count.trace_add('write',lambda *_:self.preview())
        view=ttk.LabelFrame(parent,text='공통 그래프 조작 — 휠: 기존 · 좌클릭 유지+휠: X만 · 우클릭 유지+휠: Y만 (Y고정 유지)')
        view.pack(fill='x',pady=3)
        self.graph_fixed=tk.BooleanVar(value=False)
        self.raw_ymin=tk.StringVar(); self.raw_ymax=tk.StringVar()
        self.fft_ymin=tk.StringVar(); self.fft_ymax=tk.StringVar()
        ttk.Label(view,text='RAW Y').pack(side='left',padx=(6,3))
        ttk.Label(view,text='min').pack(side='left')
        ttk.Entry(view,textvariable=self.raw_ymin,width=9).pack(side='left',padx=(2,5))
        ttk.Label(view,text='max').pack(side='left')
        ttk.Entry(view,textvariable=self.raw_ymax,width=9).pack(side='left',padx=(2,12))
        ttk.Label(view,text='FFT Y').pack(side='left',padx=(0,3))
        ttk.Label(view,text='min').pack(side='left')
        ttk.Entry(view,textvariable=self.fft_ymin,width=9).pack(side='left',padx=(2,5))
        ttk.Label(view,text='max').pack(side='left')
        ttk.Entry(view,textvariable=self.fft_ymax,width=9).pack(side='left',padx=(2,10))
        ttk.Checkbutton(view,text='Y축 고정',variable=self.graph_fixed,command=self._toggle_y_fixed).pack(side='left',padx=3)
        ttk.Button(view,text='Y축 적용',command=self._apply_axis_limits).pack(side='left',padx=3)
        ttk.Button(view,text='Y축 자동',command=self._reset_axis_limits).pack(side='left',padx=3)
        ttk.Button(view,text='커서 해제',command=self._clear_pin).pack(side='left',padx=3)
        # FFT-only logarithmic axes: RAW signed waveform remains linear.
        self.raw_symlog=tk.BooleanVar(value=False)
        self.raw_linthresh=tk.StringVar(value='')
        self.fft_log_x=tk.BooleanVar(value=False)
        self.fft_log_y=tk.BooleanVar(value=False)
        ttk.Checkbutton(view,text='RAW Y 대칭로그',variable=self.raw_symlog,command=self.preview).pack(side='left',padx=(9,2))
        ttk.Label(view,text='기준값').pack(side='left')
        raw_entry=ttk.Entry(view,textvariable=self.raw_linthresh,width=8)
        raw_entry.pack(side='left',padx=(2,6))
        raw_entry.bind('<Return>',lambda _:self.preview())
        raw_entry.bind('<FocusOut>',lambda _:self.preview())
        ttk.Checkbutton(view,text='FFT X 로그',variable=self.fft_log_x,command=self.preview).pack(side='left',padx=(10,2))
        ttk.Checkbutton(view,text='FFT Y 로그',variable=self.fft_log_y,command=self.preview).pack(side='left',padx=2)

        self.cursor_status=tk.StringVar(value='클릭: 커서 고정 | 드래그: 기존 이동 | 휠: 기존 확대 | 좌클릭 유지+휠: X만 | 우클릭 유지+휠: Y만 | 우클릭: 메뉴')
        ttk.Label(parent,textvariable=self.cursor_status,anchor='w').pack(fill='x',padx=6)
        for variable in (self.mems_window,self.mems_win_n): variable.trace_add('write',lambda *_:self.preview())

        # Keep configuration state alive while showing only the selected sensor's menus.
        self.preview_sections={'PIEZO':(p,avg,view), 'ADXL':(m,cf,detail,avg,view), '초음파':()}
        self._visible_preview_sensor=None

    def _update_preview_sections(self):
        sensor=self.sensor.get()
        if sensor==self._visible_preview_sensor:
            return
        # All widgets stay instantiated; only their placement changes.
        # This avoids resetting user-selected filters, units, or FFT windows.
        for section in dict.fromkeys(self.preview_sections['PIEZO']+self.preview_sections['ADXL']):
            section.pack_forget()
        for section in self.preview_sections.get(sensor, ()):
            section.pack(fill='x',pady=3)
        self._visible_preview_sensor=sensor


    def choose_file(self,s):
        filt=((f'{s} CSV/JSON','*.csv *.json *.jsonl *.txt'),('모든 파일','*.*')) if s in ('PIEZO','초음파') else ((s+' JSON','*.json *.jsonl *.txt'),('모든 파일','*.*'))
        path=filedialog.askopenfilename(title=f'{s} 파일 선택',filetypes=filt)
        if not path:return
        try:
            items=self._load(s,path)
            self.templates[s]=items; self.files[s].set(path); self.current_index[s]=0
            if s=='ADXL':self.adxl_input_mode='json'
            self.sensor.set(s); self.preview()
            self._log(f'[{s}] {len(items)}개 패킷 로드: {path}')
        except Exception as exc:
            messagebox.showerror('파일 오류',str(exc))

    def choose_adxl_csv(self,axis):
        path=filedialog.askopenfilename(title=f'ADXL-{axis} CSV 선택',filetypes=(('CSV','*.csv'),('모든 파일','*.*')))
        if not path:return
        self.adxl_csv_files[axis].set(path)
        if all(self.adxl_csv_files[a].get().strip() for a in ('X','Y','Z')):
            self.load_adxl_csv_selected()

    def load_adxl_csv_selected(self):
        try:
            paths={a:self.adxl_csv_files[a].get() for a in ('X','Y','Z')}
            items, report=load_adxl_csv_xyz(paths, return_report=True, recover_partial=self.recover_partial_adxl.get())
            self.templates['ADXL']=items
            self.current_index['ADXL']=0
            self.adxl_input_mode='csv_xyz'
            self.files['ADXL'].set(f'XYZ CSV: {len(items)}세트')
            self.enabled['ADXL'].set(True)
            self.sensor.set('ADXL')
            self.preview()
            self._log(f"[ADXL seq 정렬] {len(items)}세트, 불완전 {report['partial']}세트 | 원본 행={report['rows']} | 유효 축={report['axis_packets']} | 부분복구={report['recovered']}")
            for warning in report['warnings'][:100]: self._log('[복구/제외] '+warning)
            if len(report['warnings'])>100: self._log(f"오류/제외 내역 {len(report['warnings'])-100}건 추가 (화면은 100건만 표시)")
        except Exception as exc:
            messagebox.showerror('ADXL XYZ CSV 오류',str(exc))

    def _load(self,s,path):
        if s=='PIEZO':return load_piezo_templates(path)
        if s=='초음파':return load_ultrasonic_templates(path)
        axes=load_adxl_templates(path)
        # Reconstruct ADXL record sets as XYZ, keep original source-axis order.
        return [dict(zip(('X','Y','Z'),t)) for t in zip(axes['X'],axes['Y'],axes['Z'])]

    def navigate(self,delta):
        s=self.sensor.get(); data=self.templates.get(s) or []
        if not data:return
        self.current_index[s]=(self.current_index[s]+delta)%len(data)
        self.preview()

    def goto_index(self):
        s=self.sensor.get(); data=self.templates.get(s) or []
        if not data:return
        try: ix=int(self.index.get())-1
        except ValueError: return
        self.current_index[s]=max(0,min(len(data)-1,ix))
        self.preview()

    def _graph_scroll(self,event):
        # Default wheel: preserve original X/Y zoom (X-only when Y is fixed).
        # Hold left + wheel: X only. Hold right + wheel: Y only.
        # Fixed Y is a deliberate lock, so right + wheel leaves Y unchanged.
        if event.inaxes not in (self.ax_raw,self.ax_fft) or event.xdata is None:
            return
        ax=event.inaxes
        factor=.82 if event.button=='up' else 1.22
        held=self._held_graph_buttons
        # Zoom and pan are distinct gestures. A mouse motion after scrolling
        # must never reuse the drag origin/axis limits saved before zooming.
        if 1 in held:
            self._wheel_during_left_press=True
            self._drag=None
        if 3 in held:
            self._wheel_during_right_press=True
            if self.graph_fixed.get() or event.ydata is None:
                return
            axes='y'
        elif 1 in held:
            axes='x'
        else:
            axes='x' if self.graph_fixed.get() else 'xy'
        def zoom_bounds(bounds, cursor, scale):
            lo,hi=bounds
            if scale=='log':
                if min(lo,hi,cursor)<=0:return bounds
                center=np.log10(cursor)
                low,high=np.log10(lo),np.log10(hi)
                return (10**(center+(low-center)*factor),
                        10**(center+(high-center)*factor))
            if scale=='symlog':
                transform=ax.yaxis._scale.get_transform()
                inv=transform.inverted()
                v0,v1,vc=transform.transform([lo,hi,cursor])
                return tuple(inv.transform([vc+(v0-vc)*factor,vc+(v1-vc)*factor]))
            return (cursor+(lo-cursor)*factor,cursor+(hi-cursor)*factor)
        if 'x' in axes:
            ax.set_xlim(*zoom_bounds(ax.get_xlim(),event.xdata,ax.get_xscale()))
        if 'y' in axes and event.ydata is not None:
            ax.set_ylim(*zoom_bounds(ax.get_ylim(),event.ydata,ax.get_yscale()))
        self.canvas.draw_idle()

    def _nearest_curve(self,event):
        if event.inaxes not in (self.ax_raw,self.ax_fft) or event.xdata is None:return None
        ax=event.inaxes
        best=None
        for line in ax.lines:
            if line.get_label().startswith('_'): continue
            xs=np.asarray(line.get_xdata()); ys=np.asarray(line.get_ydata())
            if not len(xs): continue
            j=int(np.clip(np.searchsorted(xs,event.xdata),0,len(xs)-1))
            for k in (j,max(0,j-1)):
                if not (np.isfinite(xs[k]) and np.isfinite(ys[k])):continue
                px,py=ax.transData.transform((xs[k],ys[k]))
                d=(px-event.x)**2+(py-event.y)**2
                if best is None or d<best[0]:best=(d,ax,line.get_label(),float(xs[k]),float(ys[k]))
        return best if best and best[0]<18**2 else None

    def _draw_pin(self,pin):
        ax,axis,x,y=pin
        kind='raw' if ax is self.ax_raw else 'fft'
        for artist in self._pin_artists.pop(kind,()):
            try:artist.remove()
            except (ValueError,AttributeError):pass
        mark=ax.plot([x],[y],marker='o',color='darkorange',ms=6,zorder=20,label='_pin')[0]
        text=ax.annotate(f'{axis} | X={x:.6g} / Y={y:.6g}',(x,y),xytext=(8,13),textcoords='offset points',
                         bbox=dict(boxstyle='round',fc='white',alpha=.85),fontsize=9)
        self._pin_artists[kind]=(mark,text)
        self.cursor_status.set(f'고정: {kind.upper()} {axis} / X={x:.6g} / Y={y:.6g}')

    def _clear_hover(self, redraw=True):
        changed=self._hover_mark is not None or self._hover_text is not None
        for artist in (self._hover_mark,self._hover_text):
            if artist is not None:
                try: artist.remove()
                except (ValueError,AttributeError): pass
        self._hover_mark=self._hover_text=None
        if changed and redraw:self.canvas.draw_idle()

    def _show_hover(self,hit):
        _,ax,axis,x,y=hit
        self._clear_hover(redraw=False)
        self._hover_mark=ax.plot([x],[y],marker='o',color='#2688d6',ms=4,zorder=19,label='_hover')[0]
        self._hover_text=ax.annotate(f'{axis} | X={x:.6g} / Y={y:.6g}',(x,y),xytext=(8,11),textcoords='offset points',
                                      bbox=dict(boxstyle='round',fc='white',alpha=.85),fontsize=9,zorder=19)
        self.cursor_status.set(f'현재 위치: {axis} / X={x:.6g} / Y={y:.6g} (현재 표시 단위)')
        self.canvas.draw_idle()

    def _context_xzoom(self,ax,center,factor):
        x0,x1=ax.get_xlim()
        center=(x0+x1)/2 if center is None else center
        ax.set_xlim(center+(x0-center)*factor,center+(x1-center)*factor)
        self.canvas.draw_idle()

    def _context_full_view(self,ax):
        ax.relim();ax.autoscale_view(scalex=True,scaley=not self.graph_fixed.get())
        if self.graph_fixed.get():
            kind='raw' if ax is self.ax_raw else 'fft'
            lo=getattr(self,f'{kind}_ymin').get().strip()
            hi=getattr(self,f'{kind}_ymax').get().strip()
            if lo and hi:
                try:ax.set_ylim(float(lo),float(hi))
                except ValueError:pass
        self.canvas.draw_idle()

    def _save_preview_image(self):
        filename=filedialog.asksaveasfilename(title='미리보기 그래프 저장',defaultextension='.png',
            filetypes=[('PNG 이미지','*.png'),('PDF 문서','*.pdf'),('SVG 벡터','*.svg')])
        if filename:
            try:self.fig.savefig(filename,dpi=160)
            except Exception as exc:messagebox.showerror('이미지 저장 오류',str(exc))

    def _graph_context_menu(self,event):
        ax=event.inaxes
        if ax not in (self.ax_raw,self.ax_fft):return
        menu=tk.Menu(self.root,tearoff=0)
        name='RAW' if ax is self.ax_raw else 'FFT'
        menu.add_command(label=f'{name} X축 확대',command=lambda:self._context_xzoom(ax,event.xdata,.75))
        menu.add_command(label=f'{name} X축 축소',command=lambda:self._context_xzoom(ax,event.xdata,1.35))
        menu.add_command(label=f'{name} 전체 보기',command=lambda:self._context_full_view(ax))
        menu.add_separator()
        if self.graph_fixed.get():
            menu.add_command(label='Y축 고정 해제 / 자동',command=self._reset_axis_limits)
        else:
            menu.add_command(label='현재 Y축 범위 고정',command=lambda:(self.graph_fixed.set(True),self._toggle_y_fixed()))
        menu.add_command(label='고정 커서 해제',command=lambda:self._clear_pin('raw' if ax is self.ax_raw else 'fft'))
        menu.add_separator()
        menu.add_command(label='전체 미리보기 이미지 저장...',command=self._save_preview_image)
        gui=getattr(event,'guiEvent',None)
        px=getattr(gui,'x_root',None)
        py=getattr(gui,'y_root',None)
        if px is None:px=self.canvas.get_tk_widget().winfo_pointerx()
        if py is None:py=self.canvas.get_tk_widget().winfo_pointery()
        try:menu.tk_popup(int(px),int(py))
        finally:menu.grab_release()

    def _graph_click(self,event):
        if event.inaxes not in (self.ax_raw,self.ax_fft):return
        if event.button==3:
            # Open context menu on RELEASE, not press, so right-hold+wheel
            # can exclusively zoom Y without spawning the original menu.
            self._held_graph_buttons.add(3)
            self._wheel_during_right_press=False
            self._right_press_event=event
            return
        if event.button!=1:return
        self._held_graph_buttons.add(1)
        self._wheel_during_left_press=False
        hit=self._nearest_curve(event)
        if hit:
            _,ax,axis,x,y=hit
            self._pins['raw' if ax is self.ax_raw else 'fft']=(axis,x)
            self._clear_hover(redraw=False)
            self._draw_pin((ax,axis,x,y));self.canvas.draw_idle()
            self._drag=None
        else:
            # 빈 위치 클릭 시 고정 해제. 드래그 이동은 Y축 고정 여부에 따라 변경.
            self._clear_pin('raw' if event.inaxes is self.ax_raw else 'fft')
            if event.xdata is not None and event.ydata is not None:
                self._drag=(event.inaxes,event.x,event.y,event.inaxes.get_xlim(),event.inaxes.get_ylim())

    def _graph_motion(self,event):
        if self._wheel_during_left_press and 1 in self._held_graph_buttons:
            # Keep the zoomed viewport stable while left mouse stays pressed.
            return
        if self._drag is not None:
            if event.inaxes is not self._drag[0]:return
            ax,x,y,xlim,ylim=self._drag
            # Drag graph in the same direction as the mouse. When Y is
            # explicitly locked, preserve its limits and pan horizontally only.
            width=max(1.0, float(ax.bbox.width))
            height=max(1.0, float(ax.bbox.height))
            dx=(event.x-x)/width*(xlim[1]-xlim[0])
            ax.set_xlim(xlim[0]-dx,xlim[1]-dx)
            if not self.graph_fixed.get():
                # event.y는 화면 아래로 이동하면 감소한다.
                # 그래프가 마우스와 같은 방향으로 따라가려면 Y축 범위를 반대로 이동한다.
                dy=(event.y-y)/height*(ylim[1]-ylim[0])
                ax.set_ylim(ylim[0]-dy, ylim[1]-dy)
            self.canvas.draw_idle()
            return
        if ('raw' if event.inaxes is self.ax_raw else 'fft' if event.inaxes is self.ax_fft else None) in self._pins:
            self._clear_hover()
            return
        hit=self._nearest_curve(event)
        if hit:self._show_hover(hit)
        else:
            self._clear_hover()
            if event.inaxes not in (self.ax_raw,self.ax_fft):
                self.cursor_status.set('곡선 위 이동: X/Y 표시 | 클릭: 고정 | 빈 곳 클릭: 해제 | 우클릭: 메뉴')

    def _graph_release(self,event):
        self._drag=None
        if event.button==1:
            self._held_graph_buttons.discard(1)
            self._wheel_during_left_press=False
        elif event.button==3:
            self._held_graph_buttons.discard(3)
            right_event=self._right_press_event
            show_menu=(right_event is not None and not self._wheel_during_right_press)
            self._right_press_event=None
            self._wheel_during_right_press=False
            if show_menu:
                self._graph_context_menu(right_event)

    def _clear_pin(self,kind=None):
        for key in list(self._pins) if kind is None else [kind]:
            self._pins.pop(key,None)
            for artist in self._pin_artists.pop(key,()):
                try:artist.remove()
                except (ValueError,AttributeError):pass
        self.cursor_status.set('고정 커서 해제 | 곡선 위로 마우스를 이동하면 값이 표시됩니다')
        self.canvas.draw_idle()

    def _toggle_y_fixed(self):
        # Freeze current Y ranges when enabled. X remains free.
        if self.graph_fixed.get():
            for prefix,ax in (('raw',self.ax_raw),('fft',self.ax_fft)):
                ymin,ymax=ax.get_ylim()
                getattr(self,f'{prefix}_ymin').set(f'{ymin:.9g}')
                getattr(self,f'{prefix}_ymax').set(f'{ymax:.9g}')
        self.preview()

    def _apply_axis_limits(self):
        try:
            limits=[]
            for prefix,ax in (('raw',self.ax_raw),('fft',self.ax_fft)):
                values=[getattr(self,f'{prefix}_{k}').get().strip() for k in ('ymin','ymax')]
                if not any(values):
                    limits.append(None)
                    continue
                if not all(values):raise ValueError(prefix+' Y min/max를 모두 입력하세요.')
                y0,y1=map(float,values)
                if not all(map(math.isfinite,(y0,y1))) or y0>=y1:
                    raise ValueError(prefix+' Y 범위 오류: min < max')
                limits.append((y0,y1))
            if not any(v is not None for v in limits):
                raise ValueError('RAW 또는 FFT의 Y min/max를 입력하세요.')
            self.graph_fixed.set(True)
            for ax,value in zip((self.ax_raw,self.ax_fft),limits):
                if value is not None:ax.set_ylim(*value)
            self.canvas.draw_idle()
        except Exception as exc:messagebox.showerror('Y축 적용 오류',str(exc))

    def _reset_axis_limits(self):
        self.graph_fixed.set(False)
        for prefix in ('raw','fft'):
            for k in ('ymin','ymax'):getattr(self,f'{prefix}_{k}').set('')
        self.preview()

    def _estimate_rotation(self):
        try:
            axis=self.rot_axis.get(); data=self._last_spectrum.get(axis)
            if not data:
                self.rot_result.set(f'{axis} 축 FFT 데이터가 없습니다. 축 표시를 확인하세요.');return
            f,mag=data; rpm=float(self.rpm.get())
            if rpm<=0:raise ValueError('RPM은 양수여야 합니다')
            candidates=rotating_candidates(f,mag,rpm)
            if not candidates:self.rot_result.set('일치하는 회전부품 개수 후보가 없습니다.');return
            self.rot_result.set(f'[{self.rot_type.get()} / {axis} / {rpm:g} RPM] 후보: '+
                ' | '.join(f'{n}개 ({hz:.2f}Hz, 일치 {len(matches)}개, 점수 {score:.3f})' for score,n,hz,matches in candidates)+
                ' ※ 주파수 차수 일치도에 의한 후보이며 실제 부품 수 확정 아님')
        except Exception as exc:messagebox.showerror('회전부품 추정',str(exc))

    def copy_zero(self):
        # Kept for backward compatibility; raw preview always uses packet mean.
        self.preview()

    def _filter_args(self,kind):
        h=self.filters['HPF'];l=self.filters['LPF'];n=self.filters['Notch']
        return dict(hpf=h[kind].get(), hp_cutoff=float(h['freq'].get()),hp_order=int(h['extra'].get()),
                    lpf=l[kind].get(),lp_cutoff=None if l['auto'].get() or not l[kind].get() else float(l['freq'].get()),lp_order=int(l['extra'].get()),
                    notch=n[kind].get(),notch_center=float(n['freq'].get()),notch_width=float(n['extra'].get()))

    def _convert_fft_result(self, sensor, freq, amp):
        """Convert one packet to its final physical FFT unit before averaging.
        Automatic MEMS display units use g RMS internally; scale only for display.
        """
        if sensor == 'PIEZO':
            unit = self.unit.get()
            if unit == 'Count': return freq, amp
            vmax, amax, sens = (float(v.get()) for v in (self.piezo_vmax,self.piezo_amax,self.piezo_sensitivity))
            if not all(np.isfinite(v) and v>0 for v in (vmax,amax,sens)):
                raise ValueError('PIEZO 환산 설정 오류')
            values = amp * (1000.0*vmax/amax)
            if unit == 'mV': return freq, values
            values = values/sens/np.sqrt(2)
        else:
            unit = self.mems_fft_unit.get()
            scale = float(self.mems_g_per_lsb.get())
            if not np.isfinite(scale) or not 0 < scale <= 10: raise ValueError('MEMS 환산 설정 오류')
            values = amp if unit=='Count Peak' else amp*scale
            if unit not in ('Count Peak','g Peak'): values=values/np.sqrt(2)
            freq,values=corrected_mems_fft(freq,values,self._mems_correction_config(),apply_sensitivity=unit!='Count Peak')
        if unit == 'mm/s RMS':
            velocity=np.zeros_like(values,dtype=float); mask=freq>0
            velocity[mask]=values[mask]*9806.65/(2*np.pi*freq[mask]); values=velocity
        return freq,values

    def _average_fft_results(self, sensor, packets, selected_index):
        """Average fully converted FFT amplitudes by frequency bin.

        Never average RAW sample values, alter loaded templates, or publish averages.
        Incompatible Fs/N are skipped, and ADXL axes are processed independently.
        """
        if not self.fft_avg_on.get():
            self.fft_avg_status.set('평균 OFF — 현재 패킷 FFT 표시')
            self.fft_avg_formula.set('FFT Linear Averaging: A_avg(f_k) = (1/M) × Σ A_i(f_k) (i=1..M) | 현재 OFF — 선택 패킷 FFT 표시')
            return {}
        choice=self.fft_avg_count.get().strip()
        amount=(len(packets)-selected_index) if choice=='전체' else int(choice)
        if amount<1:
            raise ValueError('FFT 평균 개수는 1 이상이어야 합니다.')
        chosen=packets[selected_index:min(len(packets),selected_index+amount)]
        axes=('PIEZO',) if sensor=='PIEZO' else ('X','Y','Z')
        result={}
        messages=[]
        for axis in axes:
            reference_f=None
            reference_shape=None
            summed=None
            summed_counts=None
            display_f=None
            accepted=0
            skipped=0
            for packet in chosen:
                obj=packet if sensor=='PIEZO' else packet.get(axis)
                if obj is None:
                    skipped+=1
                    continue
                try:
                    raw,fs,_=decode_packet(sensor,obj)
                    if len(raw)<2: raise ValueError('샘플 부족')
                    if sensor=='PIEZO':
                        freq,amp=fft_peak(spectral_filter(raw-np.mean(raw),fs,**self._filter_args('FFT')),fs,self.window.get())
                    else:
                        source=spectral_filter(raw,fs,**self._mems_filter_args('FFT'))
                        n_text=self.mems_win_n.get()
                        window_n=None if n_text=='전체' else int(n_text)
                        if window_n is not None and window_n>len(raw): raise ValueError('FFT Window N이 패킷 샘플 수보다 큼')
                        freq,amp=fft_peak(source,fs,self.mems_window.get(),window_n,float(self.mems_beta.get()))
                    converted_f, converted_amp = self._convert_fft_result(sensor,freq,amp)
                    if reference_f is None:
                        reference_f=freq
                        reference_shape=(fs,len(raw))
                        summed=np.zeros_like(converted_amp)
                        summed_counts=np.zeros_like(amp)
                        display_f=converted_f
                    if (reference_shape!=(fs,len(raw)) or len(freq)!=len(reference_f)
                            or not np.array_equal(freq,reference_f)
                            or not np.array_equal(converted_f,display_f)):
                        skipped+=1
                        continue
                    summed+=converted_amp
                    summed_counts+=amp
                    accepted+=1
                except (ValueError,TypeError,OverflowError):
                    skipped+=1
            if accepted:
                result[axis]=(reference_f,summed_counts/accepted,accepted,display_f,summed/accepted)
            messages.append(f'{axis}: {accepted}/{len(chosen)}회'+(f' (제외 {skipped})' if skipped else ''))
        trailing='' if len(chosen)==amount else f' | 파일 끝: 요청 {amount}회 중 {len(chosen)}회 검색'
        self.fft_avg_status.set(' | '.join(messages)+trailing)
        self.fft_avg_formula.set('Linear Averaging: A_avg(f_k) = [A₁(f_k)+...+Aₘ(f_k)]/M | M=실제 유효 패킷 수 | ' + ' / '.join(f'{axis} M={item[2]}' for axis,item in result.items()) + ' | 각 패킷 필터·FFT·단위 환산·보정 완료 후 평균 (RAW/MQTT 변경 없음)')
        return result

    def _plot_ultrasonic(self, packet):
        """H payload is already a spectrum: plot 320 source values as-is."""
        x_khz, values, start_khz, step_hz = decode_ultrasonic_spectrum(packet)
        self.ax_raw.plot(x_khz, values, color='#9467bd', lw=1.0, label='ULTRASONIC H')
        self.ax_raw.set_xlabel('Frequency (kHz)')
        self.ax_raw.set_ylabel('H spectrum (original value)')
        self.ax_raw.grid(alpha=.3)
        self.ax_raw.set_xlim(float(x_khz[0]), float(x_khz[-1]))
        peak_idx=int(np.argmax(values))
        self.ax_raw.set_title(
            f'ULTRASONIC H | 320 frequency bins | start {start_khz} kHz / step {step_hz} Hz '
            f'| peak {x_khz[peak_idx]:.3f} kHz : {values[peak_idx]:.6g}', fontsize=10)
        self.ax_raw.legend(loc='upper right')

    def _plot_piezo(self,packet, averaged=None):
        raw,fs,_=decode_packet('PIEZO',packet)
        if len(raw)<2: raise ValueError('PIEZO 샘플이 부족합니다')
        vmax=float(self.piezo_vmax.get());amax=float(self.piezo_amax.get())
        sens=float(self.piezo_sensitivity.get())
        if not all(np.isfinite(x) and x>0 for x in (vmax,amax,sens)):
            raise ValueError('Vmax/Amax/센서 감도는 유효한 양수여야 합니다')
        K=1000.0*vmax/amax   # mV/Count; receiver's test conversion
        mean=float(np.mean(raw))
        centered=raw-mean
        self.auto_zero_value.set(f'원본 Count 평균: {mean:,.3f} (원본/송신 변경 없음)')
        unit=self.unit.get()
        # PIEZO RAW always centered for display only; FFT is calculated from unmodified original.
        filtered_raw=spectral_filter(centered,fs,**self._filter_args('RAW'))
        fft_src=spectral_filter(centered,fs,**self._filter_args('FFT'))
        freq,peak_counts=fft_peak(fft_src,fs,self.window.get())
        if averaged and 'PIEZO' in averaged:
            freq,peak_counts,_,_,_=averaged['PIEZO']
        if unit=='Count':
            raw_data=filtered_raw; raw_ylabel='Count (centered)'
            fft_data=peak_counts;fft_ylabel='Count Peak'
            details='RAW C_display[n]=C[n]−mean(C)  |  FFT Count Peak=2|rFFT((C−mean(C))×w)[k]|/Σw (DC/Nyquist 예외)'
        elif unit=='mV':
            raw_data=filtered_raw*K;raw_ylabel='mV (AC)'
            fft_data=peak_counts*K;fft_ylabel='mV Peak'
            details='V_abs[n]=(C[n]+Amax/2)×K [mV]  |  RAW=(C−mean(C))×K  |  FFT mV Peak=FFT Count Peak×K'
        elif unit=='g RMS':
            raw_data=filtered_raw*K/sens;raw_ylabel='g (instantaneous AC)'
            fft_data=peak_counts*K/sens/np.sqrt(2);fft_ylabel='g RMS / bin'
            total_grms=float(np.std(raw*K/sens))
            details=(f'g[n]=V_abs[n]/{sens:g}; g_mean=mean(g); 전체 g RMS=√mean((g−g_mean)²)={total_grms:.7g} g  |  '
                     'RAW(g)=(C−mean(C))×K/감도  |  FFT(g RMS)=FFT Count Peak×K/(감도×√2)')
        elif unit=='mm/s RMS':
            raw_data=filtered_raw*K/sens;raw_ylabel='g (instantaneous AC)'
            fft_grms=peak_counts*K/sens/np.sqrt(2)
            fft_data=np.zeros_like(fft_grms)
            mask=freq>0
            fft_data[mask]=fft_grms[mask]*9806.65/(2*np.pi*freq[mask]);fft_ylabel='mm/s RMS / bin'
            details=('RAW(g)=(C−mean(C))×K/감도  |  FFT(g RMS)=FFT Count Peak×K/(감도×√2)  |  '
                     'FFT velocity RMS[k]=FFT g RMS[k]×9806.65/(2πf[k]) [mm/s], f>0Hz')
        else: raise ValueError('알 수 없는 PIEZO 단위: '+unit)
        if averaged and 'PIEZO' in averaged:
            _,_,_,freq,fft_data=averaged['PIEZO']
        x=np.arange(len(raw))/fs*1000
        self.ax_raw.plot(x,raw_data,label='PIEZO',color='#d62728',linewidth=.85)
        self.ax_raw.set_ylabel(raw_ylabel);self.ax_raw.set_xlabel('Time (ms)');self.ax_raw.grid(alpha=.25)
        self.ax_raw.set_title(f'PIEZO RAW  Fs={fs:g} Hz / N={len(raw)} / 표시 평균제거',fontsize=10)
        self.ax_fft.plot(freq/1000,fft_data,label='PIEZO',color='#d62728',linewidth=.9)
        self.ax_fft.set_ylabel(fft_ylabel);self.ax_fft.set_xlabel('Frequency (kHz)');self.ax_fft.grid(alpha=.25)
        if len(fft_data)>1:
            ix=1+int(np.argmax(fft_data[1:]))
            self.ax_fft.set_title(f'FFT peak {freq[ix]:.2f} Hz / {fft_data[ix]:.6g} {fft_ylabel}',fontsize=10)
        self.piezo_formula.set(
            f'[선택 단위 계산식: {unit}] Vmax={vmax:.6f} V | Amax={amax:g} | 감도={sens:g} mV/g | '
            f'K=1000×{vmax:g}/{amax:g}={K:.9f} mV/Count\n'
            +details+'\n'
            +('미리보기 필터: RAW/FFT 독립 적용 (선택 시 실제 표시 신호 기준 계산)\n' if any(v[k].get() for v in self.filters.values() for k in ('RAW','FFT')) else '')
            +'※ signed int16 수신 원본 유지. 12bit V_abs는 검증된 절대전압 아님. FFT RMS는 주파수별 값이며 전체 RMS와 다를 수 있음.')

    def _mems_filter_args(self,kind):
        h,l,n=(self.mems_filters[key] for key in ('HPF','LPF','Notch'))
        return dict(hpf=h[kind].get(),hp_cutoff=float(h['freq'].get()),hp_order=int(h['extra'].get()),
                    lpf=l[kind].get(),lp_cutoff=None if l['auto'].get() or not l[kind].get() else float(l['freq'].get()),lp_order=int(l['extra'].get()),
                    notch=n[kind].get(),notch_center=float(n['freq'].get()),notch_width=float(n['extra'].get()))

    def _apply_mems_scale(self):
        try:
            value=float(self.mems_g_per_lsb.get())
            save_mems_g_per_lsb(value)
            self.preview()
        except (ValueError, OSError) as exc:
            messagebox.showerror('MEMS 1 LSB 환산 설정', str(exc))

    def _mems_correction_config(self):
        return validate_mems_correction({
            key: variable.get() for key, variable in self.mems_correction_vars.items()
        })

    def _apply_mems_correction(self):
        try:
            save_mems_correction(self._mems_correction_config())
            self.preview()
        except (ValueError, OSError) as exc:
            messagebox.showerror('MEMS FFT 보정 설정', str(exc))

    def _srss_device(self, packet=None):
        if packet is None:
            packets = self.templates.get('ADXL') or []
            if not packets: return ''
            packet = packets[self.current_index['ADXL']]
        for axis in ('X','Y','Z'):
            frame = packet.get(axis)
            if isinstance(frame, dict):
                key = frame.get('dvicMacAddr') or frame.get('macAddr')
                if key: return srss_limit_key(key)
        return ''

    def _refresh_srss_input(self):
        device=self._srss_device() if self.sensor.get()=='ADXL' else ''
        if device!=self._srss_input_device:
            self._srss_input_device=device
            self.srss_limit_input.set(f'{srss_limit_get(device):.9g}')

    def _edit_srss_limit(self):
        device=self._srss_device() if self.sensor.get()=='ADXL' else ''
        if not device:
            messagebox.showwarning('SRSS 한계값','MEMS 장치를 선택하세요.',parent=self.root); return
        try:
            value=float(self.srss_limit_input.get())
            srss_limit_save(device,value)
        except (OSError,ValueError,TypeError) as exc:
            messagebox.showerror('SRSS 저장 실패',str(exc),parent=self.root); return
        self.srss_limit_input.set(f'{value:.9g}')
        self.preview()

    def _plot_adxl(self,set_obj, averaged=None):
        g_per_lsb=float(self.mems_g_per_lsb.get())
        if not np.isfinite(g_per_lsb) or not 0 < g_per_lsb <= 10:raise ValueError('1 LSB당 g는 0보다 크고 10 이하여야 합니다')
        lsb=1.0/g_per_lsb
        mems_correction=self._mems_correction_config()
        raw_mode=self.mems_raw_unit.get(); fft_mode=self.mems_fft_unit.get()
        win=self.mems_window.get();beta=float(self.mems_beta.get())
        n_value=self.mems_win_n.get();n=None if n_value=='전체' else int(n_value)
        raws=[];ffts=[];summaries=[];self._last_spectrum={}
        for axis,color in zip(('X','Y','Z'),('#1f77b4','#2ca02c','#d62728')):
            if axis not in set_obj:continue
            raw,fs,_=decode_packet('ADXL',set_obj[axis])
            if n is not None and n>len(raw):raise ValueError(f'Window N={n} > 실제 샘플 {len(raw)}')
            raw_filtered=spectral_filter(raw,fs,**self._mems_filter_args('RAW'))
            fft_filtered=spectral_filter(raw,fs,**self._mems_filter_args('FFT'))
            freq,amp=fft_peak(fft_filtered,fs,win,n,beta)
            if averaged and axis in averaged:
                freq,amp,_,_,_=averaged[axis]
            self._last_spectrum[axis]=(freq,amp)  # 원본 FFT, 회전수 추정 기준 유지
            if self.mems_axis[axis].get():
                summaries.append(f'{axis}: {np.std(raw/lsb):.6g} g RMS')
                raws.append((axis,color,raw_filtered,fs))
                ffts.append((axis,color,freq,amp))

        def scaled_unit(values,suffix):
            maxval=max((float(np.max(np.abs(v))) for v in values if len(v)),default=0)
            return (1e6,'µg'+suffix) if maxval<.001 else ((1e3,'mg'+suffix) if maxval<1 else (1.,'g'+suffix))
        if raw_mode.startswith('자동'):
            rs,ru=scaled_unit([v/lsb for _,_,v,_ in raws],'');rs/=lsb
        else:
            rs,ru={'Count':(1.,'Count'),'g':(1/lsb,'g'),'mg':(1000/lsb,'mg'),'µg':(1e6/lsb,'µg')}[raw_mode]
        if fft_mode.startswith('자동'):
            fscl,fu=scaled_unit([a/lsb/np.sqrt(2) for f,a in self._last_spectrum.values()],' RMS');fscl/=lsb*np.sqrt(2)
        else:
            fscl,fu={'Count Peak':(1.,'Count Peak'),'g Peak':(1/lsb,'g Peak'),
                     'g RMS':(1/lsb/np.sqrt(2),'g RMS / bin'),
                     'mm/s RMS':(1/lsb/np.sqrt(2),'mm/s RMS / bin')}[fft_mode]
        for axis,color,raw,fs in raws:
            self.ax_raw.plot(np.arange(len(raw))/fs*1000,raw*rs,label=axis,color=color,lw=.8)
        # SRSS is one overall AC RMS value, not a frequency-dependent trace.
        fft_outputs={}
        for axis,(f,a) in self._last_spectrum.items():
            if averaged and axis in averaged:
                _,_,_,f_display,a_display=averaged[axis]
            else:
                f_display,a_display=self._convert_fft_result('ADXL',f,a)
            if fft_mode.startswith('자동'):
                a_display=a_display*(fscl*lsb*np.sqrt(2))
            fft_outputs[axis]=(np.asarray(f_display),np.asarray(a_display))
        fft_colors={'X':'#1f77b4','Y':'#2ca02c','Z':'#d62728'}
        for axis,(f_display,a_display) in fft_outputs.items():
            if self.mems_axis[axis].get():
                self.ax_fft.plot(f_display/1000,a_display,label=axis,color=fft_colors[axis],lw=.8)
        # In averaging mode average the individual packet SRSS RMS values,
        # never concatenate packets or take an RMS of averaged amplitudes.
        packets=self.templates.get('ADXL') or []
        idx=self.current_index['ADXL']
        choice=self.fft_avg_count.get().strip()
        amount=(len(packets)-idx if choice=='전체' else int(choice)) if self.fft_avg_on.get() else 1
        candidates=packets[idx:idx+amount] if self.fft_avg_on.get() else [set_obj]
        levels=[]; reference=None; line_freq=None; gn_curves=[]
        for packet in candidates:
            try:
                if not all(packet.get(axis) is not None for axis in ('X','Y','Z')): continue
                powers=[]; signature=None; current_f=None; gn_axes=[]
                for axis in ('X','Y','Z'):
                    raw,fs,_=decode_packet('ADXL',packet[axis])
                    nn=len(raw) if n is None else n
                    if nn<2 or nn>len(raw): raise ValueError('SRSS N')
                    sig=(float(fs),len(raw),nn)
                    if signature is not None and sig!=signature: raise ValueError('SRSS XYZ mismatch')
                    signature=sig
                    freq,peak=fft_peak(spectral_filter(raw,fs,**self._mems_filter_args('FFT')),fs,win,n,beta)
                    if self.mems_axis['gN'].get():
                        gf,ga=self._convert_fft_result('ADXL',freq,peak)
                        gn_axes.append((np.asarray(gf),np.asarray(ga)))
                    f,peak=corrected_mems_fft(freq,peak*(1.0 if fft_mode=='Count Peak' else g_per_lsb),mems_correction,apply_sensitivity=fft_mode!='Count Peak')
                    if fft_mode=='mm/s RMS':
                        v=np.zeros_like(peak); mask=f>0; v[mask]=peak[mask]*9806.65/(2*np.pi*f[mask]); peak=v
                    powers.append(srss_axis_power(peak,nn,fft_window(nn,win,beta)))
                    current_f=f
                if reference is not None and signature!=reference: continue
                reference=signature; line_freq=current_f
                levels.append(float(np.sqrt(sum(powers))))
                if gn_axes and all(np.array_equal(gf,gn_axes[0][0]) for gf,ga in gn_axes):
                    gn_curves.append(np.sqrt(sum(ga**2 for gf,ga in gn_axes)))
            except (ValueError,TypeError,OverflowError): continue
        if gn_curves and self.mems_axis['gN'].get():
            gn=np.mean(gn_curves,axis=0)
            if fft_mode.startswith('자동'): gn=gn*(fscl*lsb*np.sqrt(2))
            self.ax_fft.plot(line_freq/1000,gn,label='gN',color='#17a2b8',lw=1.4)
        if levels:
            level=float(np.mean(levels))
            unit='Count RMS' if fft_mode=='Count Peak' else 'mm/s RMS' if fft_mode=='mm/s RMS' else 'g RMS'
            if fft_mode.startswith('자동'):
                scale=fscl*lsb*np.sqrt(2); level*=scale; unit=fu
            text='SRSS = {:.6g} {}{}'.format(level,unit,' | 평균 {}/{}회'.format(len(levels),len(candidates)) if self.fft_avg_on.get() else '')
            if self.mems_axis['SRSS'].get():
                self.ax_fft.plot(line_freq/1000,np.full(len(line_freq),level),label='SRSS',color='#9467BD',lw=1.7,ls='--')
                self.ax_fft.set_title(text,fontsize=10)
        elif self.mems_axis['SRSS'].get():
            self.ax_fft.set_title('SRSS: 완전한 XYZ 패킷 없음',fontsize=10)
        if fft_mode=='mm/s RMS' and fft_outputs:
            device=self._srss_device(set_obj)
            limit=srss_limit_get(device)
            freq_limit=next(iter(fft_outputs.values()))[0]/1000
            self.ax_fft.plot(freq_limit,np.full(len(freq_limit),limit),label='SRSS 한계 {:.6g} mm/s RMS'.format(limit),color='#b45309',lw=1.5,ls='--')
        if raws:
            self.ax_raw.legend(loc='upper right')
        if self.ax_fft.lines:
            self.ax_fft.legend(loc='upper right')
        else:
            self.ax_raw.text(.5,.5,'표시할 축 없음',transform=self.ax_raw.transAxes,ha='center')
        self.ax_raw.set_ylabel('ADXL '+ru);self.ax_raw.set_xlabel('Time (ms)')
        self.ax_fft.set_ylabel(fu);self.ax_fft.set_xlabel('Frequency (kHz)')
        self.ax_raw.grid(alpha=.3);self.ax_fft.grid(alpha=.3)
        self.mems_rms_summary.set('축별 전체 구간 AC g RMS: '+(' | '.join(summaries) if summaries else '없음'))
        raw_formula={'Count':'RAW=원본 Count','g':'RAW=Count/LSB/g','mg':'RAW=Count/LSB/g×1000',
                     'µg':'RAW=Count/LSB/g×1000000'}.get(raw_mode,'RAW=Count/LSB/g; 표시에 따라 g/mg/µg 자동 조절')
        fft_formula={'Count Peak':'FFT=2|rFFT((Count-평균)×Window)|/ΣWindow',
                     'g Peak':'FFT=Count Peak/LSB/g',
                     'g RMS':'FFT=Count Peak/LSB/g/√2 (각 주파수 bin)',
                     'mm/s RMS':'FFT 속도=보정된 g RMS/bin×9806.65/(2π×보정된 f[Hz]); f=0은 0'}.get(fft_mode,'FFT=Count Peak/LSB/g/√2; 표시 단위 자동')
        active_filters=', '.join(f'{key}:{"RAW" if d["RAW"].get() else ""}{"+FFT" if d["FFT"].get() else ""}' for key,d in self.mems_filters.items() if d['RAW'].get() or d['FFT'].get()) or 'OFF'
        freq_enabled = mems_correction['frequency_enabled']
        sensitivity_enabled = mems_correction['sensitivity_enabled']
        self.mems_formula.set(
            f'{raw_formula} | {fft_formula} | LSB/g={lsb:g} | Window={win} '
            f'N={n or "전체"} | 필터={active_filters} | '
            f'FFT 주파수 보정={freq_enabled} / 감도 보정={sensitivity_enabled}. '
            '전체 AC g RMS=√mean(((Count−평균)/LSB/g)²)'
        )

    def _fft_diagnostic_records(self):
        sensor=self.sensor.get(); packets=self.templates.get(sensor) or []
        if sensor not in ('PIEZO','ADXL') or not packets:return {},sensor
        index=self.current_index[sensor]; packet=packets[index]; records={}
        axes={'PIEZO':packet} if sensor=='PIEZO' else packet
        for axis, obj in axes.items():
            if axis not in ('PIEZO','X','Y','Z'):continue
            raw,fs,_=decode_packet(sensor,obj)
            if len(raw)<2:continue
            if sensor=='PIEZO':
                source=spectral_filter(raw-raw.mean(),fs,**self._filter_args('FFT'))
                win=self.window.get(); beta=8.6; n=len(source)
            else:
                source=spectral_filter(raw,fs,**self._mems_filter_args('FFT'))
                win=self.mems_window.get(); beta=float(self.mems_beta.get())
                v=self.mems_win_n.get(); n=len(source) if v=='전체' else min(len(source),max(2,int(v)))
            offset=(len(source)-n)//2; source=source[offset:offset+n]; w=fft_window(n,win,beta)
            records[axis]=dict(raw=raw.copy(),fs=fs,processed=(source-source.mean())*w,gain=float(w.sum()))
        return records,f'{sensor} 패킷 {index+1}/{len(packets)}'

    def _open_fft_diagnostic(self):
        try:
            records,title=self._fft_diagnostic_records()
            show_fft_diagnostic_tk(self.graph_window,records,title)
        except Exception as exc:messagebox.showerror('FFT/IFFT 검증',str(exc),parent=self.graph_window)

    def _update_lpf_auto(self, sensor, data):
        if sensor not in ('PIEZO','ADXL'):
            return
        d=(self.filters if sensor=='PIEZO' else self.mems_filters)['LPF']
        auto=d['auto'].get()
        d['entry'].configure(state='readonly' if auto else 'normal')
        if not auto:
            return
        rates=[]
        if data:
            packet=data[self.current_index[sensor]]
            frames=[packet] if sensor=='PIEZO' else [packet[a] for a in ('X','Y','Z') if a in packet]
            for frame in frames:
                try:
                    fs=float(frame.get('samplerate') or (25600 if sensor=='PIEZO' else 3200))
                    if np.isfinite(fs) and fs>0:rates.append(fs)
                except (ValueError,TypeError):
                    pass
        # If an incomplete/mixed-rate group is loaded, use its lowest Fs/2.
        value=format(min(rates)/2,'.12g') if rates else ''
        if d['freq'].get()!=value:d['freq'].set(value)

    def preview(self):
        if not hasattr(self,'ax_raw'):return
        s=self.sensor.get(); data=self.templates.get(s) or []
        self._refresh_srss_input()
        self._update_lpf_auto(s,data)
        self._update_preview_sections()
        old_y_limits=[ax.get_ylim() for ax in (self.ax_raw,self.ax_fft)] if self.graph_fixed.get() else None
        self.ax_raw.clear();self.ax_fft.clear()
        self.ax_raw.set_visible(True)
        self.ax_fft.set_visible(s!='초음파')
        self._pin_artists={}
        self._hover_mark=None;self._hover_text=None
        if s=='초음파':
            self.fft_avg_status.set('초음파에는 FFT 평균을 적용하지 않습니다.')
        if s!='ADXL' and hasattr(self,'mems_rms_summary'):
            self.mems_rms_summary.set('축별 g RMS: ADXL 선택 시 표시')
        if data:
            ix=self.current_index[s]; self.index.set(str(ix+1))
            self.position.set(f"{s}: {ix+1:,} / {len(data):,} 패킷 | 선택 시작점: {ix+1:,}" + (f" | 유효 축: {','.join(data[ix])}" if s=='ADXL' else ''))
            try:
                averaged=self._average_fft_results(s,data,ix) if s in ('PIEZO','ADXL') else {}
                if s=='PIEZO':self._plot_piezo(data[ix],averaged)
                elif s=='ADXL':self._plot_adxl(data[ix],averaged)
                else:
                    self._plot_ultrasonic(data[ix])
            except Exception as exc:
                self.ax_raw.text(.03,.5,f'미리보기 오류: {exc}',transform=self.ax_raw.transAxes,wrap=True,color='red')
        else:
            self.index.set('1');self.position.set(f'{s}: 파일을 선택하세요')
            self.ax_raw.text(.5,.5,'파일 선택 → ◀ ▶ 이동 → 전송 시작',ha='center',va='center',transform=self.ax_raw.transAxes)
        # RAW symmetric-log is display-only; preserve signed samples, zeros and negatives.
        if s!='초음파' and self.raw_symlog.get():
            try:
                manual=self.raw_linthresh.get().strip()
                if manual:
                    linthresh=float(manual)
                    if not np.isfinite(linthresh) or linthresh<=0:
                        raise ValueError('양수 필요')
                else:
                    amplitudes=[np.asarray(line.get_ydata(),dtype=float) for line in self.ax_raw.lines]
                    peak=max((float(np.max(np.abs(v[np.isfinite(v)]))) for v in amplitudes if np.any(np.isfinite(v))),default=0.0)
                    linthresh=max(peak/100.0,1e-12)
                self.ax_raw.set_yscale('symlog',linthresh=linthresh)
            except Exception as exc:
                self.cursor_status.set(f'RAW SymLog 설정 오류: {exc}')
                self.ax_raw.set_yscale('linear')
        else:
            self.ax_raw.set_yscale('linear')
        # Only display axes change; source waveforms, FFT, RMS and publish remain unmodified.
        self.ax_fft.set_xscale('log' if s!='초음파' and self.fft_log_x.get() else 'linear')
        self.ax_fft.set_yscale('log' if s!='초음파' and self.fft_log_y.get() else 'linear')
        if s!='초음파' and self.fft_log_x.get():
            # 0 Hz cannot be represented on a logarithmic X axis.
            positive_x=[]
            for curve in self.ax_fft.lines:
                xs=np.asarray(curve.get_xdata(),dtype=float)
                positive_x.extend(xs[np.isfinite(xs)&(xs>0)].tolist())
            if positive_x:
                self.ax_fft.set_xlim(min(positive_x),max(positive_x))
        if s!='초음파' and self.fft_log_y.get():
            positive_y=[]
            for curve in self.ax_fft.lines:
                ys=np.asarray(curve.get_ydata(),dtype=float)
                positive_y.extend(ys[np.isfinite(ys)&(ys>0)].tolist())
            if positive_y and not self.graph_fixed.get():
                lo,hi=min(positive_y),max(positive_y)
                self.ax_fft.set_ylim(lo if lo<hi else lo/2,hi if lo<hi else hi*2)
        if old_y_limits:
            for prefix,ax,ylim in zip(('raw','fft'),(self.ax_raw,self.ax_fft),old_y_limits):
                ymin=getattr(self,f'{prefix}_ymin').get().strip()
                ymax=getattr(self,f'{prefix}_ymax').get().strip()
                if ymin and ymax:
                    try:
                        if prefix=='fft' and self.fft_log_y.get() and float(ymin)<=0:
                            continue  # Cannot set a nonpositive lower bound on a log scale.
                        ax.set_ylim(float(ymin),float(ymax))
                    except ValueError:ax.set_ylim(ylim)
                else:ax.set_ylim(ylim)
        for kind,(axis,x) in list(self._pins.items()):
            ax=self.ax_raw if kind=='raw' else self.ax_fft
            for line in ax.lines:
                if line.get_label()!=axis:continue
                xs=np.asarray(line.get_xdata());ys=np.asarray(line.get_ydata())
                if len(xs):
                    ix=int(np.argmin(np.abs(xs-x)))
                    self._draw_pin((ax,axis,float(xs[ix]),float(ys[ix])))
                break
        # Explicit layouts prevent tight_layout from compressing BOTH panels
        # after switching away from the single-panel ultrasonic view.
        if s=='초음파':
            self.ax_raw.set_position([.095, .14, .875, .76])
        else:
            self.ax_raw.set_position([.095, .59, .875, .32])
            self.ax_fft.set_position([.095, .12, .875, .32])
        self.canvas.draw_idle()
        self._send_pg_graph()

    def _on_connect(self,client,userdata,flags,reason_code,properties=None):
        try:ok=int(reason_code)==0
        except Exception:ok=str(reason_code).lower() in ('0','success')
        self.connected=ok
        if ok:self.connect_event.set()
        self.uiq.put(('status','Broker 연결됨' if ok else f'Broker 연결 실패: {reason_code}'))

    def _on_disconnect(self,*args):
        self.connected=False; self.uiq.put(('status','Broker 연결 끊김'))

    def _on_publish(self,*args):
        with self.counter_lock:self.acked+=1

    def _connect(self):
        self._disconnect()
        try:self.client=mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,client_id=f'mqtt-generator-v001-{os.getpid()}')
        except (TypeError,AttributeError):self.client=mqtt.Client(client_id=f'mqtt-generator-v001-{os.getpid()}')
        if self.user.get():self.client.username_pw_set(self.user.get(),self.password.get())
        self.client.on_connect=self._on_connect;self.client.on_disconnect=self._on_disconnect;self.client.on_publish=self._on_publish
        self.connect_event.clear();self.client.connect(self.host.get(),int(self.port.get()),keepalive=30);self.client.loop_start()
        if not self.connect_event.wait(4):
            self._disconnect();raise ConnectionError('MQTT Broker 연결 시간 초과')

    def _disconnect(self):
        if self.client:
            try:self.client.disconnect();self.client.loop_stop()
            except Exception:pass
        self.client=None;self.connected=False

    def test_connection(self):
        try:self._connect();messagebox.showinfo('Broker','MQTT 연결 성공')
        except Exception as e:messagebox.showerror('연결 오류',str(e))
        finally:self._disconnect()

    def _publish(self,s,obj,axis=None):
        if self.stop_event.is_set():return
        if s=='ADXL' and obj.get('_partial_recovered'):
            return  # 부분 복구는 분석 미리보기 전용; MQTT 송신 불가
        if self.loss_on.get() and random.random()*100<float(self.loss[s].get()):
            with self.counter_lock:self.dropped+=1
            return
        packet={k:v for k,v in obj.items() if not k.startswith('_')}
        packet=with_replay_metadata(packet)  # old CSV/JSON: synthesize absent status only at publish time
        if self.rewrite_mac.get():packet['dvicMacAddr']=self.mac.get().strip()
        if s in ('PIEZO','ADXL') and self.seq_auto.get():packet['seq']=self.seq_state.next_seq(axis or 'PIEZO')
        topic=self.topics[s].get().strip().replace('{device}',self.device.get().strip()).replace('{mac}',self.mac.get().strip() if self.rewrite_mac.get() else str(packet.get('dvicMacAddr') or self.mac.get()).strip())
        if not topic:raise ValueError(f'{s} 토픽이 비어 있습니다')
        info=self.client.publish(topic,json.dumps(packet,ensure_ascii=False,separators=(',',':')).encode('utf-8'),qos=int(self.qos.get()))
        if info.rc!=mqtt.MQTT_ERR_SUCCESS:raise RuntimeError(f'MQTT publish 실패 rc={info.rc}')
        with self.counter_lock:self.sent+=1

    def _worker(self,s):
        try:
            data=self.templates[s]
            start=self.start_indices[s];idx=start;total=len(data)
            base=float(self.period[s].get())
            if s in self.auto_period and self.auto_period[s].get():
                item=next(iter(data[start].values())) if s=='ADXL' else data[start]
                auto=period_ms_from_payload(item)
                if auto>0:base=auto
            gap=float(self.xyz_gap.get());jit=float(self.jitter[s].get())
            tickstep=int(self.tick_step.get());count=0;tickbase=None
            self.uiq.put(('log',f'[{s}] 패킷 {start+1}/{total} 부터 끝까지 반복 (처음 1번으로 돌아가지 않음), 주기 {base:g}ms'))
            self.uiq.put(('log',f'[{s}] 상태 메타데이터가 없으면 송신 시 acqStatus=OK, apSsid=REPLAY, apRssi=0을 재생용 임시값으로 추가합니다. 기존 값은 유지합니다.'))
            if s=='ADXL' and any(p.get('_partial_recovered') for group in data for p in group.values()):
                self.uiq.put(('log','[ADXL] 부분 복구된 패킷은 RAW/FFT 미리보기 전용이며 MQTT 송신에서 제외합니다.'))
            next_at=time.perf_counter()
            while not self.stop_event.is_set():
                wait=next_at-time.perf_counter()
                if wait>0 and self.stop_event.wait(wait):break
                obj=data[idx]
                if s=='ADXL':
                    for axis in ('X','Y','Z'):
                        if self.stop_event.is_set():break
                        if axis not in obj: continue
                        self._publish(s,obj[axis],axis)
                        if axis!='Z' and gap>0 and self.stop_event.wait(gap/1000):break
                else:
                    if s=='초음파' and tickstep:
                        obj=dict(obj)
                        if tickbase is None:
                            try:tickbase=int(obj.get('tick','0'))
                            except Exception:tickbase=0
                        obj['tick']=str(tickbase+count*tickstep)
                    self._publish(s,obj)
                count+=1
                if count%5==0:self.uiq.put(('count',s,count,idx))
                idx=idx+1 if idx+1<total else start
                interval=base+(random.uniform(-jit,jit) if self.jitter_on.get() else 0)
                next_at+=max(.1,interval)/1000
                if time.perf_counter()-next_at>max(base/1000,.5):next_at=time.perf_counter()
            self.uiq.put(('count',s,count,idx))
        except Exception as e:
            self.uiq.put(('log',f'[{s}] 오류: {e}'));self.stop_event.set()

    def start(self):
        if self.threads:return
        active=[s for s in self.templates if self.enabled[s].get()]
        try:
            if not active:raise ValueError('체크한 센서의 파일을 먼저 선택하세요')
            for s in self.enabled:
                if self.enabled[s].get() and s not in active:raise ValueError(f'{s} 체크됨: 파일을 선택하세요')
            for s in active:
                if float(self.period[s].get())<=0:raise ValueError(f'{s} 주기는 0보다 커야 합니다')
                if float(self.jitter[s].get())<0 or not 0<=float(self.loss[s].get())<=100:raise ValueError(f'{s} Jitter/Loss 범위 확인')
            # A single indexed preview selection is applied per stream.
            self.start_indices={s:self.current_index[s] for s in active}
            self._connect()
        except Exception as e:
            self._disconnect();messagebox.showerror('전송 시작 오류',str(e));return
        self.seq_state=SequenceState();self.stop_event.clear()
        with self.counter_lock:self.sent=self.acked=self.dropped=0
        self.counter={s:0 for s in self.files}
        self.threads=[threading.Thread(target=self._worker,args=(s,),daemon=True,name='REPLAY-'+s) for s in active]
        for t in self.threads:t.start()
        self.start_btn.configure(state='disabled');self.stop_btn.configure(state='normal')
        self.status.set('전송 중')
        self._log('선택 위치부터 전송 시작: '+', '.join(f'{s} #{self.start_indices[s]+1}' for s in active))

    def stop(self):
        self.stop_event.set();self.status.set('중지 요청')

    def _drain(self):
        self._poll_pg_actions()
        try:
            while True:
                item=self.uiq.get_nowait()
                if item[0]=='log':self._log(item[1])
                elif item[0]=='status':self.status.set(item[1])
                elif item[0]=='count':
                    _,s,count,idx=item;self.counter[s]=count
                    # Preview retains user-selected position while transmitting.
        except queue.Empty:pass
        with self.counter_lock:sent,ack,drop=self.sent,self.acked,self.dropped
        self.stats.set(f'Publish {sent:,} / PUBACK {ack:,} / Drop {drop:,} | '+', '.join(f'{s} {self.counter[s]}' for s in self.counter))
        if self.threads and all(not t.is_alive() for t in self.threads):
            self.threads=[];self._disconnect();self.start_btn.configure(state='normal');self.stop_btn.configure(state='disabled')
            self.status.set('중지');self._log('모든 센서 전송 종료')
        self.root.after(125,self._drain)

    def _log(self,msg):
        self.log.insert('end',f'[{time.strftime("%H:%M:%S")}] {msg}\n')
        self.log.see('end')
        if int(self.log.index('end-1c').split('.')[0])>1500:self.log.delete('1.0','300.0')

    def close(self):
        self.stop_event.set();self._disconnect()
        if self._pg_process is not None and self._pg_process.is_alive():
            try:self._pg_process.terminate();self._pg_process.join(timeout=1)
            except Exception:pass
        self.root.destroy()



def run_pyqtgraph_window(message_queue,action_queue):
    """One visible Qt window: toolbar, scrollable options, RAW/FFT plots."""
    import traceback
    try:
        import pyqtgraph as pg
        import pyqtgraph.exporters
        from pyqtgraph.Qt import QtCore, QtWidgets, QtGui
    except Exception:
        action_queue.put({'command':'error','message':traceback.format_exc()})
        return

    class SymLogAxis(pg.AxisItem):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.threshold=1.0
        def tickStrings(self, values, scale, spacing):
            return [f'{np.sign(v)*self.threshold*np.expm1(abs(v)):.3g}' for v in values]

    class QtPlots(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle('발생기 V055 PyQtGraph · 그래프 / 설정')
            self.resize(1250,850)
            self.widget=QtWidgets.QWidget();self.setCentralWidget(self.widget)
            layout=QtWidgets.QVBoxLayout(self.widget)
            toolbar=QtWidgets.QHBoxLayout()
            self.sensor=QtWidgets.QComboBox();self.sensor.addItems(['PIEZO','ADXL','초음파'])
            self.sensor.currentTextChanged.connect(lambda value:self.send('sensor',sensor=value))
            toolbar.addWidget(QtWidgets.QLabel('센서'));toolbar.addWidget(self.sensor)
            for text,delta in (('◀ 이전 패킷',-1),('다음 패킷 ▶',1)):
                b=QtWidgets.QPushButton(text);b.clicked.connect(lambda checked=False,d=delta:self.send('navigate',delta=d));toolbar.addWidget(b)
            self.index=QtWidgets.QLineEdit('1');self.index.setMaximumWidth(75)
            self.index.setValidator(QtGui.QIntValidator(1,2147483647,self.index))
            self.index.returnPressed.connect(self.go)
            toolbar.addWidget(QtWidgets.QLabel('패킷 번호'));toolbar.addWidget(self.index)
            b=QtWidgets.QPushButton('번호 이동');b.clicked.connect(self.go);toolbar.addWidget(b)
            b=QtWidgets.QPushButton('그래프 전체 보기');b.clicked.connect(lambda:self.send('reset'));toolbar.addWidget(b)
            b=QtWidgets.QPushButton('PNG 저장');b.clicked.connect(self.save_png);toolbar.addWidget(b)
            b=QtWidgets.QPushButton('FFT 과정 / IFFT 검증');b.clicked.connect(lambda:self.send('diagnostic'));toolbar.addWidget(b)
            toolbar.addStretch(1);layout.addLayout(toolbar)
            self.title=QtWidgets.QLabel('센서 그래프');layout.addWidget(self.title)
            # Top options scroll independently from the plots, with a movable divider.
            orientation=getattr(QtCore.Qt,'Orientation',QtCore.Qt)
            splitter=QtWidgets.QSplitter(orientation.Vertical)
            self.scroll=QtWidgets.QScrollArea();self.scroll.setWidgetResizable(True)
            self.options=QtWidgets.QWidget();self.option_layout=QtWidgets.QVBoxLayout(self.options)
            self.option_layout.setContentsMargins(4,4,4,4);self.option_layout.setSpacing(4)
            self.scroll.setWidget(self.options);splitter.addWidget(self.scroll)
            pg.setConfigOptions(antialias=False,background='w',foreground='k')
            self.graph=pg.GraphicsLayoutWidget();splitter.addWidget(self.graph)
            splitter.setStretchFactor(0,0);splitter.setStretchFactor(1,1)
            splitter.setSizes([235,570]);layout.addWidget(splitter,1)
            self.splitter=splitter
            self.cursor=QtWidgets.QLabel('곡선 위 이동: 값 표시 | 클릭: 고정/해제 | 휠: 확대·축소 | 좌+휠: X | 우+휠: Y');self.cursor.setWordWrap(True);layout.addWidget(self.cursor)
            self.plots=[];self.last=[];self.option_widgets={};self.option_sensor=None
            self.plot_keys=[];self.curve_items=[];self.last_revision=-1
            self.request_id=0;self.pending_request=0;self.rendered_index=1
            self.rendered_sensor=None;self.packet_total=0
            self.pins={};self.hover=None;self.markers={}
            self.press_pos=None;self.press_plot=None;self.wheel_gesture=False
            self.right_pos=None;self.right_wheel=False
            self.graph.viewport().setMouseTracking(True)
            self.graph.viewport().installEventFilter(self)
            self.graph.scene().sigMouseMoved.connect(self.mouse_moved)
        def send(self,command,**kwargs):
            if command in ('navigate','goto','sensor'):
                self.request_id+=1
                self.pending_request=self.request_id
                kwargs['request']=self.request_id
                kwargs.setdefault('sensor',self.sensor.currentText())
            action_queue.put(dict(command=command,**kwargs))
        def go(self):
            if self.index.text().strip():self.send('goto',index=self.index.text())
        def full_view(self):
            for p in self.plots:p.enableAutoRange(x=True,y=True)
        def save_png(self):
            path,_=QtWidgets.QFileDialog.getSaveFileName(self,'PNG 저장','fft_graph.png','PNG (*.png)')
            if path:pg.exporters.ImageExporter(self.graph.scene()).export(path)
        def plot_at(self,point):
            for i,p in enumerate(self.plots):
                if p.vb.sceneBoundingRect().contains(point):return i,p
            return None,None
        def nearest(self,point):
            i,p=self.plot_at(point)
            if p is None or i>=len(self.last):return None
            section=self.last[i];pos=p.vb.mapSceneToView(point)
            xlog=section.get('xlog',False);ylog=section.get('ylog',False)
            symlog=section.get('symlog',False);t=max(float(section.get('linthresh',1)),1e-12)
            target=10**pos.x() if xlog else pos.x()
            best=None
            for curve in section.get('curves',[]):
                x=np.asarray(curve['x'],dtype=float);y=np.asarray(curve['y'],dtype=float)
                if not len(x):continue
                j=int(np.searchsorted(x,target))
                for k in set((max(0,min(len(x)-1,j)),max(0,min(len(x)-1,j-1)))):
                    a,b=float(x[k]),float(y[k])
                    if not np.isfinite(a) or not np.isfinite(b) or (xlog and a<=0) or (ylog and b<=0):continue
                    vx=np.log10(a) if xlog else a
                    vy=np.log10(b) if ylog else (np.sign(b)*np.log1p(abs(b)/t) if symlog else b)
                    sp=p.vb.mapViewToScene(QtCore.QPointF(vx,vy))
                    d=(sp.x()-point.x())**2+(sp.y()-point.y())**2
                    if best is None or d<best[0]:best=(d,i,curve.get('name',''),a,b,vx,vy)
            return best if best is not None and best[0]<=18**2 else None
        def clear_marker(self,key=None):
            for k in list(self.markers) if key is None else [key]:
                for p,m in self.markers.pop(k,[]):
                    try:p.removeItem(m)
                    except Exception:pass
        def show_hit(self,hit,fixed=False):
            _,i,name,x,y,vx,vy=hit;p=self.plots[i];section=self.last[i]
            key=i if fixed else 'hover'
            self.clear_marker(key)
            color='#e67e22' if fixed else '#2688d6'
            marker=pg.ScatterPlotItem([vx],[vy],size=9 if fixed else 6,pen=pg.mkPen(color),brush=pg.mkBrush(color))
            p.addItem(marker,ignoreBounds=True);marker.setZValue(100)
            label='{} | {}={:.6g} / {}={:.6g}'.format(name,section.get('xlabel') or 'X',x,section.get('ylabel') or 'Y',y)
            text=pg.TextItem(label,color=color,anchor=(0,1),fill=pg.mkBrush(255,255,255,220))
            p.addItem(text,ignoreBounds=True);text.setPos(vx,vy);text.setZValue(101)
            self.markers[key]=[(p,marker),(p,text)]
            self.cursor.setText(('고정: ' if fixed else '현재 위치: ')+label)
        def mouse_moved(self,point):
            if self.press_pos is not None or self.right_pos is not None:return
            i,_=self.plot_at(point)
            if i in self.pins:
                self.clear_marker('hover');return
            hit=self.nearest(point)
            if hit:self.show_hit(hit)
            else:
                self.clear_marker('hover');self.cursor.setText('곡선 위 이동: 값 표시 | 클릭: 고정/해제 | 휠: 확대·축소 | 좌+휠: X | 우+휠: Y')
        def eventFilter(self,obj,event):
            if obj is not self.graph.viewport():return super().eventFilter(obj,event)
            et=getattr(QtCore.QEvent,'Type',QtCore.QEvent);buttons=getattr(QtCore.Qt,'MouseButton',QtCore.Qt)
            typ=event.type()
            if typ not in (et.MouseButtonPress,et.MouseButtonRelease,et.MouseMove,et.Wheel):
                return super().eventFilter(obj,event)
            point=event.position().toPoint() if hasattr(event,'position') else event.pos()
            scene=self.graph.mapToScene(point);i,p=self.plot_at(scene)
            if typ==et.MouseButtonPress:
                if event.button()==buttons.LeftButton:
                    self.press_pos=point;self.press_plot=i;self.wheel_gesture=False
                elif event.button()==buttons.RightButton and p is not None:
                    self.right_pos=point;self.right_wheel=False;return True
            elif typ==et.Wheel and p is not None:
                held=event.buttons();left=bool(held & buttons.LeftButton);right=bool(held & buttons.RightButton)
                if left:self.wheel_gesture=True
                if right:self.right_wheel=True
                delta=event.angleDelta().y()
                if not delta:return True
                factor=0.82**(delta/120.0);fixed=bool(self.last[i].get('ylimit')) if i<len(self.last) else False
                sx=1.0 if right else factor;sy=1.0 if left or fixed else factor
                p.vb.disableAutoRange();p.vb.scaleBy((sx,sy),center=p.vb.mapSceneToView(scene))
                return True
            elif typ==et.MouseMove and self.press_pos is not None and self.wheel_gesture:
                return True
            elif typ==et.MouseButtonRelease:
                if event.button()==buttons.LeftButton:
                    start=self.press_pos;old_plot=self.press_plot;wheel=self.wheel_gesture
                    self.press_pos=None;self.press_plot=None;self.wheel_gesture=False
                    if start is not None and not wheel and i==old_plot and (point-start).manhattanLength()<=5:
                        hit=self.nearest(scene)
                        self.clear_marker('hover')
                        if hit:
                            self.pins[i]=hit;self.show_hit(hit,True)
                        else:self.clear_pin(i)
                elif event.button()==buttons.RightButton and self.right_pos is not None:
                    wheel=self.right_wheel;start=self.right_pos;self.right_pos=None;self.right_wheel=False
                    if not wheel and p is not None and (point-start).manhattanLength()<=5:
                        menu=QtWidgets.QMenu(self);reset=menu.addAction('그래프 전체 보기');clear=menu.addAction('고정 커서 해제')
                        reset.triggered.connect(lambda:self.send('reset'));clear.triggered.connect(lambda checked=False,k=i:self.clear_pin(k))
                        menu.exec(self.graph.viewport().mapToGlobal(point)) if hasattr(menu,'exec') else menu.exec_(self.graph.viewport().mapToGlobal(point))
                    return True
            return super().eventFilter(obj,event)
        def clear_pin(self,i=None):
            if i is None:self.pins.clear();self.clear_marker()
            else:self.pins.pop(i,None);self.clear_marker(i)
            self.cursor.setText('고정 커서 해제')
        def closeEvent(self,event):
            # Hiding keeps the child alive; the main button can reopen it.
            event.ignore();self.hide();self.send('close')
        def create_control(self,d):
            k=d['kind'];key=d.get('id');value=d.get('value','')
            if k=='TLabel':
                w=QtWidgets.QLabel(str(value if d.get('var') else d.get('text','')));w.setWordWrap(True)
                # Formula/summary labels expand, while ordinary short labels stay compact.
                if d.get('var'):w.setStyleSheet('color:#365a78;')
            elif k=='TEntry':
                w=QtWidgets.QLineEdit(str(value));w.setMaximumWidth(max(65,min(150,12*d.get('width',8))))
                w.setReadOnly(bool(d.get('readonly',False)))
                w.editingFinished.connect(lambda w=w,key=key:self.send('widget',id=key,value=w.text()) if not w.isReadOnly() else None)
            elif k=='TCombobox':
                # Display descriptive units without changing the canonical values
                # used by the parent calculation model and formula selection.
                labels={
                    'Count':'ADC (Count)', 'Count Peak':'ADC (Count Peak)',
                    'mV':'전압 (mV)', 'g':'가속도 (g)', 'mg':'가속도 (mg)',
                    'µg':'가속도 (µg)', 'g Peak':'가속도 (g Peak)',
                    'g RMS':'가속도 (g RMS)', 'mm/s RMS':'속도 (mm/s RMS)',
                    '자동 (g RMS)':'자동 가속도 (g RMS)',
                    '자동 (g/mg/µg)':'자동 가속도 (g/mg/µg)',
                }
                w=QtWidgets.QComboBox()
                for option in d.get('values',[]):
                    canonical=str(option)
                    w.addItem(labels.get(canonical,canonical),canonical)
                idx=w.findData(str(value))
                if idx>=0:w.setCurrentIndex(idx)
                w.currentIndexChanged.connect(lambda _i,w=w,key=key:self.send('widget',id=key,value=w.currentData()))
            elif k=='TCheckbutton':
                w=QtWidgets.QCheckBox(d.get('text',''));w.setChecked(bool(value))
                w.toggled.connect(lambda v,key=key:self.send('widget',id=key,value=bool(v)))
            elif k=='TButton':
                w=QtWidgets.QPushButton(d.get('text',''));w.clicked.connect(lambda checked=False,key=key:self.send('widget',id=key))
            else:return None
            self.option_widgets[key]=w
            if d.get('srss_input'):self.srss_entry_id=key
            return w
        def add_rows(self,layout,rows):
            # Preserve logical rows while wrapping long packed toolbars into compact
            # horizontal lines. Formula/status labels receive their own full-width row.
            for row in rows:
                pending=[]
                def flush():
                    if not pending:return
                    line=QtWidgets.QHBoxLayout();line.setSpacing(5)
                    for item in pending:line.addWidget(item)
                    line.addStretch(1);layout.addLayout(line);pending.clear()
                for d in row:
                    if d['kind']=='group':
                        flush()
                        if d.get('text','').startswith('필터 설정'):
                            group=QtWidgets.QGroupBox(d['text'])
                            grid=QtWidgets.QGridLayout(group)
                            grid.setHorizontalSpacing(8);grid.setVerticalSpacing(5)
                            for r, filter_row in enumerate(d['children']):
                                for c, item in enumerate(filter_row):
                                    control=self.create_control(item)
                                    if control is None:continue
                                    if len(filter_row)==1:
                                        grid.addWidget(control,r,0,1,8)
                                    else:
                                        grid.addWidget(control,r,item.get('grid_column',c))
                            grid.setColumnStretch(8,1)
                            layout.addWidget(group)
                        else:
                            self.add_rows(layout,d['children'])
                        continue
                    w=self.create_control(d)
                    if w is None:continue
                    if d['kind']=='TLabel' and (d.get('var') or len(d.get('text',''))>75):
                        flush();layout.addWidget(w);continue
                    # Keep a short label adjacent to its next field even at wrap boundaries.
                    if len(pending)>=(9 if any(x.get('text')=='SRSS' for x in row) else 6) and (not pending or not isinstance(pending[-1],QtWidgets.QLabel)):
                        flush()
                    pending.append(w)
                flush()
        def update_options(self,payload):
            sensor=payload.get('sensor');groups=payload.get('options',[])
            device_changed=payload.get('srss_device')!=getattr(self,'srss_input_device',None)
            self.srss_input_device=payload.get('srss_device')
            if sensor!=self.option_sensor:
                self.option_sensor=sensor;self.option_widgets={}
                # Replace the complete scroll content; no leaked nested layouts.
                old=self.scroll.takeWidget();self.options=QtWidgets.QWidget()
                self.option_layout=QtWidgets.QVBoxLayout(self.options);self.option_layout.setContentsMargins(4,4,4,4)
                for group in groups:
                    box=QtWidgets.QGroupBox(group.get('text',''));box_layout=QtWidgets.QVBoxLayout(box)
                    self.add_rows(box_layout,group['children']);self.option_layout.addWidget(box)
                self.option_layout.addStretch(1);self.scroll.setWidget(self.options)
                if old is not None:old.deleteLater()
                self.scroll.setVisible(bool(groups))
                if groups:self.splitter.setSizes([235,570])
            def refresh(d):
                if d['kind']=='group':
                    for row in d['children']:
                        for child in row:refresh(child)
                    return
                w=self.option_widgets.get(d.get('id'))
                if w is None or not d.get('var'):return
                val=d.get('value','');w.blockSignals(True)
                try:
                    if d['kind']=='TLabel':w.setText(str(val))
                    elif d['kind']=='TCheckbutton':w.setChecked(bool(val))
                    elif d['kind']=='TCombobox':
                        idx=w.findData(str(val))
                        if idx>=0:w.setCurrentIndex(idx)
                    elif d['kind']=='TEntry':
                        readonly=bool(d.get('readonly',False))
                        was_readonly=w.isReadOnly()
                        w.setReadOnly(readonly)
                        if readonly or was_readonly or not w.hasFocus() or (device_changed and d.get('id')==getattr(self,'srss_entry_id',None)):w.setText(str(val))
                finally:w.blockSignals(False)
            for g in groups:refresh(g)
        def update_graph(self,payload):
            revision=int(payload.get('revision',0))
            ack=int(payload.get('ack',0))
            if revision<=self.last_revision or ack<self.pending_request:
                return
            sections=payload.get('sections',[])
            sensor=payload.get('sensor','')
            index=int(payload.get('index','1'))
            changed_packet=(sensor,index)!=(self.rendered_sensor,self.rendered_index)
            # Keep plot and curve objects alive between packets. Rebuild only
            # when the sensor or axis type changes (e.g. RAW symmetric log).
            keys=[(sensor,section['name'],bool(section.get('symlog'))) for section in sections]
            rebuild=keys!=self.plot_keys
            self.clear_pin()
            if rebuild:
                self.graph.clear();self.plots=[];self.curve_items=[]
                for i,section in enumerate(sections):
                    axis=SymLogAxis(orientation='left') if section.get('symlog') else pg.AxisItem(orientation='left')
                    p=self.graph.addPlot(row=i,col=0,axisItems={'left':axis})
                    p.showGrid(x=True,y=True,alpha=.20);p.addLegend(offset=(-10,10))
                    self.plots.append(p);self.curve_items.append({})
                self.plot_keys=keys
            for p,items,section in zip(self.plots,self.curve_items,sections):
                p.setTitle(section.get('title','') or section.get('notice',''))
                p.setLabel('bottom',section.get('xlabel',''));p.setLabel('left',section.get('ylabel',''))
                xlog=section.get('xlog',False);ylog=section.get('ylog',False);symlog=section.get('symlog',False)
                p.setLogMode(x=xlog,y=ylog)
                names={curve.get('name','') for curve in section.get('curves',[])}
                for name in list(items):
                    if name not in names:
                        p.removeItem(items.pop(name))
                for curve in section.get('curves',[]):
                    name=curve.get('name','')
                    x=np.asarray(curve['x'],dtype=float);y=np.asarray(curve['y'],dtype=float)
                    good=np.isfinite(x)&np.isfinite(y)
                    if xlog:good&=x>0
                    if ylog:good&=y>0
                    if symlog:
                        t=max(float(section.get('linthresh',1)),1e-12)
                        p.getAxis('left').threshold=t
                        y=np.sign(y)*np.log1p(np.abs(y)/t)
                    pen=pg.mkPen(curve.get('color','#1f77b4'),width=1.7 if curve.get('dashed') else 1.2)
                    if curve.get('dashed'):
                        pen.setDashPattern([5.0,3.0])
                    if name not in items:
                        items[name]=p.plot(x[good],y[good],pen=pen,name=name)
                    else:
                        items[name].setData(x=x[good],y=y[good],pen=pen)
                if rebuild or changed_packet or payload.get('reset'):
                    p.enableAutoRange(x=True,y=True)
                if section.get('ylimit'):
                    a,b=section['ylimit']
                    if symlog:
                        t=max(float(section.get('linthresh',1)),1e-12)
                        a=np.sign(a)*np.log1p(abs(a)/t);b=np.sign(b)*np.log1p(abs(b)/t)
                    elif ylog:
                        if a>0 and b>0:a,b=np.log10(a),np.log10(b)
                        else:a=b=0
                    if a<b:p.setYRange(a,b,padding=0)
            self.update_options(payload)
            self.sensor.blockSignals(True)
            try:self.sensor.setCurrentText(sensor)
            finally:self.sensor.blockSignals(False)
            # Do not overwrite a packet number being typed by the user during
            # an unrelated settings refresh. Navigation acknowledgements do.
            if changed_packet or ack>=self.pending_request and self.pending_request or not self.index.hasFocus():
                self.index.setText(str(index))
            self.title.setText(payload.get('position',''))
            self.last=sections;self.last_revision=revision
            self.rendered_sensor=sensor;self.rendered_index=index
            self.packet_total=int(payload.get('total',0))
            if ack>=self.pending_request:self.pending_request=0
            if payload.get('show'):
                self.show();self.raise_();self.activateWindow()
    try:
        app=QtWidgets.QApplication([]);app.setQuitOnLastWindowClosed(False)
        win=QtPlots();timer=QtCore.QTimer()
        def poll():
            latest=None;show=False;reset=False
            try:
                while True:
                    item=message_queue.get_nowait()
                    if item.get('command')=='plot':
                        if latest is None or int(item.get('revision',0))>int(latest.get('revision',0)):
                            latest=item
                        show=show or item.get('show',False);reset=reset or item.get('reset',False)
                    elif item.get('command')=='hide':win.hide();show=False
                    elif item.get('command')=='diagnostic':
                        show_fft_diagnostic_qt(win,item['records'],item['title'])
            except queue.Empty:pass
            except (EOFError,OSError):app.quit();return
            if latest is not None:
                try:
                    latest['show']=show;latest['reset']=reset;win.update_graph(latest)
                except Exception:
                    error=traceback.format_exc();win.cursor.setText('그래프 갱신 오류 — 메인 송신 로그 확인');action_queue.put({'command':'error','message':error})
        timer.timeout.connect(poll);timer.start(60)
        win.show()
        app.exec() if hasattr(app,'exec') else app.exec_()
    except Exception:
        action_queue.put({'command':'error','message':traceback.format_exc()})


def main():
    root=tk.Tk();Generator(root);root.mainloop()

if __name__=='__main__':
    mp.freeze_support()
    main()

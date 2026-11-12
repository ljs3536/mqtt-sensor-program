"""Sensor decode, file load, and graph math used by the web app.

Copied from the desktop programs so the website does not import those modules.
JSON scale files stay next to the project root.
"""
from __future__ import annotations

import csv
import json
import math
import re
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

APP_DIR = Path(__file__).resolve().parent.parent

MEMS_SCALE_FILENAME = "mems_lsb_scale.json"
MEMS_DEFAULT_G_PER_LSB = 1.0 / 256.0

def read_mems_g_per_lsb():
    try:
        path = APP_DIR / MEMS_SCALE_FILENAME
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
    path = APP_DIR / MEMS_SCALE_FILENAME
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
        path = APP_DIR / MEMS_CORRECTION_FILENAME
        return validate_mems_correction(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError):
        return dict(MEMS_CORRECTION_DEFAULTS)

def save_mems_correction(values):
    values = validate_mems_correction(values)
    path = APP_DIR / MEMS_CORRECTION_FILENAME
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

APP_TITLE = 'MQTT H/ADXL/PIEZO 발생기 V029 — ADXL CSV 부분 복구'
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
    if lpf:
        if not 0 < lp_cutoff < fs/2: raise ValueError('LPF 컷오프는 Fs/2보다 작아야 합니다')
        resp*=1/np.sqrt(1+(bins/lp_cutoff)**(2*lp_order))
    if notch:
        if not (0 < notch_center < fs/2 and 0 < notch_width < fs/2):
            raise ValueError('Notch 주파수/폭은 0~Fs/2 안으로 설정하세요')
        sigma=notch_width/(2*np.sqrt(2*np.log(2)))
        resp*=1-np.exp(-.5*((bins-notch_center)/sigma)**2)
    return np.fft.irfft(np.fft.rfft(data)*resp, n=len(data))


FFT_WINDOWS=('Rectangular','Hann','Hamming','Blackman','Blackman-Harris','Flat Top','Bartlett','Kaiser')

def fft_window(n, name='Hann', beta=8.6):
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

def fft_peak(x,fs,window='Hann',window_size=None,beta=8.6):
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

ADXL_CODE_TO_RATE = {
    int(code, 16): float(rate)
    for rate, code in ADXL_RATE_TO_CODE.items()
}

def safe_filename(value):
    text = str(value or "UNKNOWN").strip()
    text = re.sub(r'[^0-9A-Za-z._-]+', '_', text)
    return text or "UNKNOWN"

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

    has_sample_info = (
        "samplerate" in obj
        and "numofsample" in obj
    )
    return not has_sample_info


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


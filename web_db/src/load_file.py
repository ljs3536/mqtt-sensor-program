"""발생기용 JSON·CSV를 패킷으로 읽는다."""

from __future__ import annotations

import csv
import json
import math
import re
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
TYPE_NIBBLE = {'PIEZO': 0, 'X': 1, 'Y': 2, 'Z': 3}

def sensor_type_from_seq(seq: str) -> str:
    """seq 상위 4비트로 센서 종류를 고른다.

    seq는 16진수 두 자리다. 상위 4비트가 0이면 PIEZO, 1/2/3이면 ADXL X/Y/Z이다.
    하위 4비트는 보낼 때마다 1씩 오르는 카운터라 여기서는 보지 않는다.
    예: "21" → 상위 2 → "Y". 해석할 수 없거나 4 이상이면 "UNKNOWN".
    """
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
    """패킷 한 개가 담은 시간(ms) = numofsample / samplerate × 1000.

    재전송에서 '자동 주기'를 켰을 때, 실제 센서가 한 패킷을 채우는 시간만큼 기다리려고 쓴다.
    예: 3200 Hz에 4096개면 1280 ms. 값이 없거나 0이면 0을 돌려주고, 부르는 쪽이 화면 주기를 쓴다.
    """
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
    """파일을 UTF-8(BOM 허용)로 읽어 안의 JSON object를 모두 꺼낸다. 하나도 없으면 ValueError."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="strict")
    objects = parse_json_objects(text)

    if not objects:
        raise ValueError("JSON object를 찾지 못했습니다.")

    return objects


def load_adxl_templates(path: str) -> dict[str, list[dict]]:
    """ADXL JSON 파일 하나를 seq 상위 4비트로 X/Y/Z 목록에 나눈다.

    파일 안 순서를 그대로 유지한다. 축 사이 짝 맞추기는 부르는 쪽(세트 묶기)이 한다.
    """
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
        """seq 하위 4비트 카운터를 시작한다."""
        self.value = start & 0xF
        self.lock = threading.Lock()

    def next_seq(self, sensor_type: str) -> str:
        """재전송 패킷에 붙일 다음 seq. 상위 4비트는 종류, 하위 4비트는 0~F를 돈다.

        PIEZO와 ADXL 세 축이 카운터 하나를 같이 쓴다. 그래서 X, Y, Z를 차례로 보내면
        "1n", "2(n+1)", "3(n+2)"가 되어 수신 쪽 grouping.continues_set이 한 세트로 묶는다.
        송신 스레드가 여럿이라 lock으로 카운터를 지킨다.
        """
        with self.lock:
            low = self.value
            self.value = (self.value + 1) & 0xF

        high = TYPE_NIBBLE[sensor_type]
        return f"{high:X}{low:X}"


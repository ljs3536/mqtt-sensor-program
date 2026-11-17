"""수신 패킷 분류와 sensorData HEX 디코드."""

from __future__ import annotations

import re

import numpy as np

from web_db.src.signal import ADXL_CODE_TO_RATE


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

def safe_filename(value):
    """파일 이름에 쓸 수 없는 문자를 밑줄로 바꾼다."""
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
    """진동 패킷을 PIEZO 또는 ADXL 축으로 분류한다."""
    # 초음파 H는 여기서 다루지 않는다. samplerate와 numofsample이 둘 다 있어야 진동이다.
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
    - sensorData는 있으나 samplerate와 numofsample이 둘 다 없는 형식
    - 둘 중 하나만 있으면 초음파로 보지 않는다
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

    # V049: 예전 H 프레임은 samplerate와 numofsample이 둘 다 없다.
    # 둘 중 하나만 있는 패킷은 불완전한 진동 프레임이지, 초음파가 아니다.
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


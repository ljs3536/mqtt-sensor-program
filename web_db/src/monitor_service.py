"""MQTT 수신, 최신 파형, CSV·SQLite 저장."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np
import paho.mqtt.client as mqtt

from web_db.src import analysis
from web_db.src import core as gen
from web_db.src import core as mon
from web_db.src.mqtt_util import mqtt_client
from web_db.src.save_csv import CsvWriter
from web_db.src.view_pack import _envelope_index, _pack_pair, _take


# 센서 명령(수신기 V057 '모드 A command'와 같음).
# - 설정 적용·START·STOP은 S/{MAC}에 QoS 1, retain으로 보낸다(구독 QoS 선택과 별개로 고정).
# - MQTT 명령은 JSON 본문만 보낸다. 끝에 CR이나 LF를 붙이지 않는다(CR은 시리얼 전용 규칙).
# - JSON 길이는 CR 1바이트를 포함해 1024바이트 이하여야 한다.
# - STOP은 3단계(STOP retain → retain 삭제 → normal retain)이고 단계마다 PUBACK을 기다린다.
COMMAND_QOS = 1
COMMAND_MAX_BYTES = 1024
PUBACK_TIMEOUT_S = 10
# 수신(구독) QoS 기본값. 수신기 V057 DEFAULT_QOS와 같다. 현재 설정 조회(CONFIG ?)도 이 QoS로 보낸다.
DEFAULT_SUBSCRIBE_QOS = 1


def subscribe_qos(value, default=DEFAULT_SUBSCRIBE_QOS):
    """화면·저장값을 0/1/2 QoS로 읽는다. 없으면 기본값, 범위 밖이면 오류."""
    if value is None or str(value).strip() == "":
        return int(default)
    try:
        qos = int(str(value).strip())
    except ValueError:
        raise ValueError("QoS는 0, 1, 2 중 하나여야 합니다") from None
    if qos not in (0, 1, 2):
        raise ValueError("QoS는 0, 1, 2 중 하나여야 합니다")
    return qos


def command_text(payload):
    """센서 명령 JSON 문자열. CR 포함 1024바이트를 넘으면 보내지 않고 오류(V057과 같음)."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) + 1 > COMMAND_MAX_BYTES:
        raise ValueError(f"센서 명령은 CR 포함 {COMMAND_MAX_BYTES}바이트 이하여야 합니다")
    return text


def _window_count(value, length):
    """창 길이를 샘플 수 안으로 제한한다. 전체면 샘플 수다."""
    text = str(value or "전체").strip()
    if text in ("", "전체", "0"):
        return length
    try:
        count = int(text)
    except ValueError:
        return length
    return max(1, min(length, count)) if length else 0


def _sliding_ac_rms(values, window_n):
    """각 창의 AC RMS = √mean((값−창 평균)²). 창이 전체면 패킷 표준편차 한 줄이다."""
    data = np.asarray(values, dtype=np.float64)
    count_all = data.size
    if count_all == 0:
        return data
    count = _window_count(window_n, count_all)
    if count >= count_all:
        return np.full(count_all, float(np.std(data)))
    sum_value = np.concatenate(([0.0], np.cumsum(data)))
    sum_square = np.concatenate(([0.0], np.cumsum(np.square(data))))
    window_sum = sum_value[count:] - sum_value[:-count]
    window_square = sum_square[count:] - sum_square[:-count]
    mean = window_sum / count
    variance = np.maximum(window_square / count - np.square(mean), 0.0)
    rms = np.sqrt(variance)
    curve = np.empty(count_all, dtype=np.float64)
    start = (count - 1) // 2
    curve[:start] = rms[0]
    curve[start:start + rms.size] = rms
    curve[start + rms.size:] = rms[-1]
    return curve


# Fs·N 직접 입력(2026-11-24, 센서 수신 모니터 '직접 입력' 체크): 표에 없는 조합을 개발·시험용으로 보낸다.
# N은 오타로 센서를 멈추게 하지 않도록 이 범위만 받는다. MEMS Fs는 센서가 레지스터 코드로 받으므로 ADXL_RATE_TO_CODE 값만.
CUSTOM_COUNT_RANGE = (16, 65536)
CUSTOM_PIEZO_RATE_RANGE = (1, 102400)


def custom_profile(sensor, rate, count):
    """직접 입력한 Fs·N을 검사해 (rate, count) 글자 쌍으로 돌려준다. 잘못되면 ValueError(이유)."""
    rate_text, count_text = str(rate if rate is not None else "").strip(), str(count if count is not None else "").strip()
    if not rate_text.isdigit() or not count_text.isdigit():
        raise ValueError("직접 입력 Fs와 N은 양의 정수여야 합니다")
    rate_value, count_value = int(rate_text), int(count_text)
    low, high = CUSTOM_COUNT_RANGE
    if not low <= count_value <= high:
        raise ValueError(f"직접 입력 N은 {low}~{high} 사이여야 합니다")
    if str(sensor).upper() == "PIEZO":
        low, high = CUSTOM_PIEZO_RATE_RANGE
        if not low <= rate_value <= high:
            raise ValueError(f"PIEZO Fs는 {low}~{high} Hz 사이여야 합니다")
    elif str(rate_value) not in mon.ADXL_RATE_TO_CODE:
        allowed = ", ".join(mon.ADXL_RATE_TO_CODE)
        raise ValueError(f"MEMS Fs는 센서 코드가 있는 값만 됩니다: {allowed} Hz")
    return str(rate_value), str(count_value)


def _sensor_config(sensor, period, profile, stop=False, sensor_type=None, g_range="16", piezo_gain="30"):
    """모니터 명령 JSON. COMMPATH는 0으로 고정한다.

    profile: Fs+N 목록 글자('3200 Hz + 8192') 또는 직접 입력을 검사한 (rate, count) 쌍(custom_profile).

    0은 종료, 1/3은 PIEZO normal/collection, 2/4는 MEMS normal/collection이다.
    G Range와 PIEZO 게인은 화면 환산 LSB와 별개로 센서에 보낸다.
    """
    code = str(sensor_type).strip() if sensor_type is not None else ""
    if stop or code == "0":
        return {"COMMPATH": "0", "SENSORTYPE": "0"}
    if code not in ("1", "2", "3", "4"):
        code = "1" if str(sensor).upper() == "PIEZO" else "2"
    # 수신기 V057처럼 0 이상의 정수만 받는다. 빈칸·소수(1.7)·글자는 잘라 쓰지 않고 오류로 알린다.
    try:
        if isinstance(period, bool):
            raise ValueError
        if isinstance(period, float):
            if not period.is_integer():
                raise ValueError
            period = int(period)
        period_value = int(str(period).strip())
    except (TypeError, ValueError):
        raise ValueError("측정 주기는 0 이상의 정수(초)여야 합니다") from None
    if period_value < 0:
        raise ValueError("측정 주기는 0 이상의 정수(초)여야 합니다")
    payload = {"COMMPATH": "0", "SENSORTYPE": code, "SENSING_PERIOD": str(period_value)}
    if code in ("1", "3"):
        rate, count = profile if isinstance(profile, tuple) else mon.PIEZO_PROFILE_MAP[profile]
        gain = str(piezo_gain).strip()
        if not gain.isdigit():
            raise ValueError("PIEZO_GAIN은 0 이상의 정수여야 합니다")
        payload.update({
            "PIEZO_SAMPLE_RATE": rate,
            "PIEZO_SAMPLE_COUNT": count,
            "PIEZO_GAIN": gain,
        })
        return payload
    rate, count = profile if isinstance(profile, tuple) else mon.ADXL_PROFILE_MAP[profile]
    sample_code = mon.ADXL_RATE_TO_CODE.get(str(rate))
    if not sample_code:
        raise ValueError(f"ADXL Sample Rate 코드 변환 실패: {rate}")
    if str(sample_code).lower().startswith("0x"):
        sample_code = str(sample_code)[2:]
    ranges = {"2": "0", "4": "1", "8": "2", "16": "3"}
    chosen = str(g_range).strip()
    if chosen not in ranges:
        raise ValueError("MEMS G Range는 2, 4, 8, 16 중 선택하세요")
    payload.update({
        "MEMS_SAMPLE_RATE": sample_code,
        "MEMS_SAMPLE_COUNT": count,
        "MEMS_G_RANGE": ranges[chosen],
    })
    return payload


class MonitorService:
    """MQTT 구독으로 최신 파형을 유지한다. 시리얼은 열지 않는다.

    그래프 장치의 빈 값은 최근 수신 MAC이다.
    디코드 실패와 처리 예외는 로그만 남기고 다음 패킷을 계속 받는다.
    초음파는 samplerate와 numofsample이 둘 다 비어 있을 때만 H로 본다.
    """
    def __init__(self, store):
        """수신 상태와 CSV 기록기를 준비한다. store는 SQLite 저장소다."""
        self.store = store
        self.lock = threading.Lock()
        self.client = None
        self.logs = deque(maxlen=200)
        self.messages = 0
        self.records = {}
        self.devices = []
        self.device_stats = {}
        self.latest_device = ""
        self.configs = {}
        # MAC별 마지막 S/{MAC} 설정을 받은 시각('MM-DD HH:MM:SS', 2026-11-27). 수신 모니터 '현재 retain' 칸에 보인다.
        self.config_times = {}
        self.connected = False
        self.mode = ""
        self.window = "전체"
        self.csv = CsvWriter()
        self.view_options = None
        self.host = "127.0.0.1"
        self.port = 1883
        self.topic = "V/#,D/#"
        # 구독 QoS(0/1/2, 기본 1). 현재 설정 조회(CONFIG ?)도 이 값으로 보낸다(V057과 같음).
        self.qos = DEFAULT_SUBSCRIBE_QOS
        # 실제로 구독할 토픽 목록(S/# 포함). 다시 붙을 때(_on_connect) 이 목록으로 다시 구독한다.
        self._topics = []
        # 'auto'면 설정 화면 값으로 서버가 붙인 연결, 'manual'이면 수신 모니터의 연결 버튼.
        self.origin = ""

    def log(self, text):
        """시각을 붙여 로그 한 줄을 남긴다. 200줄을 넘으면 오래된 줄부터 버린다."""
        self.logs.append(f"{time.strftime('%H:%M:%S')} {text}")

    def clear_logs(self):
        """화면의 수신 로그만 비운다. 저장된 패킷과 CSV는 그대로다."""
        with self.lock:
            self.logs.clear()

    def _remember(self, topic, obj):
        """측정 패킷을 디코드해 최신 파형으로 두고 CSV와 SQLite에 넘긴다.

        _on_message가 self.lock을 잡은 채 부른다. 순서는 다음과 같다.
        1. S/{MAC} 토픽이면 센서 설정이다(브로커 retain 또는 누가 방금 보낸 설정). configs에 두고,
           설정 JSON이면 sensor_config에도 남긴다(대문 연결 판정이 주기·STOP을 읽는다). 측정 처리는 하지 않는다.
        2. 초음파(D/ 토픽, 또는 samplerate·numofsample 없음)인지 먼저 보고, 아니면 classify_vibration
           (PIEZO / ADXL-X·Y·Z). samplerate 값과 numofsample이 다 있어야 진동이다.
        3. sensorData HEX를 샘플로 푼다. 실패하면 로그만 남기고 이 패킷은 저장하지 않는다.
        4. records[MAC][축]에 최신 한 개만 둔다. 수신 화면 그래프는 이것만 그린다.
        5. _save_csv로 CSV 큐와 SQLite packet에 같은 원본을 넘긴다.
        """
        if str(topic).startswith("S/"):
            mac = str(topic).split("/", 1)[1]
            # {"CONFIG":"?"}는 조회 요청이다(우리가 보낸 것이 S/#로 되돌아온 것 포함). 설정이 아니므로 버린다.
            if str(obj.get("CONFIG", "")).strip() == "?":
                return
            self.configs[mac] = obj
            self.config_times[mac] = datetime.now().strftime("%m-%d %H:%M:%S")
            if mac not in self.devices:
                self.devices.append(mac)
            self.log(f"설정 수신 S/{mac}")
            if "SENSORTYPE" in obj:
                try:
                    # 같은 JSON이면 쓰지 않는다. 웹이 보낸 retain이 되돌아와도 action 이름이 바뀌지 않는다.
                    self.store.save_sensor_config(mac, obj, "retain 수신", only_if_changed=True)
                except Exception as exc:
                    self.log(f"설정 JSON 저장 실패 {mac}: {exc}")
            return
        device = str(obj.get("dvicMacAddr") or obj.get("deviceId") or "").strip()
        if device and device not in self.devices:
            self.devices.append(device)
        # 초음파를 먼저 본다(D/ 토픽 또는 samplerate·numofsample이 없는 형식). 진동 패킷은 두 값이 늘 있어서
        # 순서를 바꿔도 진동이 초음파로 잘못 가지 않는다. 반대로 진동을 먼저 보면, 빈 samplerate가 붙은
        # 초음파가 seq 상위 0 때문에 PIEZO로 읽혀 디코드 실패로 버려졌다(2026-11-19).
        if mon.classify_ultrasonic(obj, topic):
            classified = ("ULTRASONIC", None)
        else:
            classified = mon.classify_vibration(obj)
        if classified is None:
            self.log(f"{topic} 측정 데이터 아님")
            return
        kind, _frame = classified
        sensor_hex = str(obj.get("sensorData") or "")
        sensor_key = "ADXL" if str(kind).startswith("ADXL") else "PIEZO"
        default_fs = 3200.0 if sensor_key == "ADXL" else 25600.0
        fs = float(mon.normalize_sample_rate(sensor_key, obj.get("samplerate"), default_fs))
        axis = kind.split("-")[-1] if kind.startswith("ADXL-") else kind
        try:
            if kind == "ULTRASONIC":
                samples, _freq, meta = mon.decode_ultrasonic_samples(sensor_hex)
                record = {"kind": "spectrum", "x": np.asarray(_freq, dtype=np.float64), "y": np.asarray(samples, dtype=np.float64)}
            elif kind == "PIEZO":
                samples = mon.decode_piezo_samples(sensor_hex, obj.get("numofsample"))
                record = {"kind": "waveform", "samples": np.asarray(samples, dtype=np.float64), "fs": fs, "physical": False}
            else:
                samples = mon.decode_vibration_samples(sensor_hex, obj.get("numofsample"))
                record = {"kind": "waveform", "samples": np.asarray(samples, dtype=np.float64), "fs": fs, "physical": True}
        except Exception as exc:
            seq = obj.get("seq", "")
            axis_name = "H" if kind == "ULTRASONIC" else axis
            self.log(
                f"{kind} Graph Decode 오류 {device or '?'}/{axis_name} "
                f"seq={seq}: {exc} (해당 프레임 제외, 그래프 처리 계속)"
            )
            return
        record.update({"device": device or topic, "axis": axis, "sensor": kind, "received_at": datetime.now().isoformat(timespec="milliseconds")})
        device_id = record["device"]
        self.records.setdefault(device_id, {})[axis] = record
        stat = self.device_stats.setdefault(device_id, {"count": 0, "last": ""})
        stat["count"] += 1
        # 2026-11-26: 날짜도 붙인다(월-일 시:분:초). 센서가 1시간 주기라 며칠 뒤 화면을 열면 시각만으로는 오늘인지 알 수 없었다.
        stat["last"] = datetime.now().strftime("%m-%d %H:%M:%S")
        self.latest_device = device_id
        self._save_csv(obj, kind, axis, sensor_hex, samples if kind != "ULTRASONIC" else record["y"], meta if kind == "ULTRASONIC" else None)
        rms = ""
        if record["kind"] == "waveform" and record["physical"]:
            g = record["samples"] * gen.read_mems_g_per_lsb()
            rms = f" gRMS={float(np.sqrt(np.mean(np.square(g)))):.6g}"
        self.log(f"{record['device']} {kind} N={len(samples) if kind != 'ULTRASONIC' else len(record['y'])}{rms}")

    def _save_csv(self, obj, kind, axis, sensor_hex, decoded, meta):
        """같은 패킷을 CSV 큐와 SQLite packet에 넣는다. 통계는 계산하지 않는다.

        CSV는 큐에 넣고 바로 돌아온다(CsvWriter 스레드가 쓴다).
        SQLite는 이 자리에서 바로 INSERT·commit한다. 저장이 실패해도 로그만 남기고
        수신은 계속한다. 그때 그 패킷은 CSV에만 남는다.
        """
        device = str(obj.get("dvicMacAddr") or obj.get("deviceId") or "UNKNOWN").strip()
        item = {
            "device": device,
            "kind": kind,
            "axis": axis if kind != "ULTRASONIC" else "H",
            "seq": obj.get("seq", ""),
            "tick": obj.get("tick", ""),
            "battery": obj.get("batteryRmin", ""),
            "gw": obj.get("gwMacAddr", ""),
            "sample_rate": obj.get("samplerate", ""),
            "num_samples": obj.get("numofsample", ""),
            "payload_bytes": len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            # 수신기 V057 CSV와 같은 상태 메타데이터. 없거나 null이면 빈 칸(CSV에만 쓰고 DB에는 넣지 않는다).
            "acq_status": "" if obj.get("acqStatus") is None else str(obj.get("acqStatus")),
            "ap_ssid": "" if obj.get("apSsid") is None else str(obj.get("apSsid")),
            "ap_rssi": "" if obj.get("apRssi") is None else str(obj.get("apRssi")),
            "sensor_data": sensor_hex,
            "decoded": np.asarray(decoded),
            "meta": meta,
            "received_at": datetime.now().isoformat(timespec="milliseconds"),
        }
        self.csv.submit(item)
        try:
            self.store.save_packet(item)
        except Exception as exc:
            self.log(f"SQLite 저장 실패: {exc}")

    def _view(self, record, window, options):
        """한 축 파형을 화면용 RAW와 FFT로 얇게 만든다."""
        if record["kind"] == "spectrum":
            index = _envelope_index(record["y"])
            return {
                "kind": "spectrum",
                "axis": record["axis"],
                "x": _take(record["x"], index, x_axis=True),
                "y": _take(record["y"], index),
                "x_label": "Frequency (kHz)",
                "y_label": "H spectrum",
                "received_at": record["received_at"],
            }
        sensor_kind = "ADXL" if record["physical"] else "PIEZO"
        try:
            shown = analysis.monitor_curves(
                record["samples"], record["fs"], sensor_kind, options, record.get("axis") or "X"
            )
        except Exception as exc:
            return {
                "kind": "waveform",
                "axis": record["axis"],
                "t": [],
                "y": [],
                "rms": [],
                "fft_f": [],
                "fft_y": [],
                "info": f"{record['axis']} 그래프 오류: {exc}",
                "received_at": record["received_at"],
            }
        y = np.asarray(shown["y"], dtype=np.float64)
        t = np.asarray(shown["t"], dtype=np.float64)
        # 표시 중인 파형과 같은 단위. 창마다 그 창의 평균을 뺀 AC RMS다.
        rms = _sliding_ac_rms(y, window)
        full = float(np.std(y)) if y.size else 0.0
        time_index = _envelope_index(y)
        rms_index = _envelope_index(rms)
        fft_x, fft_y = _pack_pair(shown["fft_f"], shown["fft_y"])
        return {
            "kind": "waveform",
            "axis": record["axis"],
            "t": _take(t, time_index, x_axis=True),
            "y": _take(y, time_index),
            "rms_t": _take(t, rms_index, x_axis=True),
            "rms": _take(rms, rms_index),
            "fft_f": fft_x,
            "fft_y": fft_y,
            "rms_value": round(full, 6),
            "n": int(y.size),
            "window": _window_count(window, y.size),
            "fs": record["fs"],
            "unit": shown["unit"],
            "fft_unit": shown["fft_unit"],
            # 값은 s·Hz 그대로 보내고 화면이 ms·kHz로 바꿔 보인다(수신기 V063). 이름표는 화면이 정한다.
            "x_label": "Time (ms)",
            "fft_x_label": "Frequency (kHz)",
            "formula": shown["formula"],
            "window_factor": shown.get("window_factor") or "",
            "info": shown["info"],
            "fft_log_x": shown["fft_log_x"],
            "fft_log_y": shown["fft_log_y"],
            "received_at": record["received_at"],
            "psd": shown.get("psd"),
        }

    def _pack_shown(self, record, shown, window):
        """ADXL 공유 계산 결과를 화면용 배열로 맞춘다."""
        y = np.asarray(shown["y"], dtype=np.float64)
        t = np.asarray(shown["t"], dtype=np.float64)
        rms = _sliding_ac_rms(y, window)
        full = float(np.std(y)) if y.size else 0.0
        time_index = _envelope_index(y)
        fft_x, fft_y = _pack_pair(shown["fft_f"], shown["fft_y"])
        return {
            "kind": "waveform",
            "axis": record["axis"],
            "t": _take(t, time_index, x_axis=True),
            "y": _take(y, time_index),
            "fft_f": fft_x,
            "fft_y": fft_y,
            "rms_value": round(full, 6),
            "n": int(y.size),
            "window": _window_count(window, y.size),
            "fs": record["fs"],
            "unit": shown["unit"],
            "fft_unit": shown["fft_unit"],
            # 값은 s·Hz 그대로 보내고 화면이 ms·kHz로 바꿔 보인다(수신기 V063). 이름표는 화면이 정한다.
            "x_label": "Time (ms)",
            "fft_x_label": "Frequency (kHz)",
            "formula": shown["formula"],
            "window_factor": shown.get("window_factor") or "",
            "info": shown["info"],
            "fft_log_x": shown.get("fft_log_x"),
            "fft_log_y": shown.get("fft_log_y"),
            "received_at": record["received_at"],
        }

    def _device_views(self, snapshot, window, options):
        """장치 축을 묶어 그래프 응답을 만든다. ADXL은 X/Y/Z를 함께 계산한다."""
        opts = dict(options or {})
        sample = next(iter(snapshot.values()), {})
        opts["device"] = sample.get("device") or ""
        adxl = {
            axis: (record["samples"], record["fs"])
            for axis, record in snapshot.items()
            if record.get("kind") == "waveform" and record.get("physical") and axis in ("X", "Y", "Z")
        }
        shared = analysis.monitor_adxl_set(adxl, opts) if adxl else {}
        views = {}
        for axis, record in snapshot.items():
            if axis in shared:
                views[axis] = self._pack_shown(record, shared[axis], window)
            else:
                views[axis] = self._view(record, window, opts)
        if "_srss" in shared:
            views["_srss"] = shared["_srss"]
        if "_gn" in shared:
            views["_gn"] = shared["_gn"]
        if "_srss_limit" in shared:
            views["_srss_limit"] = shared["_srss_limit"]
        # 실시간 PSD 요약(판정·계산 단계·경고). ADXL은 세트 하나, PIEZO는 그 축 view에 들어 있다.
        if "_psd" in shared:
            views["_psd"] = shared["_psd"]
        else:
            for view in views.values():
                if isinstance(view, dict) and view.get("psd"):
                    views["_psd"] = view.pop("psd")
                    break
        return views

    def fft_check(self, device=""):
        """고른 RAW가 FFT 후 IFFT로 복원되는지 검사한다."""
        with self.lock:
            names = list(self.records)
            target = device if device in self.records else ""
            if not target:
                target = self.latest_device if self.latest_device in self.records else (names[-1] if names else "")
            snapshot = {
                axis: dict(record)
                for axis, record in self.records.get(target, {}).items()
            } if target else {}
        if not snapshot:
            return analysis.fft_check_unavailable(
                "선택된 RAW 데이터가 없습니다. 초음파 H는 위상이 없는 스펙트럼입니다."
            )
        has_h = False
        records = []
        for axis, record in snapshot.items():
            if record.get("kind") == "spectrum":
                has_h = True
                continue
            samples = np.asarray(record.get("samples"), dtype=np.float64)
            fs = float(record.get("fs") or 0)
            if samples.size >= 2 and fs > 0 and samples.ndim == 1 and np.all(np.isfinite(samples)):
                records.append((str(record.get("axis") or axis), samples, fs))
        if not records:
            message = (
                "초음파 H는 위상이 없는 스펙트럼이라 FFT/IFFT 검증을 할 수 없습니다."
                if has_h else
                "선택된 RAW 데이터가 없습니다. 초음파 H는 위상이 없는 스펙트럼입니다."
            )
            return analysis.fft_check_unavailable(message)
        result = analysis.fft_check_many(records, str(target or ""))
        if has_h:
            result["message"] = result["message"] + " | 초음파 H는 위상이 없어 검증에서 제외"
        return result

    def _on_message(self, _client, _userdata, msg):
        """MQTT 메시지 하나를 해석한다. 한 프레임 오류로 수신을 멈추지 않는다.

        paho의 네트워크 스레드(loop_start)에서 메시지마다 불린다. 여기서 시간이 오래 걸리면
        다음 메시지 수신과 keepalive 응답이 늦어진다. 그래서 무거운 통계 계산은 하지 않는다.
        """
        with self.lock:
            self.messages += 1
        # STOP 2단계(retain 삭제)는 S/{MAC}에 빈 메시지를 보낸다. S/# 구독으로 되돌아오므로 JSON 오류로 보지 않는다.
        if not msg.payload and str(msg.topic).startswith("S/"):
            self.log(f"설정 retain 삭제 수신 {msg.topic}")
            return
        try:
            obj = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            self.log(f"{msg.topic} JSON 아님 ({len(msg.payload)} bytes)")
            return
        # 한 프레임의 오류가 paho 콜백을 끝내면 이후 수신이 멈춘다.
        if isinstance(obj, dict):
            try:
                with self.lock:
                    self._remember(str(msg.topic), obj)
            except Exception as exc:
                self.log(
                    f"수신 프레임 처리 오류 {msg.topic}: {exc} "
                    "(해당 프레임 제외, 그래프 처리 계속)"
                )

    def connect_mqtt(self, host, port, topic, csv_enabled=True, background=False, origin="manual",
                     qos=DEFAULT_SUBSCRIBE_QOS):
        """브로커에 붙고 토픽을 구독한다. CSV가 켜지면 저장 폴더를 연다.

        - 이전 연결은 먼저 끊는다. 사용자가 고른 토픽에 설정 토픽 S/#를 항상 더한다.
        - 구독은 _on_connect에서 한다. 그래서 paho가 끊긴 뒤 스스로 다시 붙을 때도 다시 구독한다
          (예전에는 처음 한 번만 구독해서, 끊겼다 붙으면 데이터가 안 들어왔다).
        - background=False(수신 모니터 연결 버튼): 지금 바로 붙어 보고, 실패하면 예외를 화면에 돌려준다.
        - background=True(서버 시작·설정 화면의 자동 연결): connect_async로 붙는다. 브로커가 아직
          꺼져 있어도 예외 없이 paho가 계속 다시 시도하고, 붙는 순간 _on_connect가 구독한다.
        """
        qos = subscribe_qos(qos)
        self.disconnect()
        client = mqtt_client(f"web-monitor-{int(time.time())}")
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.on_connect = self._on_connect
        # 다시 붙기 시도 간격: 1초에서 시작해 최대 30초.
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        topics = [item.strip() for item in str(topic).replace(";", ",").split(",") if item.strip()]
        if "S/#" not in topics:
            topics.append("S/#")
        self._topics = topics
        self.client = client
        self.origin = origin
        self.host, self.port, self.topic = host, int(port), topic
        self.qos = qos
        if background:
            client.connect_async(host, int(port), keepalive=30)
        else:
            try:
                client.connect(host, int(port), keepalive=30)
            except Exception:
                self.client = None
                raise
        client.loop_start()
        self.csv.enabled = bool(csv_enabled)
        folder = self.csv.start_session() if csv_enabled else None
        # 수동 연결은 connect가 성공했으니 바로 연결 상태. 자동 연결은 _on_connect가 켠다.
        self.connected = not background
        self.mode = "MQTT"
        self.log(
            f"MQTT {'자동 ' if origin == 'auto' else ''}연결 {'시도 ' if background else ''}"
            f"{host}:{port} 구독={', '.join(topics)} QoS={qos}"
        )
        if folder:
            self.log(f"CSV 저장 시작 {folder}")

    def _on_connect(self, client, _userdata, _flags, reason_code, *_rest):
        """브로커가 연결을 받아 줄 때마다(처음·다시 붙을 때) 구독한다."""
        if client is not self.client:
            return
        failure = getattr(reason_code, "is_failure", None)
        if failure is None:
            failure = int(reason_code) != 0
        if failure:
            self.log(f"MQTT 연결 거절 {reason_code}")
            return
        client.subscribe([(item, self.qos) for item in self._topics])
        self.connected = True
        self.mode = "MQTT"
        self.log(f"MQTT 구독 {', '.join(self._topics)} QoS={self.qos}")

    def remember_connection(self, host, port, topic, on, csv=None, qos=None):
        """수신 모니터의 연결·해제를 app_setting 'mqtt'에 남긴다(2026-11-19).

        연결 = 그 주소와 CSV 저장 체크를 기억하고 auto=True(서버가 다시 켜지면 같은 값으로 다시 연결).
        해제 = auto=False(다시 켜져도 연결하지 않음). 주소는 남겨 두어 다음에 칸을 채운다.
        설정 화면의 '수신 MQTT' 칸은 없앴다. 연결은 수신 모니터에서만 한다.
        """
        cfg = dict(self.store.get_setting("mqtt") or {})
        if on:
            cfg.update(host=str(host), port=int(port), topic=str(topic))
        if csv is not None:
            cfg["csv"] = bool(csv)
        if qos is not None:
            cfg["qos"] = subscribe_qos(qos)
        cfg["auto"] = bool(on)
        self.store.save_setting("mqtt", cfg)

    def auto_connect(self):
        """서버가 켜질 때(server.py) 마지막 연결을 다시 잇는다.

        수신 모니터에서 '연결'한 뒤 '연결 해제'를 하지 않은 채 서버가 다시 켜졌으면(컨테이너 재시작,
        재부팅, 새 이미지 배포) 그 주소·CSV 체크로 background 연결한다. 브로커가 꺼져 있어도 서버 시작을
        막지 않고 계속 다시 시도한다. 돌려주는 값: 연결을 시작했으면 True.
        """
        cfg = self.store.get_setting("mqtt") or {}
        if not cfg.get("auto"):
            return False
        self.connect_mqtt(
            str(cfg.get("host") or "127.0.0.1"),
            int(cfg.get("port") or 1883),
            str(cfg.get("topic") or "V/#,D/#"),
            csv_enabled=bool(cfg.get("csv", True)),
            background=True,
            origin="auto",
            # QoS를 저장하기 전(2026-11-20 전)의 값에는 qos가 없다. 그때는 기본 1.
            qos=subscribe_qos(cfg.get("qos")),
        )
        return True

    def _on_disconnect(self, client, _userdata, *args):
        """이 클라이언트의 연결이 끊기면 수신 상태를 끈다."""
        if client is not self.client:
            return
        reason = args[1] if len(args) > 1 else (args[0] if args else "")
        self.connected = False
        self.log(f"MQTT 연결 끊김 {reason} (자동으로 다시 붙기를 시도합니다)")

    def disconnect(self):
        """MQTT 연결과 CSV 저장 세션을 닫는다."""
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self.csv.close_session()
        self.connected = False
        self.mode = ""
        self.origin = ""

    def _require_connection(self, macs):
        """명령을 보낼 수 있는 상태인지 본다. 연결과 대상 MAC이 있어야 한다."""
        if self.client is None or not self.connected:
            raise ValueError("먼저 MQTT에 연결하세요")
        if not macs:
            raise ValueError("설정할 센서 MAC을 선택하세요")

    def _save_config(self, mac, payload, label):
        """보낸 설정 JSON을 sensor_config에 남긴다. 실패해도 명령은 이미 나갔으므로 로그만."""
        try:
            self.store.save_sensor_config(mac, payload, label)
        except Exception as exc:
            self.log(f"설정 JSON 저장 실패 {mac}: {exc}")

    def publish_config(self, macs, payload, label, retain=True):
        """S/{MAC} 로 설정 JSON을 보낸다(수신기 V057 _send_control_payload와 같음).

        - retain=True(설정 적용, START): QoS 1, retain. 브로커가 마지막 값을 들고 있다가
          센서가 나중에 붙어도 그 설정을 준다. 그 JSON을 sensor_config에 MAC별로 남긴다.
          PUBACK은 기다리지 않는다(V057과 같음. 보내기 요청이 받아졌는지 rc만 본다).
        - retain=False(현재 설정 조회 CONFIG ?): 구독 QoS로 보내고 남기지 않는다.
        - 본문은 JSON만. CR LF를 붙이지 않는다. CR 포함 1024바이트를 넘으면 보내지 않는다.
        - 여러 MAC 중 일부가 실패해도 나머지는 보내고, 끝에 실패 목록을 오류로 알린다.
        """
        self._require_connection(macs)
        text = command_text(payload)
        qos = COMMAND_QOS if retain else self.qos
        failures = []
        for mac in macs:
            try:
                info = self.client.publish(f"S/{mac}", text.encode("utf-8"), qos=qos, retain=retain)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError(f"rc={info.rc}")
            except Exception as exc:
                failures.append(f"{mac}({exc})")
                self.log(f"{label} S/{mac} 전송 실패: {exc}")
                continue
            self.log(f"{label} S/{mac} QoS={qos} retain={retain} {text}")
            if retain:
                self._save_config(mac, payload, label)
        if failures:
            raise RuntimeError(
                f"{label} 일부/전체 전송 실패: 성공 {len(macs) - len(failures)}/{len(macs)}, 실패 {', '.join(failures)}"
            )
        return text

    def stop_to_normal(self, macs, normal_payload, label="STOP"):
        """STOP 3단계(수신기 V057 _mqtt_collection_stop과 같음).

        MAC마다 S/{MAC}에 QoS 1, retain으로 차례로 보낸다.
        1. {"COMMPATH":"0","SENSORTYPE":"0"}  collection 종료
        2. 빈 메시지                          브로커의 retain 지우기
        3. normal 설정(SENSORTYPE 1 또는 2)    센서가 다음에 깨어나면 normal 측정으로 돌아간다
        단계마다 PUBACK을 PUBACK_TIMEOUT_S초까지 기다린다. 한 단계라도 실패하면 거기서 멈추고 오류를 낸다
        (다음 단계·다음 MAC으로 가지 않는다). 브로커에는 마지막 normal 설정이 남으므로
        sensor_config에도 그 normal JSON을 남긴다.

        이 함수는 최대 MAC 수 × 3 × 10초 동안 기다린다. HTTP 라우트는 스레드 풀(def)에서 부른다.
        """
        self._require_connection(macs)
        stop_text = command_text({"COMMPATH": "0", "SENSORTYPE": "0"})
        normal_text = command_text(normal_payload)
        steps = (
            ("STOP retained", stop_text.encode("utf-8")),
            ("retained 삭제", b""),
            ("normal retained", normal_text.encode("utf-8")),
        )
        for mac in macs:
            topic = f"S/{mac}"
            for step, body in steps:
                info = self.client.publish(topic, body, qos=COMMAND_QOS, retain=True)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError(f"{mac} {step} 전송 오류 rc={info.rc}")
                try:
                    info.wait_for_publish(timeout=PUBACK_TIMEOUT_S)
                except Exception as exc:
                    raise RuntimeError(f"{mac} {step} PUBACK 대기 실패: {exc}") from exc
                if not info.is_published():
                    raise RuntimeError(f"{mac} {step} PUBACK 시간 초과({PUBACK_TIMEOUT_S}초)")
                self.log(f"{label} {topic} {step} QoS={COMMAND_QOS} retain=True PUBACK 확인")
            self._save_config(mac, normal_payload, f"{label} → normal")
        self.log(f"{label} 완료: {len(macs)}개 센서, normal 복귀 {normal_text}")
        return f"{stop_text}\n(retain 삭제)\n{normal_text}"

    def state(self, device="", have=""):
        """연결 상태, MAC 목록, 최신 그래프를 한 덩어리로 돌려준다.

        have: 화면이 지금 그려 둔 그래프의 지문(직전 응답의 latest_key).
        2026-11-26(LTE 데이터 절약): 그래프(latest)는 RAW 8192점·FFT·PSD라 응답의 거의 전부(약 417KB)인데,
        센서는 1시간·분 단위로 보내므로 1초 갱신 대부분은 같은 그래프를 다시 보내고 있었다.
        그래서 매번 그래프를 계산한 뒤 그 내용의 지문(MD5)을 latest_key로 함께 보내고,
        화면이 보낸 have와 같으면 latest를 빼고 latest_same=True만 보낸다(상태·로그·MAC 목록은 늘 보냄).
        지문은 '계산 결과' 자체로 만들기 때문에 새 패킷, 장치 선택, 그래프 옵션, 설정의 g/LSB·SRSS 구간
        변경 등 그래프가 달라지는 이유가 무엇이든 지문이 바뀌어 다시 보낸다(놓치는 경우가 없다).
        """
        with self.lock:
            names = list(self.records)
            target = device if device in self.records else ""
            if not target:
                target = self.latest_device if self.latest_device in self.records else (names[-1] if names else "")
            snapshot = {
                axis: dict(record)
                for axis, record in self.records.get(target, {}).items()
            } if target else {}
            window = self.window
            options = self.view_options
            payload = {
                "connected": self.connected,
                "mode": self.mode,
                "origin": self.origin,
                # 지금(또는 마지막으로) 붙은 브로커. 수신 모니터 화면이 입력칸 기본값 대신 이것을 보여 준다.
                # active = 연결 시도 중이거나 연결됨(자동 연결이 브로커를 기다리는 중이면 connected는 False).
                "host": self.host,
                "port": self.port,
                "topic": self.topic,
                "qos": self.qos,
                "active": self.client is not None,
                "messages": self.messages,
                "logs": list(self.logs),
                "devices": list(self.devices),
                "arrivals": {key: dict(value) for key, value in self.device_stats.items()},
                "graph_devices": names,
                "latest_device": self.latest_device,
                "configs": self.configs,
                "config_times": dict(self.config_times),
                "window": window,
                "csv_enabled": self.csv.enabled,
                "csv_dir": str(self.csv.session_dir or ""),
                "csv_saved": self.csv.saved,
                "csv_errors": self.csv.errors,
                "profiles": {"ADXL": list(mon.ADXL_PROFILE_LABELS), "PIEZO": list(mon.PIEZO_PROFILE_LABELS)},
            }
        # 표시 이름은 SQLite에 있다. MonitorService.lock을 푼 뒤에 읽어 수신 스레드를 막지 않는다.
        try:
            payload["names"] = self.store.display_names()
        except Exception:
            payload["names"] = {}
        latest = {target: self._device_views(snapshot, window, options)} if target else {}
        # 지문: 장치 이름 + 계산된 그래프 전체. numpy 값이 섞여도 default=str로 글자로 만들어 비교만 한다.
        key = hashlib.md5(json.dumps(latest, default=str, separators=(",", ":")).encode("utf-8")).hexdigest() if latest else ""
        payload["latest_key"] = key
        if key and have == key:
            payload["latest_same"] = True  # 화면에 있는 그래프 그대로 두면 된다
        else:
            payload["latest"] = latest
        return payload

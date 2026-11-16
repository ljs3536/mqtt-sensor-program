"""MQTT 수신, 최신 파형, CSV·SQLite 저장.

web 패키지를 import하지 않는다.
"""

from __future__ import annotations

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


def _sample_index(length, limit=1800):
    """긴 파형에서 고르게 고른 인덱스를 만든다."""
    if length <= limit:
        return np.arange(length)
    return np.linspace(0, length - 1, limit).astype(int)


def _picked(values, index):
    """고른 인덱스의 값을 소수 6자리로 꺼낸다."""
    data = np.asarray(values, dtype=np.float64)
    if data.size == 0:
        return []
    return [round(float(data[i]), 6) for i in index]


def _sensor_config(sensor, period, profile, stop=False, sensor_type=None, g_range="16", piezo_gain="30"):
    """모니터 명령 JSON. COMMPATH는 0으로 고정한다.

    0은 종료, 1/3은 PIEZO normal/collection, 2/4는 MEMS normal/collection이다.
    G Range와 PIEZO 게인은 화면 환산 LSB와 별개로 센서에 보낸다.
    """
    code = str(sensor_type).strip() if sensor_type is not None else ""
    if stop or code == "0":
        return {"COMMPATH": "0", "SENSORTYPE": "0"}
    if code not in ("1", "2", "3", "4"):
        code = "1" if str(sensor).upper() == "PIEZO" else "2"
    period_text = str(int(period))
    if int(period) < 0:
        raise ValueError("측정 주기는 0 이상의 정수(초)여야 합니다")
    payload = {"COMMPATH": "0", "SENSORTYPE": code, "SENSING_PERIOD": period_text}
    if code in ("1", "3"):
        rate, count = mon.PIEZO_PROFILE_MAP[profile]
        gain = str(piezo_gain).strip()
        if not gain.isdigit():
            raise ValueError("PIEZO_GAIN은 0 이상의 정수여야 합니다")
        payload.update({
            "PIEZO_SAMPLE_RATE": rate,
            "PIEZO_SAMPLE_COUNT": count,
            "PIEZO_GAIN": gain,
        })
        return payload
    rate, count = mon.ADXL_PROFILE_MAP[profile]
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
        self.logs = deque(maxlen=400)
        self.messages = 0
        self.records = {}
        self.devices = []
        self.device_stats = {}
        self.latest_device = ""
        self.configs = {}
        self.connected = False
        self.mode = ""
        self.window = "전체"
        self.csv = CsvWriter()
        self.view_options = None
        self.host = "127.0.0.1"
        self.port = 1883
        self.topic = "V/#,D/#"

    def log(self, text):
        """시각을 붙여 로그 한 줄을 남긴다."""
        self.logs.append(f"{time.strftime('%H:%M:%S')} {text}")

    def _remember(self, topic, obj):
        """측정 패킷을 디코드해 최신 파형으로 두고 CSV와 SQLite에 넘긴다."""
        if str(topic).startswith("S/"):
            mac = str(topic).split("/", 1)[1]
            self.configs[mac] = obj
            if mac not in self.devices:
                self.devices.append(mac)
            self.log(f"설정 수신 S/{mac}")
            return
        device = str(obj.get("dvicMacAddr") or obj.get("deviceId") or "").strip()
        if device and device not in self.devices:
            self.devices.append(device)
        classified = mon.classify_vibration(obj)
        if classified is None and mon.classify_ultrasonic(obj, topic):
            classified = ("ULTRASONIC", None)
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
        stat["last"] = datetime.now().strftime("%H:%M:%S")
        self.latest_device = device_id
        self._save_csv(obj, kind, axis, sensor_hex, samples if kind != "ULTRASONIC" else record["y"], meta if kind == "ULTRASONIC" else None)
        rms = ""
        if record["kind"] == "waveform" and record["physical"]:
            g = record["samples"] * gen.read_mems_g_per_lsb()
            rms = f" gRMS={float(np.sqrt(np.mean(np.square(g)))):.6g}"
        self.log(f"{record['device']} {kind} N={len(samples) if kind != 'ULTRASONIC' else len(record['y'])}{rms}")

    def _save_csv(self, obj, kind, axis, sensor_hex, decoded, meta):
        """같은 패킷을 CSV 큐와 SQLite packet에 넣는다. 통계는 계산하지 않는다."""
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
                "x": _take(record["x"], index),
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
            "t": _take(t, time_index),
            "y": _take(y, time_index),
            "rms_t": _take(t, rms_index),
            "rms": _take(rms, rms_index),
            "fft_f": fft_x,
            "fft_y": fft_y,
            "rms_value": round(full, 6),
            "n": int(y.size),
            "window": _window_count(window, y.size),
            "fs": record["fs"],
            "unit": shown["unit"],
            "fft_unit": shown["fft_unit"],
            "x_label": "Time (s)",
            "fft_x_label": "Frequency (Hz)",
            "formula": shown["formula"],
            "info": shown["info"],
            "fft_log_x": shown["fft_log_x"],
            "fft_log_y": shown["fft_log_y"],
            "received_at": record["received_at"],
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
            "t": _take(t, time_index),
            "y": _take(y, time_index),
            "fft_f": fft_x,
            "fft_y": fft_y,
            "rms_value": round(full, 6),
            "n": int(y.size),
            "window": _window_count(window, y.size),
            "fs": record["fs"],
            "unit": shown["unit"],
            "fft_unit": shown["fft_unit"],
            "x_label": "Time (s)",
            "fft_x_label": "Frequency (Hz)",
            "formula": shown["formula"],
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
                views[axis] = self._view(record, window, options)
        if "_srss" in shared:
            views["_srss"] = shared["_srss"]
        if "_gn" in shared:
            views["_gn"] = shared["_gn"]
        if "_srss_limit" in shared:
            views["_srss_limit"] = shared["_srss_limit"]
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
        """MQTT 메시지 하나를 해석한다. 한 프레임 오류로 수신을 멈추지 않는다."""
        with self.lock:
            self.messages += 1
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

    def connect_mqtt(self, host, port, topic, csv_enabled=True):
        """브로커에 붙고 토픽을 구독한다. CSV가 켜지면 저장 폴더를 연다."""
        self.disconnect()
        client = mqtt_client(f"web-monitor-{int(time.time())}")
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.connect(host, int(port), keepalive=30)
        client.loop_start()
        topics = [item.strip() for item in str(topic).replace(";", ",").split(",") if item.strip()]
        if "S/#" not in topics:
            topics.append("S/#")
        client.subscribe([(item, 0) for item in topics])
        self.csv.enabled = bool(csv_enabled)
        folder = self.csv.start_session() if csv_enabled else None
        self.client = client
        self.connected = True
        self.mode = "MQTT"
        self.host, self.port, self.topic = host, int(port), topic
        self.log(f"MQTT 연결 {host}:{port} 구독={', '.join(topics)}")
        if folder:
            self.log(f"CSV 저장 시작 {folder}")

    def _on_disconnect(self, client, _userdata, *args):
        """이 클라이언트의 연결이 끊기면 수신 상태를 끈다."""
        if client is not self.client:
            return
        reason = args[0] if args else ""
        self.connected = False
        self.mode = ""
        self.log(f"MQTT 연결 끊김 {reason}")

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

    def publish_config(self, macs, payload, label, retain=True):
        """S/{MAC} 로 설정 JSON을 보낸다. retain 명령은 끝에 CR LF를 붙인다."""
        if self.client is None or not self.connected:
            raise ValueError("먼저 MQTT에 연결하세요")
        if not macs:
            raise ValueError("설정할 센서 MAC을 선택하세요")
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if retain:
            text += "\r\n"
        for mac in macs:
            info = self.client.publish(f"S/{mac}", text.encode("utf-8"), qos=0, retain=retain)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"S/{mac} 전송 실패 rc={info.rc}")
            self.log(f"{label} S/{mac} retain={retain} {text.strip()}")
            if retain:
                try:
                    self.store.save_sensor_config(mac, payload, label)
                except Exception as exc:
                    self.log(f"설정 JSON 저장 실패 {mac}: {exc}")
        return text.strip()

    def state(self, device=""):
        """연결 상태, MAC 목록, 최신 그래프를 한 덩어리로 돌려준다."""
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
                "messages": self.messages,
                "logs": list(self.logs),
                "devices": list(self.devices),
                "arrivals": {key: dict(value) for key, value in self.device_stats.items()},
                "graph_devices": names,
                "latest_device": self.latest_device,
                "configs": self.configs,
                "window": window,
                "csv_enabled": self.csv.enabled,
                "csv_dir": str(self.csv.session_dir or ""),
                "csv_saved": self.csv.saved,
                "csv_errors": self.csv.errors,
                "profiles": {"ADXL": list(mon.ADXL_PROFILE_LABELS), "PIEZO": list(mon.PIEZO_PROFILE_LABELS)},
            }
        payload["latest"] = {target: self._device_views(snapshot, window, options)} if target else {}
        return payload

# Local web UI for the MQTT replay generator and the piezo/ADXL monitor.
# Run from the project root:
#   .venv\Scripts\python.exe -m uvicorn web.server:app --host 127.0.0.1 --port 8080

from __future__ import annotations

import csv
import json
import queue
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import analysis
from web import core as gen
from web import core as mon

import paho.mqtt.client as mqtt

def _mqtt_client(client_id):
    try:
        return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (TypeError, AttributeError):
        return mqtt.Client(client_id=client_id)

WEB = Path(__file__).resolve().parent
UPLOADS = WEB / "uploads"
UPLOADS.mkdir(exist_ok=True)
DATA_CSV = ROOT / "data_csv"
DATA_CSV.mkdir(exist_ok=True)

app = FastAPI(title="sensor-program")
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")


def _clean(values):
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return arr
    return np.where(np.isfinite(arr), arr, 0.0)


def _rounded(values):
    return [round(float(value), 6) for value in values]


def _envelope_index(values, limit=16384):
    """Keep each bucket's min and max so peaks survive display thinning."""
    data = _clean(values)
    count = data.size
    if count <= limit:
        return np.arange(count)
    buckets = max(1, limit // 2)
    edges = np.linspace(0, count, buckets + 1).astype(int)
    chosen = []
    for start, stop in zip(edges[:-1], edges[1:]):
        if stop <= start:
            continue
        segment = data[start:stop]
        low = start + int(np.argmin(segment))
        high = start + int(np.argmax(segment))
        if low > high:
            low, high = high, low
        chosen.append(low)
        if high != low:
            chosen.append(high)
    return np.asarray(chosen, dtype=int)


def _take(values, index):
    data = _clean(values)
    if data.size == 0:
        return []
    return _rounded(data[index])


def _pack_pair(x_values, y_values):
    y_data = _clean(y_values)
    x_data = _clean(x_values)
    count = min(x_data.size, y_data.size)
    if count == 0:
        return [], []
    index = _envelope_index(y_data[:count])
    return _take(x_data[:count], index), _take(y_data[:count], index)


def _pack_curves(curves):
    packed = {
        "kind": curves["kind"],
        "x_label": curves.get("x_label") or "",
        "y_label": curves.get("y_label") or "",
        "fft_x_label": curves.get("fft_x_label") or "",
        "fft_label": curves.get("fft_label") or "",
        "info": curves.get("info") or "",
        "formula": curves.get("formula") or "",
        "avg_status": curves.get("avg_status") or "",
        "rotation": curves.get("rotation") or "",
        "fft_log_x": bool(curves.get("fft_log_x")),
        "fft_log_y": bool(curves.get("fft_log_y")),
        "series": {},
        "fft": {},
    }
    if curves["kind"] == "spectrum":
        packed["x"], packed["y"] = _pack_pair(curves.get("x"), curves.get("y"))
        return packed
    for axis, series in (curves.get("series") or {}).items():
        t_values, y_values = _pack_pair(series["t"], series["y"])
        packed["series"][axis] = {"t": t_values, "y": y_values}
    for axis, series in (curves.get("fft") or {}).items():
        f_values, y_values = _pack_pair(series["f"], series["y"])
        packed["fft"][axis] = {"f": f_values, "y": y_values}
    return packed


class ReplayService:
    def __init__(self):
        self.lock = threading.Lock()
        self.templates = {}
        self.index = {"PIEZO": 0, "ADXL": 0, "초음파": 0}
        self.paths = {}
        self.logs = deque(maxlen=300)
        self.sent = 0
        self.acked = 0
        self.dropped = 0
        self.counter = {"PIEZO": 0, "ADXL": 0, "초음파": 0}
        self.stop_event = threading.Event()
        self.threads = []
        self.client = None
        self.config = {
            "host": "127.0.0.1",
            "port": 1883,
            "qos": 1,
            "user": "",
            "password": "",
            "device": "S01",
            "mac": "TEST00000001",
            "rewrite_mac": False,
            "seq_auto": True,
            "jitter_on": False,
            "loss_on": False,
            "topics": {"PIEZO": "V/{mac}", "ADXL": "V/{mac}", "초음파": "D/{mac}"},
            "period": {"PIEZO": 320, "ADXL": 2560, "초음파": 10000},
            "auto_period": {"PIEZO": True, "ADXL": True},
            "jitter": {"PIEZO": 0, "ADXL": 0, "초음파": 0},
            "loss": {"PIEZO": 0, "ADXL": 0, "초음파": 0},
            "xyz_gap": 5,
            "tick_step": 0,
            "enabled": {"PIEZO": True, "ADXL": False, "초음파": False},
        }
        self.seq = gen.SequenceState()
        self.view_options = None

    def log(self, text):
        self.logs.append(f"{time.strftime('%H:%M:%S')} {text}")

    def store(self, sensor, path):
        if sensor == "PIEZO":
            items = gen.load_piezo_templates(path)
        elif sensor == "초음파":
            items = gen.load_ultrasonic_templates(path)
        elif sensor == "ADXL":
            axes = gen.load_adxl_templates(path)
            items = [dict(zip(("X", "Y", "Z"), row)) for row in zip(axes["X"], axes["Y"], axes["Z"])]
        else:
            raise ValueError("알 수 없는 센서")
        with self.lock:
            self.templates[sensor] = items
            self.index[sensor] = 0
            self.paths[sensor] = path
            self.log(f"[{sensor}] {len(items)}개 패킷 로드")
        return len(items)

    def store_adxl_csv(self, paths, recover_partial=True):
        items, report = gen.load_adxl_csv_xyz(
            paths, return_report=True, recover_partial=bool(recover_partial)
        )
        with self.lock:
            self.templates["ADXL"] = items
            self.index["ADXL"] = 0
            self.paths["ADXL"] = "XYZ CSV"
            self.log(
                f"[ADXL seq 정렬] {len(items)}세트, 불완전 {report['partial']}세트 | "
                f"원본 행={report['rows']} | 유효 축={report['axis_packets']} | "
                f"부분복구={report['recovered']} | 앞부분 복구={'ON' if recover_partial else 'OFF'}"
            )
            for warning in report["warnings"][:100]:
                self.log("[복구/제외] " + warning)
            if len(report["warnings"]) > 100:
                self.log(f"오류/제외 내역 {len(report['warnings']) - 100}건 추가 (화면은 100건만 표시)")
        return len(items)

    def preview(self, sensor, index, options=None):
        if options is not None:
            self.view_options = options
        with self.lock:
            data = self.templates.get(sensor) or []
            if not data:
                raise ValueError(f"{sensor} 파일을 먼저 선택하세요")
            index = max(0, min(len(data) - 1, int(index)))
            self.index[sensor] = index
            packet = data[index]
            packets = data
        curves = analysis.generator_curves(sensor, packet, packets, index, self.view_options)
        packed = _pack_curves(curves)
        packed.update({"sensor": sensor, "index": index + 1, "count": len(data)})
        return packed

    def _on_publish(self, *_args):
        with self.lock:
            self.acked += 1

    def _connect(self, cfg):
        client = _mqtt_client(f"web-replay-{int(time.time())}")
        if cfg["user"]:
            client.username_pw_set(cfg["user"], cfg["password"])
        client.on_publish = self._on_publish
        client.connect(cfg["host"], int(cfg["port"]), keepalive=30)
        client.loop_start()
        self.client = client

    def test_broker(self, cfg):
        if any(thread.is_alive() for thread in self.threads):
            raise ValueError("전송 중에는 연결 테스트를 할 수 없습니다")
        done = threading.Event()
        result = {}

        def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
            try:
                result["ok"] = int(reason_code) == 0
            except (TypeError, ValueError):
                result["ok"] = str(reason_code).lower() in ("0", "success")
            result["reason"] = str(reason_code)
            done.set()

        client = _mqtt_client(f"web-replay-test-{int(time.time())}")
        if cfg.get("user"):
            client.username_pw_set(cfg.get("user") or "", cfg.get("password") or "")
        client.on_connect = on_connect
        try:
            client.connect(cfg.get("host") or "127.0.0.1", int(cfg.get("port") or 1883), keepalive=30)
            client.loop_start()
            if not done.wait(4) or not result.get("ok"):
                raise ConnectionError(result.get("reason") or "MQTT Broker 연결 시간 초과")
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        self.log(f"연결 테스트 성공 {cfg.get('host')}:{cfg.get('port')}")
        return {"ok": True}

    def _publish(self, cfg, sensor, obj, axis=None):
        if self.stop_event.is_set():
            return
        if sensor == "ADXL" and obj.get("_partial_recovered"):
            return
        if cfg["loss_on"] and random.random() * 100 < float(cfg["loss"][sensor]):
            with self.lock:
                self.dropped += 1
            return
        packet = {k: v for k, v in obj.items() if not str(k).startswith("_")}
        if cfg["rewrite_mac"]:
            packet["dvicMacAddr"] = cfg["mac"].strip()
        if sensor in ("PIEZO", "ADXL") and cfg["seq_auto"]:
            packet["seq"] = self.seq.next_seq(axis or "PIEZO")
        mac = cfg["mac"].strip() if cfg["rewrite_mac"] else str(packet.get("dvicMacAddr") or cfg["mac"]).strip()
        topic = cfg["topics"][sensor].replace("{device}", cfg["device"].strip()).replace("{mac}", mac)
        info = self.client.publish(
            topic,
            json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            qos=int(cfg["qos"]),
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish 실패 rc={info.rc}")
        with self.lock:
            self.sent += 1

    def _worker(self, sensor, cfg, start):
        try:
            data = self.templates[sensor]
            idx = start
            total = len(data)
            base = float(cfg["period"][sensor])
            if cfg["auto_period"].get(sensor):
                item = next(iter(data[start].values())) if sensor == "ADXL" else data[start]
                auto = gen.period_ms_from_payload(item)
                if auto > 0:
                    base = auto
            gap = float(cfg["xyz_gap"])
            jit = float(cfg["jitter"][sensor])
            tickstep = int(cfg["tick_step"])
            count = 0
            tickbase = None
            self.log(f"[{sensor}] 패킷 {start + 1}/{total}부터 반복, 주기 {base:g} ms")
            next_at = time.perf_counter()
            while not self.stop_event.is_set():
                wait = next_at - time.perf_counter()
                if wait > 0 and self.stop_event.wait(wait):
                    break
                obj = data[idx]
                if sensor == "ADXL":
                    for axis in ("X", "Y", "Z"):
                        if self.stop_event.is_set() or axis not in obj:
                            continue
                        self._publish(cfg, sensor, obj[axis], axis)
                        if axis != "Z" and gap > 0 and self.stop_event.wait(gap / 1000):
                            break
                else:
                    payload = obj
                    if sensor == "초음파" and tickstep:
                        payload = dict(obj)
                        if tickbase is None:
                            try:
                                tickbase = int(payload.get("tick", "0"))
                            except Exception:
                                tickbase = 0
                        payload["tick"] = str(tickbase + count * tickstep)
                    self._publish(cfg, sensor, payload)
                count += 1
                with self.lock:
                    self.counter[sensor] = count
                idx = idx + 1 if idx + 1 < total else start
                interval = base + (random.uniform(-jit, jit) if cfg["jitter_on"] else 0)
                next_at += max(0.1, interval) / 1000
                if time.perf_counter() - next_at > max(base / 1000, 0.5):
                    next_at = time.perf_counter()
        except Exception as exc:
            self.log(f"[{sensor}] 오류: {exc}")
            self.stop_event.set()

    def start(self, cfg):
        if any(t.is_alive() for t in self.threads):
            raise ValueError("이미 전송 중입니다")
        active = [name for name, on in cfg["enabled"].items() if on]
        if not active:
            raise ValueError("전송할 센서를 선택하세요")
        for name in active:
            if name not in self.templates:
                raise ValueError(f"{name} 파일을 먼저 불러오세요")
        self.config = cfg
        self.stop_event.clear()
        self.sent = 0
        self.acked = 0
        self.dropped = 0
        self.seq = gen.SequenceState()
        self._connect(cfg)
        starts = {name: self.index[name] for name in active}
        self.threads = []
        for name in active:
            thread = threading.Thread(target=self._worker, args=(name, cfg, starts[name]), daemon=True)
            thread.start()
            self.threads.append(thread)
        self.log("전송 시작: " + ", ".join(active))

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.threads = []
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self.log("전송 중지")

    def status(self):
        running = any(t.is_alive() for t in self.threads)
        return {
            "running": running,
            "sent": self.sent,
            "acked": self.acked,
            "dropped": self.dropped,
            "counter": dict(self.counter),
            "loaded": {name: len(items) for name, items in self.templates.items()},
            "index": {name: value + 1 for name, value in self.index.items()},
            "logs": list(self.logs),
        }


def _window_count(value, length):
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
    if length <= limit:
        return np.arange(length)
    return np.linspace(0, length - 1, limit).astype(int)


def _picked(values, index):
    data = np.asarray(values, dtype=np.float64)
    if data.size == 0:
        return []
    return [round(float(data[i]), 6) for i in index]


def _sensor_config(sensor, period, profile, stop=False):
    period_text = str(int(period))
    if int(period) < 0:
        raise ValueError("측정 주기는 0 이상의 정수(초)여야 합니다")
    if stop:
        return {"COMMPATH": "0", "SENSORTYPE": "0", "SENSING_PERIOD": period_text}
    sensor = str(sensor).upper()
    if sensor == "PIEZO":
        rate, count = mon.PIEZO_PROFILE_MAP[profile]
        return {
            "COMMPATH": "0",
            "SENSORTYPE": "1",
            "SENSING_PERIOD": period_text,
            "PIEZO_SAMPLE_RATE": rate,
            "PIEZO_SAMPLE_COUNT": count,
        }
    if sensor == "ADXL":
        rate, count = mon.ADXL_PROFILE_MAP[profile]
        code = mon.ADXL_RATE_TO_CODE.get(str(rate))
        if not code:
            raise ValueError(f"ADXL Sample Rate 코드 변환 실패: {rate}")
        return {
            "COMMPATH": "0",
            "SENSORTYPE": "2",
            "SENSING_PERIOD": period_text,
            "MEMS_SAMPLE_RATE": code,
            "MEMS_SAMPLE_COUNT": count,
        }
    raise ValueError("센서 종류는 PIEZO 또는 ADXL이어야 합니다")


class CsvWriter:
    def __init__(self):
        self.queue = queue.Queue()
        self.session_dir = None
        self.saved = 0
        self.errors = 0
        self.enabled = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def start_session(self):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = DATA_CSV / stamp
        path.mkdir(parents=True, exist_ok=True)
        self.session_dir = path
        self.saved = 0
        self.errors = 0
        self.queue.put({"_reset": True})
        return path

    def close_session(self):
        self.session_dir = None
        self.queue.put({"_close": True})

    def submit(self, item):
        if self.enabled and self.session_dir is not None:
            self.queue.put(item)

    def _loop(self):
        files = {}
        writers = {}
        counters = {}

        def writer_for(path, header):
            key = str(path)
            if key not in files:
                is_new = not path.exists() or path.stat().st_size == 0
                handle = path.open("a", newline="", encoding="utf-8-sig")
                files[key] = handle
                writers[key] = csv.writer(handle)
                if is_new:
                    writers[key].writerow(header)
                    handle.flush()
            return writers[key], files[key]

        def close_all():
            for handle in files.values():
                try:
                    handle.flush()
                    handle.close()
                except Exception:
                    pass
            files.clear()
            writers.clear()
            counters.clear()

        common = [
            "packet_no", "received_at", "source", "dvicMacAddr", "gwMacAddr",
            "sensor_type", "axis", "seq", "tick", "batteryRmin", "samplerate",
            "numofsample", "payload_bytes",
        ]
        hex_header = common + ["sensorData_bytes", "sensorData_hex"]
        decoded_header = common + ["decoded_sample_count", "decoded_samples"]
        ultrasonic_header = common + [
            "minKhz", "intervalHz", "freq_start_kHz", "freq_end_kHz",
            "decoded_sample_count", "decoded_samples",
        ]
        while not self._stop.is_set():
            try:
                item = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if item.get("_reset") or item.get("_close"):
                    close_all()
                    continue
                session = self.session_dir
                if session is None:
                    continue
                session.mkdir(parents=True, exist_ok=True)
                device = mon.safe_filename(item["device"])
                kind = mon.safe_filename(item["kind"])
                key = (item["device"], item["kind"])
                counters[key] = counters.get(key, 0) + 1
                base = [
                    counters[key], item["received_at"], "MQTT", item["device"], item["gw"],
                    item["kind"], item["axis"], item["seq"], item["tick"], item["battery"],
                    item["sample_rate"], item["num_samples"], item["payload_bytes"],
                ]
                hex_path = session / f"{device}_{kind}_sensorData_hex.csv"
                hex_writer, hex_file = writer_for(hex_path, hex_header)
                sensor_data = item["sensor_data"]
                hex_writer.writerow(base + [len(sensor_data) // 2, sensor_data])
                hex_file.flush()
                decoded_path = session / f"{device}_{kind}_decoded_samples.csv"
                if item["kind"] == "ULTRASONIC":
                    row_writer, row_file = writer_for(decoded_path, ultrasonic_header)
                    meta = item["meta"]
                    text = ",".join(format(float(value), ".9g") for value in item["decoded"])
                    row_writer.writerow(base + [
                        meta["minKhz"], meta["intervalHz"],
                        f"{meta['freq_start_khz']:.9g}", f"{meta['freq_end_khz']:.9g}",
                        len(item["decoded"]), text,
                    ])
                else:
                    row_writer, row_file = writer_for(decoded_path, decoded_header)
                    text = ",".join(str(int(value)) for value in item["decoded"])
                    row_writer.writerow(base + [len(item["decoded"]), text])
                row_file.flush()
                self.saved += 1
            except Exception:
                self.errors += 1
            finally:
                self.queue.task_done()
        close_all()


class MonitorService:
    def __init__(self):
        self.lock = threading.Lock()
        self.client = None
        self.logs = deque(maxlen=400)
        self.messages = 0
        self.records = {}
        self.devices = []
        self.device_stats = {}
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
        self.logs.append(f"{time.strftime('%H:%M:%S')} {text}")

    def _remember(self, topic, obj):
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
            self.log(f"디코드 실패 {device}/{kind}: {exc}")
            return
        record.update({"device": device or topic, "axis": axis, "sensor": kind, "received_at": datetime.now().isoformat(timespec="milliseconds")})
        device_id = record["device"]
        self.records.setdefault(device_id, {})[axis] = record
        stat = self.device_stats.setdefault(device_id, {"count": 0, "last": ""})
        stat["count"] += 1
        stat["last"] = datetime.now().strftime("%H:%M:%S")
        self._save_csv(obj, kind, axis, sensor_hex, samples if kind != "ULTRASONIC" else record["y"], meta if kind == "ULTRASONIC" else None)
        rms = ""
        if record["kind"] == "waveform" and record["physical"]:
            g = record["samples"] * gen.read_mems_g_per_lsb()
            rms = f" gRMS={float(np.sqrt(np.mean(np.square(g)))):.6g}"
        self.log(f"{record['device']} {kind} N={len(samples) if kind != 'ULTRASONIC' else len(record['y'])}{rms}")

    def _save_csv(self, obj, kind, axis, sensor_hex, decoded, meta):
        device = str(obj.get("dvicMacAddr") or obj.get("deviceId") or "UNKNOWN").strip()
        self.csv.submit({
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
        })

    def _view(self, record, window, options):
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
        adxl = {
            axis: (record["samples"], record["fs"])
            for axis, record in snapshot.items()
            if record.get("kind") == "waveform" and record.get("physical") and axis in ("X", "Y", "Z")
        }
        shared = analysis.monitor_adxl_set(adxl, options) if adxl else {}
        views = {}
        for axis, record in snapshot.items():
            if axis in shared:
                views[axis] = self._pack_shown(record, shared[axis], window)
            else:
                views[axis] = self._view(record, window, options)
        return views

    def _on_message(self, _client, _userdata, msg):
        with self.lock:
            self.messages += 1
        try:
            obj = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            self.log(f"{msg.topic} JSON 아님 ({len(msg.payload)} bytes)")
            return
        if isinstance(obj, dict):
            with self.lock:
                self._remember(str(msg.topic), obj)

    def connect_mqtt(self, host, port, topic, user, password, csv_enabled=True):
        self.disconnect()
        client = _mqtt_client(f"web-monitor-{int(time.time())}")
        if user:
            client.username_pw_set(user, password)
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
        if client is not self.client:
            return
        reason = args[0] if args else ""
        self.connected = False
        self.mode = ""
        self.log(f"MQTT 연결 끊김 {reason}")

    def disconnect(self):
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
        return text.strip()

    def state(self, device=""):
        with self.lock:
            names = list(self.records)
            target = device if device in self.records else (names[0] if names else "")
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

replay = ReplayService()
monitor = MonitorService()


@app.get("/")
def home():
    return FileResponse(WEB / "index.html")


@app.get("/generator")
def generator_page():
    return FileResponse(WEB / "generator.html")


@app.get("/monitor")
def monitor_page():
    return FileResponse(WEB / "index.html")


@app.get("/archive")
def archive_page():
    return RedirectResponse("/generator")


@app.post("/api/generator/upload")
async def upload_sensor(sensor: str = Form(...), file: UploadFile = File(...)):
    if sensor not in ("PIEZO", "ADXL", "초음파"):
        raise HTTPException(400, "센서는 PIEZO, ADXL, 초음파 중 하나여야 합니다")
    dest = UPLOADS / f"{sensor}_{Path(file.filename or 'data.txt').name}"
    dest.write_bytes(await file.read())
    try:
        count = replay.store(sensor, str(dest))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"sensor": sensor, "count": count, "preview": replay.preview(sensor, 0)}


def _form_flag(value, default=True):
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() not in {"0", "false", "off", "no"}


@app.post("/api/generator/upload-adxl-csv")
async def upload_adxl_csv(
    x: UploadFile = File(...),
    y: UploadFile = File(...),
    z: UploadFile = File(...),
    recover_partial: str = Form("true"),
):
    paths = {}
    for axis, item in (("X", x), ("Y", y), ("Z", z)):
        dest = UPLOADS / f"ADXL_{axis}_{Path(item.filename or axis).name}"
        dest.write_bytes(await item.read())
        paths[axis] = str(dest)
    recover = _form_flag(recover_partial, default=True)
    try:
        count = replay.store_adxl_csv(paths, recover_partial=recover)
        preview = replay.preview("ADXL", 0)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"count": count, "sensor": "ADXL", "recover_partial": recover, "preview": preview}


@app.get("/api/generator/preview")
def generator_preview(sensor: str, index: int = 1):
    try:
        return replay.preview(sensor, index - 1)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/generator/preview")
async def generator_preview_post(body: dict):
    try:
        return replay.preview(body.get("sensor"), int(body.get("index") or 1) - 1, body.get("options"))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/generator/test")
async def generator_test(body: dict):
    try:
        return replay.test_broker(body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/generator/start")
async def generator_start(body: dict):
    try:
        replay.start(body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return replay.status()


@app.post("/api/generator/stop")
def generator_stop():
    replay.stop()
    return replay.status()


@app.get("/api/generator/status")
def generator_status():
    return replay.status()


@app.post("/api/monitor/connect")
async def monitor_connect(body: dict):
    try:
        monitor.connect_mqtt(
            body.get("host") or "127.0.0.1",
            int(body.get("port") or 1883),
            body.get("topic") or "V/#,D/#",
            body.get("user") or "",
            body.get("password") or "",
            csv_enabled=body.get("csv_enabled", True),
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return monitor.state()


@app.post("/api/monitor/disconnect")
def monitor_disconnect():
    monitor.disconnect()
    monitor.log("연결 해제")
    return monitor.state()


@app.get("/api/monitor/state")
def monitor_state(device: str = ""):
    return monitor.state(device)


@app.post("/api/monitor/view")
async def monitor_view(body: dict):
    monitor.view_options = body.get("options") or {}
    if body.get("window") is not None:
        monitor.window = str(body.get("window") or "전체")
    return monitor.state(str(body.get("device") or ""))


@app.post("/api/monitor/window")
async def monitor_window(body: dict):
    monitor.window = str(body.get("window") or "전체")
    return {"window": monitor.window}


@app.post("/api/monitor/command")
async def monitor_command(body: dict):
    action = str(body.get("action") or "")
    macs = [str(item).strip() for item in body.get("macs") or [] if str(item).strip()]
    sensor = str(body.get("sensor") or "ADXL").upper()
    period = body.get("period") or 1
    labels = mon.ADXL_PROFILE_LABELS if sensor == "ADXL" else mon.PIEZO_PROFILE_LABELS
    profile = body.get("profile") or labels[-1]
    try:
        if action == "query":
            if len(macs) != 1:
                raise ValueError("현재 설정 조회는 센서 MAC 1개만 선택하세요")
            payload = monitor.publish_config(macs, {"CONFIG": "?"}, "현재 설정 조회", retain=False)
        elif action == "stop":
            payload = monitor.publish_config(macs, _sensor_config(sensor, period, profile, stop=True), "STOP")
        elif action in ("start", "apply"):
            label = "START" if action == "start" else "설정 적용"
            payload = monitor.publish_config(macs, _sensor_config(sensor, period, profile), label)
        else:
            raise ValueError("명령은 start, stop, apply, query 중 하나여야 합니다")
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"payload": payload, "state": monitor.state()}


@app.get("/api/analysis/defaults")
def analysis_defaults():
    return analysis.default_options()


@app.post("/api/analysis/mems-scale")
async def save_mems_scale(body: dict):
    try:
        value = float(body.get("g_per_lsb"))
        gen.save_mems_g_per_lsb(value)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"g_per_lsb": value}


@app.post("/api/analysis/mems-correction")
async def save_mems_correction(body: dict):
    try:
        saved = gen.save_mems_correction(body.get("correction") or body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"correction": saved}



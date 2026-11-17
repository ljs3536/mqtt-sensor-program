"""파일에 있는 패킷을 MQTT로 반복 송신한다."""

from __future__ import annotations

import json
import random
import threading
import time
from collections import deque
from pathlib import Path

import paho.mqtt.client as mqtt

from web_db.src import analysis
from web_db.src import core as gen
from web_db.src.mqtt_util import mqtt_client
from web_db.src.view_pack import _pack_curves


class ReplayService:
    """파일에 있는 패킷을 고른 위치부터 MQTT로 반복 송신한다.

    브로커에는 연결 테스트와 전송 시작 때만 붙는다.
    status()는 메모리의 횟수와 로그만 돌려주고 브로커를 호출하지 않는다.
    """

    def __init__(self):
        """송신 목록, 주기, 브로커 상태를 비운 채로 준비한다."""
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
        """시각을 붙여 로그 한 줄을 남긴다."""
        self.logs.append(f"{time.strftime('%H:%M:%S')} {text}")

    def store(self, sensor, path, label=None):
        """파일에서 센서 패킷을 읽어 그 센서의 전송 목록으로 둔다."""
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
            self.paths[sensor] = label or Path(path).name
            self.log(f"[{sensor}] {len(items)}개 패킷 로드")
        return len(items)

    def store_adxl_csv(self, paths, recover_partial=True):
        """X/Y/Z CSV를 seq로 묶어 ADXL 전송 목록으로 둔다."""
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
        """고른 패킷의 RAW와 FFT 그래프 데이터를 만든다."""
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

    def fft_check(self, sensor):
        """고른 RAW가 FFT 후 IFFT로 복원되는지 검사한다."""
        sensor = str(sensor or "")
        if sensor == "초음파":
            return analysis.fft_check_unavailable(
                "초음파 H는 위상이 없는 스펙트럼이라 FFT/IFFT 검증을 할 수 없습니다."
            )
        with self.lock:
            data = list(self.templates.get(sensor) or [])
            index = int(self.index.get(sensor, 0))
        if sensor not in ("PIEZO", "ADXL") or not data:
            return analysis.fft_check_unavailable(
                "선택된 RAW 데이터가 없습니다. 초음파 H는 위상이 없는 스펙트럼입니다."
            )
        index = max(0, min(len(data) - 1, index))
        packet = data[index]
        records = []
        try:
            if sensor == "PIEZO":
                raw, fs = analysis.decode_wave("PIEZO", packet)
                if raw.size >= 2 and fs > 0:
                    records.append(("PIEZO", raw, fs))
            else:
                for axis in ("X", "Y", "Z"):
                    obj = packet.get(axis) if isinstance(packet, dict) else None
                    if not obj:
                        continue
                    raw, fs = analysis.decode_wave("ADXL", obj)
                    if raw.size >= 2 and fs > 0:
                        records.append((axis, raw, fs))
        except (ValueError, TypeError) as exc:
            return analysis.fft_check_unavailable(str(exc))
        title = "{} 패킷 {}/{}".format(sensor, index + 1, len(data))
        return analysis.fft_check_many(records, title)

    def _on_publish(self, *_args):
        """브로커 확인(PUBACK)이 오면 확인 횟수를 올린다."""
        with self.lock:
            self.acked += 1

    def _connect(self, cfg):
        """전송용 MQTT 클라이언트를 붙이고 수신 루프를 시작한다."""
        client = mqtt_client(f"web-replay-{int(time.time())}")
        client.on_publish = self._on_publish
        client.connect(cfg["host"], int(cfg["port"]), keepalive=30)
        client.loop_start()
        self.client = client

    def test_broker(self, cfg):
        """패킷을 보내지 않고 브로커 연결만 확인한다."""
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

        client = mqtt_client(f"web-replay-test-{int(time.time())}")
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
        """패킷 하나를 토픽으로 보낸다. 손실 옵션이면 버릴 수 있다."""
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
        """고른 위치부터 같은 센서 패킷을 주기로 반복 송신한다."""
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
        """켜진 센서의 반복 송신을 시작한다."""
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
        """송신 스레드를 멈추고 브로커 연결을 끊는다."""
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
        """보낸 수, 확인 수, 버린 수, 로그를 돌려준다. 브로커를 부르지 않는다."""
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

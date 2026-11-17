"""수신 패킷을 data_csv 폴더의 HEX CSV와 decoded CSV로 쓴다."""

from __future__ import annotations

import csv
import queue
import threading
from datetime import datetime

from web_db.src import core as mon
from web_db.src.paths import DATA_CSV


class CsvWriter:
    """수신 패킷을 data_csv/YYYYMMDD_HHMMSS 에 HEX와 decoded로 나눠 쓴다.

    측정 START와 무관하게, CSV 저장이 켜진 연결 세션 동안만 기록한다.
    축 패킷은 도착 즉시 그 축 파일에 쓰고, 행마다 flush한다.

    파일은 MAC과 종류마다 두 개다.
    - {MAC}_{종류}_sensorData_hex.csv: 받은 HEX 그대로
    - {MAC}_{종류}_decoded_samples.csv: 디코드한 샘플(초음파는 주파수 범위 포함)
    packet_no는 파일마다 1부터 센다. 그래서 X/Y/Z 파일의 packet_no는 짝이 아니다.
    수신 스레드가 디스크를 기다리지 않도록, 쓰기는 큐를 받는 별도 스레드 하나가 한다.
    """
    def __init__(self):
        """저장 큐와 백그라운드 기록 스레드를 만든다."""
        self.queue = queue.Queue()
        self.session_dir = None
        self.saved = 0
        self.errors = 0
        self.enabled = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def start_session(self):
        """연결 시각 이름의 폴더를 만들고 그 폴더에 CSV를 쓰기 시작한다."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = DATA_CSV / stamp
        path.mkdir(parents=True, exist_ok=True)
        self.session_dir = path
        self.saved = 0
        self.errors = 0
        self.queue.put({"_reset": True})
        return path

    def close_session(self):
        """열린 CSV를 닫고 저장 폴더를 비운다."""
        self.session_dir = None
        self.queue.put({"_close": True})

    def submit(self, item):
        """저장이 켜져 있고 연결 중이면 패킷을 CSV 기록 큐에 넣는다."""
        if self.enabled and self.session_dir is not None:
            self.queue.put(item)

    def _loop(self):
        """큐의 패킷을 HEX CSV와 decoded CSV로 나눠 쓴다."""
        files = {}
        writers = {}
        counters = {}

        def writer_for(path, header):
            """파일별 csv.writer를 한 번만 열어 재사용한다.

            파일이 새로 생기거나 비어 있으면 머리줄을 먼저 쓴다. 같은 폴더에 다시
            연결해도(같은 초에 재연결) 이어 쓰기만 하고 머리줄을 겹쳐 쓰지 않는다.
            """
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
            """열린 CSV를 모두 닫고 packet_no 카운터를 0으로 돌린다. 연결 시작·해제 때 부른다."""
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

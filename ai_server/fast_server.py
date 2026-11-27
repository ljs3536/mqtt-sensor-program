"""AI 판정 서버(FastAPI, 기본 포트 8000). LDMS ldt_ssad/fast_server.py를 가져와 sensor-program용으로 정리(2026-11-27).

실행(이 폴더의 '위' 폴더 = D:\\sensor-program 에서):
    ai_server\\.venv\\Scripts\\python.exe -m uvicorn ai_server.fast_server:app --host 127.0.0.1 --port 8000
    - 이 폴더 안에서 `uvicorn fast_server:app`로 띄워도 된다. 워커는 늘 위 폴더에서 `python -m ai_server.모듈`로 뜬다.
    - 전용 가상환경 ai_server\\.venv(Python 3.11, requirements.txt). 웹(루트 .venv)에는 torch가 없다.

구조:
- API 하나마다 워커 프로세스 묶음(WorkerPool)이 있다. 워커는 `python -m <패키지>.<모듈> --stdin`으로 떠서
  표준입력으로 JSON 한 줄을 받고, 표준출력으로 JSON 한 줄을 돌려준다(모델 계산은 워커 안에서).
- 서버를 켤 때 워커가 모두 뜬다(지금 설정: 추론 4 + 학습 4씩, 진단 1씩 = 18개). 워커가 죽어 있으면 다음 요청 때 다시 띄운다.
- 모델 파일은 요청마다 다시 읽는다(캐시 없음). 경로는 요청 JSON의 url·saveUrl로 받는다.

LDMS와 다른 점(2026-11-27):
- 패키지 이름 'ldt_ssad'를 코드에 박지 않았다. 이 파일이 든 폴더 이름(지금 ai_server)을 그대로 쓴다.
  폴더 이름을 바꿔도 여기는 고칠 필요가 없다(모듈끼리는 상대 import).
- 워커의 작업 폴더(cwd)를 이 폴더의 위 폴더로 고정했다. 어느 폴더에서 uvicorn을 띄워도 워커가 패키지를 찾는다.
- API·요청/응답 형식은 LDMS와 같다(LDMS도 같은 형식으로 부를 수 있게 둔다).
"""

from fastapi import FastAPI
import subprocess
import json
import sys
import threading
from pathlib import Path

app = FastAPI()

PYTHON_EXE = sys.executable
INFER_WORKERS = 4
TRAIN_WORKERS = 4
DIAG_WORKERS = 1

# 이 파일이 든 폴더 = 패키지(ai_server), 그 위 폴더 = 워커를 띄울 작업 폴더.
PACKAGE_DIR = Path(__file__).resolve().parent
PACKAGE = PACKAGE_DIR.name
ROOT_DIR = PACKAGE_DIR.parent


def create_worker(module_name):
    """워커 하나를 띄운다. module_name은 패키지 안 모듈 이름(예: 'test_vibe_SSAD_OW_ProtoNet')."""
    return subprocess.Popen(
        [PYTHON_EXE, "-m", f"{PACKAGE}.{module_name}", "--stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(ROOT_DIR),
    )

class WorkerPool:
    def __init__(self, module_name, pool_size):
        self.module_name = module_name
        self.workers = [create_worker(module_name) for _ in range(pool_size)]
        self.locks = [threading.Lock() for _ in range(pool_size)]
        self.index = 0
        self.pool_lock = threading.Lock()

    def process(self, payload):
        with self.pool_lock:
            idx = self.index
            self.index = (self.index + 1) % len(self.workers)

        with self.locks[idx]:
            worker = self.workers[idx]

            if worker.poll() is not None:
                worker = create_worker(self.module_name)
                self.workers[idx] = worker

            try:
                worker.stdin.write(json.dumps(payload) + "\n")
                worker.stdin.flush()

                # JSON이 나올 때까지 절대 포기하지 않고 계속 읽어들이는 무한 루프
                while True:
                    output = worker.stdout.readline().strip()

                    if not output: # 파이썬 프로세스가 죽었을 때만 종료
                        return {"ok": False, "error": "WORKER_DIED", "raw": ""}

                    # 정상적인 JSON 결과물을 찾은 경우에만 파싱 후 리턴
                    if output.startswith("{") and output.endswith("}"):
                        try:
                            return json.loads(output)
                        except Exception:
                            pass

                    # JSON이 아닌 문구 (GPU available: False 등)는 무시하고 다음 줄을 읽음
                    print(f"[Worker Ignore] {output}")

            except Exception as e:
                return {"ok": False, "error": str(e)}

# ===========================
# 안전한 워커 풀 초기화
# (/train·/vibe/train은 LDMS에서도 꺼져 있던 것이라 스크립트를 가져오지 않았다. 학습은 /update·/vibe/update)
# ===========================
pool_infer = WorkerPool("test_ultrasonic_SSAD_OW_ProtoNet", INFER_WORKERS)
pool_update = WorkerPool("update_UltraSonic_SSAD_OW_ProtoNet", TRAIN_WORKERS)
pool_vibe_infer = WorkerPool("test_vibe_SSAD_OW_ProtoNet", INFER_WORKERS)
pool_vibe_update = WorkerPool("update_vibe_SSAD_OW_ProtoNet", TRAIN_WORKERS)
pool_diag_train = WorkerPool("diag_train", DIAG_WORKERS)
pool_diag_infer = WorkerPool("diag_infer", DIAG_WORKERS)
# ===========================
# API 라우팅
# ===========================
@app.post("/infer")
def infer(data: dict):
    return pool_infer.process(data)

@app.post("/update")
def train(data: dict):
    return pool_update.process(data)

@app.post("/vibe/infer")
def vibe_infer(data: dict):
    return pool_vibe_infer.process(data)

@app.post("/vibe/update")
def vibe_update(data: dict):
    return pool_vibe_update.process(data)

@app.post("/diag/train")
def diag_train(data: dict):
    return pool_diag_train.process(data)

@app.post("/diag/infer")
def diag_infer(data: dict):
    return pool_diag_infer.process(data)

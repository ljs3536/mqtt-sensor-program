# sensor-program

센서(PIEZO / MEMS(ADXL) X·Y·Z / 초음파)의 MQTT 데이터를 받아 저장하고, 웹으로 데이터 분석과 통계를 확인한다.
AI로 이상 여부를 판정하는 기능은 추가 예정이다.

**Python 버전: 3.11** (세 폴더 모두)

| 폴더         | 역할                                                 |
| ------------ | ---------------------------------------------------- |
| `web_db/`    | 센서 웹 서버 (운영 중)                               |
| `ai_server/` | AI 판정 서버 (LDMS에서 가져옴, 웹과 붙이는 중)       |
| `program/`   | 데스크톱 프로그램 (PySide6 창 프로그램, 개발·검증용) |

## 1. 폴더

### `web_db/` — 센서 웹 서버 (FastAPI + SQLite, 컨테이너 안 포트 8060)

| 파일·폴더                         | 내용                                                                |
| --------------------------------- | ------------------------------------------------------------------- |
| `server.py`                       | 진입점 (`uvicorn web_db.server:app`)                                |
| `src/`                            | 서버 코드 (MQTT 수신·CSV/DB 저장·통계·API)                          |
| `front/`                          | 화면 (HTML/JS/CSS. 센서 현황, 수신 모니터, 데이터 분석, 통계, 설정) |
| `requirements.txt`                | 웹 서버 패키지 (Docker 이미지도 이것만 설치)                        |
| `Dockerfile`                      | 웹 이미지 (빌드 기준 폴더는 저장소 루트, 아래 3 참고)               |
| `mems_lsb_scale.json`             | MEMS 1 LSB당 g                                                      |
| `mems_frequency_sensitivity.json` | MEMS 주파수·감도 보정                                               |
| `sensor.sqlite`                   | 로컬 실행 때 DB (git에 안 올림)                                     |
| `site_files/`                     | 로컬 실행 때 평면도·센서 사진 (git에 안 올림)                       |

MEMS 보정 JSON 두 개는 웹이 읽고 쓰는 파일이다. 로컬 실행 때는 여기 있는 파일을 쓰고, Docker에서는 `/data`의 파일을 쓴다(없으면 이 파일이 기본값).

### `ai_server/` — AI 판정 서버 (FastAPI, 기본 포트 8000, torch CPU)

| 파일·폴더                                                                             | 내용                                                              |
| ------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `fast_server.py`                                                                      | 진입점. API마다 워커 프로세스 묶음을 띄워 JSON 한 줄씩 주고받는다 |
| `test_vibe_…` / `update_vibe_…`                                                       | 진동 판정 / 학습 (`/vibe/infer`, `/vibe/update`)                  |
| `test_ultrasonic_…` / `update_UltraSonic_…`                                           | 초음파 판정 / 학습 (`/infer`, `/update`)                          |
| `diag_train` / `diag_infer`                                                           | 진단 (`/diag/train`, `/diag/infer`)                               |
| `model_*.py`, `hoko_field.py`, `dataset.py`, `loss.py`, `taug.py`, `diag_spectrum.py` | 위 워커가 쓰는 모델·도구                                          |
| `requirements.txt`                                                                    | AI 서버 패키지 (버전 고정)                                        |
| `.venv/`                                                                              | AI 서버 전용 가상환경 (git에 안 올림)                             |

### `program/` — 데스크톱 프로그램 (최신 버전만 둔다)

| 파일·폴더                         | 내용                                          |
| --------------------------------- | --------------------------------------------- |
| `new_piezo_adc_….py`              | 센서 수신·분석 프로그램                       |
| `mqtt_wave_replay_generator_….py` | CSV 재생·MQTT 전송 프로그램                   |
| `mems_lsb_scale.json`             | MEMS 1 LSB당 g                                |
| `mems_frequency_sensitivity.json` | MEMS 주파수·감도 보정                         |
| `requirements.txt`                | 프로그램 패키지 (PySide6, pyqtgraph 등)       |
| `build/`, `dist/`                 | PyInstaller 결과 (옛 버전 exe. git에 안 올림) |
| `data_csv/`                       | 프로그램이 받은 CSV (생기면. git에 안 올림)   |

MEMS 보정 JSON 두 개는 프로그램이 `.py` 옆에서 읽고 쓰는 파일이다. 웹(`web_db/`)의 파일과 따로라 한쪽에서 바꿔도 다른 쪽은 그대로다.

### 그 밖

| 파일·폴더               | 내용                                                                               |
| ----------------------- | ---------------------------------------------------------------------------------- |
| `docker/make_bundle.py` | 웹 이미지 빌드 + 저장 → `docker-bundle/` (`sensor-web.tar` + `docker-compose.yml`) |
| `docker/entrypoint.sh`  | 컨테이너 시작 스크립트 (CSV 폴더 주인으로 실행)                                    |
| `docker-compose.yml`    | 운영 서버에서 웹 컨테이너 띄우는 파일 (포트 8060 → 컨테이너 안 8060)               |
| `docker-bundle/`        | `make_bundle.py` 결과 (서버에 가져갈 두 파일. git에 안 올림)                       |
| `deploy/`               | 운영에 가져갈 설정 DB (`export_settings` 결과. git에 안 올림)                      |
| `data_csv_web/`         | 로컬 실행 때 웹이 쌓는 수신 CSV (git에 안 올림)                                    |
| `docs/`                 | 문서 (아래 4)                                                                      |
| `.venv/`                | 웹 + 데스크톱 프로그램 가상환경 (git에 안 올림)                                    |

## 2. 이 PC에서 실행

가상환경은 둘이다. 웹과 프로그램은 루트 `.venv`, AI 서버는 `ai_server\.venv`.
AI 서버는 torch 등 1GB가 넘고, 운영에서도 웹과 다른 이미지라 따로 둔다.

**만들기** (처음 한 번, Python 3.11)

```bat
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r web_db\requirements.txt -r program\requirements.txt
py -3.11 -m venv ai_server\.venv
ai_server\.venv\Scripts\python.exe -m pip install -r ai_server\requirements.txt
```

**웹 서버** (저장소 루트에서)

```bat
.venv\Scripts\python.exe -m uvicorn web_db.server:app --host 0.0.0.0 --port 8081
```

- 데이터: DB `web_db\sensor.sqlite`, CSV `data_csv_web\`, 평면도 `web_db\site_files\`, MEMS 보정 `web_db\*.json`
- 프로세스 하나로만 띄운다(`--workers`를 늘리지 않는다). MQTT 수신·통계 스레드가 한 프로세스에 있다.

**AI 서버** (저장소 루트에서)

```bat
ai_server\.venv\Scripts\python.exe -m uvicorn ai_server.fast_server:app --host 127.0.0.1 --port 8000
```

**데스크톱 프로그램**

```bat
.venv\Scripts\python.exe program\new_piezo_adc_V063_axis_units_aligned.py
.venv\Scripts\python.exe program\mqtt_wave_replay_generator_v073_pyqtgraph_analysis_data_export.py
```

## 3. 운영 배포 (웹)

```bat
python docker\make_bundle.py
```

- 하는 일: `docker build -f web_db/Dockerfile -t sensor-web:latest .`(저장소 루트 기준) + `docker save`
- 결과: `docker-bundle\sensor-web.tar`, `docker-bundle\docker-compose.yml`

서버에서는 두 파일을 같은 폴더에 두고 다음을 실행한다. 접속 주소는 `http://서버:8060`이다.

```sh
docker load -i sensor-web.tar
docker compose up -d --force-recreate
```

자세한 절차는 [docs/운영-배포-절차.md](docs/운영-배포-절차.md).

## 4. 문서 (`docs/`)

| 문서                                                            | 내용                       |
| --------------------------------------------------------------- | -------------------------- |
| [웹-기능상세.md](docs/웹-기능상세.md)                           | 웹 기능·API·데이터 위치    |
| [web_db-구성과-데이터흐름.md](docs/web_db-구성과-데이터흐름.md) | 웹 파일 구성과 데이터 흐름 |
| [web_db-DB명세서.md](docs/web_db-DB명세서.md)                   | SQLite 표 설명             |
| [운영-배포-절차.md](docs/운영-배포-절차.md)                     | 운영 서버에 올리는 순서    |

# web_db 화면 역할

기준은 `http://127.0.0.1:8081` 프로세스다. 실행은 프로젝트 루트에서 다음이다.

```text
.\.venv\Scripts\python.exe -m uvicorn web_db.server:app --host 127.0.0.1 --port 8081
```

8080의 `web` 폴더는 CSV 수신 프로그램이다. 8081은 `web_db`다. 내부 구성과 데이터 흐름은 `docs/web_db-구성과-데이터흐름.md`에 있다.

| 위치 | 내용 |
| --- | --- |
| `web_db/server.py` | 실행 진입점. `uvicorn web_db.server:app` |
| `web_db/src` | 역할별 파이썬. `server.py`는 HTTP 입구만 둔다 |
| `web_db/src/routes` | 주소별 API. 화면, `/api/generator`, `/api/monitor`, `/api/analysis`, `/api/db` |
| `web_db/src/save_csv.py` | 수신 CSV 저장 |
| `web_db/front` | HTML과 `/static` 화면 파일 |
| `web_db/sensor.sqlite` | 패킷, 이름, 설정 JSON, 통계. git에 넣지 않는다. 없으면 서버가 켜질 때 테이블과 함께 만든다 |

## 데이터가 저장되는 곳

센서 수신 모니터가 MQTT에 연결되면 측정 패킷은 두 곳에 남는다.

| 저장 | 내용 | 언제 |
| --- | --- | --- |
| `data_csv/YYYYMMDD_HHMMSS/` | HEX CSV, decoded CSV | 연결할 때마다 폴더 하나. 연결이 유지되는 동안 그 폴더에 계속 쓴다 |
| `web_db/sensor.sqlite`의 `packet` | CSV 한 줄과 같은 원본. MAC, 시각, 축, seq, 샘플링, HEX, 디코드 샘플, `set_no` | 패킷이 디코드될 때마다 한 줄. session 은 쓰지 않는다 |

ADXL X/Y/Z는 축마다 한 줄이다. 같은 측정은 `set_no`가 같다. seq 상위 4비트는 축이고, 하위 4비트는 축을 보낼 때마다 1씩 증가한다. `10`, `21`, `32`가 한 세트다.

통계 숫자인 SRSS, RMS, PEAK는 이 시점에 만들지 않는다. `measurement`에 넣는 일은 하루 한 번 스케줄 또는 통계 화면의 새로고침 확인 때만 한다. 이미 계산한 세트는 다시 계산하지 않는다.

## 화면별 역할

### 센서 수신 모니터 `/`

살아 있는 수신과 센서 명령이다.

- 브로커에 연결하고 `V/#`, `D/#`와 설정 토픽 `S/#`를 구독한다.
- 들어온 파형을 RAW, FFT로 바로 본다. 이 그래프용 계산은 저장 통계와 별개다.
- CSV와 `packet` 테이블에 원본을 남긴다.
- 센서 설정을 `S/{MAC}`에 retain으로 보낸다. 센서 타입 0~4, 주기, Fs+N, G Range, PIEZO 게인이 JSON에 들어간다. START는 PIEZO 3, MEMS 4다. STOP은 0이다.
- retain으로 보낸 JSON은 `sensor_config`에 그 MAC 기준으로 남긴다. 현재 설정 조회(`CONFIG ?`)는 저장하지 않는다.
- MAC에 표시 이름을 붙여 `device`에 저장한다. 목록에 없는 MAC도 직접 입력하면 설정 적용과 이름 저장을 할 수 있다. 패킷이 아직 없어도 이름과 설정 JSON은 남는다.

### 센서 데이터 분석 `/studio`

수신으로 쌓인 DB 패킷만 연다. 파일 선택은 없다.

- MAC을 고르고, 최근 N세트 또는 시작~끝 기간으로 연다. 한 번에 최대 200세트다.
- ADXL은 X/Y/Z가 갖춰진 `set_no`만 연다. 기간을 잘라도 세트가 쪼개지지 않는다.
- 그래프에서 PIEZO, MEMS, 초음파를 고르면 그 센서의 설정과 전송 주기만 보인다.
- 전송 체크와 Topic 입력칸은 없다. Topic은 PIEZO·MEMS `V/{mac}`, 초음파 `D/{mac}`을 서버가 넣는다.
- 전송 시작은 지금 열어 둔 DB 패킷을 고른 위치부터 브로커로 반복 송신한다.

### 데이터 분석 CSV `/generator`

화면 배치는 센서 데이터 분석과 같다. 데이터만 파일·CSV다. SQLite는 읽지 않는다. 외부 CSV를 열어 자세히 보고, 그 파일을 MQTT로 다시 보낸다.

### 통계 `/dashboard`

`measurement`에 이미 들어 있는 값만 그린다. 페이지를 열거나 5초마다 다시 그릴 때는 계산하지 않는다.

- 한 줄이 MAC 하나다. 왼쪽은 최신 SRSS(mm/s RMS) 막대와 한계선, 옆은 RMS와 PEAK의 최근 추세다.
- 새로고침은 `N시간 전에 최신화되었습니다. 최신화 하시겠습니까?`를 묻는다. 확인하면 마지막 최신화 이후의 새 XYZ 세트만 `measurement`에 넣는다.
- 서버가 켜져 있는 동안, 마지막 최신화 날짜가 오늘보다 이전이면 약 10분 간격 확인 뒤 하루에 한 번 같은 최신화를 한다.

## 테이블

| 테이블 | 역할 |
| --- | --- |
| `device` | MAC, 표시 이름, 마지막 수신 정보 |
| `packet` | 수신 원본. 통계 계산 전 |
| `sensor_config` | 그 MAC에 마지막으로 retain 전송한 설정 JSON |
| `measurement` | 세트별 SRSS, RMS, PEAK. 최신화 때만 추가 |
| `stat_meta` | 통계를 마지막으로 만든 시각 |
| `daily_stat` | `measurement`를 날짜로 묶은 요약 |
| `srss_limit` | 장치별 SRSS 한계. 비어 있으면 4.5 mm/s RMS |

## CSV 분석과 DB 분석을 나누는 안

지금 센서 데이터 분석은 CSV 열기와 DB 열기를 한 화면에 같이 두고, MQTT 파형 발생기는 같은 송신 기능을 다른 배치로 다시 가지고 있다. 그래서 전송 칸에 센서를 또 고르게 되어 있었다.

나누는 편이 맞다.

- MQTT 파형 발생기 자리는 데이터 분석 CSV다. 분석 화면과 같은 배치이고, 외부 CSV만 열어 MQTT로 다시 보낸다.
- 센서 데이터 분석은 DB 전용이다. MAC과 최근 N세트 또는 기간으로 열고, 같은 그래프와 MQTT 재전송을 쓴다.
- 센서 수신 모니터와 통계는 그대로 둔다. 모니터는 받아서 저장하고 명령을 보내는 화면이고, 통계는 `measurement`만 보는 화면이다.

이렇게 하면 한 화면의 데이터 출처가 하나다. 송신 기능은 둘 다 남기되, 발생기는 파일 패킷을 보내고 분석 페이지는 DB 패킷을 보낸다.

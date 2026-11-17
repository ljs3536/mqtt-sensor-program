# web_db 구성과 데이터 흐름

기준 프로세스는 `http://127.0.0.1:8081`이다.

```text
.\.venv\Scripts\python.exe -m uvicorn web_db.server:app --host 127.0.0.1 --port 8081
```

`web_db/server.py`는 실행 입구만 두고, 실제 앱은 `web_db/src/server.py`다.

## 폴더

| 위치 | 역할 |
| --- | --- |
| `web_db/src/server.py` | FastAPI 앱. 정적 파일과 라우트를 붙이고 하루 통계 스레드를 켠다 |
| `web_db/src/routes` | 주소별 HTTP. `pages`, `monitor`, `generator`, `analysis`, `db` |
| `web_db/src/monitor_service.py` | MQTT 수신, 최신 파형, CSV와 SQLite로 넘김 |
| `web_db/src/save_csv.py` | 연결 동안 `data_csv/YYYYMMDD_HHMMSS`에 HEX·decoded CSV |
| `web_db/src/replay.py` | 파일 또는 DB에서 연 패킷을 MQTT로 반복 송신 |
| `web_db/src/store.py` | SQLite 연결, 스키마, 패킷·이름·설정 저장 |
| `web_db/src/store_query.py` | MAC 목록, 통계 조회, 측정 최신화, 분석용 패킷 조회 |
| `web_db/src/grouping.py` | ADXL seq로 X/Y/Z를 한 세트에 묶음 |
| `web_db/src/stats_calc.py` | 완성된 XYZ 한 세트의 RMS, PEAK, SRSS |
| `web_db/src/decode.py` | 패킷 분류와 sensorData HEX 디코드 |
| `web_db/src/signal.py` | 필터, FFT, SRSS 전력 |
| `web_db/src/mems.py` | g/LSB, 주파수·감도 보정 JSON |
| `web_db/src/graph_curves.py` | 화면용 RAW, FFT, SRSS 곡선 |
| `web_db/src/view_pack.py` | 계산이 끝난 곡선을 브라우저에 보내기 전에 점 수를 줄임 |
| `web_db/front` | HTML. `/static`은 이 폴더의 `static` |
| `web_db/sensor.sqlite` | 패킷, 이름, 설정 JSON, 통계. 없으면 서버가 켜질 때 만든다 |

화면은 다섯 개다.

| 주소 | 파일 | 하는 일 |
| --- | --- | --- |
| `/` | `front/index.html` | 수신, 명령, CSV와 SQLite 저장 |
| `/generator` | `front/generator.html` | 외부 CSV 분석과 MQTT 재전송 |
| `/studio` | `front/studio.html` | DB 패킷 분석과 MQTT 재전송 |
| `/deck` | `front/deck.html` | `/studio`와 같은 기능, 계측기형 화면 |
| `/dashboard` | `front/dashboard.html` | 저장된 SRSS, RMS, PEAK만 조회 |

## 수신해서 저장하기

1. 모니터가 브로커에 붙으면 `V/#`, `D/#`, `S/#`를 구독한다.
2. 측정 JSON이 오면 `decode.py`가 PIEZO, ADXL 축, 초음파를 가른다.
3. HEX를 샘플 배열로 푼다. PIEZO는 7D escape를 풀고, ADXL은 풀지 않은 signed int16이다.
4. 같은 패킷을 두 곳에 넣는다.
   - CSV가 켜져 있으면 `save_csv.py`가 연결 폴더에 한 줄씩 쓴다.
   - `store.save_packet`이 `packet` 테이블에 같은 원본을 넣는다. 여기서 FFT나 SRSS는 계산하지 않는다.
5. ADXL은 seq 상위 4비트가 축, 하위 4비트가 증가 번호다. `10`, `21`, `32`처럼 이어지면 같은 `set_no`다.
6. 메모리의 최신 파형만 수신 화면 그래프로 그린다. 이 계산은 통계 테이블과 별개다.

설정 적용으로 retain 전송한 JSON은 `sensor_config`에 남긴다. `CONFIG ?` 조회는 저장하지 않는다. 표시 이름은 `device.display_name`이다.

## 그래프 계산

파일 분석과 DB 분석은 같은 `graph_curves.py`를 쓴다. 차이는 패킷이 파일에서 왔는지, SQLite에서 왔는지뿐이다.

- RAW와 FFT는 연 패킷의 샘플 배열로 계산한다.
- 화면 단위, 필터, 창, 로그 눈금은 그래프에만 쓰이고 저장된 HEX를 다시 쓰지 않는다.
- ADXL X/Y/Z는 한 세트로 묶어 눈금을 맞추고, SRSS는 세 축 스펙트럼으로 만든 가로선이다.
- 계산이 끝난 뒤에만 `view_pack.py`가 브라우저로 보낼 점을 줄인다.

## 통계

통계는 패킷이 도착할 때 만들지 않는다. 통계 화면에서 새로고침을 확인하거나, 서버가 켜진 동안 날짜가 바뀌어 하루 한 번 확인할 때만 `update_statistics`가 돈다.

대상은 `measurement`에 아직 없는, X/Y/Z가 모두 있는 ADXL `set_key`다. `stats_calc.adxl_set_stats`는 그 세 줄의 `decoded_samples` 전체를 쓴다. 이미 넣은 세트는 다시 계산하지 않는다.

대시보드의 RMS, PEAK 작은 그래프 x축은 시각이 아니다. 그 MAC의 최근 측정 최대 40개를 오래된 순서로 놓은 번호다. y값은 g다.

## 값을 자르는 곳

계산에 넣는 샘플과, 화면에 그리려고 줄인 점은 다르다.

| 위치 | 무엇을 자르나 | 계산에 영향 |
| --- | --- | --- |
| `view_pack._envelope_index` | 점이 16384개를 넘을 때만, 구간마다 최소·최대 점을 남긴다 | 없다. RMS, FFT, SRSS를 만든 다음의 전송용 배열이다 |
| `view_pack._rounded` | 화면 숫자를 소수 6자리로 반올림한다 | 없다. 같은 이유다 |
| `signal.fft_peak`의 `x[offset:offset+n]` | Window N이 `전체`가 아닐 때만 가운데 n개로 FFT한다 | 있다. 사용자가 1024처럼 짧은 창을 고른 그래프 FFT만 해당한다. 기본값 `전체`는 패킷 샘플을 그대로 쓴다 |
| `spectrum[:n//2+1]` | 단측 스펙트럼의 0 Hz부터 나이퀴스트까지 | 샘플을 버리는 것이 아니다. 음수 주파수는 양수 쪽과 짝이라 단측에서 접는다 |
| `stats_calc.adxl_set_stats` | 자르지 않는다 | `fft_peak`를 창 길이 없이 호출하므로 저장된 샘플 전체로 RMS, PEAK, SRSS를 만든다 |
| `store_query`의 LIMIT 40, 조회 개수 1~200 | 가져오는 패킷·세트 개수 | 한 패킷 안의 샘플 수는 줄이지 않는다. 분석 화면에 몇 세트를 올릴지만 제한한다 |
| `load_file.recover_adxl_hex_prefix` | 깨진 HEX의 앞쪽 완전한 16비트 단어만 남긴다 | 원본 CSV 파일은 고치지 않는다. 복구본은 미리보기용이고, 잘린 패킷은 재전송에서 뺀다 |
| `graph_curves._decimate_xy` | FFT 왕복 검사 그림만 160점 안쪽으로 줄인다 | 합격 판정은 줄이기 전의 전체 샘플로 한다 |

그래서 화면 점이 패킷 샘플 수보다 적어도, 저장된 값이나 통계 숫자가 그 때문에 줄어들지는 않는다. 그래프 FFT만, 사용자가 Window N을 `전체`가 아닌 값으로 두었을 때 가운데 구간으로 계산한다.

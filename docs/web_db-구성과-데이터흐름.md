# web_db 구성과 데이터 흐름

기준 프로세스는 `http://127.0.0.1:8081`이다.

```text
.\.venv\Scripts\python.exe -m uvicorn web_db.server:app --host 127.0.0.1 --port 8081
```

`web_db/server.py`는 실행 입구만 두고, 실제 앱은 `web_db/src/server.py`다.

## 폴더

| 위치 | 역할 |
| --- | --- |
| `web_db/src/server.py` | FastAPI 앱. 정적 파일과 라우트를 붙이고 매시간 통계 스레드를 켠다 |
| `web_db/src/routes` | 주소별 HTTP. `pages`, `monitor`, `generator`, `analysis`, `db`, `site` |
| `web_db/src/monitor_service.py` | MQTT 수신, 최신 파형, CSV와 SQLite로 넘김 |
| `web_db/src/save_csv.py` | 연결 동안 `data_csv_web/YYYYMMDD_HHMMSS`에 HEX·decoded CSV |
| `web_db/src/replay.py` | 파일 또는 DB에서 연 패킷을 MQTT로 반복 송신 |
| `web_db/src/store.py` | SQLite 연결, 스키마, 패킷·이름·설정 저장 |
| `web_db/src/store_query.py` | MAC 목록, 통계 조회, 측정 최신화, 분석용 패킷 조회 |
| `web_db/src/store_site.py` | 앱 설정(`app_setting`), 평면도(`floor_plan`, 이미지는 `site_files/`), 센서 위치(`sensor_placement`), 대문 상태용 원자료 |
| `web_db/src/site_status.py` | 대문의 센서 연결·알람 판정. 임시 기준값 상수가 맨 위에 있다 |
| `web_db/src/paths.py` | 코드·데이터 경로. 수신 CSV는 어디서나 `data_csv_web`. 환경변수 `SENSOR_DATA_DIR`가 있으면 DB·평면도·MEMS JSON을 그 폴더에, CSV를 그 아래 `data_csv_web`에 둔다(Docker: `/data` = `./sensor_web_db`, `/data/data_csv_web` = `./data_csv_web`). 배포는 `docs/웹-기능상세.md` "실행" |
| `web_db/src/csv_import.py` | data_csv_web의 누적 CSV를 packet으로 가져오기(2026-11-24, 설정 '통계 데이터' 탭). 전에는 `csv_stats.py`(CSV로 통계만) |
| `web_db/src/stat_profile.py` | 통계 계산 기준(버전) 검사·기본값, 세트 하나 통계(`set_stats`), 한 시간 묶음·합치기 |
| `web_db/src/stats_job.py` | 통계 재계산 백그라운드 작업(기준 저장·재계산 버튼·업그레이드 직후) |
| `web_db/src/import_csv.py` | CSV 폴더를 SQLite `packet`에 다시 채우기(명령줄 전용). 아래 "CSV 폴더로 DB 채우기" |
| `web_db/src/export_settings.py` | 설정(이름·위치·구간·평면도·통계 기준)만 담은 새 SQLite 만들기(명령줄, 2026-11-24 운영 첫 배포용). `docs/운영-배포-절차.md` |
| `web_db/src/compact_db.py` | 쓰지 않는 `packet.decoded_samples` 열 지우기 + VACUUM(명령줄, 서버를 멈추고) |
| `web_db/src/grouping.py` | ADXL seq로 X/Y/Z를 한 세트에 묶음 |
| `web_db/src/decode.py` | 패킷 분류와 sensorData HEX 디코드 |
| `web_db/src/signal.py` | 필터, FFT(Window 보정), Welch PSD, SRSS 전력 |
| `web_db/src/mems.py` | g/LSB, 주파수·감도 보정 JSON |
| `web_db/src/graph_curves.py` | 화면용 RAW, FFT, SRSS 곡선. PSD 모드면 FFT 칸을 `graph_psd` 결과로 바꾼다 |
| `web_db/src/graph_psd.py` | PSD(Welch) 곡선·합성 SRSS·판정, ADXL 속도 RMS 시간 이력 (발생기 v070) |
| `web_db/src/view_pack.py` | 계산이 끝난 곡선을 브라우저에 보내기 전에 점 수를 줄임 |
| `web_db/front` | HTML. `/static`은 이 폴더의 `static` |
| `web_db/front/partials` | 여러 페이지가 같이 쓰는 HTML 조각. 분석 화면 설정 서랍 `drawer.html`, PSD 결과 칸과 시간 이력 창 `psd-history.html` |
| `web_db/sensor.sqlite` | 패킷, 이름, 설정 JSON, 통계. 없으면 서버가 켜질 때 만든다 |

화면은 여섯 개다. 브라우저 스크립트는 `web_db/front/static`에 있고, 계산은 하지 않는다. 서버 JSON을 받아 칸을 채우거나, 칸의 값을 JSON으로 보낸다.

| 주소 | 파일 | 하는 일 |
| --- | --- | --- |
| `/` | `front/site.html` | 대문: 센서 현황(평면도 위 센서 연결·수신·알람) |
| `/receiver` | `front/index.html` | 수신, 명령, CSV와 SQLite 저장 |
| `/generator` | `front/generator.html` + `partials/drawer.html` | 외부 CSV 분석과 MQTT 재전송 |
| `/studio` | `front/studio.html` + `partials/drawer.html` | DB 패킷 분석과 MQTT 재전송 |
| `/dashboard` | `front/dashboard.html` | 저장된 SRSS, RMS, PEAK만 조회 |
| `/settings` | `front/settings.html` | MAC 표시 이름과 SRSS 한계 |

`/generator`와 `/studio`의 설정 서랍(필터, 단위, FFT, SRSS, MEMS 보정, 전송, 로그)은 내용이 같아서 `partials/drawer.html` 한 파일에 둔다. 두 페이지 HTML에는 그 자리에 `<!-- @include partials/drawer.html -->`, `<!-- @include partials/psd-history.html -->` 주석만 있고, `routes/pages.py`의 `_with_includes`가 요청마다 파일을 읽어 그 자리에 넣은 뒤 한 장의 HTML로 보낸다. 브라우저가 따로 불러오는 방식이 아니라서, 스크립트가 처음 실행될 때 서랍의 입력 칸이 이미 있다. 서랍을 고칠 때는 이 파일 하나만 고치면 되고, 서버를 다시 켜지 않아도 새로고침하면 반영된다. 화면별 차이(전송 체크와 Topic 숨김 등)는 `studio.js`가 불러온 뒤에 처리한다.

## 화면 스크립트

스크립트는 빌드하지 않는다. HTML이 순서대로 불러 오고, 함수는 페이지 전역에 둔다. 나중 스크립트가 앞 함수를 덮어쓸 수 있다. `studio.js`가 분석 화면의 `draw`와 `showPanels`를 그렇게 덮는다.

| 파일 | 역할 |
| --- | --- |
| `api.js` | `readJson`, `postJson`. 응답 본문을 읽고 JSON POST를 보낸다 |
| `filters.js` | HPF/LPF/Notch 칸(수신 모니터·분석 화면 공용). 줄마다 실제 계산식 문구, LPF `자동 Fs/2` |
| `graph.js` | RAW/FFT 차트, 확대, 축 고정, 고정 커서(클릭, `pinCursor`), FFT 검증 그림. 서버를 호출하지 않는다 |
| `theme.js` | White / Gray / Dark. 차트가 다시 그려질 때 눈금 색을 맞춘다. 나중에 만드는 차트는 `SensorTheme.watch`로 붙인다 |
| `monitor-draw.js` | 수신 화면의 그래프 옵션과 곡선 그리기 |
| `monitor-api.js` | `/api/monitor/state`, 명령, `/api/monitor/view` |
| `monitor.js` | 수신 화면 버튼을 위 함수에 연결. MQTT가 연결된 동안만 1초마다 상태를 다시 읽는다 |
| `monitor-db.js` | 표시 이름과 설정 JSON. 설정 적용 뒤에 그 MAC 프로필을 다시 읽는다 |
| `generator-draw.js` | 분석 화면의 그래프 옵션과 미리보기 그리기. 초음파 스펙트럼은 RAW 칸에만 둔다. PSD 결과 칸(`showPsd`) |
| `velocity-history.js` | X/Y/Z 속도 RMS 시간 이력 창. `/api/generator/velocity-history` 결과를 차트·표·제외 목록으로 그린다 |
| `generator-api.js` | 파일 업로드, 미리보기, 송신 시작·중지, 송신 상태 |
| `generator.js` | 분석 화면 버튼을 위 함수에 연결. 전송 중일 때만 1초마다 송신 숫자와 로그를 갱신한다. 그 밖에는 버튼 동작 뒤에 한 번 읽는다 |
| `studio.js` | DB 분석 서랍과 센서 탭. 계산 API는 `generator-*.js`를 그대로 쓴다 |
| `db.js` | `/api/db/devices`, `/api/db/stats`, `/api/db/load`. 연 패킷은 `draw`로 넘긴다 |
| `settings.js` | 설정 탭, 장치 이름과 SRSS 한계 저장, CSV 데이터 가져오기. `/api/db/device-name`, `/api/db/csv-import` |
| `stat-profile.js` | 설정 > 통계 데이터의 '통계 계산 기준'(버전 저장·재계산·JSON 내보내기/가져오기). `/api/db/stat-profile`, `/api/db/stat-rebuild` |
| `site.js` | 대문. 평면도 탭(여러 장, 탭마다 문제 수 배지), 아이콘 그리기, 정보 카드, 배치 편집(평면도 추가·교체·이름·순서·삭제, 끌어 놓기·이동·빼기·저장), 15초 상태 갱신 |

수신 화면의 흐름은 `monitor.js` 버튼 → `monitor-api.js` → 서버 → `monitor-draw.js`다. 분석 화면은 `generator.js` 또는 `db.js` → `generator-api.js` → 서버 → `generator-draw.js`다. 통계와 설정 화면은 각자 `dashboard.html`, `settings.js` 안에서 같은 방식으로 서버 JSON만 읽고 쓴다.

## 서버가 켜질 때

`uvicorn web_db.server:app`이 `web_db/src/server.py`를 import하면 다음 순서로 준비된다.

1. `routes`를 import하면서 `runtime.py`가 프로세스에 하나뿐인 객체 셋을 만든다.
   - `Store()`가 `sensor.sqlite`를 열고(WAL 모드) `_migrate`를 한다. 테이블과 인덱스가 없으면 만든다.
     이미 있는 테이블은 그대로 두고, 쓰지 않는 옛 `daily_stat`과 옛 열(`device.asset_no` 등)을 지운다. `device.location`은 없으면 다시 만든다(2026-11-24, 설정 '위치' 칸). 저장된 패킷을 다시 쓰는 일은 없다.
     옛 DB로 새 버전을 켤 때 일어나는 일과 `decoded_samples` 정리(`compact_db`)는 `docs/web_db-DB명세서.md` "기존 DB로 새 버전을 켤 때".
   - `MonitorService(store)`는 CSV 기록 스레드를 켠다. MQTT에는 아직 붙지 않는다.
   - `ReplayService()`는 빈 재전송 목록으로 시작한다.
2. FastAPI 앱에 `/static`과 라우터 다섯 개(`pages`, `generator`, `monitor`, `analysis`, `db`)를 붙인다.
3. `start_daily_stats()`가 매시간 통계 스레드를 켠다. 이미 살아 있으면 새로 만들지 않는다. 통계 기준 버전이 없으면 첫 버전을 만들고, 지금 버전이 없으면(하루 통계에서 업그레이드한 직후) 그 버전으로 재계산을 백그라운드로 시작한다.

4. 수신 모니터에서 마지막으로 '연결'한 뒤 '연결 해제'를 하지 않았으면 `monitor.auto_connect()`가 기억한 주소·Topic·CSV 체크로 백그라운드 연결을 시작한다. 브로커가 꺼져 있어도 서버는 뜨고, paho가 1~30초 간격으로 다시 붙기를 시도한다.

마지막에 '연결 해제'를 했으면 MQTT 수신은 시작하지 않는다. 수신 화면(`/receiver`)에서 연결해야 한다.

## 스레드와 잠금

한 프로세스 안에서 다음 스레드가 같은 객체를 쓴다.

| 스레드 | 하는 일 | 쓰는 잠금 |
| --- | --- | --- |
| uvicorn 요청 스레드 | HTTP 요청. `def` 라우트는 스레드 풀에서 돈다 | 요청에 따라 아래 잠금 |
| 수신 paho 루프 (`loop_start`) | `_on_message` → 디코드 → CSV 큐 → `save_packet` | `MonitorService.lock` 안에서 `Store.lock` |
| CSV 기록 (`CsvWriter._loop`) | 큐에서 꺼내 파일에 쓰고 flush | 큐만 쓴다 |
| `hourly-stats` | 1분마다 시각을 보고 매시간 한 번 최신화 | `Store.stats_lock`, `Store.lock`(짧게) |
| `stat-rebuild` | 통계 기준 버전으로 packet 전체 재계산(필요할 때만) | `Store.stats_lock`, `Store.lock`(짧게) |
| 재전송 센서별 스레드 (`ReplayService._worker`) | 고른 패킷을 주기마다 publish | `ReplayService.lock` (카운터만) |
| 재전송 paho 루프 | PUBACK을 받아 확인 수를 올린다 | `ReplayService.lock` |

| 잠금 | 지키는 것 | 왜 필요한가 |
| --- | --- | --- |
| `Store.lock` | SQLite 연결 하나 | 연결을 여러 스레드가 같이 쓴다. 동시에 쓰면 한쪽 commit이 다른 쪽의 반쯤 한 작업까지 확정할 수 있다. 쿼리와 commit은 모두 이 안에서 한다. 재진입이 안 되므로, 이 잠금을 잡은 채로 잠금을 잡는 공개 메서드를 부르지 않는다 |
| `Store.stats_lock` | 통계 최신화 전체 | 최신화는 계산하는 동안 `Store.lock`을 푼다. 두 최신화가 겹치면 둘 다 같은 읽기 위치를 보고 같은 세트를 두 번 넣는다. 이 잠금으로 한 번에 하나만 돌고, 뒤에 온 쪽은 앞이 끝난 위치부터 읽는다 |
| `MonitorService.lock` | 최신 파형, MAC 목록, 로그, 메시지 수 | 수신 스레드가 쓰는 중에 `/api/monitor/state`가 반쯤 바뀐 값을 읽지 않게 한다. 그래프 계산은 스냅샷을 복사한 뒤 잠금 밖에서 한다 |
| `ReplayService.lock` | 재전송 목록, 보낸·확인·버린 수 | 송신 스레드와 HTTP 요청이 같은 숫자를 고친다 |

잠금은 프로세스 안에서만 효과가 있다. 이 시스템은 서버 프로세스 하나로만 돌린다. `--workers 2`처럼 여러 프로세스로 띄우거나, 같은 `sensor.sqlite`로 서버를 두 개 띄우지 않는다.

## HTTP API

| 주소 | 하는 일 |
| --- | --- |
| `GET /`, `/receiver`, `/generator`, `/studio`, `/dashboard`, `/settings` | 화면 HTML |
| `GET /api/site/status` | 대문: MAC별 연결(link)·알람(alarm) 판정과 개수, 수신 연결 상태. 최신화는 하지 않는다 |
| `GET·POST /api/site/plans` | 평면도 목록(탭 순서)과 모든 센서 위치 / 평면도 추가(multipart: file, width, height, name) |
| `GET·POST /api/site/plans/{id}/image` | 평면도 이미지 / 이미지 교체(이름·위치 유지) |
| `PATCH·DELETE /api/site/plans/{id}` | 이름 바꾸기 `{"name"}`·탭 순서 `{"move": -1\|1}` / 평면도 한 장과 그 위치 삭제 |
| `POST /api/site/placements` | 모든 평면도의 센서 위치를 통째로 저장 `[{plan_id, mac, x, y}]`(x·y는 0~1). 같은 MAC은 마지막 한 곳만 |
| `GET /api/site/mqtt` | 마지막 수신 연결 값(수신 모니터 '연결' 때 저장)과 지금 연결 상태. 저장 API는 없다(연결·해제가 저장한다) |
| `POST /api/monitor/connect`, `/disconnect` | 수신 MQTT 연결·해제. 본문 `host`, `port`, `topic`, `qos`(0/1/2, 기본 1), `csv_enabled`. 연결 때 CSV 폴더를 연다 |
| `GET /api/monitor/state?device=` | 연결 상태, MAC 목록, 로그, 최신 그래프. 연결된 동안 화면이 1초마다 부른다 |
| `POST /api/monitor/view` | 수신 그래프 옵션과 창 길이를 바꾸고 다시 그린다 |
| `POST /api/monitor/command` | `start`, `stop`(3단계, PUBACK 대기), `apply`, `query`를 `S/{MAC}`에 보낸다 |
| `POST /api/monitor/fft-check`, `/logs/clear` | 수신 RAW의 FFT 왕복 검사, 로그 비우기 |
| `POST /api/generator/upload`, `/upload-adxl-csv` | 파일을 임시 파일로 읽어 재전송 목록에 올리고 바로 지운다 |
| `POST /api/generator/preview`, `/fft-check` | 재전송 목록의 패킷 하나를 옵션대로 그린다(옵션 `psd.on`이면 FFT 칸이 PSD), FFT 왕복 검사 |
| `POST /api/generator/velocity-history` | 올려 둔 ADXL 세트(CSV 또는 DB)로 X/Y/Z 속도 RMS 시간 이력. 본문 `start`, `count`, `options` |
| `POST /api/generator/test`, `/start`, `/stop`, `/logs/clear`, `GET /status` | 브로커 시험, 반복 송신 시작·중지, 송신 수와 로그 |
| `GET /api/analysis/defaults`, `GET·POST /srss-limit`, `POST /mems-scale`, `/mems-correction` | 그래프 기본값, SRSS 한계, 1 LSB, MEMS 보정 저장 |
| `GET /api/db/devices`, `/stats?mac=`, `/profile?mac=` | MAC 목록과 패킷 수, 한 MAC 통계, 이름과 마지막 설정 JSON. 분석 화면 MAC 선택 상자는 `MAC · 이름`만 보이고 패킷 수는 보이지 않는다. 패킷 수는 설정 화면이 이름 삭제 때 목록에서 뺄지 정하는 데 쓴다 |
| `POST /api/db/load` | DB 패킷을 골라 재전송 목록에 올리고 첫 미리보기를 준다 |
| `POST /api/db/device-visible` | 설정 > 장치 '표시' 체크 저장 `{mac, visible}`(2026-11-24) |
| `POST /api/db/device-kind` | 설정 > 장치 종류 칸 저장 `{mac, kind: PIEZO\|ADXL\|ULTRASONIC, shown}` → `{mac, kind, shown, kind_view}`(2026-11-25, `src/kind_view.py`) |
| `GET /api/db/devices?named=` | MAC 목록(설정 표는 전부, `named=true`면 이름 있는 MAC만: 센서 데이터 분석, 2026-11-24) |
| `GET /api/db/dashboard?period=&mac=&bucket=` | 저장된 통계 읽기(지금 기준 버전 줄만, 계산 기준 요약 포함. 기간 24h 기본·7d·30d·all. 초음파 MAC 제외. mac은 한 MAC만, bucket(hour/day)은 그래프 점 묶음 — 통계2 자세히 보기용. 최신화 API는 없앴다) |
| `GET·POST /api/db/stat-profile` | 통계 기준 버전 목록·기본값·그래프 설정 파일 값·재계산 진행 / 새 버전 저장 + 재계산 시작(돌고 있으면 409) |
| `POST /api/db/stat-rebuild` | 지금 버전으로 재계산 시작(돌고 있으면 409) |
| `GET·POST /api/db/csv-import` | CSV 가져오기 상태·찾은 CSV 파일 수·크기·DB 패킷 수(실시간/가져옴) / 백그라운드 시작(이미 돌거나 재계산 중이면 409) |
| `POST·DELETE /api/db/device-name` | 표시 이름 저장·삭제 |

## 수신해서 저장하기

1. 모니터가 브로커에 붙으면 `V/#`, `D/#`, `S/#`를 고른 QoS(기본 1)로 구독한다.
2. 측정 JSON이 오면 `decode.py`가 PIEZO, ADXL 축, 초음파를 가른다.
3. HEX를 샘플 배열로 푼다. PIEZO는 7D escape를 풀고, ADXL은 풀지 않은 signed int16이다.
4. 같은 패킷을 두 곳에 넣는다.
   - CSV가 켜져 있으면 `save_csv.py`가 연결 폴더에 한 줄씩 쓴다. ADXL의 `packet_no`는 X/Y/Z가 같이 쓰는 세트 번호다
     (아래 "CSV packet_no" 참고). PIEZO·초음파는 파일마다 1부터 센 행 번호다.
     열은 수신기 V057과 같다: `packet_no, received_at, source, dvicMacAddr, gwMacAddr, sensor_type, axis, seq, tick,
     batteryRmin, samplerate, numofsample, payload_bytes, acqStatus, apSsid, apRssi` 뒤에 HEX(`sensorData_bytes, sensorData_hex`)
     또는 decoded 열. `acqStatus`·`apSsid`·`apRssi`는 받은 JSON 값 그대로(없으면 빈 칸)이고 2026-11-20부터 있다. DB에는 넣지 않는다.
     CSV를 열 때(`load_file.copy_status_metadata`)는 값이 있는 열만 패킷에 넣어 그대로 송신한다(발생기 v070과 같음).
   - `store.save_packet`이 `packet` 테이블에 같은 원본을 넣는다. 여기서 FFT나 SRSS는 계산하지 않는다.
5. ADXL은 seq 상위 4비트가 축, 하위 4비트가 증가 번호다. `10`, `21`, `32`처럼 이어지면 같은 `set_no`다.
6. 메모리의 최신 파형만 수신 화면 그래프로 그린다. 이 계산은 통계 테이블과 별개다.

### CSV packet_no (ADXL 세트 번호)

센서의 seq는 MAC 하나에 카운터 하나다. 다만 X/Y/Z가 같은 값을 나눠 갖는 것이 아니라, 축 패킷을 하나 보낼 때마다 하위 4비트가 1씩 오른다(X `1A` → Y `2B` → Z `3C` → 다음 X `1D`). 그래서 Z가 빠져도 다음 X의 seq가 이어지지 않으므로 새 세트로 바로 알 수 있다. DB의 `set_no`(`grouping.continues_set`)는 이 규칙으로 묶어서 축이 빠져도 어긋나지 않는다.

문제는 CSV의 `packet_no`다. 이것은 센서 seq가 아니라 CSV를 쓰는 쪽이 붙이는 행 번호다.

- 수신기 V057은 축 파일마다 따로 세지만, X/Y/Z가 다 모인 세트만 쓰므로 세 파일의 번호가 늘 같다.
- 예전 웹(`save_csv.py`)은 축 패킷을 도착 즉시 쓰면서 축 파일마다 따로 셌다. Z가 한 번 빠지면 그 뒤로 Z 파일 번호가 하나씩 밀려, 같은 번호의 X/Y/Z가 서로 다른 측정이 된다.
- 발생기 v070은 `packet_no`가 같은 행을 한 세트로 묶는다. 시각 검사(X ≤ Y ≤ Z, 30초 안)만 하므로 측정 주기가 짧으면 밀린 번호를 걸러내지 못한다.

그래서 웹은 두 곳을 고쳤다.

1. 저장(`save_csv.py`): ADXL `packet_no`를 `continues_set`으로 정한 세트 번호로 쓴다. 축이 빠진 세트는 그 번호의 행이 한두 파일에만 있고, 다음 세트 번호는 세 축이 다시 같이 쓴다. 축 패킷은 지금처럼 도착 즉시 쓴다.
2. 읽기(`load_file.load_adxl_csv_xyz`): 세 파일 모든 행에 숫자 `packet_no`가 있으면 `(MAC, packet_no)`로 묶는다(v070 방식). 축 누락·중복, 디코드 실패, 수신 순서·30초 이탈 세트는 제외한다. 여기에 v070에 없는 seq 검사를 더했다. 같은 번호의 X/Y/Z seq 하위 4비트가 a, a+1, a+2가 아닌 묶음이 하나라도 있으면 그 파일의 `packet_no`는 세트 번호가 아니라고 보고 seq 방식으로 다시 묶는다. 예전 웹 CSV가 이 경우다.

어느 방식으로 묶었는지는 송신 로그 첫 줄 `[ADXL 묶음: …]`에 나온다. `packet_no` 방식은 완전한 세트만 남고, seq 방식은 축이 빠진 세트도 남는다(예전과 같음).

설정 적용으로 retain 전송한 JSON은 `sensor_config`에 남긴다. `CONFIG ?` 조회는 저장하지 않는다. 표시 이름은 `device.display_name`이다.

디코드에 실패한 패킷은 로그만 남기고 CSV와 SQLite 어디에도 저장하지 않는다. SQLite 저장이 실패하면 그 패킷은 CSV에만 남는다. 어느 경우든 다음 패킷 수신은 계속한다.

`packet`에는 받은 값을 바꾸지 않고 넣는다. `samplerate`도 `3200`이면 숫자, `0x0F`처럼 장비 코드면 글자 그대로다. Hz로 바꾸는 일은 나중에 읽는 쪽(그래프, 통계)이 `normalize_sample_rate`로 한다. 저장할 때 계산하지 않아야 수신 스레드가 빨리 끝난다.

## 센서 설정 명령

수신 화면의 버튼은 `POST /api/monitor/command` → `_sensor_config`가 JSON을 만들고 → `publish_config`(STOP은 `stop_to_normal`)가 `S/{MAC}`로 보낸다. 규칙은 수신기 V057 '모드 A command'와 같다(2026-11-20 맞춤).

| 버튼 | SENSORTYPE | QoS | retain | 내용 |
| --- | --- | --- | --- | --- |
| 설정 적용 | 고른 값(1 PIEZO normal, 2 MEMS normal, 3·4 collection) | 1 | O | 주기, Fs+N, PIEZO는 `PIEZO_GAIN`, MEMS는 `MEMS_G_RANGE`(2/4/8/16 → 0/1/2/3) |
| START | 센서 타입 칸에서 고른 값 그대로 (수신기 V057과 같음. collection으로 시작하려면 3 또는 4를 고른다) | 1 | O | 설정 적용과 같은 항목 |
| STOP | 0 → (retain 삭제) → normal | 1 | O | 아래 "STOP 3단계" |
| 현재 설정 조회 | 없음 | 구독 QoS | X | `{"CONFIG":"?"}`. MAC 1개만 |

- `COMMPATH`는 항상 `0`이다. `MEMS_SAMPLE_RATE`는 `0x`를 뺀 코드(`08`~`0F`)다.
- 본문은 JSON만 보낸다. 끝에 CR이나 LF를 붙이지 않는다(V057: CR은 시리얼 전용. 웹은 11-20 전에 `\r\n`을 붙였다).
- JSON이 CR 포함 1024바이트를 넘으면 보내지 않는다(`command_text`).
- retain이라 센서가 나중에 붙어도 마지막 설정을 받는다.
- 측정 주기는 0 이상의 정수만 받는다. 빈칸·소수·음수는 400.
- 여러 MAC 중 일부 전송이 실패해도 나머지는 보내고, 끝에 실패 목록을 오류로 알린다(V057과 같음).
- 설정 적용·START는 PUBACK을 기다리지 않는다(보내기 요청 rc만 본다, V057과 같음).
- 연결된 상태에서만 보낼 수 있다. 끊겨 있으면 `먼저 MQTT에 연결하세요`가 나온다.

### STOP 3단계

MAC마다 QoS 1, retain으로 차례로 보내고, 단계마다 PUBACK을 10초(`PUBACK_TIMEOUT_S`)까지 기다린다. 한 단계라도 실패하면 거기서 멈추고 오류를 낸다(다음 단계·다음 MAC으로 가지 않는다).

1. `{"COMMPATH":"0","SENSORTYPE":"0"}` — collection 종료
2. 빈 메시지(0바이트) — 브로커의 retain 지우기
3. normal 설정 — 종류 칸이 PIEZO면 `SENSORTYPE 1`, MEMS면 `2`. 주기·Fs+N·게인/G Range는 지금 칸 값

- 그래서 STOP 뒤 브로커에는 normal 설정이 남고, 센서는 다음에 깨어나면 normal 측정으로 돌아간다. `sensor_config`에도 그 normal JSON을 `STOP → normal`로 남긴다(대문은 '측정 중지'가 아니라 normal 주기로 판정).
- normal 설정을 만드는 칸 값이 잘못되면(주기 빈칸 등) 1단계부터 아무것도 보내지 않는다.
- S/# 구독으로 2단계의 빈 메시지가 되돌아오면 `설정 retain 삭제 수신` 로그만 남긴다.
- 보내는 데 성공하면 화면의 센서 타입 칸이 normal(1/2)로 돌아간다. 보내는 동안 명령 버튼은 잠긴다.
- MAC 수 × 3 × 10초까지 걸릴 수 있어 이 라우트는 `def`(스레드 풀)다. 기다리는 동안 다른 요청은 막히지 않는다.

### 구독 QoS

수신 모니터 연결 줄의 QoS(0/1/2, 기본 1, V057 `DEFAULT_QOS`와 같음)로 `V/#`, `D/#`, `S/#`를 구독하고, 현재 설정 조회도 이 QoS로 보낸다. 연결 값과 함께 `app_setting` 'mqtt'의 `qos`에 기억하고, 서버가 다시 켜질 때 같은 QoS로 다시 붙는다. 11-20 전에 저장한 값에는 `qos`가 없어서 1로 붙는다. 센서 명령(설정 적용·START·STOP)은 이 값과 상관없이 늘 QoS 1이다.

## 그래프 계산

파일 분석과 DB 분석은 같은 `graph_curves.py`를 쓴다. 차이는 패킷이 파일에서 왔는지, SQLite에서 왔는지뿐이다.

- RAW와 FFT는 연 패킷의 샘플 배열로 계산한다.
- 화면 단위, 필터, 창, 로그 눈금은 그래프에만 쓰이고 저장된 HEX를 다시 쓰지 않는다.
- ADXL X/Y/Z는 한 세트로 묶어 눈금을 맞추고, SRSS는 세 축 스펙트럼으로 만든 가로선이다.
- 계산이 끝난 뒤에만 `view_pack.py`가 브라우저로 보낼 점을 줄인다.

### Window 보정 (수신기 V057, 발생기 v070)

PIEZO와 MEMS 그래프 옵션에 `Window 보정`이 있다. 세 화면(수신 모니터, CSV 분석, DB 분석)이 같은 서버 계산(`signal.fft_peak`의 `correction`)을 쓴다. 기본은 `진폭 보정`이고, 이 값은 예전 FFT와 똑같다.

| 모드 | FFT 진폭 분모 | 쓰임 | 보정계수 표시 |
| --- | --- | --- | --- |
| 보정 없음 | N | 창 때문에 줄어든 진폭을 되돌리지 않은 값 | `C=1` |
| 진폭 보정 | Σw | 정현파 peak 크기가 맞는다 | `C=N/Σw` (Blackman 약 ×2.38) |
| 에너지 보정 | √(N·Σw²) | 넓게 퍼진 신호의 RMS가 맞는다 | `C=√(N/Σw²)` (Blackman 약 ×1.81) |

- 표시 FFT, FFT 평균, gN에 같은 모드를 적용한다.
- SRSS는 원래 에너지 보정 값이라 `진폭 보정`, `에너지 보정`에서는 같다. `보정 없음`일 때만 `√mean(w²)`를 곱한다(`srss_window_ratio`). SRSS 문구 끝에 `SRSS 에너지 보정` 또는 `SRSS 보정 없음`이 붙는다.
- 보정계수 문구(`window_factor`)는 서버가 만들어 선택 상자 옆에 보여 준다. 축마다 N이 같으면 한 번만 보인다.
- 통계는 FFT가 아니라 Welch PSD 적분이라 Window 보정 모드와 관계없다(2026-11-23부터 `stat_profile`). 화면 선택은 통계 값에 영향이 없다.
- 계산식은 데스크톱 v070의 `window_correction_ratio`, `window_correction_label`, `srss_window_ratio`와 같다. 세 모드와 창 네 종류로 결과가 같은 것을 확인했다.

### PSD (Welch, 발생기 v070)

서랍의 `PSD (Welch)`를 켜면 FFT 그래프 자리에 PSD 밀도를 그린다. RAW와 송신 원본은 그대로다. 계산은 `graph_psd.packet_psd`이고, 데스크톱 v070의 `_plot_packet_psd`와 같은 값이 나오는 것을 확인했다(ADXL 3가지, PIEZO 3가지 설정에서 곡선·합성 SRSS·상태 문구가 모두 같음).

| 칸 | 뜻 |
| --- | --- |
| PSD 구간 N | Welch 한 구간 길이. `전체`면 패킷 전체가 한 구간 |
| Overlap (%) | 구간 겹침. 이동 = N − int(N × overlap / 100) |
| PSD 평균 목표 구간 | 축마다 평균할 구간 수. 채우면 더 읽지 않는다 |

- 패킷 범위: PSD일 때 FFT 평균은 하지 않는다. `FFT 평균 (Linear Averaging)`과 평균 개수는 PSD에 넣을 패킷 범위로만 쓰고, 꺼져 있으면 선택 패킷 하나다.
- 축마다 ① 평균 제거와 FFT용 필터 → ② Welch(Count²/Hz) → ③ 단위 배율의 제곱(PIEZO K 또는 K/감도, ADXL g/LSB) → ④ ADXL 감도 S(f)와 주파수 보정. 주파수축을 f/나눗수로 줄이면 밀도에 나눗수를 곱한다 → ⑤ mm/s면 × (9806.65/2πf)² → ⑥ RMS = √(ΣP·Δf).
- 단위: Count, mV, g(g RMS·g Peak·자동), mm/s 중 하나의 제곱/Hz. PSD는 창 에너지로 나누므로 Window 보정 모드와 무관하다.
- ADXL은 gN 자리에 X+Y+Z 합성 PSD를 그리고, 합성 SRSS = √(Σ(Px+Py+Pz)·Δf)를 그래프 위 `PSD 결과` 칸에 숫자로 보여 준다. 밀도 단위가 아니라서 가로선으로 그리지 않는다. mm/s일 때만 장치 SRSS 한계와 비교해 정상·초과를 표시한다.
- `PSD 연산 단계` 칸에 ①~⑥을 현재 값으로 보여 준다. 정보 칩의 FFT peak는 빼고 축별 PSD RMS를 넣는다.
- 값이 잘못되면(구간 N > 샘플 수, 목표 0 등) RAW는 그대로 그리고 PSD 칸에 오류 문구를 띄운다.

### X/Y/Z 속도 RMS 시간 이력 (발생기 v070)

서랍의 `X/Y/Z 속도 RMS 시간 이력` 버튼이 창을 연다. `graph_psd.velocity_history`가 v070 `calculate_adxl_velocity_history`와 같은 계산을 한다(8세트, 설정 6가지에서 값이 같음).

- 세트마다 PSD를 따로 구한다. 세트끼리 평균하지 않는다. 세트 안에서는 축마다 목표 구간 수까지 Welch 평균.
- 단위는 화면 FFT 단위와 상관없이 mm/s(감도 보정 포함). SRSS = √(X²+Y²+Z²). 한계는 첫 세트 장치의 SRSS 한계.
- 가로축은 X 축 패킷의 원래 수신 시각이다. CSV는 `received_at`, DB는 `packet.received_at`. 재전송 시각이 아니다.
- 시작 세트(기본 = 지금 보는 패킷)와 세트 수(빈칸 = 끝까지)를 바꿔 다시 계산할 수 있다. 표의 줄을 누르면 메인 그래프가 그 세트로 이동한다.
- 축이 빠진 세트, 수신 시각이 없는 세트, PSD 구간이 부족한 세트는 `제외 세트`에 사유와 함께 남는다.
- CSV와 DB 패킷 모두 `_received_at`, `_packet_no` 메타 키를 단다(DB는 `set_no`). `_`로 시작하는 키는 송신 때 빠진다.

### 필터 칸 (HPF / LPF / Notch)

수신 모니터와 분석 화면이 `filters.js` 하나를 쓴다. 줄마다 RAW·FFT 체크와 값 아래에 실제 계산식이 보인다(수신기 V057·발생기 v070과 같은 문구).

| 필터 | 식 |
| --- | --- |
| HPF | \|H(f)\| = 1/√(1+(fc/f)^(2n)), f>0 |
| LPF | \|H(f)\| = 1/√(1+(f/fc)^(2n)) |
| Notch | H(f) = 1 − exp(−(f−f0)²/(2σ²)), σ = 폭/(2√(2ln2)) |

LPF에는 `자동 Fs/2`가 있고 기본으로 켜져 있다(V057·v070과 같음). 켜져 있으면 차단 주파수가 Nyquist라서 LPF를 실제로 걸지 않는다(`lp_cutoff=None`). 직접 차단 주파수를 쓰려면 체크를 끈다. 계산식 줄의 필터 목록에는 `LPF(자동 Fs/2→우회)`로 나온다.

## CSV 데이터 가져오기 (2026-11-24)

설정 '통계 데이터' 탭의 'CSV 데이터 가져오기' 버튼(`POST /api/db/csv-import`) → `csv_import.CsvImportJob`. 이전 웹(SQLite 없음) 때 CSV만 쌓인 운영 서버에서 그 CSV를 `packet`에 넣는다. 통계는 여기서 만들지 않고, 끝난 뒤 '지금 기준으로 다시 계산'으로 모든 기간을 같은 길(packet → 시간별 통계)로 만든다.
(2026-11-23까지의 'CSV로 시간별 통계 다시 만들기'(`csv_stats.py`)를 대신한다. 그 방식은 통계만 만들고 packet에는 없어서, '다시 계산'을 누르면 CSV 기간 통계가 지워지고 분석 화면(DB)에서 옛 기간을 열 수 없었다.)

1. `data_csv_web` 아래(하위 폴더 포함) 모든 `{MAC}_{종류}_sensorData_hex.csv`(ADXL-X/Y/Z, PIEZO, ULTRASONIC)를 찾는다. `_decoded_samples.csv`는 읽지 않는다.
2. 폴더마다 그 폴더 파일들을 수신 시각 순으로 합쳐 **한 줄씩 흘려** 읽는다(파일 전체를 메모리에 올리지 않음). 줄마다 실시간 수신과 같은 디코드로 HEX를 확인하고(`import_csv._item`), 안 되면 건너뛰고 개수를 결과에 남긴다.
3. ADXL은 MAC마다 실시간과 같은 규칙(`grouping.continues_set`, seq 하위 4비트 X→Y→Z)으로 세트를 묶는다. PIEZO·초음파는 패킷 하나가 한 묶음.
4. HEX 약 1MB(ADXL 약 10세트)씩 모아 `Store.import_packets`로 넣는다. 세트는 끊지 않는다. DB 잠금은 이 한 번(중복 확인 + INSERT + commit)만 잡고, 묶음 사이에 5ms 쉰다.
5. 중복: 같은 MAC·종류·축·수신 시각이 이미 `packet`에 있으면(실시간으로 받음, 전에 가져옴, 다른 폴더의 같은 CSV) 그 묶음을 통째로 건너뛴다. 여러 번 눌러도 두 번 들어가지 않는다.
6. 가져온 줄은 `packet.source='csv'`, `set_key = CSV|종류|MAC|n`(n은 MAC·종류마다 가져온 줄의 최대+1부터).
7. `device`: 없는 MAC은 등록. 마지막 수신(`last_at`·배터리·종류·게이트웨이)은 묶음의 가장 늦은 시각이 지금 값보다 늦을 때만 바꾼다(실시간 값은 늘 더 늦어 덮이지 않음). 2026-11-24 전에는 '비어 있을 때만'이라 새 DB에서 첫 묶음 시각에 멈췄다.
8. 막지 못하는 것: **재송신**. 옛 파형을 다시 보내면 수신기가 새 수신 시각으로 CSV에 남기므로 시각 기준 중복 확인을 지나 실측처럼 들어간다. 재송신·시험 폴더는 가져오기 전에 `data_csv_web` 밖으로 옮긴다(`운영-배포-절차.md` '주의 1').

수신 중에 돌려도 되는 이유
- CSV 저장은 CsvWriter 스레드라 DB와 상관없다.
- 실시간 DB 저장(`save_packet`)은 가져오기 묶음 하나의 잠금 시간만 기다린다. 측정(2026-11-24, 로컬 `data_csv_web` 256파일 154MB, 5232줄, 7.5초): 묶음 잠금 대부분 60~100ms, 최대 275ms. 그동안 0.05초마다 저장한 가짜 실시간 패킷의 대기 중앙 2.8ms, 최대 231ms. MQTT keepalive(수십 초)에 비해 짧다.
- 가져온 줄은 실시간 줄보다 큰 id로 끼어든다. 그래서 실시간 세트 묶기(`assign_set`)는 `source=''` 줄만 보고, 통계는 ADXL을 source별로 따로 읽는다(읽기 위치 `stat_cursor` 키: 실시간 `MAC`, 가져온 줄 `CSV|MAC`). 시험에서 가져오는 동안 저장된 실시간 47세트가 모두 X/Y/Z로 묶였다.
- 가져오기와 재계산·기준 저장은 서로 409로 막는다. 매시간 통계는 막지 않는다(가져온 줄도 새 줄로 보고 시간 줄에 더한다. 끝난 뒤 '다시 계산'을 누르면 처음부터 정리된다).
- 한 달치(수 GB)면 수 분~수십 분으로 예상한다(로컬 154MB 7.5초 기준, 디스크 속도에 따라 다름).

명령줄 `python -m web_db.src.import_csv 폴더 --reset`은 그대로 있다(서버를 멈추고 DB를 비운 뒤 실시간 길로 넣음). 운영 서버에서는 위 버튼을 쓴다.

## CSV 폴더로 DB 채우기

수신기가 남긴 CSV 폴더(예: `D:\20261106_100046`)를 실시간 수신과 같은 길로 `packet`에 넣는다. 8081 서버와 DB를 연 다른 프로그램(DB Browser 등)을 닫고 실행한다.

```
python -m web_db.src.import_csv D:\20261106_100046 --reset   # 지우고 채우기
python -m web_db.src.import_csv D:\20261106_100046           # 지우지 않고 더 넣기
```

서버를 켠 채로 옛 CSV를 더 넣을 때(운영 서버)는 이 명령 대신 설정 'CSV 데이터 가져오기'를 쓴다(위 절). 이 명령의 '지우지 않고 더 넣기'는 중복을 확인하지 않아, DB에 이미 있는 기간의 CSV를 넣으면 같은 패킷이 두 번 들어간다.

- `*_sensorData_hex.csv`만 읽는다. HEX를 수신과 같은 디코드 함수로 다시 푼다(`_decoded_samples.csv`는 안 읽음). 디코드가 안 되는 줄은 실시간 수신처럼 건너뛴다.
- 모든 파일의 줄을 `received_at` 순으로 넣는다. `assign_set`이 MAC의 직전 패킷과 seq를 비교해 X/Y/Z 세트를 묶으므로 받은 순서대로 넣어야 한다.
- `--reset`: 먼저 `sensor.sqlite.bak-날짜-시각`으로 백업한다(git 제외). `packet`, `measurement`, `stat_cursor`, `stat_meta`를 지우고 device의 마지막 수신 정보를 비운다. 표시 이름, 한계값, 설정 JSON, MQTT 설정, 평면도와 배치는 남긴다.
- 끝나면 통계 최신화를 한 번 돌린다(`--no-stats`로 끔). 대문·통계의 SRSS 알람 값이 바로 보인다.

## 파일·DB 재전송

`/generator`와 `/studio`는 같은 `ReplayService` 하나를 쓴다. 데이터가 어디서 왔는지만 다르다.

1. 열기
   - `/generator`: 파일을 올리면 임시 파일에 쓰고 `load_file`이 패킷 목록을 만든 뒤 임시 파일은 지운다.
     ADXL X/Y/Z CSV 세 개는 세트로 묶는다(`packet_no` 세트 번호 우선, 안 되면 seq. 위 "CSV packet_no").
     깨진 HEX는 앞부분만 복구해 미리보기에만 쓴다.
   - `/studio`: `POST /api/db/load`가 `packets_for`로 MAC의 최근 N개(최대 200) 또는 기간을 고른다.
     ADXL은 세 축이 다 있는 `set_key`만 고르고 `group_adxl`이 X/Y/Z 세트로 묶는다.
2. 두 경우 모두 결과는 `replay.templates[센서]`에 들어간다. 센서마다 목록 하나다. 그래서 `/studio`에서 DB를 열면
   같은 센서로 `/generator`에 올려 둔 파일 목록을 덮는다. 반대도 같다.
3. 미리보기는 `replay.preview`가 목록의 패킷 하나를 `graph_curves`로 계산하고 `view_pack`으로 점을 줄인다.
4. 전송 시작은 센서마다 스레드를 하나씩 켠다. 고른 위치부터 끝까지 보내고 처음 위치로 돌아가 반복한다.
   - 주기는 화면 값, 또는 자동 주기면 `numofsample / samplerate`다. Jitter와 손실(Loss) 옵션을 적용한다.
   - ADXL은 X, Y, Z를 `xyz_gap` ms 간격으로 보낸다. seq 자동이면 `SequenceState`가 수신 쪽이 한 세트로 묶을 수 있는 seq를 붙인다.
   - 앞부분만 복구한 ADXL 패킷은 보내지 않는다.
   - publish가 실패하면 그 센서 스레드는 로그를 남기고 멈춘다. 브로커에 다시 붙지 않는다.
5. `GET /api/generator/status`는 메모리의 숫자와 로그만 돌려준다. 브로커에 접속하지 않는다.

## 통계

통계는 패킷이 도착할 때 만들지 않는다(수신 저장을 늦추지 않게). 2026-11-23부터 **매시간** 한 번 `update_statistics`가 돈다(정각 뒤 1분 안, `hourly-stats` 스레드). 화면의 최신화 버튼은 없다(2026-11-18, 모두 같은 계산 시각을 보게). 명령줄 `import_csv`도 끝에 한 번 돌린다. 겹쳐도 `Store.stats_lock` 때문에 차례로 돈다.

통계는 MAC마다 한 시간 한 줄이고, 계산 기준은 DB `stat_profile` 버전이다(설정 > 통계 데이터 > '통계 계산 기준'). 자세한 열과 기준은 `docs/web_db-DB명세서.md` "measurement".

최신화 한 번은 MAC마다 다음 순서로 돈다. 패킷 수신은 그동안에도 계속된다.

1. `Store.lock` 안에서 읽기 위치(`stat_cursor`) 뒤의 ADXL 줄을 300줄 읽는다(HEX 칸만).
2. 잠금을 풀고 `complete_sets`로 seq를 보며 X/Y/Z를 다시 묶는다. 세트마다 `stat_profile.set_stats`(필터 → Welch PSD → 보정 → 속도 RMS)를 계산하고, 세트 시각의 '시'로 모은다.
3. `settled_through`로 이번에 다 본 마지막 id를 정한다. 계산에 실패한 세트와 축이 빠진 묶음도 다 본 것이다. 맨 끝 묶음에 Z가 아직 없으면 그 묶음 앞까지만 본 것으로 한다.
4. 다시 `Store.lock` 안에서, 시간 줄이 이미 있으면 세트 수로 가중해 합치고 없으면 넣는다. `stat_cursor`도 같은 commit으로 옮긴다.
5. 10ms 쉬고 300줄이 더 있으면 1로. 모든 MAC이 끝나면 `stat_meta` 시각을 갱신한다.

잠금을 잡는 것은 1과 4의 순간뿐이다. 427세트 전체 재계산(1.6초) 동안 수신 쪽이 잠금을 기다린 시간은 중앙값 0.01ms, 최대 36ms였다(2026-11-23).

재계산(`rebuild_statistics`)은 같은 순서를 위치 0부터 돌고, 새 버전 줄은 끝날 때까지 화면에 안 보인다. 끝나면 한 commit에서 지금 버전을 바꾸고, 다른 버전 줄을 지우고, 읽기 위치를 맞춘다.

`set_no`는 수신할 때 `assign_set`이 붙이고, 그 뒤로 바꾸지 않는다. 통계는 `set_no`를 믿지 않고 seq로 다시 묶으므로, 번호가 어긋나 있어도 통계 결과는 같다. `set_no`는 분석 화면이 세트 단위로 고를 때만 쓴다.

통계 화면의 막대와 RMS 숫자는 고른 기간(24시간 기본·7일·30일·전체, 2026-11-24)의 시간 줄을 세트 수로 가중한 평균이다. PEAK 숫자는 그 기간 최댓값이다. 작은 그래프는 그 기간의 변화다(7일·30일·전체는 하루 한 점, 7일 = 오늘 포함 7일). 센서 현황 경보는 가장 늦은 시간 줄의 SRSS(mm/s)다.

## 값을 자르는 곳

계산에 넣는 샘플과, 화면에 그리려고 줄인 점은 다르다.

| 위치 | 무엇을 자르나 | 계산에 영향 |
| --- | --- | --- |
| `view_pack._envelope_index` | 점이 16384개를 넘을 때만, 구간마다 최소·최대 점을 남긴다 | 없다. RMS, FFT, SRSS를 만든 다음의 전송용 배열이다 |
| `view_pack._rounded`, `_rounded_x` | 화면 Y는 소수 6자리와 유효숫자 7자리 중 자세한 쪽, X는 유효숫자 12자리(2026-11-23) | 없다. 같은 이유다 |
| `signal.fft_peak`의 `x[offset:offset+n]` | Window N이 `전체`가 아닐 때만 가운데 n개로 FFT한다 | 있다. 사용자가 1024처럼 짧은 창을 고른 그래프 FFT만 해당한다. 기본값 `전체`는 패킷 샘플을 그대로 쓴다 |
| `spectrum[:n//2+1]` | 단측 스펙트럼의 0 Hz부터 나이퀴스트까지 | 샘플을 버리는 것이 아니다. 음수 주파수는 양수 쪽과 짝이라 단측에서 접는다 |
| `stat_profile.set_stats` | 자르지 않는다 | 저장된 샘플 전체로 Welch PSD(기준의 구간 N)를 만든다 |
| `store_query`의 조회 개수 1~200 | 분석 화면에 올릴 세트 수 | 한 패킷 안의 샘플 수는 줄이지 않는다 |
| `load_file.recover_adxl_hex_prefix` | 깨진 HEX의 앞쪽 완전한 16비트 단어만 남긴다 | 원본 CSV 파일은 고치지 않는다. 복구본은 미리보기용이고, 잘린 패킷은 재전송에서 뺀다 |
| `graph_curves._decimate_xy` | FFT 왕복 검사 그림만 160점 안쪽으로 줄인다 | 합격 판정은 줄이기 전의 전체 샘플로 한다 |

그래서 화면 점이 패킷 샘플 수보다 적어도, 저장된 값이나 통계 숫자가 그 때문에 줄어들지는 않는다. 그래프 FFT만, 사용자가 Window N을 `전체`가 아닌 값으로 두었을 때 가운데 구간으로 계산한다.

## 알려진 제약과 주의

지금 코드에서 알고 써야 하는 점이다. 고치기 전까지는 이렇게 동작한다.

| 항목 | 지금 동작 | 영향 |
| --- | --- | --- |
| 패킷 저장 때 직전 패킷 조회 | `assign_set`이 `WHERE mac, sensor_type ORDER BY id DESC LIMIT 1`로 직전 ADXL을 찾는다. 인덱스가 `(mac, received_at)`뿐이라 그 MAC의 행을 모두 정렬한다 | 행이 쌓일수록 저장 한 번이 느려진다. 시험 DB에서 같은 MAC 1만 행 약 4 ms, 10만 행 약 46 ms, 50만 행 약 234 ms. `(mac, sensor_type, id)` 인덱스를 추가하면 0.3 ms 안팎이다. 저장이 느려지면 수신 스레드와 수신 화면 갱신도 같이 늦어진다 |
| 통계 읽기, `/studio` 최근 N세트 | 같은 인덱스 문제로 그 MAC의 행을 정렬하거나 `set_key`로 묶는다 | 패킷이 많을수록 느려진다. 위 인덱스로 통계 읽기는 함께 빨라진다 |
| `GET /api/db/devices` | MAC마다 `packet`의 전체 행 수를 센다 | 분석·설정 화면에서 MAC 목록을 열 때 전체 패킷 수에 비례해 느려진다 |
| MQTT 끊김 | 고쳤다. 구독을 `_on_connect`에서 하므로 paho가 다시 붙으면 다시 구독한다. clean session이라 구독 QoS가 1이어도 끊긴 동안 온 메시지는 받지 못한다 | 끊긴 동안의 측정은 저장되지 않는다 |
| retain 설정으로 생기는 장치 | 구독하면 브로커가 가진 `S/{MAC}` retain 설정을 모두 받아 `sensor_config`에 남기고, `device`에도 그 MAC이 생긴다 | 데이터를 보낸 적 없는 MAC도 대문·설정·통계 목록에 나온다(대문에서는 '기록 없음') |
| 수신 그래프 옵션 | `MonitorService.view_options`, `window`가 서버에 하나다 | 브라우저 두 개로 수신 화면을 열면 한쪽에서 바꾼 옵션이 다른 쪽에도 적용된다 |
| 재전송 목록 | `/generator`와 `/studio`가 `replay.templates`를 같이 쓴다 | 한 화면에서 연 데이터가 다른 화면의 같은 센서 목록을 덮는다 |
| 보존 기간 | `packet`을 지우지 않는다. 한 줄이 약 55~90 KB(HEX와 디코드 샘플을 함께 저장)다 | 1초 주기 ADXL 한 대면 하루 수 GB가 늘어난다. 용량과 위의 조회 속도에 영향을 준다 |
| 1 LSB(g) | 통계는 최신화하는 순간의 값을 쓴다 | 값을 바꾸면 이전 구간과 이후 구간의 눈금이 섞인 채 전체 평균이 난다 |
| 통계 화면 표시 이름 | 이름을 HTML에 그대로 넣는다 | 이름에 `<` 같은 글자가 있으면 화면이 깨진다 |
| 접속 | 인증이 없다. 기본 실행은 `127.0.0.1`에만 연다 | `--host 0.0.0.0`으로 열면 같은 망의 누구나 센서 명령을 보낼 수 있다 |

# web_db DB 명세서

기준은 `http://127.0.0.1:8081` 의 `web_db/sensor.sqlite`다. 이 문서는 통계를 위해 정한 저장 계획이다. 수신 화면의 RAW, FFT는 이 테이블을 쓰지 않는다.

DB로 바꾼 이유는 CSV다. 패킷은 연결할 때마다 폴더에 계속 쌓이고, 통계를 만들려면 그 파일 전부를 찾아 해당하는 CSV를 모두 읽어야 했다. 원본은 `packet`에 쌓아 두고, 통계 숫자는 그 테이블만 읽어 `measurement`에 만든다.

## 역할

| 테이블 | 역할 |
| --- | --- |
| `device` | MAC 명부. 표시 이름과 마지막 수신 정보. MAC당 한 줄 |
| `packet` | 수신 원본. 디코드된 패킷마다 한 줄씩 계속 추가한다. 통계 숫자는 없다 |
| `sensor_config` | 그 MAC의 마지막 설정 JSON. 웹이 retain 전송한 것과, 구독 중 `S/{MAC}`로 받은 설정(브로커 retain 포함, 데스크톱이 보낸 것도)을 같이 남긴다. MAC당 한 줄 |
| `srss_limit` | 설정 화면에서 저장한 MAC별 SRSS 한계(mm/s RMS). 통계 막대와 그래프 기준선이 읽는다 |
| `measurement` | 통계 결과. MAC마다 **한 시간 한 줄**(2026-11-23, 전에는 최신화 구간마다 한 줄). 늦게 온 세트는 그 시간 줄에 합친다 |
| `stat_profile` | 통계 계산 기준 버전(g/LSB, MEMS 보정, 필터, Window, Welch). 저장할 때마다 새 줄, 지우지 않는다 |
| `stat_cursor` | 통계가 이미 본 마지막 packet id. 다음 최신화는 그 다음부터 읽는다. 키: ADXL 실시간 `MAC`, ADXL 가져온 CSV `CSV|MAC`, PIEZO `PIEZO|MAC` |
| `stat_meta` | `measurement`를 마지막으로 만든 시각. 키는 `measurement` 하나다. 매시간 스케줄러가 이 시각의 '시'로 이번 시간에 돌았는지 본다 |
| `app_setting` | 키 하나에 JSON 하나인 앱 설정. `mqtt`(설정 화면의 수신 Broker·Port·Topic·CSV·자동 연결), `stat_profile_active`(지금 통계 기준 버전 `{"id": n}`) |
| `floor_plan` | 대문 평면도 이미지 정보. 여러 장(층·구역마다 한 장, 화면은 탭). 이미지 파일은 DB 옆 `site_files/`에 둔다 |
| `site_state` | 센서별 지금 상태(경보·끊김·주의·정상·대기)와 그 상태가 시작된 시각(`since`). `site_watch`가 1분마다 고친다. MAC당 한 줄 |
| `site_state_history` | 상태가 바뀔 때마다 한 줄(`prev_state` → `state`, `changed_at`). 수신이 꺼져 있을 때는 기록하지 않는다. 180일보다 오래된 줄은 지운다 |
| `sensor_placement` | 평면도 위 센서 아이콘 위치. x·y는 이미지 폭·높이에 대한 비율(0~1). 평면도 한 장에 센서 여러 개, 센서 하나는 평면도 한 장에만 |

통계의 저장소는 `measurement` 한 곳이다.

`srss_limit`의 키는 MAC을 대문자로 바꾸고 `:`와 `-`를 뺀 문자열이다. 줄이 없거나 값이 0 이하이면 4.5를 쓴다. 경보 판정이 아니라 기준선이다.

## 스키마를 만드는 방식

서버가 켜질 때 `Store._migrate`가 `CREATE TABLE IF NOT EXISTS`로 테이블과 인덱스를 만든다. 이미 있는 테이블은 건드리지 않는다. 예전 형식 DB를 바꾸는 이전 코드는 없다. 열을 더하거나 바꿀 때는 그 DB를 직접 고치거나 새 파일로 시작한다.

옛 `daily_stat` 테이블은 쓰지 않으므로 켤 때 `DROP TABLE IF EXISTS`로 지운다.

### 기존 DB로 새 버전을 켤 때 (2026-11-23)

서버가 켜질 때 `Store._migrate`와 `routes/db.start_daily_stats`가 차례로 한다. 손으로 할 일은 없다.

1. 새 표 `stat_profile`을 만들고, `measurement`에 새 열(`profile_id`, `hour`, `x_mms` 등)을 붙인다.
2. 지금 코드가 읽지도 쓰지도 않는 옛 열을 지운다: `device.asset_no`, `measurement.packet_id`, `packet_x_id`, `packet_y_id`, `packet_z_id`(작은 표라 바로 끝난다. 실패하면 그대로 두고 서버는 뜬다).
3. 통계 기준 버전이 없으면 첫 버전(v1)을 만든다. 값은 MEMS JSON 파일(`/data`에 없으면 이미지에 든 기본 파일) + HPF 10Hz·LPF 1000Hz.
   - `device.location`은 11-23에 지웠다가 11-24부터 설정 > 장치 '위치' 칸으로 다시 쓴다. 지우는 목록에서 뺐고, 없으면(이미 지운 DB) 빈 값으로 다시 만든다. 지워졌던 옛 값은 돌아오지 않는다.
4. 지금 버전이 없으면 v1로 `packet` 전체를 백그라운드로 다시 계산한다. 끝나면 v1이 지금 버전이 되고 옛 하루 통계 줄은 지워진다. 그 전까지 화면·경보는 옛 하루 통계를 보인다.

SQLite 파일이 아예 없는 곳(운영 서버처럼 CSV만 있는 곳)도 같다. 빈 DB가 만들어지고 v1이 생기고, 패킷이 없어 재계산은 0세트로 바로 끝난다(v1이 지금 버전). 그 뒤 설정 > 통계 데이터 > 'CSV 데이터 가져오기'로 `data_csv_web`의 CSV를 `packet`에 넣고(2026-11-24), '지금 기준으로 다시 계산'을 누르면 모든 기간 통계가 생긴다(`web_db-구성과-데이터흐름.md` 'CSV 데이터 가져오기').

빈 DB 대신 **설정만 옮긴 DB**로 시작할 수 있다(2026-11-24, `python -m web_db.src.export_settings 새경로\sensor.sqlite`).
`device`(이름·위치·표시, 마지막 수신은 비움)·`srss_limit`·`stat_profile`+`app_setting.stat_profile_active`·`floor_plan`·`sensor_placement`와
`site_files/`를 옮기고, `packet`·통계·상태 기록은 옮기지 않는다. MQTT 설정(`--with-mqtt`)과 `sensor_config`(`--with-sensor-config`)는 고를 때만.
v1이 이미 지금 버전이라 시작 재계산은 돌지 않는다. 순서와 주의는 `운영-배포-절차.md`.

### packet.decoded_samples

HEX를 푼 샘플 글자다. 2026-11-23부터 읽는 곳이 없다(통계·분석 모두 `sensor_data_hex`를 직접 푼다). DB 크기의 절반쯤이다.

- 새 DB에는 이 열을 만들지 않는다.
- 옛 DB는 열이 남아 있으면 새 줄에 빈 글자만 넣는다(NOT NULL이라).
- 이미 쌓인 값을 지우고 파일을 줄이려면 서버를 멈추고 `python -m web_db.src.compact_db`를 실행한다(Docker: `docker compose stop web` → `docker compose run --rm web python -m web_db.src.compact_db` → `docker compose up -d`). 먼저 `sensor.sqlite.bak-날짜-시각` 백업을 만들고, 열을 지운 뒤 VACUUM 한다. packet 표를 통째로 다시 쓰므로 서버 시작 때 자동으로 하지 않는다. 잠깐 DB 크기만큼 빈 디스크가 더 필요하다. 로컬 DB 97.4MB → 48.4MB(2026-11-23).

| 테이블 | 열 |
| --- | --- |
| `device` | `mac`(PK), `display_name`, `location`(설치 위치, 2026-11-24 다시 씀), `visible`(설정 '표시', 1 기본·0이면 센서 현황·분석·통계에서 숨김, 2026-11-24), `created_at`, `updated_at`, `last_gw`, `last_battery`, `last_sensor`, `last_at` |
| `packet` | `id`(PK, AUTOINCREMENT), `mac`, `received_at`, `gw_mac`, `sensor_type`(PIEZO·ADXL·ULTRASONIC), `axis`(X·Y·Z, PIEZO, H), `seq`, `tick`, `battery`, `sample_rate`, `sample_count`, `payload_bytes`, `sensor_data_hex`, `min_khz`, `interval_hz`, `freq_start_khz`, `freq_end_khz`, `set_key`, `set_no`, `source` |
| `measurement` | 아래 measurement 절 |
| `srss_limit` | `mac`(PK, 정규화한 키), `limit_mm_s`(위험 기준), `good_mm_s`(양호 상한), `caution_mm_s`(주의 상한), `updated_at`. 양호·주의 상한이 비어 있으면(NULL) 위험 기준 × 1.4/4.5, × 2.8/4.5(2026-11-23, 옛 DB에는 시작할 때 열을 붙인다) |
| `sensor_config` | `mac`(PK), `action`(설정 적용·START·`STOP → normal`·`retain 수신`), `payload`(JSON 문자열), `updated_at`. STOP은 3단계 끝에 브로커에 남는 normal 설정을 저장한다(2026-11-20 전에는 `STOP`과 `SENSORTYPE 0`) |
| `stat_cursor` | `mac`(PK), `through_id`, `updated_at` |
| `stat_profile` | `id`(PK, AUTOINCREMENT), `settings`(JSON, 아래 '통계 계산 기준'), `note`(메모), `created_at` |
| `stat_meta` | `key`(PK), `updated_at` |
| `app_setting` | `key`(PK), `value`(JSON 문자열), `updated_at`. `mqtt` 값: `host`, `port`, `topic`, `qos`(구독 QoS, 2026-11-20부터. 없으면 1), `csv`, `auto` |
| `floor_plan` | `id`(PK, AUTOINCREMENT), `name`, `file`(site_files 안 이름 `floor_plan_{id}.{확장자}`), `width`, `height`(원본 픽셀), `sort_order`(탭 순서, 한 장만 쓰던 DB에는 시작할 때 열을 붙인다), `created_at`, `updated_at` |
| `sensor_placement` | `plan_id`(→floor_plan, 삭제 시 함께 삭제), `mac`, `x`, `y`(0~1), `updated_at`. PK는 (`plan_id`, `mac`). MAC 하나가 한 평면도에만 있게 하는 것은 `replace_placements`가 지킨다 |

인덱스는 `packet(mac, received_at)`, `packet(set_key)`, `measurement(mac, received_at)`, `measurement(mac, last_at)`, `measurement(profile_id, mac, hour)`이다.

2026-11-23에 `measurement`에 열을 붙였다(`profile_id`, `hour`, `x_mms`, `y_mms`, `z_mms`, `srss_mms_max`, `first_id`, `notes`). 서버가 켜질 때 없으면 `ALTER TABLE`로 붙인다. 옛 하루 통계 줄은 이 칸이 비어 있고(`profile_id` NULL), 첫 재계산이 끝나면 지워진다.

`packet.id`는 AUTOINCREMENT라 줄을 지워도 번호를 다시 쓰지 않는다. 통계 읽기 위치(`through_id`)가 이 번호에 기대므로 이 성질이 필요하다.

## packet

측정 JSON이 디코드되면 바로 한 줄 넣는다. 이 시점에는 RMS, PEAK, SRSS를 계산하지 않는다. MQTT가 끊겼다가 다시 붙어도 세션 줄은 만들지 않는다. 같은 MAC의 `device` 줄을 갱신하고 `packet`에 이어서 넣는다. `set_no`는 연결이 아니라 직전 패킷의 seq로 정한다.

ADXL은 축마다 한 줄이다. seq 상위 4비트는 축, 하위 4비트는 축을 보낼 때마다 1씩 증가한다. `10`, `21`, `32`처럼 이어지면 같은 세트다. 축이 빠져도 도착한 줄은 남긴다. 빠진 축 때문에 저장을 멈추지 않는다.

PIEZO와 초음파도 같은 테이블에 쌓인다. 통계 계산의 대상은 아니다.

지우는 주기는 없다. 통계를 만들 때 CSV를 다시 열지 않는 대신, 이 테이블이 원본이다.

## measurement

2026-11-23부터 MAC마다 **한 시간 한 줄**이다. 계산 기준은 `stat_profile` 버전이고, 줄마다 쓴 버전(`profile_id`)이 남는다. 화면·경보는 **지금 버전**(`app_setting.stat_profile_active`) 줄만 본다.

### 언제 만드나

| 때 | 하는 일 | 코드 |
| --- | --- | --- |
| 매시간 (정각 뒤 1분 안) | 지금 버전으로, 읽기 위치(`stat_cursor`) 뒤의 새 세트만 더한다 | `routes/db._hourly_stats` → `Store.update_statistics` |
| 서버 시작 때 지금 버전이 없을 때 (하루 통계에서 업그레이드한 직후) | 첫 버전을 만들고 그 버전으로 `packet` 전체를 다시 계산 | `start_daily_stats` → `stats_job.StatRebuildJob` |
| 설정에서 기준을 저장 / '지금 기준으로 다시 계산' | 그 버전으로 `packet` 전체를 다시 계산, 끝나면 그 버전이 지금 버전 | `POST /api/db/stat-profile`, `/api/db/stat-rebuild` |
| 설정 'CSV 데이터 가져오기'(2026-11-24) | 통계는 만들지 않는다. CSV를 `packet`(source='csv')에 넣을 뿐이고, 끝난 뒤 '지금 기준으로 다시 계산'(그 전에도 매시간 최신화가 가져온 줄을 새 줄로 더함) | `csv_import.CsvImportJob` |
| 명령줄 `python -m web_db.src.import_csv` | CSV를 넣은 뒤 매시간 최신화와 같은 일을 한 번 | `Store.update_statistics` |

통계 화면의 최신화 버튼과 `POST /api/db/update-stats`는 없다(2026-11-18). 사람마다 다른 시각에 최신화하면 보는 값의 기준 시각이 달라 혼란스럽기 때문이다. 수신 콜백에서는 계산하지 않는다.

### SQLite를 오래 잡지 않는다

수신 저장이 가장 중요하므로 통계는 DB 잠금(`Store.lock`)을 짧게만 잡는다(`store_query._compute_statistics`).

1. (잠금) 그 MAC의 읽기 위치 뒤 ADXL 줄을 **300줄씩** 읽는다(약 100세트). HEX 칸만 읽는다. ADXL이 끝나면 PIEZO도 같은 순서(패킷 하나 = 측정 하나).
2. (잠금 없음) seq로 X/Y/Z 세트를 묶고 세트마다 계산한다(세트당 약 5ms).
3. (잠금) 시간 줄을 넣거나 합치고 읽기 위치를 같은 commit으로 저장한다.
4. 10ms 쉬고 다음 300줄.

측정(2026-11-23, 427세트 전체 재계산 1.6초): 그동안 수신 쪽이 잠금을 기다린 시간은 중앙값 0.01ms, 최대 36ms.

통계 작업은 한 번에 하나만 돈다(`Store.stats_lock`). 매시간 최신화·재계산이 겹치면 뒤에 온 쪽이 기다린다. CSV 가져오기와 재계산은 서로 409로 막는다. 같은 세트가 두 번 들어가지 않는다.

### 세트 하나 계산 (`stat_profile.set_stats`)

분석 데이터 저장(ZIP)·속도 RMS 시간 이력과 같은 `graph_psd.axis_psd`를 쓴다. 기준만 화면 서랍이 아니라 `stat_profile` 버전이다.

- 축마다: RAW − 평균 → 필터(HPF/LPF/Notch) → Welch PSD × (g/LSB)² → MEMS 주파수·감도 보정 → 가속도 PSD Pa[g²/Hz] → 속도 PSD Pv = Pa × (9806.65/2πf)² → 속도 RMS = √(ΣPv·Δf)
- `srss_mms` = √(vX² + vY² + vZ²), `srss`(g) = √(Σ aRMS²) (aRMS = √(ΣPa·Δf))
- `rms`(g) = √(Σ 축별 시간영역 RMS²), `peak`(g) = |필터 출력|의 최댓값. 둘 다 필터 출력 기준
- 세트의 Fs에서 쓸 수 없는 필터(차단 주파수 ≥ Fs/2)는 그 세트에서만 끄고 `notes`에 남긴다.
- 축이 빠지거나, 세 축의 샘플 수·Fs가 다르거나, 계산이 안 되는 세트는 넣지 않는다(응답 `failed_sets`). 읽기 위치는 넘긴다.
- 맨 끝 묶음에 Z가 아직 없으면 그 묶음은 다음 최신화로 남긴다.

### 한 시간 한 줄

세트 시각 = X/Y/Z 중 마지막 수신 시각. 그 시각의 '시'(`YYYY-MM-DDTHH:00:00`)가 묶음 키다. 같은 시간에 세트가 여러 개면(수집 모드 START 등) 평균하고, 늦게 온 세트는 이미 있는 줄에 세트 수로 가중해 합친다(`stat_profile.merge_rows`). 그래서 나눠서 최신화해도 한 번에 계산한 것과 같다(2026-11-23 확인: 차이 0).

나중에 세트별 저장이 필요하면 `set_stats` 결과를 그대로 넣으면 된다. 시간 묶음은 그 위의 단계일 뿐이다.

| 열 | 내용 |
| --- | --- |
| `mac` | 장치 |
| `hour` | 묶음 키 `YYYY-MM-DDTHH:00:00` |
| `profile_id` | 계산에 쓴 `stat_profile.id` |
| `srss_mms` | 그 시간 세트 SRSS(mm/s RMS)의 평균. 통계 막대·센서 현황 경보가 읽는 값 |
| `srss_mms_max` | 그 시간 세트 SRSS(mm/s)의 최대 |
| `x_mms`, `y_mms`, `z_mms` | 축별 속도 RMS(mm/s) 평균 |
| `srss` | 세트 가속도 SRSS(g)의 평균 |
| `rms` | 세트 시간영역 RMS(g)의 평균 |
| `peak` | 세트 PEAK(g)의 최대 |
| `set_count` | 그 시간 세트 수. 평균의 가중치 |
| `first_at`, `last_at` | 그 시간 세트 중 가장 이른·늦은 수신 시각. `received_at` = `last_at` |
| `first_id`, `through_id` | 그 시간 세트의 가장 작은·큰 packet id(원본 범위). 2026-11-24 전 CSV 통계 줄은 0 |
| `g_per_lsb` | 기준의 g/LSB |
| `sample_rate`, `sample_count` | 세트가 모두 같으면 그 값, 섞이면 비움(Hz) |
| `set_key` | `H|{버전}|{MAC}|{시}`. 2026-11-24 전 CSV 통계는 `CSV|{버전}|{MAC}|{시}`(다시 계산하면 지워짐) |
| `notes` | 계산 메모 JSON 목록(그 Fs에서 끈 필터 등). 없으면 빈 글자 |
| `computed_at` | 이 줄을 마지막으로 쓴 시각 |
| `unit` | `g RMS` |

`packet.sample_rate`에는 수신 JSON의 `samplerate`가 그대로 있다. 계산은 그래프와 같은 `normalize_sample_rate`로 장비 코드(10~15, `0x0A`~`0x0F`)를 Hz로 읽는다.

### 통계 계산 기준 (`stat_profile`)

`settings` JSON(검사·기본값: `stat_profile.validate`).

| 키 | 내용 | 첫 버전 |
| --- | --- | --- |
| `g_per_lsb` | 1 LSB당 g | `mems_lsb_scale.json` 값 |
| `correction` | MEMS 주파수·감도 보정(`frequency_enabled`, `sensitivity_enabled`, `frequency_divisor`, `low_hz`, `high_hz`, `low_sensitivity`, `mid_intercept`, `mid_slope`, `high_sensitivity`) | `mems_frequency_sensitivity.json` 값 |
| `filters.hpf` / `filters.lpf` | `{on, hz, order}` | HPF 켬 10Hz 4차 / LPF 켬 1000Hz 4차 |
| `filters.notch` | `{on, hz, width}` | 끔 975Hz 20Hz |
| `window` | `{name, beta}` | Blackman, 8.6 |
| `psd` | `{segment, overlap, target}` | 전체, 50%, 30 |

HPF 10Hz + LPF 1000Hz를 기본으로 한 이유(2026-11-23 실측, 같은 6세트 SRSS mm/s): 필터 없음 3.6~11.9로 흔들림, HPF 10Hz + LPF 1000Hz 2.3~2.5로 안정. 10Hz 아래 성분(드리프트 등)이 값을 좌우했다. 10~1000Hz는 진동 속도 평가에 보통 쓰는 대역이다.

- 그래프·분석 화면은 여전히 JSON 파일(`mems_lsb_scale.json`, `mems_frequency_sensitivity.json`)과 서랍 값을 쓴다. 통계 기준과는 따로다. 설정 화면의 '그래프 설정 파일 값 가져오기'로 환산 칸을 파일 값에 맞출 수 있다.
- 저장하면 새 버전이 된다. 재계산이 끝나야 지금 버전이 바뀐다. 그 전까지 화면·경보는 이전 버전 통계를 본다. 재계산이 실패하면 새 버전 줄만 지우고 지금 버전은 그대로다.
- 재계산이 끝나면 다른 버전 줄은 지운다(packet이 원본이라 언제든 다시 만들 수 있다).

## 통계 화면이 읽는 것

통계 화면은 `measurement`의 지금 버전 줄만 읽는다. 페이지를 열거나 다시 그릴 때는 계산하지 않는다.

`GET /api/db/dashboard?period=24h|7d|30d|all`(기본 24h, 2026-11-24). SRSS 막대·RMS 숫자는 고른 기간(위 기간 버튼: 24시간 기본·7일·30일·전체, 2026-11-24)의 시간 줄을 세트 수로 가중한 평균이고, PEAK 숫자는 그 기간의 최댓값이다. 작은 그래프도 그 기간의 RMS·PEAK 변화다(24시간은 한 시간 한 점 'MM-DD HH시', 7일·30일·전체는 하루 한 점 — 7일·30일은 오늘 포함 7·30일(0시 기준), 'MM-DD': 하루 RMS는 세트 수 가중 평균, PEAK는 그날 최대). 기간 조건은 `COALESCE(hour, last_at) >= 시작`(옛 하루 통계는 hour가 비어 last_at). '최근'은 기간과 상관없이 가장 늦은 시간 줄(센서 현황 경보와 같은 값)이다. 통계 줄이 없고 `device.last_sensor`가 ULTRASONIC인 MAC은 넣지 않는다(초음파는 판정 기준이 없어 통계를 계산하지 않음).

분석 화면이 파형을 열 때는 `measurement`를 쓰지 않는다. `packet`에서 고른 세트의 샘플을 읽는다.

## 구현

통계 최신화는 이 명세대로 한다. 수신 중에는 `packet`만 늘리고 `measurement`는 건드리지 않는다. 매시간 스케줄러는 아직 넣지 않은 패킷만 읽어 시간 줄을 넣거나 합친다.

### PIEZO 줄 (2026-11-23)

| 칸 | ADXL | PIEZO |
| --- | --- | --- |
| `sensor_type` | `ADXL` | `PIEZO` |
| `set_key` | `H|버전|MAC|시`(옛 CSV 통계 `CSV|…`) | `HP|버전|MAC|시`(옛 CSV 통계 `CSVP|…`) |
| `set_count` | X/Y/Z 세트 수 | 패킷 수 |
| `srss_mms` | √(vX²+vY²+vZ²) | 그 한 축의 vRMS(mm/s) |
| `srss` | 가속도 SRSS(g) | 그 축 가속도 RMS(g, PSD 적분) |
| `x_mms`·`y_mms`·`z_mms` | 축별 vRMS | 비움(NULL) |
| `g_per_lsb` | g/LSB | g/Count = 1000 × Vmax / Amax / 감도 |
| 읽기 위치 `stat_cursor.mac` | 실시간 줄 MAC, 가져온 CSV 줄 `CSV|MAC` | `PIEZO|MAC`(source 구분 없음) |

- 계산: `stat_profile.piezo_stats`. 분석 화면 PIEZO PSD와 같은 `graph_psd.axis_psd("PIEZO")`에 통계 기준의 필터·창·PSD 구간을 쓴다. 환산은 기준의 `piezo` {vmax 3.89, amax 4096, sensitivity 100}(분석 화면 PIEZO 칸 기본값). 기준 요약 글자에 `PIEZO 3.89V/4096·100mV/g`로 보인다.
- Fs가 패킷마다 달라도(2560·12800·25600 Hz, 8192점) 같은 평가 대역(기본 10~1000 Hz)의 면적이라 한 시간 줄로 평균한다. Fs가 높을수록 패킷 길이가 짧고 Δf가 커서 10 Hz 부근이 덜 안정적이다.
- 판정: 센서 현황은 가장 늦은 통계 줄(ADXL·PIEZO 중)을 본다. PIEZO 줄이면 판정하지 않는다(`site_status.JUDGED_KINDS = ("ADXL",)`). 환산이 확정되면 여기에 `"PIEZO"`를 더하면 MEMS와 같은 네 구간이 된다.
- 실측(복사본 DB): D25871312270 평균 148 mm/s(28~208), 865872312270 1.2 mm/s. MEMS(1~6)보다 수십 배라 환산 확인이 필요하다(`단위환산.md` 15절).

### packet.source (2026-11-24)

| 값 | 뜻 | set_key |
| --- | --- | --- |
| `''`(기본) | 실시간 수신(`save_packet`). 옛 DB의 줄도 모두 이것 | `종류|MAC|n` |
| `csv` | 설정 'CSV 데이터 가져오기'로 넣은 옛 CSV 줄(`import_packets`) | `CSV|종류|MAC|n` |
| `fill` | 실측이 빈 시간을 그 센서의 실측 파형으로 한 시간마다 채운 줄(2026-11-24 정리 작업). 시각만 새로, 나머지 칸은 원본 실측 그대로 | `FILL|종류|MAC|n` |

- 왜: 가져오기는 수신 중에 돌아 옛 줄과 실시간 줄이 id 순서로 섞인다. 실시간 세트 묶기(`grouping.assign_set`)는 `source=''` 줄만 보고, 통계(`store_query._compute_adxl`)는 ADXL을 source별로 따로 읽는다(읽기 위치 `MAC` / `CSV|MAC` / `FILL|MAC`). PIEZO는 묶을 축이 없어 나누지 않는다.
- 인덱스 `idx_packet_stream(mac, sensor_type, source, id)`: 세트 묶기·통계·줄 세기(설정 화면 안내)가 표 본문(큰 HEX)을 읽지 않게 한다. 옛 DB는 서버 시작 때 칸과 인덱스를 더한다.
- 중복 확인은 `idx_packet_mac_time(mac, received_at)`로 묶음의 수신 시각 범위를 한 번에 읽는다. 키 = MAC·종류·축·수신 시각(CSV `received_at`과 DB `received_at`은 같은 글자).
- `set_no` 최대값은 `MAX(set_no)` 대신 가장 큰 id 줄의 값으로 읽는다(번호는 늘 +1로 매김). `set_no`가 HEX 칸 뒤에 있어 `MAX`는 그 MAC의 모든 줄을 읽게 된다(2026-11-24 측정: 가져오기 묶음 잠금 0.7초의 원인 중 하나). 실시간 PIEZO·초음파 번호 매기기도 같은 방식으로 바꿨다.

### 2026-11-24 packet 정리와 빈 시간 채우기 (로컬 8060 DB)

요청: 재송신으로 들어간 '몇 초마다' 데이터를 지우고, 11-09부터 지금까지 한 시간마다 들어온 것처럼 실측(`data_csv_web/20261106_100046`, 11-06~09 위주)으로 채운다.

- 서버(8060)를 멈추고 한 번만 돌린 작업이다(스크립트는 작업 폴더 밖, 코드에 넣지 않음). 백업: `web_db/sensor.sqlite.bak-20261124-155637`.
- 지움: 실측 폴더에 없는 (MAC, received_at) packet 2,211줄(재송신을 CSV로 가져온 줄, 재송신을 실시간으로 받은 줄, 시험 MAC). 남은 실측 3,033줄.
- 채움: MAC·종류마다 11-09 0시부터 지금까지, 실측 측정이 한 시간(±45분) 안에 없으면 3600초(±4초) 뒤에 측정 하나. 파형은 그 MAC의 11-06~09 실측 측정을 차례로 돌려 쓴다(실측이 6개 미만이면 그 MAC 실측 전부). MEMS는 X→Y→Z 세트, 초음파는 2초 안에 붙은 패킷 묶음, PIEZO는 패킷 하나. 측정 안 시간 간격과 seq·배터리·Fs·N·HEX는 원본 그대로. 8,509줄(`source='fill'`).
  - 실측이 지금까지 있는 165874312270·D25871312270은 빈 시간(35·30번)만 채웠다. 925874312270 MEMS는 실측에 X/Y/Z가 다 있는 세트가 2개뿐이라 그 둘을 번갈아 쓴다.
- device 마지막 수신 정보를 가장 늦은 packet으로 맞추고, 지금 기준(v1)으로 통계를 다시 계산했다(3,784세트, 실패 0).
- 결과: 센서 현황 12대 모두 연결 정상, 통계 7일 = 센서마다 약 160세트·7점. 채운 줄만 지우려면 `DELETE FROM packet WHERE source='fill'` 뒤 '지금 기준으로 다시 계산'.
- 앞으로의 수신(실시간)은 평소처럼 쌓인다. 자동으로 계속 채우지는 않는다.


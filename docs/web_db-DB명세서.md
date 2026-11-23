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
| `stat_cursor` | MAC마다 통계가 이미 본 마지막 packet id. 다음 최신화는 그 다음부터 읽는다. MAC당 한 줄 |
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

옛 `daily_stat` 테이블은 쓰지 않으므로 켤 때 `DROP TABLE IF EXISTS`로 지운다. 이전부터 쓰던 DB에는 지금 코드가 읽지도 쓰지도 않는 열이 남아 있을 수 있다(`device.location`, `device.asset_no`, `measurement.packet_id`, `packet_x_id`, `packet_y_id`, `packet_z_id`). 값이 비어 있을 뿐 동작에는 영향이 없다.

| 테이블 | 열 |
| --- | --- |
| `device` | `mac`(PK), `display_name`, `created_at`, `updated_at`, `last_gw`, `last_battery`, `last_sensor`, `last_at` |
| `packet` | `id`(PK, AUTOINCREMENT), `mac`, `received_at`, `gw_mac`, `sensor_type`(PIEZO·ADXL·ULTRASONIC), `axis`(X·Y·Z, PIEZO, H), `seq`, `tick`, `battery`, `sample_rate`, `sample_count`, `payload_bytes`, `sensor_data_hex`, `decoded_samples`, `min_khz`, `interval_hz`, `freq_start_khz`, `freq_end_khz`, `set_key`, `set_no` |
| `measurement` | 아래 measurement 절 |
| `srss_limit` | `mac`(PK, 정규화한 키), `limit_mm_s`, `updated_at` |
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
| 설정 'CSV로 시간별 통계 만들기' | `data_csv_web` CSV로 가장 최근 버전 통계를 만들고 `measurement`를 통째로 바꿈 | `csv_stats.CsvStatsJob` |
| 명령줄 `python -m web_db.src.import_csv` | CSV를 넣은 뒤 매시간 최신화와 같은 일을 한 번 | `Store.update_statistics` |

통계 화면의 최신화 버튼과 `POST /api/db/update-stats`는 없다(2026-11-18). 사람마다 다른 시각에 최신화하면 보는 값의 기준 시각이 달라 혼란스럽기 때문이다. 수신 콜백에서는 계산하지 않는다.

### SQLite를 오래 잡지 않는다

수신 저장이 가장 중요하므로 통계는 DB 잠금(`Store.lock`)을 짧게만 잡는다(`store_query._compute_statistics`).

1. (잠금) 그 MAC의 읽기 위치 뒤 ADXL 줄을 **300줄씩** 읽는다(약 100세트). HEX 칸만 읽고 큰 `decoded_samples`는 읽지 않는다.
2. (잠금 없음) seq로 X/Y/Z 세트를 묶고 세트마다 계산한다(세트당 약 5ms).
3. (잠금) 시간 줄을 넣거나 합치고 읽기 위치를 같은 commit으로 저장한다.
4. 10ms 쉬고 다음 300줄.

측정(2026-11-23, 427세트 전체 재계산 1.6초): 그동안 수신 쪽이 잠금을 기다린 시간은 중앙값 0.01ms, 최대 36ms.

통계 작업은 한 번에 하나만 돈다(`Store.stats_lock`). 매시간 최신화·재계산·CSV 통계가 겹치면 뒤에 온 쪽이 기다린다. 같은 세트가 두 번 들어가지 않는다.

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
| `first_id`, `through_id` | 그 시간 세트의 가장 작은·큰 packet id(원본 범위). CSV 통계 줄은 0 |
| `g_per_lsb` | 기준의 g/LSB |
| `sample_rate`, `sample_count` | 세트가 모두 같으면 그 값, 섞이면 비움(Hz) |
| `set_key` | `H|{버전}|{MAC}|{시}`. CSV 통계는 `CSV|{버전}|{MAC}|{시}` |
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

막대의 SRSS와 RMS 숫자는 시간 줄을 세트 수로 가중한 전체 평균이다. PEAK 숫자는 시간 PEAK의 최댓값이다. '최근'은 가장 늦은 시간 줄(센서 현황 경보와 같은 값)이다. 작은 그래프는 최근 시간 줄 최대 168개(7일)다.

분석 화면이 파형을 열 때는 `measurement`를 쓰지 않는다. `packet`에서 고른 세트의 샘플을 읽는다.

## 구현

통계 최신화는 이 명세대로 한다. 수신 중에는 `packet`만 늘리고 `measurement`는 건드리지 않는다. 매시간 스케줄러는 아직 넣지 않은 패킷만 읽어 시간 줄을 넣거나 합친다.

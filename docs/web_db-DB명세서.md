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
| `measurement` | 통계 결과. 최신화 한 번에 MAC마다 구간 한 줄을 추가한다. 이전 줄은 지우지 않는다 |
| `stat_cursor` | MAC마다 통계가 이미 본 마지막 packet id. 다음 최신화는 그 다음부터 읽는다. MAC당 한 줄 |
| `stat_meta` | `measurement`를 마지막으로 만든 시각. 키는 `measurement` 하나다 |
| `app_setting` | 키 하나에 JSON 하나인 앱 설정. 지금은 `mqtt`(설정 화면의 수신 Broker·Port·Topic·CSV·자동 연결) |
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
| `stat_meta` | `key`(PK), `updated_at` |
| `app_setting` | `key`(PK), `value`(JSON 문자열), `updated_at`. `mqtt` 값: `host`, `port`, `topic`, `qos`(구독 QoS, 2026-11-20부터. 없으면 1), `csv`, `auto` |
| `floor_plan` | `id`(PK, AUTOINCREMENT), `name`, `file`(site_files 안 이름 `floor_plan_{id}.{확장자}`), `width`, `height`(원본 픽셀), `sort_order`(탭 순서, 한 장만 쓰던 DB에는 시작할 때 열을 붙인다), `created_at`, `updated_at` |
| `sensor_placement` | `plan_id`(→floor_plan, 삭제 시 함께 삭제), `mac`, `x`, `y`(0~1), `updated_at`. PK는 (`plan_id`, `mac`). MAC 하나가 한 평면도에만 있게 하는 것은 `replace_placements`가 지킨다 |

인덱스는 `packet(mac, received_at)`, `packet(set_key)`, `measurement(mac, received_at)`이다.

`packet.id`는 AUTOINCREMENT라 줄을 지워도 번호를 다시 쓰지 않는다. 통계 읽기 위치(`through_id`)가 이 번호에 기대므로 이 성질이 필요하다.

## packet

측정 JSON이 디코드되면 바로 한 줄 넣는다. 이 시점에는 RMS, PEAK, SRSS를 계산하지 않는다. MQTT가 끊겼다가 다시 붙어도 세션 줄은 만들지 않는다. 같은 MAC의 `device` 줄을 갱신하고 `packet`에 이어서 넣는다. `set_no`는 연결이 아니라 직전 패킷의 seq로 정한다.

ADXL은 축마다 한 줄이다. seq 상위 4비트는 축, 하위 4비트는 축을 보낼 때마다 1씩 증가한다. `10`, `21`, `32`처럼 이어지면 같은 세트다. 축이 빠져도 도착한 줄은 남긴다. 빠진 축 때문에 저장을 멈추지 않는다.

PIEZO와 초음파도 같은 테이블에 쌓인다. 통계 계산의 대상은 아니다.

지우는 주기는 없다. 통계를 만들 때 CSV를 다시 열지 않는 대신, 이 테이블이 원본이다.

## measurement

MAC당 통계는 두 층이다. 구간 줄은 하루 스케줄러(또는 명령줄 import_csv)가 돌 때마다 그때까지 아직 넣지 않은 완성 세트만 모아 한 줄 추가한다. 이전 줄은 지우지 않는다. 화면의 전체 값은 이 줄들을 세트 수로 가중해 평균한 것이고, PEAK 전체는 구간 PEAK의 최댓값이다. 작은 그래프는 최근 구간 최대 40개의 RMS와 PEAK다.

갱신 시점은 둘이다.

- 서버가 켜져 있는 동안 하루 스케줄러가 최신화를 실행할 때(자정 뒤 10분 안)
- 명령줄 `python -m web_db.src.import_csv`가 CSV를 넣은 뒤 한 번 실행할 때

통계 화면의 최신화 버튼과 `POST /api/db/update-stats`는 없앴다(2026-11-18). 사람마다 다른 시각에 최신화하면 보는 값의 기준 시각이 달라 혼란스럽기 때문이다.

둘 다 하는 일은 같다. 그 MAC의 `packet` 가운데 읽기 위치(`stat_cursor.through_id`와 `measurement.through_id` 중 큰 값)보다 나중 것만 읽어 완성된 ADXL X/Y/Z 세트를 고르고, 그 구간을 `measurement`에 한 줄 추가한다. 축이 아직 덜 온 끝부분은 다음 최신화로 남긴다. 수신 콜백에서는 하지 않는다.

스케줄러는 서버 프로세스에 스레드 하나(`daily-stats`)다. `routes/db.start_daily_stats`가 켜고, 이미 살아 있으면 새로 만들지 않는다. 10분마다 `stat_meta` 날짜를 보고, 오늘보다 이전이면 최신화한다.

최신화는 한 번에 하나만 돈다. `Store.stats_lock`이 `update_statistics` 전체를 감싼다. 스케줄러와 import_csv가 겹치면 뒤에 온 쪽은 앞의 최신화가 끝날 때까지 기다린 뒤, 앞이 남긴 읽기 위치 다음부터 읽는다. 그래서 같은 구간이 두 번 들어가지 않는다. 세트 계산은 DB 잠금(`Store.lock`) 밖에서 하므로, 그동안 수신 저장은 막히지 않는다.

한 줄에 넣는 값은 그 구간에 새로 들어온 완성 세트만의 결과다. 전체 평균은 줄을 다시 합쳐 만들지, 이 줄 하나가 전체를 덮어쓰지는 않는다.

세트 하나의 숫자는 저장된 샘플 전체로 만든다. 그래프의 필터, Window N, 주파수 보정은 넣지 않는다. 창은 Blackman이다.

- 세트 RMS: 축마다 시간 영역 AC RMS를 구한 뒤 `sqrt(X²+Y²+Z²)`. 단위는 g
- 세트 PEAK: 세 축 샘플 중 절대값이 가장 큰 값. 단위는 g
- 세트 SRSS: 세 축 스펙트럼 전력을 합친 뒤 제곱근. 단위는 g RMS
- 세트 SRSS mm/s: 같은 스펙트럼을 `g × 9806.65 / (2πf)`로 바꾼 뒤 같은 방식으로 합친 값

구간 한 줄에는 이렇게 모은다.

| 열 | 내용 |
| --- | --- |
| `mac` | 장치. 같은 MAC에 구간 줄이 여러 개 쌓인다 |
| `received_at` | 넣은 세트 중 가장 늦은 수신 시각. `last_at`과 같다 |
| `rms` | 완성 세트 RMS의 평균. 단위는 g |
| `peak` | 완성 세트 PEAK의 최댓값. 단위는 g |
| `srss` | 완성 세트 SRSS(g RMS)의 평균 |
| `srss_mms` | 완성 세트 SRSS(mm/s RMS)의 평균. 통계 막대가 읽는 값 |
| `set_count` | 이 구간에 넣은 완성 세트 수. 전체 평균의 가중치다 |
| `first_at` | 넣은 세트 중 가장 이른 수신 시각 |
| `last_at` | 넣은 세트 중 가장 늦은 수신 시각 |
| `computed_at` | 이 줄을 쓴 시각 |
| `g_per_lsb` | 이번 계산에 쓴 눈금 |
| `sample_rate`, `sample_count` | 넣은 세트가 모두 같으면 그 값. 섞이면 비운다. `sample_rate`는 Hz로 읽은 값이다 |
| `set_key` | `ADXL|{MAC}|{구간에 넣은 마지막 packet id}` |
| `through_id` | 이 최신화가 다 본 마지막 packet id. 계산에 실패한 세트까지 포함한다. `stat_cursor`와 같은 값이다 |
| `unit` | `g RMS` |

축이 하나라도 없는 세트, 샘플이 2개 미만인 세트, 세 축의 샘플 수나 샘플링 속도가 다른 세트는 평균에 넣지 않는다. 그 패킷 줄은 `packet`에 그대로 둔다. 응답의 `failed_sets`가 계산에 실패한 완성 세트 수다.

빠진 세트도 읽기 위치는 넘긴다. 다음 최신화에서 같은 패킷을 다시 읽지 않는다. 남기는 것은 맨 끝 묶음뿐이고, 그 묶음에 Z가 없을 때만이다. X, Y 뒤에 Z가 아직 올 수 있기 때문이다.

`packet.sample_rate`에는 수신 JSON의 `samplerate`가 그대로 있다. 숫자면 숫자, `0x0F`처럼 숫자가 아니면 글자다. 통계 계산은 그래프와 같은 `normalize_sample_rate`로 장비 코드(10~15, `0x0A`~`0x0F`)를 Hz로 읽는다.

새로 넣을 완성 세트가 하나도 없으면 그 MAC의 줄은 추가하지 않는다. 이전 구간 줄은 그대로 둔다. 실패하거나 빠진 세트만 있었으면 `stat_cursor`의 읽기 위치만 넘긴다. `stat_meta` 시각은 늘 갱신된다.

## 통계 화면이 읽는 것

통계 화면은 `measurement`만 읽는다. 페이지를 열거나 다시 그릴 때는 계산하지 않는다.

막대의 SRSS와 RMS 숫자는 구간 줄을 세트 수로 가중한 전체 평균이다. PEAK 숫자는 구간 PEAK의 최댓값이다. 작은 그래프는 구간이 쌓인 순서다. 한 구간만 있으면 점은 하나다.

분석 화면이 파형을 열 때는 `measurement`를 쓰지 않는다. `packet`에서 고른 세트의 샘플을 읽는다.

## 구현

통계 최신화는 이 명세대로 한다. 수신 중에는 `packet`만 늘리고 `measurement`는 건드리지 않는다. 하루 스케줄러는 아직 넣지 않은 패킷만 읽어 구간 한 줄을 추가한다.

"""대문(평면도) 대시보드의 센서 상태 판정. 계산만 하고 DB·MQTT는 직접 만지지 않는다.

입력은 store.site_rows()의 MAC별 원자료와 수신 연결 여부다. 결과는 /api/site/status가 그대로 보낸다.

판정은 두 가지를 따로 낸다.
1. 연결(link): 데이터가 원래 간격대로 들어오고 있는가.
   센서는 생존 신호를 따로 보내지 않으므로 "마지막 수신 후 경과 시간"을 "원래 간격"과 비교한다.
   원래 간격 = 실제 수신 간격의 중앙값(측정 3회 이상일 때) → 없으면 설정 SENSING_PERIOD.
2. 알람(alarm): 가장 최근 통계 한 줄(measurement)의 SRSS(mm/s)로 판정한다(2026-11-19).
   2026-11-23부터 ISO 20816-3 Group 2(강성 지지) 네 구간: 양호 good < 1.4 ≤ 주의 caution < 2.8 ≤ 경고 warn
   < 4.5 ≤ 위험 over (mm/s RMS). 경계는 MAC마다 설정 > 장치에서 바꾸며, 비어 있으면 위험 기준(예전 SRSS 한계)의
   ISO 비율(limits.zone_bounds). ADXL·PIEZO 해당(PIEZO는 2026-11-25부터, 초음파는 기준이 없다).
   예전(2026-11-23 전)에는 ≥ 한계 → 위험, ≥ 한계×80% → 경보 두 단계였다.
3. 배터리(battery): 센서가 보내는 batteryRmin(0~10). 3 이하면 low. 상태(state)와는 따로 표시한다.

연결 기준(2026-11-19 확정): 무선 센서는 보통 1시간에 한 번 보낸다.
정상 = 마지막 수신 후 경과 ≤ 원래 간격 + 30초, 지연 = ≤ 간격×2 + 30초(한 번 빠짐), 그 밖은 미수신.
바꿀 때는 아래 상수만 고친다(docs/대문-평면도-대시보드-계획.md "연결 판정").
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime

from web_db.src.limits import ZONE_TEXT, zone_bounds, zone_of

# 정상: 경과 ≤ 간격 × LINK_OK_FACTOR + LINK_GRACE_S. 지연: ≤ 간격 × LINK_LATE_FACTOR + LINK_GRACE_S. 그 밖은 미수신.
LINK_OK_FACTOR = 1.0
LINK_LATE_FACTOR = 2.0
LINK_GRACE_S = 30.0
# 축 정보가 없는 옛 자료(시각만)일 때만 쓰는 묶음 기준: 이 시간 안에 붙어 온 패킷은 같은 측정 한 번.
# 축 정보가 있으면 축 순서(X→Y→Z)로 묶는다. 실제 ADXL은 축 사이가 5초 넘게 벌어지기도 해서
# (2026-11-06 CSV: X→Y→Z 각 약 5.5초) 시간만으로 묶으면 축 간격을 측정 주기로 잘못 본다.
SAME_MEASURE_S = 2.0
AXIS_RANK = {"X": 0, "Y": 1, "Z": 2}
# 실제 수신 간격을 쓰려면 측정이 이만큼 있어야 한다(간격은 하나 적다).
MIN_MEASURES = 3
# 원래 간격의 하한(초, 2026-11-24). 옛 데이터를 몰아서 재송신하면(데이터 분석 CSV 송신 등) 최근 간격이
# 2~3초로 잡혀, 받은 지 30여 초 만에 '미수신'이 됐다. 설정 주기(SENSING_PERIOD)보다, 그리고 이 값보다
# 짧게 보지 않는다.
MIN_INTERVAL_S = 60.0
# 배터리(batteryRmin, 0~10). 이 값 이하면 부족(빨강).
BATTERY_LOW = 3.0
# 배터리 표시 고정값(2026-11-25). 지금 센서는 batteryRmin을 늘 0으로 보낸다(하드웨어 팀에 전달함, 펌웨어 반영 전).
# 0을 그대로 보이면 모든 센서가 '배터리 부족'으로 보여서, 반영될 때까지 화면 값만 이 숫자로 보인다.
# 저장(CSV·packet.battery·device.last_battery)은 받은 값(0) 그대로다. 센서가 실제 값을 보내기 시작하면
# **None으로 바꾸면** 받은 값을 그대로 보이고 3 이하 '부족' 판정도 다시 한다. API의 battery는 늘 받은 값.
BATTERY_DISPLAY_FIXED = 10.0

# 화면 문구와 색 이름. 화면(site.js)은 state 값으로 색을 고른다.
# 2026-11-23 피드백(docs/센서-현황-작업.md 3·4번): 끊김 → '미수신', 대기 → '확인 불가',
# 측정 이상 단계는 진동·초음파 공통으로 '경보 / 위험' 두 단계.
LINK_TEXT = {
    "ok": "정상 수신",
    "late": "수신 지연",
    "lost": "미수신",
    "stopped": "측정 중지",
    "none": "받은 데이터 없음",
    "unknown": "주기 미상",
    "offline": "수신 꺼짐",
}
# 측정 판정(진동, 2026-11-23): 양호 / 주의 / 경고 / 위험. 'na' = 판정 기준 없음(초음파, 표시만. PIEZO는 2026-11-25부터 판정).
ALARM_TEXT = dict(ZONE_TEXT, none="통계 없음", na="기준 없음")

# 수신 상태 묶음(위쪽 요약 '수신 정상 / 이상 / 확인 불가').
LINK_GROUP = {"ok": "ok", "late": "bad", "lost": "bad",
              "stopped": "unknown", "none": "unknown", "unknown": "unknown", "offline": "unknown"}

# 화면에 보이는 '하나의 상태'(아이콘 색). 측정과 수신 판정 중 더 나쁜 쪽 하나로 줄인다.
# 순서(나쁜 것부터): 위험 > 미수신 > 경고 > 지연 > 주의 > 양호 > 정상 > 확인 불가.
# 양호는 제때 수신 중이고 진동이 양호 구간인 MEMS·PIEZO, 정상은 제때 수신 중이지만 판정 기준이 없는 센서(초음파)
# 또는 아직 통계가 없는 MEMS다. 키 'warn'은 예전 '경보'(한계 80%) 자리를 이어 '경고'(ISO C 구간)로 쓴다.
STATE_TEXT = {"alarm": "위험", "lost": "미수신", "warn": "경고", "late": "지연", "caution": "주의",
              "good": "양호", "ok": "정상", "idle": "확인 불가"}
STATE_ORDER = ["alarm", "lost", "warn", "late", "caution", "good", "ok", "idle"]


def overall_state(link, alarm):
    """연결·알람 판정 → 하나의 상태.

    - 위험(alarm): 최근 통계 SRSS ≥ 위험 기준(4.5).
    - 미수신(lost): 원래 간격 ×2 + 30초보다 오래 수신 없음.
    - 경고(warn): SRSS ≥ 주의 상한(2.8).
    - 지연(late): 원래 간격 + 30초를 넘겼지만 ×2 + 30초 안.
    - 주의(caution): 제때 수신 중이고 SRSS ≥ 양호 상한(1.4).
    - 양호(good): 제때 수신 중이고 SRSS < 양호 상한.
    - 정상(ok): 제때 수신 중이지만 진동 판정이 없음(초음파, 통계 전 MEMS·PIEZO).
    - 확인 불가(idle): 판단할 수 없음(수신 꺼짐, 측정 중지, 받은 데이터 없음, 주기 미상).
    진동 값은 마지막 통계라 수신이 끊겨도 남는다. 그래서 위험·경고는 수신 상태보다 앞서 보이고,
    주의·양호는 제때 수신 중일 때만 보인다(끊긴 센서를 '양호'로 보이지 않게).
    """
    if alarm == "over":
        return "alarm"
    if link == "lost":
        return "lost"
    if alarm == "warn":
        return "warn"
    if link == "late":
        return "late"
    if link == "ok":
        if alarm == "caution":
            return "caution"
        if alarm == "good":
            return "good"
        return "ok"
    return "idle"


def _time(text):
    """저장된 ISO 시각 문자열 → datetime. 해석할 수 없으면 None."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def measure_times(times):
    """패킷들을 측정 한 번씩의 시작 시각으로 줄인다.

    times 항목은 (시각, 센서 종류, 축) 또는 시각 문자열(옛 형식).
    - ADXL: 축이 X→Y→Z로 이어지는 동안은 같은 측정. 축 순서가 앞으로 돌아가면(Z 다음 X, 또는
      X가 빠져 Y 다음 Y 등) 새 측정. 축 사이가 몇 초 벌어져도 한 측정으로 본다.
    - PIEZO·초음파: 패킷 하나가 측정 하나.
    - 시각만 있으면: SAME_MEASURE_S 안에 붙어 온 패킷을 한 측정으로.
    """
    packets = []
    for entry in times:
        when, kind, axis = (entry if isinstance(entry, (tuple, list)) else (entry, None, None))
        value = _time(when)
        if value is not None:
            packets.append((value, str(kind or "").upper(), str(axis or "").upper()))
    packets.sort(key=lambda p: p[0])
    result = []
    last_rank = None
    prev_ultra = None
    for value, kind, axis in packets:
        if kind == "ADXL" and axis in AXIS_RANK:
            rank = AXIS_RANK[axis]
            if last_rank is None or rank <= last_rank:
                result.append(value)
            last_rank = rank
        elif kind == "ULTRASONIC":
            # 초음파는 측정 한 번에 패킷 3~4개를 약 1초 간격으로 보낸다(2026-11-24 실제 수신 15:37:07~10).
            # 앞 패킷과 SAME_MEASURE_S 안이면 같은 측정으로 본다(패킷마다 측정으로 세면 간격이 1초가 된다).
            if prev_ultra is None or (value - prev_ultra).total_seconds() > SAME_MEASURE_S:
                result.append(value)
            prev_ultra = value
            last_rank = None
            continue
        elif kind:
            result.append(value)
            last_rank = None
        elif not result or (value - result[-1]).total_seconds() > SAME_MEASURE_S:
            result.append(value)
    return result


def _config(row):
    """sensor_config의 JSON. 없거나 깨졌으면 {}."""
    try:
        value = json.loads(row.get("config") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def expected_interval(row):
    """(원래 간격 초, 출처). 실제 수신 간격 중앙값과 설정 SENSING_PERIOD 중 긴 값, 하한 MIN_INTERVAL_S.

    2026-11-24: 예전에는 실제 간격이 있으면 그것만 썼다. 옛 데이터를 몰아 재송신하면 간격이 2~3초로 잡혀
    받자마자 '미수신'이 됐으므로, 설정 주기보다 짧게 보지 않는다(실제가 더 길면 실제 간격).
    둘 다 없으면 (None, '').
    """
    measured = None
    measures = measure_times(row.get("recent_times") or [])
    if len(measures) >= MIN_MEASURES:
        gaps = [(b - a).total_seconds() for a, b in zip(measures, measures[1:])]
        gaps = [g for g in gaps if g > 0]
        if gaps:
            measured = float(statistics.median(gaps))
    try:
        period = float(_config(row).get("SENSING_PERIOD"))
    except (TypeError, ValueError):
        period = 0.0
    if measured is None and period <= 0:
        return None, ""
    if measured is not None and measured >= period:
        interval, source = measured, "실제 수신 간격"
    else:
        interval, source = period, "설정 주기"
    return max(interval, MIN_INTERVAL_S), source


def link_state(row, receiver_on, now):
    """연결 판정 (state, 경과 초 또는 None, 간격 초 또는 None, 간격 출처)."""
    last = _time(row.get("last_at"))
    age = None if last is None else max(0.0, (now - last).total_seconds())
    interval, source = expected_interval(row)
    if not receiver_on:
        return "offline", age, interval, source
    if str(_config(row).get("SENSORTYPE", "")).strip() == "0":
        return "stopped", age, interval, source
    if age is None:
        return "none", None, interval, source
    if interval is None:
        return "unknown", age, None, ""
    if age <= interval * LINK_OK_FACTOR + LINK_GRACE_S:
        return "ok", age, interval, source
    if age <= interval * LINK_LATE_FACTOR + LINK_GRACE_S:
        return "late", age, interval, source
    return "lost", age, interval, source


def battery_state(row):
    """(배터리 값 또는 None, 'low' | 'ok' | 'none'). 값이 없거나 숫자가 아니면 'none'.

    BATTERY_DISPLAY_FIXED가 있으면 받은 값과 상관없이 그 값·'ok'(수신 기록이 없는 센서는 그대로 'none').
    """
    if BATTERY_DISPLAY_FIXED is not None and str(row.get("last_battery") or "").strip() != "":
        return float(BATTERY_DISPLAY_FIXED), "ok"
    try:
        value = float(str(row.get("last_battery") or "").strip())
    except ValueError:
        return None, "none"
    return value, ("low" if value <= BATTERY_LOW else "ok")


# 진동 구간 판정을 하는 통계 종류.
# 2026-11-25부터 PIEZO도 판정한다: 설정 > 장치의 MAC별 기준(기본 4.5 mm/s, 통계 화면 기준선과 같은 값)으로
# MEMS와 같은 ISO 네 구간. PIEZO 값은 한 축 Vrms(통계 stat_profile 환산). 환산 상수(Vmax/Amax/감도·게인)가
# 확정되면 그 MAC의 기준만 설정에서 조정한다. 판정을 다시 끄려면 "PIEZO"를 빼면 된다.
JUDGED_KINDS = ("ADXL", "PIEZO")


def alarm_state(row):
    """알람 판정 (구간, 최근 통계 SRSS 또는 None, 구간 경계 dict). MEMS(ADXL)·PIEZO가 아닌 센서는 'na'.

    구간: good 양호 / caution 주의 / warn 경고 / over 위험(limits.zone_of). 경계는 row['zones'](store.site_rows),
    없으면 row['limit']를 위험 기준으로 ISO 비율.
    """
    zones = row.get("zones") or zone_bounds(row.get("limit") or 4.5)
    value = row.get("srss_mms")
    if value is None:
        # 통계가 없으면: 판정 대상(MEMS·PIEZO)이면 아직 최신화 전('none'), 그 밖(초음파·종류 모름)은 알람 대상 아님.
        return ("none" if sensor_kind(row) in JUDGED_KINDS else "na"), None, zones
    value = float(value)
    if (row.get("stat_sensor") or "ADXL") not in JUDGED_KINDS:
        return "na", value, zones  # 값은 있지만 판정 안 함(위 JUDGED_KINDS 밖)
    return zone_of(value, zones), value, zones


def sensor_kind(row):
    """아이콘 종류: ADXL(MEMS) / PIEZO / ULTRASONIC / 빈 값.

    마지막으로 받은 패킷 종류가 우선이다. 패킷 없이 retain 설정만 받은 장치는 설정의
    SENSORTYPE으로 추정한다(1·3 PIEZO, 2·4 MEMS). 0(STOP)이나 설정이 없으면 빈 값.
    """
    sensor = str(row.get("last_sensor") or "").upper()
    if sensor.startswith("ADXL"):
        return "ADXL"
    if sensor.startswith("PIEZO"):
        return "PIEZO"
    if sensor.startswith("ULTRA"):
        return "ULTRASONIC"
    code = str(_config(row).get("SENSORTYPE", "")).strip()
    return {"1": "PIEZO", "3": "PIEZO", "2": "ADXL", "4": "ADXL"}.get(code, "")


def build_status(rows, receiver, now=None):
    """MAC별 상태 목록과 요약.

    rows: store.site_rows(). receiver: {"connected": bool, "host", "port"} (MonitorService에서).
    돌려주는 dict: now, receiver, sensors[...], counts{link 상태별, alarm 상태별, state(하나의 상태)별}.
    sensors는 나쁜 상태부터(STATE_ORDER), 같은 상태 안에서는 원래 순서.
    """
    now = now or datetime.now()
    receiver_on = bool(receiver.get("connected"))
    sensors = []
    counts = {"total": 0, "link": {}, "alarm": {}, "state": {}}
    for row in rows:
        link, age, interval, source = link_state(row, receiver_on, now)
        alarm, value, zones = alarm_state(row)
        battery, battery_flag = battery_state(row)
        config = _config(row)
        state = overall_state(link, alarm)
        sensors.append({
            "mac": row["mac"],
            "state": state,
            "state_text": STATE_TEXT[state],
            "name": row.get("display_name") or "",
            "kind": sensor_kind(row),
            "last_at": row.get("last_at") or "",
            "age_s": age,
            # battery = 센서가 보낸 값 그대로(화면은 쓰지 않는다). 화면은 battery_level(고정값이면 BATTERY_DISPLAY_FIXED)만 쓴다.
            # 고정 여부를 알리는 필드는 두지 않는다(2026-11-25, 시연 때 티가 나지 않게).
            "battery": row.get("last_battery") or "",
            "battery_level": battery,
            "battery_state": battery_flag,
            "link": link,
            "link_text": LINK_TEXT[link],
            "link_group": LINK_GROUP[link],
            "interval_s": interval,
            "interval_source": source,
            "config_period": config.get("SENSING_PERIOD"),
            "config_type": config.get("SENSORTYPE"),
            "alarm": alarm,
            "alarm_text": ALARM_TEXT[alarm],
            "srss_mms": value,
            "limit": zones["limit"],
            "zones": zones,
            "set_count": int(row.get("set_count") or 0),
            "stat_last_at": row.get("stat_last_at") or "",
            "stat_first_at": row.get("stat_first_at") or "",
        })
        counts["total"] += 1
        counts["link"][link] = counts["link"].get(link, 0) + 1
        counts["alarm"][alarm] = counts["alarm"].get(alarm, 0) + 1
        counts["state"][state] = counts["state"].get(state, 0) + 1
    sensors.sort(key=lambda item: STATE_ORDER.index(item["state"]))
    return {
        "now": now.isoformat(timespec="seconds"),
        "receiver": receiver,
        "sensors": sensors,
        "counts": counts,
        "rules": {
            "ok_factor": LINK_OK_FACTOR,
            "late_factor": LINK_LATE_FACTOR,
            "grace_s": LINK_GRACE_S,
            "zone_text": ZONE_TEXT,
            "battery_low": BATTERY_LOW,
        },
    }

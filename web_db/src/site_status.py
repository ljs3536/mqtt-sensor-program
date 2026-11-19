"""대문(평면도) 대시보드의 센서 상태 판정. 계산만 하고 DB·MQTT는 직접 만지지 않는다.

입력은 store.site_rows()의 MAC별 원자료와 수신 연결 여부다. 결과는 /api/site/status가 그대로 보낸다.

판정은 두 가지를 따로 낸다.
1. 연결(link): 데이터가 원래 간격대로 들어오고 있는가.
   센서는 생존 신호를 따로 보내지 않으므로 "마지막 수신 후 경과 시간"을 "원래 간격"과 비교한다.
   원래 간격 = 실제 수신 간격의 중앙값(측정 3회 이상일 때) → 없으면 설정 SENSING_PERIOD.
2. 알람(alarm): 통계 화면과 같은 값·같은 기준. 최신화는 하지 않고 이미 쌓인 measurement만 본다.
   SRSS 전체 평균(mm/s, 세트 수 가중) ≥ 한계 → over, ≥ 한계×80% → warn. ADXL만 해당.

아래 상수는 임시 기준이다(docs/대문-평면도-대시보드-계획.md "연결 판정"). 바꿀 때는 이 파일만 고친다.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime

# 정상: 경과 ≤ 간격 × LINK_OK_FACTOR + LINK_GRACE_S. 지연: ≤ 간격 × LINK_LATE_FACTOR + LINK_GRACE_S. 그 밖은 끊김.
LINK_OK_FACTOR = 2.0
LINK_LATE_FACTOR = 5.0
LINK_GRACE_S = 30.0
# 축 정보가 없는 옛 자료(시각만)일 때만 쓰는 묶음 기준: 이 시간 안에 붙어 온 패킷은 같은 측정 한 번.
# 축 정보가 있으면 축 순서(X→Y→Z)로 묶는다. 실제 ADXL은 축 사이가 5초 넘게 벌어지기도 해서
# (2026-11-06 CSV: X→Y→Z 각 약 5.5초) 시간만으로 묶으면 축 간격을 측정 주기로 잘못 본다.
SAME_MEASURE_S = 2.0
AXIS_RANK = {"X": 0, "Y": 1, "Z": 2}
# 실제 수신 간격을 쓰려면 측정이 이만큼 있어야 한다(간격은 하나 적다).
MIN_MEASURES = 3
# 통계 화면과 같은 주의 기준(한계의 80%).
WARN_RATIO = 0.8

# 화면 문구와 색 이름. 화면(site.js)은 state 값으로 색을 고른다.
LINK_TEXT = {
    "ok": "정상 수신",
    "late": "수신 지연",
    "lost": "수신 끊김",
    "stopped": "측정 중지",
    "none": "기록 없음",
    "unknown": "주기 미상",
    "offline": "수신 꺼짐",
}
ALARM_TEXT = {"ok": "정상", "warn": "주의", "over": "초과", "none": "통계 없음", "na": "알람 없음"}

# 화면에 보이는 '하나의 상태'. 연결(link)과 알람(alarm) 두 판정 중 더 나쁜 쪽 하나로 줄인다.
# 사용자는 아이콘 색 하나만 보면 된다. 순서(나쁜 것부터): 경보 > 끊김 > 주의 > 정상 > 대기.
STATE_TEXT = {"alarm": "경보", "lost": "끊김", "warn": "주의", "ok": "정상", "idle": "대기"}
STATE_ORDER = ["alarm", "lost", "warn", "ok", "idle"]


def overall_state(link, alarm):
    """연결·알람 판정 → 하나의 상태.

    - 경보(alarm): 쌓인 통계 SRSS ≥ 한계.
    - 끊김(lost): 원래 간격보다 훨씬 오래 수신 없음.
    - 주의(warn): SRSS ≥ 한계의 80%, 또는 수신 지연.
    - 정상(ok): 제때 수신 중이고 알람 없음.
    - 대기(idle): 판단할 수 없음(수신 꺼짐, 측정 중지, 기록 없음, 주기 미상).
    """
    if alarm == "over":
        return "alarm"
    if link == "lost":
        return "lost"
    if alarm == "warn" or link == "late":
        return "warn"
    if link == "ok":
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
    for value, kind, axis in packets:
        if kind == "ADXL" and axis in AXIS_RANK:
            rank = AXIS_RANK[axis]
            if last_rank is None or rank <= last_rank:
                result.append(value)
            last_rank = rank
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
    """(원래 간격 초, 출처). 실제 수신 간격 중앙값 → 설정 SENSING_PERIOD → (None, '')."""
    measures = measure_times(row.get("recent_times") or [])
    if len(measures) >= MIN_MEASURES:
        gaps = [(b - a).total_seconds() for a, b in zip(measures, measures[1:])]
        gaps = [g for g in gaps if g > 0]
        if gaps:
            return float(statistics.median(gaps)), "실제 수신 간격"
    try:
        period = float(_config(row).get("SENSING_PERIOD"))
    except (TypeError, ValueError):
        period = 0.0
    if period > 0:
        return period, "설정 주기"
    return None, ""


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


def alarm_state(row):
    """알람 판정 (state, SRSS 평균 또는 None, 한계). MEMS(ADXL)가 아닌 센서는 'na'."""
    limit = float(row.get("limit") or 4.5)
    value = row.get("srss_mms")
    if value is None:
        # 통계가 없으면: MEMS면 아직 최신화 전('none'), 그 밖(PIEZO·초음파·종류 모름)은 알람 대상 아님.
        return ("none" if sensor_kind(row) == "ADXL" else "na"), None, limit
    value = float(value)
    if value >= limit:
        return "over", value, limit
    if value >= limit * WARN_RATIO:
        return "warn", value, limit
    return "ok", value, limit


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
    sensors는 나쁜 상태부터(경보 → 끊김 → 주의 → 정상 → 대기), 같은 상태 안에서는 원래 순서.
    """
    now = now or datetime.now()
    receiver_on = bool(receiver.get("connected"))
    sensors = []
    counts = {"total": 0, "link": {}, "alarm": {}, "state": {}}
    for row in rows:
        link, age, interval, source = link_state(row, receiver_on, now)
        alarm, value, limit = alarm_state(row)
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
            "battery": row.get("last_battery") or "",
            "link": link,
            "link_text": LINK_TEXT[link],
            "interval_s": interval,
            "interval_source": source,
            "config_period": config.get("SENSING_PERIOD"),
            "config_type": config.get("SENSORTYPE"),
            "alarm": alarm,
            "alarm_text": ALARM_TEXT[alarm],
            "srss_mms": value,
            "limit": limit,
            "set_count": int(row.get("set_count") or 0),
            "stat_last_at": row.get("stat_last_at") or "",
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
            "warn_ratio": WARN_RATIO,
        },
    }

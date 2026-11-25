"""센서 종류별 화면 표시(2026-11-25). device.kind_view(JSON 글자)를 읽고 쓰는 규칙을 한곳에 둔다.

왜 있나: 같은 MAC이 PIEZO·MEMS(ADXL)·초음파를 섞어 보내면(SENSORTYPE을 바꿨거나 시험 데이터가 섞인 운영 CSV)
통계 화면에 같은 이름이 두 줄로 나오고, 센서 현황 판정이 가장 늦은 통계 종류에 따라 오락가락한다.
시연·운영 화면을 깔끔하게 하려고 MAC + 종류 단위로 화면에서 뺄 수 있게 한다.

값 모양: {"PIEZO": 1, "ADXL": -1}
  1  = 보임
  0  = 사용자가 숨김(설정 > 장치 종류 칸을 끔)
  -1 = 새 종류라 자동으로 숨김(그 MAC에 이미 다른 종류가 있는데 새 종류 패킷이 처음 들어옴)
  키 없음 = 그 종류를 받은 적 없음 → 보이는 것으로 본다(옛 동작과 같음).
  빈 글자('')·{} = 아직 아무 종류도 안 받음. 처음 들어온 종류는 1(보임)이 된다.

숨겨도 하는 일: 수신 저장(CSV·packet), 매시 통계 계산, 수신 판정(정상·지연·미수신). 화면에서만 뺀다
(센서 현황 판정·아이콘, 통계 화면 줄, 센서 데이터 분석 MAC 목록). 다시 켜면 지난 값이 바로 보인다.
모든 종류를 숨기면 device.visible=0('표시' 끔)과 같다.
"""

from __future__ import annotations

import json

# 화면에서 쓰는 이름과 패킷 sensor_type이 같다. 초음파 탭 글자('초음파')는 to_kind로 바꾼다.
KINDS = ("PIEZO", "ADXL", "ULTRASONIC")
KIND_TEXT = {"PIEZO": "PIEZO", "ADXL": "MEMS", "ULTRASONIC": "초음파"}
SHOWN, HIDDEN, AUTO_HIDDEN = 1, 0, -1


def to_kind(name):
    """'ADXL'·'MEMS'·'PIEZO'·'초음파'·'ULTRASONIC' → KINDS 중 하나. 모르면 ''."""
    text = str(name or "").strip().upper()
    if text.startswith("ADXL") or text == "MEMS":
        return "ADXL"
    if text.startswith("PIEZO"):
        return "PIEZO"
    if text.startswith("ULTRA") or text == "초음파":
        return "ULTRASONIC"
    return ""


def parse(text):
    """device.kind_view 글자 → {종류: 값}. 비었거나 깨졌으면 {}(아무 종류도 숨기지 않음)."""
    try:
        data = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        kind = to_kind(key)
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if kind and number in (SHOWN, HIDDEN, AUTO_HIDDEN):
            result[kind] = number
    return result


def is_shown(view, kind):
    """이 종류를 화면에 보이는가. 받은 적 없는 종류(키 없음)는 보임."""
    return view.get(to_kind(kind), SHOWN) == SHOWN


def shown_kinds(view):
    """보이는 종류 목록(받은 순서). 아무 종류도 안 받았으면 None(제한 없음)."""
    if not view:
        return None
    return [kind for kind, value in view.items() if value == SHOWN]


def all_hidden(view):
    """받은 종류가 있고 그 모두를 숨겼는가(= '표시' 끔과 같음)."""
    return bool(view) and not shown_kinds(view)


# SQL 조각: 테이블 별칭 alias의 kind_view에서 종류 열(kind_col)이 보이는가(1/0).
# json_valid로 깨진 값·빈 글자를 먼저 거른다(CASE는 앞에서부터 계산하므로 json_extract가 오류를 내지 않는다).
def sql_shown(alias, kind_col):
    return (f"(CASE WHEN json_valid({alias}.kind_view) "
            f"THEN COALESCE(json_extract({alias}.kind_view, '$.' || {kind_col}), 1) ELSE 1 END) = 1")


# 패킷이 들어올 때 부른다(store.save_packet·import_packets). 그 MAC이 이 종류를 처음 받으면 기록한다:
# 아무 종류도 없었으면 1(보임), 다른 종류가 이미 있으면 -1(새 종류 자동 숨김). 이미 있는 종류면 쓰지 않는다
# (WHERE가 거짓이라 쓰기 없음 → 패킷마다 불러도 가볍다). 인자: (종류, mac, 종류)
NOTE_KIND_SQL = """
    UPDATE device SET kind_view = json_set(
        CASE WHEN json_valid(kind_view) THEN kind_view ELSE '{}' END,
        '$.' || ?,
        CASE WHEN NOT json_valid(kind_view) OR kind_view = '{}' THEN 1 ELSE -1 END
    )
    WHERE mac = ? AND (CASE WHEN json_valid(kind_view) THEN json_type(kind_view, '$.' || ?) IS NULL ELSE 1 END)
"""


def note_kind(conn, mac, sensor_type):
    """NOTE_KIND_SQL 실행. lock은 부르는 쪽이 잡는다. 모르는 종류는 무시."""
    kind = to_kind(sensor_type)
    if kind and mac:
        conn.execute(NOTE_KIND_SQL, (kind, mac, kind))


def backfill_sql():
    """옛 DB에 kind_view 칸을 처음 만들 때 채울 (mac, 종류) 목록 쿼리. MAC마다 처음 받은(가장 작은 id) 종류 순."""
    return ("SELECT mac, sensor_type, MIN(id) AS first_id FROM packet "
            "WHERE mac IS NOT NULL AND mac<>'' GROUP BY mac, sensor_type ORDER BY mac, first_id")


def backfill(rows):
    """backfill_sql 결과 → {mac: kind_view 글자}. 처음 받은 종류만 보임, 나머지는 자동 숨김(-1).

    2026-11-25 적용 때: 865872312270(MOP2)은 PIEZO를 먼저 받고 시험용 ADXL CSV가 뒤에 들어와 ADXL이 숨김이 된다.
    """
    result = {}
    for row in rows:
        kind = to_kind(row[1])
        if not kind:
            continue
        view = result.setdefault(row[0], {})
        if kind not in view:
            view[kind] = SHOWN if not view else AUTO_HIDDEN
    return {mac: json.dumps(view, ensure_ascii=False) for mac, view in result.items()}

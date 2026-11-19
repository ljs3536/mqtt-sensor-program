"""ADXL X/Y/Z를 한 측정 번호로 묶는다.

CSV를 열 때와 같다. seq 상위 4비트는 축이고, 하위 4비트는 축을 보낼 때마다 1씩 증가한다.
DB는 packet_no를 저장하지 않고 seq만으로 묶는다(set_no). 웹 CSV의 ADXL packet_no는
save_csv.py가 이 continues_set으로 매긴 세트 번호라서 DB의 묶음과 같은 규칙이다.
"""

from __future__ import annotations


def seq_low(seq):
    """seq 문자열의 하위 4비트. 해석할 수 없으면 None."""
    text = str(seq or "").strip()
    try:
        return int(text.removeprefix("0x").removeprefix("0X"), 16) & 15
    except ValueError:
        return None


def continues_set(last_axis, last_low, axis, low):
    """직전 축 다음 순서로 seq 하위 값이 이어지면 같은 측정이다.

    축 순서는 X → Y → Z만 허용한다. 뒤 축의 seq 하위 4비트는 앞 축보다
    (축 차이)만큼 커야 한다(0xF 다음은 0).
    - X "1A" 뒤 Y "2B" → 이어짐. X "1A" 뒤 Z "3C" → Y가 빠졌어도 같은 세트(차이 2).
    - Z 뒤에는 무엇이 와도 새 세트다. 같은 축이 두 번 와도 새 세트다.
    - seq를 해석할 수 없으면 이어지지 않는 것으로 본다.
    """
    if last_axis not in ("X", "Y", "Z") or axis not in ("X", "Y", "Z"):
        return False
    if last_low is None or low is None:
        return False
    prev = ("X", "Y", "Z").index(last_axis)
    idx = ("X", "Y", "Z").index(axis)
    if idx <= prev:
        return False
    return ((last_low + (idx - prev)) & 15) == low


def assign_set(conn, mac, seq, sensor_type, axis):
    """이번 패킷의 set_key와 set_no를 정한다. 이어지는 ADXL 축은 번호를 공유한다.

    Store.save_packet이 self.lock을 잡은 채 부른다(conn은 그 연결).
    - ADXL: 그 MAC의 직전 ADXL 패킷과 continues_set으로 비교한다. 이어지면 같은 set_no,
      아니면 +1. 축이 빠져도 번호만 넘어가고 패킷은 저장된다.
    - PIEZO·초음파: 패킷 하나가 측정 하나라 매번 +1.
    set_key는 `{종류}|{MAC}|{set_no}`이고, 분석 화면이 세트 단위로 고를 때 쓴다.
    """
    if sensor_type != "ADXL":
        row = conn.execute(
            "SELECT COALESCE(MAX(set_no), 0) AS n FROM packet WHERE mac=? AND sensor_type=?",
            (mac, sensor_type),
        ).fetchone()
        number = int(row["n"]) + 1
        return f"{sensor_type}|{mac}|{number}", number
    latest = conn.execute(
        """
        SELECT set_no, axis, seq FROM packet
        WHERE mac=? AND sensor_type='ADXL'
        ORDER BY id DESC LIMIT 1
        """,
        (mac,),
    ).fetchone()
    low = seq_low(seq)
    if latest is not None and continues_set(latest["axis"], seq_low(latest["seq"]), axis, low):
        number = int(latest["set_no"] or 0)
        return f"ADXL|{mac}|{number}", number
    number = int(latest["set_no"] or 0) + 1 if latest is not None else 1
    return f"ADXL|{mac}|{number}", number


def packet_to_template(row):
    """SQLite 패킷 한 줄을 발생기 미리보기가 읽는 패킷 dict로 바꾼다."""
    obj = {
        "gwMacAddr": row.get("gw_mac") or "HALOW",
        "dvicMacAddr": row.get("mac") or "",
        "batteryRmin": row.get("battery") or "0",
        "seq": row.get("seq") or "",
        "samplerate": "" if row.get("sample_rate") is None else str(row["sample_rate"]),
        "numofsample": "" if row.get("sample_count") is None else str(int(row["sample_count"])),
        "sensorData": str(row.get("sensor_data_hex") or ""),
    }
    # 초음파는 실제 센서·CSV 송신(load_file.load_ultrasonic_csv)처럼 samplerate·numofsample을 넣지 않는다.
    # 빈 값으로라도 두 키가 있으면 수신 쪽이 진동(seq 상위 0 → PIEZO)으로 읽어 디코드 실패로 버렸다(2026-11-19).
    if str(row.get("sensor_type") or "").upper() == "ULTRASONIC":
        obj.pop("samplerate")
        obj.pop("numofsample")
    if row.get("tick"):
        obj["tick"] = row["tick"]
    # '_' 메타: 송신 때는 replay가 빼고, 속도 RMS 시간 이력이 시각·번호로 쓴다.
    # CSV를 열 때(load_file)와 같은 이름이다. DB의 packet_no 자리는 세트 번호(set_no).
    if row.get("received_at"):
        obj["_received_at"] = str(row["received_at"])
    if row.get("set_no") is not None:
        obj["_packet_no"] = str(row["set_no"])
    return obj


def group_adxl(rows):
    """같은 set_key의 X/Y/Z가 모두 있는 세트만 미리보기 목록으로 돌려준다."""
    grouped = {}
    order = []
    for row in rows:
        key = row["set_key"]
        if key not in grouped:
            grouped[key] = {}
            order.append(key)
        grouped[key][row["axis"]] = packet_to_template(row)
    items = []
    for key in order:
        axes = grouped[key]
        if all(axis in axes for axis in ("X", "Y", "Z")):
            items.append({"X": axes["X"], "Y": axes["Y"], "Z": axes["Z"]})
    return items


def _axis_groups(rows):
    """id 순 패킷을 seq가 이어지는 묶음으로 나눈다. 축이 빠진 묶음도 그대로 둔다."""
    sets = []
    current = {}
    last_axis = None
    last_low = None
    for row in rows:
        axis = row["axis"]
        low = seq_low(row["seq"])
        if current and not continues_set(last_axis, last_low, axis, low):
            sets.append(current)
            current = {}
        if axis in ("X", "Y", "Z"):
            current[axis] = row
            last_axis, last_low = axis, low
    if current:
        sets.append(current)
    return sets


def complete_sets(rows):
    """id 순 패킷을 완성된 X/Y/Z 세트 목록으로 나눈다. 각 세트는 축별 행이다.

    통계 최신화가 쓴다. 저장된 set_no를 믿지 않고 seq로 다시 묶는다. 그래서 서버가
    재시작되어 set_no가 다시 매겨져도 통계 결과는 같다. 세 축이 다 있는 묶음만 돌려준다.
    """
    return [item for item in _axis_groups(rows) if all(axis in item for axis in ("X", "Y", "Z"))]


def settled_through(rows, base):
    """이번에 다 본 것으로 칠 마지막 packet id.

    완성 세트는 계산 성공 여부와 관계없이 끝난 것이다. 축이 빠진 묶음도 뒤에 다른 묶음이
    왔으면 더 채워지지 않는다. 마지막 묶음만, Z가 없으면 뒤 축이 아직 올 수 있어 남긴다.
    """
    if not rows:
        return base
    last_id = max(int(row["id"]) for row in rows)
    groups = _axis_groups(rows)
    if not groups or "Z" in groups[-1]:
        return last_id
    first_open = min(int(row["id"]) for row in groups[-1].values())
    earlier = [int(row["id"]) for row in rows if int(row["id"]) < first_open]
    return max(earlier) if earlier else base

"""ADXL X/Y/Z를 한 측정 번호로 묶는다.

CSV를 열 때와 같다. seq 상위 4비트는 축이고, 하위 4비트는 축을 보낼 때마다 1씩 증가한다.
packet_no는 축 파일마다 따로 세므로 묶음 키가 아니다.
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
    """직전 축 다음 순서로 seq 하위 값이 이어지면 같은 측정이다."""
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
    """이번 패킷의 set_key와 set_no를 정한다. 이어지는 ADXL 축은 번호를 공유한다."""
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
    if row.get("tick"):
        obj["tick"] = row["tick"]
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

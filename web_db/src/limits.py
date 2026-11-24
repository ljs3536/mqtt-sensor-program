"""장치별 SRSS 한계. sensor.sqlite의 srss_limit 테이블을 쓴다."""

from __future__ import annotations


def srss_limit_key(device):
    """한계 테이블 키. 대문자이며 콜론과 하이픈은 뺀다."""
    return str(device or "").strip().upper().replace(":", "").replace("-", "")


def _store():
    """순환 import를 피하려고 호출 시점에 DB를 가져온다."""
    from web_db.src.runtime import store

    return store


def srss_limit_get(device):
    """저장된 값이 없으면 4.5 mm/s RMS. 경보 판정이 아니라 그래프 기준선이다."""
    return _store().get_srss_limit(device)


def srss_limit_save(device, value):
    """장치 SRSS 한계를 srss_limit 테이블에 저장한다."""
    return _store().save_srss_limit(device, value)


# ---------- 진동 평가 구간(2026-11-23, ISO 20816-3 Group 2 강성 지지 기준) ----------
# 화면 이름: A 양호 / B 허용 → 주의 / C 개선 → 경고 / D 위험. 단위 mm/s RMS(통계 SRSS).
#   양호 < good ≤ 주의 < caution ≤ 경고 < limit ≤ 위험
# MAC마다 srss_limit 표에 세 값을 둔다. limit(위험 기준)은 예전 'SRSS 한계' 그대로이고,
# good·caution이 비어 있으면 ISO 비율로 계산한다: 한계 4.5면 정확히 1.4 / 2.8 / 4.5, 15면 4.67 / 9.33 / 15.
ZONE_LIMIT_DEFAULT = 4.5
ZONE_GOOD_RATIO = 1.4 / 4.5
ZONE_CAUTION_RATIO = 2.8 / 4.5
ZONE_TEXT = {"good": "양호", "caution": "주의", "warn": "경고", "over": "위험"}


def zone_bounds(limit, good=None, caution=None):
    """구간 경계 dict {good, caution, limit, custom}. good·caution이 없거나 순서가 맞지 않으면 비율로 채운다.

    custom: good·caution을 사람이 저장했으면 True(설정 화면이 '자동'과 구분해 보인다).
    """
    try:
        limit = float(limit)
    except (TypeError, ValueError):
        limit = ZONE_LIMIT_DEFAULT
    if not limit > 0:
        limit = ZONE_LIMIT_DEFAULT
    try:
        good, caution = float(good), float(caution)
        custom = 0 < good < caution < limit
    except (TypeError, ValueError):
        custom = False
    if not custom:
        good, caution = limit * ZONE_GOOD_RATIO, limit * ZONE_CAUTION_RATIO
    return {"good": good, "caution": caution, "limit": limit, "custom": custom}


def zone_of(value, zones):
    """SRSS(mm/s) → 'good' / 'caution' / 'warn' / 'over'. 경계값은 위 구간에 넣는다(1.4는 주의, 4.5는 위험)."""
    value = float(value)
    if value >= zones["limit"]:
        return "over"
    if value >= zones["caution"]:
        return "warn"
    if value >= zones["good"]:
        return "caution"
    return "good"

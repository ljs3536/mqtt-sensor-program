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

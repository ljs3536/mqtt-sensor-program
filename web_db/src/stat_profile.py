"""통계 계산 기준(버전 관리)과 세트 하나의 통계 계산 (2026-11-23).

왜 따로 두나
- 그래프·분석 ZIP은 화면 서랍에 지금 켜 둔 값으로 계산한다. 같은 데이터라도 체크에 따라 값이 바뀐다.
- 통계(measurement)와 센서 현황 경보는 모두가 같은 기준으로 봐야 하므로, 기준을 DB(stat_profile)에
  버전으로 저장하고 통계 한 줄마다 계산에 쓴 버전(profile_id)을 남긴다.
- 실측(2026-11-23, 같은 6세트 SRSS mm/s): 필터 없음 3.6~11.9로 흔들림, HPF 10Hz + LPF 1000Hz 2.3~2.5로 안정.
  10Hz 아래 성분(드리프트 등)이 값을 좌우했다. 그래서 기본은 HPF 10Hz + LPF 1000Hz(진동 속도 평가 대역 10~1000Hz).

기준(settings) 모양 — DB에는 이 dict가 JSON으로 들어간다
  g_per_lsb   1 LSB당 g. 처음 버전은 mems_lsb_scale.json 값.
  correction  MEMS 주파수·감도 보정(mems_frequency_sensitivity.json과 같은 키).
  filters     hpf/lpf {on, hz, order}, notch {on, hz, width}. FFT측 필터와 같은 계산(signal.spectral_filter).
  window      Welch 창 이름, beta(Kaiser용)
  psd         segment('전체' 또는 8192…128), overlap(%), target(평균 목표 구간 수)

세트 하나 계산(set_stats) — 분석 ZIP(analysis_export)과 같은 graph_psd.axis_psd를 쓴다
  축마다: RAW − 평균 → 필터 → Welch PSD × (g/LSB)² → MEMS 보정 → 가속도 PSD Pa[g²/Hz]
          → 속도 PSD Pv = Pa × (9806.65/2πf)² → vRMS = √(ΣPv·Δf)
  srss_mms = √(vX² + vY² + vZ²), srss(g) = √(ΣaRMS²) (aRMS = √(ΣPa·Δf))
  rms(g)  = √(Σ 축별 시간영역 RMS²) — 필터 출력 기준, peak(g) = 필터 출력 |g|의 최댓값
  세트의 Fs에서 쓸 수 없는 필터(차단 ≥ Fs/2)는 그 세트에서만 끄고 notes에 남긴다.

한 시간 한 줄(hour_bucket, merge_rows)
  세트 시각 = X/Y/Z 중 마지막 수신 시각. 그 시각의 '시'(YYYY-MM-DDTHH:00:00)가 묶음 키다.
  평균은 세트 수로 가중해서 합친다. 그래서 늦게 온 세트를 나중에 더해도 한 번에 계산한 것과 같다.
  나중에 세트별 저장이 필요하면 set_stats 결과를 그대로 넣으면 된다(시간 묶음은 그 위의 단계일 뿐이다).
"""

from __future__ import annotations

import copy
import json
import math

import numpy as np

from web_db.src import core as gen
from web_db.src.graph_options import _filter_kwargs, decode_wave, resolve_options
from web_db.src.graph_psd import G0, PSD_SEGMENTS, axis_psd, psd_settings

# 기본 필터: HPF 10Hz + LPF 1000Hz (2026-11-23 결정). Notch는 끔.
DEFAULT_FILTERS = {
    "hpf": {"on": True, "hz": 10.0, "order": 4},
    "lpf": {"on": True, "hz": 1000.0, "order": 4},
    "notch": {"on": False, "hz": 975.0, "width": 20.0},
}
DEFAULT_PSD = {"segment": "전체", "overlap": 50.0, "target": 30}
# 통계 한 줄 평균에 들어가는 값(세트 수로 가중 평균). peak·srss_mms_max는 최댓값으로 합친다.
MEAN_KEYS = ("rms", "srss", "srss_mms", "x_mms", "y_mms", "z_mms")


def default_settings():
    """처음 버전. g/LSB·MEMS 보정은 지금 그래프가 쓰는 JSON 파일 값, 필터는 HPF 10 + LPF 1000."""
    return validate({
        "g_per_lsb": gen.read_mems_g_per_lsb(),
        "correction": gen.load_mems_correction(),
        "filters": copy.deepcopy(DEFAULT_FILTERS),
        "window": {"name": "Blackman", "beta": 8.6},
        "psd": dict(DEFAULT_PSD),
    })


def _number(value, name, low=None, high=None, low_open=False):
    """숫자 하나 검사. 화면에 그대로 보일 한글 오류를 낸다."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: 숫자가 필요합니다") from None
    if not math.isfinite(number):
        raise ValueError(f"{name}: 숫자가 필요합니다")
    if low is not None and (number <= low if low_open else number < low):
        raise ValueError(f"{name}: {low}{'보다 커야' if low_open else ' 이상이어야'} 합니다")
    if high is not None and number > high:
        raise ValueError(f"{name}: {high} 이하여야 합니다")
    return number


def validate(raw):
    """화면·JSON에서 온 기준을 검사하고 빠진 칸은 기본값으로 채운다. 잘못되면 ValueError."""
    raw = raw if isinstance(raw, dict) else {}
    filters_in = raw.get("filters") if isinstance(raw.get("filters"), dict) else {}
    filters = {}
    for key, base in DEFAULT_FILTERS.items():
        item = dict(base)
        item.update(filters_in.get(key) or {})
        label = {"hpf": "HPF", "lpf": "LPF", "notch": "Notch"}[key]
        clean = {"on": bool(item.get("on")), "hz": _number(item.get("hz"), f"{label} 주파수", 0, None, True)}
        if key == "notch":
            clean["width"] = _number(item.get("width"), "Notch 폭", 0, None, True)
        else:
            order = _number(item.get("order"), f"{label} 차수", 1, 12)
            clean["order"] = int(order)
        filters[key] = clean
    if filters["hpf"]["on"] and filters["lpf"]["on"] and filters["hpf"]["hz"] >= filters["lpf"]["hz"]:
        raise ValueError("HPF 주파수는 LPF 주파수보다 작아야 합니다")
    window = raw.get("window") if isinstance(raw.get("window"), dict) else {}
    name = str(window.get("name") or "Blackman").strip()
    if name not in gen.FFT_WINDOWS:
        raise ValueError(f"Window는 {', '.join(gen.FFT_WINDOWS)} 중 하나여야 합니다")
    psd = dict(DEFAULT_PSD)
    psd.update(raw.get("psd") if isinstance(raw.get("psd"), dict) else {})
    psd["segment"] = str(psd.get("segment") or "전체").strip()
    if psd["segment"] not in PSD_SEGMENTS:
        raise ValueError(f"PSD 구간 N은 {', '.join(PSD_SEGMENTS)} 중 하나여야 합니다")
    psd["overlap"] = _number(psd.get("overlap"), "Overlap", 0, 95)
    psd["target"] = int(_number(psd.get("target"), "평균 목표 구간", 1, 100000))
    return {
        "g_per_lsb": _number(raw.get("g_per_lsb", gen.read_mems_g_per_lsb()), "g/LSB", 0, 10, True),
        "correction": gen.validate_mems_correction(raw.get("correction") or gen.load_mems_correction()),
        "filters": filters,
        "window": {"name": name, "beta": _number(window.get("beta", 8.6), "Kaiser β", 0, 100)},
        "psd": psd,
    }


def summary_text(settings):
    """'HPF 10Hz · LPF 1000Hz · 보정 O · Blackman · N=전체'처럼 짧은 설명(화면·문서용)."""
    f = settings["filters"]
    parts = []
    parts.append(f"HPF {f['hpf']['hz']:g}Hz" if f["hpf"]["on"] else "HPF 끔")
    parts.append(f"LPF {f['lpf']['hz']:g}Hz" if f["lpf"]["on"] else "LPF 끔")
    if f["notch"]["on"]:
        parts.append(f"Notch {f['notch']['hz']:g}±{f['notch']['width'] / 2:g}Hz")
    c = settings["correction"]
    fix = [name for name, on in (("주파수", c["frequency_enabled"]), ("감도", c["sensitivity_enabled"])) if on]
    parts.append(("보정 " + "·".join(fix)) if fix else "보정 끔")
    parts.append(f"g/LSB {settings['g_per_lsb']:.6g}")
    parts.append(f"{settings['window']['name']} · N={settings['psd']['segment']}")
    return " · ".join(parts)


def to_json(settings):
    """DB 저장용 글자(키 순서 고정이라 같은 기준이면 같은 글자)."""
    return json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _options(settings, fs):
    """세트 Fs에 맞춘 graph_psd용 옵션과, 이 Fs에서 끈 필터 설명 목록."""
    options = resolve_options({})
    adxl = options["adxl"]
    f = settings["filters"]
    nyquist = fs / 2.0
    notes = []

    def usable(key, name, cutoff, extra=0.0):
        if not f[key]["on"]:
            return False
        if 0 < cutoff - extra and cutoff + extra < nyquist:
            return True
        notes.append(f"{name} {cutoff:g}Hz ≥ Fs/2 {nyquist:g}Hz → 이 세트는 끔")
        return False

    hpf_on = usable("hpf", "HPF", f["hpf"]["hz"])
    lpf_on = usable("lpf", "LPF", f["lpf"]["hz"])
    notch_on = usable("notch", "Notch", f["notch"]["hz"], f["notch"]["width"] / 2.0)
    adxl["filters"] = {
        "HPF": {"raw": False, "fft": hpf_on, "freq": f["hpf"]["hz"], "extra": f["hpf"]["order"]},
        "LPF": {"raw": False, "fft": lpf_on, "freq": f["lpf"]["hz"], "extra": f["lpf"]["order"], "auto": not lpf_on},
        "Notch": {"raw": False, "fft": notch_on, "freq": f["notch"]["hz"], "extra": f["notch"]["width"]},
    }
    adxl["g_per_lsb"] = settings["g_per_lsb"]
    adxl["correction"] = dict(settings["correction"])
    adxl["window"] = settings["window"]["name"]
    adxl["beta"] = settings["window"]["beta"]
    options["psd"] = dict(settings["psd"], on=True)
    return options, notes


def set_stats(objs, settings):
    """X/Y/Z 축 obj(MQTT JSON 모양, sensorData HEX 포함) 세 개로 세트 하나의 통계.

    돌려주는 dict: sample_rate, sample_count, rms, peak, srss(g), srss_mms, x_mms, y_mms, z_mms, notes
    세 축의 Fs·샘플 수가 다르거나 한 축이라도 계산이 안 되면 ValueError(그 세트는 버린다).
    """
    rates, counts, a_time, a_psd, v_rms, notes = [], [], [], [], {}, []
    peak = 0.0
    g_per_lsb = settings["g_per_lsb"]
    for axis in ("X", "Y", "Z"):
        obj = objs[axis]
        raw, fs = decode_wave("ADXL", obj)
        if raw.size < 2 or not math.isfinite(fs) or fs <= 0:
            raise ValueError(f"{axis}: 샘플/Fs 오류")
        options, skipped = _options(settings, fs)
        for text in skipped:
            if text not in notes:
                notes.append(text)
        rates.append(float(fs))
        counts.append(int(raw.size))
        # 시간영역: 필터 출력(평균 제거) × g/LSB
        filtered = gen.spectral_filter(raw - np.mean(raw), fs, **_filter_kwargs(options["adxl"]["filters"], "FFT"))
        grams = filtered * g_per_lsb
        a_time.append(float(np.sqrt(np.mean(np.square(grams)))))
        peak = max(peak, float(np.max(np.abs(grams))))
        # 주파수영역: 분석 ZIP과 같은 axis_psd(가속도 g²/Hz) → 속도
        settings_psd = psd_settings(options)
        freq, acc, used, _fs, _n = axis_psd("ADXL", obj, options, settings_psd, "g", max_segments=settings_psd["target"])
        if used < 1 or len(freq) < 2:
            raise ValueError(f"{axis}: PSD 구간 부족")
        df = float(freq[1] - freq[0])
        vel = np.zeros_like(acc)
        positive = freq > 0
        vel[positive] = acc[positive] * (G0 / (2 * np.pi * freq[positive])) ** 2
        a_psd.append(math.sqrt(max(0.0, float(np.sum(acc)) * df)))
        v_rms[axis] = math.sqrt(max(0.0, float(np.sum(vel)) * df))
    if len(set(rates)) != 1 or len(set(counts)) != 1:
        raise ValueError("X/Y/Z 샘플 수 또는 샘플링 속도가 다릅니다")
    return {
        "sample_rate": rates[0],
        "sample_count": counts[0],
        "rms": math.sqrt(sum(v * v for v in a_time)),
        "peak": peak,
        "srss": math.sqrt(sum(v * v for v in a_psd)),
        "srss_mms": math.sqrt(sum(v * v for v in v_rms.values())),
        "x_mms": v_rms["X"],
        "y_mms": v_rms["Y"],
        "z_mms": v_rms["Z"],
        "notes": notes,
    }


def hour_bucket(stamp):
    """세트 시각 '2026-11-23T15:37:23.979' → 묶음 키 '2026-11-23T15:00:00'."""
    text = str(stamp or "").replace(" ", "T")
    if len(text) < 13:
        raise ValueError(f"시각 형식 오류: {stamp!r}")
    return text[:13] + ":00:00"


def row_from_sets(items):
    """같은 시간 묶음의 세트 통계 [(세트 통계, 세트 시각, 세트 마지막 packet id)] → measurement 한 줄 값."""
    stats = [item[0] for item in items]
    times = sorted(item[1] for item in items)
    rates = {s["sample_rate"] for s in stats}
    counts = {s["sample_count"] for s in stats}
    notes = []
    for s in stats:
        for text in s.get("notes") or []:
            if text not in notes:
                notes.append(text)
    row = {key: float(np.mean([s[key] for s in stats])) for key in MEAN_KEYS}
    row.update({
        "sample_rate": next(iter(rates)) if len(rates) == 1 else None,
        "sample_count": next(iter(counts)) if len(counts) == 1 else None,
        "peak": float(max(s["peak"] for s in stats)),
        "srss_mms_max": float(max(s["srss_mms"] for s in stats)),
        "set_count": len(stats),
        "first_at": times[0],
        "last_at": times[-1],
        "first_id": min(int(item[2]) for item in items),
        "notes": notes,
    })
    return row


def merge_rows(old, new):
    """이미 저장된 시간 줄(old)에 새 세트들의 줄(new)을 더한다. 평균은 세트 수 가중, 최대는 최대."""
    total = int(old["set_count"]) + int(new["set_count"])
    merged = {}
    for key in MEAN_KEYS:
        a, b = old.get(key), new.get(key)
        if a is None:
            merged[key] = b
        elif b is None:
            merged[key] = a
        else:
            merged[key] = (float(a) * int(old["set_count"]) + float(b) * int(new["set_count"])) / total
    merged.update({
        "sample_rate": old["sample_rate"] if old.get("sample_rate") == new.get("sample_rate") else None,
        "sample_count": old["sample_count"] if old.get("sample_count") == new.get("sample_count") else None,
        "peak": max(float(old.get("peak") or 0.0), float(new.get("peak") or 0.0)),
        "srss_mms_max": max(float(old.get("srss_mms_max") or 0.0), float(new.get("srss_mms_max") or 0.0)),
        "set_count": total,
        "first_at": min(old["first_at"], new["first_at"]),
        "last_at": max(old["last_at"], new["last_at"]),
        "first_id": min(int(old.get("first_id") or new["first_id"]), int(new["first_id"])),
        "notes": list(dict.fromkeys(list(old.get("notes") or []) + list(new.get("notes") or []))),
    })
    return merged

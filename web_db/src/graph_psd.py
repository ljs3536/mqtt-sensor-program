"""PSD(Welch) 그래프와 ADXL 속도 RMS 시간 이력. 발생기 v070의 계산을 옮겼다.

쓰는 곳
- generator_curves(graph_curves.py): 옵션 psd.on이 켜져 있으면 FFT 칸을 PSD로 바꾼다.
  RAW 그래프와 MQTT 송신 원본은 그대로다.
- velocity_history(routes/analysis.py): 불러온 ADXL X/Y/Z 세트마다 PSD로 속도 RMS를 구해
  수신 시각 순서의 시간 이력을 만든다.

FFT(Window 보정)와 다른 점
- FFT 진폭은 창 합(Σw)이나 N으로 나눠 '한 칸의 진폭'을 본다.
- PSD는 창 에너지(Σw²)와 Fs로 나눈 '밀도'(단위²/Hz)다. 넓이 √(ΣP×Δf)가 그 대역의 RMS라서
  창 종류나 Window 보정 모드와 무관하게 같은 RMS가 나온다.

계산 순서(축마다, v070 _plot_packet_psd와 같음)
  ① 디코드한 Count에서 평균을 빼고 FFT용 필터(HPF/LPF/Notch)를 건다.
  ② welch_packet_psd → Count²/Hz.
  ③ 단위 환산은 '진폭'의 배율을 제곱해서 곱한다.
     PIEZO: K = 1000×Vmax/Amax [mV/Count], g면 K/감도. ADXL: g/LSB.
  ④ ADXL 보정: √P를 진폭처럼 감도 S(f)로 나누고, 주파수축은 f/나눗수로 옮긴다.
     주파수축을 줄이면 같은 넓이를 지키려고 밀도에 나눗수를 곱한다(야코비안).
  ⑤ mm/s: Pv(f) = Pa(f) × (9806.65 / (2πf))², f>0. 0 Hz는 0.
  ⑥ RMS = √(Σ P × Δf).
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from web_db.src import core as gen
from web_db.src.graph_options import (
    ADXL_FFT_UNITS,
    PIEZO_UNITS,
    piezo_units,
    PIEZO_WINDOWS,
    _filter_kwargs,
    _flag,
    _merge,
    _num,
    avg_window,
    decode_wave,
)

# 화면 선택지와 같다(v070과 같은 값). 'PSD 구간 N' 전체는 패킷 전체를 한 구간으로 쓴다.
PSD_SEGMENTS = ("전체", "8192", "4096", "2048", "1024", "512", "256", "128")
PSD_OVERLAPS = ("0", "25", "50", "75")
G0 = 9806.65  # 1 g = 9806.65 mm/s²


def psd_settings(options):
    """옵션의 psd 블록을 검사해서 계산에 쓸 값으로 바꾼다.

    돌려주는 dict
    - segment: 구간 N(int) 또는 None(전체), segment_text: 화면 표시용 글자
    - overlap: % (float), target: 평균 목표 구간 수(int, 1 이상), target_text
    잘못된 값이면 화면 문구 그대로 ValueError를 낸다(v070 메시지와 같음).
    """
    psd = options.get("psd") or {}
    seg_text = str(psd.get("segment") or "전체").strip()
    if seg_text not in PSD_SEGMENTS:
        raise ValueError(f"PSD 구간 N은 {', '.join(PSD_SEGMENTS)} 중 하나여야 합니다.")
    segment = None if seg_text == "전체" else int(seg_text)
    overlap = _num(psd.get("overlap"), 50)
    if not np.isfinite(overlap) or not 0 <= overlap < 100:
        raise ValueError("Overlap은 0 이상 100 미만이어야 합니다.")
    target_text = str(psd.get("target") if psd.get("target") is not None else "30").strip()
    if not target_text.isascii() or not target_text.isdecimal() or int(target_text) < 1:
        raise ValueError("PSD 평균 목표 구간에는 1 이상의 정수를 입력해 주세요.")
    return {
        "segment": segment,
        "segment_text": seg_text,
        "overlap": float(overlap),
        "target": int(target_text),
        "target_text": target_text,
    }


def base_unit(sensor, options):
    """PSD에 쓸 기본 단위. 밀도 단위는 이 값의 제곱/Hz다.

    v070 규칙: Count·Count Peak → Count, mV → mV, mm/s RMS → mm/s, 나머지(g RMS, g Peak, 자동) → g.
    Peak/RMS 구분은 밀도에는 의미가 없어서 버린다(PSD 적분이 곧 RMS).
    """
    if sensor == "PIEZO":
        # PIEZO도 FFT 단위 칸으로 정한다(2026-11-25 RAW·FFT 분리). mV Peak → mV.
        unit = piezo_units(options["piezo"])[1]
        if unit == "mV Peak":
            unit = "mV"
    else:
        unit = options["adxl"].get("fft_unit")
        unit = unit if unit in ADXL_FFT_UNITS else ADXL_FFT_UNITS[0]
    if unit in ("Count", "Count Peak"):
        return "Count"
    if unit == "mV":
        return "mV"
    if unit == "mm/s RMS":
        return "mm/s"
    return "g"


def _window(sensor, options):
    """(창 이름, Kaiser β). PIEZO는 PIEZO 창 목록과 β=8.6, ADXL은 MEMS 칸 값."""
    if sensor == "PIEZO":
        name = options["piezo"].get("window")
        return (name if name in PIEZO_WINDOWS else "Blackman"), 8.6
    name = str(options["adxl"].get("window") or "Blackman").strip()
    if name not in gen.FFT_WINDOWS:
        name = "Blackman"
    return name, _num(options["adxl"].get("beta"), 8.6)


def _mems_correction(options):
    """MEMS 주파수·감도 보정 설정. 화면 값이 저장값을 덮어쓴다."""
    raw = (options.get("adxl") or {}).get("correction") or {}
    return gen.validate_mems_correction(_merge(gen.load_mems_correction(), raw))


def axis_psd(sensor, obj, options, settings, unit, max_segments=None):
    """패킷(축) 하나의 PSD를 unit 단위 밀도로 만든다. 위 모듈 설명의 ①~⑤.

    sensor: 'PIEZO' 또는 'ADXL'. obj: 그 축의 MQTT JSON.
    unit: base_unit 값(Count / mV / g / mm/s).
    돌려주는 값: (주파수 Hz, 밀도, 쓴 구간 수, Fs, 샘플 수).
    """
    raw, fs = decode_wave("ADXL" if sensor == "ADXL" else "PIEZO", obj)
    if raw.size < 2 or fs <= 0:
        raise ValueError("샘플 부족")
    block = options["piezo"] if sensor == "PIEZO" else options["adxl"]
    window, beta = _window(sensor, options)
    # ① 평균 제거 + FFT용 필터. RAW용 필터 칸은 PSD에 쓰지 않는다.
    src = gen.spectral_filter(raw - np.mean(raw), fs, **_filter_kwargs(block.get("filters"), "FFT"))
    # ② Count²/Hz
    freq, power, used = gen.welch_packet_psd(
        src, fs, window, settings["segment"], settings["overlap"], beta, max_segments=max_segments
    )
    # ③ 단위 환산: 진폭 배율의 제곱
    if sensor == "PIEZO":
        if unit != "Count":
            scale = 1000.0 * _num(block.get("vmax"), 3.89) / _num(block.get("amax"), 4096)
            if unit != "mV":
                scale /= _num(block.get("sensitivity"), 100)
            if not np.isfinite(scale) or scale <= 0:
                raise ValueError("PIEZO 환산 설정 오류")
            power = power * scale ** 2
    else:
        if unit != "Count":
            scale = _num(block.get("g_per_lsb"), gen.read_mems_g_per_lsb())
            if not np.isfinite(scale) or not 0 < scale <= 10:
                raise ValueError("MEMS 환산 설정 오류")
            power = power * scale ** 2
        # ④ 감도 S(f)는 진폭에 거는 보정이라 √P에 적용하고 다시 제곱한다. Count면 감도 보정 없음.
        config = _mems_correction(options)
        freq, amp = gen.corrected_mems_fft(freq, np.sqrt(power), config, apply_sensitivity=unit != "Count")
        power = amp ** 2
        # 주파수축을 f/나눗수로 줄였으므로 같은 적분(파워)을 지키려면 밀도 × 나눗수.
        if config["frequency_enabled"]:
            power = power * config["frequency_divisor"]
    # ⑤ 속도 밀도
    if unit == "mm/s":
        positive = freq > 0
        velocity = np.zeros_like(power)
        velocity[positive] = power[positive] * (G0 / (2 * np.pi * freq[positive])) ** 2
        power = velocity
    return np.asarray(freq, dtype=np.float64), np.asarray(power, dtype=np.float64), int(used), float(fs), int(raw.size)


def _rms(freq, density):
    """⑥ RMS = √(Σ P × Δf). 주파수 칸이 하나뿐이면 계산하지 않는다."""
    if len(freq) < 2:
        return None
    return float(np.sqrt(max(0.0, float(np.sum(density)) * float(freq[1] - freq[0]))))


def packet_psd(sensor, packets, index, options, device=""):
    """선택 패킷부터 고른 범위의 Welch PSD 곡선과 요약. v070 _plot_packet_psd와 같은 결과.

    패킷 범위: 'FFT 평균 (Linear Averaging)'이 켜져 있으면 평균 개수(전체 가능)만큼, 꺼져 있으면 선택 패킷 하나.
      (v070도 PSD 모드에서는 Linear Averaging 체크를 '범위'로만 쓰고 FFT 평균은 하지 않는다.)
    목표 구간: 축마다 쓴 구간 수가 target이 되면 그 축은 더 읽지 않는다.
    Fs·샘플 수가 선택 패킷과 다른 패킷은 건너뛴다(주파수 칸이 달라 평균할 수 없음).
    PSD 평균: 구간 수로 가중한 평균 Σ(P_i × k_i) / Σk_i.

    돌려주는 dict는 generator_curves가 기존 곡선 위에 덮어쓴다.
    - fft: 축별 {"f": kHz, "y": 밀도}, fft_label: 밀도 단위
    - gn: ADXL에서 gN을 켜면 X+Y+Z 합성 PSD(같은 칸 합)
    - srss: 가로선 없음(PSD 합성 RMS는 밀도 단위가 아니라서 그래프에 선으로 그리지 않는다)
    - psd: 화면 'PSD 결과' 칸에 넣을 SRSS/한계값/판정/단계 문구
    """
    settings = psd_settings(options)
    # FFT 평균과 같은 패킷 범위: 지금 패킷부터 뒤로, 파일 끝이면 앞쪽으로 채운다(graph_options.avg_window).
    chosen, amount, fill_note = avg_window(packets, index, options)
    if not chosen:
        raise ValueError("PSD를 계산할 패킷이 없습니다")
    axes = ("PIEZO",) if sensor == "PIEZO" else ("X", "Y", "Z")
    unit = base_unit(sensor, options)
    window, beta = _window(sensor, options)
    target = settings["target"]

    def obj_of(packet, axis):
        return packet if sensor == "PIEZO" else (packet.get(axis) if isinstance(packet, dict) else None)

    # 기준: 선택 패킷의 축별 (Fs, 샘플 수). 구간 N이 샘플 수보다 크면 바로 알린다.
    refs = {}
    for axis in axes:
        obj = obj_of(chosen[0], axis)
        if obj is not None:
            raw, fs = decode_wave("ADXL" if sensor == "ADXL" else "PIEZO", obj)
            refs[axis] = (float(fs), int(raw.size))
            if settings["segment"] is not None and settings["segment"] > raw.size:
                raise ValueError(f"PSD 구간 N={settings['segment']} > {axis} 샘플 수 {raw.size}")
    sums = {}       # axis → (주파수, Σ P×k)
    counts = {}     # axis → 쓴 패킷 수
    segments = {}   # axis → 쓴 구간 수
    for packet in chosen:
        for axis in axes:
            obj = obj_of(packet, axis)
            if obj is None:
                continue
            remaining = target - segments.get(axis, 0)
            if remaining <= 0:
                continue
            try:
                freq, density, used, fs, size = axis_psd(sensor, obj, options, settings, unit, max_segments=remaining)
            except (ValueError, TypeError, OverflowError, ZeroDivisionError):
                continue
            if axis not in refs:
                refs[axis] = (fs, size)
            if refs[axis] != (fs, size):
                continue
            if axis not in sums:
                sums[axis] = (freq.copy(), np.zeros_like(density))
                counts[axis] = 0
                segments[axis] = 0
            if not np.array_equal(freq, sums[axis][0]):
                continue
            sums[axis][1][:] += density * used
            counts[axis] += 1
            segments[axis] += used

    enabled = (options["adxl"].get("axes") or {}) if sensor == "ADXL" else {}
    fft = {}
    axis_rms = {}
    details = []
    for axis, (freq, total) in sums.items():
        density = total / segments[axis]
        if sensor == "PIEZO" or _flag(enabled.get(axis), True):
            fft[axis] = {"f": freq / 1000.0, "y": density}
        level = _rms(freq, density)
        if level is not None:
            axis_rms[axis] = level
        details.append(f"{axis}: {counts[axis]}/{len(chosen)} 패킷, {segments[axis]} 구간")

    # 합성 SRSS = √(Σ(Px+Py+Pz) × Δf). 세 축 주파수 칸이 같을 때만.
    summary = {
        "srss": "XYZ 합성 SRSS: 계산 불가 (X/Y/Z 유효 PSD 필요)",
        "limit": "한계값: -",
        "judgement": "판정: 데이터 부족",
        "state": "none",       # none / ok / over (화면 색)
        "level": None,
        "limit_value": None,
    }
    gn = {"ok": False, "f": [], "y": []}
    srss_limit = {"ok": False, "f": [], "y": [], "value": None, "text": "", "device": device}
    if sensor == "ADXL" and all(a in sums and segments[a] > 0 for a in ("X", "Y", "Z")):
        xf = sums["X"][0]
        if len(xf) > 1 and all(np.array_equal(sums[a][0], xf) for a in ("Y", "Z")):
            combined = sum(sums[a][1] / segments[a] for a in ("X", "Y", "Z"))
            if _flag(enabled.get("gN"), True):
                gn = {"ok": True, "f": xf / 1000.0, "y": combined, "text": "XYZ 합성 PSD"}
            level = _rms(xf, combined)
            summary["level"] = level
            summary["srss"] = f"XYZ 합성 SRSS: {level:.6g} {unit} RMS"
            if unit == "mm/s":
                limit = float(gen.srss_limit_get(device))
                exceeded = level > limit
                summary.update(
                    limit=f"속도 한계값: {limit:.6g} mm/s RMS",
                    judgement="판정: 초과" if exceeded else "판정: 정상",
                    state="over" if exceeded else "ok",
                    limit_value=limit,
                )
                # 한계값 입력칸을 장치 값으로 맞추는 용도. 그래프에는 그리지 않는다(ok=False).
                srss_limit.update(value=limit)
            else:
                summary.update(limit="속도 한계값: mm/s RMS 선택 시 비교", judgement="판정: 단위 불일치로 비교 안 함")
        else:
            summary["judgement"] = "판정: PSD 주파수축 불일치"
    elif sensor != "ADXL":
        summary.update(srss="XYZ 합성 SRSS: MEMS X/Y/Z에만 적용", judgement="판정: MEMS 장치에서만 지원")

    rms_text = ", ".join(f"{a}={v:.6g} {unit} RMS" for a, v in axis_rms.items()) or "유효 데이터 없음"
    short = any(segments[a] < target for a in segments)
    status = (
        (" | ".join(details) or "유효 PSD 패킷 없음")
        + f" | 선택 {len(chosen)}/{amount} 패킷 | Overlap={settings['overlap']:g}% | 목표={settings['target_text']}구간"
        + f" | RMS: {rms_text}"
        + (" | 목표 미달(파일 구간 부족)" if short else "")
        + (f" | {fill_note}" if fill_note else "")
    )
    if unit == "mm/s":
        formula = "PSD (속도): Pa[g²/Hz] → Pv[(mm/s)²/Hz] = Pa × (9806.65/(2πf))², f>0 → vRMS[mm/s] = √(Σ Pv×Δf)"
    elif unit == "g":
        formula = "PSD (가속도): Pa[g²/Hz] = Welch 구간 PSD 평균 → aRMS[g] = √(Σ Pa×Δf) | 속도 변환은 mm/s RMS 선택 시 적용"
    else:
        formula = f"PSD ({unit}): P[{unit}²/Hz] = Welch 구간 PSD 평균 → RMS[{unit}] = √(Σ P×Δf) | 속도 변환은 mm/s RMS 선택 시 적용"
    steps = _steps(sensor, options, settings, refs, segments, unit, rms_text, window, beta)

    title = f"PSD | Window={window} | 구간 N={settings['segment_text']}"
    if sums:
        first_f = next(iter(sums.values()))[0]
        if len(first_f) > 1:
            title += f" | Δf={first_f[1] - first_f[0]:.6g} Hz"
    return {
        "fft": fft,
        "fft_label": f"({unit})²/Hz",
        "fft_x_label": "Frequency (kHz)",
        "gn": gn,
        "srss": {"ok": False, "f": [], "y": [], "text": title},
        "srss_limit": srss_limit,
        "avg_status": status,
        "psd_formula": formula,
        "psd": dict(summary, on=True, axis_rms=axis_rms, unit=unit, steps=steps, title=title,
                    empty=not fft, segments=dict(segments), target=target),
    }


def _steps(sensor, options, settings, refs, segments, unit, rms_text, window, beta):
    """PSD 연산 단계 ①~⑥을 현재 설정과 실제 값으로 적는다(v070 'PSD 연산 단계' 칸)."""
    if not refs:
        return "PSD 계산 가능한 유효 패킷 없음"
    fs0, n0 = next(iter(refs.values()))
    seg0 = n0 if settings["segment"] is None else settings["segment"]
    overlap = settings["overlap"]
    hop = max(1, seg0 - int(seg0 * overlap / 100))
    k0 = len(range(0, n0 - seg0 + 1, hop)) if seg0 <= n0 else 0
    gain = float(np.dot(gen.fft_window(seg0, window, beta), gen.fft_window(seg0, window, beta)))
    block = options["piezo"] if sensor == "PIEZO" else options["adxl"]
    kw = _filter_kwargs(block.get("filters"), "FFT")
    hpf = f"HPF={kw['hp_cutoff']:g}Hz/{kw['hp_order']}차 Butterworth" if kw["hpf"] else "HPF=OFF"
    if not kw["lpf"]:
        lpf = "LPF=OFF"
    elif kw["lp_cutoff"] is None:
        lpf = "LPF=Fs/2 상한(실제 필터 우회)"   # 자동 Fs/2
    else:
        lpf = f"LPF={kw['lp_cutoff']:g}Hz/{kw['lp_order']}차"
    notch = f"Notch={kw['notch_center']:g}Hz±{kw['notch_width']:g}" if kw["notch"] else "Notch=OFF"
    df = fs0 / seg0
    if unit == "mm/s":
        step5 = "⑤ 속도 PSD: Pv(f) = Pa(f) × (9806.65/(2πf))²   [단위: (mm/s)²/Hz, f>0]"
        step6 = f"⑥ 속도 RMS: vRMS = √(Σ Pv(f) × Δf), Δf={df:.6g}Hz → {rms_text}"
    elif unit == "g":
        step5 = "⑤ 가속도 PSD: Pa(f) = 평균된 PSD   [단위: g²/Hz] (속도 PSD 변환 미적용)"
        step6 = f"⑥ 가속도 RMS: aRMS = √(Σ Pa(f) × Δf), Δf={df:.6g}Hz → {rms_text}"
    else:
        step5 = f"⑤ {unit} PSD: P(f) = 평균된 PSD   [단위: {unit}²/Hz] (속도 PSD 변환 미적용)"
        step6 = f"⑥ {unit} RMS: RMS = √(Σ P(f) × Δf), Δf={df:.6g}Hz → {rms_text}"
    return "\n".join((
        f"① 원본: Fs={fs0:g}Hz, N={n0}, T=N/Fs={n0 / fs0:.6g}s, 원본 Δf={fs0 / n0:.6g}Hz",
        f"② 전처리: {hpf}, {lpf}, {notch}, Window={window}, Σw²={gain:.6g}",
        f"③ Welch: 구간 N={seg0}, Δf=Fs/N={df:.6g}Hz, Overlap={overlap:g}%, 이동={hop}샘플, 패킷당 최대 {k0}구간",
        f"④ 평균: Pavg(f)=Σ PSD_i(f)/M, 목표 M={settings['target_text']}, 실제 "
        + (", ".join(f"{a}={segments[a]}구간" for a in segments) or "0구간"),
        step5,
        step6,
    ))


def velocity_history(packets, options, start=1, count=None, device=""):
    """ADXL X/Y/Z 세트마다 독립 PSD로 속도 RMS를 구한 시간 이력. v070 calculate_adxl_velocity_history.

    - start: 1부터 세는 시작 세트 번호. count: 분석할 세트 수(None이면 끝까지).
    - 세트끼리 PSD를 평균하지 않는다. 한 세트 = 시간 이력의 점 하나.
    - 세트 안에서는 목표 구간 수(target)만큼만 Welch 평균한다(축마다).
    - 단위는 화면 FFT 단위와 상관없이 항상 mm/s(감도 보정 포함).
    - 시각: X 축 CSV의 received_at(원래 수신 시각). MQTT 재전송 시각이 아니다.
      received_at이 없는 세트(DB 패킷 등)는 건너뛴다.
    - 한계값: 첫 세트 장치의 SRSS 한계(설정 화면 값).

    돌려주는 dict: rows(점 목록), skipped(제외 사유), limit, settings, start, count, total.
    """
    settings = psd_settings(options)
    total = len(packets)
    if total == 0:
        raise ValueError("ADXL CSV 데이터가 없습니다.")
    start = int(start)
    if not 1 <= start <= total:
        raise ValueError(f"시작 패킷 번호는 1~{total} 사이여야 합니다.")
    count = total - start + 1 if count in (None, "") else int(count)
    if count < 1:
        raise ValueError("분석 세트 수는 1 이상이어야 합니다.")
    chosen = list(packets[start - 1:start - 1 + count])
    first = chosen[0] if chosen else {}
    if not device:
        x_obj = first.get("X") if isinstance(first, dict) else None
        device = str((x_obj or {}).get("dvicMacAddr") or "")
    limit = float(gen.srss_limit_get(device))
    rows = []
    skipped = []
    for offset, packet in enumerate(chosen):
        number = start + offset
        if not isinstance(packet, dict) or any(packet.get(a) is None for a in ("X", "Y", "Z")):
            skipped.append(f"{number}번째 세트: X/Y/Z 불완전")
            continue
        stamp = str(packet["X"].get("_received_at") or "")
        if not stamp:
            skipped.append(f"{number}번째 세트: X received_at 없음")
            continue
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            values = {}
            used = {}
            for axis in ("X", "Y", "Z"):
                freq, density, k, _fs, _n = axis_psd(
                    "ADXL", packet[axis], options, settings, "mm/s", max_segments=settings["target"]
                )
                level = _rms(freq, density)
                if k < 1 or level is None:
                    raise ValueError("PSD 구간 부족")
                values[axis] = level
                used[axis] = k
            srss = float(np.sqrt(sum(v * v for v in values.values())))
            rows.append({
                "set": number,
                "packet_no": str(packet["X"].get("_packet_no") or number),
                "received_at": stamp,
                "t": when.timestamp() * 1000.0,   # 브라우저 시간축(ms)
                "x": values["X"], "y": values["Y"], "z": values["Z"],
                "srss": srss,
                "over": srss > limit,
                "segments": used,
            })
        except (ValueError, TypeError, KeyError, OverflowError, ZeroDivisionError) as exc:
            skipped.append(f"{number}번째 세트: {exc}")
    rows.sort(key=lambda row: row["t"])
    if not rows:
        raise ValueError("유효한 RMS를 계산하지 못했습니다. " + " / ".join(skipped[:3]))
    return {
        "sensor": "ADXL",
        "rows": rows,
        "skipped": skipped,
        "limit": limit,
        "device": device,
        "start": start,
        "count": len(chosen),
        "total": total,
        "settings": {
            "segment": settings["segment_text"],
            "overlap": settings["overlap"],
            "target": settings["target"],
        },
    }


def piezo_velocity_history(packets, options, start=1, count=None, device=""):
    """PIEZO 패킷마다 독립 PSD로 속도 RMS(Vrms)를 구한 시간 이력(2026-11-25, 센서 데이터 분석 PIEZO 설정).

    ADXL velocity_history와 같은 길이다. 다른 점만:
    - PIEZO는 한 축이라 패킷 하나 = 점 하나(세트 대신 '패킷'). 값은 vrms 하나(SRSS 없음).
    - 환산은 서랍 PIEZO 칸(Vmax·Amax·감도)과 PIEZO 창·FFT 필터(axis_psd 'PIEZO', 단위 mm/s).
      매시간 통계(stat_profile.piezo_stats)는 DB 통계 기준 값을 쓰므로, 서랍 값이 다르면 두 값도 다르다.
    - 이 창에는 한계선을 그리지 않는다(서랍 환산 값이라 통계 값과 다를 수 있음). 센서 현황 판정은 통계 Vrms와 설정 기준으로 한다(site_status.JUDGED_KINDS).
    - 시각: 패킷의 received_at(_received_at). 없으면 건너뛴다.
    돌려주는 dict: sensor='PIEZO', rows, skipped, limit=None, settings, start, count, total, device.
    """
    settings = psd_settings(options)
    total = len(packets)
    if total == 0:
        raise ValueError("PIEZO 데이터가 없습니다.")
    start = int(start)
    if not 1 <= start <= total:
        raise ValueError(f"시작 패킷 번호는 1~{total} 사이여야 합니다.")
    count = total - start + 1 if count in (None, "") else int(count)
    if count < 1:
        raise ValueError("분석 패킷 수는 1 이상이어야 합니다.")
    chosen = list(packets[start - 1:start - 1 + count])
    if not device and chosen and isinstance(chosen[0], dict):
        device = str(chosen[0].get("dvicMacAddr") or "")
    rows = []
    skipped = []
    for offset, packet in enumerate(chosen):
        number = start + offset
        if not isinstance(packet, dict):
            skipped.append(f"{number}번째 패킷: 형식 오류")
            continue
        stamp = str(packet.get("_received_at") or "")
        if not stamp:
            skipped.append(f"{number}번째 패킷: received_at 없음")
            continue
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            freq, density, k, _fs, _n = axis_psd(
                "PIEZO", packet, options, settings, "mm/s", max_segments=settings["target"]
            )
            level = _rms(freq, density)
            if k < 1 or level is None:
                raise ValueError("PSD 구간 부족")
            rows.append({
                "set": number,
                "packet_no": str(packet.get("_packet_no") or number),
                "received_at": stamp,
                "t": when.timestamp() * 1000.0,
                "vrms": level,
                "over": False,
                "segments": {"PIEZO": k},
            })
        except (ValueError, TypeError, KeyError, OverflowError, ZeroDivisionError) as exc:
            skipped.append(f"{number}번째 패킷: {exc}")
    rows.sort(key=lambda row: row["t"])
    if not rows:
        raise ValueError("유효한 RMS를 계산하지 못했습니다. " + " / ".join(skipped[:3]))
    return {
        "sensor": "PIEZO",
        "rows": rows,
        "skipped": skipped,
        "limit": None,
        "device": device,
        "start": start,
        "count": len(chosen),
        "total": total,
        "settings": {
            "segment": settings["segment_text"],
            "overlap": settings["overlap"],
            "target": settings["target"],
        },
    }

"""웹 그래프 수식. MQTT payload와 저장 HEX는 여기서 바꾸지 않는다.

발생기 곡선은 generator_curves, 모니터 단일 축은 monitor_curves,
ADXL X/Y/Z 공유 눈금은 monitor_adxl_set이 만든다.
모니터는 시간을 초, FFT 주파수를 Hz로 되돌린다. 발생기 미리보기는 ms, kHz다.

PIEZO: K = 1000 × Vmax / Amax [mV/Count], g = (Count−평균) × K / 감도.
ADXL: g = Count × (g/LSB). FFT g RMS = Count Peak × (g/LSB) / √2.
ADXL FFT 보정: G_final = G / S(f), f_final = f / 1.014.
SRSS는 X/Y/Z 단측 peak로 만든 전체 AC RMS 하나이고 FFT 가로선으로만 그린다.
기본 FFT 창은 Blackman이다. 식의 기본값은 docs/단위환산.md, 화면 동작은 docs/웹-기능상세.md.
"""

from __future__ import annotations

import numpy as np

from web import core as gen
from web import core as mon


PIEZO_UNITS = ("Count", "mV", "g RMS", "mm/s RMS")
PIEZO_WINDOWS = ("Blackman", "Hann", "Hamming", "Rectangular")
SRSS_MISSING = "SRSS: 완전한 XYZ 데이터 없음"
ADXL_RAW_UNITS = ("자동 (g/mg/µg)", "Count", "g", "mg", "µg")
ADXL_FFT_UNITS = ("자동 (g RMS)", "Count Peak", "g Peak", "g RMS")


def _flag(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "off", "no", ""}


def _num(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not np.isfinite(number):
        return float(default)
    return number


def _merge(base, raw):
    if not isinstance(raw, dict):
        return base
    merged = dict(base)
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _filter_block(hpf_hz, lpf_hz):
    return {
        "HPF": {"raw": False, "fft": False, "freq": hpf_hz, "extra": 4},
        "LPF": {"raw": False, "fft": False, "freq": lpf_hz, "extra": 4},
        "Notch": {"raw": False, "fft": False, "freq": 975, "extra": 20},
    }


def default_options():
    correction = gen.load_mems_correction()
    return {
        "fft_log_x": False,
        "fft_log_y": False,
        "fft_avg": False,
        "fft_avg_count": "16",
        "piezo": {
            "unit": "mV",
            "vmax": 3.89,
            "amax": 4096,
            "sensitivity": 100,
            "window": "Blackman",
            "window_n": "전체",
            "filters": _filter_block(10, 3000),
        },
        "adxl": {
            "axes": {"X": True, "Y": True, "Z": True, "SRSS": True},
            "raw_unit": "자동 (g/mg/µg)",
            "fft_unit": "자동 (g RMS)",
            "g_per_lsb": gen.read_mems_g_per_lsb(),
            "window": "Blackman",
            "window_n": "전체",
            "beta": 8.6,
            "filters": _filter_block(10, 1000),
            "correction": correction,
            "rpm": 1800,
            "rot_axis": "X",
            "rot_type": "미지정",
        },
    }


def resolve_options(raw):
    return _merge(default_options(), raw or {})


def _filter_kwargs(filters, kind):
    filters = filters or {}
    hpf = filters.get("HPF") or {}
    lpf = filters.get("LPF") or {}
    notch = filters.get("Notch") or {}
    field = "raw" if kind == "RAW" else "fft"
    return dict(
        hpf=_flag(hpf.get(field)),
        hp_cutoff=_num(hpf.get("freq"), 10),
        hp_order=max(1, int(_num(hpf.get("extra"), 4))),
        lpf=_flag(lpf.get(field)),
        lp_cutoff=_num(lpf.get("freq"), 3000),
        lp_order=max(1, int(_num(lpf.get("extra"), 4))),
        notch=_flag(notch.get(field)),
        notch_center=_num(notch.get("freq"), 975),
        notch_width=_num(notch.get("extra"), 20),
    )


def _window_n(value):
    text = str(value or "전체").strip()
    if text in {"", "전체", "0"}:
        return None
    count = int(float(text))
    if count < 2:
        raise ValueError("FFT Window N은 2 이상이어야 합니다")
    return count


def decode_wave(sensor, obj):
    """sensorData HEX → 부호 있는 16비트 Count. 시간축은 n / Fs 이고 Fs는 Hz다."""
    raw_hex = "".join(str(obj.get("sensorData") or "").split())
    raw = bytes.fromhex(raw_hex)
    if len(raw) % 2:
        raise ValueError(f"{sensor} bytes 길이는 짝수여야 합니다")
    samples = np.frombuffer(raw, dtype=">i2").astype(np.float64)
    default = 3200.0 if sensor == "ADXL" else 25600.0
    rate = mon.normalize_sample_rate(sensor, obj.get("samplerate"), default)
    return samples, float(rate)


def _scaled_unit(values, suffix):
    """g로 환산한 최댓값으로 눈금만 고른다. 1 미만은 mg(×1000), 0.001 미만은 µg(×1e6)."""
    finite = []
    for item in values:
        data = np.asarray(item, dtype=np.float64)
        if data.size and np.any(np.isfinite(data)):
            finite.append(float(np.max(np.abs(data[np.isfinite(data)]))))
    maxval = max(finite) if finite else 0.0
    if maxval < 0.001:
        return 1e6, "µg" + suffix
    if maxval < 1:
        return 1e3, "mg" + suffix
    return 1.0, "g" + suffix


def _correction(options):
    raw = (options.get("adxl") or {}).get("correction") or {}
    return gen.validate_mems_correction(_merge(gen.load_mems_correction(), raw))


def _average(sensor, packets, index, options):
    if not _flag(options.get("fft_avg")):
        return {}, "평균 OFF — 현재 패킷 FFT 표시"
    choice = str(options.get("fft_avg_count") or "16").strip()
    amount = (len(packets) - index) if choice == "전체" else int(float(choice))
    if amount < 1:
        raise ValueError("FFT 평균 개수는 1 이상이어야 합니다")
    chosen = packets[index:min(len(packets), index + amount)]
    piezo = options["piezo"]
    adxl = options["adxl"]
    axes = ("PIEZO",) if sensor == "PIEZO" else ("X", "Y", "Z")
    result = {}
    messages = []
    for axis in axes:
        reference_f = None
        reference_shape = None
        summed = None
        accepted = 0
        skipped = 0
        for packet in chosen:
            obj = packet if sensor == "PIEZO" else packet.get(axis)
            if obj is None:
                skipped += 1
                continue
            try:
                raw, fs = decode_wave("ADXL" if sensor == "ADXL" else "PIEZO", obj)
                if raw.size < 2 or fs <= 0:
                    raise ValueError("샘플 부족")
                if sensor == "PIEZO":
                    source = gen.spectral_filter(raw - np.mean(raw), fs, **_filter_kwargs(piezo["filters"], "FFT"))
                    window = piezo["window"] if piezo["window"] in PIEZO_WINDOWS else "Blackman"
                    freq, amp = gen.fft_peak(source, fs, window, _window_n(piezo.get("window_n")))
                else:
                    source = gen.spectral_filter(raw, fs, **_filter_kwargs(adxl["filters"], "FFT"))
                    count = _window_n(adxl.get("window_n"))
                    if count is not None and count > raw.size:
                        raise ValueError("FFT Window N이 패킷 샘플 수보다 큼")
                    freq, amp = gen.fft_peak(
                        source, fs, _window_name(adxl.get("window")), count, _num(adxl.get("beta"), 8.6)
                    )
                if reference_f is None:
                    reference_f = freq
                    reference_shape = (fs, raw.size)
                    summed = np.zeros_like(amp)
                if reference_shape != (fs, raw.size) or freq.shape != reference_f.shape or not np.array_equal(freq, reference_f):
                    skipped += 1
                    continue
                summed += amp
                accepted += 1
            except (ValueError, TypeError, OverflowError):
                skipped += 1
        if accepted:
            result[axis] = (reference_f, summed / accepted, accepted)
        note = f"{axis}: {accepted}/{len(chosen)}회"
        if skipped:
            note += f" (제외 {skipped})"
        messages.append(note)
    trailing = "" if len(chosen) == amount else f" | 파일 끝: 요청 {amount}회 중 {len(chosen)}회 검색"
    return result, " | ".join(messages) + trailing


def _piezo_curves(packet, averaged, options):
    piezo = options["piezo"]
    raw, fs = decode_wave("PIEZO", packet)
    if raw.size < 2 or fs <= 0:
        raise ValueError("PIEZO 샘플이 부족합니다")
    vmax = _num(piezo.get("vmax"), 3.89)
    amax = _num(piezo.get("amax"), 4096)
    sens = _num(piezo.get("sensitivity"), 100)
    if not all(np.isfinite(x) and x > 0 for x in (vmax, amax, sens)):
        raise ValueError("Vmax/Amax/센서 감도는 유효한 양수여야 합니다")
    # K [mV/Count] = 1000 × Vmax / Amax. 기본 3.89 V, 4096 → 0.94970703125.
    scale = 1000.0 * vmax / amax
    # RAW는 평균을 뺀 Count. 상수 오프셋(Amax/2)은 평균 제거로 사라진다.
    centered = raw - np.mean(raw)
    filtered = gen.spectral_filter(centered, fs, **_filter_kwargs(piezo["filters"], "RAW"))
    fft_src = gen.spectral_filter(centered, fs, **_filter_kwargs(piezo["filters"], "FFT"))
    window = piezo.get("window") if piezo.get("window") in PIEZO_WINDOWS else "Blackman"
    count = _window_n(piezo.get("window_n"))
    if count is not None and count > raw.size:
        raise ValueError(f"Window N={count} > 실제 샘플 {raw.size}")
    # Count Peak[k] = 2 × |rFFT((Count−평균)×창)[k]| / Σ창. 0 Hz·나이퀴스트는 2를 곱하지 않는다.
    freq, peak = gen.fft_peak(fft_src, fs, window, count)
    if averaged and "PIEZO" in averaged:
        freq, peak, _used = averaged["PIEZO"]
    unit = piezo.get("unit") if piezo.get("unit") in PIEZO_UNITS else "mV"
    if unit == "Count":
        wave, wave_label = filtered, "Count (centered)"
        spectrum, fft_label = peak, "Count Peak"
        detail = "RAW C[n]−mean(C) | FFT Count Peak"
    elif unit == "mV":
        wave, wave_label = filtered * scale, "mV (AC)"
        spectrum, fft_label = peak * scale, "mV Peak"
        detail = f"K=1000×{vmax:g}/{amax:g}={scale:.6g} mV/Count | RAW=(C−mean)×K | FFT=Count Peak×K"
    elif unit == "g RMS":
        # RAW g = (Count−평균) × K / 감도. FFT g RMS = Count Peak × K / (감도 × √2).
        wave, wave_label = filtered * scale / sens, "g (instantaneous AC)"
        spectrum, fft_label = peak * scale / sens / np.sqrt(2.0), "g RMS / bin"
        total = float(np.std(raw * scale / sens))
        detail = f"전체 AC g RMS={total:.6g} | RAW=(C−mean)×K/감도 | FFT=Count Peak×K/(감도×√2)"
    else:
        wave = filtered * scale / sens
        wave_label = "g (instantaneous AC)"
        grms = peak * scale / sens / np.sqrt(2.0)
        spectrum = np.zeros_like(grms)
        positive = freq > 0
        # mm/s RMS = g RMS × 9806.65 / (2πf), f > 0. RAW Y는 g와 같다.
        spectrum[positive] = grms[positive] * 9806.65 / (2 * np.pi * freq[positive])
        fft_label = "mm/s RMS / bin"
        detail = "FFT velocity = g RMS × 9806.65 / (2πf), f>0"
    time_ms = np.arange(raw.size, dtype=np.float64) / fs * 1000.0
    peak_note = ""
    if spectrum.size > 1:
        peak_at = 1 + int(np.argmax(spectrum[1:]))
        peak_note = f" | peak {freq[peak_at]:.2f} Hz / {spectrum[peak_at]:.6g} {fft_label}"
    return {
        "kind": "waveform",
        "x_label": "Time (ms)",
        "y_label": wave_label,
        "fft_x_label": "Frequency (kHz)",
        "fft_label": fft_label,
        # 발생기 눈금: 시간 ms, 주파수 kHz. 모니터는 monitor_curves에서 s, Hz로 되돌린다.
        "series": {"PIEZO": {"t": time_ms, "y": wave}},
        "fft": {"PIEZO": {"f": freq / 1000.0, "y": spectrum}},
        "info": f"PIEZO N={raw.size} Fs={fs:g} Vmax={vmax:g} Amax={amax:g} 감도={sens:g} mV/g{peak_note}",
        "formula": detail + " | 미리보기 전용, MQTT 원본 유지",
        "rotation": "",
    }


def _window_name(value):
    name = str(value or "Blackman").strip()
    if name not in gen.FFT_WINDOWS:
        return "Blackman"
    return name


def _line_samples(freq, level, limit=256):
    freq = np.asarray(freq, dtype=np.float64)
    if freq.size == 0 or not np.isfinite(level):
        return [], []
    step = max(1, int(np.ceil(freq.size / float(limit))))
    index = np.arange(0, freq.size, step)
    if int(index[-1]) != freq.size - 1:
        index = np.append(index, freq.size - 1)
    chosen = freq[index]
    height = float(level)
    return [float(value) for value in chosen], [height for _value in chosen]


def _srss_from_packets(packets, index, options):
    """Packet SRSS scalars. Averaging means the scalars, never an averaged spectrum."""
    adxl = options["adxl"]
    fft_mode = adxl.get("fft_unit") if adxl.get("fft_unit") in ADXL_FFT_UNITS else ADXL_FFT_UNITS[0]
    window = _window_name(adxl.get("window"))
    beta = _num(adxl.get("beta"), 8.6)
    count = _window_n(adxl.get("window_n"))
    correction = _correction(options)
    g_per_lsb = _num(adxl.get("g_per_lsb"), gen.read_mems_g_per_lsb())
    packets = list(packets or [])
    if not packets:
        return [], None, 0, False
    index = max(0, min(len(packets) - 1, int(index)))
    averaging = _flag(options.get("fft_avg"))
    if averaging:
        choice = str(options.get("fft_avg_count") or "16").strip()
        amount = (len(packets) - index) if choice == "전체" else int(float(choice))
        if amount < 1:
            raise ValueError("FFT 평균 개수는 1 이상이어야 합니다")
        chosen = packets[index:index + amount]
    else:
        chosen = [packets[index]]
    levels = []
    reference = None
    line_freq = None
    for packet in chosen:
        try:
            if not isinstance(packet, dict) or any(packet.get(axis) is None for axis in ("X", "Y", "Z")):
                continue
            powers = []
            signature = None
            current_f = None
            for axis in ("X", "Y", "Z"):
                raw, fs = decode_wave("ADXL", packet[axis])
                nn = int(raw.size if count is None else count)
                if nn < 2 or nn > int(raw.size) or fs <= 0:
                    raise ValueError("SRSS N")
                sig = (float(fs), int(raw.size), nn)
                if signature is not None and sig != signature:
                    raise ValueError("SRSS XYZ mismatch")
                signature = sig
                fft_src = gen.spectral_filter(raw, fs, **_filter_kwargs(adxl["filters"], "FFT"))
                freq, peak = gen.fft_peak(fft_src, fs, window, count, beta)
                peak_scale = 1.0 if fft_mode == "Count Peak" else g_per_lsb
                corrected_f, corrected_peak = gen.corrected_mems_fft(
                    freq,
                    peak * peak_scale,
                    correction,
                    apply_sensitivity=fft_mode != "Count Peak",
                )
                if current_f is not None and (
                    corrected_f.shape != current_f.shape or not np.array_equal(corrected_f, current_f)
                ):
                    raise ValueError("SRSS bins")
                current_f = corrected_f
                powers.append(gen.srss_axis_power(corrected_peak, nn, gen.fft_window(nn, window, beta)))
            if reference is not None and signature != reference:
                continue
            reference = signature
            line_freq = current_f
            levels.append(float(np.sqrt(float(np.sum(powers)))))
        except (ValueError, TypeError, OverflowError):
            continue
    return levels, line_freq, len(chosen), averaging


def _adxl_srss(packet, options, packets, index, fft_mode, fft_scale, fft_label, lsb):
    try:
        levels, line_freq, searched, averaging = _srss_from_packets(
            packets if packets else [packet], index, options
        )
    except (ValueError, TypeError, OverflowError):
        levels, line_freq, searched, averaging = [], None, 0, False
    if not levels or line_freq is None or np.asarray(line_freq).size == 0:
        return {"ok": False, "text": SRSS_MISSING, "unit": "", "level": None, "f": [], "y": []}
    level = float(np.mean(levels))
    if str(fft_mode).startswith("자동"):
        level *= float(fft_scale) * lsb * np.sqrt(2.0)
        unit = fft_label
    elif fft_mode == "Count Peak":
        unit = "Count RMS"
    else:
        unit = "g RMS"
    if not np.isfinite(level):
        return {"ok": False, "text": SRSS_MISSING, "unit": "", "level": None, "f": [], "y": []}
    text = "SRSS = {:.6g} {}".format(level, unit)
    if averaging:
        text += " | 평균 {}/{}회".format(len(levels), searched)
    line_f, line_y = _line_samples(np.asarray(line_freq, dtype=np.float64) / 1000.0, level)
    return {"ok": True, "text": text, "unit": unit, "level": float(level), "f": line_f, "y": line_y}


def _adxl_curves(packet, averaged, options, packets=None, index=0):
    adxl = options["adxl"]
    g_per_lsb = _num(adxl.get("g_per_lsb"), gen.read_mems_g_per_lsb())
    if not 0 < g_per_lsb <= 10:
        raise ValueError("1 LSB당 g는 0보다 크고 10 이하여야 합니다")
    # RAW g = Count / (LSB/g) = Count × (g/LSB). 공칭 0.00390625, 저장값 0.00380625.
    lsb = 1.0 / g_per_lsb
    correction = _correction(options)
    raw_mode = adxl.get("raw_unit") if adxl.get("raw_unit") in ADXL_RAW_UNITS else ADXL_RAW_UNITS[0]
    fft_mode = adxl.get("fft_unit") if adxl.get("fft_unit") in ADXL_FFT_UNITS else ADXL_FFT_UNITS[0]
    window = _window_name(adxl.get("window"))
    beta = _num(adxl.get("beta"), 8.6)
    count = _window_n(adxl.get("window_n"))
    enabled = adxl.get("axes") or {}
    raws = []
    ffts = []
    summaries = []
    spectra = {}
    for axis in ("X", "Y", "Z"):
        if axis not in packet or not _flag(enabled.get(axis), True):
            continue
        raw, fs = decode_wave("ADXL", packet[axis])
        if raw.size < 2 or fs <= 0:
            raise ValueError(f"ADXL {axis} 샘플이 부족합니다")
        if count is not None and count > raw.size:
            raise ValueError(f"Window N={count} > 실제 샘플 {raw.size}")
        wave = gen.spectral_filter(raw, fs, **_filter_kwargs(adxl["filters"], "RAW"))
        fft_src = gen.spectral_filter(raw, fs, **_filter_kwargs(adxl["filters"], "FFT"))
        # amp는 Count Peak. 평균 제거는 fft_peak 안에서만 한다. RAW 파형에는 중력이 남는다.
        freq, amp = gen.fft_peak(fft_src, fs, window, count, beta)
        if averaged and axis in averaged:
            freq, amp, _used = averaged[axis]
        spectra[axis] = (freq, amp)
        # 전체 AC g RMS = std(Count × g/LSB). FFT 한 칸의 g RMS와 다른 값이다.
        summaries.append(f"{axis} AC g RMS={float(np.std(raw / lsb)):.6g}")
        raws.append((axis, wave, fs))
        ffts.append((axis, freq, amp))
    if str(raw_mode).startswith("자동"):
        # values/lsb 는 g. 눈금 배율을 고른 뒤 다시 /lsb 해서 Count에 곱할 계수를 만든다.
        raw_scale, raw_label = _scaled_unit([values / lsb for _axis, values, _fs in raws], "")
        raw_scale /= lsb
    else:
        raw_scale, raw_label = {
            "Count": (1.0, "Count"),
            "g": (1.0 / lsb, "g"),
            "mg": (1000.0 / lsb, "mg"),
            "µg": (1e6 / lsb, "µg"),
        }[raw_mode]
    if str(fft_mode).startswith("자동"):
        # 비교값 = Count Peak × (g/LSB) / √2. 배율은 g, mg(×1000), µg(×1e6) 중 하나.
        fft_scale, fft_label = _scaled_unit(
            [amp / lsb / np.sqrt(2.0) for _axis, _freq, amp in ffts], " RMS"
        )
        fft_scale /= lsb * np.sqrt(2.0)
    else:
        fft_scale, fft_label = {
            "Count Peak": (1.0, "Count Peak"),
            "g Peak": (1.0 / lsb, "g Peak"),
            # g RMS = Count Peak × (g/LSB) / √2.
            "g RMS": (1.0 / lsb / np.sqrt(2.0), "g RMS / bin"),
        }[fft_mode]
    # Count Peak는 물리 단위가 아니므로 S(f)로 나누지 않는다. f / 1.014는 그대로 적용된다.
    physical = fft_mode != "Count Peak"
    series = {}
    fft = {}
    peaks = []
    for axis, wave, fs in raws:
        series[axis] = {"t": np.arange(wave.size, dtype=np.float64) / fs * 1000.0, "y": wave * raw_scale}
    for axis, freq, amp in ffts:
        # G_final = G / S(f), f_final = f / 1.014. S는 나누기 전 측정 주파수.
        # S: f≤60 → 0.99, 60<f≤360 → 1.07−0.0005×f, f>360 → 0.89. 나눗수 기본 1.014.
        shown_f, shown_a = gen.corrected_mems_fft(
            freq, amp * fft_scale, correction, apply_sensitivity=physical
        )
        fft[axis] = {"f": shown_f / 1000.0, "y": shown_a}
        if shown_a.size > 1:
            peak_at = 1 + int(np.argmax(shown_a[1:]))
            peaks.append(f"{axis} {shown_f[peak_at]:.2f} Hz / {shown_a[peak_at]:.6g} {fft_label}")
    active = []
    for name, spec in (adxl.get("filters") or {}).items():
        if _flag(spec.get("raw")) or _flag(spec.get("fft")):
            marks = []
            if _flag(spec.get("raw")):
                marks.append("RAW")
            if _flag(spec.get("fft")):
                marks.append("FFT")
            active.append(f"{name} {'+'.join(marks)}")
    formula = (
        f"RAW {raw_label} | FFT {fft_label} | 1 LSB={g_per_lsb:.8g} g | Window={window} "
        f"N={count or '전체'} | 필터={', '.join(active) if active else 'OFF'} | "
        f"주파수 보정={correction['frequency_enabled']} 감도 보정={correction['sensitivity_enabled']} | "
        "AC g RMS=std(Count/LSB) | 미리보기 전용"
    )
    rotation = _rotation(spectra, options)
    srss = _adxl_srss(packet, options, packets, index, fft_mode, fft_scale, fft_label, lsb)
    return {
        "kind": "waveform",
        "x_label": "Time (ms)",
        "y_label": "ADXL " + raw_label,
        "fft_x_label": "Frequency (kHz)",
        "fft_label": fft_label,
        "series": series,
        "fft": fft,
        "srss": srss,
        "info": " | ".join(summaries + peaks) if summaries else "표시할 축 없음",
        "formula": formula,
        "rotation": rotation,
    }


def _rotation(spectra, options):
    adxl = options["adxl"]
    axis = str(adxl.get("rot_axis") or "X")
    data = spectra.get(axis)
    if not data:
        return f"{axis} 축 FFT 데이터가 없습니다"
    freq, amp = data
    rpm = _num(adxl.get("rpm"), 1800)
    if rpm <= 0:
        return "RPM은 양수여야 합니다"
    try:
        candidates = gen.rotating_candidates(freq, amp, rpm)
    except Exception as exc:
        return f"회전부품 추정 오류: {exc}"
    kind = str(adxl.get("rot_type") or "미지정")
    if not candidates:
        return f"[{kind} / {axis} / {rpm:g} RPM] 일치하는 후보가 없습니다"
    text = " | ".join(
        f"{count}개 ({hz:.2f} Hz, 일치 {len(matches)}개, 점수 {score:.3f})"
        for score, count, hz, matches in candidates
    )
    return f"[{kind} / {axis} / {rpm:g} RPM] 후보: {text} ※ 차수 일치 후보이며 부품 수 확정이 아닙니다"


def _ultrasonic(packet):
    freq, amp, start_khz, step_hz = gen.decode_ultrasonic_spectrum(packet)
    peak_at = int(np.argmax(amp)) if amp.size else 0
    peak = f"peak {freq[peak_at]:.3f} kHz : {amp[peak_at]:.6g}" if amp.size else "데이터 없음"
    return {
        "kind": "spectrum",
        "x_label": "Frequency (kHz)",
        "y_label": "H spectrum",
        "fft_x_label": "Frequency (kHz)",
        "fft_label": "",
        "x": freq,
        "y": amp,
        "series": {},
        "fft": {},
        "info": f"시작 {start_khz} kHz / 간격 {step_hz} Hz / {amp.size} bins | {peak}",
        "formula": "초음파 H는 320-bin 스펙트럼 원본입니다. 송신 payload는 바꾸지 않습니다.",
        "rotation": "",
    }


def generator_curves(sensor, packet, packets, index, options):
    """파일 한 패킷의 RAW/FFT. 초음파는 스펙트럼만 있고 FFT 서브플롯이 없다."""
    options = resolve_options(options)
    averaged, status = ({}, "")
    if sensor in ("PIEZO", "ADXL"):
        averaged, status = _average(sensor, packets, index, options)
    if sensor == "초음파":
        curves = _ultrasonic(packet)
    elif sensor == "PIEZO":
        curves = _piezo_curves(packet, averaged, options)
    elif sensor == "ADXL":
        curves = _adxl_curves(packet, averaged, options, packets, index)
    else:
        raise ValueError("알 수 없는 센서")
    curves["avg_status"] = status
    curves["fft_log_x"] = _flag(options.get("fft_log_x")) and sensor != "초음파"
    curves["fft_log_y"] = _flag(options.get("fft_log_y")) and sensor != "초음파"
    return curves


def monitor_adxl_set(axis_samples, options):
    """X/Y/Z를 한 번에 환산해 RAW·FFT 눈금을 공유한다.

    축마다 따로 자동 단위를 고르면 Z(중력, 약 1g)는 g, X/Y는 mg가 되어
    같은 그래프에서 작은 축이 바닥에 붙는다.
    """
    options = resolve_options(options)
    packet = {}
    for axis, (samples, fs) in axis_samples.items():
        if axis not in ("X", "Y", "Z"):
            continue
        packet[axis] = {"sensorData": _counts_to_hex(samples), "samplerate": float(fs or 0)}
    if not packet:
        return {}
    options["adxl"]["axes"] = {axis: axis in packet for axis in ("X", "Y", "Z")}
    curves = _adxl_curves(packet, {}, options, [packet], 0)
    shown = {}
    for axis in packet:
        wave = curves["series"][axis]
        spectrum = curves["fft"][axis]
        shown[axis] = {
            "t": np.asarray(wave["t"], dtype=np.float64) / 1000.0,
            "y": np.asarray(wave["y"], dtype=np.float64),
            "fft_f": np.asarray(spectrum["f"], dtype=np.float64) * 1000.0,
            "fft_y": np.asarray(spectrum["y"], dtype=np.float64),
            "unit": curves["y_label"],
            "fft_unit": curves["fft_label"],
            "formula": curves["formula"],
            "info": curves["info"],
            "fft_log_x": _flag(options.get("fft_log_x")),
            "fft_log_y": _flag(options.get("fft_log_y")),
        }
    srss = curves.get("srss") or {}
    shown["_srss"] = {
        "ok": bool(srss.get("ok")),
        "text": srss.get("text") or "",
        "unit": srss.get("unit") or "",
        "level": None if srss.get("level") is None else float(srss["level"]),
        "f": [float(value) * 1000.0 for value in (srss.get("f") or [])],
        "y": [float(value) for value in (srss.get("y") or [])],
    }
    return shown


def monitor_curves(samples, fs, sensor_kind, options, axis="X"):
    """발생기와 같은 g·g RMS 환산. 시간만 ms→s, FFT 주파수만 kHz→Hz로 되돌린다."""
    samples = np.asarray(samples, dtype=np.float64)
    fs = float(fs or 0)
    if samples.size < 2 or fs <= 0:
        raise ValueError("그래프에 필요한 샘플 또는 샘플링 속도가 없습니다")
    fake = {"sensorData": _counts_to_hex(samples), "samplerate": fs}
    options = resolve_options(options)
    if sensor_kind == "ADXL":
        name = axis if axis in ("X", "Y", "Z") else "X"
        options["adxl"]["axes"] = {item: item == name for item in ("X", "Y", "Z")}
        curves = _adxl_curves({name: fake}, {}, options)
        chosen = name
    else:
        curves = _piezo_curves(fake, {}, options)
        chosen = "PIEZO"
    wave = curves["series"][chosen]
    spectrum = curves["fft"][chosen]
    return {
        "t": wave["t"] / 1000.0,
        "y": wave["y"],
        "fft_f": spectrum["f"] * 1000.0,
        "fft_y": spectrum["y"],
        "unit": curves["y_label"],
        "fft_unit": curves["fft_label"],
        "formula": curves["formula"],
        "info": curves["info"],
        "fft_log_x": _flag(options.get("fft_log_x")),
        "fft_log_y": _flag(options.get("fft_log_y")),
    }


def _counts_to_hex(samples):
    clipped = np.clip(np.rint(samples), -32768, 32767).astype(">i2")
    return clipped.tobytes().hex()


FFT_CHECK_NOTE = (
    "① 입력 → ② 양측 FFT → ③ 단측 FFT | "
    "② 복소수 FFT → ④ IFFT 복원 · 평균 제거/SRSS/물리단위 보정은 검증 제외"
)
FFT_CHECK_STEPS = [
    "① 입력: 원본 RAW Count. 평균을 빼지 않는다.",
    "② 양측 FFT: np.fft.fft 후 fftshift. 표시 크기 = |FFT| / N.",
    "③ 단측 FFT: DC와 짝수 N의 Nyquist를 제외하고 양측 크기의 2배.",
    "④ IFFT: ②의 복소수 FFT를 np.fft.ifft로 되돌린다.",
    "평균 제거, SRSS, g·mm/s 같은 물리단위 보정은 이 검증에 넣지 않는다.",
    "PASS: 복원된 실수부가 입력과 allclose (rtol 1e-10, atol 1e-8).",
]


def fft_check_unavailable(message):
    return {
        "ok": False,
        "passed": None,
        "title": "FFT 과정 / IFFT 검증",
        "note": FFT_CHECK_NOTE,
        "steps": list(FFT_CHECK_STEPS),
        "message": message,
        "axes": [],
    }


def _decimate_xy(x_values, y_values, limit=160):
    x_data = np.asarray(x_values, dtype=np.float64)
    y_data = np.asarray(y_values, dtype=np.float64)
    count = min(x_data.size, y_data.size)
    if count == 0:
        return [], []
    step = max(1, int(np.ceil(count / float(limit))))
    index = np.arange(0, count, step)
    if int(index[-1]) != count - 1:
        index = np.append(index, count - 1)
    return [float(value) for value in x_data[index]], [float(value) for value in y_data[index]]


def fft_check_many(records, title):
    """복소수 FFT를 그대로 IFFT로 되돌려 입력과 비교한다.

    평균 제거, SRSS, g·mm/s 환산은 넣지 않는다.
    미리보기 점만 줄이고, 판정에 쓰는 오차는 원본 길이로 계산한다.
    """
    axes = []
    for label, samples, fs in records:
        diagnostic = gen.fft_roundtrip_diagnostic(samples, fs)
        two_f, two_y = _decimate_xy(diagnostic["f"], diagnostic["magnitude"])
        one_f, one_y = _decimate_xy(diagnostic["single_f"], diagnostic["single_magnitude"])
        axes.append({
            "label": str(label),
            "passed": bool(diagnostic["passed"]),
            "max_error": float(diagnostic["max_error"]),
            "rms_error": float(diagnostic["rms_error"]),
            "imag_error": float(diagnostic["imag_error"]),
            "n": int(diagnostic["n"]),
            "fs": float(diagnostic["fs"]),
            "text": gen.fft_diagnostic_text(diagnostic, str(label)),
            "two_sided": {"f": two_f, "y": two_y},
            "single_sided": {"f": one_f, "y": one_y},
        })
    if not axes:
        return fft_check_unavailable("선택된 RAW 데이터가 없습니다. 초음파 H는 위상이 없는 스펙트럼입니다.")
    passed = all(item["passed"] for item in axes)
    return {
        "ok": True,
        "passed": passed,
        "title": title,
        "note": FFT_CHECK_NOTE,
        "steps": list(FFT_CHECK_STEPS),
        "message": "IFFT PASS" if passed else "IFFT FAIL",
        "axes": axes,
    }

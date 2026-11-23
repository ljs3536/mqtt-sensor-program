"""ADXL 분석 데이터 내보내기(발생기 v073 '분석 데이터 저장 (CSV)'). 웹은 파일 5개를 ZIP 하나로 내려준다.

쓰는 곳: routes/generator.py `POST /api/generator/velocity-export` ← 속도 RMS 시간 이력 창의 '분석 데이터 저장'.
/generator(CSV)와 /studio(DB) 모두 지금 불러온 ADXL X/Y/Z 세트에서 고른 범위를 쓴다.

계산은 속도 RMS 시간 이력(graph_psd.velocity_history)과 같다. 화면 서랍의 설정을 그대로 쓴다.
  RAW − 평균 → FFT측 필터(HPF/LPF/Notch) → 세트·축마다 Welch PSD(세트끼리 평균하지 않음, 목표 구간 수까지)
  → ×(g/LSB)² → MEMS 감도·주파수 보정 → 가속도 PSD Pa [g²/Hz]
  → 속도 PSD Pv = Pa × (9806.65 / 2πf)², f>0 → vRMS = √(Σ Pv·Δf), SRSS = √(X²+Y²+Z²).
세 축이 모두 계산된 세트만 쓴다(한 축이라도 실패하면 그 세트는 '제외'에 사유와 함께 남는다).

ZIP 안 파일(이름·열은 v073과 같다)
- acceleration_time_history.csv  세트·축·샘플마다 한 줄: 수신 원본 g, PSD에 쓴 필터 출력 g(평균 제거)
- averaged_acceleration_psd.csv  세트·축·주파수마다 한 줄: g²/Hz, 쓴 Welch 구간 수
- averaged_velocity_psd.csv      세트·축·주파수마다 한 줄: (mm/s)²/Hz
- velocity_rms_history.csv       세트마다 한 줄: X/Y/Z 속도 RMS, SRSS, SRSS 한계, 정상/초과, 축별 구간 수
- analysis_settings.json         쓴 설정, 저장/요청 세트 수, 제외 사유, 계산식 설명

크기: 가속도 시간 이력은 세트 × 3축 × 샘플 수 줄이다(예: 50세트 × 3 × 8192 ≈ 120만 줄).
그래서 메모리에 모으지 않고 임시 ZIP 파일에 바로 쓴다(write_zip). 원본 CSV·DB는 바꾸지 않는다.
"""

from __future__ import annotations

import csv
import io
import json
import math
import zipfile

import numpy as np

from web_db.src import core as gen
from web_db.src.graph_options import _filter_kwargs, _num, decode_wave
from web_db.src.graph_psd import G0, _mems_correction, _window, axis_psd, psd_settings

FILES = {
    "acceleration": "acceleration_time_history.csv",
    "acc_psd": "averaged_acceleration_psd.csv",
    "velocity_psd": "averaged_velocity_psd.csv",
    "rms": "velocity_rms_history.csv",
}
HEADERS = {
    "acceleration": ["packet_no", "received_at", "axis", "sample_index", "elapsed_s",
                     "raw_acceleration_g", "fft_filtered_acceleration_g"],
    "acc_psd": ["packet_no", "received_at", "axis", "frequency_hz", "psd_g2_per_hz", "welch_segments_used"],
    "velocity_psd": ["packet_no", "received_at", "axis", "frequency_hz", "psd_mm2_per_s2_per_hz", "welch_segments_used"],
    "rms": ["packet_no", "x_received_at", "x_rms_mm_per_s", "y_rms_mm_per_s", "z_rms_mm_per_s", "srss_mm_per_s",
            "srss_limit_mm_per_s", "status", "x_segments", "y_segments", "z_segments"],
}


def _g(value):
    """CSV 숫자 표기(v073과 같은 유효숫자 12자리)."""
    return f"{float(value):.12g}"


def _axis_result(obj, options, settings, g_per_lsb):
    """한 축 계산. (수신 시각, 원본 g, 필터 g, Fs, 주파수, 가속도 PSD, 속도 PSD, 구간 수, 속도 RMS)."""
    raw, fs = decode_wave("ADXL", obj)
    if raw.size < 2 or not math.isfinite(fs) or fs <= 0:
        raise ValueError("샘플/Fs 오류")
    filtered = gen.spectral_filter(raw - np.mean(raw), fs, **_filter_kwargs(options["adxl"].get("filters"), "FFT"))
    freq, acc, used, _fs, _n = axis_psd("ADXL", obj, options, settings, "g", max_segments=settings["target"])
    if used < 1 or len(freq) < 2:
        raise ValueError("PSD 구간 부족")
    vel = np.zeros_like(acc)
    positive = freq > 0
    vel[positive] = acc[positive] * (G0 / (2 * np.pi * freq[positive])) ** 2
    rms = float(np.sqrt(max(0.0, float(np.sum(vel)) * float(freq[1] - freq[0]))))
    received = str(obj.get("_received_at") or "")
    return received, raw * g_per_lsb, filtered * g_per_lsb, fs, freq, acc, vel, int(used), rms


def write_zip(target, packets, options, start=1, count=None, device=""):
    """고른 범위 세트의 분석 데이터를 target(파일 경로 또는 쓰기 가능한 바이너리 파일)에 ZIP으로 쓴다.

    packets: 불러온 ADXL 세트 목록([{X, Y, Z}]). start: 1부터. count: None이면 끝까지.
    돌려주는 dict: {"good", "requested", "skipped", "start", "end", "files"}. 유효한 세트가 없으면 ValueError.
    """
    settings = psd_settings(options)
    total = len(packets)
    if total == 0:
        raise ValueError("ADXL X/Y/Z 데이터를 먼저 불러와 주세요.")
    start = int(start)
    if not 1 <= start <= total:
        raise ValueError(f"시작 세트는 1~{total} 사이여야 합니다.")
    count = total - start + 1 if count in (None, "") else int(count)
    if count < 1:
        raise ValueError("세트 수는 1 이상이어야 합니다.")
    chosen = list(packets[start - 1:start - 1 + count])
    end = start + len(chosen) - 1
    first = chosen[0] if chosen else {}
    if not device:
        device = str(((first or {}).get("X") or {}).get("dvicMacAddr") or "")
    limit = float(gen.srss_limit_get(device))
    g_per_lsb = _num(options["adxl"].get("g_per_lsb"), gen.read_mems_g_per_lsb())
    if not math.isfinite(g_per_lsb) or not 0 < g_per_lsb <= 10:
        raise ValueError("MEMS g/LSB 설정 오류")
    window, beta = _window("ADXL", options)
    good = 0
    skipped = []
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        # ZipFile은 한 번에 한 항목만 쓸 수 있다. 그래서 세트 계산 결과(숫자 배열)를 먼저 모으고,
        # CSV 글자는 항목마다 생성기로 한 줄씩 만들어 바로 압축한다(큰 CSV 글자를 메모리에 쌓지 않는다).
        # 숫자 배열 크기: 200세트 × 3축 × 8192샘플 × 2(원본·필터) × 8바이트 ≈ 80MB가 최대 수준.
        results = []
        for offset, packet in enumerate(chosen):
            number = start + offset
            if not isinstance(packet, dict) or any(packet.get(a) is None for a in ("X", "Y", "Z")):
                skipped.append(f"{number}: 불완전한 X/Y/Z")
                continue
            if not str(packet["X"].get("_received_at") or ""):
                skipped.append(f"{number}: X축 수신 시각 없음")
                continue
            try:
                # 세 축이 모두 계산된 뒤에만 쓴다(v073과 같음). 샘플 배열은 쓸 때 다시 만들지 않도록 보관한다.
                axes = {axis: _axis_result(packet[axis], options, settings, g_per_lsb) for axis in ("X", "Y", "Z")}
            except (ValueError, TypeError, KeyError, OverflowError, ZeroDivisionError) as exc:
                skipped.append(f"{number}: {exc}")
                continue
            no = str(packet["X"].get("_packet_no") or number)
            results.append((no, str(packet["X"].get("_received_at")), axes))
            good += 1
        if not good:
            raise ValueError("내보낼 유효한 X/Y/Z 세트가 없습니다. " + " / ".join(skipped[:3]))

        def entry(key, rows):
            """ZIP 항목 하나를 UTF-8(BOM, 엑셀용) CSV로 쓴다. rows는 줄을 하나씩 내는 생성기."""
            with archive.open(FILES[key], "w") as raw_stream:
                text = io.TextIOWrapper(raw_stream, encoding="utf-8-sig", newline="")
                writer = csv.writer(text)
                writer.writerow(HEADERS[key])
                writer.writerows(rows)
                text.flush()
                text.detach()

        entry("acceleration", (
            (no, axes[a][0], a, j, f"{j / axes[a][3]:.9g}", _g(r), _g(d))
            for no, _t, axes in results for a in ("X", "Y", "Z")
            for j, (r, d) in enumerate(zip(axes[a][1], axes[a][2]))
        ))
        entry("acc_psd", (
            (no, axes[a][0], a, _g(f), _g(p), axes[a][7])
            for no, _t, axes in results for a in ("X", "Y", "Z")
            for f, p in zip(axes[a][4], axes[a][5])
        ))
        entry("velocity_psd", (
            (no, axes[a][0], a, _g(f), _g(v), axes[a][7])
            for no, _t, axes in results for a in ("X", "Y", "Z")
            for f, v in zip(axes[a][4], axes[a][6])
        ))

        def rms_rows():
            for no, stamp, axes in results:
                values = [axes[a][8] for a in ("X", "Y", "Z")]
                srss = math.sqrt(sum(v * v for v in values))
                yield (no, stamp, *(_g(v) for v in values), _g(srss), _g(limit),
                       "초과" if srss > limit else "정상", *(axes[a][7] for a in ("X", "Y", "Z")))

        entry("rms", rms_rows())
        adxl = options["adxl"]
        info = {
            "data_description": "ADXL 분석 데이터 (세트별 Welch PSD / 속도 RMS) · web_db 내보내기(발생기 v073 형식)",
            "device": device,
            "set_range": f"{start}~{end}",
            "packet_count_exported": good,
            "packet_count_requested": len(chosen),
            "skipped": skipped,
            "psd_segment_n": settings["segment_text"],
            "psd_overlap_percent": settings["overlap"],
            "psd_average_target_segments": settings["target"],
            "psd_window": window,
            "window_beta": beta,
            "fft_filters": _filter_kwargs(adxl.get("filters"), "FFT"),
            "mems_frequency_sensitivity_correction": _mems_correction(options),
            "g_per_lsb": g_per_lsb,
            "srss_limit_mm_per_s": limit,
            "time_reference": "X-axis received_at (수신 시각, 센서 측정 시각 아님)",
            "acceleration_time_history_description":
                "raw_acceleration_g는 물리단위로 환산한 수신 원본; fft_filtered_acceleration_g는 PSD 분석에 사용한 FFT 필터 출력 (DC 제거).",
            "psd_description": "각 세트마다 Welch PSD를 별도 계산 (세트 간 평균하지 않음). 감도/주파수 보정을 반영한 단측 PSD.",
            "velocity_conversion": "Pv[(mm/s)^2/Hz] = Pa[g^2/Hz] * (9806.65 / (2*pi*f))^2, f>0",
            "rms_calculation": "vRMS[mm/s] = sqrt(sum(Pv * delta_f)); SRSS=sqrt(X^2+Y^2+Z^2)",
        }
        archive.writestr("analysis_settings.json", json.dumps(info, ensure_ascii=False, indent=2, default=str))
    return {
        "good": good,
        "requested": len(chosen),
        "skipped": skipped,
        "start": start,
        "end": end,
        "files": list(FILES.values()) + ["analysis_settings.json"],
    }

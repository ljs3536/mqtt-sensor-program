import argparse
import json
import math
import os
import sys
import warnings

warnings.filterwarnings("ignore")

import numpy as np
import torch

torch.set_num_threads(1)

from .diag_spectrum import fft_magnitude, normalize_window
from .model_vibe_autoencoder import AutoEncoder
from .model_vibe_cnn import Classifier
from .update_vibe_SSAD_OW_ProtoNet import parse_signal_text


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdin", action="store_true")
    return parser.parse_args()


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def normalize_state_dict(state_dict):
    normalized = {}
    for key, value in state_dict.items():
        if key.startswith("model."):
            normalized[key[len("model."):]] = value
        else:
            normalized[key] = value
    return normalized


def number_or_none(value):
    if value in ("", "null", None):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or number <= 0:
        return None
    return number


# 스펙트럼 중앙값 대비 이 배 이상이고, 그 축의 최대 피크 대비 이 비율 이상이면 두드러진 피크로 본다.
PROMINENCE = 4.0
DOMINANCE = 0.35
MATCH_RATIO = 0.08


def fft_spectrum(signal, sample_rate):
    centered = signal.astype(np.float64) - float(np.mean(signal))
    window = np.hanning(len(centered))
    mag = np.abs(np.fft.rfft(centered * window))
    if mag.size > 1:
        mag[0] = 0.0
    freqs = np.fft.rfftfreq(len(centered), d=1.0 / float(sample_rate))
    return freqs, mag


def band_peak(freqs, mag, target_hz):
    if target_hz <= 0 or mag.size < 3:
        return None
    df = float(freqs[1] - freqs[0]) if freqs.size > 1 else 0.5
    half = max(target_hz * MATCH_RATIO, df * 1.5)
    indexes = np.where(np.abs(freqs - target_hz) <= half)[0]
    if indexes.size == 0:
        return None
    local = int(indexes[int(np.argmax(mag[indexes]))])
    return float(freqs[local]), float(mag[local])


def count_or_none(value):
    number = number_or_none(value)
    if number is None:
        return None
    return int(round(number))


def evaluate_band(freqs, mag, baseline, peak_max, target_hz, code, label, axis, dominance=DOMINANCE):
    found = band_peak(freqs, mag, target_hz)
    measured = None
    level = 0.0
    ratio = 0.0
    if found is not None and baseline > 0:
        measured, level = found
        ratio = level / baseline
    hit = baseline > 0 and peak_max > 0 and level >= PROMINENCE * baseline and level >= dominance * peak_max
    return {
        "code": code,
        "label": label,
        "targetHz": round(float(target_hz), 2),
        "measuredHz": None if measured is None else round(measured, 2),
        "ratio": round(ratio, 2),
        "hit": bool(hit),
        "axis": axis,
        "level": level,
    }


def top_peaks(freqs, mag, limit=5):
    peaks = []
    for index in range(1, mag.size - 1):
        if mag[index] >= mag[index - 1] and mag[index] > mag[index + 1]:
            peaks.append((float(mag[index]), float(freqs[index])))
    peaks.sort(reverse=True)
    return [{"hz": round(hz, 1), "level": round(level, 4)} for level, hz in peaks[:limit]]


def peak_hint(hz, targets):
    best = None
    for code, target in targets:
        if target <= 0:
            continue
        gap = abs(hz - target) / target
        if gap <= MATCH_RATIO and (best is None or gap < best[0]):
            best = (gap, code)
    return best[1] if best else ""


def order_report(scored, sample_rate, payload, is_anomaly):
    rpm = number_or_none(payload.get("operRpm")) or number_or_none(payload.get("ratedRpm"))
    blade_cnt = count_or_none(payload.get("bladeCnt"))
    gear_teeth = count_or_none(payload.get("gearTeeth"))
    ball_cnt = count_or_none(payload.get("ballCnt"))
    line_hz = number_or_none(payload.get("lineHz"))
    running_hz = None if rpm is None else rpm / 60.0

    best_by_code = {}
    peak_source = None
    if running_hz is not None and sample_rate > 0:
        for _score, axis, signal in scored:
            if signal.size < 8:
                continue
            freqs, mag = fft_spectrum(signal, sample_rate)
            positive = mag[mag > 0]
            baseline = float(np.median(positive)) if positive.size else 0.0
            peak_max = float(np.max(mag)) if mag.size else 0.0
            if peak_source is None:
                peak_source = (freqs, mag, axis)
            bands = [
                evaluate_band(freqs, mag, baseline, peak_max, running_hz, "1X", "1X 회전", axis),
                evaluate_band(freqs, mag, baseline, peak_max, running_hz * 2, "2X", "2X 회전", axis),
                evaluate_band(freqs, mag, baseline, peak_max, running_hz * 3, "3X", "3X 회전", axis),
            ]
            if gear_teeth:
                gmf = running_hz * gear_teeth
                bands.append(evaluate_band(freqs, mag, baseline, peak_max, gmf, "GMF", "기어맞물림", axis))
                bands.append(evaluate_band(freqs, mag, baseline, peak_max, gmf - running_hz, "GMF-1X", "GMF 하측 사이드밴드", axis, 0.08))
                bands.append(evaluate_band(freqs, mag, baseline, peak_max, gmf + running_hz, "GMF+1X", "GMF 상측 사이드밴드", axis, 0.08))
            if blade_cnt:
                bands.append(evaluate_band(freqs, mag, baseline, peak_max, running_hz * blade_cnt, "BPF", "날개통과", axis))
            if ball_cnt:
                bands.append(evaluate_band(freqs, mag, baseline, peak_max, 0.4 * ball_cnt * running_hz, "BPFO", "베어링 외륜", axis))
                bands.append(evaluate_band(freqs, mag, baseline, peak_max, 0.6 * ball_cnt * running_hz, "BPFI", "베어링 내륜", axis))
            if line_hz:
                bands.append(evaluate_band(freqs, mag, baseline, peak_max, line_hz, "LINE", "전원 주파수", axis))
                bands.append(evaluate_band(freqs, mag, baseline, peak_max, line_hz * 2, "LINE2", "전원 주파수 2배", axis))
            for band in bands:
                current = best_by_code.get(band["code"])
                if current is None or band["ratio"] > current["ratio"]:
                    best_by_code[band["code"]] = band
                    if band["hit"]:
                        peak_source = (freqs, mag, axis)

    orders = []
    for code in ("1X", "2X", "3X", "GMF", "GMF-1X", "GMF+1X", "BPF", "BPFO", "BPFI", "LINE", "LINE2"):
        band = best_by_code.get(code)
        if band is None:
            continue
        orders.append({key: band[key] for key in ("code", "label", "targetHz", "measuredHz", "ratio", "hit", "axis")})

    candidates = []
    one = best_by_code.get("1X")
    two = best_by_code.get("2X")
    three = best_by_code.get("3X")
    if one and one["hit"]:
        two_level = two["level"] if two else 0.0
        if one["level"] >= two_level:
            candidates.append((one["ratio"], "회전체 불균형(Unbalance) 의심 (%.1fHz)" % one["targetHz"], one))
    harmonic = None
    if two and two["hit"]:
        harmonic = two
    if three and three["hit"] and (harmonic is None or three["ratio"] > harmonic["ratio"]):
        harmonic = three
    if harmonic is not None:
        one_level = one["level"] if one else 0.0
        if one_level <= 0 or harmonic["level"] >= one_level * 0.7:
            candidates.append((harmonic["ratio"], "모터 축 정렬 불량(Misalignment) 의심 (%.1fHz)" % harmonic["targetHz"], harmonic))
    gmf = best_by_code.get("GMF")
    if gmf and gmf["hit"]:
        side_lo = best_by_code.get("GMF-1X")
        side_hi = best_by_code.get("GMF+1X")
        side_note = ""
        if side_lo and side_hi and side_lo["hit"] and side_hi["hit"]:
            side_note = ", 사이드밴드 %.1f/%.1fHz" % (side_lo["targetHz"], side_hi["targetHz"])
        candidates.append((gmf["ratio"], "기어 톱니 손상 의심 (%.1fHz 피크 발생%s)" % (gmf["targetHz"], side_note), gmf))
    bpf = best_by_code.get("BPF")
    if bpf and bpf["hit"]:
        candidates.append((bpf["ratio"], "팬 날개 불균형 의심 (%.1fHz 피크 발생)" % bpf["targetHz"], bpf))
    for code, title in (("BPFO", "베어링 외륜 결함(BPFO) 의심"), ("BPFI", "베어링 내륜 결함(BPFI) 의심")):
        band = best_by_code.get(code)
        if band and band["hit"]:
            candidates.append((band["ratio"], "%s (%.1fHz)" % (title, band["targetHz"]), band))
    for code, title in (("LINE", "전원 주파수 이상 의심"), ("LINE2", "전원 주파수 2배 이상 의심")):
        band = best_by_code.get(code)
        if band and band["hit"]:
            candidates.append((band["ratio"], "%s (%.1fHz)" % (title, band["targetHz"]), band))

    if is_anomaly != "Y":
        fault_reason = "정상"
    elif running_hz is None:
        fault_reason = "재원 없음 (AI 패턴 이상)"
    elif candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        fault_reason = candidates[0][1]
        winner = candidates[0][2]
        if peak_source is None or winner.get("axis") != peak_source[2]:
            for _score, axis, signal in scored:
                if axis == winner.get("axis") and signal.size >= 8:
                    peak_source = fft_spectrum(signal, sample_rate) + (axis,)
                    break
    else:
        fault_reason = "원인 불명 (AI 패턴 이상)"

    peaks = []
    if peak_source is not None:
        freqs, mag, _axis = peak_source
        targets = [(band["code"], band["targetHz"]) for band in orders]
        peaks = top_peaks(freqs, mag)
        for peak in peaks:
            peak["hint"] = peak_hint(peak["hz"], targets)
    return fault_reason, orders, peaks, None if running_hz is None else round(running_hz, 3)


def standardize_signal(signal):
    std = float(signal.std())
    if std < 1e-6:
        std = 1e-6
    return ((signal - float(signal.mean())) / std).astype(np.float32)


def prepare_signal(signal):
    if signal.size < 64:
        raise ValueError("센서 파형이 너무 짧아 진단모델에 넣을 수 없습니다. 길이 %d" % signal.size)
    return signal


def signal_items(payload):
    parsed = []
    signals = payload.get("signals")
    if isinstance(signals, list):
        for item in signals:
            if not isinstance(item, dict):
                continue
            signal = parse_signal_text(item.get("sensorData", ""))
            signal = prepare_signal(signal) if signal.size else signal
            if signal.size == 0:
                continue
            parsed.append((str(item.get("axis") or ""), signal))
    if parsed:
        return parsed
    signal = parse_signal_text(payload.get("sensorData", ""))
    if signal.size == 0:
        return []
    return [("", prepare_signal(signal))]


def checkpoint_bundle(model_path):
    loaded = torch.load(model_path, map_location="cpu")
    if isinstance(loaded, dict) and "state_dict" in loaded:
        return loaded
    return {"state_dict": loaded}


def saved_window(bundle):
    window = bundle.get("fftWindow") if isinstance(bundle, dict) else None
    if window in ("", None):
        return None
    return normalize_window(window)


def model_features(signal, window_type):
    if not window_type:
        return np.asarray(signal, dtype=np.float32)
    return fft_magnitude(signal, window_type)


def load_cnn(payload):
    model_path = payload["modelPath"]
    proto_path = payload.get("protoPath")
    if not proto_path or not os.path.exists(proto_path):
        raise FileNotFoundError("CNN prototype 파일이 없습니다.")
    bundle = checkpoint_bundle(model_path)
    state_dict = bundle["state_dict"]
    n_filters = int(bundle.get("n_filters", payload.get("nFilters", 20))) if isinstance(bundle, dict) else int(payload.get("nFilters", 20))
    model = Classifier(n_filters=n_filters)
    model.load_state_dict(normalize_state_dict(state_dict), strict=False)
    model.eval()
    prototype = torch.from_numpy(np.load(proto_path).astype(np.float32)).float()
    return model, prototype, saved_window(bundle)


def cnn_distance(model, prototype, signal, window_type):
    features = model_features(signal, window_type)
    with torch.no_grad():
        emb = model(torch.from_numpy(features[None, :]).float()).squeeze(0)
        proto = prototype.squeeze(0) if prototype.ndim == 2 else prototype
        return float(torch.norm(emb - proto).item())


def score_cnn(payload, signal):
    model, prototype, window_type = load_cnn(payload)
    return cnn_distance(model, prototype, signal, window_type)


def load_ae(payload):
    bundle = checkpoint_bundle(payload["modelPath"])
    n_filters = int(bundle.get("n_filters", payload.get("nFilters", 20)))
    model = AutoEncoder(n_filters=n_filters)
    model.load_state_dict(bundle["state_dict"])
    model.eval()
    return model, saved_window(bundle)


def ae_distance(model, signal, window_type):
    signal = model_features(signal, window_type)
    signal = standardize_signal(signal)
    with torch.no_grad():
        x = torch.from_numpy(signal[None, :]).float()
        recon, _ = model(x)
        if recon.shape[-1] != x.shape[-1]:
            recon = torch.nn.functional.interpolate(
                recon.unsqueeze(1), size=x.shape[-1], mode="linear", align_corners=False
            ).squeeze(1)
        return float(torch.mean((recon - x) ** 2).item())


def score_ae(payload, signal):
    model, window_type = load_ae(payload)
    return ae_distance(model, signal, window_type)


def load_hoko(payload):
    from .model_vibe_hoko import HokoAnomaly

    proto_path = payload.get("protoPath")
    if not proto_path or not os.path.exists(proto_path):
        raise FileNotFoundError("HOKO prototype 파일이 없습니다.")
    bundle = torch.load(payload["modelPath"], map_location="cpu")
    state_dict = bundle["state_dict"] if isinstance(bundle, dict) and "state_dict" in bundle else bundle
    model = HokoAnomaly()
    model.load_state_dict(state_dict)
    model.eval()
    prototype = torch.from_numpy(np.load(proto_path).astype(np.float32)).float()
    return model, prototype


def hoko_distance(model, prototype, signal):
    signal = standardize_signal(signal)
    with torch.no_grad():
        wave = torch.from_numpy(signal[None, :]).float()
        return float(model.score(wave, prototype).item())


def main():
    args = parse_args()
    if not args.stdin:
        raise ValueError("This script requires --stdin JSON input.")

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            payload = json.loads(line)
            model_type = str(payload.get("modelType", "")).upper()
            model_path = payload.get("modelPath")
            if model_type not in ("CNN", "AE", "HOKO"):
                raise ValueError("modelType은 CNN, AE, HOKO 입니다.")
            if not model_path or not os.path.exists(model_path):
                raise FileNotFoundError("진단 모델 파일이 없습니다.")

            items = signal_items(payload)
            if not items:
                raise ValueError("센서 데이터가 없습니다.")
            fft_window = None
            if model_type == "CNN":
                model, prototype, fft_window = load_cnn(payload)
                scored = [(cnn_distance(model, prototype, signal, fft_window), axis, signal) for axis, signal in items]
            elif model_type == "HOKO":
                model, prototype = load_hoko(payload)
                scored = [(hoko_distance(model, prototype, signal), axis, signal) for axis, signal in items]
            else:
                model, fft_window = load_ae(payload)
                scored = [(ae_distance(model, signal, fft_window), axis, signal) for axis, signal in items]
            score, worst_axis, worst_signal = max(scored, key=lambda item: item[0])
            if math.isnan(score) or math.isinf(score):
                score = 0.0

            threshold = payload.get("threshold")
            if threshold in ("", "null", None):
                threshold = 1.0
            threshold = float(threshold)
            is_anomaly = "Y" if score > threshold else "N"
            if threshold > 0 and is_anomaly == "Y":
                prob = min(0.99, score / (threshold * 2.0))
            elif threshold > 0:
                prob = max(0.01, (score / threshold) * 0.49)
            else:
                prob = 0.0

            axis = str(payload.get("axis") or "")
            sample_rate = number_or_none(payload.get("sampleRate"))
            if sample_rate is None:
                sample_rate = 25600.0 if axis == "PIEZO" else 3200.0
            fault_reason, orders, peaks, running_hz = order_report(scored, sample_rate, payload, is_anomaly)
            axis_scores = [{"axis": item_axis, "score": round(item_score, 6)} for item_score, item_axis, _signal in scored if item_axis]

            emit({
                "ok": True,
                "modelType": model_type,
                "score": score,
                "is_anomaly": is_anomaly,
                "prob": round(prob, 4),
                "threshold": threshold,
                "fault_reason": fault_reason,
                "runningHz": running_hz,
                "orders": orders,
                "peaks": peaks,
                "worstAxis": worst_axis,
                "axisScores": axis_scores,
                "fftWindow": fft_window,
                "specReady": running_hz is not None,
            })
        except Exception as exc:
            emit({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    main()

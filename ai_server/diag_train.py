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

import lightning.pytorch as pl

from .diag_spectrum import fft_magnitudes, normalize_window
from .model_vibe_autoencoder import AutoEncoder
from .update_vibe_SSAD_OW_ProtoNet import BaseTrainer, parse_signal_text


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdin", action="store_true")
    return parser.parse_args()


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def parse_signals(sensor_data_list):
    signals = [parse_signal_text(text) for text in sensor_data_list if str(text).strip()]
    if not signals:
        raise ValueError("학습할 센서 데이터가 없습니다.")
    lengths = {len(signal) for signal in signals}
    if len(lengths) != 1:
        raise ValueError("선택한 센서 데이터 길이가 서로 다릅니다.")
    return np.asarray(signals, dtype=np.float32)


def threshold_from_scores(scores):
    valid_scores = [float(score) for score in scores if not (math.isnan(score) or math.isinf(score))]
    if not valid_scores:
        valid_scores = [0.0]
    mean_dist = float(np.mean(valid_scores))
    std_dist = float(np.std(valid_scores))
    max_dist = float(np.max(valid_scores))
    p999_dist = float(np.percentile(valid_scores, 99.9))
    calc_threshold = p999_dist * 1.5
    if calc_threshold < max_dist * 1.3:
        calc_threshold = max_dist * 1.3
    if calc_threshold < mean_dist + (6.0 * std_dist):
        calc_threshold = mean_dist + (6.0 * std_dist)
    if calc_threshold < 0.05:
        calc_threshold = 0.05
    return calc_threshold, mean_dist, std_dist


class AETrainer(pl.LightningModule):
    def __init__(self, n_filters, lr, batch_size, normal_signals):
        super().__init__()
        self.model = AutoEncoder(n_filters=n_filters)
        self.lr = lr
        self.batch_size = batch_size
        self.normal_signals = torch.from_numpy(normal_signals).float()
        self.loss_fn = torch.nn.MSELoss()

    def train_dataloader(self):
        dataset = torch.utils.data.TensorDataset(self.normal_signals)
        return torch.utils.data.DataLoader(dataset, batch_size=self.batch_size, shuffle=True)

    def configure_optimizers(self):
        return torch.optim.AdamW(self.model.parameters(), lr=self.lr)

    def training_step(self, batch, idx):
        x = batch[0]
        recon, _ = self.model(x)
        return self.loss_fn(recon, x)


def standardize_rows(signals):
    mean = signals.mean(axis=1, keepdims=True)
    std = np.maximum(signals.std(axis=1, keepdims=True), 1e-6)
    return ((signals - mean) / std).astype(np.float32)


def match_length(recon, target_len):
    if recon.shape[-1] == target_len:
        return recon
    return torch.nn.functional.interpolate(
        recon.unsqueeze(1), size=target_len, mode="linear", align_corners=False
    ).squeeze(1)


def train_ae(signals, payload, ckpt_path):
    signals = standardize_rows(signals)
    n_filters = int(payload.get("nFilters", 20))
    epochs = int(payload.get("epochs", 40))
    lr = float(payload.get("learningRate", 1e-4))
    batch_size = max(1, min(int(payload.get("batchSize", 4)), len(signals)))
    model = AETrainer(n_filters, lr, batch_size, signals)
    trainer = pl.Trainer(
        logger=False,
        max_epochs=epochs,
        accelerator="cpu",
        enable_checkpointing=False,
        enable_progress_bar=False,
        enable_model_summary=False,
    )
    trainer.fit(model)
    model.eval()
    with torch.no_grad():
        x = torch.from_numpy(signals).float()
        recon, _ = model.model(x)
        recon = match_length(recon, x.shape[-1])
        mse_scores = torch.mean((recon - x) ** 2, dim=1).numpy()
    threshold, mean_dist, std_dist = threshold_from_scores(mse_scores.tolist())
    torch.save(
        {
            "state_dict": model.model.state_dict(),
            "n_filters": n_filters,
            "modelType": "AE",
            "fftWindow": normalize_window(payload.get("fftWindow")),
        },
        ckpt_path,
    )
    return threshold, mean_dist, std_dist, None


def train_cnn(signals, payload, ckpt_path, proto_path):
    n_filters = int(payload.get("nFilters", 20))
    epochs = int(payload.get("epochs", 50))
    lr = float(payload.get("learningRate", 5.0e-05))
    n_support = int(payload.get("nSupport", 5))
    n_query = int(payload.get("nQuery", 8))
    required = n_support + n_query
    if len(signals) < required:
        repeat_count = (required // len(signals)) + 1
        signals = np.tile(signals, (repeat_count, 1))
    model = BaseTrainer(
        n_filters=n_filters,
        n_support=n_support,
        n_query=n_query,
        n=7,
        m=6,
        lr=lr,
        normal_signals=signals,
    )
    trainer = pl.Trainer(
        logger=False,
        max_epochs=epochs,
        accelerator="cpu",
        devices=1,
        enable_checkpointing=False,
        enable_progress_bar=False,
        enable_model_summary=False,
    )
    trainer.fit(model)
    model.eval()
    with torch.no_grad():
        emb = model.model(torch.from_numpy(signals).float())
        prototype = emb.mean(0).detach().cpu()
        if prototype.ndim == 1:
            prototype = prototype.unsqueeze(0)
        dist = torch.norm(emb.cpu() - prototype, dim=1).numpy()
    threshold, mean_dist, std_dist = threshold_from_scores(dist.tolist())
    torch.save(
        {
            "state_dict": model.state_dict(),
            "n_filters": n_filters,
            "location": payload.get("location"),
            "axis": payload.get("axis"),
            "modelType": "CNN",
            "fftWindow": normalize_window(payload.get("fftWindow")),
        },
        ckpt_path,
    )
    np.save(proto_path, prototype.numpy())
    return threshold, mean_dist, std_dist, proto_path


def train_hoko(signals, payload, ckpt_path, proto_path):
    from .model_vibe_hoko import HokoAnomaly

    signals = standardize_rows(signals)
    epochs = int(payload.get("epochs", 20))
    lr = float(payload.get("learningRate", 1e-4))
    batch_size = max(1, min(int(payload.get("batchSize") or 4), len(signals)))
    model = HokoAnomaly()
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    data = torch.from_numpy(signals).float()
    model.train()
    for _epoch in range(max(1, epochs)):
        order = torch.randperm(len(data))
        for start in range(0, len(data), batch_size):
            batch = data[order[start:start + batch_size]]
            optimizer.zero_grad()
            loss = model.forecast_loss(batch)
            loss.backward()
            optimizer.step()
    model.eval()
    reps = []
    with torch.no_grad():
        for start in range(0, len(data), batch_size):
            reps.append(model.representation(data[start:start + batch_size]))
        emb = torch.cat(reps, dim=0)
        prototype = emb.mean(0, keepdim=True)
        dist = torch.norm(emb - prototype, dim=1).numpy()
    threshold, mean_dist, std_dist = threshold_from_scores(dist.tolist())
    torch.save({"state_dict": model.state_dict(), "modelType": "HOKO"}, ckpt_path)
    np.save(proto_path, prototype.detach().cpu().numpy())
    return threshold, mean_dist, std_dist, proto_path


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
            if model_type not in ("CNN", "AE", "HOKO"):
                raise ValueError("modelType은 CNN, AE, HOKO 입니다.")

            save_url = payload.get("saveUrl")
            file_prefix = payload.get("filePrefix")
            if not save_url or not file_prefix:
                raise ValueError("saveUrl과 filePrefix가 필요합니다.")
            os.makedirs(save_url, exist_ok=True)

            signals = parse_signals(payload.get("sensorData") or [])
            if model_type in ("CNN", "AE"):
                payload["fftWindow"] = normalize_window(payload.get("fftWindow"))
                signals = fft_magnitudes(signals, payload["fftWindow"])
            ckpt_path = os.path.join(save_url, file_prefix + ".ckpt")
            proto_path = os.path.join(save_url, file_prefix + "_prototype.npy")

            if model_type == "AE":
                threshold, mean_dist, std_dist, proto_path = train_ae(signals, payload, ckpt_path)
            elif model_type == "HOKO":
                threshold, mean_dist, std_dist, proto_path = train_hoko(signals, payload, ckpt_path, proto_path)
            else:
                threshold, mean_dist, std_dist, proto_path = train_cnn(signals, payload, ckpt_path, proto_path)

            emit({
                "ok": True,
                "modelType": model_type,
                "modelPath": ckpt_path,
                "protoPath": proto_path,
                "threshold": threshold,
                "meanDist": mean_dist,
                "stdDist": std_dist,
                "sampleCnt": int(len(signals)),
            })
        except Exception as exc:
            emit({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    main()

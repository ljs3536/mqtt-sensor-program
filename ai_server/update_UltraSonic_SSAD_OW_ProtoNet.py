import argparse
import os
import json
import sys
import warnings
import logging

warnings.filterwarnings("ignore")
logging.getLogger("lightning.pytorch").setLevel(logging.ERROR)
logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)

import numpy as np
import torch
import lightning.pytorch as pl
import random
import math

random.seed(42)
np.random.seed(42)
torch.manual_seed(42)

try:
    from .dataset import LeakDataset, KAERIBatchSampler
    from .loss import euclidean_dist
    from .model_ultrasonic_cnn import Classifier
    from . import taug
except ImportError:
    from dataset import LeakDataset, KAERIBatchSampler
    from loss import euclidean_dist
    from model_ultrasonic_cnn import Classifier
    import taug

def augment_list():
    return [
        (taug.flip, 0., 1.0), (taug.jitter, 0., 0.24), (taug.scailing, 0., 0.8),
        (taug.magnitude_warp, 0., 1.6), (taug.time_warp, 0., 1.6),
        (taug.window_slice, 0., 0.8), (taug.window_warp, 0., 0.8),
    ]

class RandAugment:
    def __init__(self, n, m):
        self.n = n
        self.m = m
        self.augment_list = augment_list()

    def __call__(self, img):
        ops = random.choices(self.augment_list, k=self.n)
        for op, minval, maxval in ops:
            val = (float(self.m) / 10) * float(maxval - minval) + minval
            img = op(img, val)
        return img

def parse_signal_text(text: str) -> np.ndarray:
    parts = [p.strip() for p in str(text).split('|') if p.strip() != '']
    return np.asarray([float(p) for p in parts], dtype=np.float32)


class BaseTrainer(pl.LightningModule):
    def __init__(self, n_filters, n_support, n_query, n, m, lr, normal_signals):
        super().__init__()
        self.save_hyperparameters(ignore=['normal_signals'])
        self.n_filters = n_filters
        self.n_support = n_support
        self.n_query = n_query
        self.lr = lr
        
        self.model = Classifier(n_filters=self.n_filters)
        self.transforms = RandAugment(n=n, m=m)
        self.normal_signals = np.asarray(normal_signals, dtype=np.float32)

    def train_dataloader(self):
        normal_data = self.normal_signals
        labels = np.array([1] * len(normal_data), dtype=int)
        labels = np.concatenate([labels, -labels])
        leak_data = normal_data.copy()
        data = np.concatenate([normal_data, leak_data])

        dataset = LeakDataset(data=data, labels=labels, transforms=self.transforms, normalize=False)
        sampler = KAERIBatchSampler(
            labels=labels, n_support=self.n_support, n_query=self.n_query, n_iters=20
        )
        return torch.utils.data.DataLoader(dataset, num_workers=0, batch_sampler=sampler, pin_memory=False)

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.model.parameters(), self.lr)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(optimizer=optimizer, T_0=25)
        return [optimizer], [scheduler]

    def training_step(self, data, idx):
        signals, labels = data['data'], data['label']
        logits = self.model(signals)
        support_logits = logits[:self.n_support]
        prototypes = support_logits.mean(0)
        query_logits = logits[self.n_support:]
        dists = euclidean_dist(query_logits, prototypes)
        query_labels = torch.LongTensor([0] * self.n_query + [1] * self.n_query).to(signals.device)
        loss = (dists ** (1 - 2 * query_labels)).sum()
        return loss

def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            break

        try:
            payload = json.loads(line)
            location = payload['location']
            sensor_data_list = payload['sensorData']
            save_url = payload.get('saveUrl', r"C:/LDMS/python_env/data")

            os.makedirs(save_url, exist_ok=True)
            ckpt_path = os.path.join(save_url, f'{location}.ckpt')
            proto_path = os.path.join(save_url, f'{location}_prototype.npy')

            normal_signals = np.asarray([parse_signal_text(x) for x in sensor_data_list], dtype=np.float32)
            
            n_support = 5
            n_query = 8
            required_samples = n_support + n_query
            if len(normal_signals) < required_samples:
                repeat_count = (required_samples // len(normal_signals)) + 1
                normal_signals = np.tile(normal_signals, (repeat_count, 1))

            # 1. 모델 학습
            model = BaseTrainer(n_filters=20, n_support=n_support, n_query=n_query, n=7, m=6, lr=5.0e-05, normal_signals=normal_signals)
            trainer = pl.Trainer(logger=False, max_epochs=20, accelerator='cpu', devices=1, deterministic=True, enable_checkpointing=False, enable_progress_bar=False, enable_model_summary=False)
            trainer.fit(model)

            # 2. 모델 평가 및 Score 추출 (기존 test_train 스크립트 병합)
            model.eval()
            with torch.no_grad():
                x = torch.from_numpy(normal_signals).float()
                emb = model.model(x)
                prototype = emb.mean(0).detach().cpu()
                
                if prototype.ndim == 1:
                    prototype = prototype.unsqueeze(0)
                
                # 거리(Score) 계산
                dist = torch.norm(emb.cpu() - prototype, dim=1)
                scores = dist.numpy().tolist()

            valid_scores = [float(s) for s in scores if not (math.isnan(s) or math.isinf(s))]
            if not valid_scores:
                valid_scores = [0.0]

            torch.save({'state_dict': model.state_dict(), 'n_filters': 20, 'location': location}, ckpt_path)
            np.save(proto_path, prototype.numpy())

            # 3. 자바로 결과 리턴 (원본 scores 대신 valid_scores 사용)
            result = {
                'ok': True,
                'location': location,
                'count': len(valid_scores),
                'scores': valid_scores,
                'min': float(np.min(valid_scores)),
                'max': float(np.max(valid_scores)),
                'mean': float(np.mean(valid_scores))
            }
            
            print(json.dumps(result, ensure_ascii=False))
            sys.stdout.flush()

        except Exception as e:
            print(json.dumps({'ok': False, 'error': str(e)}))
            sys.stdout.flush()

if __name__ == '__main__':
    main()
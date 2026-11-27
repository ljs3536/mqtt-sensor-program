import argparse
import os
import json
import sys
import warnings
import logging
import math

warnings.filterwarnings("ignore")
logging.getLogger("lightning.pytorch").setLevel(logging.ERROR)
logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)

import numpy as np
import torch
import lightning.pytorch as pl
import random

random.seed(42)
np.random.seed(42)
torch.manual_seed(42)

try:
    from .dataset import LeakDataset, KAERIBatchSampler
    from .loss import euclidean_dist
    from .model_vibe_cnn import Classifier
    from . import taug
except ImportError:
    from dataset import LeakDataset, KAERIBatchSampler
    from loss import euclidean_dist
    from model_vibe_cnn import Classifier
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
    parts = [p.strip() for p in text.split('|') if p.strip() != '']
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
        sampler = KAERIBatchSampler(labels=labels, n_support=self.n_support, n_query=self.n_query, n_iters=20)
        return torch.utils.data.DataLoader(dataset, num_workers=0, batch_sampler=sampler, pin_memory=False)

    def configure_optimizers(self):
        # 이제 self.lr을 정상적으로 찾을 수 있습니다!
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
        return (dists ** (1 - 2 * query_labels)).sum()

def main():
    while True:
        line = sys.stdin.readline()
        if not line: break

        try:
            payload = json.loads(line)
            location = payload['location']
            axis = payload.get('axis', 'PIEZO')
            sensor_data_list = payload['sensorData']
            save_url = payload.get('saveUrl', r"C:/LDMS/python_env/data")

            os.makedirs(save_url, exist_ok=True)
            model_prefix = f"{location}_{axis}"
            ckpt_path = os.path.join(save_url, f'{model_prefix}.ckpt')
            proto_path = os.path.join(save_url, f'{model_prefix}_prototype.npy')

            normal_signals = np.asarray([parse_signal_text(x) for x in sensor_data_list], dtype=np.float32)
            n_support = 5
            n_query = 8
            required_samples = n_support + n_query
            if len(normal_signals) < required_samples:
                repeat_count = (required_samples // len(normal_signals)) + 1
                normal_signals = np.tile(normal_signals, (repeat_count, 1))

            # 1. 모델 학습 진행
            model = BaseTrainer(n_filters=20, n_support=n_support, n_query=n_query, n=7, m=6, lr=5.0e-05, normal_signals=normal_signals)
            trainer = pl.Trainer(logger=False, max_epochs=50, accelerator='cpu', devices=1, deterministic=True, enable_checkpointing=False, enable_progress_bar=False, enable_model_summary=False)
            trainer.fit(model)

            # 2. 모델 평가 및 Score 추출
            model.eval()
            with torch.no_grad():
                x = torch.from_numpy(normal_signals).float()
                emb = model.model(x)
                prototype = emb.mean(0).detach().cpu()
                if prototype.ndim == 1:
                    prototype = prototype.unsqueeze(0)
                
                dist = torch.norm(emb.cpu() - prototype, dim=1).numpy()
                scores = dist.tolist()

            valid_scores = [float(s) for s in scores if not (math.isnan(s) or math.isinf(s))]
            if not valid_scores:
                valid_scores = [0.0]

            # valid_scores로 기준점(통계)을 계산
            mean_dist = float(np.mean(valid_scores))
            std_dist = float(np.std(valid_scores))
            max_dist = float(np.max(valid_scores))
            
            # 다중 모드(Multi-Mode)를 고려한 백분위수(Percentile) 기반 기준점
            # 정규분포(평균+Sigma)의 한계를 극복하기 위해 정상 데이터의 상위 99.9% 지점을 추출
            p999_dist = float(np.percentile(valid_scores, 99.9))
            
            # 현장에서는 학습 기간에 수집되지 않은 미세한 변동이 늘 발생하므로, 
            # 99.9% 지점에 안전 마진(Safety Margin) 30%를 부여하여 기본 임계치 설정
            calc_threshold = p999_dist * 1.3
            
            # 오탐 방지를 위한 3중 방어막 (Max, Sigma, Min-cap)
            # p999 기반 임계치가 튀는 데이터(max_dist)를 다 덮지 못하면, 최대값의 1.3배로 넓혀줌
            if calc_threshold < max_dist * 1.3:
                calc_threshold = max_dist * 1.3
                
            # 만약 데이터의 변동성이 너무 커서 6-Sigma 값이 더 넉넉하다면, 더 넓은 쪽(안전한 쪽)을 채택
            sigma_threshold = mean_dist + (6.0 * std_dist)
            if calc_threshold < sigma_threshold:
                calc_threshold = sigma_threshold

            # 센서 자체의 화이트 노이즈나 기구적 미세 떨림을 무시하기 위한 절대 하한선 (0.05 보장)
            if calc_threshold < 0.05: 
                calc_threshold = 0.05

            torch.save({'state_dict': model.state_dict(), 'n_filters': 20, 'location': location, 'axis': axis}, ckpt_path)
            np.save(proto_path, prototype.numpy())

            _min = float(np.min(valid_scores))
            _max = float(np.max(valid_scores))
            _mean = float(np.mean(valid_scores))

            result = {
                'ok': True,
                'location': location,
                'axis': axis,
                'threshold': calc_threshold,
                'meanDist': mean_dist,
                'stdDist': std_dist,
                'scores': valid_scores,
                'min': _min,
                'max': _max,
                'mean': _mean
            }
            
            print(json.dumps(result, ensure_ascii=False))
            sys.stdout.flush()

        except Exception as e:
            print(json.dumps({'ok': False, 'error': str(e)}))
            sys.stdout.flush()

if __name__ == '__main__':
    main()
import argparse
import os
import json
import sys

import numpy as np
import torch

try:
    from .model_ultrasonic_cnn import Classifier
except ImportError:
    from model_ultrasonic_cnn import Classifier


def parse_signal_text(text: str) -> np.ndarray:
    parts = [p.strip() for p in str(text).split('|') if p.strip() != '']
    return np.asarray([float(p) for p in parts], dtype=np.float32)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stdin', action='store_true')
    parser.add_argument('--threshold', type=float, default=None)
    return parser.parse_args()

def normalize_state_dict(state_dict):
    # 변경: LightningModule로 저장된 checkpoint의 'model.' prefix를 제거한다.
    normalized = {}
    for key, value in state_dict.items():
        if key.startswith('model.'):
            normalized[key[len('model.'):]] = value
        else:
            normalized[key] = value
    return normalized


def main():
    args = parse_args()

    if not args.stdin:
        raise ValueError('This script requires --stdin JSON input.')

    while True:
        line = sys.stdin.readline()
        if not line:
            break

        try:
            payload = json.loads(line)

            location = payload['location']
            sensor_data = payload['sensorData']
            url = payload['url']

            ckpt_path = os.path.join(url, f'{location}.ckpt')
            default_ckpt_path = os.path.join(url, 'default.ckpt')
            proto_path = os.path.join(url, f'{location}_prototype.npy')
            default_proto_path = os.path.join(url, 'default_prototype.npy')

            if os.path.exists(ckpt_path):
                target_ckpt = ckpt_path
            elif os.path.exists(default_ckpt_path):
                target_ckpt = default_ckpt_path
            else:
                raise FileNotFoundError(f'Checkpoint not found: {ckpt_path}')

            if os.path.exists(proto_path):
                target_npy = proto_path
            elif os.path.exists(default_proto_path):
                target_npy = default_proto_path
            else:
                raise FileNotFoundError(f'Model not found: {proto_path}')
            
            # ----------------------------
            # 모델 로드 (캐싱 제거됨)
            # ----------------------------
            bundle = torch.load(target_ckpt, map_location='cpu')

            state_dict = bundle['state_dict'] if isinstance(bundle, dict) and 'state_dict' in bundle else bundle
            n_filters = int(bundle.get('n_filters', 20)) if isinstance(bundle, dict) else 20

            model = Classifier(n_filters=n_filters)
            model.load_state_dict(normalize_state_dict(state_dict), strict=False)
            model.eval()

            # ----------------------------
            # 데이터 파싱
            # ----------------------------
            signal = parse_signal_text(sensor_data)

            # ----------------------------
            # prototype 로드
            # ----------------------------
            prototype = np.load(target_npy).astype(np.float32)
            proto = torch.from_numpy(prototype).float()

            if proto.ndim == 2:
                proto = proto.squeeze(0)

            # ----------------------------
            # 추론
            # ----------------------------
            with torch.no_grad():
                x = torch.from_numpy(signal[None, :]).float()

                emb = model(x).squeeze(0)

                proto = proto.squeeze(0) if proto.ndim == 2 else proto

                dist = torch.norm(emb - proto)
                score = float(dist.item())

            # ----------------------------
            # 결과
            # ----------------------------
            result = {
                'ok': True,
                'location': location,
                'prototypeReady': True,
                'score': score,
            }

            if args.threshold is not None:
                result['pred'] = 1 if score > args.threshold else 0

            print(json.dumps(result, ensure_ascii=False))
            sys.stdout.flush()

        except Exception as e:
            print(json.dumps({
                "ok": False,
                "error": str(e)
            }))
            sys.stdout.flush()


if __name__ == '__main__':
    main()
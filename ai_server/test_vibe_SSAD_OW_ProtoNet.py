import argparse
import os
import json
import sys
import warnings

warnings.filterwarnings("ignore")
import numpy as np
import torch
import math

try:
    from .model_vibe_cnn import Classifier
except ImportError:
    from model_vibe_cnn import Classifier


def parse_signal_text(text: str) -> np.ndarray:
    parts = [p.strip() for p in str(text).split('|') if p.strip() != '']
    return np.asarray([float(p) for p in parts], dtype=np.float32)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stdin', action='store_true')
    parser.add_argument('--threshold', type=float, default=None)
    return parser.parse_args()


def normalize_state_dict(state_dict):
    # LightningModule로 저장된 checkpoint의 'model.' prefix 제거
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
            axis = payload.get('axis', 'PIEZO')  # PIEZO, X, Y, Z 구분
            sensor_data = payload['sensorData']
            url = payload['url']

            # 진동 센서는 축별 파일명(예: MAC_X.ckpt)을 우선 탐색하고, 없으면 기본값으로 폴백
            if not url:
                result = {
                    'ok': False,
                    'location': location,
                    'axis': axis,
                    'message': f'notfoundUrl'
                }
                print(json.dumps(result, ensure_ascii=False))
                sys.stdout.flush()
                continue
            
            # 절대 경로 기반으로 디렉토리 보장 및 파일 경로 확정
            base_dir = os.path.abspath(url)
            
            model_prefix = f"{location}_{axis}"
            ckpt_path = os.path.join(base_dir, f'{model_prefix}.ckpt')
            proto_path = os.path.join(base_dir, f'{model_prefix}_prototype.npy')
            default_ckpt_path = os.path.join(base_dir, f'default_{axis}.ckpt')
            default_proto_path = os.path.join(base_dir, f'default_{axis}_prototype.npy')

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
            # 모델 로드
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
            # 추론 (유클리드 거리 계산)
            # ----------------------------
            with torch.no_grad():
                x = torch.from_numpy(signal[None, :]).float()

                emb = model(x).squeeze(0)
                proto = proto.squeeze(0) if proto.ndim == 2 else proto

                dist = torch.norm(emb - proto)
                score = float(dist.item())

            if math.isnan(score) or math.isinf(score):
                score = 0.0
                
            # ----------------------------
            # 결과 반환 (기존 test_train 스크립트와 동일한 구조로 통합)
            # ----------------------------
            is_anomaly = "N"
            prob = 0.0
            
            threshold = payload.get('threshold')
            if threshold == "" or threshold == "null":
                threshold = None

            # 1. Payload에 없고
            # 2. 커맨드라인(args)에도 없다면
            # 최후의 기본값으로 1.0을 부여 (ProtoNet 유클리디안 거리 임시 기준치)
            if threshold is None:
                threshold = args.threshold if args.threshold is not None else 1.0

            # 임계치가 0보다 큰 정상적인 값일 경우에만 확률 계산 수행
            if threshold is not None and float(threshold) > 0:
                threshold = float(threshold)
                is_anomaly = "Y" if score > threshold else "N"
                prob = min(0.99, score / (threshold * 2.0)) if is_anomaly == "Y" else max(0.01, (score / threshold) * 0.49)
                prob = round(prob, 4)
                
            item = {
                'score': score,
                'is_anomaly': is_anomaly,
                'prob': prob
            }

            result = {
                'ok': True,
                'location': location,
                'axis': axis,
                'prototypeReady': True,
                'count': 1,
                'results': [item],  
                'scores': [score],
                'min': score,       
                'max': score,
                'mean': score,
                'score': score,
                'is_anomaly': is_anomaly,
                'prob': prob
            }

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
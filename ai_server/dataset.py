import torch
import numpy as np


def normalize_data(data):
    # 신호 데이터의 최소/최대 값을 이용해 0~1 사이의 값으로 정규화
    min_vals = data.min()
    max_vals = data.max()
    normalized_data = (data - min_vals) / (max_vals - min_vals)

    return normalized_data


class LeakDataset(torch.utils.data.Dataset):
    def __init__(self, data, labels, normalize=True, transforms=None):
        self.data = data
        self.labels = labels
        self.normalize = normalize
        self.transforms = transforms # RandAugment 등의 데이터 변형 기법이 들어감


    def __len__(self):
        return len(self.data)


    def __getitem__(self, i):
        data = self.data[i]
        label = self.labels[i]
        # 핵심 로직 : 라벨이 0보다 작은 경우(-1, 가상 누출 데이터)에만 transforms(증강/왜곡)를 적용
        # 정상 데이터는 원형을 유지하고, 복사된 데이터만 훼손하여 이상 데이터를 억지로 만듬
        if label < 0 and self.transforms is not None:
            # 형태를 맞추기 위해 차원을 늘렸다 줄였다를 반복
            data = self.transforms(data[None, :, None]).squeeze().astype(np.float32)

        if self.normalize:
            data = normalize_data(data)
        # PyTorch 모델에 입력될 최종 형태의 딕셔너리로 반환
        return {'data': data.squeeze(), 'label': label}


class KAERIBatchSampler():
    def __init__(self, labels, n_support=5, n_query=8, n_iters=50):
        '''
        labels: class 라벨, 0 ~ n-1 의 정수로 이루어진 1차원 배열
        n_support: support features 개수
        n_query: episode당 query 데이터 개수
        n_iters: 에폭당 episodes 수
        '''
        np.random.seed(42)
        self.labels = labels
        self.n_way = 1 # n_way: episode에서 학습에 사용할 class 수, one-class protonet 이기 때문에 1을 사용
        self.n_support = n_support # 기준점이 될 정상 데이터 개수
        self.n_query = n_query # 시험을 칠 정상/이상 데이터 개수
        self.n_iters = n_iters # 한 에폭당 구성할 에피소드(미니배치) 반복 횟수

        # 라벨별(정상 +1, 이상 -1)로 데이터의 인덱스 저장
        self.cls_types, self.n_per_cls = np.unique(self.labels, return_counts=True)
        self.idxs = {label.item(): list() for label in self.cls_types}

        for idx, label in enumerate(self.labels):
            self.idxs[label].append(idx)


    def __iter__(self):
        """
        yield (n_support for normal + normal n_query + leak n_query) idx
        """
        # 모델 학습 시 데이터로더가 이 메서드를 호출하여 배치
        for _ in range(self.n_iters):
            selected_cls = np.random.choice(self.cls_types[self.cls_types > 0]) # 항상 정상(+1) 클래스 선택
            # 1. 정상 인덱스 목록에서 (Support 5개 + Query 8개) = 13개를 무작위로 비복원 추출
            positive_idx = np.random.choice(self.idxs[selected_cls], self.n_support + self.n_query, replace=False)
            # 2. 가상 누출(-1) 인덱스 목록에서 (Query 8개)를 무작위로 비복원 추출
            negative_idx = np.random.choice(self.idxs[-selected_cls], self.n_query, replace=False)
            # 3. 추출한 인덱스를 하나로 합쳐서 한 에피소드의 배치
            batch = np.concatenate([positive_idx, negative_idx], dtype=int)

            yield batch


    def __len__(self):
        """
        returns the number of iterations (episodes) per epoch
        """
        return self.n_iters
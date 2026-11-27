"""현재 수신 파형에 맞춘 HOKO 이상탐지.

내륜/외륜/볼 분류는 하지 않는다. 선택한 파형으로 회전 예측 오차를 낮춘 뒤,
그 동역학 표현과 정상 프로토타입의 거리로 이상을 판정한다.
"""

import torch
from torch import nn

from .hoko_field import BalancedSubbandAttentionMixer, DualViewKoopmanMoriField


class HokoAnomaly(nn.Module):
    def __init__(self):
        super().__init__()
        self.atoms = 32
        self.carrier_bands = 4
        self.field_width = 12
        self.mixer = BalancedSubbandAttentionMixer(
            atom_count=self.atoms,
            band_count=self.carrier_bands,
            harmonics=2,
            iterations=10,
        )
        self.trunk = DualViewKoopmanMoriField(
            carrier_bands=self.carrier_bands,
            field_width=self.field_width,
            state_width=0,
            embedding_width=48,
            operation_count=1,
            attention_heads=4,
            temporal_layers=1,
            feedforward_width=96,
            mixture_count=1,
            minimum_query_width=1.0,
            maximum_query_width=1.0,
            hop_revolutions=1.0,
            forecast_horizons=1,
        )

    def observation(self, wave):
        if wave.ndim != 2:
            raise ValueError("입력은 [배치, 파형 길이] 여야 합니다.")
        batch, length = wave.shape
        coords = length // self.atoms
        coords = coords - (coords % 2)
        windows = coords // 2
        if windows < 2:
            raise ValueError("파형이 너무 짧아 HOKO 창을 만들 수 없습니다. 길이 %d" % length)
        valid_length = self.atoms * coords
        mixed = self.mixer(wave[:, :valid_length].reshape(batch, self.atoms, coords))
        paths = mixed.reshape(batch, windows, self.carrier_bands, 1, 2)
        valid = torch.ones((batch, windows), dtype=torch.bool, device=wave.device)
        orders = torch.ones((1,), device=wave.device, dtype=wave.dtype)
        return paths, valid, orders

    def representation(self, wave):
        paths, valid, orders = self.observation(wave)
        _normalized, encoded = self.trunk._causal_states(paths, valid, orders)
        state_field = self.trunk.field_decoder(self.trunk._joint_order_states(encoded, orders))
        last = valid.sum(1).clamp_min(1) - 1
        index = last.view(-1, 1, 1, 1).expand(-1, state_field.shape[1], 1, self.field_width)
        field = state_field.gather(2, index).reshape(wave.shape[0], -1)
        field = field - field.mean(dim=1, keepdim=True)
        return field / field.square().mean(dim=1, keepdim=True).add(1e-6).sqrt()

    def forecast_loss(self, wave):
        paths, valid, orders = self.observation(wave)
        return self.trunk.dynamics_loss(paths, valid, orders)

    def score(self, wave, prototype):
        rep = self.representation(wave)
        center = prototype.reshape(1, -1).to(device=rep.device, dtype=rep.dtype)
        return torch.norm(rep - center, dim=1)

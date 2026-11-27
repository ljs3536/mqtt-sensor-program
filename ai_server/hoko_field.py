"""Deployable DV-HOKO core shared by HUST and Data_final."""

from __future__ import annotations

import math

import torch
from torch import Tensor, nn


class BalancedSubbandAttentionMixer(nn.Module):
    """Learn a balanced grouping of generic contiguous spectral atoms."""
    """
    [Sinkhorn 정규화 기반 주파수 혼합기]
    1D 진동 신호를 쪼갠 32개의 미세 주파수 대역(Atoms)을 
    정보의 쏠림(Mode Collapse) 없이 4개의 핵심 대역(Carrier Bands)으로 균형 있게 압축합니다.
    """
    def __init__(self, atom_count: int, band_count: int, harmonics: int, iterations: int):
        super().__init__()
        # 파라미터 유효성 검사 (최소 1 이상이어야 함)
        if min(atom_count, band_count, harmonics, iterations) < 1:
            raise ValueError("invalid mixer geometry")
        self.atom_count = int(atom_count)          # 입력될 미세 주파수 대역 수 (예: 32)
        self.band_count = int(band_count)          # 출력될 핵심 주파수 대역 수 (예: 4)
        self.sinkhorn_iterations = int(iterations) # Sinkhorn 균형화 반복 횟수 (예: 10)

        # 1. 서브밴드 위치 인코딩 (Positional Encoding)
        # 주파수 대역의 순서를 신경망이 인지할 수 있도록 0.0 ~ 1.0 사이의 좌표값 생성
        coordinate = torch.linspace(0.0, 1.0, atom_count)

        # 기본 좌표 특성: 상수(1), 선형(x), 이차식(x^2)
        features = [torch.ones_like(coordinate), coordinate, coordinate.square()]

        # 푸리에 급수(Fourier Series) 기반 조화파(Harmonics) 특성 추가
        # 주파수 신호의 주기적 특성을 잘 표현하기 위해 sin, cos 함수 적용
        for harmonic in range(1, harmonics + 1):
            features.extend((
                torch.sin(math.pi * harmonic * coordinate),
                torch.cos(math.pi * harmonic * coordinate),
            ))
        features = torch.stack(features, dim=-1)

        # 특성 벡터들의 스케일을 균일하게 맞추기 위한 정규화
        features = features / features.square().mean(0, keepdim=True).sqrt().clamp_min(1e-6)

        # 학습되지 않는 고정 텐서(Buffer)로 등록
        self.register_buffer("subband_coordinate", features)

        # 2. 밴드 쿼리 (학습 가능한 파라미터)
        # 4개의 캐리어 대역이 각각 어떤 주파수 특성을 가져갈지 결정하는 가중치 벡터
        self.band_queries = nn.Parameter(torch.empty(band_count, features.shape[-1]))
        nn.init.normal_(self.band_queries, std=features.shape[-1] ** -0.5)

    def masks(self) -> Tensor:
        """
        Sinkhorn-Knopp 알고리즘을 사용하여 
        32개 주파수 대역 -> 4개 캐리어 대역으로 분배할 '균형 잡힌 가중치 마스크'를 계산합니다.
        """
        # 밴드 쿼리와 서브밴드 좌표 간의 내적을 통해 기본 연관성(Logits) 계산
        logits = self.band_queries @ self.subband_coordinate.T
        # Softmax와 유사하게 양수 확률값으로 변환 (오버플로우 방지를 위해 max값 차감)
        values = torch.exp(logits / math.sqrt(self.subband_coordinate.shape[-1]) - logits.max())
        # 목표 분배 비율: 32개 대역이 4개 대역으로 가므로, 각 캐리어는 8개(32/4) 분량씩 가져가야 함
        target = values.new_tensor(values.shape[1] / values.shape[0])

        # Sinkhorn Iteration (최적 운송 알고리즘)
        # 특정 대역에만 정보가 몰리는 현상(Mode Collapse)을 방지
        for _ in range(self.sinkhorn_iterations):
            # 행(Row) 정규화: 32개 미세 대역 입장에서 총합을 1로 맞춤 (분배 비율 맞추기)
            values = values / values.sum(0, keepdim=True).clamp_min(1e-12)
            # 열(Column) 정규화: 4개 캐리어 대역 입장에서 목표 할당량(target)만큼 가져가도록 강제
            values = values * target / values.sum(1, keepdim=True).clamp_min(1e-12)
        # 최종적으로 열(미세 대역) 기준 합이 1이 되도록 다시 한번 정규화하여 반환
        return values / values.sum(0, keepdim=True).clamp_min(1e-12)

    def forward(self, envelopes: Tensor) -> Tensor:
        """
        입력된 32대역 주파수 신호에 Sinkhorn 마스크를 곱하여 4대역으로 압축합니다.
        """
        # 입력 형태 검증: [배치 크기(observation), 미세 대역 수(atom), 시간 좌표(coordinate)]
        if envelopes.ndim != 3 or envelopes.shape[1] != self.atom_count:
            raise ValueError("expected [observation,atom,coordinate]")
        # 아인슈타인 표기법(einsum)을 사용한 행렬 곱
        # masks(): [band(4), atom(32)] / envelopes: [batch, atom(32), coords]
        # 결과: [batch, band(4), coords] 형태로 압축되어 반환
        return torch.einsum("ba,san->sbn", self.masks(), envelopes)


class DualViewKoopmanMoriField(nn.Module):
    """One shared neural trunk with centered dynamics and preserved state views."""
    """
    [다중 관점 쿱만-모리 동역학 엔진]
    관측된 진동 데이터에서 회전체의 정상적인 움직임(마르코프 항)을 분리하고,
    예측을 빗나가는 물리적 충돌 오차(모리 혁신)만을 추출하여 인과적으로 분석합니다.
    """
    def __init__(
        self, *, carrier_bands: int, field_width: int, state_width: int,
        embedding_width: int, operation_count: int, attention_heads: int,
        temporal_layers: int, feedforward_width: int, mixture_count: int,
        minimum_query_width: float, maximum_query_width: float,
        hop_revolutions: float, forecast_horizons: int,
    ) -> None:
        super().__init__()
        self.carrier_bands = int(carrier_bands)
        self.field_width = int(field_width)
        self.state_width = int(state_width)
        self.embedding_width = int(embedding_width)
        self.operation_count = int(operation_count)
        self.mixture_count = int(mixture_count)
        self.minimum_query_width = float(minimum_query_width)
        self.maximum_query_width = float(maximum_query_width)
        self.hop_revolutions = float(hop_revolutions)
        self.forecast_horizons = int(forecast_horizons)

        # 1. 상태 임베딩 레이어 (Lifting to Koopman Space)
        # 관측 뷰(2) + 동기화 뷰(2) + 혁신 뷰(2) = 총 6개의 텐서 채널을
        # 쿱만 연산자가 선형적으로 작동할 수 있는 128차원의 고차원 공간으로 끌어올립니다.
        self.path_embedding = nn.Sequential(
            nn.Linear(6 * carrier_bands, embedding_width), nn.GELU(),
            nn.LayerNorm(embedding_width),
        )
        # 시간에 따른 진동 궤적을 추적하기 위한 위치/시간 인코딩
        self.time_embedding = nn.Sequential(
            nn.Linear(5, embedding_width), nn.GELU(), nn.Linear(embedding_width, embedding_width)
        )

        # 2. 인과적 트랜스포머 (Causal Transformer)
        # 미래의 진동 데이터를 미리 보지 못하게 차단(Causal Masking)한 상태에서,
        # 과거의 물리적 충돌 오차(Innovation)들이 어떻게 누적되어 왔는지 시간적 맥락을 파악합니다.
        layer = nn.TransformerEncoderLayer(
            d_model=embedding_width, nhead=attention_heads,
            dim_feedforward=feedforward_width, dropout=0.0,
            batch_first=True, norm_first=True,
        )
        self.temporal_encoder = nn.TransformerEncoder(layer, num_layers=temporal_layers)
        self.order_embedding = nn.Sequential(
            nn.Linear(5, embedding_width), nn.GELU(), nn.Linear(embedding_width, embedding_width)
        )
        self.field_decoder = nn.Sequential(
            nn.LayerNorm(embedding_width), nn.Linear(embedding_width, feedforward_width),
            nn.GELU(), nn.Linear(feedforward_width, field_width),
        )

        # These operation-reader parameters are retained for checkpoint compatibility.
        self.mode_score = nn.Sequential(
            nn.Linear(field_width, embedding_width), nn.GELU(),
            nn.LayerNorm(embedding_width), nn.Linear(embedding_width, 1),
        )
        self.class_embedding = nn.Parameter(torch.empty(operation_count, embedding_width))
        nn.init.normal_(self.class_embedding, std=embedding_width ** -0.5)
        self.query_generator = nn.Sequential(
            nn.Linear(embedding_width, embedding_width), nn.GELU(),
            nn.Linear(embedding_width, 3 * mixture_count),
        )
        self.context_mode_embedding = nn.Sequential(
            nn.LayerNorm(field_width), nn.Linear(field_width, embedding_width),
            nn.GELU(), nn.LayerNorm(embedding_width),
        )
        self.context_order_embedding = nn.Sequential(
            nn.Linear(5, embedding_width), nn.GELU(), nn.Linear(embedding_width, embedding_width)
        )
        self.context_query = nn.Parameter(torch.empty(embedding_width))
        nn.init.normal_(self.context_query, std=embedding_width ** -0.5)

        # 3. 다중 구간 예측 헤드 (Multi-horizon Forecast)
        # 현재 상태를 바탕으로 '미래 여러 시점'의 진동을 예측하기 위한 디코더입니다.
        self.memory_forecast_head = nn.Sequential(
            nn.LayerNorm(field_width),
            nn.Linear(field_width, forecast_horizons * 2 * carrier_bands),
        )
        state_input = embedding_width + 2 * carrier_bands
        self.state_decoder = None
        if self.state_width > 0:
            self.state_decoder = nn.Sequential(
                nn.LayerNorm(state_input), nn.Linear(state_input, embedding_width),
                nn.GELU(), nn.Linear(embedding_width, state_width),
            )

    def field_parameters(self):
        """Parameters fitted by the class-free Koopman--Mori forecast stage."""

        for module in (
            self.path_embedding,
            self.time_embedding,
            self.temporal_encoder,
            self.order_embedding,
            self.field_decoder,
            self.memory_forecast_head,
        ):
            yield from module.parameters()

    def state_parameters(self):
        """Parameters fitted by the health-state stage."""

        if self.state_decoder is None:
            return
        yield from self.state_decoder.parameters()

    @staticmethod
    def _coordinate(value: Tensor, divisor: float) -> Tensor:
        normalized = value / divisor
        return torch.stack((
            normalized, torch.sin(math.pi * normalized), torch.cos(math.pi * normalized),
            torch.sin(2.0 * math.pi * normalized), torch.cos(2.0 * math.pi * normalized),
        ), dim=-1)

    def _causal_states(self, paths: Tensor, valid: Tensor, orders: Tensor):
        """
        [핵심 물리 연산부: 모리-쯔반지히 형식론 구현]
        데이터를 3가지 관점(관측, 동기화, 오차)으로 쪼개고 트랜스포머에 태우는 메서드입니다.
        """
        batch, windows, bands, order_count, _ = paths.shape

        # 1. 신호 정규화 (Energy Normalization)
        # 진동의 에너지를 시간 누적치로 나누어 스케일을 일정하게 맞춥니다.
        power = paths.square().sum(dim=-1)
        weighted = power * valid[:, :, None, None]
        power_steps, count_steps = [], []
        running_power = weighted.new_zeros((batch, bands))
        running_count = weighted.new_zeros((batch,))
        for step in range(windows):
            running_power = running_power + weighted[:, step].sum(dim=2)
            running_count = running_count + valid[:, step].to(weighted.dtype)
            power_steps.append(running_power); count_steps.append(running_count)
        cumulative_power = torch.stack(power_steps, dim=1)
        cumulative_count = torch.stack(count_steps, dim=1).clamp_min(1) * order_count
        scale = (cumulative_power / cumulative_count[:, :, None]).add(1e-6).sqrt()
        normalized = paths / scale[:, :, :, None, None] # -> [관측 뷰 (Normalized View)]

        # 2. 가상의 회전 동기화 (Phase Locking)
        # RPM(orders)과 시간(time)을 바탕으로 회전 각도(lock_angle)를 계산합니다.
        time = torch.arange(windows, device=paths.device, dtype=paths.dtype)
        lock_angle = -2.0 * math.pi * time[:, None] * orders[None] * self.hop_revolutions
        lock_cos, lock_sin = torch.cos(lock_angle), torch.sin(lock_angle)

        # 실제 진동을 이 회전 각도에 맞춰 억지로 고정시켜버립니다.
        # (마치 회전하는 축에 카메라를 달고 진동을 찍는 것과 같은 효과)
        locked = torch.stack((
            lock_cos[None, :, None] * normalized[..., 0]
            - lock_sin[None, :, None] * normalized[..., 1],
            lock_sin[None, :, None] * normalized[..., 0]
            + lock_cos[None, :, None] * normalized[..., 1],
        ), dim=-1) # -> [동기화 뷰 (Locked View)]

        # 3. 쿱만 예측 및 모리 혁신 오차 계산 (Koopman Prediction & Mori Innovation)
        # 이전 시점(t-1)의 진동을 바탕으로 현재 시점(t)의 진동 궤적을 예측합니다.
        angle = 2.0 * math.pi * orders * self.hop_revolutions
        predicted = torch.stack((
            torch.cos(angle)[None, None, None] * normalized[:, :-1, ..., 0]
            - torch.sin(angle)[None, None, None] * normalized[:, :-1, ..., 1],
            torch.sin(angle)[None, None, None] * normalized[:, :-1, ..., 0]
            + torch.cos(angle)[None, None, None] * normalized[:, :-1, ..., 1],
        ), dim=-1) # -> [쿱만 연산자가 예측한 마르코프 궤적]

        # [매우 중요] 실제 들어온 진동(normalized)에서 예측된 진동(predicted)을 뺍니다.
        # 베어링 표면 긁힘 등 예측할 수 없는 '돌발적인 물리 충돌 오차'만 순수하게 남습니다.
        innovation = normalized.new_zeros(normalized.shape)
        innovation[:, 1:] = normalized[:, 1:] - predicted # -> [모리 혁신 뷰 (Innovation View)]

        # 4. 특징 결합 및 고차원 투영
        # 세 가지 관점(관측, 동기화, 오차)을 모두 합쳐서 고차원 임베딩 공간으로 보냅니다.
        features = torch.cat((normalized, locked, innovation), dim=-1)
        tokens = features.permute(0, 3, 1, 2, 4).reshape(
            batch * order_count, windows, 6 * bands
        )
        tokens = self.path_embedding(tokens)
        tokens = tokens + self.time_embedding(self._coordinate(time, 32.0))[None]
        expanded_valid = valid[:, None].expand(-1, order_count, -1).reshape(
            batch * order_count, windows
        )

        # 5. 인과적 트랜스포머 인코딩 (미래 참조 방지)
        # torch.triu를 통해 대각선 위쪽이 가려진 상삼각 마스크(Causal Mask)를 생성합니다.
        mask = torch.triu(torch.ones(windows, windows, dtype=torch.bool, device=paths.device), 1)

        # 과거의 오차 맥락만을 활용하여 현재 상태를 인코딩합니다.
        encoded = self.temporal_encoder(
            tokens, mask=mask, src_key_padding_mask=~expanded_valid
        ).reshape(batch, order_count, windows, self.embedding_width)
        return normalized, encoded

    def _joint_order_states(self, encoded: Tensor, orders: Tensor) -> Tensor:
        context = self.order_embedding(
            self._coordinate(orders, max(float(orders.max()), 1.0))
        )[None, :, None]
        return encoded + context

    def _field_and_forecast(self, paths: Tensor, valid: Tensor, orders: Tensor):
        """
        인코딩된 상태를 바탕으로 미래의 오차를 계산하고(Loss), 최종 특징 벡터(Field)를 뽑아냅니다.
        """
        normalized, encoded = self._causal_states(paths, valid, orders)
        batch, windows, bands, order_count, _ = normalized.shape

        # 인코딩된 맥락을 디코딩하여 현재 시스템의 동역학 상태(State Field)를 정의합니다.
        state_field = self.field_decoder(self._joint_order_states(encoded, orders))
        raw = self.memory_forecast_head(state_field).reshape(
            batch, order_count, windows, self.forecast_horizons, bands, 2
        )
        losses = []
        for offset in range(1, self.forecast_horizons + 1):
            if offset >= windows:
                continue
            angle = 2.0 * math.pi * orders * self.hop_revolutions * offset
            source = normalized[:, :-offset]
            resolved = torch.stack((
                torch.cos(angle)[None, None, None] * source[..., 0]
                - torch.sin(angle)[None, None, None] * source[..., 1],
                torch.sin(angle)[None, None, None] * source[..., 0]
                + torch.cos(angle)[None, None, None] * source[..., 1],
            ), dim=-1)
            memory = raw[:, :, :-offset, offset - 1].permute(0, 2, 3, 1, 4)
            pair = valid[:, :-offset] & valid[:, offset:]
            error = resolved + memory - normalized[:, offset:]
            mask = pair[:, :, None, None, None]
            denominator = mask.sum().clamp_min(1) * bands * order_count * 2
            losses.append((error.square() * mask).sum() / denominator)
        if not losses:
            raise ValueError("trajectory is shorter than the forecast horizon")

        # 최종적으로 모델의 마지막 구간(last)에서 추출한 특징 벡터를 정규화하여 반환합니다.
        # 이것이 HokoClassifier의 forward에서 뽑아가서 ProtoNet 좌표로 쓰는 핵심 데이터입니다.
        last = valid.sum(1).clamp_min(1) - 1
        gather = last[:, None, None, None].expand(-1, order_count, 1, self.field_width)
        field = state_field.gather(2, gather).squeeze(2)
        field = field - field.mean(1, keepdim=True)
        field = field / field.square().mean(1, keepdim=True).add(1e-6).sqrt()
        return field, torch.stack(losses).mean()

    def _field(self, paths: Tensor, valid: Tensor, orders: Tensor) -> Tensor:
        """
        우리의 HokoClassifier가 호출하는 메서드입니다. 
        Loss는 버리고, 위에서 계산된 순수 동역학 특징 벡터(Field) 텐서만 반환합니다.
        """
        return self._field_and_forecast(paths, valid, orders)[0]

    def dynamics_loss(self, paths: Tensor, valid: Tensor, orders: Tensor) -> Tensor:
        return self._field_and_forecast(paths, valid, orders)[1]

    def state_base(
        self, paths: Tensor, valid: Tensor, orders: Tensor, state_observation: Tensor
    ) -> Tensor:
        if state_observation.shape != (len(paths), 2 * self.carrier_bands):
            raise ValueError("state observation does not match the carrier count")
        _, encoded = self._causal_states(paths, valid, orders)
        joint = self._joint_order_states(encoded, orders)
        last = valid.sum(1).clamp_min(1) - 1
        gather = last[:, None, None, None].expand(-1, joint.shape[1], 1, joint.shape[-1])
        temporal = joint.gather(2, gather).squeeze(2).mean(1)
        return torch.cat((temporal, state_observation), dim=-1)

    def state_mode_base(
        self, paths: Tensor, valid: Tensor, orders: Tensor, state_observation: Tensor
    ) -> Tensor:
        """Return every resolved mode for the learned health-state attention."""

        if state_observation.shape != (len(paths), 2 * self.carrier_bands):
            raise ValueError("state observation does not match the carrier count")
        _, encoded = self._causal_states(paths, valid, orders)
        joint = self._joint_order_states(encoded, orders)
        last = valid.sum(1).clamp_min(1) - 1
        gather = last[:, None, None, None].expand(
            -1, joint.shape[1], 1, joint.shape[-1]
        )
        temporal = joint.gather(2, gather).squeeze(2)
        state = state_observation[:, None].expand(-1, temporal.shape[1], -1)
        return torch.cat((temporal, state), dim=-1)

    def state_view(self, base: Tensor) -> Tensor:
        if self.state_decoder is None:
            raise RuntimeError("this checkpoint uses the attentive health readout")
        values = self.state_decoder(base)
        return torch.cat((values, torch.ones_like(values[..., :1])), dim=-1)


def build_model(config: dict, device: torch.device) -> DualViewKoopmanMoriField:
    observation, architecture = config["observation"], config["architecture"]
    return DualViewKoopmanMoriField(
        carrier_bands=int(observation["carrier_bands"]),
        field_width=int(architecture["field_width"]),
        state_width=int(architecture["state_width"]),
        embedding_width=int(architecture["embedding_width"]),
        operation_count=int(architecture["operation_count"]),
        attention_heads=int(architecture["attention_heads"]),
        temporal_layers=int(architecture["temporal_layers"]),
        feedforward_width=int(architecture["feedforward_width"]),
        mixture_count=int(architecture["mixture_count"]),
        minimum_query_width=float(architecture["minimum_query_width"]),
        maximum_query_width=float(architecture["maximum_query_width"]),
        hop_revolutions=float(observation["hop_units"]),
        forecast_horizons=int(architecture["forecast_horizons"]),
    ).to(device)


def build_mixer(config: dict, device: torch.device) -> BalancedSubbandAttentionMixer:
    observation, mixer = config["observation"], config["learned_filterbank"]
    return BalancedSubbandAttentionMixer(
        int(observation["spectral_atoms"]), int(observation["carrier_bands"]),
        int(mixer["coordinate_harmonics"]), int(mixer["sinkhorn_iterations"]),
    ).to(device)

import torch
import torch.nn as nn

class SpatialAttention1D(nn.Module):
    # 특정 Hz 구간(위치)에 가중치를 부여하는 어텐션 메커니즘
    def __init__(self, kernel_size=7):
        super().__init__()
        # 채널(필터) 축으로 평균과 최대값을 구한 뒤, 합성곱을 통해 '어느 위치가 중요한지' 확률값(0~1)을 만듦
        padding = kernel_size // 2
        self.conv = nn.Conv1d(2, 1, kernel_size, padding=padding, bias=False)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # x.shape = (batch, channels, seq_len(Hz길이))
        avg_out = torch.mean(x, dim=1, keepdim=True)
        max_out, _ = torch.max(x, dim=1, keepdim=True)
        # 평균값과 최대값을 이어 붙임
        concat = torch.cat([avg_out, max_out], dim=1)
        # 합성곱을 거쳐 0~1 사이의 가중치 지도(Attention Map) 생성
        attn_weight = self.sigmoid(self.conv(concat))
        # 원본 데이터에 중요도(가중치)를 곱해줌 (중요한 Hz는 살리고, 불필요한 노이즈 Hz는 죽임)
        return x * attn_weight
    

class Downsample(nn.Module):

    # 1D CNN 블록 
    # 1차원 합성곱(Conv1d)을 이용해 신호의 길이는 반으로 줄이고(stride=2), 
    # 채널(필터) 수는 늘려서 데이터의 핵심 특징(패턴, 주파수 특성 등)만 뽑아내는 레이어
    def __init__(self, in_channels, out_channels, kernel_size=4, stride=2, padding=1, instance_norm=True):
        super().__init__()
        self.block = nn.Sequential(
            # 패턴 찾기 & 압축 (nn.Conv1d)
            nn.Conv1d(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=kernel_size,  # 확인할 진동 데이터 개수
                stride=stride,
                padding=padding,
                # bias=nn.InstanceNorm1d,
                bias=True
            ),
            # 데이터를 읽을때마다 stride가 2 이므로 2칸씩 확인 데이터를 넘기는데
            # Downsample이 진행될때마다 전체 파형의 길이가 절반으로 줄어들게됨
        )
        if instance_norm:
            # 데이터 안정화 (nn.InstanceNorm1d)
            # 진동값이 비정상적으로 튀거나 너무 작아져서 학습이 망가지는 것을 막기 위해
            # 데이터의 평균과 분산을 일정하게 억눌러주는(정규화) 안전장치
            self.block.append(nn.InstanceNorm1d(out_channels))
        # 핵심 신호 활성화 (nn.GELU)
        # 누출을 잡는데 중요한 신호는 통과시키고 쓸모없는 배경 노이즈는 0으로 무시해버리는
        # 필터 역할
        self.block.append(nn.GELU())

    def forward(self, x):
        return self.block(x)


def init_weights(net, init_type='normal', init_gain=0.02):
    # 가중치 초기화: 딥러닝 모델이 처음 태어났을 때, 가중치(뇌세포의 연결 강도)를 
    # 랜덤하게 아무렇게나 두는 게 아니라 수학적으로 예쁘게(정규분포 등) 세팅해 주어 학습이 빠르고 안정적이게 함
    """Initialize network weights.
    Parameters:
        net (network)   -- network to be initialized
        init_type (str) -- the name of an initialization method: normal | xavier | kaiming | orthogonal
        init_gain (float)    -- scaling factor for normal, xavier and orthogonal.
    We use 'normal' in the original pix2pix and CycleGAN paper. But xavier and kaiming might
    work better for some applications. Feel free to try yourself.
    """
    def init_func(m):  # define the initialization function
        classname = m.__class__.__name__
        if hasattr(m, 'weight') and (classname.find('Conv') != -1 or classname.find('Linear') != -1):
            if init_type == 'normal':
                nn.init.normal_(m.weight.data, 0.0, init_gain)
            elif init_type == 'xavier':
                nn.init.xavier_normal_(m.weight.data, gain=init_gain)
            elif init_type == 'kaiming':
                nn.init.kaiming_normal_(m.weight.data, a=0, mode='fan_in')
            elif init_type == 'orthogonal':
                nn.init.orthogonal_(m.weight.data, gain=init_gain)
            else:
                raise NotImplementedError('initialization method [%s] is not implemented' % init_type)
            if hasattr(m, 'bias') and m.bias is not None:
                nn.init.constant_(m.bias.data, 0.0)
        elif classname.find('BatchNorm2d') != -1:  # BatchNorm Layer's weight is not a matrix; only normal distribution applies.
            nn.init.normal_(m.weight.data, 1.0, init_gain)
            nn.init.constant_(m.bias.data, 0.0)

    net.apply(init_func)  # apply the initialization function <init_func>


class Upsample(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=4, stride=2, padding=1, instance_norm=True):
        super().__init__()
        self.block = nn.Sequential(
            nn.ConvTranspose1d(in_channels, out_channels, kernel_size=kernel_size, stride=stride, padding=padding, bias=True)
        )
        if instance_norm:
            self.block.append(nn.InstanceNorm1d(out_channels))
        self.block.append(nn.GELU())

    def forward(self, x):
        return self.block(x)

class AutoEncoder(nn.Module):
    def __init__(self, n_filters=20):
        super().__init__()
        self.attention = SpatialAttention1D(kernel_size=7)
        
        self.encoder = nn.Sequential(
            Downsample(1, n_filters, instance_norm=False),
            Downsample(n_filters, n_filters * 2),
            Downsample(n_filters * 2, n_filters * 4),
            Downsample(n_filters * 4, n_filters * 8),
            Downsample(n_filters * 8, n_filters * 16, stride=1),
        )
        
        self.decoder = nn.Sequential(
            Upsample(n_filters * 16, n_filters * 8, stride=1),
            Upsample(n_filters * 8, n_filters * 4),
            Upsample(n_filters * 4, n_filters * 2),
            Upsample(n_filters * 2, n_filters),
            nn.ConvTranspose1d(n_filters, 1, kernel_size=4, stride=2, padding=1)
        )
        init_weights(self)

    def forward(self, x):
        b, seq_len = x.shape
        x = x.reshape(b, 1, seq_len)
        x_attn = self.attention(x)
        
        latent = self.encoder(x_attn)
        reconstructed = self.decoder(latent)
        
        return reconstructed.squeeze(1), latent
import torch
import torch.nn as nn
  

class AddCoords1D(nn.Module):
    # CoordConv 기법
    # CNN은 보통 데이터의 순서/위치를 잘 모르는 단점이 있는데 
    # 이를 보완하기 위해 신호 데이터 옆에 -1 부터 1까지 서서히 증가하는 좌표값을
    # 새로운 채널로 이어 붙여 신경망이 앞부분인지 뒷부분인지 공간적으로 인지할 수 있도록 함
    def __call__(self, x):

        b, c, seq_len = x.shape
        coord = torch.arange(seq_len)

        coord = coord[None, None, :]
        coord = coord.float() / (seq_len - 1)
        coord = coord * 2 - 1 ## range in [-1, ... , 1] 범위로 스케일링
        coord = coord.repeat(b, 1, 1)
        coord = coord.to(x.device)

        ret = torch.cat([x, coord], dim=1) # 원본 신호와 좌표 정보를 하나로 뭉침

        return ret


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



class Classifier(nn.Module):
    # 특징 추출기
    def __init__(self, n_filters=10, coord_conv=False):
        super().__init__()
        in_channels = 1
        if coord_conv:
            add_coords = AddCoords1D()
            in_channels = in_channels + 1
        else:
            add_coords = nn.Identity()

        # 여러 겹의 Downsample을 샌드위치처럼 쌓아 올려서, 긴 센서 파형을 점점 작고 묵직한 데이터로 압축
        self.block = nn.Sequential(
            add_coords,
            Downsample(in_channels=in_channels, out_channels=n_filters, instance_norm=False), ## (b, n_filters, 160)
            Downsample(in_channels=n_filters, out_channels=n_filters * 2), ## (b, n_filters * 2, 80)
            Downsample(in_channels=n_filters * 2, out_channels=n_filters * 4), ## (b, n_filters * 4, 40)
            Downsample(in_channels=n_filters * 4, out_channels=n_filters * 8), ## (b, n_filters * 8, 20)
            Downsample(in_channels=n_filters * 8, out_channels=n_filters * 16, stride=1), ## (b, n_filters * 16, 19)
        )
        # 압축된 파형을 1개의 점(평균값)으로 꽉 눌러버려서 최종적인 1차원 숫자 배열(벡터)로 변환
        self.avgpool = nn.AdaptiveAvgPool1d((1))
        # self.fc = nn.Linear(n_filters * 16, 2)
        init_weights(self)


    def forward(self, x):
        b, seq_len = x.shape
        x = x.reshape([b, 1, seq_len])

        x = self.block(x) # (b x 80 x 19) 형태의 다차원 특징맵으로 변환
        x = self.avgpool(x) # (b x 80 x 1) 형태로 압축
        x = x.view(b, -1)    # 최종적으로 공간 계산을 하기 좋게 (배치크기, 특징길이)의 1차원 벡터로 펼침
        # x = self.fc(x)

        return x


class ProtoNet(nn.Module):
    """
    Model as described in the reference paper,
    source: https://github.com/jakesnell/prototypical-networks/blob/f0c48808e496989d01db59f86d4449d7aee9ab0c/protonets/models/few_shot.py#L62-L84
    """

    def __init__(self):
        super().__init__()
        self.block1 = nn.Linear(320, 80, bias=True)
        self.block2 = nn.Linear(80, 20, bias=True)
        self.block3 = nn.Linear(20, 5, bias=True)
        init_weights(self)

    def forward(self, x):
        out = self.block1(x)
        out = self.block2(out)
        out = self.block3(out)
        out = out.view(out.size(0), -1)

        return out


if __name__ == '__main__':
    import torch
    input = torch.randn((2, 320))
    model = Classifier(5, coord_conv=True)

    print(model(input).shape)
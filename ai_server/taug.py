'''
https://github.com/uchidalab/time_series_augmentation/blob/master/utils/augmentation.py
'''


import random, torch
import numpy as np


def identity(x, _):
    return x


def padifneeded(x, target_len=320):
    '''
    x.shape = (n_batch, cur_len, n_channels)
    '''
    n_batch, cur_len, n_channels = x.shape
    if cur_len < target_len:
        diff = target_len - cur_len
        # ret = np.pad(x, ((0, 0), (diff // 2, diff - diff // 2), (0, 0)))
        ret = np.pad(x, ((0, 0), (0, diff), (0, 0)))
    else:
        ret = x
        
    return ret


def left_crop(x, target_len=320):
    '''
    x.shape = (n_batch, cur_len, n_channels)
    '''
    n_batch, cur_len, n_channels = x.shape
    assert cur_len >= target_len, f"Current_len :{cur_len}, but target_len : {target_len}"

    ret = x[:, :target_len, :]
    return ret


def center_crop(x, target_len=320):
    '''
    x.shape = (n_batch, cur_len, n_channels)
    '''
    n_batch, cur_len, n_channels = x.shape
    assert cur_len >= target_len, f"Current_len :{cur_len}, but target_len : {target_len}"

    if cur_len == target_len:
        ret = x
    else:
        starts = (cur_len - target_len) // 2
        ret = x[:, starts:starts+target_len, :]
        # ret = x[:, -target_len:, :]

    return ret


def random_crop(x, target_len=320):
    '''
    x.shape = (n_batch, cur_len, n_channels)
    '''
    n_batch, cur_len, n_channels = x.shape
    assert cur_len >= target_len, f"Current_len :{cur_len}, but target_len : {target_len}"

    if cur_len == target_len:
        ret = x
    else:
        starts = np.random.randint(0, cur_len - target_len)
        ret = x[:, starts:starts+target_len, :]
        # ret = x[:, -target_len:, :]

    return ret


def flip(x, _=None):
    ret = np.ascontiguousarray(x[:, ::-1, :])
    return ret


def jitter(x, sigma=0.03):
    # https://arxiv.org/pdf/1706.00527.pdf
    return x + np.random.normal(loc=0., scale=sigma, size=x.shape)


def scailing(x, sigma=0.1):
    # https://arxiv.org/pdf/1706.00527.pdf
    factor = np.random.normal(loc=1., scale=sigma, size=(x.shape[0],x.shape[2]))
    return np.multiply(x, factor[:,np.newaxis,:])


def rotation(x):
    flip = np.random.choice([-1, 1], size=(x.shape[0],x.shape[2]))
    rotate_axis = np.arange(x.shape[2])
    np.random.shuffle(rotate_axis)    
    return flip[:,np.newaxis,:] * x[:,:,rotate_axis]


def permutation(x, max_segments=5, seg_mode="equal"):
    orig_steps = np.arange(x.shape[1])
    
    num_segs = np.random.randint(1, max_segments, size=(x.shape[0]))
    
    ret = np.zeros_like(x)
    for i, pat in enumerate(x):
        if num_segs[i] > 1:
            if seg_mode == "random":
                split_points = np.random.choice(x.shape[1]-2, num_segs[i]-1, replace=False)
                split_points.sort()
                splits = np.split(orig_steps, split_points)
            else:
                splits = np.array_split(orig_steps, num_segs[i])
            warp = np.concatenate(np.random.permutation(splits)).ravel()
            ret[i] = pat[warp]
        else:
            ret[i] = pat
    return ret


def magnitude_warp(x, sigma=0.2):
    knot = 4
    from scipy.interpolate import CubicSpline
    orig_steps = np.arange(x.shape[1])
    
    random_warps = np.random.normal(loc=1.0, scale=sigma, size=(x.shape[0], knot+2, x.shape[2]))
    warp_steps = (np.ones((x.shape[2],1))*(np.linspace(0, x.shape[1]-1., num=knot+2))).T
    ret = np.zeros_like(x)
    for i, pat in enumerate(x):
        warper = np.array([CubicSpline(warp_steps[:,dim], random_warps[i,:,dim])(orig_steps) for dim in range(x.shape[2])]).T
        ret[i] = pat * warper

    return ret


def time_warp(x, sigma=0.2):
    knot = 4
    from scipy.interpolate import CubicSpline
    orig_steps = np.arange(x.shape[1])
    
    random_warps = np.random.normal(loc=1.0, scale=sigma, size=(x.shape[0], knot+2, x.shape[2]))
    warp_steps = (np.ones((x.shape[2],1))*(np.linspace(0, x.shape[1]-1., num=knot+2))).T
    
    ret = np.zeros_like(x)
    for i, pat in enumerate(x):
        for dim in range(x.shape[2]):
            time_warp = CubicSpline(warp_steps[:,dim], warp_steps[:,dim] * random_warps[i,:,dim])(orig_steps)
            scale = (x.shape[1]-1)/time_warp[-1]
            ret[i,:,dim] = np.interp(orig_steps, np.clip(scale*time_warp, 0, x.shape[1]-1), pat[:,dim]).T
    return ret


def window_slice(x, reduce_ratio=0.1):
    reduce_ratio = 1 - reduce_ratio
    # https://halshs.archives-ouvertes.fr/halshs-01357973/document
    target_len = np.ceil(reduce_ratio*x.shape[1]).astype(int)
    if target_len >= x.shape[1]:
        return x
    starts = np.random.randint(low=0, high=x.shape[1]-target_len, size=(x.shape[0])).astype(int)
    ends = (target_len + starts).astype(int)
    
    ret = np.zeros_like(x)
    for i, pat in enumerate(x):
        for dim in range(x.shape[2]):
            ret[i,:,dim] = np.interp(np.linspace(0, target_len, num=x.shape[1]), np.arange(target_len), pat[starts[i]:ends[i],dim]).T
    return ret


def window_warp(x, window_ratio=0.1, scales=[0.5, 2.]):
    # https://halshs.archives-ouvertes.fr/halshs-01357973/document
    warp_scales = np.random.choice(scales, x.shape[0])
    warp_size = np.ceil(window_ratio*x.shape[1]).astype(int)
    window_steps = np.arange(warp_size)
        
    window_starts = np.random.randint(low=1, high=x.shape[1]-warp_size-1, size=(x.shape[0])).astype(int)
    window_ends = (window_starts + warp_size).astype(int)
            
    ret = np.zeros_like(x)
    for i, pat in enumerate(x):
        for dim in range(x.shape[2]):
            start_seg = pat[:window_starts[i],dim]
            window_seg = np.interp(np.linspace(0, warp_size-1, num=int(warp_size*warp_scales[i])), window_steps, pat[window_starts[i]:window_ends[i],dim])
            end_seg = pat[window_ends[i]:,dim]
            warped = np.concatenate((start_seg, window_seg, end_seg))                
            ret[i,:,dim] = np.interp(np.arange(x.shape[1]), np.linspace(0, x.shape[1]-1., num=warped.size), warped).T
    return ret


def augment_list(): 
    # l = [
    #     (flip, 0., 1.0),
    #     (jitter, 0., 0.03),
    #     (scailing, 0., 0.1),
    #     (magnitude_warp, 0., 0.2),
    #     (time_warp, 0., 0.2),
    #     (window_slice, 0., 0.1),
    #     (window_warp, 0., 0.1),
    # ]

    l = [
        (flip, 0., 1.0),
        (jitter, 0., 0.06),
        (scailing, 0., 0.2),
        (magnitude_warp, 0., 0.4),
        (time_warp, 0., 0.4),
        (window_slice, 0., 0.2),
        (window_warp, 0., 0.2),
    ]

    return l


class RandAugment:
    def __init__(self, n, m):
        '''
        n : number of augmentation transformations to apply sequentially
        m : magnitude for all transformations
        '''
        self.n = n 
        self.m = m      # [0, 10]
        self.augment_list = augment_list()

        assert n <= len(self.augment_list)
        assert 0 <= m <= 10

    def __call__(self, img):
        ops = random.choices(self.augment_list, k=self.n) ## 중복 선택을 포함
        for op, minval, maxval in ops:
            val = (float(self.m) / 10) * float(maxval - minval) + minval
            img = op(img, val)

        return img


class Compose:
    def __init__(self, transforms=None):
        self.transforms = transforms

    def __call__(self, x):
        n = random.randint(3, len(self.transforms))
        transforms = random.sample(self.transforms, n)

        for transform in transforms:
            x = transform(x)

        return x
"""필터, FFT, SRSS 전력, 샘플링 프로필.

web 패키지를 import하지 않는다.
"""

from __future__ import annotations

import numpy as np


def spectral_filter(x, fs, *, hpf=False, hp_cutoff=10.0, hp_order=4,
                    lpf=False, lp_cutoff=3000.0, lp_order=4,
                    notch=False, notch_center=975.0, notch_width=20.0):
    """HPF, LPF, Notch를 주파수 영역에서 적용한다."""
    data=np.asarray(x,dtype=np.float64)
    if data.size<2 or not (hpf or lpf or notch): return data.copy()
    bins=np.fft.rfftfreq(len(data), d=1/fs)
    resp=np.ones_like(bins)
    if hpf:
        if not 0 < hp_cutoff < fs/2: raise ValueError('HPF 컷오프는 Fs/2보다 작아야 합니다')
        p=np.zeros_like(bins); mask=bins>0
        p[mask]=1/np.sqrt(1+(hp_cutoff/bins[mask])**(2*hp_order))
        resp*=p
    if lpf:
        if not 0 < lp_cutoff < fs/2: raise ValueError('LPF 컷오프는 Fs/2보다 작아야 합니다')
        resp*=1/np.sqrt(1+(bins/lp_cutoff)**(2*lp_order))
    if notch:
        if not (0 < notch_center < fs/2 and 0 < notch_width < fs/2):
            raise ValueError('Notch 주파수/폭은 0~Fs/2 안으로 설정하세요')
        sigma=notch_width/(2*np.sqrt(2*np.log(2)))
        resp*=1-np.exp(-.5*((bins-notch_center)/sigma)**2)
    return np.fft.irfft(np.fft.rfft(data)*resp, n=len(data))


FFT_WINDOWS=('Rectangular','Hann','Hamming','Blackman','Blackman-Harris','Flat Top','Bartlett','Kaiser')

def fft_window(n, name='Blackman', beta=8.6):
    """FFT에 쓸 창 계수를 만든다."""
    if name=='Rectangular': return np.ones(n)
    if name=='Hamming': return np.hamming(n)
    if name=='Blackman': return np.blackman(n)
    if name=='Bartlett': return np.bartlett(n)
    if name=='Kaiser': return np.kaiser(n,beta)
    if name=='Blackman-Harris':
        a=np.arange(n)*2*np.pi/max(1,n-1)
        return .35875-.48829*np.cos(a)+.14128*np.cos(2*a)-.01168*np.cos(3*a)
    if name=='Flat Top':
        a=np.arange(n)*2*np.pi/max(1,n-1)
        return .21557895-.41663158*np.cos(a)+.277263158*np.cos(2*a)-.083578947*np.cos(3*a)+.006947368*np.cos(4*a)
    return np.hanning(n)

def fft_peak(x,fs,window='Blackman',window_size=None,beta=8.6):
    """평균을 뺀 뒤 창을 곱한 단측 peak. DC와 짝수 N Nyquist는 2배하지 않는다."""
    x=np.asarray(x,dtype=np.float64)
    n=len(x) if window_size is None else min(len(x),max(2,int(window_size)))
    if n<2: return np.array([]),np.array([])
    offset=(len(x)-n)//2
    x=x[offset:offset+n]
    x=x-np.mean(x)
    win=fft_window(n,window,beta)
    scale=win.sum()
    if scale<=0: raise ValueError('FFT 창 함수 합계가 0입니다.')
    mag=2*np.abs(np.fft.rfft(x*win))/scale
    mag[0]/=2
    if n%2==0: mag[-1]/=2
    return np.fft.rfftfreq(n,d=1/fs),mag


def srss_axis_power(peak, n, window):
    """One-sided coherent-gain peak spectrum -> window-power-corrected AC mean square.
    DC excluded; Nyquist is not a paired bin. Units follow the input peak amplitudes.
    """
    a=np.asarray(peak,dtype=float)
    w=np.asarray(window,dtype=float)
    if n<2 or len(w)!=n or len(a)!=n//2+1 or not np.all(np.isfinite(a)):
        raise ValueError('SRSS: FFT N/Window mismatch')
    den=n*float(np.sum(w*w))
    if den<=0: raise ValueError('SRSS: invalid window')
    weights=np.full(len(a),0.5); weights[0]=0.0
    if n%2==0: weights[-1]=1.0
    return float(np.sum(weights*a*a)*float(np.sum(w))**2/den)


def fft_roundtrip_diagnostic(samples, fs, gain=None):
    """FFT 후 IFFT가 원래 샘플을 복원하는지 검사한다."""
    x = np.asarray(samples, dtype=np.float64)
    fs = float(fs)
    if x.ndim != 1 or len(x) < 2 or fs <= 0 or not np.isfinite(fs) or not np.all(np.isfinite(x)):
        raise ValueError('유효한 RAW 샘플과 Fs가 필요합니다.')
    n = len(x)
    spectrum = np.fft.fft(x)
    reconstructed = np.fft.ifft(spectrum)
    f = np.fft.fftshift(np.fft.fftfreq(n, d=1.0/fs))
    shifted = np.fft.fftshift(spectrum)
    gain = float(n if gain is None else gain)
    if not np.isfinite(gain) or gain <= 0:
        raise ValueError('FFT 정규화 분모 오류')
    # Single-sided amplitude derived from the same complex FFT used for IFFT.
    # DC and (for even N) Nyquist are unique bins and are not doubled.
    single_f = np.fft.rfftfreq(n, d=1.0/fs)
    single_magnitude = np.abs(spectrum[:n//2+1])/gain
    if n % 2 == 0:
        single_magnitude[1:-1] *= 2.0
    else:
        single_magnitude[1:] *= 2.0
    error = reconstructed.real - x
    return dict(t=np.arange(n)/fs, x=x, f=f, magnitude=np.abs(shifted)/gain,
                single_f=single_f, single_magnitude=single_magnitude,
                real=shifted.real/gain, imag=shifted.imag/gain,
                restored=reconstructed.real, error=error,
                max_error=float(np.max(np.abs(error))),
                rms_error=float(np.sqrt(np.mean(error**2))),
                imag_error=float(np.max(np.abs(reconstructed.imag))),
                passed=bool(np.allclose(reconstructed.real,x,rtol=1e-10,atol=1e-8)),
                gain=gain, fs=fs, n=n)


def fft_diagnostic_text(d, label):
    """왕복 검사 결과를 한 줄 문장으로 만든다."""
    return (f'{label} | N={d["n"]:,} / Fs={d["fs"]:g} Hz | '
            f'IFFT {"PASS" if d["passed"] else "FAIL"} | '
            f'최대 오차={d["max_error"]:.3e} Count | RMS 오차={d["rms_error"]:.3e} | '
            f'잔류 허수={d["imag_error"]:.3e} | 양측=|FFT|/{d["gain"]:g} | 단측=2×양측 (DC/Nyquist 제외)')


def rotating_candidates(freqs, amps, rpm, min_count=2, max_count=48):
    """Receiver-style relative harmonic matching; exploratory, not a diagnosis."""
    f=np.asarray(freqs); a=np.asarray(amps)
    if rpm<=0 or len(f)<5: return []
    peaks=np.where((a[1:-1]>a[:-2])&(a[1:-1]>=a[2:]))[0]+1
    if len(peaks)==0: return []
    floor=np.median(a[a>0]) if np.any(a>0) else 0
    peaks=peaks[a[peaks]>=max(3*floor, .015*np.max(a))]
    peaks=peaks[np.argsort(a[peaks])[-120:]]
    df=float(np.median(np.diff(f))); rot=rpm/60
    results=[]
    for count in range(min_count,max_count+1):
        base=rot*count
        if base>=f[-1]-df: continue
        score=0.; matched=[]
        for h in range(1,min(5,int(f[-1]//base))+1):
            target=base*h; tol=max(1.5*df,.015*target)
            idx=peaks[np.abs(f[peaks]-target)<=tol]
            if len(idx)==0: continue
            j=idx[np.argmax(a[idx])]
            ratio=float(a[j]/max(np.max(a),1e-12))
            proximity=max(0,1-abs(f[j]-target)/tol)
            score+=(ratio**.5)*(.5+.5*proximity)/h**.6
            matched.append((h,round(float(f[j]),2)))
        if matched:
            score*=1 if any(h==1 for h,_ in matched) else .35
            score*=1+.18*min(len(matched)-1,3)
            results.append((score,count,base,matched))
    return sorted(results,reverse=True)[:5]


ADXL_RATE_TO_CODE = {
    "25": "0x08",
    "50": "0x09",
    "100": "0x0A",
    "200": "0x0B",
    "400": "0x0C",
    "800": "0x0D",
    "1600": "0x0E",
    "3200": "0x0F",
}

# FFT X축 기준표에 정의된 사용자 선택값.
# 마지막 조합이 장비 기본값이다.
PIEZO_SAMPLE_RATES = ("512", "1280", "2560", "5120", "12800", "25600")
ADXL_SAMPLE_RATES = ("100", "200", "400", "800", "1600", "3200")
SAMPLE_COUNTS = ("1024", "2048", "4096", "8192")

# UI에서는 Sample Rate와 Sample Count를 따로 고르지 않고
# "Fs + N" 하나의 세트로 선택한다.
PIEZO_SAMPLE_PROFILES = tuple(
    (rate, count, f"{rate} Hz + {count}")
    for rate in PIEZO_SAMPLE_RATES
    for count in SAMPLE_COUNTS
)
ADXL_SAMPLE_PROFILES = tuple(
    (rate, count, f"{rate} Hz + {count}")
    for rate in ADXL_SAMPLE_RATES
    for count in SAMPLE_COUNTS
)

PIEZO_PROFILE_LABELS = tuple(item[2] for item in PIEZO_SAMPLE_PROFILES)
ADXL_PROFILE_LABELS = tuple(item[2] for item in ADXL_SAMPLE_PROFILES)

PIEZO_PROFILE_MAP = {
    label: (rate, count)
    for rate, count, label in PIEZO_SAMPLE_PROFILES
}
ADXL_PROFILE_MAP = {
    label: (rate, count)
    for rate, count, label in ADXL_SAMPLE_PROFILES
}

ADXL_CODE_TO_RATE = {
    int(code, 16): float(rate)
    for rate, code in ADXL_RATE_TO_CODE.items()
}

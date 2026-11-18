"""필터, FFT, SRSS 전력, 샘플링 프로필."""

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
    # lp_cutoff=None은 '자동 Fs/2'다. 차단 주파수가 Nyquist라서 실제로는 걸 필터가 없다(v070과 같음).
    if lpf and lp_cutoff is not None:
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

# ---------- Window 보정 (수신기 V057, 발생기 v070) ----------
# FFT 진폭을 어떤 기준으로 나눌지 고른다. fft_peak는 원래 Σw로 나눈다(=진폭 보정).
#   보정 없음  : N으로 나눈다. 창 때문에 줄어든 진폭을 되돌리지 않는다.
#   진폭 보정  : Σw로 나눈다(coherent gain). 정현파 peak 크기가 맞는다. 기본값.
#   에너지 보정: √(N·Σw²)로 나눈다. 잡음처럼 넓게 퍼진 신호의 RMS가 맞는다.
# SRSS(srss_axis_power)는 원래 에너지 기준이라, '보정 없음'일 때만 srss_window_ratio로 되돌린다.
WINDOW_CORRECTIONS = ('보정 없음', '진폭 보정', '에너지 보정')
DEFAULT_WINDOW_CORRECTION = '진폭 보정'


def window_correction_mode(value):
    """화면에서 온 값을 세 모드 중 하나로. 모르는 값이면 기본(진폭 보정)."""
    text = str(value or '').strip()
    return text if text in WINDOW_CORRECTIONS else DEFAULT_WINDOW_CORRECTION


def window_correction_ratio(w, mode):
    """진폭 보정(Σw로 나눈) FFT 값에 곱할 배율. 진폭 보정이면 1.

    보정 없음: Σw/N, 에너지 보정: Σw/√(N·Σw²). 창 길이가 2 미만이면 1.
    """
    w = np.asarray(w, dtype=float)
    if len(w) < 2:
        return 1.0
    gain = float(np.sum(w))
    mode = window_correction_mode(mode)
    if mode == '보정 없음':
        return gain / len(w)
    if mode == '에너지 보정':
        return gain / np.sqrt(len(w) * float(np.sum(w * w)))
    return 1.0


def window_correction_label(w, mode):
    """화면에 보일 보정계수 문구. '보정 없음' FFT(N으로 나눈 값) 대비 실제 곱해진 배수다.

    예: Blackman N=8192 진폭 보정 → 'C=N/Σw = ×2.3810 (N=8192)'
    """
    w = np.asarray(w, dtype=float)
    n = len(w)
    if n < 2:
        return '보정계수: 데이터 대기'
    mode = window_correction_mode(mode)
    if mode == '보정 없음':
        formula, factor = 'C=1', 1.0
    elif mode == '에너지 보정':
        formula, factor = 'C=√(N/Σw²)', 1.0 / np.sqrt(np.mean(w * w))
    else:
        formula, factor = 'C=N/Σw', 1.0 / np.mean(w)
    if not (np.isfinite(factor) and factor > 0):
        return '보정계수: 유효하지 않은 Window'
    return f'{formula} = ×{factor:.4f} (N={n})'


def srss_window_ratio(w, mode):
    """SRSS에 곱할 배율. SRSS는 원래 에너지 보정 값이므로 '보정 없음'일 때만 √mean(w²)."""
    if window_correction_mode(mode) == '보정 없음':
        return float(np.sqrt(np.mean(np.square(np.asarray(w, dtype=float)))))
    return 1.0


def srss_mode_text(mode):
    """그래프 제목 끝에 붙일 SRSS 기준. 보정 없음이 아니면 모두 에너지 보정이다."""
    return '보정 없음' if window_correction_mode(mode) == '보정 없음' else '에너지 보정'


def fft_peak(x,fs,window='Blackman',window_size=None,beta=8.6,correction=DEFAULT_WINDOW_CORRECTION):
    """평균을 뺀 뒤 창을 곱한 단측 peak. DC와 짝수 N Nyquist는 2배하지 않는다.

    correction은 Window 보정 모드다. 기본 '진폭 보정'은 예전과 같은 값(Σw로 나눔)이라,
    인자를 넘기지 않는 통계 계산(stats_calc)과 SRSS는 결과가 바뀌지 않는다.
    """
    x=np.asarray(x,dtype=np.float64)
    n=len(x) if window_size is None else min(len(x),max(2,int(window_size)))
    if n<2: return np.array([]),np.array([])
    offset=(len(x)-n)//2
    x=x[offset:offset+n]
    x=x-np.mean(x)
    win=fft_window(n,window,beta)
    scale=win.sum()
    if scale<=0: raise ValueError('FFT 창 함수 합계가 0입니다.')
    mag=2*np.abs(np.fft.rfft(x*win))/scale*window_correction_ratio(win,correction)
    mag[0]/=2
    if n%2==0: mag[-1]/=2
    return np.fft.rfftfreq(n,d=1/fs),mag


def welch_packet_psd(samples, fs, window='Blackman', segment=None, overlap=50, beta=8.6, max_segments=None):
    """한 패킷의 Welch PSD(단측 밀도). 발생기 v070의 welch_packet_psd와 같은 계산이다.

    - 단위: 입력 단위²/Hz. Count를 넣으면 Count²/Hz, g를 넣으면 g²/Hz.
    - segment: 구간 길이 N. None이면 패킷 전체가 한 구간이다. 패킷보다 길면 오류.
    - overlap: 구간 겹침 %(0 이상 100 미만). 이동 간격 = N − int(N×overlap/100).
    - max_segments: 이 패킷에서 쓸 구간 수 상한(None이면 들어가는 만큼 전부).
    - 구간마다 평균을 빼고 창 w를 곱한 뒤 P = |rFFT|² / (Fs × Σw²).
      0 Hz와 짝수 N의 Nyquist를 뺀 칸은 단측이라 ×2.
    - 끝에 남는 짧은 구간은 0을 채우지 않고 버린다(전체 길이 구간만 사용).
    - 창 에너지(Σw²)로 나누므로 Window 보정 모드와 무관하다. 적분 √(ΣP×Δf)가 그 신호의 RMS다.

    돌려주는 값: (주파수 Hz 배열, 구간 평균 PSD 배열, 실제 사용한 구간 수).
    """
    x=np.asarray(samples,dtype=float)
    n=len(x) if segment is None else int(segment)
    if not np.isfinite(fs) or fs<=0 or n<2 or n>len(x):
        raise ValueError('PSD 구간 N은 2 이상, 패킷 샘플 수 이하여야 합니다.')
    if not np.isfinite(overlap) or not 0<=overlap<100:
        raise ValueError('Overlap은 0 이상 100 미만이어야 합니다.')
    w=fft_window(n,window,beta); power=float(np.dot(w,w))
    if power<=0 or not np.isfinite(power): raise ValueError('PSD Window 에너지 오류')
    step=max(1,n-int(n*overlap/100)); total=np.zeros(n//2+1); count=0
    for start in range(0,len(x)-n+1,step):
        if max_segments is not None and count >= max_segments: break
        z=x[start:start+n]; z=z-np.mean(z)
        p=np.abs(np.fft.rfft(z*w))**2/(fs*power)
        if n%2==0: p[1:-1]*=2
        else: p[1:]*=2
        total+=p; count+=1
    if count==0:
        # max_segments=0처럼 한 구간도 못 쓴 경우. v070은 0으로 나눠 nan이 되지만 여기서는 오류로 알린다.
        raise ValueError('PSD 구간을 하나도 쓰지 못했습니다.')
    return np.fft.rfftfreq(n,1/fs),total/count,count


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

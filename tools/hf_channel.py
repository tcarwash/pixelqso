"""Seeded audio HF channel; simplified Gaussian-scatter tapped-delay model."""
from __future__ import annotations
import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.signal import hilbert
from scipy.io import wavfile

@dataclass(frozen=True)
class Channel:
    delay_ms: float = 0
    doppler_sigma_hz: float = 0
    fading: bool = False

# Illustrative severity presets, not certified ITU test profiles.
PROFILES = {
    'awgn': Channel(),
    'hf_mild': Channel(.5, .1, True),
    'hf_moderate': Channel(2, .5, True),
    'hf_severe': Channel(4, 2, True),
}

def fading_gain(n, rate, sigma_hz, rng):
    """Unit ensemble-power complex Gaussian process, Gaussian Doppler PSD.

    Generate at >=64 samples/Doppler sigma then interpolate to audio rate.
    Extra margins remove filter startup; no per-capture gain normalization.
    """
    low_rate = max(32., 64 * sigma_hz)
    width = low_rate / (2 * np.sqrt(2) * np.pi * sigma_hz)
    margin = int(np.ceil(5 * width))
    count = int(np.ceil((n - 1) / rate * low_rate)) + 2
    z = (rng.normal(size=count + 2 * margin) +
         1j * rng.normal(size=count + 2 * margin)) / np.sqrt(2)
    kernel_x = np.arange(-int(4 * width + .5), int(4 * width + .5) + 1)
    kernel = np.exp(-.5 * (kernel_x / width) ** 2)
    kernel /= kernel.sum()
    z = (gaussian_filter1d(z.real, width) +
         1j * gaussian_filter1d(z.imag, width)) / np.sqrt(np.sum(kernel ** 2))
    z = z[margin:margin + count]
    t = np.arange(n) / rate * low_rate
    return np.interp(t, np.arange(count), z.real) + 1j * np.interp(t, np.arange(count), z.imag)

def simulate(audio, rate, *, profile='hf_moderate', snr_db=10., seed=1,
             bandwidth_hz=2500., cfo_hz=0., clock_ppm=0., drift_hz_per_second=0.):
    """SNR uses input power / noise power in bandwidth_hz, before fading.

    No clipping/AGC or per-realization normalization. Returned float audio can
    exceed unity. Full echo tail retained; 0.3 s noise guards added each end.
    Frequency offset is cfo_hz + drift_hz_per_second*t, with t measured from
    the start of the guarded capture, before clock interpolation.
    """
    if rate <= 0 or not 0 < bandwidth_hz <= rate / 2:
        raise ValueError('invalid sample rate or noise reference bandwidth')
    if not all(np.isfinite(v) for v in (snr_db, cfo_hz, clock_ppm, drift_hz_per_second)) or abs(clock_ppm) >= 1e6:
        raise ValueError('invalid SNR, offset or clock error')
    x = np.asarray(audio, dtype=float)
    if x.ndim != 1 or not len(x) or not np.all(np.isfinite(x)):
        raise ValueError('expected nonempty finite mono audio')
    cfg = PROFILES[profile]
    rng = np.random.default_rng(seed)
    power = float(np.mean(x*x))
    delay = round(rate * cfg.delay_ms / 1000)
    guard = round(.3 * rate)
    analytic = hilbert(np.pad(x, (guard, guard + delay)))
    if cfg.fading:
        y = np.zeros(len(analytic), complex)
        for d in (0, delay):
            path = analytic if d == 0 else np.pad(analytic[:-d], (d, 0))
            y += path * fading_gain(len(path), rate, cfg.doppler_sigma_hz, rng) / np.sqrt(2)
    else:
        y = analytic
    t = np.arange(len(y))/rate
    y *= np.exp(2j*np.pi*(cfo_hz*t+.5*drift_hz_per_second*t*t))
    received = y.real
    if clock_ppm:
        received = np.interp(np.arange(0, len(y), 1 + clock_ppm * 1e-6),
                             np.arange(len(y)), received)
    noise_std = np.sqrt(power * 10 ** (-snr_db / 10) * rate / (2 * bandwidth_hz))
    received += rng.normal(0, noise_std, len(received))
    return received.astype(np.float32), {
        **asdict(cfg), 'profile': profile, 'seed': seed, 'sample_rate': rate,
        'snr_db': snr_db, 'snr_reference_hz': bandwidth_hz,
        'input_power': power, 'noise_std': float(noise_std),
        'cfo_hz': cfo_hz, 'clock_ppm': clock_ppm,
        'drift_hz_per_second': drift_hz_per_second,
        'snr_reference': 'input power before fading; noise added after channel',
    }

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--profile', choices=PROFILES, default='hf_moderate')
    p.add_argument('--snr-db', type=float, default=10)
    p.add_argument('--seed', type=int, default=1)
    p.add_argument('--cfo-hz', type=float, default=0)
    p.add_argument('--clock-ppm', type=float, default=0)
    p.add_argument('--drift-hz-per-second', type=float, default=0)
    p.add_argument('--bandwidth-hz', type=float, default=2500)
    a = p.parse_args()
    rate, x = wavfile.read(a.input)
    if x.dtype.kind in 'iu':
        info = np.iinfo(x.dtype)
        x = ((x.astype(float) - 128) / 128 if x.dtype == np.uint8
             else x.astype(float) / max(abs(info.min), info.max))
    y, meta = simulate(x, rate, profile=a.profile, snr_db=a.snr_db, seed=a.seed,
                       cfo_hz=a.cfo_hz, clock_ppm=a.clock_ppm, bandwidth_hz=a.bandwidth_hz,
                       drift_hz_per_second=a.drift_hz_per_second)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(a.output, rate, y)
    a.output.with_suffix('.channel.json').write_text(json.dumps(meta, indent=2) + '\n')
    print(a.output)

if __name__ == '__main__':
    main()

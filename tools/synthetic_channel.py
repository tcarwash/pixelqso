"""Shared, explicitly calibrated synthetic receive filtering and AWGN."""
from __future__ import annotations

import hashlib
import math

import numpy as np


class ReceiveFilter:
    """Windowed-sinc audio bandpass; compensate its known FIR delay offline."""

    def __init__(self, sample_rate, low_hz=100., high_hz=3100., taps=1025):
        if not (math.isfinite(sample_rate) and sample_rate > 0
                and math.isfinite(low_hz) and math.isfinite(high_hz)
                and 0 <= low_hz < high_hz < sample_rate / 2):
            raise ValueError("receive edges must satisfy 0 <= low < high < Nyquist")
        if taps < 3 or taps % 2 != 1:
            raise ValueError("receive FIR taps must be odd and at least 3")
        n = np.arange(taps) - taps // 2
        upper = 2 * high_hz / sample_rate * np.sinc(2 * high_hz / sample_rate * n)
        lower = 2 * low_hz / sample_rate * np.sinc(2 * low_hz / sample_rate * n)
        h = (upper - lower) * np.kaiser(taps, 8.6)
        center = (low_hz + high_hz) / 2
        center_gain = abs(np.sum(h * np.exp(-2j * np.pi * center * n / sample_rate)))
        self.coefficients = h / center_gain
        # Real white noise variance after the FIR is sigma² * sum(h²).
        self.enbw_hz = sample_rate / 2 * float(np.sum(self.coefficients ** 2))
        self.spec = {"low_hz": low_hz, "high_hz": high_hz,
                     "nominal_bandwidth_hz": high_hz - low_hz,
                     "equivalent_noise_bandwidth_hz": self.enbw_hz,
                     "taps": taps, "window": "Kaiser beta=8.6",
                     "delay_compensated_samples": taps // 2,
                     "coefficients_sha256": hashlib.sha256(self.coefficients.tobytes()).hexdigest()}

    def apply(self, samples):
        if not len(samples):
            raise ValueError("cannot filter empty audio")
        size = len(samples) + len(self.coefficients) - 1
        fft_size = 1 << (size - 1).bit_length()
        result = np.fft.irfft(np.fft.rfft(samples, fft_size) *
                              np.fft.rfft(self.coefficients, fft_size), fft_size)
        delay = len(self.coefficients) // 2
        return result[delay:delay + len(samples)]


def noise_sigma(snr_db, reference_hz, sample_rate, signal_power=.01):
    """White-sample sigma for signal power / noise in reference_hz, not Fs/2."""
    if not (math.isfinite(sample_rate) and sample_rate > 0
            and math.isfinite(reference_hz) and 0 < reference_hz <= sample_rate / 2
            and math.isfinite(signal_power) and signal_power >= 0):
        raise ValueError("invalid sample rate, noise reference, or signal power")
    if snr_db is None:
        return 0.
    if not math.isfinite(snr_db):
        raise ValueError("SNR must be finite")
    return math.sqrt(signal_power * (sample_rate / 2) / reference_hz) * 10 ** (-snr_db / 20)

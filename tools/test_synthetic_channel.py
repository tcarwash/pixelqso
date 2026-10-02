"""Numerical regressions for SNR calibration and shared receive filtering."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import modem_benchmark as benchmark
from synthetic_channel import ReceiveFilter, noise_sigma
from two_client_loopback import impair


class SyntheticChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fs = 48000
        cls.filter = ReceiveFilter(cls.fs)
        cls.sine = np.sin(2 * np.pi * 1600 * np.arange(cls.fs * 8) / cls.fs)

    def test_reference_calibration_including_negative_snr(self):
        for snr in (-6, 0, 6):
            with self.subTest(snr=snr):
                audio, measures = benchmark.channel(self.sine, {}, snr, 7331,
                                                     rx_filter=self.filter)
                self.assertAlmostEqual(measures['measured_snr_db_reference'], snr, delta=.15)
                self.assertEqual(measures['snr_db_2500'], snr)
                self.assertEqual(len(audio), len(self.sine) + 2 * self.fs)
                self.assertTrue(np.isfinite(audio).all())

    def test_full_band_reference_offset_preserves_noise_psd(self):
        calibrated, _ = benchmark.channel(self.sine, {}, -3, 7331, rx_filter=self.filter)
        sampled_snr = -3 + 10 * np.log10(2500 / 24000)
        equivalent, _ = benchmark.channel(self.sine, {}, sampled_snr, 7331,
                                          snr_reference_hz=24000, rx_filter=self.filter)
        np.testing.assert_allclose(calibrated, equivalent, atol=1e-7)

    def test_enbw_matches_numerically_integrated_frequency_response(self):
        fft_size = 65536
        power = abs(np.fft.rfft(self.filter.coefficients, fft_size)) ** 2
        numerical = (power.sum() - (power[0] + power[-1]) / 2) * self.fs / fft_size
        self.assertAlmostEqual(self.filter.enbw_hz, numerical, places=8)

    def test_filter_preserves_modem_tones_and_rejects_out_of_band_noise(self):
        n = np.arange(len(self.filter.coefficients))
        for hz in (900, 2300):
            gain = abs(np.sum(self.filter.coefficients * np.exp(-2j * np.pi * hz * n / self.fs)))
            self.assertAlmostEqual(gain, 1, delta=.001)
        gain = abs(np.sum(self.filter.coefficients * np.exp(-2j * np.pi * 6000 * n / self.fs)))
        self.assertLess(gain, .001)

    def test_seeded_channel_is_repeatable(self):
        one, measures = benchmark.channel(self.sine, {}, -3, 7331, rx_filter=self.filter)
        two, measures2 = benchmark.channel(self.sine, {}, -3, 7331, rx_filter=self.filter)
        np.testing.assert_array_equal(one, two)
        self.assertEqual(measures, measures2)

    def test_loopback_uses_reference_calibration_without_mutating_input(self):
        x = (.1 * np.sqrt(2) * self.sine).astype(np.float32)
        original = x.copy()
        common = dict(cfo_hz=0, clock_ppm=0, drop_start=0, drop_end=0, seed=7331,
                      rx_filter=self.filter)
        clean = impair(x, snr_db=None, **common)
        noisy = impair(x, snr_db=-3, **common)
        noise_power = np.mean((noisy[1000:-1000] - clean[1000:-1000]) ** 2)
        signal_power = np.mean(clean[1000:-1000] ** 2)
        measured = 10 * np.log10(signal_power / (noise_power * 2500 / self.filter.enbw_hz))
        self.assertAlmostEqual(measured, -3, delta=.15)
        np.testing.assert_array_equal(x, original)

    def test_invalid_filter_and_noise_reference_are_rejected(self):
        for low, high in ((-1, 3100), (3100, 100), (100, 24000)):
            with self.assertRaises(ValueError):
                ReceiveFilter(self.fs, low, high)
        for reference in (0, -1, float('nan'), 24001):
            with self.assertRaises(ValueError):
                noise_sigma(0, reference, self.fs)


if __name__ == '__main__':
    unittest.main()

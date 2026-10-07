import unittest
import numpy as np
from tools.hf_channel import simulate, fading_gain

class HFChannelTests(unittest.TestCase):
    def test_linear_drift_is_reproducible_and_retains_calibration_metadata(self):
        rate=8000
        x=.1*np.sin(2*np.pi*1000*np.arange(rate*2)/rate)
        y,meta=simulate(x,rate,profile='awgn',snr_db=120,seed=2,
                        cfo_hz=17,drift_hz_per_second=-20)
        other,_=simulate(x,rate,profile='awgn',snr_db=120,seed=2,
                         cfo_hz=17,drift_hz_per_second=-20)
        np.testing.assert_array_equal(y,other)
        from scipy.signal import hilbert
        frequency=np.diff(np.unwrap(np.angle(hilbert(y))))*rate/(2*np.pi)
        for seconds in (.6,1.6):
            at=round(seconds*rate)
            self.assertAlmostEqual(float(np.median(frequency[at-100:at+100])),
                                   1000+17-20*seconds,delta=.5)
        self.assertEqual(meta['drift_hz_per_second'],-20)
        self.assertEqual(meta['snr_reference_hz'],2500)

    def test_awgn_calibration_and_repeatability(self):
        rate = 8000
        x = .1 * np.sin(2*np.pi*1000*np.arange(rate*10)/rate)
        y, meta = simulate(x, rate, profile='awgn', snr_db=10, seed=4)
        y2, _ = simulate(x, rate, profile='awgn', snr_db=10, seed=4)
        np.testing.assert_array_equal(y, y2)
        clean = np.pad(x, (2400, 2400))
        self.assertAlmostEqual(np.var(y-clean), meta['noise_std']**2, delta=meta['noise_std']**2*.02)

    def test_fading_ensemble_power_and_correlation(self):
        g = fading_gain(200000, 100, .5, np.random.default_rng(7))
        self.assertAlmostEqual(float(np.mean(abs(g)**2)), 1, delta=.15)
        correlation = np.mean(g[20:] * g[:-20].conj()).real / np.mean(abs(g)**2)
        expected = np.exp(-2*np.pi**2*.5**2*.2**2)
        self.assertAlmostEqual(correlation, expected, delta=.06)

    def test_echo_tail_and_finite_audio(self):
        x = np.sin(2*np.pi*1000*np.arange(8000)/8000)
        y, _ = simulate(x, 8000, profile='hf_moderate', seed=8)
        self.assertEqual(len(y), len(x)+4800+16)
        self.assertTrue(np.all(np.isfinite(y)))

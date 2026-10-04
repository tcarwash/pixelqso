"""Wire, DSP and integrity regressions for the experimental modem."""
import sys
import hashlib
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cardmodem as pixel
import weak_signal_modem as weak
from modem_benchmark import channel, PROFILES


class WeakSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.card = pixel.example_card()
        cls.audio = weak.encode(cls.card, message_type='exchange', snr_db=-3)
        cls.expected = pixel.unpack_indices(pixel.minimal_avatar_payload(cls.card), 1024)

    def test_packet_size_and_crc(self):
        body = weak.packet(self.card)
        self.assertEqual(len(body)*8, weak.K)
        bits = np.unpackbits(np.frombuffer(body, np.uint8)) ^ weak.constants()[3]
        self.assertEqual(weak.unpack(bits, {})[0], self.expected)
        bits[1000] ^= 1
        with self.assertRaisesRegex(ValueError, 'CRC'):
            weak.unpack(bits, {})

    def test_frozen_wire_sequences(self):
        expected = ['afb0b022e01b13d309d947f2ef55e258c8d52e7fcba2bf2360d1d48c2cd7c8f8',
                    '20694608c77587bc0af4d114197100f0f6e2df15a0401eeb28178736be8287c7',
                    '11b15a8003ccc6caa7cac13af2de18cc7fee0f5bca0ec425d7733083664984c1',
                    '47cf26c22bf2f1e36747bf549534f3bda6f88091a82b4d40c0b7c8131f3869c4']
        for array, digest in zip(weak.constants()[:4], expected):
            canonical = array.astype('<c16' if array.dtype.kind == 'c' else '<i8')
            self.assertEqual(hashlib.sha256(canonical.tobytes()).hexdigest(), digest)

    def test_clean_waveform_and_metadata(self):
        actual, metadata = weak.decode(self.audio)
        self.assertEqual(actual, self.expected)
        self.assertEqual(metadata['message_type'], 'exchange')
        self.assertEqual(metadata['snr_db'], -3)
        self.assertTrue(metadata['exact'])
        self.assertEqual(metadata['card_id'], self.card.card_id)
        self.assertEqual(metadata['callsign'], self.card.callsign)
        self.assertEqual(metadata['grid'], self.card.grid)
        self.assertEqual((metadata['width'], metadata['height']), (32,32))

    def test_sub_noise_floor_frequency_and_clock_offsets(self):
        for seed in (801, 802, 803):
            with self.subTest(seed=seed):
                audio, _ = channel(self.audio, PROFILES['tuning'], -3, seed)
                actual, metadata = weak.decode(audio)
                self.assertEqual(actual, self.expected)
                self.assertAlmostEqual(metadata['diagnostics']['carrier_offset_hz'], 80, delta=1)

    def test_arbitrary_start_phase_and_rate(self):
        audio = np.pad(self.audio, (1237, 991))
        self.assertEqual(weak.decode(audio)[0], self.expected)
        from scipy.signal import resample_poly
        self.assertEqual(weak.decode(resample_poly(audio, 2, 3), 32000)[0], self.expected)

    def test_no_signal_and_truncated_burst_are_rejected(self):
        for audio in (np.zeros(48000*6), self.audio[:48000*2],
                      np.random.default_rng(33).normal(0,.1,48000*6)):
            with self.assertRaises(ValueError):
                weak.decode(audio)

    def test_duration_and_spectral_budget(self):
        self.assertEqual(len(self.audio)/48000, weak.seconds())
        self.assertLessEqual(weak.seconds()+.350, 5)
        frequencies = np.fft.rfftfreq(len(self.audio), 1/48000)
        power = abs(np.fft.rfft(self.audio))**2
        within = power[(frequencies >= 250) & (frequencies <= 2750)].sum()/power.sum()
        self.assertGreater(within, .999)

    def test_unsupported_dimensions_and_copies(self):
        with self.assertRaises(ValueError):
            weak.encode(self.card, 2)
        card = pixel.Card('W7PXQ', 'CN87', self.card.palette, [0]*256, width=16, height=16)
        with self.assertRaises(ValueError):
            weak.encode(card)

    def test_soft_combining_recovers_below_single_burst_threshold(self):
        audio, _ = channel(np.tile(self.audio, 4), PROFILES['tuning'], -9, 801)
        actual, metadata = weak.decode(audio)
        self.assertEqual(actual, self.expected)
        self.assertTrue(metadata['exact'])
        self.assertGreater(metadata['diagnostics']['combined_copies'], 1)
        single, _ = channel(self.audio, PROFILES['tuning'], -9, 801)
        with self.assertRaises(ValueError):
            weak.decode(single)

    def test_combining_does_not_accept_different_payloads_as_one_card(self):
        other = pixel.example_card()
        other.pixels = [(value + 1) % 8 for value in other.pixels]
        other.card_id = (other.card_id + 1) & 65535
        different = weak.encode(other, message_type='exchange', snr_db=-3)
        audio, _ = channel(np.concatenate((self.audio, different)), {}, -9, 805)
        with self.assertRaises(ValueError):
            weak.decode(audio)

    def test_random_payload_ldpc(self):
        rng = np.random.default_rng(907)
        bits = rng.integers(0,2,weak.K,dtype=np.uint8)
        transmitted = weak.code().encode(bits[None,:])[0].astype(float)
        decoded, _ = weak.decode_ldpc((1-2*transmitted)*8 + rng.normal(0,1,weak.N))
        np.testing.assert_array_equal(bits, decoded)


if __name__ == '__main__':
    unittest.main()

import unittest

import numpy as np

import cardmodem as pixel
from card_backends import get_backend, is_available


class NarrowModeTests(unittest.TestCase):
    def test_placement_preserves_duration_and_checked_payload(self):
        card = pixel.example_card()
        expected = pixel.unpack_indices(pixel.minimal_avatar_payload(card), 1024)
        for baud in (100, 50, 25):
            for placement in ("centered", 300, 2200):
                with self.subTest(baud=baud, placement=placement):
                    backend = get_backend(f"resilient_{baud}", placement)
                    tones = backend.profile.tones_hz
                    self.assertAlmostEqual(tones[-1] - tones[0], 7 * baud)
                    if placement == "centered":
                        self.assertEqual((tones[0] + tones[-1]) / 2, 1500)
                    else:
                        self.assertEqual(tones[0], placement)
                    audio = backend.encode(card, 1, "cq", None)
                    self.assertAlmostEqual(len(audio) / 48000, 1236 / baud)
                    audio = np.pad(audio, (12000, 12000))
                    audio += np.random.default_rng(10).normal(0, .05, len(audio))
                    report = backend.decode(audio, 48000)
                    self.assertTrue(report["card"]["exact"])
                    self.assertEqual(report["pixels"], expected)
                    self.assertEqual(report["card"]["audio_tones_hz"], list(tones))
        # Placement changes produce independent instances; no station can
        # move another station's waveform by mutating the registry.
        self.assertEqual(get_backend("resilient_25").profile.tones_hz[0], 25)
        for invalid in (-1, 0, float("nan"), float("inf"), 2201):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                get_backend("resilient_100", invalid)

    def test_checked_card_after_silence_and_noise(self):
        card = pixel.example_card()
        expected = pixel.unpack_indices(pixel.minimal_avatar_payload(card), 1024)
        for baud in (100, 50, 25):
            with self.subTest(baud=baud):
                backend = get_backend(f"resilient_{baud}")
                self.assertTrue(is_available(backend.key))
                self.assertEqual(backend.profile.tones_hz, tuple(range(baud, 9 * baud, baud)))
                audio = backend.encode(card, 1, "exchange", -7)
                self.assertAlmostEqual(len(audio) / 48000, 1236 / baud)
                audio = np.pad(audio, (12000, 12000))
                audio += np.random.default_rng(4).normal(0, .05, len(audio))
                report = backend.decode(audio, 48000)
                self.assertTrue(report["card"]["exact"])
                self.assertEqual(report["pixels"], expected)
                for field, value in (("callsign", card.callsign), ("grid", card.grid),
                                     ("card_id", card.card_id), ("message_type", "exchange"),
                                     ("snr_db", -7), ("avatar_mode", backend.key)):
                    self.assertEqual(report["card"][field], value)

    def test_near_carrier_with_frequency_offset(self):
        card = pixel.example_card()
        expected = pixel.unpack_indices(pixel.minimal_avatar_payload(card), 1024)
        symbols = pixel.minimal_avatar_resilient_cycle_symbols(card, message_type="cq")
        for baud in (100, 50, 25):
            backend = get_backend(f"resilient_{baud}")
            for offset in (-7, 7):
                with self.subTest(baud=baud, offset=offset):
                    audio = pixel.synthesize(symbols, profile=backend.profile,
                                             frequency_offset_hz=offset)
                    audio = np.pad(audio, (12000, 12000))
                    audio += np.random.default_rng(7).normal(0, .05, len(audio))
                    report = backend.decode(audio, 48000)
                    self.assertTrue(report["card"]["exact"])
                    self.assertEqual(report["pixels"], expected)

    def test_lowest_mode_clean_8khz_with_leading_silence(self):
        card = pixel.example_card()
        backend = get_backend("resilient_25")
        symbols = pixel.minimal_avatar_resilient_cycle_symbols(card)
        for offset in (-7, 7):
            with self.subTest(offset=offset):
                audio = pixel.synthesize(symbols, sample_rate=8000, profile=backend.profile,
                                         frequency_offset_hz=offset)
                report = backend.decode(np.pad(audio, (2000, 2000)), 8000)
                self.assertTrue(report["card"]["exact"])
                self.assertEqual(report["pixels"],
                                 pixel.unpack_indices(pixel.minimal_avatar_payload(card), 1024))


if __name__ == "__main__":
    unittest.main()

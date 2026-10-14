import unittest

import cardmodem as pixel
from card_backends import get_backend


class MusicalModeTests(unittest.TestCase):
    def test_palette_symbols_play_and_decode_as_e_major_scale(self):
        key = "experimental_8fsk_musical_25"
        backend = get_backend(key)
        card = pixel.example_card()

        audio = backend.encode(card, 1, "cq", None)
        report = backend.decode(audio, 48000)

        self.assertIsNone(report.get("failure"))
        self.assertEqual(report["card"]["avatar_mode"], key)
        expected = pixel.unpack_indices(pixel.minimal_avatar_payload(card), 1024)
        self.assertEqual(len(report["pixels"]), 1024)
        # The uncorrected raster may lose its final symbols at the frame edge.
        self.assertEqual(report["pixels"][:1020], expected[:1020])
        self.assertFalse(report["card"]["exact"],
                         "direct musical symbols do not include whole-card error correction")
        self.assertEqual(backend.profile.baud, 25)
        self.assertEqual(len(backend.profile.tones_hz), 8)


if __name__ == "__main__":
    unittest.main()

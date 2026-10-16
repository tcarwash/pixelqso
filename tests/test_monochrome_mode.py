import unittest

import cardmodem
from card_backends import get_backend


class MonochromeModeTests(unittest.TestCase):
    def test_mode_thresholds_color_card_and_encodes_checked_compact_raster(self):
        card = cardmodem.example_card()
        mono = cardmodem.monochrome_card(card)
        self.assertEqual(len(mono.palette), 2)
        self.assertEqual(set(mono.pixels), {0, 1})
        self.assertEqual(len(cardmodem.minimal_avatar_payload(mono)), 128)
        self.assertLess(len(cardmodem.minimal_avatar_payload(mono)),
                        len(cardmodem.minimal_avatar_payload(card)))

        backend = get_backend("experimental_8fsk_mono_fast")
        audio = backend.encode(card, 1, "card", None)
        self.assertLess(len(audio) / cardmodem.SAMPLE_RATE, 6.0)
        report = backend.decode(audio, cardmodem.SAMPLE_RATE)
        self.assertTrue(report["card"]["exact"])
        self.assertEqual(report["card"]["palette"], [list(c) for c in mono.palette])
        self.assertEqual(report["pixels"], mono.pixels)

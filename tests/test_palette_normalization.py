import unittest

import cardmodem as modem
from app import normalize_card_palette


class PaletteNormalizationTests(unittest.TestCase):
    def test_two_color_card_maps_to_shared_palette(self):
        card = modem.Card("N0CALL", "AA00", [(0, 0, 0), (255, 255, 255)], [0, 1] * 512)
        normalized = normalize_card_palette(card)
        self.assertEqual(len(normalized.palette), 8)
        self.assertEqual(normalized.pixels, [0, 1] * 512)
        normalized.validate()

    def test_existing_protocol_palette_is_unchanged(self):
        card = modem.example_card()
        self.assertIs(normalize_card_palette(card), card)

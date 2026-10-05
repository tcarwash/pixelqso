import unittest
from types import SimpleNamespace

from PySide6.QtGui import QColor

import cardmodem as pixel
from app import MainWindow
from card_backends import get_backend


def preview_state():
    return SimpleNamespace(live_preview_states={}, live_preview_key=None,
                           live_preview_state=None, live_preview_sequence=0,
                           live_preview_window=1)


class LivePreviewTests(unittest.TestCase):
    def test_crc_failed_fec_block_stays_visible_as_unverified_preview(self):
        source = pixel.example_card()
        symbols = list(pixel.minimal_avatar_resilient_cycle_symbols(source, message_type="cq"))
        first_block = len(pixel.FRAME_SYNC) + pixel.AVATAR_META_TONE_COUNT
        for gf_symbol in (0, 1, 2, 3):
            symbols[first_block + gf_symbol * 2] ^= 1
        audio = pixel.synthesize(symbols, profile=pixel.MINIMAL_AVATAR_PROFILE)
        decoded = get_backend("fast_avatar_fec").decode(audio, 48000)

        state = preview_state()
        report, changed = MainWindow._merge_live_preview(state, decoded)
        card = report["card"]
        pixels_per_block = (61 * 6) // card["bits_per_pixel"]

        self.assertTrue(changed)
        self.assertFalse(card["received_blocks"][0], "CRC failure must not verify the block")
        self.assertTrue(all(card["pixel_coverage"][:pixels_per_block]),
                        "received candidate pixels should remain visible")
        self.assertFalse(any(state.live_preview_state["verified_pixels"][:pixels_per_block]))
        image = MainWindow.card_image(None, report)
        self.assertIsNotNone(image)
        for index in range(pixels_per_block):
            rgb = card["palette"][report["pixels"][index]]
            expected = tuple(int(channel) * 17 for channel in rgb)
            actual = image.pixelColor(index % card["width"], index // card["width"])
            self.assertEqual((actual.red(), actual.green(), actual.blue()), expected)

    def test_unverified_whole_card_pixels_are_previewed_not_promoted(self):
        card = {"callsign": "AG7SU", "grid": "CN85", "card_id": 0x1234,
                "width": 4, "height": 2, "palette": [[0, 0, 0], [15, 0, 0]],
                "avatar_burst": True, "exact": False,
                "pixel_coverage": [True] * 8, "received_copies": 1}
        report, changed = MainWindow._merge_live_preview(
            preview_state(), {"card": card, "pixels": [1] * 8})

        self.assertTrue(changed)
        self.assertFalse(report["card"]["exact"])
        self.assertTrue(all(report["card"]["pixel_coverage"]))
        self.assertEqual(report["pixels"], [1] * 8)
        image = MainWindow.card_image(None, report)
        self.assertEqual(QColor(image.pixelColor(0, 0)).name(), "#ff0000")

    def test_new_unverified_pixels_appear_as_more_data_blocks_arrive(self):
        state = preview_state()
        card = {"callsign": "AG7SU", "grid": "CN85", "card_id": 0x1234,
                "width": 4, "height": 2, "palette": [[0, 0, 0], [15, 0, 0]],
                "avatar_burst": True, "exact": False,
                "pixel_coverage": [True] * 4 + [False] * 4, "received_copies": 1}
        first, _ = MainWindow._merge_live_preview(
            state, {"card": card, "pixels": [1] * 8})
        self.assertEqual(first["pixels"], [1] * 4 + [0] * 4)

        # A later Data2G fragment adds the second half of the same image. It
        # does not increment copy count, but its newly covered pixels are still
        # useful provisional preview data.
        later_card = {**card, "pixel_coverage": [True] * 8}
        later, changed = MainWindow._merge_live_preview(
            state, {"card": later_card, "pixels": [1] * 8})

        self.assertTrue(changed)
        self.assertEqual(later["pixels"], [1] * 8)
        self.assertFalse(any(state.live_preview_state["verified_pixels"]))
        image = MainWindow.card_image(None, later)
        self.assertEqual(QColor(image.pixelColor(0, 1)).name(), "#ff0000")


if __name__ == "__main__":
    unittest.main()

import unittest

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QColor

from qr_support import make_qr_card, read_qr_image


class QRSupportTests(unittest.TestCase):
    def test_qr_card_is_monochrome_and_decodes_after_pixel_scaling(self):
        card = make_qr_card("HELLO", "AG7SU", "CN85", 1234, 32, 32)
        self.assertEqual(len(card.palette), 2)
        self.assertEqual(set(card.pixels), {0, 1})
        card.validate()

        image = QImage(card.width, card.height, QImage.Format.Format_RGB32)
        for index, pixel in enumerate(card.pixels):
            image.setPixelColor(index % card.width, index // card.width,
                                QColor(0, 0, 0) if pixel == 0 else QColor(255, 255, 255))
        self.assertEqual(read_qr_image(image), "HELLO")

    def test_qr_rejects_payload_that_cannot_fit_card(self):
        with self.assertRaisesRegex(ValueError, "needs at least"):
            make_qr_card("x" * 80, "AG7SU", "CN85", 1234, 32, 32)

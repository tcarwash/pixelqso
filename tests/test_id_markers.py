import os
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

import cardmodem as pixel
from card_backends import get_backend
from transmit_preview import LocalTransmitPreview


class IdMarkerTests(unittest.TestCase):
    def test_unframed_narrow_card_is_checked_and_shorter(self):
        card = pixel.Card("N0CALL", "AA00", list(pixel.MINIMAL_AVATAR_PALETTE),
                          [index % 8 for index in range(16)], width=4, height=4)
        for baud in (10, 5):
            with self.subTest(baud=baud):
                backend = get_backend(f"resilient_{baud}")
                audio = backend.encode(card, 1, "cq", None, include_id_markers=False)
                self.assertAlmostEqual(len(audio) / pixel.SAMPLE_RATE,
                                       backend.estimate_seconds(card, 1, include_id_markers=False))
                self.assertLess(len(audio) / pixel.SAMPLE_RATE,
                                backend.estimate_seconds(card, 1))
                report = backend.decode(audio, pixel.SAMPLE_RATE)
                self.assertTrue(report["card"]["exact"])
                self.assertEqual(report["pixels"], card.pixels)
                preview = LocalTransmitPreview(card, backend.key, backend, 1, "cq",
                                               None, len(audio), include_id_markers=False)
                self.assertEqual(preview.prefix, 0)
                self.assertEqual(preview.suffix, 0)

    def test_grouped_mode_uses_native_format_without_markers(self):
        card = pixel.example_card()
        backend = get_backend("experimental_8fsk_grouped")
        audio = backend.encode(card, 1, "cq", None, include_id_markers=False)
        self.assertAlmostEqual(len(audio) / pixel.SAMPLE_RATE,
                               backend.estimate_seconds(card, 1, include_id_markers=False))
        report = backend.decode(audio, pixel.SAMPLE_RATE)
        self.assertTrue(report["card"]["exact"])
        self.assertEqual(report["receive_profile"], "experimental_8fsk_ldpc")

    def test_weak_signal_mode_round_trips_without_markers(self):
        card = pixel.example_card()
        backend = get_backend("experimental_qpsk_5s")
        audio = backend.encode(card, 1, "cq", None, include_id_markers=False)
        self.assertAlmostEqual(len(audio) / pixel.SAMPLE_RATE,
                               backend.estimate_seconds(card, 1, include_id_markers=False))
        report = backend.decode(audio, pixel.SAMPLE_RATE)
        self.assertTrue(report["card"]["exact"])

    def test_checkbox_defaults_on_and_uses_shorter_tx_when_off(self):
        import app
        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, {"PIXELQSO_DATA_DIR": folder}), patch("app.QTimer.singleShot"):
            window = app.MainWindow()
            try:
                self.assertTrue(window.id_markers.isChecked())
                window.show_experimental_modes.setChecked(True)
                window.backend_combo.setCurrentIndex(window.backend_combo.findData("experimental"))
                window._refresh_mode_options(preferred="resilient_10")
                self.assertEqual(window.selected_mode(), "resilient_10")
                card = pixel.Card("N0CALL", "AA00", list(pixel.MINIMAL_AVATAR_PALETTE),
                                  [index % 8 for index in range(16)], width=4, height=4)
                window.id_markers.setChecked(False)
                self.assertFalse(window.interlaced_copies.isEnabled())
                audio = window._card_tx_audio(card)
                self.assertAlmostEqual(len(audio) / pixel.SAMPLE_RATE,
                                       window.selected_backend().estimate_seconds(
                                           card, 1, include_id_markers=False))
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()

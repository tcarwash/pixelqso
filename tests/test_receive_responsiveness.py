import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PySide6.QtWidgets import QApplication

import cardmodem as pixel
import weak_signal_modem as weak
from app import MainWindow
from card_backends import data2g_mode_key, host_mode_backends
from data2g_transport import Data2GMode


class ReceiveResponsivenessTests(unittest.TestCase):
    def test_clean_qpsk_copy_finishes_without_demodulating_later_copies(self):
        card = pixel.example_card()
        audio = np.tile(weak.encode(card), 3)
        with patch.object(weak, "soft_frame", wraps=weak.soft_frame) as soft:
            pixels, metadata = weak.decode(audio)
        self.assertEqual(soft.call_count, 1)
        self.assertEqual(pixels, pixel.unpack_indices(pixel.minimal_avatar_payload(card), 1024))
        self.assertTrue(metadata["exact"])
        self.assertEqual(metadata["received_copies"], 1)
        self.assertGreater(metadata["diagnostics"]["acquired_copies"], 1)

    def test_failed_first_copy_still_combines_before_releasing_pixels(self):
        evidence = np.ones(weak.N)
        diagnostic = {"acquisition_score": 1, "estimated_snr_db_2500": -7}
        def ldpc(llrs):
            if np.max(llrs) < 2:
                raise ValueError("unchecked copy")
            return np.zeros(weak.K), 3
        with patch.object(weak, "acquire", return_value=[(1, 0, 0), (.9, 100, 0)]), \
                patch.object(weak, "soft_frame", return_value=(evidence, diagnostic)), \
                patch.object(weak, "decode_ldpc", side_effect=ldpc), \
                patch.object(weak, "unpack", return_value=([2], {"exact": True})) as unpack:
            pixels, metadata = weak.decode(np.zeros(48000))
        self.assertEqual(metadata["received_copies"], 2)
        self.assertEqual(pixels, [2])
        self.assertEqual(unpack.call_count, 1)

    def test_mode_shortlist_separator_and_saved_selection(self):
        qt = QApplication.instance() or QApplication([])
        modes = [Data2GMode(name, bandwidth, 116, 64, 1.0, 60.0)
                 for name, bandwidth in (("n4-qpsk-r1/2", 200),
                     ("n10-qpsk-r1/2", 500), ("qpsk-r1/2", 1200),
                     ("16qam-r1/2", 1200), ("qpsk-r1/5", 1200))]
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"PIXELQSO_DATA_DIR": temp_dir}), \
                patch("app.QTimer.singleShot"):
            window = MainWindow()
            window.data2g_modes = host_mode_backends(modes)
            selected = data2g_mode_key("qpsk-r1/5")
            window._set_mode_options(selected)
            self.assertEqual(window.mode_combo.currentData(), selected)
            self.assertEqual([window.mode_combo.itemData(i) for i in range(3)],
                [data2g_mode_key(name) for name in ("qpsk-r1/2", "n10-qpsk-r1/2", "16qam-r1/2")])
            self.assertFalse(window.mode_combo.model().item(3).isEnabled())
            self.assertEqual(window.mode_combo.itemText(4), "All other Data2G modes")
            self.assertFalse(window.mode_combo.model().item(4).isEnabled())
            self.assertEqual({window.mode_combo.itemData(i) for i in range(window.mode_combo.count())}
                             - {None}, set(window.data2g_modes))
            window.close()
            qt.processEvents()


if __name__ == "__main__":
    unittest.main()

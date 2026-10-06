import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QLabel
import app as pixelqso


class TransferControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stop_units_and_periodic_beacon_at_current_frequency(self):
        old_data = pixelqso.DATA
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"PIXELQSO_DATA_DIR": folder}):
            Path(folder, "settings.ini").write_text('[radio]\nauto_connect_cat=false\n')
            window = pixelqso.MainWindow()
            try:
                window.auto_stop_btn.setEnabled(True)
                window.auto_stop_btn.click()
                self.assertIn('Automatic exchange stopped by operator.', window.exchange_log.toPlainText())
                self.assertEqual(window.cq_interval.suffix(), ' s')
                self.assertEqual(window.beacon_interval.suffix(), ' min')
                self.assertEqual(window.selected_audio_placement(), 300)
                labels = {label.text() for label in window.findChildren(QLabel)}
                self.assertFalse({'Audio placement', 'Lowest tone'} & labels)
                window.exchange_mode.setCurrentIndex(2)
                window.beacon_interval.setValue(2)
                with patch.object(window, 'transmit_beacon') as send:
                    window.beacon_button.setChecked(True)
                    send.assert_called_once()
                    self.assertEqual(window.beacon_timer.interval(), 120000)
                    self.assertTrue(window.beacon_timer.isActive())
                    window.beacon_button.setChecked(False)
                    self.assertFalse(window.beacon_timer.isActive())
                with patch.object(window, 'selected_adapter', return_value=pixelqso.EXPERIMENTAL_ADAPTER), patch.object(window.rig, 'connected', return_value=True), patch.object(window.rig, 'request') as request, patch.object(window, 'read_rig') as read:
                    window.transmit_beacon()
                    read.assert_called_once_with(window._preflight_and_confirm_beacon)
                    request.assert_not_called()
                    window.beacon_pending = False
                    window._preflight_and_confirm_beacon(True)
                    window._confirm_beacon_readback(True, window.frequency.value(), window.rig_mode)
                    request.assert_not_called()
                    window.tx_timer.start(10000)
                    window.transmit_beacon()
                    self.assertEqual(read.call_count, 1)
                    window.tx_timer.stop()
            finally:
                window.close()
                pixelqso.DATA = old_data

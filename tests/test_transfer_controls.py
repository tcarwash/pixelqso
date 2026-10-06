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

    def test_automatic_reply_sequences_and_ignores_duplicate_stages(self):
        old_data = pixelqso.DATA
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"PIXELQSO_DATA_DIR": folder}):
            Path(folder, "settings.ini").write_text('[radio]\nauto_connect_cat=false\n')
            window = pixelqso.MainWindow()
            def message(kind, call="K1ABC"):
                return {"assembly_event": {"verified_complete": True},
                        "card": {"avatar_burst": True, "message_type": kind,
                                 "callsign": call, "measured_snr_db": -12}}
            try:
                window.reply_report = message("cq")
                with patch.object(window, "start_auto_exchange") as arm:
                    window.reply_to_received()
                    arm.assert_called_once_with(reply_report=window.reply_report)
                    self.assertEqual(window.exchange_mode.currentIndex(), 1)
                    self.assertEqual(window.exchange_role.currentIndex(), 1)
                window.backend_combo.setCurrentIndex(window.backend_combo.findData("experimental"))
                window.show_experimental_modes.setChecked(True)
                window.mode_combo.setCurrentIndex(window.mode_combo.findData("resilient_100"))
                window.audio_test_mode.setChecked(True)
                window.test_link_enabled = True
                with patch.object(window, "_schedule_contact_transmit") as send:
                    window.start_auto_exchange(reply_report=window.reply_report)
                    self.assertTrue(window.auto_armed)
                    self.assertEqual(window.contact_stage, "send_exchange")
                    self.assertEqual(window.auto_peer, "K1ABC")
                    send.assert_called_once()
                with patch.object(pixelqso.QTimer, "singleShot") as timer, patch.object(window, "transmit_exchange") as transmit:
                    window._schedule_contact_transmit()
                    callback = timer.call_args.args[1]
                    window.stop_auto_exchange()
                    callback()
                    transmit.assert_not_called()
                window.auto_armed = True
                window.auto_role = "responder"
                window.auto_peer = ""
                window.contact_stage = "listen_cq"
                with patch.object(window, "_schedule_contact_transmit") as send:
                    window._handle_contact_message(message("cq"))
                    send.assert_not_called()
                    self.assertEqual(window.auto_peer, "")
                    window._handle_contact_message(message("cq"), initiated=True)
                    self.assertEqual(window.contact_stage, "send_exchange")
                    send.assert_called_once()
                    window._handle_contact_message(message("cq"), initiated=True)
                    self.assertEqual(send.call_count, 1)
                    window.contact_stage = "await_report73"
                    window._handle_contact_message(message("73", "K2OTHER"))
                    self.assertEqual(send.call_count, 1)
                    window._handle_contact_message(message("73"))
                    self.assertEqual(window.contact_stage, "send_final73")
                    self.assertEqual(send.call_count, 2)
                    window._handle_contact_message(message("73"))
                    self.assertEqual(send.call_count, 2)
                window.auto_role = "caller"
                window.auto_peer = ""
                window.contact_stage = "listen_cq"
                with patch.object(window, "_schedule_contact_transmit") as send, patch.object(window, "_complete_contact") as complete:
                    window._handle_contact_message(message("cq"))
                    self.assertEqual(window.auto_peer, "")
                    window._handle_contact_message(message("exchange"))
                    self.assertEqual(window.contact_stage, "send_report73")
                    send.assert_called_once()
                    window._handle_contact_message(message("73"))
                    complete.assert_not_called()
                    window.contact_stage = "await_final73"
                    window._handle_contact_message(message("73"))
                    complete.assert_called_once()
            finally:
                window.close()
                pixelqso.DATA = old_data

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

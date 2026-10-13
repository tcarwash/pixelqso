import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QLabel
import app as pixelqso


class TransferControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_advanced_controls_toggle_preserves_operating_configuration(self):
        old_data = pixelqso.DATA
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"PIXELQSO_DATA_DIR": folder}), \
                patch("app.QTimer.singleShot"):
            Path(folder, "settings.ini").write_text('[radio]\nauto_connect_cat=false\n')
            window = pixelqso.MainWindow()
            try:
                self.assertFalse(window.advanced_controls_action.isChecked())
                self.assertTrue(window.auto_stage_group.isHidden())
                self.assertTrue(window.backend_combo.isHidden())
                self.assertEqual(
                    window.station_identity.text(),
                    f"{window.station_call.text().strip().upper()} · "
                    f"{window.station_grid.text().strip().upper()}")
                window.station_call.setText(" w7xyz ")
                window.station_grid.setText("cn87")
                window._refresh_station_identity()
                self.assertEqual(window.station_identity.text(), "W7XYZ · CN87")
                self.assertEqual(window.rig_status.text(), "Data2G not connected")
                self.assertEqual(window.listen_indicator.text(), "● NOT CONNECTED")
                self.assertEqual(window.station_button.text(), "Connect station")
                self.assertFalse(window.tx_btn.isHidden())
                self.assertFalse(window.tx_btn.isEnabled())
                self.assertIn("Connect station", window.tx_availability_hint.text())
                original_backend = window.backend_combo.currentData()
                original_mode = window.mode_combo.currentData()
                stage_combo = window.stage_card_combos['exchange']
                stage_combo.addItem("Saved stage fixture", "saved-stage")
                stage_combo.setCurrentIndex(0)
                window.advanced_controls_action.setChecked(True)
                self.assertFalse(window.auto_stage_group.isHidden())
                self.assertFalse(window.backend_combo.isHidden())
                self.assertTrue(window.receive_all_modes.isHidden())
                window.data2g_connecting = True
                window._update_backend_ui()
                self.assertEqual(window.rig_status.text(), "Connecting to Data2G host…")
                self.assertEqual(window.listen_indicator.text(), "● CONNECTING")
                window.data2g_connecting = False
                window.data2g_session = Mock()
                with patch.object(pixelqso.DATA2G_ADAPTER, "is_connected", return_value=True):
                    window._update_backend_ui()
                    self.assertEqual(
                        window.rig_status.text(),
                        "Data2G connected · audio/PTT readiness unverified")
                    self.assertEqual(window.listen_indicator.text(), "● WAITING FOR FRAMES")
                    self.assertEqual(window.station_button.text(), "Station settings")
                    window.refresh_tx_button()
                    self.assertIn("Data2G mode", window.tx_availability_hint.text())
                    window.data2g_ptt_failed = True
                    window._update_backend_ui()
                    self.assertIn("PTT fault", window.rig_status.text())
                    window.data2g_ptt_failed = False
                window.data2g_session = None
                window.advanced_controls_action.setChecked(False)
                self.assertTrue(window.auto_stage_group.isHidden())
                self.assertTrue(window.exchange_mode.isHidden())
                window._update_transfer_controls()
                self.assertTrue(window.profile_form_widget.isHidden())
                self.assertEqual(window.backend_combo.currentData(), original_backend)
                self.assertEqual(window.mode_combo.currentData(), original_mode)
                self.assertEqual(window.settings.value('ui/advanced_controls', False, type=bool), False)
                window.advanced_controls_action.setChecked(True)
                self.assertEqual(window.stage_card_combos['exchange'].currentData(), "saved-stage")
            finally:
                window.close()
                pixelqso.DATA = old_data

    def test_settings_and_about_are_available_from_every_destination(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, {"PIXELQSO_DATA_DIR": folder}), patch("app.QTimer.singleShot"):
            window = pixelqso.MainWindow()
            try:
                self.assertEqual(window.station_settings_action.shortcut().toString(), "Ctrl+,")
                menus = {action.text(): action.menu()
                         for action in window.menuBar().actions()}
                self.assertIn(window.station_settings_action, menus["Settings"].actions())
                self.assertIn(window.about_action, menus["Help"].actions())
                for tab_index in range(window.tabs.count()):
                    window.tabs.setCurrentIndex(tab_index)
                    self.assertTrue(window.station_settings_action.isEnabled())
                    self.assertTrue(window.about_action.isEnabled())
                with patch("app.QMessageBox.about") as show_about:
                    window.about_action.trigger()
                    show_about.assert_called_once()
                    self.assertIn("digital radio", show_about.call_args.args[2])
            finally:
                window.close()

    def test_getting_started_guide_shows_setup_steps_and_can_be_reopened(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, {"PIXELQSO_DATA_DIR": folder}), patch("app.QTimer.singleShot"):
            window = pixelqso.MainWindow()
            try:
                captured = {}
                def inspect_dialog(dialog):
                    captured["dialog"] = dialog
                    return 0
                with patch("app.QDialog.exec", new=inspect_dialog):
                    window.open_getting_started()
                dialog = captured["dialog"]
                content = " ".join(label.text() for label in dialog.findChildren(QLabel))
                self.assertIn("Your station", content)
                self.assertIn("Connect your station", content)
                self.assertIn("Check reception", content)
                self.assertIn("Choose a starter card", content)
                buttons = {button.text() for button in dialog.findChildren(pixelqso.QPushButton)}
                self.assertIn("Done for now", buttons)
                self.assertIn("Connection settings…", buttons)

                with patch.object(window, "open_getting_started") as show_guide:
                    window.settings.setValue("setup/guide_seen", False)
                    window._maybe_show_getting_started()
                    show_guide.assert_called_once()
                    self.assertTrue(window.settings.value("setup/guide_seen", False, type=bool))
                    window._maybe_show_getting_started()
                    show_guide.assert_called_once()
            finally:
                window.close()

    def test_station_settings_cancel_rolls_back_and_connected_changes_require_reconnect(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, {"PIXELQSO_DATA_DIR": folder}), patch("app.QTimer.singleShot"):
            window = pixelqso.MainWindow()
            original_call = window.station_call.text()
            try:
                def cancel_with_identity_edit(dialog):
                    window.station_call.setText("W7XYZ")
                    self.assertTrue(window.station_settings_apply_button.isEnabled())
                    return pixelqso.QDialog.DialogCode.Rejected
                with patch("app.QDialog.exec", new=cancel_with_identity_edit):
                    window.open_station_settings()
                self.assertEqual(window.station_call.text(), original_call)
                self.assertEqual(window.settings.value("station/callsign", "N0CALL"), "N0CALL")

                original_source = window.data2g_host_source.currentData()
                original_host = window.data2g_host_input.text()
                def cancel_with_connection_edit(dialog):
                    if window.data2g_host_source.currentData() != "remote":
                        window.data2g_host_source.setCurrentIndex(
                            window.data2g_host_source.findData("remote"))
                    window.data2g_host_input.setText("radio.example")
                    self.assertFalse(window.station_settings_reconnect_button.isHidden())
                    self.assertTrue(window.station_settings_apply_button.isHidden())
                    return pixelqso.QDialog.DialogCode.Rejected
                with patch.object(pixelqso.DATA2G_ADAPTER, "is_connected", return_value=True), \
                        patch.object(window, "_reconnect_data2g_after_settings") as reconnect, \
                        patch("app.QDialog.exec", new=cancel_with_connection_edit):
                    window.open_station_settings()
                reconnect.assert_not_called()
                self.assertEqual(window.data2g_host_source.currentData(), original_source)
                self.assertEqual(window.data2g_host_input.text(), original_host)
            finally:
                window.close()

    def test_station_settings_apply_persists_changes(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, {"PIXELQSO_DATA_DIR": folder}), patch("app.QTimer.singleShot"), \
                patch.object(pixelqso.MainWindow, "_refresh_mode_options"):
            window = pixelqso.MainWindow()
            try:
                def apply_station_details(dialog):
                    window.station_call.setText("W7XYZ")
                    window._apply_station_settings()
                    return dialog.result()
                with patch("app.QDialog.exec", new=apply_station_details):
                    window.open_station_settings()
                self.assertEqual(window.settings.value("station/callsign"), "W7XYZ")
                self.assertEqual(window.station_identity.text(), "W7XYZ · AA00")
            finally:
                window.close()

    def test_station_settings_apply_and_reconnect_dispatches_after_apply(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, {"PIXELQSO_DATA_DIR": folder}), patch("app.QTimer.singleShot"), \
                patch.object(pixelqso.MainWindow, "_refresh_mode_options"):
            window = pixelqso.MainWindow()
            try:
                def apply_connection_change(dialog):
                    window.data2g_host_input.setText("radio.example")
                    self.assertFalse(window.station_settings_reconnect_button.isHidden())
                    window._apply_station_settings(reconnect=True)
                    return dialog.result()
                with patch.object(pixelqso.DATA2G_ADAPTER, "is_connected", return_value=True), \
                        patch.object(window, "_reconnect_data2g_after_settings") as reconnect, \
                        patch("app.QDialog.exec", new=apply_connection_change):
                    window.open_station_settings()
                self.assertEqual(window.settings.value("data2g/host"), "radio.example")
                reconnect.assert_called_once()
            finally:
                window.close()

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
                    self.assertIn("K1ABC · Sending exchange", window.exchange_status.text())
                    self.assertIn("✓ CQ    ● Exchange", window.exchange_status.text())
                    send.assert_called_once()
                window.contact_stage = "await_report73"
                window._refresh_contact_stage_indicator()
                self.assertIn("K1ABC · Waiting for RR73", window.exchange_status.text())
                self.assertIn("● RR73", window.exchange_status.text())
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

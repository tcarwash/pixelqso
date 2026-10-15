import unittest
from unittest.mock import Mock, patch

from app import MainWindow


class CatOwnershipTests(unittest.TestCase):
    def test_connect_cat_with_background_receiver_and_radio_handoffs(self):
        cases = [
            ("experimental", True, False, False, False, False, True),
            ("experimental", True, True, True, False, False, True),
            ("experimental", True, False, False, True, False, False),
            ("experimental", True, False, False, False, True, False),
            ("experimental", False, False, False, False, False, False),
            ("data2g", True, False, False, False, False, False),
        ]
        for backend, monitor, connecting, starting, closing, handoff, allowed in cases:
            with self.subTest(backend=backend, monitor=monitor, connecting=connecting,
                              closing=closing, handoff=handoff):
                window = Mock()
                window.backend_combo.currentData.return_value = backend
                window.data2g_background_monitor = monitor
                window.data2g_connecting = connecting
                window.data2g_local_starting = starting
                window.data2g_closing = closing
                window.data2g_handoff_pending = handoff
                window.rig.connected.return_value = False
                window.rig_host.text.return_value = "127.0.0.1"
                window.rig_port.value.return_value = 4532
                window._station_settings_connection_edits_pending.return_value = False
                window._confirm_data2g_ptt_recovery.return_value = True
                window._data2g_blocks_local_cat.side_effect = lambda: (
                    MainWindow._data2g_blocks_local_cat(window))
                with patch("app.DATA2G_ADAPTER.is_connected", return_value=True):
                    MainWindow.connect_rig(window)
                if allowed:
                    window.rig.connect_to.assert_called_once_with(
                        "127.0.0.1", 4532, window.rig_changed)
                else:
                    window.rig.connect_to.assert_not_called()

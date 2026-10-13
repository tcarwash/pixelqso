import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from card_views import incoming_card_text
from app import MainWindow


class IncomingMetadataTests(unittest.TestCase):
    def test_identity_and_measurements_display_without_pixels(self):
        report = {'card': {'callsign': 'W1ABC', 'grid': 'FN31', 'card_id': 42,
                          'message_type': 'exchange', 'width': 32, 'height': 32,
                          'measured_snr_db': -9, 'measured_snr_bandwidth_hz': 2500,
                          'snr_db': -12, 'received_blocks': [True, False], 'exact': False}}
        text = incoming_card_text(report, 'Resilient 50')
        for value in ('W1ABC', 'FN31', '002A', 'EXCHANGE', '32 × 32', '-9 dB',
                      '-12 dB', '1/2', 'Resilient 50', 'Image incomplete'):
            self.assertIn(value, text)
        self.assertNotIn('Whole card verified', text)

    def test_header_only_never_invents_station_identity(self):
        report = {'card': {'measured_snr_db': -6},
                  'copy_header': {'announced_mode': 'resilient_100', 'content_id': 'abc123'}}
        text = incoming_card_text(report)
        self.assertIn('station identity pending', text)
        self.assertIn('resilient_100', text)
        self.assertIn('abc123', text)
        self.assertNotIn('Card ID:', text)
        self.assertNotIn('Stage:', text)

    def test_preview_with_no_image_still_updates_metadata(self):
        window = SimpleNamespace(_update_receive_metadata=Mock())
        report = {'card': {'callsign': 'W1ABC'}}
        MainWindow._show_receive_preview(window, None, report=report)
        window._update_receive_metadata.assert_called_once_with(report)

    def test_new_identity_replaces_previous_label_without_old_fields(self):
        label = Mock()
        window = SimpleNamespace(incoming_card_details=label, _receive_mode_label=lambda mode: mode)
        MainWindow._update_receive_metadata(window, {'card': {'callsign': 'W1ABC', 'grid': 'FN31'}})
        MainWindow._update_receive_metadata(window, {'card': {'callsign': 'K2XYZ'}})
        text = label.setText.call_args.args[0]
        self.assertIn('K2XYZ', text)
        self.assertNotIn('W1ABC', text)
        self.assertNotIn('FN31', text)

    def test_managed_burst_metadata_displayed_when_application_frames_are_lost(self):
        session = SimpleNamespace(port=3)
        window = SimpleNamespace(data2g_session=session, _show_receive_activity=Mock(),
                                 receive_activity_timeout=Mock(), exchange_log=Mock(), on_air=None)
        MainWindow._data2g_host_status(window, session,
            'BCAST 3 RXMETADATA -11 2500 W1ABC n4-qpsk-r1/5 4')
        report = window._show_receive_activity.call_args.kwargs['report']
        text = incoming_card_text(report)
        self.assertIn('W1ABC', text)
        self.assertIn('-11 dB', text)
        self.assertIn('n4-qpsk-r1/5', text)
        self.assertIn('Burst codewords: 4', text)
        self.assertNotIn('Card ID:', text)

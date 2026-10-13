import hashlib
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from scipy.signal import hilbert

from snr import (KISS_RX_SNR, encode_rx_snr, decode_rx_snr,
                 set_measured_snr, snr_lines)


class SnrTests(unittest.TestCase):
    def test_all_common_header_modes_populate_report_and_survive_cache(self):
        import modem_envelope as envelope
        for mode in envelope.MODE_IDS:
            copy = envelope.AnnouncedCopy(mode, 'identity', 0, 80,
                {'carrier_offset_hz': 0, 'estimated_snr_db_2500': -8.5})
            cache = envelope.CopyCaptureCache()
            audio = np.ones(80)
            cache.update(audio, 8000, [copy])
            cached_audio, copies = cache.materialize()
            report = envelope.decode_capture(mode, cached_audio, 8000,
                lambda audio, rate: {'card': {'exact': False, 'snr_db': -12}, 'pixels': []},
                copies=copies)
            self.assertEqual(report['card']['measured_snr_db'], -8)
            self.assertEqual(report['card']['measured_snr_bandwidth_hz'], 2500)
            self.assertEqual(report['card']['snr_db'], -12)

    def test_received_header_has_measurement(self):
        import modem_envelope as envelope
        header = envelope.header_audio('resilient_100', 48000, b'x'*16)
        source = np.concatenate((header, np.zeros(48000)))
        source += np.random.default_rng(3).normal(0, .03, len(source))
        copies = envelope.find_copies(source, 48000)
        self.assertTrue(copies)
        self.assertTrue(np.isfinite(copies[0].diagnostics['estimated_snr_db_2500']))

    def test_header_estimate_tracks_known_noise_and_is_gain_independent(self):
        from modem_envelope import header_snr_db, constants, TONES, RATE, BAUD
        training, _ = constants()
        step = RATE // BAUD
        signal = np.concatenate([np.cos(2*np.pi*TONES[t]*np.arange(step)/RATE) for t in training])
        for target in (-10, 0, 10):
            noise = np.random.default_rng(9).normal(0, np.sqrt(.8/10**(target/10)), len(signal))
            for gain in (.01, 1, 10):
                estimate = header_snr_db(hilbert(gain*(signal+noise)), 0, 0)
                self.assertAlmostEqual(estimate, target, delta=1.5)

    def test_standardization_preserves_peer_report(self):
        card = {'snr_db': -12}
        self.assertTrue(set_measured_snr(card, 0, 'test', 500))
        self.assertEqual(card['measured_snr_db'], -7)
        self.assertEqual(card['measured_snr_bandwidth_hz'], 2500)
        self.assertEqual(card['snr_db'], -12)
        self.assertEqual(len(snr_lines(card)), 2)
        self.assertFalse(set_measured_snr(card, float('nan'), 'test'))

    def test_telemetry_validates_length_version_and_finite_values(self):
        digest = hashlib.sha256(b'card').hexdigest()
        self.assertEqual(decode_rx_snr(encode_rx_snr(digest, -8.5, 2500)),
                         (digest, -8.5, 2500))
        for payload in (b'bad', encode_rx_snr(digest, float('nan'), 2500),
                        encode_rx_snr(digest, 0, 0)):
            with self.assertRaises(ValueError):
                decode_rx_snr(payload)

    def test_managed_host_pairs_measurements_with_exact_frames(self):
        from data2g.kisslink import KissLink
        from data2g.tnc import KissServer
        from data2g.arq import phy
        from data2g_receive_host import enable_receive_snr
        from data2g_transport import KissDecoder
        original_receive, original_broadcast = KissLink.on_burst, KissServer.broadcast
        frame = b'card\xc0\xdb'
        client = Mock()
        server = SimpleNamespace(lock=threading.Lock(), clients={client})
        try:
            with patch.object(KissLink, 'on_burst', new=lambda self, received, rx=None: [(3, frame)]), \
                    patch.object(phy, 'measure', side_effect=[{'snr_est': -9}, {'snr_est': 2},
                                                            {'snr_est': float('nan')}]):
                enable_receive_snr()
                KissLink.on_burst(None, {})
                KissLink.on_burst(None, {})
                KissServer.broadcast(server, frame, 3)
                KissServer.broadcast(server, frame, 3)
                KissLink.on_burst(None, {})
                KissServer.broadcast(server, frame, 3)
            for call, expected in zip(client.sendall.call_args_list, (-9, 2)):
                events = KissDecoder().feed(call.args[0])
                self.assertEqual(events[0][:2], (3, KISS_RX_SNR))
                self.assertEqual(decode_rx_snr(events[0][2])[1], expected)
                self.assertEqual(events[1], (3, 0, frame))
            self.assertEqual(KissDecoder().feed(client.sendall.call_args_list[-1].args[0]),
                             [(3, 0, frame)])
        finally:
            KissLink.on_burst, KissServer.broadcast = original_receive, original_broadcast

    def test_transport_delivers_metadata_before_frame_on_same_socket(self):
        from data2g_transport import Data2GTransport, encode_kiss
        frame = b'card'
        digest = hashlib.sha256(frame).hexdigest()
        wire = (encode_kiss(3, KISS_RX_SNR, encode_rx_snr(digest, -9, 2500)) +
                encode_kiss(3, 0, frame))
        events = []
        transport = Data2GTransport('localhost', 1, 3,
            on_status=lambda line: events.append(('status', line)),
            on_frame=lambda port, data: events.append(('frame', port, data)))
        transport.kiss_socket = Mock()
        transport.kiss_socket.recv.side_effect = [wire[:13], wire[13:], b'']
        with patch.object(transport, '_fail_connection'):
            transport._read_kiss()
        self.assertTrue(events[0][1].startswith(f'PIXELQSO_RX_SNR 3 {digest} '))
        self.assertEqual(events[1], ('frame', 3, frame))

    def test_lost_data_frames_still_expose_decoded_burst_metadata(self):
        from data2g.kisslink import KissLink
        from data2g.tnc import KissServer
        from data2g.arq import phy
        from data2g_receive_host import enable_receive_snr
        originals = KissLink.on_burst, KissServer.broadcast
        link = SimpleNamespace(events=[])
        def lost(self, received, rx=None):
            self.events.extend(['BCAST 3 HEARD W1ABC', 'BCAST 3 LOST 2'])
            return []
        try:
            with patch.object(KissLink, 'on_burst', new=lost), \
                    patch.object(phy, 'measure', return_value={'snr_est': -11}):
                enable_receive_snr()
                result = KissLink.on_burst(link, {'spec': SimpleNamespace(name='n4-qpsk-r1/5'), 'n_cw': 4})
                self.assertEqual(result, [])
                self.assertEqual(link.events[-1], 'BCAST 3 RXMETADATA -11.0 2500 W1ABC n4-qpsk-r1/5 4')
        finally:
            KissLink.on_burst, KissServer.broadcast = originals

    def test_managed_measurement_reaches_card_and_can_be_reported_back(self):
        from app import MainWindow
        import cardmodem
        from card_transfer import CardAssemblyService, CardTransferReceiver, fragment
        from backend_adapters import DATA2G_ADAPTER
        session = SimpleNamespace(port=3)
        card = cardmodem.example_card()
        pixels = cardmodem.minimal_avatar_payload(card)
        frame = fragment(card, pixels, 0, pixels[:30], 'exchange', -12)
        assembly = CardAssemblyService()
        window = SimpleNamespace(data2g_session=session, on_air=None, card_assembly=assembly,
            _merge_live_preview=assembly.merge_preview, _report_receive_identity=lambda r: 'identity',
            _assembly_verified=lambda r: False, card_image=Mock(), _show_receive_preview=Mock(),
            rx_status=Mock(), exchange_log=Mock())
        digest = hashlib.sha256(frame).hexdigest()
        MainWindow._data2g_host_status(window, session, f'PIXELQSO_RX_SNR 3 {digest} -9 2500')
        MainWindow._data2g_rx_frame(window, session, 3, frame)
        received = window._show_receive_preview.call_args.kwargs['report']['card']
        self.assertEqual(received['measured_snr_db'], -9)
        self.assertEqual(received['snr_db'], -12)
        self.assertFalse(window.data2g_rx_measurements)
        replies = DATA2G_ADAPTER.frame_card(card, 512, '73', received['measured_snr_db'])
        receiver = CardTransferReceiver()
        for reply in replies:
            _, meta = receiver.feed('PIXELQSO', reply)
        self.assertEqual(meta['snr_db'], -9)

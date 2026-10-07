import unittest
from unittest.mock import patch
import numpy as np
import cardmodem as pixel
import grid_fsk_modem as grid
import modem_envelope as envelope
from card_backends import get_backend

KEY = 'experimental_16fsk_grid'

class GridTests(unittest.TestCase):
    def test_sample_schedule_and_latency(self):
        for phase in np.linspace(0, .5, 101):
            now = 1000 + phase
            target, samples = grid.plan_start(now, output_latency=.075)
            self.assertAlmostEqual(target % .5, 0)
            onset = now + samples/48000 + .075
            self.assertLessEqual(abs(onset-target), 1/48000 + 1e-10)
            self.assertGreaterEqual(samples/48000, .04-1e-10)
            self.assertLessEqual(samples/48000, .54+1/48000)
        self.assertTrue(grid.near_grid(1000.49))
        self.assertFalse(grid.near_grid(1000.25))
        with self.assertRaises(ValueError): grid.plan_start(float('nan'))

    def test_clock_unknown_and_unsynced(self):
        with patch('grid_fsk_modem.shutil.which', return_value=None):
            self.assertIn('unknown', grid.clock_status())
        from subprocess import CompletedProcess
        row = 'id,1,0,0.001,0,0,0,0,0,0.010,0.002,1,Normal'
        with patch('grid_fsk_modem.shutil.which', return_value='chronyc'), patch(
                'grid_fsk_modem.subprocess.run', return_value=CompletedProcess([],0,row,'')):
            self.assertIn('8.0 ms', grid.clock_status())

    def test_grid_wire_identity_and_wrong_timing_fallback(self):
        card = pixel.example_card()
        backend = get_backend(KEY)
        self.assertTrue(backend.utc_grid)
        audio = backend.encode(card, 1, 'cq', -6)
        # Intentionally wrong UTC origin: normal acquisition still works.
        copies = envelope.find_copies(audio,48000,capture_start_utc=1000.25)
        self.assertEqual(copies[0].mode, KEY)
        self.assertEqual(copies[0].diagnostics['version'], 3)
        self.assertEqual(envelope.MODE_IDS[KEY],18)
        report = backend.decode(audio,48000)
        self.assertTrue(report['card']['whole_raster_crc32_valid'])
        self.assertEqual(report['pixels'], pixel.unpack_indices(pixel.minimal_avatar_payload(card),1024))
        self.assertNotEqual(envelope.content_id(KEY,card,'cq',-6),
                            envelope.content_id('experimental_16fsk_compressed',card,'cq',-6))

    def test_noise_has_no_grid_header(self):
        audio=np.random.default_rng(1).normal(0,.1,48000*3)
        self.assertEqual(envelope.find_copies(audio,48000,capture_start_utc=1000),[])

if __name__ == '__main__': unittest.main()

class GridAppTests(unittest.TestCase):
    def test_real_app_schedules_only_grid_and_tracks_clear_origin(self):
        from app import MainWindow
        from types import SimpleNamespace
        state=SimpleNamespace(selected_backend=lambda:get_backend(KEY),
            grid_clock_status='test clock', test_link_enabled=True,
            audio_test_mode=SimpleNamespace(isChecked=lambda:True),
            tx_bytes=b'payload',tx_deadline=30.,exchange_log=SimpleNamespace(append=lambda x:None))
        with patch('app.time.time',return_value=1000.1):
            MainWindow._prepare_grid_audio(state)
        self.assertEqual(state.grid_tx_target_utc,1000.5)
        self.assertEqual(state.tx_bytes,b'\0'*38400+b'payload')
        state.selected_backend=lambda:get_backend('experimental_16fsk_compressed')
        previous=state.tx_bytes
        MainWindow._prepare_grid_audio(state)
        self.assertEqual(state.tx_bytes,previous)
        state.rx_capture_start_utc=1000.;state.rx_capture_sample_offset=48000
        state.rx_rate=48000;state.rx_bytes=bytearray(96000)
        MainWindow._clear_rx_bytes(state)
        self.assertEqual(state.rx_capture_start_utc,1002.)
        self.assertEqual(state.rx_capture_sample_offset,0)

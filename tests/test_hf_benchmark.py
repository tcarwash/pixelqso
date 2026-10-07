import unittest
from unittest.mock import patch
from types import SimpleNamespace
from tools.benchmark_hf import trial
import cardmodem as pixel

class BenchmarkScoringTests(unittest.TestCase):
    def test_legacy_rgb_and_burst_indices_score_same_source(self):
        indices = [i % 8 for i in range(1024)]
        expected = dict(pixels=indices, callsign='W7PXQ', grid='CN87')
        for mode in ('standard', 'experimental_qpsk_5s'):
            pixels = ( [[tuple(pixel.MINIMAL_AVATAR_PALETTE[i]) for i in indices[y*32:(y+1)*32]]
                        for y in range(32)] if mode == 'standard' else indices)
            report = dict(pixels=pixels, card=dict(callsign='W7PXQ', grid='CN87'),
                          assembly_event=dict(verified_complete=True))
            outcome = SimpleNamespace(failure=None, report=report)
            with patch('tools.benchmark_hf.EXPERIMENTAL_ADAPTER.decode_capture', return_value=outcome), \
                 patch('tools.benchmark_hf.EXPERIMENTAL_ADAPTER.decode_legacy_audio', return_value=outcome), \
                 patch('tools.benchmark_hf.CardAssemblyService.merge_preview', return_value=(report, False)):
                row = trial(mode, [], expected, 'awgn', 1, -6)
            self.assertTrue(row['verified'])
            self.assertFalse(row['false_verified'])

    def test_noise_control_rejects_claimed_verification(self):
        report = dict(pixels=[], card={}, assembly_event=dict(verified_complete=True))
        outcome = SimpleNamespace(failure=None, report=report)
        with patch('tools.benchmark_hf.EXPERIMENTAL_ADAPTER.decode_capture', return_value=outcome), \
             patch('tools.benchmark_hf.CardAssemblyService.merge_preview', return_value=(report, False)):
            row = trial('experimental_qpsk_5s', [], None, 'noise_only', 1, None)
        self.assertTrue(row['false_verified'])
        self.assertFalse(row['verified'])

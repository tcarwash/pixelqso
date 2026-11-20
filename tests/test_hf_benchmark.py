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

    def test_partial_reception_metrics_do_not_imply_verified_card(self):
        card = dict(callsign='W7PXQ', grid='CN87', width=4, height=2,
                    pixel_coverage=[True] * 6 + [False] * 2,
                    verified_pixel_coverage=[True] * 3 + [False] * 5,
                    exact=False)
        report = dict(card=card, pixels=[1] * 8,
                      assembly_event=dict(verified_complete=False))
        outcome = SimpleNamespace(failure=None, report=report)
        expected = dict(pixels=[0] * 8, callsign='W7PXQ', grid='CN87')
        with patch('tools.benchmark_hf.EXPERIMENTAL_ADAPTER.decode_capture', return_value=outcome), \
             patch('tools.benchmark_hf.CardAssemblyService.merge_preview',
                   return_value=(report, False)):
            row = trial('experimental_qpsk_5s', [], expected, 'awgn', 1, -6)
        self.assertFalse(row['verified'])
        self.assertFalse(row['false_verified'])
        self.assertEqual(row['heard_percent'], 75)
        self.assertEqual(row['crc_verified_percent'], 38)

    def test_report_displays_partial_coverage_without_counting_missing_reports_as_zero(self):
        from tools.report_hf import render
        values = dict(mode='experimental_qpsk_5s', profile='awgn',
                      snr_db_2500=-6, verified=False, false_verified=False,
                      exact_match=False, airtime_seconds=15,
                      decode_seconds=.5)
        with_pixels = dict(values, heard_percent=75, crc_verified_percent=25)
        no_card = dict(values, heard_percent=None, crc_verified_percent=None)
        result = render({
            'arguments': dict(modes=['experimental_qpsk_5s'],
                profiles=['awgn'], snr_db=[-6], trials=2, repeats=1,
                cfo_hz=0, clock_ppm=0),
            'trials': [with_pixels, no_card],
        })
        self.assertIn('Provisional reception coverage', result)
        self.assertIn('75.0% (1/2)', result)
        self.assertIn('25.0% (1/2)', result)
        self.assertIn('Failed acquisitions are excluded', result)

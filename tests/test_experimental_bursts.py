import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PySide6.QtWidgets import QApplication

import cardmodem as pixel
import experimental_burst_modem as burst
import experimental_fec as fec
import weak_signal_modem as weak
from app import DecodeWorker, LiveDecodeWorker, MainWindow, decode_all_card_backends
from backend_adapters import EXPERIMENTAL_ADAPTER, DecodeFailureKind
from modem_envelope import HEADER_SECONDS
from copy_combining import MAX_COPIES
from card_backends import MODE_REGISTRY, available_modes, get_backend
from card_transfer import CardAssemblyService


class ExperimentalBurstTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.card = pixel.example_card()
        cls.expected = pixel.unpack_indices(pixel.minimal_avatar_payload(cls.card), 1024)
        cls.audio = {key: burst.encode(key, cls.card, "cq", -9)
                     for key in burst.MODE_KEYS}

    def assert_verified(self, key, report):
        self.assertEqual(report["pixels"], self.expected)
        self.assertEqual(report["card"]["callsign"], self.card.callsign)
        self.assertEqual(report["card"]["avatar_mode"], key)
        self.assertTrue(report["card"]["exact"])
        service = CardAssemblyService()
        merged, _ = service.merge_preview(report)
        self.assertTrue(merged["assembly_event"]["verified_complete"])
        self.assertEqual(merged["assembly_event"]["verification_scope"], "whole_raster_crc32")

    def test_four_modes_are_gated_and_share_copy_policy(self):
        self.assertFalse(set(burst.MODE_KEYS) & {key for key, _ in available_modes()})
        self.assertTrue(set(burst.MODE_KEYS) <= {key for key, _ in available_modes(include_experimental=True)})
        for key in burst.MODE_KEYS:
            with self.subTest(key=key):
                backend = get_backend(key)
                self.assertTrue(MODE_REGISTRY[key].experimental)
                self.assertEqual(backend.max_repeats, MAX_COPIES)
                self.assertAlmostEqual(len(self.audio[key]) / 48000,
                                       backend.estimate_seconds(self.card, 1) - 2*HEADER_SECONDS, places=5)
                self.assertLess(backend.estimate_seconds(self.card, 1), 10)
                with self.assertRaises(ValueError):
                    backend.encode(self.card, MAX_COPIES+1, "cq", None)
                with self.assertRaises(ValueError):
                    backend.estimate_seconds(self.card, MAX_COPIES+1)
        large = pixel.Card(**{**self.card.__dict__, "width": 64, "height": 64,
                              "pixels": [0] * 4096})
        for key in burst.MODE_KEYS:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "32.*32"):
                get_backend(key).estimate_seconds(large, 1)

    def test_clean_and_noisy_delayed_captures_verify_through_adapter(self):
        for key in burst.MODE_KEYS:
            audio = self.audio[key]
            audio = audio / np.sqrt(np.mean(audio * audio)) * .1
            noise = np.random.default_rng(82).normal(
                0, .1 * 10 ** (-6 / 20) * np.sqrt(48000 / 5000), len(audio))
            for name, source in (("clean", audio), ("noise", audio + noise),
                                 ("echo", audio + np.pad(.6 * audio[:-48], (48, 0)))):
                with self.subTest(key=key, channel=name):
                    source = np.pad(source, (16320, 12320))
                    outcome = EXPERIMENTAL_ADAPTER.decode_capture(get_backend(key), source, 48000)
                    self.assertIsNone(outcome.failure)
                    self.assert_verified(key, outcome.report)

    def test_partial_ldpc_and_qpsk_produce_activity_without_pixels(self):
        for key in ("experimental_8fsk_ldpc", "experimental_16fsk_ldpc", burst.QPSK_KEY):
            with self.subTest(key=key):
                outcome = EXPERIMENTAL_ADAPTER.decode_capture(
                    get_backend(key), self.audio[key][:48000], 48000)
                self.assertIsNone(outcome.failure)
                self.assertIn("receive_activity", outcome.report)
                self.assertEqual(outcome.report["pixels"], [])
                self.assertFalse(CardAssemblyService.assess(outcome.report).verified_complete)

    def test_outer_rs_exposes_checked_pixels_before_full_card(self):
        key = "experimental_8fsk_outer_rs"
        report = get_backend(key).decode(self.audio[key][:int(2 * 48000)], 48000)
        self.assertEqual(report["card"]["callsign"], self.card.callsign)
        coverage = report["card"]["pixel_coverage"]
        self.assertTrue(any(coverage))
        self.assertFalse(all(coverage))
        for i, covered in enumerate(coverage):
            if covered:
                self.assertEqual(report["pixels"][i], self.expected[i])
        service = CardAssemblyService()
        merged, _ = service.merge_preview(report)
        self.assertFalse(merged["assembly_event"]["verified_complete"])
        self.assertGreater(merged["assembly_event"]["verified_pixels"], 0)

    def test_outer_rs_recovers_four_missing_data_shards_in_one_waveform(self):
        key = "experimental_8fsk_outer_rs"
        audio = self.audio[key].copy()
        fmt = burst.FORMATS[key]
        missing = (0, 2, 5, 7)
        for index in missing:
            start = int((burst.GUARD_SECONDS +
                         (burst.TRAINING + index * 128 + 4) / fmt.baud) * 48000)
            audio[start:start + int(124 / fmt.baud * 48000)] = 0
        report = get_backend(key).decode(audio, 48000)
        self.assert_verified(key, report)
        self.assertEqual(report["card"]["diagnostics"]["outer_recovered_shards"], list(missing))

    def test_stronger_inner_rs_corrects_five_gf64_symbol_errors(self):
        rng = np.random.default_rng(81)
        for errors in range(1, 6):
            data = rng.integers(0, 64, 52).tolist()
            word = fec.rs_encode(data)
            for at in rng.choice(len(word), errors, replace=False):
                word[at] ^= int(rng.integers(1, 64))
            decoded, corrected = fec.rs_decode(word)
            self.assertEqual(decoded, data)
            self.assertEqual(corrected, errors)

    def test_noise_and_wrong_modulation_never_verify_a_card(self):
        rng = np.random.default_rng(44)
        for key in burst.MODE_KEYS:
            for trial in range(2):
                with self.subTest(key=key, noise=trial):
                    noise = rng.normal(0, .1, len(self.audio[key]))
                    outcome = EXPERIMENTAL_ADAPTER.decode_capture(get_backend(key), noise, 48000)
                    self.assertIsNotNone(outcome.failure)
                    self.assertEqual(outcome.failure.kind, DecodeFailureKind.NO_MATCH)
        outcome = EXPERIMENTAL_ADAPTER.decode_capture(get_backend("experimental_16fsk_ldpc"),
                                                     self.audio["experimental_8fsk_ldpc"], 48000)
        self.assertIsNotNone(outcome.failure)

    def test_whole_card_checksum_rejects_a_damaged_packet(self):
        packet = bytearray(weak.packet(self.card))
        packet[weak.HEADER.size + 3] ^= 1
        for key in burst.MODE_KEYS:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "CRC"):
                burst._whole_report(key, bytes(packet), {})

    def test_equalizer_recovers_a_controlled_echo_where_original_receiver_fails(self):
        audio = self.audio[burst.QPSK_KEY]
        audio = audio / np.sqrt(np.mean(audio * audio)) * .1
        noise = np.random.default_rng(64).normal(
            0, .1 * 10 ** (4 / 20) * np.sqrt(48000 / 5000), len(audio))
        source = np.pad(audio + np.pad(.95 * audio[:-48], (48, 0)) + noise, (5000, 7000))
        with self.assertRaises(ValueError):
            weak.decode(source)
        report = get_backend(burst.QPSK_KEY).decode(source, 48000)
        self.assert_verified(burst.QPSK_KEY, report)
        self.assertEqual(report["card"]["diagnostics"]["successful_receiver"], "adaptive_equalizer")

    def test_receive_all_finds_each_new_mode(self):
        for key in burst.MODE_KEYS:
            with self.subTest(key=key):
                outcome = decode_all_card_backends(self.audio[key], 48000,
                    preferred_mode=key, include_experimental=True)
                self.assertIsNone(outcome.failure)
                self.assert_verified(key, outcome.report)

    def test_saved_wav_routes_to_new_backend_and_rejects_activity_only(self):
        qt = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            for key in burst.MODE_KEYS:
                with self.subTest(key=key):
                    path = Path(folder) / (key + ".wav")
                    pixel.write_wav(path, self.audio[key])
                    results = []
                    worker = DecodeWorker(path, Path(folder) / "state.json", mode=key)
                    worker.signals.finished.connect(results.append)
                    worker.run()
                    _path, report, error = results[0]
                    self.assertIsNone(error)
                    self.assert_verified(key, report)
            path = Path(folder) / "partial.wav"
            pixel.write_wav(path, self.audio["experimental_8fsk_ldpc"][:48000])
            results = []
            worker = DecodeWorker(path, Path(folder) / "state.json", mode="experimental_8fsk_ldpc")
            worker.signals.finished.connect(results.append)
            worker.run()
            self.assertIsNone(results[0][1])
            self.assertIn("no decoded card data", results[0][2])
        qt.processEvents()

    def test_qt_live_worker_preserves_activity_and_promotes_verified_cards(self):
        qt = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"PIXELQSO_DATA_DIR": folder}), \
                patch("app.QTimer.singleShot"):
            window = MainWindow()
            window.show_experimental_modes.setChecked(True)
            window.backend_combo.setCurrentIndex(window.backend_combo.findData("experimental"))
            window._record_received_card = Mock()
            window._handle_contact_message = Mock()
            window.rx_timer.start()
            for key in burst.MODE_KEYS:
                with self.subTest(key=key):
                    window.mode_combo.setCurrentIndex(window.mode_combo.findData(key))
                    self.assertEqual(window.repeat_count.value(), 3)
                    self.assertTrue(window.repeat_count.isHidden())
                    audio = window._card_tx_audio(self.card)
                    self.assertAlmostEqual(len(audio) / 48000,
                                           window.repeat_count.value() * (burst.seconds(key)+2*HEADER_SECONDS), places=5)
                    window.card_assembly.clear_current_preview()
                    window.seen_received_cards.clear()
                    window._record_received_card.reset_mock()
                    pcm = (self.audio[key] * 32767).astype("<i2").tobytes()
                    results = []
                    worker = LiveDecodeWorker(pcm, 48000, "auto", window.auto_generation, mode=key)
                    worker.signals.finished.connect(results.append)
                    worker.signals.finished.connect(window._live_decode_finished)
                    window.live_decode_inflight = True
                    window.decode_pool.start(worker)
                    deadline = time.monotonic() + 10
                    while not results and time.monotonic() < deadline:
                        qt.processEvents()
                        time.sleep(.01)
                    qt.processEvents()
                    self.assertTrue(results, "live decode worker did not finish")
                    self.assert_verified(key, results[0][1])
                    window._record_received_card.assert_called_once()
                    self.assertEqual(window.receive_progress.value(), 100)
                    self.assertFalse(window.live_decode_inflight)
            window._record_received_card.reset_mock()
            window.card_assembly.clear_current_preview()
            key = "experimental_8fsk_ldpc"
            pcm = (self.audio[key][:48000] * 32767).astype("<i2").tobytes()
            worker = LiveDecodeWorker(pcm, 48000, "auto", window.auto_generation, mode=key)
            worker.signals.finished.connect(window._live_decode_finished)
            worker.run()
            self.assertEqual(window.listen_indicator.text(), "● RECEIVING SIGNAL")
            self.assertEqual(window.receive_progress.maximum(), 0)
            window._record_received_card.assert_not_called()
            window.rx_timer.stop()
            window.close()
            qt.processEvents()

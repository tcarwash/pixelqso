import unittest
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel

import cardmodem as pixel
from app import (LiveDecodeWorker, MainWindow, decode_all_card_backends,
                 local_adapter_for_mode)
from backend_adapters import (ADAPTERS, AdapterCapabilities, AdapterKind,
                              DecodeFailure, DecodeFailureKind, EXPERIMENTAL_ADAPTER,
                              ExperimentalLocalAdapter, IntegrityScope,
                              register_adapter)
from card_backends import get_backend
from card_transfer import CardAssemblyService, CardPreviewAssembler, fragment


def preview_state():
    assembler = CardPreviewAssembler()
    assembler.begin_window()
    return assembler


class LivePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_data2g_activity_precedes_frames_and_does_not_erase_completion(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"PIXELQSO_DATA_DIR": temp_dir}), \
                patch("app.QTimer.singleShot"):
            window = MainWindow()
            session = SimpleNamespace(port=2, close=Mock(), connected=False)
            window.data2g_session = session
            window._data2g_host_status(SimpleNamespace(port=2), "BUSY ON")
            self.assertTrue(window.receive_preview_box.isHidden())
            window._data2g_host_status(session, "BUSY ON")
            self.assertFalse(window.receive_preview_box.isHidden())
            self.assertIn("Signal detected", window.rx_status.text())
            self.assertEqual(window.listen_indicator.text(), "● RECEIVING SIGNAL")
            self.assertEqual(window.receive_progress.maximum(), 0)
            self.assertFalse(window.receive_activity_timeout.isActive())
            window._data2g_host_status(session, "BUSY OFF")
            self.assertTrue(window.receive_activity_timeout.isActive())
            source = pixel.example_card()
            packed = pixel.minimal_avatar_payload(source)
            with patch.object(window, "save_received_report"), \
                    patch.object(window, "_record_received_card"), \
                    patch.object(window, "_handle_contact_message"):
                window._data2g_rx_frame(session, 2, fragment(source, packed, 0, packed))
            self.assertEqual(window.receive_progress.maximum(), 100)
            self.assertEqual(window.receive_progress.value(), 100)
            self.assertFalse(window.receive_activity_timeout.isActive())
            status = window.rx_status.text()
            window._data2g_host_status(session, "BUSY OFF")
            self.assertEqual(window.rx_status.text(), status)
            window.close()

    def test_receive_all_publishes_preview_before_next_decoder(self):
        report = {"card": {"exact": False}, "pixels": []}
        published = []
        backend = SimpleNamespace(minimum_audio_seconds=lambda: 0)
        adapter = Mock()
        from backend_adapters import DecodeOutcome
        def decode(*_args,**_kwargs):
            if adapter.decode_capture.call_count == 2:
                self.assertEqual(published, [report])
            return DecodeOutcome(report=report)
        adapter.decode_capture.side_effect = decode
        with patch("app.available_modes", return_value=[("resilient_100", backend),
                                                       ("resilient_50", backend)]), \
                patch("app.get_backend", return_value=backend), \
                patch("app.local_adapter_for_mode", return_value=adapter):
            outcome = decode_all_card_backends([0.0] * 32, 48000,
                on_preview=published.append)
        self.assertIs(outcome.report, report)
        self.assertEqual(published, [report])

    def test_station_settings_follow_backend_ownership(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"PIXELQSO_DATA_DIR": temp_dir}), \
                patch("app.QTimer.singleShot"), patch("app.QDialog.exec", return_value=0):
            window = MainWindow()
            window.auto_connect_cat.setChecked(False)
            window.open_station_settings()

            self.assertEqual(window.backend_combo.currentData(), "data2g")
            self.assertFalse(window.data2g_settings_group.isHidden())
            self.assertTrue(window.cat_settings_group.isHidden())
            self.assertTrue(window.experimental_audio_group.isHidden())
            self.assertTrue(window.receive_all_modes.isHidden())
            self.assertTrue(window.receive_all_modes_label.isHidden())

            window.show_experimental_modes.setChecked(True)
            window.backend_combo.setCurrentIndex(window.backend_combo.findData("experimental"))
            self.assertTrue(window.data2g_settings_group.isHidden())
            self.assertFalse(window.cat_settings_group.isHidden())
            self.assertFalse(window.experimental_audio_group.isHidden())
            self.assertFalse(window.receive_all_modes.isHidden())
            self.assertIn("Used only by experimental local modems",
                          " ".join(label.text() for label in
                                   window.cat_settings_group.findChildren(QLabel)))
            window.close()
            self.app.processEvents()

    def test_decode_failure_uses_shared_receive_status_for_data2g_mode_key(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"PIXELQSO_DATA_DIR": temp_dir}), \
                patch("app.QTimer.singleShot"):
            window = MainWindow()
            self.assertTrue(window.selected_mode().startswith("data2g_"))
            window.rx_timer.start()
            window._live_decode_finished((window.auto_generation, None, False,
                                          "No checked Data2G card fragment received yet", {}))
            self.assertEqual(window.rx_status.text(),
                             "Listening · no complete card decoded yet")
            failure = DecodeFailure(EXPERIMENTAL_ADAPTER.key,
                                    DecodeFailureKind.BACKEND_ERROR,
                                    "decoder worker failed", False)
            window._live_decode_finished((window.auto_generation, None, False, failure, {}))
            self.assertEqual(window.rx_status.text(),
                             "Receive decoder error: decoder worker failed")
            window.rx_timer.stop()
            window.close()
            self.app.processEvents()

    def test_live_decode_worker_emits_adapter_owned_failure(self):
        class NoMatchBackend:
            def decode(self, *_args):
                raise ValueError("no valid avatar identity header received yet")

        results = []
        samples = bytes(4800 * 2)
        worker = LiveDecodeWorker(samples, 48000, "auto", 17, mode="resilient_100")
        worker.signals.finished.connect(results.append)
        with patch("app.get_backend", return_value=NoMatchBackend()):
            worker.run()
        self.assertEqual(len(results), 1)
        generation, report, complete, failure, stats = results[0]
        self.assertEqual(generation, 17)
        self.assertIsNone(report)
        self.assertFalse(complete)
        self.assertEqual(failure.kind, DecodeFailureKind.NO_MATCH)
        self.assertTrue(failure.retryable)
        self.assertEqual(stats["mode"], "resilient_100")

    def test_legacy_live_decode_uses_adapter_and_requires_legacy_end_marker(self):
        card = {"card_id": 9, "exact": True}
        report = {"card": card, "pixels": [], "complete": False,
                  "valid_packet_count": 4, "fresh_packets": [], "receive_profile": "wide"}
        adapter = Mock()
        adapter.decode_legacy_audio.return_value = SimpleNamespace(report=report, failure=None)
        results = []
        worker = LiveDecodeWorker(bytes(96000), 48000, "auto", 21, mode="standard")
        worker.signals.finished.connect(results.append)
        with patch("app.local_adapter_for_mode", return_value=adapter):
            worker.run()

        self.assertEqual(adapter.decode_legacy_audio.call_count, 1)
        self.assertEqual(len(results), 1)
        self.assertIs(results[0][1], report)
        self.assertFalse(results[0][2])

    def test_mode_registry_routes_decode_to_compatible_adapter(self):
        class CompatibleAdapter(ExperimentalLocalAdapter):
            key = "test-custom-image-modem"
            capabilities = AdapterCapabilities(
                AdapterKind.LOCAL_IMAGE_MODEM, "custom", "custom-radio",
                "local_waveform_decode", "local_audio_completion",
                IntegrityScope.CHECKED_REGION, True, True, True)

        adapter = CompatibleAdapter()
        register_adapter(adapter)
        try:
            with patch("app.MODE_REGISTRY", {
                    "test-custom-mode": SimpleNamespace(adapter=adapter.key)}):
                self.assertIs(local_adapter_for_mode("test-custom-mode"), adapter)
        finally:
            ADAPTERS.pop(adapter.key, None)

    def test_auto_decode_uses_registered_mode_adapter(self):
        key = "test-auto-custom-mode"

        class CompatibleAdapter(ExperimentalLocalAdapter):
            key = "test-auto-custom-adapter"
            capabilities = AdapterCapabilities(
                AdapterKind.LOCAL_IMAGE_MODEM, "custom", "custom-radio",
                "local_waveform_decode", "local_audio_completion",
                IntegrityScope.CHECKED_REGION, True, True, True)

        class Backend:
            @staticmethod
            def minimum_audio_seconds():
                return 0

            @staticmethod
            def decode(*_args):
                return {"card": {"exact": True}, "pixels": []}

        adapter = CompatibleAdapter()
        backend = Backend()
        register_adapter(adapter)
        try:
            with patch("app.MODE_REGISTRY", {
                    key: SimpleNamespace(adapter=adapter.key,
                                         supports_audio_placement=False)}), \
                    patch("app.available_modes", return_value=[(key, backend)]), \
                    patch("app.get_backend", return_value=backend):
                outcome = decode_all_card_backends([0.0] * 32, 48000,
                                                   preferred_mode=key,
                                                   include_experimental=True)
            self.assertTrue(outcome.report["card"]["exact"])
            self.assertIsNone(outcome.failure)
        finally:
            ADAPTERS.pop(adapter.key, None)

    def test_data2g_qt_receive_actions_wait_for_service_completion_event(self):
        source = pixel.example_card()
        packed = pixel.minimal_avatar_payload(source)
        split = len(packed) // 2
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"PIXELQSO_DATA_DIR": temp_dir}):
            window = MainWindow()
            window.data2g_session = SimpleNamespace(port=8100)
            window.seen_received_cards = set()
            wall = Mock()
            saved = Mock()
            contact = Mock()
            window._add_session_wall_card = wall
            window._record_received_card = saved
            window._handle_contact_message = contact
            window.save_received_report = Mock()
            window._show_receive_preview = Mock()
            window.card_image = Mock(return_value=None)

            window._data2g_rx_frame(window.data2g_session, 8100, fragment(source, packed, 0, packed[:split], "cq"))
            self.assertEqual(wall.call_count, 0)
            self.assertEqual(saved.call_count, 0)
            self.assertEqual(contact.call_count, 0)

            window._data2g_rx_frame(window.data2g_session, 8100, fragment(source, packed, split,
                                                   packed[split:], "exchange"))
            self.assertEqual(wall.call_count, 1)
            self.assertEqual(saved.call_count, 1)
            self.assertEqual(contact.call_count, 1)
            self.assertEqual(window.save_received_report.call_count, 1)
            window.data2g_session = None
            window.close()
            self.app.processEvents()

    def test_checked_data2g_fragment_pixels_stay_verified_before_raster_completion(self):
        source = pixel.example_card()
        packed = pixel.minimal_avatar_payload(source)
        service = CardAssemblyService()
        _assembly, metadata = service.feed_transport_frame(
            source.callsign, fragment(source, packed, 0, packed[:len(packed) // 2], "cq"))
        self.assertFalse(metadata["exact"])
        self.assertEqual(metadata["verified_pixel_coverage"], metadata["pixel_coverage"])
        card = {**metadata, "palette": [list(color) for color in pixel.AVATAR_PALETTES[metadata["palette_id"]]],
                "avatar_burst": True, "received_copies": 1}
        partial, _ = service.merge_preview({"card": card, "pixels": metadata["pixels"]})
        event = partial["assembly_event"]
        self.assertFalse(event["verified_complete"])
        self.assertGreater(event["verified_pixels"], 0)
        self.assertEqual(event["verification_scope"], "checked_data2g_fragment_regions")
        self.assertTrue(any(partial["card"]["verified_pixel_coverage"]))

        # Candidate pixels from another decoder/copy may add preview coverage,
        # but they cannot replace a region carried by a checked host frame.
        verified_index = partial["card"]["verified_pixel_coverage"].index(True)
        checked_pixel = partial["pixels"][verified_index]
        candidate = {**partial["card"], "exact": False,
                     "verified_pixel_coverage": [False] * len(partial["pixels"]),
                     "pixel_coverage": [True] * len(partial["pixels"])}
        pixels = list(partial["pixels"])
        pixels[verified_index] = (checked_pixel + 1) % len(source.palette)
        merged, _ = service.merge_preview({"card": candidate, "pixels": pixels})
        self.assertEqual(merged["pixels"][verified_index], checked_pixel)
        self.assertGreater(merged["assembly_event"]["verified_pixels"], 0)

    def test_local_receive_uses_adapter_owned_audio_handle_across_tx_handoff(self):
        class Device:
            def isFormatSupported(self, _audio_format):
                return True

            def description(self):
                return "fake input"

            def id(self):
                return bytes((1, 2, 3))

        class Source:
            instances = []

            def __init__(self, device, audio_format, parent):
                self.started = 0
                self.stopped = 0
                self.stream = object()
                self.instances.append(self)

            def start(self):
                self.started += 1
                return self.stream

            def stop(self):
                self.stopped += 1

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"PIXELQSO_DATA_DIR": temp_dir}), \
                patch("app.QAudioSource", Source):
            window = MainWindow()
            window.show_experimental_modes.setChecked(True)
            window.auto_connect_cat.setChecked(False)
            window.backend_combo.setCurrentIndex(1)
            device = Device()
            window.input_device.clear()
            window.input_device.addItem("fake input", device)

            window.start_receive(quiet=True)
            source = Source.instances[-1]
            self.assertIs(window.local_receive_adapter, EXPERIMENTAL_ADAPTER)
            self.assertIs(window.rx_audio, source)
            self.assertIs(window.rx_device, source.stream)
            self.assertEqual(source.started, 1)

            window._discard_receive_for_tx()
            self.assertEqual(source.stopped, 1)
            self.assertIsNone(window.local_receive_handle)
            self.assertIsNone(window.rx_audio)
            self.assertFalse(window.rx_timer.isActive())
            window.close()
            self.app.processEvents()

    def test_local_receive_adapter_error_is_shown_without_starting_listener(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"PIXELQSO_DATA_DIR": temp_dir}):
            window = MainWindow()
            window.show_experimental_modes.setChecked(True)
            window.auto_connect_cat.setChecked(False)
            window.backend_combo.setCurrentIndex(1)
            window.input_device.clear()

            window.start_receive(quiet=True)

            self.assertIn("select an audio input", window.rx_status.text())
            self.assertFalse(window.rx_timer.isActive())
            self.assertIsNone(window.local_receive_handle)
            self.assertEqual(window.listen_indicator.text(), "● NOT LISTENING")
            window.close()
            self.app.processEvents()

    def test_assembly_service_emits_explicit_completion_only_after_integrity(self):
        service = CardAssemblyService()
        base = {"callsign": "AG7SU", "grid": "CN85", "card_id": 7,
                "width": 2, "height": 1, "palette": [[0, 0, 0], [15, 0, 0]],
                "avatar_burst": True, "image_crc32": 123,
                "whole_raster_crc32_valid": False, "content_tag_valid": False,
                "image_id": "raster-123", "bits_per_pixel": 3}
        partial = {"card": {**base, "exact": False,
                            "pixel_coverage": [True, False]},
                   "pixels": [1, 0]}
        preview, _ = service.merge_preview(partial)
        self.assertFalse(preview["assembly_event"]["verified_complete"])
        self.assertEqual(preview["assembly_event"]["verification_scope"],
                         "whole_raster_crc32_and_content_tag")

        forged_tag = {"card": {**base, "exact": True,
                                "whole_raster_crc32_valid": True,
                                "content_tag_valid": False,
                                "pixel_coverage": [True, True]},
                      "pixels": [1, 1]}
        rejected, _ = service.merge_preview(forged_tag)
        self.assertFalse(rejected["assembly_event"]["verified_complete"])
        self.assertEqual(rejected["assembly_event"]["verification_scope"],
                         "whole_raster_crc32_and_content_tag")

        complete = {"card": {**base, "exact": True,
                              "whole_raster_crc32_valid": True,
                              "content_tag_valid": True,
                              "pixel_coverage": [True, True]},
                    "pixels": [1, 1]}
        received, _ = service.merge_preview(complete)
        self.assertTrue(received["assembly_event"]["verified_complete"])
        self.assertEqual(received["assembly_event"]["verified_pixels"], 2)

    def test_preview_cache_refines_across_receive_windows_and_is_bounded(self):
        service = CardAssemblyService(max_preview_images=2)

        def candidate(image_id, coverage, pixels):
            return {"card": {"callsign": "K1ABC", "grid": "FN31", "card_id": 1,
                              "width": 2, "height": 1,
                              "palette": [[0, 0, 0], [15, 0, 0]],
                              "avatar_burst": True, "image_id": image_id,
                              "pixel_coverage": coverage, "exact": False,
                              "received_copies": service.previews.window + 1},
                    "pixels": pixels}

        service.merge_preview(candidate("image-a", [True, False], [1, 0]))
        service.merge_preview(candidate("image-b", [True, False], [0, 0]))
        service.begin_receive_window()
        resumed, _ = service.merge_preview(candidate("image-a", [True, True], [1, 1]))
        self.assertEqual(len(service.previews.states), 2)
        self.assertEqual(resumed["pixels"], [1, 1])
        self.assertEqual(sum(resumed["card"]["pixel_coverage"]), 2)

        service.merge_preview(candidate("image-c", [True, False], [0, 0]))
        self.assertEqual(len(service.previews.states), 2)
        self.assertNotIn("image-b", [key[-1] for key in service.previews.states])

    def test_legacy_preview_identity_ignores_qso_stage_but_keeps_wire_version(self):
        service = CardAssemblyService()
        base = {"callsign": "K1ABC", "grid": "FN31", "card_id": 44,
                "width": 2, "height": 1, "palette": [[0, 0, 0], [15, 0, 0]],
                "avatar_burst": True, "header_version": 3, "avatar_mode": "fast_avatar_fec",
                "exact": False, "received_copies": 1}
        service.merge_preview({"card": {**base, "message_type": "cq",
                                         "pixel_coverage": [True, False]},
                              "pixels": [1, 0]})
        service.begin_receive_window()
        refined, _ = service.merge_preview({"card": {**base, "message_type": "exchange",
                                                       "received_copies": 2,
                                                       "pixel_coverage": [True, True]},
                                            "pixels": [1, 1]})
        self.assertEqual(len(service.previews.states), 1)
        self.assertEqual(refined["pixels"], [1, 1])

        # A versioned/content-addressed burst with the same numeric card ID is
        # kept separate from a legacy identity that lacks a content digest.
        v4 = {**base, "header_version": 4, "image_id": "v4-image",
              "whole_raster_valid": True, "received_blocks": [True]}
        service.merge_preview({"card": v4, "pixels": [1, 1]})
        self.assertEqual(len(service.previews.states), 2)

    def test_transport_assembly_survives_a_receive_window_restart(self):
        service = CardAssemblyService()
        source = pixel.example_card()
        packed = pixel.minimal_avatar_payload(source)
        split = len(packed) // 2
        first = fragment(source, packed, 0, packed[:split], "cq")
        last = fragment(source, packed, split, packed[split:], "exchange")

        assembly, _ = service.feed_transport_frame(source.callsign, first)
        self.assertFalse(assembly.exact)
        service.begin_receive_window()  # New listener/window; session cache persists.
        assembly, metadata = service.feed_transport_frame(source.callsign, last)
        self.assertTrue(assembly.exact)
        self.assertTrue(metadata["whole_raster_crc32_valid"])
        self.assertTrue(metadata["content_tag_valid"])

    def test_v4_wire_carries_canonical_whole_raster_identity(self):
        source = pixel.example_card()
        symbols = pixel.minimal_avatar_resilient_cycle_symbols(source)
        offset = len(pixel.FRAME_SYNC)
        metadata, end = pixel._decode_avatar_metadata_after(symbols, offset)
        packed = pixel.minimal_avatar_payload(source)

        self.assertEqual(metadata["header_version"], 4)
        self.assertEqual(metadata["image_crc32"], pixel.zlib.crc32(packed))
        self.assertEqual(metadata["image_tag"], pixel.hashlib.blake2s(packed, digest_size=4).hexdigest())
        self.assertEqual(metadata["image_id"],
                         f"{pixel.zlib.crc32(packed):08x}{pixel.hashlib.blake2s(packed, digest_size=4).hexdigest()}")
        self.assertGreater(end, offset)

        _, blocks, _, decoded = pixel.decode_minimal_avatar_resilient_symbols(symbols)
        self.assertTrue(all(blocks))
        self.assertTrue(decoded["whole_raster_crc32_valid"])
        self.assertTrue(decoded["content_tag_valid"])
        self.assertTrue(decoded["whole_raster_valid"])

    def test_v4_reused_card_id_never_combines_different_rasters(self):
        first_card = pixel.example_card()
        second_card = pixel.Card(first_card.callsign, first_card.grid,
                                 first_card.palette[:], first_card.pixels[:],
                                 first_card.card_id, first_card.width, first_card.height)
        second_card.pixels[0] = (second_card.pixels[0] + 1) % len(second_card.palette)
        first_symbols = pixel.minimal_avatar_resilient_cycle_symbols(first_card, wire_version=4)
        second_symbols = pixel.minimal_avatar_resilient_cycle_symbols(second_card, wire_version=4)

        first_decoded = pixel.decode_minimal_avatar_resilient_symbols(first_symbols)
        second_decoded = pixel.decode_minimal_avatar_resilient_symbols(second_symbols)
        self.assertNotEqual(first_decoded[3]["image_id"], second_decoded[3]["image_id"])

        # Interleaved complete copies with the same numeric card ID are
        # content-addressed separately; the decoder returns one intact raster.
        merged = pixel.decode_minimal_avatar_resilient_symbols(first_symbols + second_symbols)
        self.assertIn(merged[3]["image_id"],
                      {first_decoded[3]["image_id"], second_decoded[3]["image_id"]})
        expected = (first_decoded[0] if merged[3]["image_id"] == first_decoded[3]["image_id"]
                    else second_decoded[0])
        self.assertEqual(merged[0], expected)
        self.assertTrue(merged[3]["whole_raster_valid"])

    def test_v4_blocks_from_another_raster_fail_identity_binding(self):
        first_card = pixel.example_card()
        second_card = pixel.Card(first_card.callsign, first_card.grid,
                                 first_card.palette[:], first_card.pixels[:],
                                 first_card.card_id, first_card.width, first_card.height)
        second_card.pixels[0] = (second_card.pixels[0] + 1) % len(second_card.palette)
        first = pixel.minimal_avatar_resilient_cycle_symbols(first_card, wire_version=4)
        second = pixel.minimal_avatar_resilient_cycle_symbols(second_card, wire_version=4)

        metadata, block_start = pixel._decode_avatar_metadata_after(first, len(pixel.FRAME_SYNC))
        self.assertNotEqual(metadata["image_tag"],
                            pixel._decode_avatar_metadata_after(second, len(pixel.FRAME_SYNC))[0]["image_tag"])
        pixels_per_block = ((pixel.AVATAR_RS_K - pixel.AVATAR_V4_BLOCK_OVERHEAD) * 6 //
                            metadata["bits_per_pixel"])
        block_tones = []
        offset = block_start
        for start in range(0, metadata["width"] * metadata["height"], pixels_per_block):
            count = min(pixels_per_block, metadata["width"] * metadata["height"] - start)
            data_count = (count * metadata["bits_per_pixel"] + 5) // 6 + pixel.AVATAR_V4_BLOCK_OVERHEAD
            length = (data_count + 2) * 2 + 6
            block_tones.append((first[offset:offset + length], second[offset:offset + length]))
            offset += length

        hybrid = (tuple(first[:block_start]) + block_tones[0][0] + tuple(
            tone for _block_a, block_b in block_tones[1:] for tone in block_b))
        _, received, _, decoded = pixel.decode_minimal_avatar_resilient_symbols(hybrid)
        self.assertTrue(received[0])
        self.assertFalse(any(received[1:]))
        self.assertFalse(decoded["whole_raster_valid"])
        report = {"card": {**decoded, "avatar_burst": True,
                           "received_blocks": received, "exact": False}}
        self.assertFalse(CardAssemblyService.assess(report).verified_complete)

    def test_repeated_v3_and_v4_cycles_are_both_acquired_and_decoded(self):
        source = pixel.example_card()
        for mode in ("fast_avatar_fec", "fast_avatar_fec_v4"):
            with self.subTest(mode=mode):
                backend = get_backend(mode)
                audio = backend.encode(source, 2, "cq", None)
                report = backend.decode(audio, 48000)
                self.assertTrue(report["card"]["exact"])
                self.assertEqual(report["card"]["header_version"], 3 if mode.endswith("fec") else 4)
                if mode.endswith("v4"):
                    self.assertTrue(report["card"]["whole_raster_valid"])

    def test_v3_raw_header_remains_decodable_with_legacy_integrity_scope(self):
        symbols = pixel.minimal_avatar_symbols(pixel.example_card())
        start = len(pixel.FRAME_SYNC)
        metadata, _ = pixel._decode_avatar_metadata_after(symbols, start)
        self.assertEqual(metadata["header_version"], 3)
        self.assertNotIn("whole_raster_valid", metadata)

    def test_v3_resilient_bursts_remain_decodable_without_claiming_raster_crc(self):
        source = pixel.example_card()
        old = pixel._decode_avatar_metadata(
            pixel._encode_avatar_metadata(source, resilient=False))
        base = pixel._avatar_metadata(source, resilient=False)["body"]
        fields = pixel.AVATAR_META_STRUCT.unpack(base)
        body = pixel.AVATAR_META_STRUCT.pack(*fields[:10], 2, fields[11], fields[12])
        wire = body + pixel.crc16(body).to_bytes(2, "big")
        data = pixel._bytes_to_gf64(wire)
        shortened = pixel.AVATAR_RS_K - len(data)
        header = pixel._gf64_to_tones(pixel._rs64_encode([0] * shortened + data)[shortened:])

        bpp = old["bits_per_pixel"]
        pixels_per_block = (pixel.AVATAR_RS_K * 6) // bpp
        values = pixel.unpack_indices(pixel.minimal_avatar_payload(source), source.width * source.height)
        symbols = list(pixel.FRAME_SYNC + tuple(header))
        for start in range(0, len(values), pixels_per_block):
            block = pixel._pixels_to_gf64(values[start:start + pixels_per_block], bpp)
            symbols.extend(pixel._avatar_block_symbols(block))

        recovered, blocks, _, metadata = pixel.decode_minimal_avatar_resilient_symbols(symbols)
        self.assertEqual(metadata["header_version"], 3)
        self.assertTrue(all(blocks))
        self.assertNotIn("whole_raster_valid", metadata)
        self.assertEqual(recovered, values)

    def test_assembly_verification_uses_integrity_not_union_of_preview_coverage(self):
        candidate = {"card": {"avatar_burst": True, "exact": False,
                              "image_crc32": 123, "pixel_coverage": [True] * 8}}
        decision = CardAssemblyService.assess(candidate)
        self.assertFalse(decision.verified_complete)
        self.assertEqual(decision.scope, "whole_raster_crc32")
        self.assertEqual(decision.candidate_pixels, 8)

        blocks = {"card": {"avatar_burst": True, "exact": False,
                           "received_blocks": [True, False],
                           "pixel_coverage": [True] * 8}}
        self.assertFalse(CardAssemblyService.assess(blocks).verified_complete)

        complete = {"card": {"avatar_burst": True, "exact": True,
                              "image_crc32": 123, "pixel_coverage": [True] * 8}}
        self.assertTrue(CardAssemblyService.assess(complete).verified_complete)

        v4_blocks_only = {"card": {"avatar_burst": True, "header_version": 4,
                                    "exact": True, "received_blocks": [True, True],
                                    "whole_raster_valid": False,
                                    "pixel_coverage": [True] * 8}}
        self.assertFalse(CardAssemblyService.assess(v4_blocks_only).verified_complete)
        v4_complete = {"card": {**v4_blocks_only["card"], "whole_raster_valid": True}}
        self.assertTrue(CardAssemblyService.assess(v4_complete).verified_complete)

    def test_crc_failed_fec_block_stays_visible_as_unverified_preview(self):
        source = pixel.example_card()
        symbols = list(pixel.minimal_avatar_resilient_cycle_symbols(
            source, message_type="cq", wire_version=4))
        first_block = len(pixel.FRAME_SYNC) + pixel.AVATAR_META_V4_TONE_COUNT
        for gf_symbol in (0, 1, 2, 3):
            symbols[first_block + gf_symbol * 2] ^= 1
        audio = pixel.synthesize(symbols, profile=pixel.MINIMAL_AVATAR_PROFILE)
        decoded = get_backend("fast_avatar_fec_v4").decode(audio, 48000)

        state = preview_state()
        report, changed = state.merge(decoded)
        card = report["card"]
        pixels_per_block = (61 * 6) // card["bits_per_pixel"]

        self.assertTrue(changed)
        self.assertFalse(card["received_blocks"][0], "CRC failure must not verify the block")
        self.assertTrue(all(card["pixel_coverage"][:pixels_per_block]),
                        "received candidate pixels should remain visible")
        self.assertFalse(any(state.current_state["verified_pixels"][:pixels_per_block]))
        image = MainWindow.card_image(None, report)
        self.assertIsNotNone(image)
        for index in range(pixels_per_block):
            rgb = card["palette"][report["pixels"][index]]
            expected = tuple(int(channel) * 17 for channel in rgb)
            actual = image.pixelColor(index % card["width"], index // card["width"])
            self.assertEqual((actual.red(), actual.green(), actual.blue()), expected)

    def test_unverified_whole_card_pixels_are_previewed_not_promoted(self):
        card = {"callsign": "AG7SU", "grid": "CN85", "card_id": 0x1234,
                "width": 4, "height": 2, "palette": [[0, 0, 0], [15, 0, 0]],
                "avatar_burst": True, "exact": False,
                "pixel_coverage": [True] * 8, "received_copies": 1}
        report, changed = preview_state().merge({"card": card, "pixels": [1] * 8})

        self.assertTrue(changed)
        self.assertFalse(report["card"]["exact"])
        self.assertTrue(all(report["card"]["pixel_coverage"]))
        self.assertEqual(report["pixels"], [1] * 8)
        image = MainWindow.card_image(None, report)
        self.assertEqual(QColor(image.pixelColor(0, 0)).name(), "#ff0000")

    def test_new_unverified_pixels_appear_as_more_data_blocks_arrive(self):
        state = preview_state()
        card = {"callsign": "AG7SU", "grid": "CN85", "card_id": 0x1234,
                "width": 4, "height": 2, "palette": [[0, 0, 0], [15, 0, 0]],
                "avatar_burst": True, "exact": False,
                "pixel_coverage": [True] * 4 + [False] * 4, "received_copies": 1}
        first, _ = state.merge({"card": card, "pixels": [1] * 8})
        self.assertEqual(first["pixels"], [1] * 4 + [0] * 4)

        # A later Data2G fragment adds the second half of the same image. It
        # does not increment copy count, but its newly covered pixels are still
        # useful provisional preview data.
        later_card = {**card, "pixel_coverage": [True] * 8}
        later, changed = state.merge({"card": later_card, "pixels": [1] * 8})

        self.assertTrue(changed)
        self.assertEqual(later["pixels"], [1] * 8)
        self.assertFalse(any(state.current_state["verified_pixels"]))
        image = MainWindow.card_image(None, later)
        self.assertEqual(QColor(image.pixelColor(0, 1)).name(), "#ff0000")

    def test_preview_snapshot_is_detached_and_immutable(self):
        service = CardAssemblyService()
        card = {"callsign": "AG7SU", "grid": "CN85", "card_id": 7,
                "width": 2, "height": 1, "palette": [[0, 0, 0], [15, 0, 0]],
                "avatar_burst": True, "exact": False,
                "pixel_coverage": [True, False], "received_copies": 1}
        service.merge_preview({"card": card, "pixels": [1, 0]})
        snapshot = service.preview_snapshot()
        self.assertEqual(snapshot.pixels, (1, 0))
        self.assertEqual(snapshot.coverage, (True, False))
        with self.assertRaises(TypeError):
            snapshot.card["exact"] = True
        with self.assertRaises(TypeError):
            snapshot.card["palette"][0][0] = 9
        # Mutating the decoder's original report cannot mutate cached evidence.
        card["exact"] = True
        self.assertFalse(service.preview_snapshot().card["exact"])

    def test_content_identity_merges_mode_changes_and_rejects_verified_conflicts_atomically(self):
        service = CardAssemblyService()
        common = {"callsign": "AG7SU", "grid": "CN85", "card_id": 19,
                  "width": 2, "height": 1, "palette": [[0, 0, 0], [15, 0, 0]],
                  "avatar_burst": True, "image_id": "same-content", "bits_per_pixel": 3,
                  "pixel_coverage": [True, True], "received_blocks": [True],
                  "block_checksums": [123], "exact": False}
        first = {"card": {**common, "avatar_mode": "resilient_100"}, "pixels": [0, 1]}
        service.merge_preview(first)
        second = {"card": {**common, "avatar_mode": "Data2G"}, "pixels": [0, 1]}
        service.merge_preview(second)
        self.assertEqual(len(service.previews.states), 1,
                         "stable content identity should survive a backend/mode change")
        sequence_before_conflict = service.previews.sequence
        current_key_before_conflict = service.previews.current_key
        conflicting = {"card": {**common, "avatar_mode": "Data2G"}, "pixels": [1, 1]}
        with self.assertRaisesRegex(ValueError, "conflicting verified pixels"):
            service.merge_preview(conflicting)
        self.assertEqual(service.preview_snapshot().pixels, (0, 1),
                         "a rejected conflicting report must not partially mutate the assembly")
        self.assertEqual(service.previews.sequence, sequence_before_conflict)
        self.assertEqual(service.previews.current_key, current_key_before_conflict)


if __name__ == "__main__":
    unittest.main()

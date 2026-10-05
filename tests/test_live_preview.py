import unittest
from PySide6.QtGui import QColor

import cardmodem as pixel
from app import MainWindow
from card_backends import get_backend
from card_transfer import CardAssemblyService, CardPreviewAssembler, fragment


def preview_state():
    assembler = CardPreviewAssembler()
    assembler.begin_window()
    return assembler


class LivePreviewTests(unittest.TestCase):
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

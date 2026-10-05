import unittest
from types import SimpleNamespace

from card_transfer import Assembly, CardTransferReceiver, HEADER, fragment


class CardTransferTests(unittest.TestCase):
    def setUp(self):
        self.card = SimpleNamespace(card_id=71, width=4, height=2,
                                    palette=[(0, 0, 0)] * 8,
                                    callsign="AG7SU", grid="CN85")
        self.packed = bytes((0x05, 0x39, 0x77))  # 8 palette indices at 3 bits each.

    def test_out_of_order_variable_sized_duplicate_and_refinement(self):
        rx = CardTransferReceiver()
        frames = [fragment(self.card, self.packed, 0, self.packed[:1]),
                  fragment(self.card, self.packed, 1, self.packed[1:2], "cq"),
                  fragment(self.card, self.packed, 2, self.packed[2:])]
        assembly, meta = rx.feed("ag7su", frames[2])
        self.assertFalse(assembly.exact)
        assembly, _ = rx.feed("AG7SU", frames[0])
        self.assertFalse(assembly.exact)
        assembly, meta = rx.feed("AG7SU", frames[1])
        self.assertTrue(assembly.exact)
        self.assertEqual(bytes(assembly.data), self.packed)
        self.assertEqual(meta["message_type"], "cq")
        self.assertEqual(meta["pixel_coverage"], [True] * 8)
        self.assertTrue(all(0 <= value < 8 for value in meta["pixels"]))
        self.assertEqual(len(rx.assemblies), 1)
        rx.feed("AG7SU", frames[0])
        self.assertEqual(len(rx.assemblies), 1)

    def test_interleaved_images_and_senders_stay_separate(self):
        rx = CardTransferReceiver()
        other = bytes(reversed(self.packed))
        a, _ = rx.feed("AG7SU", fragment(self.card, self.packed, 0, self.packed[:1]))
        b, _ = rx.feed("K1ABC", fragment(self.card, other, 0, other[:1]))
        self.assertIsNot(a, b)
        self.assertEqual(len(rx.assemblies), 2)
        resumed, _ = rx.feed("AG7SU", fragment(self.card, self.packed, 1, self.packed[1:]))
        self.assertIs(resumed, a)
        self.assertTrue(resumed.exact)

    def test_content_identity_separates_images_with_reused_card_id(self):
        rx = CardTransferReceiver()
        second = bytes(value ^ 0x01 for value in self.packed)
        first_assembly, _ = rx.feed("AG7SU", fragment(self.card, self.packed, 0, self.packed))
        second_assembly, _ = rx.feed("AG7SU", fragment(self.card, second, 0, second))
        self.assertIsNot(first_assembly, second_assembly)
        self.assertEqual(len(rx.assemblies), 2)

    def test_content_identity_merges_refinement_after_card_id_changes(self):
        rx = CardTransferReceiver()
        first, _ = rx.feed("AG7SU", fragment(self.card, self.packed, 0, self.packed[:1]))
        revised_card_id = SimpleNamespace(**{**vars(self.card), "card_id": 991})
        resumed, _ = rx.feed("AG7SU", fragment(revised_card_id, self.packed, 1, self.packed[1:]))
        self.assertIs(resumed, first)
        self.assertTrue(resumed.exact)
        self.assertEqual(len(rx.assemblies), 1)

    def test_complete_transfer_requires_both_raster_crc_and_content_tag(self):
        frame = bytearray(fragment(self.card, self.packed, 0, self.packed))
        frame[HEADER.size - 1] ^= 0x01
        assembly, metadata = CardTransferReceiver().feed("AG7SU", bytes(frame))
        self.assertTrue(all(assembly.present))
        self.assertFalse(assembly.exact)
        self.assertTrue(metadata["whole_raster_crc32_valid"])
        self.assertFalse(metadata["content_tag_valid"])

    def test_assembly_cache_is_bounded(self):
        rx = CardTransferReceiver(max_assemblies=2)
        assemblies = []
        for card_id in (71, 72, 73):
            card = SimpleNamespace(**{**vars(self.card), "card_id": card_id})
            packed = bytes(value ^ (card_id & 0xff) for value in self.packed)
            assembly, _ = rx.feed("AG7SU", fragment(card, packed, 0, packed[:1]))
            assemblies.append(assembly)
        self.assertEqual(len(rx.assemblies), 2)
        self.assertNotIn(71, [assembly.metadata[0] for assembly in rx.assemblies.values()])

    def test_corruption_and_conflicts_never_promote(self):
        rx = CardTransferReceiver()
        damaged = bytearray(self.packed)
        damaged[-1] ^= 1
        frame = fragment(self.card, self.packed, 0, bytes(damaged))
        assembly, _ = rx.feed("AG7SU", frame)
        self.assertFalse(assembly.exact)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            rx.feed("AG7SU", fragment(self.card, self.packed, 0, b"bad"))

    def test_conflicting_fragment_is_rejected_before_any_bytes_are_applied(self):
        assembly = Assembly((), 4, 0)
        assembly.add(1, b"x")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            assembly.add(0, b"ay")
        self.assertEqual(assembly.present, bytearray((0, 1, 0, 0)))
        self.assertEqual(assembly.data[0], 0)

    def test_header_is_versioned_and_checks_dimensions(self):
        frame = fragment(self.card, self.packed, 0, self.packed)
        self.assertEqual(len(frame), HEADER.size + 3)
        damaged = b"NOPE" + frame[4:]
        with self.assertRaisesRegex(ValueError, "malformed"):
            CardTransferReceiver().feed("AG7SU", damaged)


if __name__ == "__main__":
    unittest.main()

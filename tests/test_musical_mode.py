import unittest
import numpy as np
import modem_envelope as envelope
from copy_combining import ADMITTED_IDENTITY

import cardmodem as pixel
from card_backends import get_backend


class MusicalModeTests(unittest.TestCase):
    def test_palette_symbols_play_and_decode_as_e_major_scale(self):
        key = "experimental_8fsk_musical_25"
        backend = get_backend(key)
        card = pixel.example_card()
        card.pixels[-4:] = [1, 2, 3, 4]

        audio = backend.encode(card, 1, "cq", None)
        report = backend.decode(audio, 48000)

        self.assertIsNone(report.get("failure"))
        self.assertEqual(report["card"]["avatar_mode"], key)
        expected = pixel.unpack_indices(pixel.minimal_avatar_payload(card), 1024)
        self.assertEqual(len(report["pixels"]), 1024)
        self.assertEqual(report["pixels"], expected)
        self.assertEqual(sum(report["card"]["pixel_coverage"]), 1024)
        self.assertTrue(report["card"]["exact"])
        self.assertTrue(report["card"]["content_checksum_valid"])
        self.assertEqual(backend.profile.baud, 25)
        self.assertEqual(len(backend.profile.tones_hz), 8)

    def test_partial_new_copy_preserves_complete_verified_raster(self):
        backend = get_backend("experimental_8fsk_musical_25")
        card = pixel.example_card()
        card.pixels[-4:] = [1, 2, 3, 4]
        audio = backend.encode(card, 1, "cq", None)
        copies = envelope.find_copies(audio, 48000)
        payload = audio[copies[0].start:copies[0].end]
        padded = np.pad(payload, (2400, 2400))
        token = ADMITTED_IDENTITY.set((backend.key, copies[0].identity))
        try:
            report = backend.decode_payload_copies([padded, padded[:len(padded)//2]], 48000)
        finally:
            ADMITTED_IDENTITY.reset(token)
        self.assertEqual(report["pixels"], pixel.unpack_indices(pixel.minimal_avatar_payload(card), 1024))
        self.assertEqual(envelope.report_content_id(backend.key, report).hex(), copies[0].identity)

    def test_unverified_combination_does_not_replace_single_copy_preview(self):
        backend = get_backend("experimental_8fsk_musical_25")
        card = pixel.example_card()
        symbols = pixel.minimal_avatar_symbols(card, message_type="cq", snr_db=None)
        header = len(pixel.FRAME_SYNC) + pixel.AVATAR_META_TONE_COUNT
        first = list(symbols)
        first[header + 300] = (first[header + 300] + 1) % 8
        second = list(first)
        second[header + 400:header + 800] = [7] * 400
        def waveform(values):
            return np.pad(pixel.synthesize(values, sample_rate=8000, profile=backend.profile), (400, 400))
        token = ADMITTED_IDENTITY.set((backend.key, envelope.content_id(backend.key, card, "cq", None).hex()))
        try:
            baseline = backend.decode_payload_copies([waveform(first)], 8000)
            combined = backend.decode_payload_copies([waveform(first), waveform(second)], 8000)
        finally:
            ADMITTED_IDENTITY.reset(token)
        self.assertEqual(combined["pixels"], baseline["pixels"])
        self.assertFalse(combined["card"]["exact"])


if __name__ == "__main__":
    unittest.main()

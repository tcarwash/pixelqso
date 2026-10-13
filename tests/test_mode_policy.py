import unittest

from card_backends import (BACKENDS, DEFAULT_MODE_KEY, LEGACY_MODE_KEY, MODE_REGISTRY, audio_backend_keys, available_modes,
                           data2g_max_frame_bytes, data2g_mode_key, get_backend, host_mode_backends, is_experimental_mode,
                           mode_supports_audio_placement, normal_default_mode, recommended_data2g_modes)
from data2g_transport import Data2GMode
import cardmodem


class ModePolicyTests(unittest.TestCase):
    def test_ack_radio_modes_are_excluded_even_with_card_capacity(self):
        modes = [Data2GMode(name, 500, 116, 64, 1.0, 60.0)
                 for name in ("ack-1f", "ack-4f", "n4-ack-8f",
                              "n10-ack-4f", "n4-ack-2f", "n10-qpsk-r1/2")]
        catalog = host_mode_backends(modes, {mode.name for mode in modes})
        self.assertEqual(set(catalog), {data2g_mode_key("n10-qpsk-r1/2")})

    def test_recommended_modes_are_short_usable_and_ordered(self):
        modes = [Data2GMode(name, bandwidth, 116, 64, 1.0, 60.0)
                 for name, bandwidth in (("n4-qpsk-r1/2", 200),
                     ("n10-qpsk-r1/2", 500), ("qpsk-r1/2", 1200),
                     ("16qam-r1/2", 1200), ("qpsk-r1/5", 1200))]
        catalog = host_mode_backends(modes, {mode.name for mode in modes})
        shortlist = recommended_data2g_modes(catalog)
        self.assertEqual([catalog[key].mode_name for key, _ in shortlist],
                         ["qpsk-r1/2", "n10-qpsk-r1/2", "16qam-r1/2"])
        catalog[data2g_mode_key("qpsk-r1/2")] = host_mode_backends([modes[2]], set())[data2g_mode_key("qpsk-r1/2")]
        self.assertNotIn(data2g_mode_key("qpsk-r1/2"),
                         dict(recommended_data2g_modes(catalog)))
        self.assertEqual(recommended_data2g_modes({}), [])

    def test_registry_covers_backend_modes_and_records_policy(self):
        self.assertTrue(set(MODE_REGISTRY) - {LEGACY_MODE_KEY} <= set(BACKENDS))
        self.assertEqual(MODE_REGISTRY["resilient_100"].execution_path, "pixelqso_audio")
        self.assertTrue(is_experimental_mode("resilient_100"))
        self.assertTrue(MODE_REGISTRY["experimental_qpsk_5s"].experimental)
        self.assertEqual(MODE_REGISTRY["experimental_qpsk_5s"].availability, "scipy")
        self.assertEqual(MODE_REGISTRY[LEGACY_MODE_KEY].label, "4-FSK · soft Viterbi · receive compatibility")
        self.assertTrue(mode_supports_audio_placement("resilient_100"))
        self.assertFalse(mode_supports_audio_placement("fast_avatar_fec_v4"))
        self.assertEqual(get_backend("resilient_100", 300).profile.tones_hz[0], 300)

    def test_static_mode_catalog_contains_only_experimental_local_modems(self):
        visible = {key for key, _backend in available_modes()}
        self.assertFalse(visible)
        self.assertFalse({"fast_avatar", "fast_avatar_fec", "experimental_qpsk_5s",
                          "experimental_qpsk_combined", LEGACY_MODE_KEY} & visible)
        self.assertTrue(all(is_experimental_mode(key) for key, _backend in
                            available_modes(include_experimental=True)))

    def test_experimental_modes_appear_only_when_enabled(self):
        normal = {key for key, _backend in available_modes()}
        experimental = {key for key, _backend in available_modes(include_experimental=True)}
        self.assertTrue({"fast_avatar", "fast_avatar_fec", "experimental_qpsk_5s",
                         "experimental_qpsk_combined"} & experimental)
        self.assertEqual(normal, set())

    def test_default_prefers_host_only_when_connected(self):
        choices = {"resilient_100", "resilient_50", "resilient_25"}
        self.assertEqual(normal_default_mode(choices), DEFAULT_MODE_KEY)
        self.assertEqual(normal_default_mode(choices, host_connected=True), DEFAULT_MODE_KEY)

    def test_host_airtime_accounts_for_fragment_and_codeword_capacity(self):
        template = BACKENDS["data2g_1200_robust"]
        backend = template.__class__(
            template.key, template.label,
            Data2GMode("qpsk-r1/2", 1200, 116, 64, 1.78, 74.36))
        estimate = backend.estimate_seconds(cardmodem.example_card(), 1)
        per_codeword = ((backend.mode.seconds_at_max - backend.mode.seconds_at_one) /
                        (backend.mode.max_codewords - 1))
        frame_seconds = backend.mode.seconds_at_one + 6 * per_codeword
        self.assertGreater(estimate, 2 * backend.mode.seconds_at_one)
        self.assertLessEqual(estimate, 2 * frame_seconds)
        self.assertNotIn("data2g_1200_robust", audio_backend_keys())
        self.assertIn("resilient_100", audio_backend_keys())

    def test_slow_host_frame_target_limits_burst_duration_and_airtime_matches_fragments(self):
        from backend_adapters import DATA2G_ADAPTER
        from card_transfer import HEADER
        mode = Data2GMode("n10-qpsk-r1/5", 500, 23, 64, 2.0, 130.0)
        backend = host_mode_backends([mode], {mode.name})[data2g_mode_key(mode.name)]
        card = cardmodem.example_card()
        frames = DATA2G_ADAPTER.frame_card(card, backend.max_frame_bytes, "card")
        self.assertLess(backend.max_frame_bytes, 256)
        self.assertTrue(all(len(frame) <= backend.max_frame_bytes for frame in frames))
        self.assertTrue(all((4 + (len(frame) + 2 + mode.bytes_per_codeword - 1) // mode.bytes_per_codeword)
                            <= mode.max_codewords for frame in frames))
        step = (mode.seconds_at_max - mode.seconds_at_one) / (mode.max_codewords - 1)
        expected = sum(mode.seconds_at_one + (3 + (len(frame) + 2 + mode.bytes_per_codeword - 1)
                            // mode.bytes_per_codeword) * step for frame in frames)
        self.assertAlmostEqual(backend.estimate_seconds(card, 2), 2 * expected)
        self.assertTrue(all(mode.seconds_at_one + (3 + (len(frame) + 2 + mode.bytes_per_codeword - 1)
                            // mode.bytes_per_codeword) * step <= 16 for frame in frames))
        self.assertGreater(backend.max_frame_bytes, HEADER.size)

    def test_narrow_mode_keeps_minimum_viable_fragment_when_16_seconds_is_impossible(self):
        from card_transfer import HEADER
        mode = Data2GMode("n4-qpsk-r1/2", 200, 58, 64, 4.14, 221.86)
        limit = data2g_max_frame_bytes(mode)
        self.assertGreater(limit, HEADER.size)
        self.assertLess(limit, 256)
        self.assertEqual(4 + (limit + 2 + 57) // 58,
                         4 + (HEADER.size + 1 + 2 + 57) // 58)

    def test_fast_host_preserves_256_byte_default_and_preference_is_capacity_bounded(self):
        mode = Data2GMode("qpsk-r1/2", 1200, 116, 64, 1.78, 74.36)
        self.assertEqual(data2g_max_frame_bytes(mode), 256)
        self.assertEqual(data2g_max_frame_bytes(mode, preferred=100), 100)
        self.assertIsNone(data2g_max_frame_bytes(mode, preferred=1))

    def test_host_catalog_exposes_every_advertised_mode_and_marks_unusable_ones(self):
        modes = [
            Data2GMode("n4-qpsk-r1/2", 200, 58, 64, 4.14, 221.86),
            Data2GMode("n10-qpsk-r1/2", 500, 60, 64, 1.2, 75.0),
            Data2GMode("n10-qpsk-r1/5", 500, 23, 64, 2.0, 130.0),
            Data2GMode("n10-16qam-r3/4", 500, 184, 64, 0.4, 25.0),
            # Too little capacity for a maximum-size PixelQSO fragment.
            Data2GMode("16qam-r1/2", 1200, 8, 10, 0.5, 8.0),
        ]
        catalog = host_mode_backends(modes, {mode.name for mode in modes})
        self.assertEqual(len(catalog), len(modes))
        self.assertEqual(catalog[data2g_mode_key("n10-qpsk-r1/2")].mode_name,
                         "n10-qpsk-r1/2")
        self.assertIn("500 Hz", catalog[data2g_mode_key("n10-qpsk-r1/2")].label)
        too_small = catalog[data2g_mode_key("16qam-r1/2")]
        self.assertFalse(too_small.usable)
        self.assertIn("capacity", too_small.disabled_reason)

    def test_host_refused_mode_remains_visible_with_reason(self):
        mode = Data2GMode("new-family-r1/2", 240, 60, 64, 1.0, 60.0)
        catalog = host_mode_backends([mode], set())
        backend = catalog[data2g_mode_key(mode.name)]
        self.assertFalse(backend.usable)
        self.assertIn("refused", backend.disabled_reason)


if __name__ == "__main__":
    unittest.main()

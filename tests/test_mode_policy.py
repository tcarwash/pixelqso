import unittest

from card_backends import (BACKENDS, DEFAULT_MODE_KEY, LEGACY_MODE_KEY, MODE_REGISTRY, audio_backend_keys, available_modes,
                           data2g_mode_key, get_backend, host_mode_backends, is_experimental_mode,
                           mode_supports_audio_placement, normal_default_mode)
from data2g_transport import Data2GMode
import cardmodem


class ModePolicyTests(unittest.TestCase):
    def test_registry_covers_backend_modes_and_records_policy(self):
        self.assertTrue(set(MODE_REGISTRY) - {LEGACY_MODE_KEY} <= set(BACKENDS))
        self.assertEqual(MODE_REGISTRY["resilient_100"].execution_path, "pixelqso_audio")
        self.assertTrue(is_experimental_mode("resilient_100"))
        self.assertTrue(MODE_REGISTRY["experimental_qpsk_5s"].experimental)
        self.assertEqual(MODE_REGISTRY["experimental_qpsk_5s"].availability, "scipy")
        self.assertEqual(MODE_REGISTRY[LEGACY_MODE_KEY].label, "Legacy packet · experimental")
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

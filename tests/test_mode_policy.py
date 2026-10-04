import unittest

from card_backends import (BACKENDS, DEFAULT_MODE_KEY, LEGACY_MODE_KEY, MODE_REGISTRY, audio_backend_keys, available_modes,
                           host_mode_backends, is_experimental_mode, normal_default_mode)
from data2g_transport import Data2GMode
import cardmodem


class ModePolicyTests(unittest.TestCase):
    def test_registry_covers_backend_modes_and_records_policy(self):
        self.assertEqual(set(MODE_REGISTRY) - {LEGACY_MODE_KEY}, set(BACKENDS))
        self.assertEqual(MODE_REGISTRY["data2g_1200_robust"].execution_path, "data2g_host")
        self.assertEqual(MODE_REGISTRY["resilient_100"].execution_path, "pixelqso_audio")
        self.assertTrue(MODE_REGISTRY["experimental_qpsk_5s"].experimental)
        self.assertEqual(MODE_REGISTRY["experimental_qpsk_5s"].availability, "scipy")
        self.assertEqual(MODE_REGISTRY[LEGACY_MODE_KEY].label, "Legacy packet · experimental")

    def test_normal_modes_include_data2g_and_resilient_at_or_below_100_baud(self):
        visible = {key for key, _backend in available_modes()}
        self.assertTrue({"resilient_100", "resilient_50", "resilient_25"} <= visible)
        self.assertFalse({"fast_avatar", "fast_avatar_fec", "experimental_qpsk_5s",
                          "experimental_qpsk_combined", LEGACY_MODE_KEY} & visible)
        self.assertFalse(is_experimental_mode("data2g_1200_robust"))

    def test_experimental_modes_appear_only_when_enabled(self):
        normal = {key for key, _backend in available_modes()}
        experimental = {key for key, _backend in available_modes(include_experimental=True)}
        self.assertTrue({"fast_avatar", "fast_avatar_fec", "experimental_qpsk_5s",
                         "experimental_qpsk_combined"} & experimental)
        self.assertTrue(normal <= experimental)

    def test_default_prefers_host_only_when_connected(self):
        choices = {"data2g_1200_robust", "resilient_100", "resilient_50", "resilient_25"}
        self.assertEqual(normal_default_mode(choices), DEFAULT_MODE_KEY)
        self.assertEqual(normal_default_mode(choices, host_connected=True), "data2g_1200_robust")

    def test_host_airtime_accounts_for_fragment_and_codeword_capacity(self):
        template = BACKENDS["data2g_1200_robust"]
        backend = template.__class__(
            template.key, template.label,
            Data2GMode("qpsk-r1/2", 1200, 116, 64, 1.78, 74.36))
        estimate = backend.estimate_seconds(cardmodem.example_card(), 1)
        self.assertGreater(estimate, 3 * 1.78)
        self.assertLess(estimate, 3 * 4.1)
        self.assertNotIn("data2g_1200_robust", audio_backend_keys())
        self.assertIn("resilient_100", audio_backend_keys())

    def test_host_presets_fall_back_to_modes_within_a_narrow_bandwidth_cap(self):
        modes = [
            Data2GMode("n4-qpsk-r1/2", 200, 58, 64, 4.14, 221.86),
            Data2GMode("n10-qpsk-r1/2", 500, 60, 64, 1.2, 75.0),
            Data2GMode("n10-qpsk-r1/5", 500, 23, 64, 2.0, 130.0),
            Data2GMode("n10-16qam-r3/4", 500, 184, 64, 0.4, 25.0),
            # Too little capacity for a maximum-size PixelQSO fragment.
            Data2GMode("16qam-r1/2", 1200, 8, 16, 0.5, 8.0),
        ]
        presets = host_mode_backends(modes)
        self.assertEqual(presets["data2g_1200_robust"].mode_name, "n10-qpsk-r1/2")
        self.assertEqual(presets["data2g_1200_fast"].mode_name, "n10-16qam-r3/4")
        self.assertIn("500 Hz", presets["data2g_1200_robust"].label)


if __name__ == "__main__":
    unittest.main()

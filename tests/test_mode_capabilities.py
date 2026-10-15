import unittest

from card_backends import MODE_REGISTRY


class ModeCapabilityTests(unittest.TestCase):
    def test_fixed_burst_modes_advertise_legacy_card_shape(self):
        spec = MODE_REGISTRY["experimental_8fsk_ldpc"]
        self.assertTrue(spec.supports_card(32, 32, 8))
        self.assertFalse(spec.supports_card(64, 64, 8))
        self.assertFalse(spec.supports_card(32, 32, 16))
        self.assertEqual(spec.capability_label, "32×32 · 8 colors")

    def test_resilient_avatar_modes_advertise_variable_palette(self):
        spec = MODE_REGISTRY["resilient_50"]
        self.assertTrue(spec.supports_card(64, 64, 16))
        self.assertTrue(spec.supports_card(16, 16, 32))
        self.assertEqual(spec.capability_label, "variable size · 8/16/32 colors")

import hashlib
import unittest

import numpy as np

from weak_signal_ldpc import fixed_code


class FixedLDPCCompatibilityTests(unittest.TestCase):
    def test_fixed_wire_bits_match_the_previous_construction(self):
        bits = np.random.default_rng(1234).integers(0, 2, (1, 3376), dtype=np.uint8)
        transmitted = fixed_code().encode(bits)
        self.assertEqual(transmitted.shape, (1, 14080))
        self.assertEqual(
            hashlib.sha256(transmitted.tobytes()).hexdigest(),
            "1772eb0c00a52334d3b8eb223bea863c506e5ba8f5a8cd6c45e3cec00760ad54",
        )

    def test_received_position_graph_has_fixed_dimensions(self):
        code = fixed_code()
        self.assertEqual(code.base.shape, (33, 43))
        self.assertEqual(code.sent.size, 14080)
        self.assertEqual(code.edges[0].size, code.edges[1].size)


if __name__ == "__main__":
    unittest.main()

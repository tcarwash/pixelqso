"""Fixed QC-LDPC encoder and graph for the experimental card modem.

The shift matrix is the attributed 3GPP BG2 table in licenses/weak-signal.
"""
from functools import cached_property
import numpy as np
from weak_signal_ldpc_data import DATA


class FixedCode:
    def __init__(self):
        data = DATA
        self.base = np.asarray(data["base"], dtype=np.int64)
        self.z, self.kb, self.k, self.n = data["z"], data["kb"], data["k"], data["n"]
        self.mb = self.base.shape[0]

    @property
    def n_cols(self):
        return self.base.shape[1] * self.z

    @cached_property
    def sent(self):
        cols = np.arange(2 * self.z, self.n_cols)
        cols = cols[(cols < self.k) | (cols >= self.kb * self.z)]
        return cols[:self.n]

    @cached_property
    def edges(self):
        r, c = np.nonzero(self.base >= 0)
        i = np.arange(self.z)
        rows = (r[:, None] * self.z + i).reshape(-1)
        cols = (c[:, None] * self.z + (i + self.base[r, c][:, None]) % self.z).reshape(-1)
        return rows, cols

    @staticmethod
    def _inverse(matrix):
        n = len(matrix)
        augmented = np.concatenate([matrix.astype(bool), np.eye(n, dtype=bool)], axis=1)
        for column in range(n):
            pivots = column + np.flatnonzero(augmented[column:, column])
            if not len(pivots):
                raise ValueError("LDPC core matrix is singular")
            pivot = pivots[0]
            augmented[[column, pivot]] = augmented[[pivot, column]]
            rows = np.flatnonzero(augmented[:, column])
            augmented[rows[rows != column]] ^= augmented[column]
        return augmented[:, n:].astype(np.uint8)

    @cached_property
    def _core_inverse(self):
        core = self.base[:4, self.kb:self.kb + 4]
        dense = np.zeros((4 * self.z, 4 * self.z), dtype=np.uint8)
        i = np.arange(self.z)
        for r, c in zip(*np.nonzero(core >= 0)):
            dense[r*self.z+i, c*self.z+(i+core[r,c]) % self.z] = 1
        return self._inverse(dense)

    def _multiply(self, blocks, values):
        out = np.zeros((values.shape[0], blocks.shape[0], self.z), dtype=np.uint8)
        i = np.arange(self.z)
        for r, c in zip(*np.nonzero(blocks >= 0)):
            out[:, r] ^= values[:, c, (i + blocks[r, c]) % self.z]
        return out

    def encode(self, bits):
        bits = np.atleast_2d(bits).astype(np.uint8)
        info = np.zeros((len(bits), self.kb * self.z), dtype=np.uint8)
        info[:, :self.k] = bits
        shaped = info.reshape(len(bits), self.kb, self.z)
        checks = self._multiply(self.base[:4, :self.kb], shaped).reshape(len(bits), -1)
        parity = (checks.astype(np.int32) @ self._core_inverse.T.astype(np.int32)) % 2
        known = np.concatenate([shaped, parity.astype(np.uint8).reshape(len(bits), 4, self.z)], axis=1)
        extension = self._multiply(self.base[4:, :self.kb + 4], known)
        full = np.concatenate([info, parity.astype(np.uint8), extension.reshape(len(bits), -1)], axis=1)
        return full[:, self.sent]

    def syndrome_ok(self, full):
        rows, cols = self.edges
        syndrome = np.zeros((len(full), self.mb * self.z), dtype=np.uint8)
        np.add.at(syndrome.T, rows, full[:, cols].T)
        return np.all(syndrome % 2 == 0, axis=1)


_CODE = None


def fixed_code():
    global _CODE
    if _CODE is None:
        _CODE = FixedCode()
    return _CODE

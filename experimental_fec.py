"""Small, deterministic Reed–Solomon helpers for experimental burst formats.

Inner RS uses the existing GF(64) polynomial; outer systematic Vandermonde
coding uses GF(256), primitive polynomial 0x11d. No optional GPU dependency.
"""
from functools import lru_cache
import numpy as np


class Field:
    def __init__(self, bits, polynomial):
        self.order = (1 << bits) - 1
        self.exp = [0] * (2 * self.order)
        self.log = [0] * (self.order + 1)
        value = 1
        for index in range(self.order):
            self.exp[index] = value
            self.log[value] = index
            value <<= 1
            if value > self.order:
                value ^= polynomial
        self.exp[self.order:] = self.exp[:self.order]

    def mul(self, a, b):
        return self.exp[self.log[int(a)] + self.log[int(b)]] if a and b else 0

    def div(self, a, b):
        if not b:
            raise ValueError("singular finite-field operation")
        return self.exp[(self.log[int(a)] - self.log[int(b)]) % self.order] if a else 0

    def power(self, a, n):
        return self.exp[(self.log[int(a)] * n) % self.order] if a else int(n == 0)

    def evaluate(self, coefficients, x):
        result = 0
        for value in coefficients:
            result = self.mul(result, x) ^ int(value)
        return result

    def inverse(self, matrix):
        n = len(matrix)
        rows = [list(map(int, row)) + [int(i == j) for j in range(n)]
                for i, row in enumerate(matrix)]
        for column in range(n):
            pivot = next((i for i in range(column, n) if rows[i][column]), None)
            if pivot is None:
                raise ValueError("singular finite-field matrix")
            rows[column], rows[pivot] = rows[pivot], rows[column]
            scale = rows[column][column]
            rows[column] = [self.div(value, scale) for value in rows[column]]
            for i in range(n):
                factor = rows[i][column] if i != column else 0
                if factor:
                    rows[i] = [a ^ self.mul(factor, b)
                               for a, b in zip(rows[i], rows[column])]
        return np.asarray([row[n:] for row in rows], dtype=np.uint8)

    def matmul(self, left, right):
        left, right = np.asarray(left), np.asarray(right)
        result = np.zeros((len(left), right.shape[1]), dtype=np.uint8)
        for i, row in enumerate(left):
            for k, coefficient in enumerate(row):
                if coefficient:
                    result[i] ^= np.asarray([self.mul(coefficient, value)
                                             for value in right[k]], dtype=np.uint8)
        return result


# Match cardmodem's GF(64) construction rather than defining another alphabet.
GF64 = Field(6, 0x43)
GF256 = Field(8, 0x11d)


@lru_cache(None)
def rs_generator(parity):
    generator = [1]
    for i in range(parity):
        expanded = [0] * (len(generator) + 1)
        for j, value in enumerate(generator):
            expanded[j] ^= value
            expanded[j + 1] ^= GF64.mul(value, GF64.exp[i])
        generator = expanded
    return tuple(generator)


def rs_encode(values, parity=10):
    values = list(map(int, values))
    if not values or len(values) + parity > 63 or any(not 0 <= v < 64 for v in values):
        raise ValueError("invalid shortened RS(63,53) data")
    generator = rs_generator(parity)
    work = values + [0] * parity
    for i in range(len(values)):
        coefficient = work[i]
        for j in range(1, parity + 1):
            work[i + j] ^= GF64.mul(coefficient, generator[j])
    return values + work[-parity:]


def rs_decode(values, parity=10):
    word = list(map(int, values))
    if not parity < len(word) <= 63 or any(not 0 <= v < 64 for v in word):
        raise ValueError("invalid RS codeword")
    syndromes = [GF64.evaluate(word, GF64.exp[i]) for i in range(parity)]
    if not any(syndromes):
        return word[:-parity], 0
    # Berlekamp–Massey, with locator coefficients in ascending order.
    locator, previous = [1] + [0] * parity, [1] + [0] * parity
    degree, distance, discrepancy_previous = 0, 1, 1
    for n in range(parity):
        discrepancy = syndromes[n]
        for i in range(1, degree + 1):
            discrepancy ^= GF64.mul(locator[i], syndromes[n - i])
        if not discrepancy:
            distance += 1
            continue
        saved = locator[:]
        scale = GF64.div(discrepancy, discrepancy_previous)
        for i in range(parity + 1 - distance):
            locator[i + distance] ^= GF64.mul(scale, previous[i])
        if 2 * degree <= n:
            degree, previous = n + 1 - degree, saved
            discrepancy_previous, distance = discrepancy, 1
        else:
            distance += 1
    if not 0 < degree <= parity // 2:
        raise ValueError("RS correction capacity exceeded")
    positions = [i for i in range(len(word))
                 if GF64.evaluate(locator[:degree + 1][::-1],
                                  GF64.exp[-(len(word) - 1 - i) % 63]) == 0]
    if len(positions) != degree:
        raise ValueError("RS locator has missing roots")
    locations = [GF64.exp[(len(word) - 1 - i) % 63] for i in positions]
    matrix = [[GF64.power(x, row) for x in locations] for row in range(degree)]
    magnitudes = GF64.matmul(GF64.inverse(matrix),
                             np.asarray(syndromes[:degree], dtype=np.uint8)[:, None])[:, 0]
    for position, magnitude in zip(positions, magnitudes):
        word[position] ^= int(magnitude)
    if any(GF64.evaluate(word, GF64.exp[i]) for i in range(parity)):
        raise ValueError("RS syndrome remains after correction")
    return word[:-parity], degree


OUTER_DATA, OUTER_PARITY, SHARD_BYTES = 12, 4, 36


@lru_cache(None)
def outer_matrix():
    vandermonde = np.asarray([[GF256.power(i + 1, j) for j in range(OUTER_DATA)]
                             for i in range(OUTER_DATA + OUTER_PARITY)], dtype=np.uint8)
    return GF256.matmul(vandermonde, GF256.inverse(vandermonde[:OUTER_DATA]))


def outer_encode(packet):
    if len(packet) > OUTER_DATA * SHARD_BYTES:
        raise ValueError("outer parity packet is too large")
    data = np.frombuffer(bytes(packet).ljust(OUTER_DATA * SHARD_BYTES, b"\0"),
                         dtype=np.uint8).reshape(OUTER_DATA, SHARD_BYTES)
    return [row.tobytes() for row in GF256.matmul(outer_matrix(), data)]


def outer_recover(shards):
    if len(shards) < OUTER_DATA:
        raise ValueError("not enough checked shards for outer recovery")
    indices = sorted(shards)[:OUTER_DATA]
    if any(not 0 <= i < OUTER_DATA + OUTER_PARITY or len(shards[i]) != SHARD_BYTES for i in indices):
        raise ValueError("invalid outer shard")
    received = np.asarray([list(shards[i]) for i in indices], dtype=np.uint8)
    recovered = GF256.matmul(GF256.inverse(outer_matrix()[indices]), received)
    return recovered.tobytes()

#!/usr/bin/env python3
"""Reference codec and sound-card WAV modem for Pixel QSO.

This development prototype searches frame start, carrier offset, and symbol
clock drift. It remains intended for local loopback and channel experiments.
"""
from __future__ import annotations
import argparse, base64, hashlib, json, struct, wave, zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

VERSION = 7
SAMPLE_RATE = 48_000
BAUD = 50
SAMPLES_PER_SYMBOL = SAMPLE_RATE // BAUD
TONES_HZ = (1100, 1200, 1300, 1400)
MAGIC = b"PQ"
# 16 alternating acquisition symbols followed by a fixed 8-symbol sync word.
PREAMBLE = (0, 3) * 8
SYNC = (2, 1, 3, 0, 3, 2, 0, 1)
FRAME_SYNC = PREAMBLE + SYNC
TYPE_ID, TYPE_PREVIEW, TYPE_LUMA, TYPE_COLOR4, TYPE_COLOR8 = 1, 2, 3, 4, 5
TYPE_STRUCTURE, TYPE_GRAY, TYPE_GRAY_PREVIEW = 6, 7, 8
TYPE_FINAL = 9
TYPE_END = 10
TYPE_COMPACT_PALETTE, TYPE_COMPACT_RASTER = 11, 12
TYPE_ACK = 13
TYPE_RAW_AVATAR = 14
CARD_PACKET_COUNT = 19
HEADER = struct.Struct(">2sBHB BB")  # magic, version/type, card_id, seq, length, reserved
HEADER_SIZE = HEADER.size
HEADER_WIRE_SIZE = HEADER_SIZE * 2  # hexadecimal ASCII representation
MAX_PAYLOAD = 255
HEADER_INTERLEAVE_DEPTH = 8
BODY_INTERLEAVE_DEPTH = 16
CONVOLUTIONAL_POLYS = (0o171, 0o133)
CONVOLUTIONAL_TAIL_BITS = 6
MINIMAL_AVATAR_WIDTH = MINIMAL_AVATAR_HEIGHT = 32
MINIMAL_AVATAR_PALETTE = ((0, 0, 0), (15, 15, 15), (0, 0, 15), (0, 15, 15),
                          (0, 15, 0), (15, 15, 0), (15, 0, 0), (15, 0, 15))
AVATAR_PALETTES = (
    MINIMAL_AVATAR_PALETTE,
    tuple((r, g, b) for r in (0, 5, 10, 15) for g in (0, 15) for b in (0, 15)),
    tuple((r, g, b) for r in (0, 5, 10, 15) for g in (0, 5, 10, 15) for b in (0, 15)),
)
AVATAR_PALETTE_BITS = (3, 4, 5)
AVATAR_RS_N, AVATAR_RS_K = 63, 61
AVATAR_META_LEGACY_STRUCT = struct.Struct(">2s6BH12s8s")
AVATAR_META_STRUCT = struct.Struct(">2s6BH12s8sBBb")
AVATAR_META_V4_STRUCT = struct.Struct(">2s6BH12s8sBBbI4s")
AVATAR_META_LEGACY_DATA_SYMBOLS = (AVATAR_META_LEGACY_STRUCT.size + 2) * 8 // 6 + (((AVATAR_META_LEGACY_STRUCT.size + 2) * 8) % 6 != 0)
AVATAR_META_LEGACY_TONE_COUNT = (AVATAR_META_LEGACY_DATA_SYMBOLS + 2) * 2
AVATAR_META_DATA_SYMBOLS = (AVATAR_META_STRUCT.size + 2) * 8 // 6 + (((AVATAR_META_STRUCT.size + 2) * 8) % 6 != 0)
AVATAR_META_SHORTENING = AVATAR_RS_K - AVATAR_META_DATA_SYMBOLS
AVATAR_META_TONE_COUNT = (AVATAR_META_DATA_SYMBOLS + 2) * 2
AVATAR_META_V4_DATA_SYMBOLS = (AVATAR_META_V4_STRUCT.size + 2) * 8 // 6 + (((AVATAR_META_V4_STRUCT.size + 2) * 8) % 6 != 0)
AVATAR_META_V4_TONE_COUNT = (AVATAR_META_V4_DATA_SYMBOLS + 2) * 2
AVATAR_BLOCK_ID_SYMBOLS = 2
AVATAR_V4_PIXELS_PER_BLOCK = ((AVATAR_RS_K - AVATAR_BLOCK_ID_SYMBOLS) * 6) // 5
AVATAR_MESSAGE_CODES = {"card": 0, "cq": 1, "exchange": 2, "73": 3}
AVATAR_MESSAGE_NAMES = {value: key for key, value in AVATAR_MESSAGE_CODES.items()}
AVATAR_BURST_CODES = {"fast_avatar": 1, "fast_avatar_fec": 2}
FAST_AVATAR_RESILIENT_CYCLE_SYMBOLS = (len(FRAME_SYNC) + AVATAR_META_V4_TONE_COUNT +
                                       8 * (AVATAR_RS_N * 2 + 6) +
                                       (512 - 8 * (AVATAR_RS_K - AVATAR_BLOCK_ID_SYMBOLS) + 4) * 2 + 6)
FAST_AVATAR_RESILIENT_CYCLE_SYMBOLS_V3 = (len(FRAME_SYNC) + AVATAR_META_TONE_COUNT +
                                          8 * (AVATAR_RS_N * 2 + 6) +
                                          (512 - 8 * AVATAR_RS_K + 2) * 2 + 6)


@dataclass(frozen=True)
class ModemProfile:
    key: str
    label: str
    baud: int
    tones_hz: tuple[int, ...]
    nominal_bandwidth_hz: int


PROFILES = (
    ModemProfile("narrow", "Narrow · 25 baud · ≈200 Hz", 25, (1275, 1325, 1375, 1425), 200),
    ModemProfile("standard", "Standard · 50 baud · ≈400 Hz", 50, TONES_HZ, 400),
    ModemProfile("fast", "Fast · 100 baud · ≈800 Hz", 100, (1050, 1250, 1450, 1650), 800),
    ModemProfile("wide", "Wide · 200 baud · ≈1.6 kHz", 200, (900, 1300, 1700, 2100), 1600),
    ModemProfile("turbo", "Turbo · 400 baud · ≈1.8 kHz", 400, (900, 1300, 1700, 2100), 1800),
)
MINIMAL_AVATAR_PROFILE = ModemProfile("minimal-avatar", "Minimal avatar · 8-FSK · ≈1.8 kHz", 400,
                                       tuple(900 + 200 * i for i in range(8)), 1800)
DEFAULT_PROFILE = "standard"


def resolve_profile(profile: str | ModemProfile | None = None) -> ModemProfile:
    if profile is None:
        return next(item for item in PROFILES if item.key == DEFAULT_PROFILE)
    if isinstance(profile, ModemProfile):
        return profile
    for item in PROFILES:
        if item.key == profile:
            return item
    raise ValueError(f"unknown modem profile {profile!r}; choose " + ", ".join(item.key for item in PROFILES))


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def hamming74_encode_nibble(n: int) -> int:
    d1, d2, d3, d4 = (n >> 3) & 1, (n >> 2) & 1, (n >> 1) & 1, n & 1
    p1, p2, p4 = d1 ^ d2 ^ d4, d1 ^ d3 ^ d4, d2 ^ d3 ^ d4
    return (p1 << 6) | (p2 << 5) | (d1 << 4) | (p4 << 3) | (d2 << 2) | (d3 << 1) | d4


def hamming74_decode_word(word: int) -> tuple[int, bool]:
    b = [(word >> (6 - i)) & 1 for i in range(7)]
    s1, s2, s4 = b[0]^b[2]^b[4]^b[6], b[1]^b[2]^b[5]^b[6], b[3]^b[4]^b[5]^b[6]
    syndrome = (s4 << 2) | (s2 << 1) | s1
    corrected = bool(syndrome)
    if syndrome:
        b[syndrome - 1] ^= 1
    nibble = (b[2] << 3) | (b[4] << 2) | (b[5] << 1) | b[6]
    return nibble, corrected


def _parity(value: int) -> int:
    return value.bit_count() & 1


def coded_symbols_for_bytes(nbytes: int) -> int:
    return nbytes * 8 + CONVOLUTIONAL_TAIL_BITS


def coded_frame_symbols(wire_bytes: int) -> int:
    return coded_symbols_for_bytes(HEADER_WIRE_SIZE) + coded_symbols_for_bytes(wire_bytes-HEADER_WIRE_SIZE)


def interleave_symbols(values: Iterable, depth: int):
    values = list(values)
    rows = [values[start:start+depth] for start in range(0, len(values), depth)]
    return [row[column] for column in range(depth) for row in rows if column < len(row)]


def encode_bytes(data: bytes, depth: int = BODY_INTERLEAVE_DEPTH) -> tuple[int, ...]:
    """Rate-1/2 K=7 convolutional code; each FSK symbol carries two coded bits."""
    bits = [(byte >> shift) & 1 for byte in data for shift in range(7, -1, -1)]
    bits.extend([0] * CONVOLUTIONAL_TAIL_BITS)
    state = 0
    tones = []
    for bit in bits:
        register = ((state << 1) | bit) & 0x7F
        tones.append((_parity(register & CONVOLUTIONAL_POLYS[0]) << 1) |
                     _parity(register & CONVOLUTIONAL_POLYS[1]))
        state = register & 0x3F
    return tuple(interleave_symbols(tones, depth))


def deinterleave(values: Iterable, nbytes: int, depth: int = BODY_INTERLEAVE_DEPTH) -> list:
    """Undo the interleaver for tone IDs or tone-metric vectors."""
    source = list(values)
    count = coded_symbols_for_bytes(nbytes)
    if len(source) < count:
        raise ValueError("truncated coded packet")
    rows = [None] * ((count + depth - 1)//depth)
    cursor = 0
    for column in range(depth):
        for row_index in range(len(rows)):
            row_length = min(depth, count-row_index*depth)
            if column < row_length:
                if rows[row_index] is None:
                    rows[row_index] = [None] * row_length
                rows[row_index][column] = source[cursor]
                cursor += 1
    return [value for row in rows for value in row]


def decode_bytes(symbols: Iterable[int], nbytes: int, depth: int = BODY_INTERLEAVE_DEPTH) -> tuple[bytes, int]:
    syms = tuple(symbols)
    need = coded_symbols_for_bytes(nbytes)
    if len(syms) < need:
        raise ValueError("truncated coded packet")
    metrics = np.zeros((need, 4), dtype=np.float64)
    metrics[np.arange(need), np.asarray(syms[:need], dtype=np.int64)] = 1.0
    return decode_bytes_soft(metrics, nbytes, depth)


def decode_bytes_soft(tone_metrics: np.ndarray, nbytes: int,
                      depth: int = BODY_INTERLEAVE_DEPTH) -> tuple[bytes, int]:
    """Soft-decision Viterbi decoding for the K=7 convolutional code."""
    metrics = np.asarray(tone_metrics, dtype=np.float64)
    count = coded_symbols_for_bytes(nbytes)
    if metrics.ndim != 2 or metrics.shape[0] < count or metrics.shape[1] != 4:
        raise ValueError("truncated soft-coded packet")
    ordered = np.asarray(deinterleave(metrics[:count], nbytes, depth), dtype=np.float64)
    logp = np.log(np.maximum(ordered, 1e-15))
    scores = np.full(64, -np.inf); scores[0] = 0.0
    predecessors = np.full((count, 64), -1, dtype=np.int16)
    earlier = np.arange(32)
    states = earlier[None, :] + np.array([0, 32])[:, None]
    tones = np.empty((2, 2, 32), dtype=np.intp)
    for bit in (0, 1):
        registers = ((states << 1) | bit) & 0x7f
        tones[bit] = np.vectorize(lambda register: (_parity(int(register) & CONVOLUTIONAL_POLYS[0]) << 1) |
                                                   _parity(int(register) & CONVOLUTIONAL_POLYS[1]))(registers)
    for t in range(count):
        next_scores = np.full(64, -np.inf)
        for bit in (0, 1):
            candidates = scores.reshape(2, 32) + logp[t, tones[bit]]
            winners = np.argmax(candidates, axis=0)
            next_scores[bit::2] = candidates[winners, earlier]
            predecessors[t, bit::2] = states[winners, earlier]
        scores = next_scores
    state = 0  # six zero tail bits terminate the trellis
    decoded = [0] * count
    for t in range(count - 1, -1, -1):
        decoded[t] = state & 1
        state = int(predecessors[t, state])
    data_bits = decoded[:nbytes * 8]
    result = bytes(sum(data_bits[i+j] << (7-j) for j in range(8)) for i in range(0, len(data_bits), 8))
    hard = np.argmax(ordered, axis=1)
    decoded_tones = deinterleave(encode_bytes(result, depth), nbytes, depth)
    corrected = int(np.count_nonzero(np.asarray(decoded_tones) != hard))
    return result, corrected


def decode_frame(symbols: Iterable[int], nbytes: int) -> tuple[bytes, int]:
    """Decode an ASCII-armoured header/body with independent interleavers."""
    values = tuple(symbols)
    header_symbols = coded_symbols_for_bytes(HEADER_WIRE_SIZE)
    total_symbols = coded_frame_symbols(nbytes)
    if nbytes < HEADER_WIRE_SIZE or len(values) < total_symbols:
        raise ValueError("truncated coded frame")
    header_ascii, head_fixed = decode_bytes(values[:header_symbols], HEADER_WIRE_SIZE, HEADER_INTERLEAVE_DEPTH)
    header = bytes.fromhex(header_ascii.decode("ascii"))
    _, _, _, _, payload_length, _ = HEADER.unpack(header)
    body_wire_size = b85_size(payload_length + 2)
    if HEADER_WIRE_SIZE + body_wire_size != nbytes:
        raise ValueError("coded frame length disagrees with header")
    body_ascii, body_fixed = decode_bytes(values[header_symbols:total_symbols], body_wire_size, BODY_INTERLEAVE_DEPTH)
    body = base64.b85decode(body_ascii)
    return header + body, head_fixed + body_fixed


def decode_frame_soft(tone_metrics: np.ndarray, nbytes: int) -> tuple[bytes, int]:
    """Soft-decode an ASCII-armoured frame with separate interleavers."""
    metrics = np.asarray(tone_metrics, dtype=np.float64)
    header_symbols = coded_symbols_for_bytes(HEADER_WIRE_SIZE)
    total_symbols = coded_frame_symbols(nbytes)
    if nbytes < HEADER_WIRE_SIZE or metrics.shape[0] < total_symbols:
        raise ValueError("truncated soft-coded frame")
    header_ascii, head_fixed = decode_bytes_soft(metrics[:header_symbols], HEADER_WIRE_SIZE, HEADER_INTERLEAVE_DEPTH)
    header = bytes.fromhex(header_ascii.decode("ascii"))
    _, _, _, _, payload_length, _ = HEADER.unpack(header)
    body_wire_size = b85_size(payload_length + 2)
    if HEADER_WIRE_SIZE + body_wire_size != nbytes:
        raise ValueError("soft-coded frame length disagrees with header")
    body_ascii, body_fixed = decode_bytes_soft(metrics[header_symbols:total_symbols], body_wire_size, BODY_INTERLEAVE_DEPTH)
    body = base64.b85decode(body_ascii)
    return header + body, head_fixed + body_fixed


def b85_size(byte_count: int) -> int:
    """Exact RFC 1924 base85 text length for an arbitrary byte count."""
    full, remainder = divmod(byte_count, 4)
    return full * 5 + (0, 2, 3, 4)[remainder]


def unarmour_header(header_ascii: bytes) -> tuple[bytes, int, int, int, int, int]:
    header = bytes.fromhex(header_ascii.decode("ascii"))
    return HEADER.unpack(header)


@dataclass
class Card:
    callsign: str
    grid: str
    # RGB444 swatches in binary-refinement order. The top index bit separates
    # dark/light groups, the middle bit separates color families, and the low
    # bit selects the final exact swatch in each pair.
    palette: list[tuple[int, int, int]]
    pixels: list[int]
    card_id: int = 0xC0DE
    width: int = 32
    height: int = 32

    def validate(self) -> None:
        if not 1 <= int(self.width) <= 255 or not 1 <= int(self.height) <= 255:
            raise ValueError("canvas dimensions must be in 1..255")
        if len(self.pixels) != int(self.width) * int(self.height):
            raise ValueError(f"pixels must contain {self.width * self.height} palette indices")
        if len(self.palette) not in (8, 16, 32):
            raise ValueError("palette must contain 8, 16, or 32 RGB444 colors")
        if any(not 0 <= int(p) < len(self.palette) for p in self.pixels):
            raise ValueError(f"pixel index outside 0..{len(self.palette)-1}")
        if any(len(c) != 3 or any(not 0 <= int(v) < 16 for v in c) for c in self.palette):
            raise ValueError("palette entries must be RGB444 triples")
        for label, value, maximum in (("callsign", self.callsign, 12), ("grid", self.grid, 8)):
            if len(value.upper().encode("ascii")) > maximum:
                raise ValueError(f"{label} must be at most {maximum} ASCII bytes")


def _pack_indices(values: Iterable[int], bits_per_index: int) -> bytes:
    bits = []
    for v in values:
        if not 0 <= int(v) < (1 << bits_per_index):
            raise ValueError(f"palette index outside 0..{(1 << bits_per_index)-1}")
        bits.extend((int(v) >> shift) & 1 for shift in range(bits_per_index-1, -1, -1))
    while len(bits) % 8:
        bits.append(0)
    out = bytearray()
    for i in range(0, len(bits), 8):
        b = 0
        for bit in bits[i:i + 8]:
            b = (b << 1) | bit
        out.append(b)
    return bytes(out)


def pack_indices(values: Iterable[int]) -> bytes:
    """Pack the legacy eight-color, three-bit palette format."""
    return _pack_indices(values, 3)


def _unpack_indices(data: bytes, count: int, bits_per_index: int) -> list[int]:
    bits = [(byte >> shift) & 1 for byte in data for shift in range(7, -1, -1)]
    required = count * bits_per_index
    if len(bits) < required:
        raise ValueError("packed palette indices are truncated")
    return [sum(bits[i * bits_per_index + j] << (bits_per_index-1-j)
                for j in range(bits_per_index)) for i in range(count)]


def unpack_indices(data: bytes, count: int) -> list[int]:
    """Unpack the legacy eight-color, three-bit palette format."""
    return _unpack_indices(data, count, 3)


def _indices_to_tones(values: Iterable[int], bits_per_index: int) -> tuple[int, ...]:
    bits = [bit for value in values
            for bit in ((int(value) >> shift) & 1 for shift in range(bits_per_index-1, -1, -1))]
    bits.extend([0] * ((-len(bits)) % 3))
    return tuple(sum(bits[i+j] << (2-j) for j in range(3)) for i in range(0, len(bits), 3))


def _tones_to_indices(tones: Iterable[int], count: int, bits_per_index: int) -> list[int]:
    bits = [((int(tone) >> shift) & 1) for tone in tones for shift in (2, 1, 0)]
    required = count * bits_per_index
    if len(bits) < required:
        raise ValueError("avatar pixel tones are truncated")
    return [sum(bits[i*bits_per_index+j] << (bits_per_index-1-j)
                for j in range(bits_per_index)) for i in range(count)]


def _avatar_palette_id(card: Card) -> int:
    return {8: 0, 16: 1, 32: 2}[len(card.palette)]


def _avatar_palette_map(card: Card) -> list[int]:
    target = AVATAR_PALETTES[_avatar_palette_id(card)]
    return [min(range(len(target)), key=lambda i: sum((int(channel)-target[i][k])**2
            for k, channel in enumerate(rgb))) for rgb in card.palette]


def minimal_avatar_payload(card: Card) -> bytes:
    """Pack the image indices using the closest shared 8/16/32-color palette."""
    card.validate()
    bits = AVATAR_PALETTE_BITS[_avatar_palette_id(card)]
    index_map = _avatar_palette_map(card)
    return _pack_indices((index_map[int(index)] for index in card.pixels), bits)


def minimal_avatar_symbols(card: Card, *, include_sync: bool = True,
                           message_type: str = "card", snr_db: int | None = None) -> tuple[int, ...]:
    """Identity-protected burst followed by packed raw pixels on 8-FSK tones."""
    bits = AVATAR_PALETTE_BITS[_avatar_palette_id(card)]
    palette_map = _avatar_palette_map(card)
    symbols = _indices_to_tones((palette_map[int(index)] for index in card.pixels), bits)
    header = tuple(_encode_avatar_metadata(card, message_type=message_type, snr_db=snr_db))
    return (FRAME_SYNC + header + symbols) if include_sync else (header + symbols)


def _gf64_tables():
    """GF(64), using primitive polynomial x^6 + x + 1."""
    exp = [0] * 126
    log = [0] * 64
    value = 1
    for i in range(63):
        exp[i] = value
        log[value] = i
        value <<= 1
        if value & 0x40:
            value ^= 0x43
    exp[63:] = exp[:63]
    if value != 1 or len(set(exp[:63])) != 63:
        raise RuntimeError("GF(64) polynomial is not primitive")
    return exp, log


_GF64_EXP, _GF64_LOG = _gf64_tables()


def _gf64_mul(a: int, b: int) -> int:
    return 0 if not a or not b else _GF64_EXP[_GF64_LOG[a] + _GF64_LOG[b]]


def _gf64_div(a: int, b: int) -> int:
    if not b:
        raise ZeroDivisionError("GF(64) division by zero")
    return 0 if not a else _GF64_EXP[(_GF64_LOG[a] - _GF64_LOG[b]) % 63]


def _rs64_encode(data: list[int]) -> list[int]:
    """Systematic RS(63,61) over GF(64), correcting one symbol error."""
    if len(data) != AVATAR_RS_K or any(not 0 <= x < 64 for x in data):
        raise ValueError("RS(63,61) needs exactly 61 GF(64) symbols")
    # Generator (x + 1)(x + alpha), with roots alpha^0 and alpha^1.
    generator = (1, 1 ^ 2, 2)
    work = data[:] + [0, 0]
    for i in range(AVATAR_RS_K):
        coefficient = work[i]
        for j in range(1, 3):
            work[i + j] ^= _gf64_mul(coefficient, generator[j])
    return data[:] + work[-2:]


def _rs64_decode(codeword: list[int], *, shortened: int = 0) -> tuple[list[int], bool]:
    """Correct one symbol error; shortened leading zero symbols are implicit."""
    if len(codeword) != AVATAR_RS_N - shortened:
        raise ValueError("invalid RS(63,61) codeword length")
    full = [0] * shortened + codeword[:]
    if any(not 0 <= x < 64 for x in full):
        raise ValueError("invalid GF(64) symbol")
    s0 = 0
    for value in full:
        s0 ^= value
    s1 = 0
    for value in full:
        s1 = _gf64_mul(s1, 2) ^ value
    corrected = False
    if s0 or s1:
        if not s0:
            raise ValueError("avatar block has an uncorrectable error")
        ratio = _gf64_div(s1, s0)
        exponent = _GF64_LOG[ratio] if ratio else -1
        position = AVATAR_RS_N - 1 - exponent
        if position < shortened or position >= AVATAR_RS_N:
            raise ValueError("avatar block error is outside its correctable range")
        full[position] ^= s0
        check0 = 0
        check1 = 0
        for value in full:
            check0 ^= value
            check1 = _gf64_mul(check1, 2) ^ value
        if check0 or check1:
            raise ValueError("avatar block has an uncorrectable error")
        corrected = True
    if any(full[:shortened]):
        raise ValueError("invalid shortened avatar block")
    return full[shortened:AVATAR_RS_K], corrected


def _pair_pixels(pixels: list[int]) -> list[int]:
    return [(pixels[i] << 3) | pixels[i + 1] for i in range(0, len(pixels), 2)]


def _pixels_to_gf64(pixels: Iterable[int], bits_per_pixel: int) -> list[int]:
    bits = [((int(pixel) >> shift) & 1) for pixel in pixels
            for shift in range(bits_per_pixel-1, -1, -1)]
    bits.extend([0] * ((-len(bits)) % 6))
    return [sum(bits[i+j] << (5-j) for j in range(6)) for i in range(0, len(bits), 6)]


def _gf64_to_pixels(values: Iterable[int], count: int, bits_per_pixel: int) -> list[int]:
    bits = [((int(value) >> shift) & 1) for value in values for shift in range(5, -1, -1)]
    required = count * bits_per_pixel
    if len(bits) < required:
        raise ValueError("resilient avatar block data is truncated")
    return [sum(bits[i*bits_per_pixel+j] << (bits_per_pixel-1-j)
                for j in range(bits_per_pixel)) for i in range(count)]


def _gf64_to_tones(values: Iterable[int]) -> list[int]:
    return [tone for value in values for tone in ((value >> 3) & 7, value & 7)]


def _tones_to_gf64(tones: list[int]) -> list[int]:
    if len(tones) % 2 or any(not 0 <= tone < 8 for tone in tones):
        raise ValueError("invalid fast-resilient avatar tone stream")
    return [(tones[i] << 3) | tones[i + 1] for i in range(0, len(tones), 2)]


def _bytes_to_gf64(data: bytes) -> list[int]:
    bits = [(byte >> shift) & 1 for byte in data for shift in range(7, -1, -1)]
    bits.extend([0] * ((-len(bits)) % 6))
    return [sum(bits[i + j] << (5 - j) for j in range(6)) for i in range(0, len(bits), 6)]


def _gf64_to_bytes(values: Iterable[int], byte_count: int) -> bytes:
    bits = [(int(value) >> shift) & 1 for value in values for shift in range(5, -1, -1)]
    if len(bits) < byte_count * 8:
        raise ValueError("metadata block is too short")
    out = bytearray()
    for start in range(0, byte_count * 8, 8):
        value = 0
        for bit in bits[start:start + 8]:
            value = (value << 1) | bit
        out.append(value)
    return bytes(out)


def _avatar_metadata(card: Card, *, resilient: bool = False,
                     message_type: str = "card", snr_db: int | None = None,
                     wire_version: int = 4) -> dict:
    card.validate()
    try:
        call = card.callsign.upper().encode("ascii")
        grid = card.grid.upper().encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("avatar identity must be ASCII") from exc
    if len(call) > 12 or len(grid) > 8:
        raise ValueError("callsign or grid is too long for the avatar burst")
    if message_type not in AVATAR_MESSAGE_CODES:
        raise ValueError(f"unsupported card message type {message_type!r}")
    if snr_db is not None and not -127 <= int(snr_db) <= 127:
        raise ValueError("SNR report must be between -127 and 127 dB")
    palette_id = _avatar_palette_id(card)
    if wire_version not in (3, 4):
        raise ValueError("unsupported resilient avatar wire version")
    common = (b"PQ", wire_version if resilient else 3, card.width, card.height,
              palette_id, len(call), len(grid), int(card.card_id) & 0xFFFF,
              call.ljust(12, b"\0"), grid.ljust(8, b"\0"),
              AVATAR_BURST_CODES["fast_avatar_fec" if resilient else "fast_avatar"],
              AVATAR_MESSAGE_CODES[message_type], -128 if snr_db is None else int(snr_db))
    if resilient and wire_version == 4:
        packed = minimal_avatar_payload(card)
        body = AVATAR_META_V4_STRUCT.pack(
            *common, zlib.crc32(packed), hashlib.blake2s(packed, digest_size=4).digest())
    elif resilient:
        body = AVATAR_META_STRUCT.pack(*common)
    else:
        body = AVATAR_META_STRUCT.pack(*common)
    return {"body": body, "wire": body + crc16(body).to_bytes(2, "big")}


def _encode_avatar_metadata(card: Card, *, resilient: bool = False,
                            message_type: str = "card", snr_db: int | None = None,
                            wire_version: int = 4) -> list[int]:
    wire = _avatar_metadata(card, resilient=resilient, message_type=message_type,
                            snr_db=snr_db, wire_version=wire_version)["wire"]
    values = _bytes_to_gf64(wire)
    expected_count = (AVATAR_META_V4_DATA_SYMBOLS
                      if resilient and wire_version == 4 else AVATAR_META_DATA_SYMBOLS)
    if len(values) != expected_count:
        raise RuntimeError("avatar metadata symbol count changed")
    shortened = AVATAR_RS_K - len(values)
    coded = _rs64_encode([0] * shortened + values)[shortened:]
    return _gf64_to_tones(coded)


def _decode_avatar_metadata(tones: list[int]) -> dict:
    coded = _tones_to_gf64(tones)
    if len(tones) == AVATAR_META_V4_TONE_COUNT:
        metadata_struct = AVATAR_META_V4_STRUCT
        shortening = AVATAR_RS_K - AVATAR_META_V4_DATA_SYMBOLS
    elif len(tones) == AVATAR_META_TONE_COUNT:
        metadata_struct = AVATAR_META_STRUCT
        shortening = AVATAR_META_SHORTENING
    elif len(tones) == AVATAR_META_LEGACY_TONE_COUNT:
        metadata_struct = AVATAR_META_LEGACY_STRUCT
        shortening = AVATAR_RS_K - AVATAR_META_LEGACY_DATA_SYMBOLS
    else:
        raise ValueError("unexpected avatar metadata length")
    data, corrected = _rs64_decode(coded, shortened=shortening)
    wire = _gf64_to_bytes(data, metadata_struct.size + 2)
    body, check = wire[:-2], int.from_bytes(wire[-2:], "big")
    if crc16(body) != check:
        raise ValueError("avatar identity checksum failed")
    fields = metadata_struct.unpack(body)
    magic, version, width, height, palette_id, call_len, grid_len, card_id, call, grid = fields[:10]
    image_crc32 = image_tag = None
    if metadata_struct is AVATAR_META_V4_STRUCT:
        burst_code, message_code, snr_db, image_crc32, image_tag = fields[10:]
        if version != 4 or burst_code != AVATAR_BURST_CODES["fast_avatar_fec"] or message_code not in AVATAR_MESSAGE_NAMES:
            raise ValueError("unsupported version 4 card burst metadata")
        avatar_mode = "fast_avatar_fec"
        message_type = AVATAR_MESSAGE_NAMES[message_code]
        snr_db = None if snr_db == -128 else snr_db
    elif metadata_struct is AVATAR_META_STRUCT:
        burst_code, message_code, snr_db = fields[10:]
        if version != 3 or burst_code not in (1, 2) or message_code not in AVATAR_MESSAGE_NAMES:
            raise ValueError("unsupported card burst metadata")
        avatar_mode = "fast_avatar_fec" if burst_code == 2 else "fast_avatar"
        message_type = AVATAR_MESSAGE_NAMES[message_code]
        snr_db = None if snr_db == -128 else snr_db
    else:
        if version not in (1, 2):
            raise ValueError("unsupported legacy burst metadata")
        avatar_mode = "fast_avatar_fec" if version == 2 else "fast_avatar"
        message_type, snr_db = "card", None
    if magic != b"PQ" or not width or not height or palette_id >= len(AVATAR_PALETTES):
        raise ValueError("unsupported avatar burst metadata")
    if call_len > 12 or grid_len > 8:
        raise ValueError("invalid avatar identity length")
    result = {"callsign": call[:call_len].decode("ascii"), "grid": grid[:grid_len].decode("ascii"),
            "card_id": card_id, "width": width, "height": height, "palette_id": palette_id,
            "palette": [list(color) for color in AVATAR_PALETTES[palette_id]],
            "bits_per_pixel": AVATAR_PALETTE_BITS[palette_id],
            "metadata_corrected": corrected,
            "avatar_mode": avatar_mode, "transmission_type": avatar_mode,
            "message_type": message_type, "snr_db": snr_db, "header_version": version}
    if image_crc32 is not None:
        result.update(image_crc32=image_crc32, image_tag=image_tag.hex(),
                      image_id=f"{image_crc32:08x}{image_tag.hex()}")
    return result


def _decode_avatar_metadata_after(tones: list[int] | tuple[int, ...], start: int) -> tuple[dict, int]:
    """Decode current or older protected headers and return the payload offset."""
    for count in (AVATAR_META_V4_TONE_COUNT, AVATAR_META_TONE_COUNT,
                  AVATAR_META_LEGACY_TONE_COUNT):
        end = start + count
        if end > len(tones):
            continue
        try:
            return _decode_avatar_metadata(list(tones[start:end])), end
        except (ValueError, UnicodeDecodeError, ZeroDivisionError):
            continue
    raise ValueError("no valid avatar identity header received yet")


def _crc16_tones(data: bytes) -> list[int]:
    bits = [(byte >> shift) & 1 for byte in data for shift in range(7, -1, -1)]
    bits.extend([0] * ((-len(bits)) % 3))
    return [sum(bits[i + j] << (2 - j) for j in range(3)) for i in range(0, len(bits), 3)]


def _tones_crc16(tones: list[int]) -> bytes:
    bits = [(tone >> shift) & 1 for tone in tones for shift in (2, 1, 0)]
    if len(bits) < 16:
        raise ValueError("missing avatar checksum")
    out = bytearray()
    for start in range(0, 16, 8):
        byte = 0
        for bit in bits[start:start + 8]:
            byte = (byte << 1) | bit
        out.append(byte)
    return bytes(out)


def _avatar_data_bytes(values: list[int]) -> bytes:
    bits = [(value >> shift) & 1 for value in values for shift in range(5, -1, -1)]
    bits.extend([0] * ((-len(bits)) % 8))
    out = bytearray()
    for start in range(0, len(bits), 8):
        value = 0
        for bit in bits[start:start + 8]:
            value = (value << 1) | bit
        out.append(value)
    return bytes(out)


def _avatar_crc_tones(data: bytes) -> list[int]:
    value = crc16(data)
    return _crc16_tones(value.to_bytes(2, "big"))


def _avatar_block_symbols(values: list[int]) -> list[int]:
    shortened = AVATAR_RS_K - len(values)
    coded = _rs64_encode([0] * shortened + values)[shortened:]
    return _gf64_to_tones(coded) + _avatar_crc_tones(_avatar_data_bytes(values))


def minimal_avatar_resilient_cycle_symbols(card: Card, *, include_sync: bool = True,
                                          message_type: str = "card",
                                          snr_db: int | None = None,
                                          wire_version: int = 4) -> tuple[int, ...]:
    """One cycle carries identity and independently checked raster blocks."""
    card.validate()
    bpp = AVATAR_PALETTE_BITS[_avatar_palette_id(card)]
    mapping = _avatar_palette_map(card)
    pixels = [mapping[int(index)] for index in card.pixels]
    if wire_version not in (3, 4):
        raise ValueError("resilient avatar wire version must be 3 or 4")
    pixels_per_block = (((AVATAR_RS_K - AVATAR_BLOCK_ID_SYMBOLS) * 6) // bpp
                        if wire_version == 4 else (AVATAR_RS_K * 6) // bpp)
    block_count = (len(pixels) + pixels_per_block - 1) // pixels_per_block
    body: list[int] = _encode_avatar_metadata(card, resilient=True,
                                              message_type=message_type, snr_db=snr_db,
                                              wire_version=wire_version)
    for block_index, start in enumerate(range(0, len(pixels), pixels_per_block)):
        block_pixels = pixels[start:start + pixels_per_block]
        block_values = (_pixels_to_gf64(block_pixels, bpp) if wire_version == 3 else
                        [block_index, block_count] + _pixels_to_gf64(block_pixels, bpp))
        body.extend(_avatar_block_symbols(block_values))
    tones = tuple(body)
    return (FRAME_SYNC + tones) if include_sync else tones


def minimal_avatar_resilient_symbols(card: Card, *, repeats: int = 1,
                                     message_type: str = "card",
                                     snr_db: int | None = None) -> tuple[int, ...]:
    if repeats < 1:
        raise ValueError("at least one avatar cycle is required")
    cycle = minimal_avatar_resilient_cycle_symbols(card, message_type=message_type, snr_db=snr_db)
    return cycle * repeats


def minimal_avatar_resilient_duration(repeats: int = 1, card: Card | None = None) -> float:
    symbols = (FAST_AVATAR_RESILIENT_CYCLE_SYMBOLS if card is None else
               len(minimal_avatar_resilient_cycle_symbols(card)))
    return repeats * symbols / MINIMAL_AVATAR_PROFILE.baud


def _decode_soft_avatar_block(codeword: list[int], received_tone_metrics: np.ndarray,
                              expected_crc: int, shortened: int) -> tuple[list[int], bool] | None:
    """Bounded Chase search for weak GF(64) symbols; CRC is the acceptance gate."""
    def checked(candidate):
        try:
            values, corrected = _rs64_decode(candidate, shortened=shortened)
        except ValueError:
            return None
        return (values, corrected) if crc16(_avatar_data_bytes(values)) == expected_crc else None

    result = checked(codeword)
    if result is not None or received_tone_metrics.shape[0] < len(codeword) * 2:
        return result
    metrics = np.asarray(received_tone_metrics[:len(codeword) * 2], dtype=np.float64)
    metrics /= np.maximum(metrics.sum(axis=1, keepdims=True), 1e-12)
    alternatives = []
    for index, hard in enumerate(codeword):
        pair = metrics[index * 2:index * 2 + 2]
        likelihood = np.asarray([pair[0, value >> 3] + pair[1, value & 7]
                                 for value in range(64)])
        order = np.argsort(likelihood)[::-1]
        # Keep the hard decisions in the received word as the baseline. Chase
        # hypotheses only replace that symbol with one of its soft runners-up.
        choices = [int(value) for value in order if int(value) != hard][:2]
        if choices:
            alternatives.append((float(likelihood[order[0]] - likelihood[order[1]]), index, choices))
    alternatives.sort()
    weakest = alternatives[:10]
    for _, index, choices in weakest:
        for value in choices:
            candidate = codeword[:]
            candidate[index] = value
            result = checked(candidate)
            if result is not None:
                return result
    # Let RS correct one additional symbol after hypothesizing two weak ones.
    for left in range(min(6, len(weakest))):
        for right in range(left + 1, min(6, len(weakest))):
            _, i, a_values = weakest[left]
            _, j, b_values = weakest[right]
            for a in a_values:
                for b in b_values:
                    candidate = codeword[:]
                    candidate[i], candidate[j] = a, b
                    result = checked(candidate)
                    if result is not None:
                        return result
    return None


def decode_minimal_avatar_resilient_symbols(symbols: Iterable[int], *, require_sync: bool = True,
                                             tone_metrics: np.ndarray | None = None) -> tuple[list[int], list[bool], int, dict]:
    tones = [int(value) for value in symbols]
    groups: dict[tuple, dict] = {}
    cursor = 0
    saw_sync = False
    while cursor <= len(tones) - len(FRAME_SYNC):
        start = next((i for i in range(cursor, len(tones) - len(FRAME_SYNC) + 1)
                      if tuple(tones[i:i + len(FRAME_SYNC)]) == FRAME_SYNC), None)
        if start is None:
            break
        saw_sync = True
        meta_start = start + len(FRAME_SYNC)
        try:
            metadata, meta_end = _decode_avatar_metadata_after(tones, meta_start)
        except (ValueError, UnicodeDecodeError, ZeroDivisionError):
            cursor = start + len(FRAME_SYNC)
            continue
        width, height = metadata["width"], metadata["height"]
        bpp = metadata["bits_per_pixel"]
        pixel_count = width * height
        new_format = metadata.get("header_version") == 4
        content_id = metadata.get("image_id")
        key = ((metadata["callsign"].upper(), metadata["grid"].upper(),
                width, height, metadata["palette_id"], content_id)
               if new_format else
               (metadata["callsign"].upper(), metadata["grid"].upper(), metadata["card_id"],
                width, height, metadata["palette_id"], metadata.get("message_type", "card"),
                metadata.get("snr_db")))
        pixels_per_block = ((AVATAR_RS_K - AVATAR_BLOCK_ID_SYMBOLS) * 6 // bpp
                            if new_format else (AVATAR_RS_K * 6) // bpp)
        block_count = (pixel_count + pixels_per_block - 1) // pixels_per_block
        group = groups.setdefault(key, {"pixels": [0] * pixel_count, "received": [False] * block_count,
                                        "block_checksums": [None] * block_count,
                                        "coverage": [False] * pixel_count,
                                        "votes": [[0] * (1 << bpp) for _ in range(pixel_count)],
                                        "copies": 0, "corrected": 0, "metadata": metadata})
        group["copies"] += 1
        block_start = meta_end
        for block_index in range(block_count):
            first_pixel = block_index * pixels_per_block
            current_pixel_count = min(pixels_per_block, pixel_count - first_pixel)
            pixel_data_count = (current_pixel_count * bpp + 5) // 6
            data_count = pixel_data_count + (AVATAR_BLOCK_ID_SYMBOLS if new_format else 0)
            coded_tone_count = (data_count + 2) * 2
            tone_count = coded_tone_count + 6
            block_end = block_start + tone_count
            data_tone_count = coded_tone_count
            data_symbols_tones = tones[block_start:min(block_end, len(tones))]
            minimum_data_tones = data_count * 2
            if len(data_symbols_tones) >= minimum_data_tones:
                # The shortened RS word is systematic: its first data_count
                # GF(64) values already make a useful preview, even before
                # parity and block CRC arrive. Keep this candidate explicitly
                # provisional; a later checked copy replaces it.
                raw_values = _tones_to_gf64(data_symbols_tones[:minimum_data_tones])
                if new_format:
                    raw_values = raw_values[AVATAR_BLOCK_ID_SYMBOLS:]
                candidate_pixels = _gf64_to_pixels(raw_values, current_pixel_count, bpp)
                first_pixel = block_index * pixels_per_block
                for pixel_offset, value in enumerate(candidate_pixels):
                    pixel_index = first_pixel + pixel_offset
                    group["votes"][pixel_index][value] += 1
                    group["coverage"][pixel_index] = True
                    group["pixels"][pixel_index] = max(
                        range(1 << bpp), key=group["votes"][pixel_index].__getitem__)

            if block_end <= len(tones):
                wire = tones[block_start:block_end]
                try:
                    coded = _tones_to_gf64(wire[:data_tone_count])
                    shortened = AVATAR_RS_K - data_count
                    expected_crc = int.from_bytes(_tones_crc16(wire[data_tone_count:]), "big")
                    try:
                        values, corrected = _rs64_decode(coded, shortened=shortened)
                        if crc16(_avatar_data_bytes(values)) != expected_crc:
                            raise ValueError("avatar block checksum failed")
                    except ValueError:
                        if tone_metrics is None:
                            raise
                        block_metrics = tone_metrics[block_start:block_start + data_tone_count]
                        recovered = _decode_soft_avatar_block(coded, block_metrics,
                                                              expected_crc, shortened)
                        if recovered is None:
                            raise
                        values, corrected = recovered
                    if new_format:
                        if values[0] != block_index or values[1] != block_count:
                            raise ValueError("avatar block position check failed")
                        values = values[AVATAR_BLOCK_ID_SYMBOLS:]
                    block_pixels = _gf64_to_pixels(values, current_pixel_count, bpp)
                    group["pixels"][first_pixel:first_pixel + current_pixel_count] = block_pixels
                    group["coverage"][first_pixel:first_pixel + current_pixel_count] = [True] * current_pixel_count
                    if not group["received"][block_index]:
                        group["corrected"] += int(corrected)
                    group["received"][block_index] = True
                    group["block_checksums"][block_index] = expected_crc
                except (ValueError, ZeroDivisionError):
                    pass
            block_start = block_end
        cursor = start + len(FRAME_SYNC)
    if require_sync and not saw_sync:
        raise ValueError("fast-resilient avatar sync sequence not found")
    if not groups:
        raise ValueError("no valid avatar identity block received yet")
    best = max(groups.values(), key=lambda item: sum(item["received"]))
    if not any(best["coverage"]):
        raise ValueError("no avatar image symbols received yet")
    metadata = dict(best["metadata"])
    if metadata.get("header_version") == 4 and all(best["received"]):
        packed = _pack_indices(best["pixels"], metadata["bits_per_pixel"])
        crc_matches = zlib.crc32(packed) == metadata["image_crc32"]
        tag_matches = hashlib.blake2s(packed, digest_size=4).hexdigest() == metadata["image_tag"]
        metadata["whole_raster_crc32_valid"] = bool(crc_matches)
        metadata["content_tag_valid"] = bool(tag_matches)
        metadata["whole_raster_valid"] = bool(crc_matches and tag_matches)
    elif metadata.get("header_version") == 4:
        metadata["whole_raster_crc32_valid"] = False
        metadata["content_tag_valid"] = False
        metadata["whole_raster_valid"] = False
    return best["pixels"], best["received"], best["corrected"], {
        **metadata, "pixel_coverage": best["coverage"],
        "received_copies": best["copies"],
        "block_checksums": best["block_checksums"]}


def _decode_repeated_avatar_header(audio: np.ndarray, sample_rate: int,
                                   profile: ModemProfile, start: int, offset: float,
                                   period: float) -> tuple[list[int], dict, int, dict, np.ndarray] | None:
    """Soft-combine repeated default 32x32 resilient cycles before header CRC."""
    candidates = [_decode_repeated_avatar_header_cycle(
        audio, sample_rate, profile, start, offset, period, cycle)
        for cycle in (FAST_AVATAR_RESILIENT_CYCLE_SYMBOLS,
                      FAST_AVATAR_RESILIENT_CYCLE_SYMBOLS_V3)]
    candidates = [candidate for candidate in candidates if candidate is not None]
    return max(candidates, key=lambda candidate: candidate[2]) if candidates else None


def _decode_repeated_avatar_header_cycle(audio: np.ndarray, sample_rate: int,
                                         profile: ModemProfile, start: int, offset: float,
                                         period: float, cycle: int):
    if period <= 0 or sample_rate / period <= 0:
        return None
    cycle_samples = cycle * period
    available = int((len(audio) - start) // cycle_samples)
    if available < 2:
        return None
    available = min(available, 10)
    header_end = len(FRAME_SYNC) + AVATAR_META_V4_TONE_COUNT
    shifts = sorted({float(offset + delta) for delta in (-15, -10, -5, 0, 5, 10, 15)})
    best = None
    for delta_start in (-4, -2, 0, 2, 4):
        for carrier in shifts:
            copies = []
            for index in range(available):
                frame_start = int(round(start + delta_start + index * cycle_samples))
                metrics = demodulate_metrics(audio, sample_rate,
                    start_sample=frame_start, frequency_offset_hz=carrier,
                    symbol_period_samples=period, profile=profile)[:cycle]
                if len(metrics) < header_end:
                    break
                normalized = metrics / np.maximum(metrics.sum(axis=1, keepdims=True), 1e-12)
                sync_score = float(np.mean(normalized[np.arange(len(FRAME_SYNC)),
                    np.asarray(FRAME_SYNC)]))
                copies.append((normalized, sync_score))
                if index >= 1 and sync_score < 0.22:
                    # A quiet gap marks the end of this burst. One weak cycle
                    # is tolerated so a fade does not discard later copies.
                    if index + 1 < available:
                        next_start = int(round(start + delta_start + (index + 1) * cycle_samples))
                        next_metrics = demodulate_metrics(audio, sample_rate,
                            start_sample=next_start, frequency_offset_hz=carrier,
                            symbol_period_samples=period, profile=profile)[:cycle]
                        if len(next_metrics) >= header_end:
                            next_norm = next_metrics / np.maximum(next_metrics.sum(axis=1, keepdims=True), 1e-12)
                            next_score = float(np.mean(next_norm[np.arange(len(FRAME_SYNC)),
                                np.asarray(FRAME_SYNC)]))
                            if next_score < 0.22:
                                break
                        else:
                            break
            for count in range(2, len(copies) + 1):
                selected = [item[0] for item in copies[:count] if item[1] >= 0.22]
                if len(selected) < 2:
                    continue
                combined_header = np.sum([item[len(FRAME_SYNC):header_end]
                                           for item in selected], axis=0)
                header_symbols = np.argmax(combined_header, axis=1).astype(int).tolist()
                try:
                    metadata, _ = _decode_avatar_metadata_after(header_symbols, 0)
                except (ValueError, UnicodeDecodeError, ZeroDivisionError):
                    continue
                if metadata.get("avatar_mode") != "fast_avatar_fec":
                    continue
                combined = np.sum(selected, axis=0)
                symbols = np.argmax(combined, axis=1).astype(int).tolist()
                symbols[:len(FRAME_SYNC)] = FRAME_SYNC
                symbols[len(FRAME_SYNC):header_end] = header_symbols
                candidate = (symbols, metadata, count,
                    {"start_sample": start + delta_start, "sample_rate": sample_rate,
                     "frequency_offset_hz": carrier, "symbol_period_samples": period,
                     "clock_error_ppm": (period / (sample_rate / profile.baud) - 1) * 1e6,
                     "combined_copies": len(selected)}, combined)
                if best is None or len(selected) > best[2]:
                    best = candidate
    return best


def minimal_avatar_duration(profile: str | ModemProfile | None = None, card: Card | None = None) -> float:
    selected = MINIMAL_AVATAR_PROFILE if profile is None else resolve_profile(profile)
    pixel_count = MINIMAL_AVATAR_WIDTH * MINIMAL_AVATAR_HEIGHT if card is None else card.width * card.height
    bits_per_pixel = 3 if card is None else AVATAR_PALETTE_BITS[_avatar_palette_id(card)]
    pixel_tones = (pixel_count * bits_per_pixel + 2) // 3
    return (len(FRAME_SYNC) + AVATAR_META_TONE_COUNT + pixel_tones) / selected.baud


def repeat_symbol_stream(symbols: Iterable[int], repeats: int) -> tuple[int, ...]:
    """Repeat one complete image attempt without requiring a return channel."""
    cycle = tuple(symbols)
    if not cycle:
        raise ValueError("cannot repeat an empty transmission")
    if not 1 <= int(repeats) <= 100:
        raise ValueError("repeat count must be between 1 and 100")
    return cycle * int(repeats)


def decode_minimal_avatar_symbols(symbols: Iterable[int], *, require_sync: bool = True) -> list[int]:
    """Majority-merge matching pixel positions across repeated raw frames."""
    return decode_minimal_avatar_symbols_progress(symbols, require_sync=require_sync)[0]


def decode_minimal_avatar_symbols_progress(symbols: Iterable[int], *, require_sync: bool = True) -> tuple[list[int], list[bool], int, dict]:
    tones = tuple(int(value) for value in symbols)
    if any(value < 0 or value > 7 for value in tones):
        raise ValueError("raw avatar contains an invalid 8-FSK symbol")
    starts = []
    if require_sync:
        cursor = 0
        while cursor <= len(tones) - len(FRAME_SYNC):
            start = next((i for i in range(cursor, len(tones) - len(FRAME_SYNC) + 1)
                          if tones[i:i + len(FRAME_SYNC)] == FRAME_SYNC), None)
            if start is None:
                break
            starts.append(start + len(FRAME_SYNC))
            cursor = start + len(FRAME_SYNC)
        if not starts:
            raise ValueError("raw avatar sync sequence not found")
    else:
        starts = [0]
    candidates: dict[tuple, dict] = {}
    for start in starts:
        meta_start = start
        try:
            metadata, meta_end = _decode_avatar_metadata_after(tones, meta_start)
        except (ValueError, UnicodeDecodeError, ZeroDivisionError):
            continue
        key = (metadata["callsign"].upper(), metadata["grid"].upper(), metadata["card_id"],
               metadata["width"], metadata["height"], metadata["palette_id"],
               metadata.get("message_type", "card"), metadata.get("snr_db"))
        pixel_count = metadata["width"] * metadata["height"]
        bpp = metadata["bits_per_pixel"]
        tone_count = (pixel_count * bpp + 2) // 3
        candidate = candidates.setdefault(key, {"metadata": metadata,
            "votes": [[0] * (1 << bpp) for _ in range(pixel_count)], "copies": 0})
        frame = tones[meta_end:meta_end + tone_count]
        available_pixels = min(pixel_count, len(frame) * 3 // bpp)
        if available_pixels:
            indices = _tones_to_indices(frame, available_pixels, bpp)
            for index, color_index in enumerate(indices):
                candidate["votes"][index][color_index] += 1
        candidate["copies"] += 1
    if not candidates:
        raise ValueError("no valid avatar identity header received yet")
    candidate = max(candidates.values(), key=lambda item: sum(any(row) for row in item["votes"]))
    votes = candidate["votes"]
    present = [any(row) for row in votes]
    if not any(present):
        raise ValueError("no avatar pixels received yet")
    pixels = [max(range(len(row)), key=row.__getitem__) if received else 0
              for row, received in zip(votes, present)]
    return pixels, present, candidate["copies"], candidate["metadata"]


def decode_minimal_avatar_audio(audio: np.ndarray, sample_rate: int = SAMPLE_RATE,
                                profile: str | ModemProfile | None = None) -> list[int]:
    """Receive the raw avatar waveform; this intentionally has no error check."""
    selected = MINIMAL_AVATAR_PROFILE if profile is None else resolve_profile(profile)
    if len(selected.tones_hz) != 8:
        raise ValueError("raw avatar audio requires an eight-tone profile")
    start, offset, period = acquire(audio, sample_rate, profile=selected)
    metrics = demodulate_metrics(audio, sample_rate, start_sample=start,
                                 frequency_offset_hz=offset,
                                 symbol_period_samples=period, profile=selected)
    return decode_minimal_avatar_symbols(np.argmax(metrics, axis=1))


def decode_minimal_avatar_audio_progress(audio: np.ndarray, sample_rate: int = SAMPLE_RATE,
                                          profile: str | ModemProfile | None = None) -> tuple[list[int], list[bool], int, dict]:
    selected = MINIMAL_AVATAR_PROFILE if profile is None else resolve_profile(profile)
    if len(selected.tones_hz) != 8:
        raise ValueError("raw avatar audio requires an eight-tone profile")
    start, offset, period = acquire(audio, sample_rate, profile=selected)
    metrics = demodulate_metrics(audio, sample_rate, start_sample=start,
                                 frequency_offset_hz=offset,
                                 symbol_period_samples=period, profile=selected)
    return decode_minimal_avatar_symbols_progress(np.argmax(metrics, axis=1))


def decode_minimal_avatar_resilient_audio(audio: np.ndarray, sample_rate: int = SAMPLE_RATE,
                                          profile: str | ModemProfile | None = None) -> tuple[list[int], list[bool], int, dict]:
    selected = MINIMAL_AVATAR_PROFILE if profile is None else resolve_profile(profile)
    if len(selected.tones_hz) != 8:
        raise ValueError("fast-resilient avatar audio requires an eight-tone profile")
    start, offset, period = acquire(audio, sample_rate, profile=selected)
    metrics = demodulate_metrics(audio, sample_rate, start_sample=start,
                                 frequency_offset_hz=offset,
                                 symbol_period_samples=period, profile=selected)
    return decode_minimal_avatar_resilient_symbols(np.argmax(metrics, axis=1))


def decode_minimal_avatar_audio_auto(audio: np.ndarray, sample_rate: int = SAMPLE_RATE,
                                     profile: str | ModemProfile | None = None) -> tuple[list[int], dict]:
    """Decode either avatar wire format from its protected identity version field."""
    selected = MINIMAL_AVATAR_PROFILE if profile is None else resolve_profile(profile)
    if len(selected.tones_hz) != 8:
        raise ValueError("avatar audio requires an eight-tone profile")
    start, offset, period = acquire(audio, sample_rate, profile=selected)
    metrics = demodulate_metrics(audio, sample_rate, start_sample=start,
                                 frequency_offset_hz=offset,
                                 symbol_period_samples=period, profile=selected)
    symbols = np.argmax(metrics, axis=1).astype(int).tolist()
    measured_snr_db = _estimate_snr_db(audio, metrics, start, period, selected, offset)
    ordered = np.sort(metrics, axis=1)
    confidence = ((ordered[:, -1] - ordered[:, -2]) / np.maximum(ordered[:, -1], 1e-12))
    acquisition = {"start_sample": start, "sample_rate": sample_rate,
                   "frequency_offset_hz": offset, "symbol_period_samples": period,
                   "clock_error_ppm": (period / (sample_rate / selected.baud) - 1) * 1e6,
                   "symbol_confidence_median": float(np.median(confidence)) if len(confidence) else None,
                   "symbol_confidence_p10": float(np.percentile(confidence, 10)) if len(confidence) else None}
    metadata = None
    cursor = 0
    while cursor <= len(symbols) - len(FRAME_SYNC):
        sync_at = next((i for i in range(cursor, len(symbols) - len(FRAME_SYNC) + 1)
                        if tuple(symbols[i:i + len(FRAME_SYNC)]) == FRAME_SYNC), None)
        if sync_at is None:
            break
        meta_start = sync_at + len(FRAME_SYNC)
        try:
            metadata, _meta_end = _decode_avatar_metadata_after(symbols, meta_start)
            break
        except (ValueError, UnicodeDecodeError, ZeroDivisionError):
            pass
        cursor = sync_at + 1
    if metadata is None:
        repeated = _decode_repeated_avatar_header(audio, sample_rate, selected,
                                                  start, offset, period)
        if repeated is None:
            raise ValueError("no valid avatar identity header received yet")
        symbols, metadata, combined_copies, acquisition, combined_metrics = repeated
    else:
        combined_copies = 1
        combined_metrics = metrics
    if metadata["avatar_mode"] == "fast_avatar_fec" or metadata["header_version"] == 1:
        try:
            pixels, blocks, corrected, fec_metadata = decode_minimal_avatar_resilient_symbols(
                symbols, tone_metrics=combined_metrics)
            pixels_per_block = (AVATAR_RS_K * 6) // fec_metadata["bits_per_pixel"]
            coverage = fec_metadata.get("pixel_coverage") or [present for index, present in enumerate(blocks)
                        for _ in range(min(pixels_per_block,
                            fec_metadata["width"]*fec_metadata["height"]-index*pixels_per_block))]
            received_copies = max(combined_copies, int(fec_metadata.get("received_copies", 1)))
            checked = sum(blocks)
            return pixels, {**fec_metadata, "measured_snr_db": measured_snr_db,
                            "acquisition": acquisition,
                            "avatar_mode": "fast_avatar_fec",
                            "avatar_burst": True,
                            "exact": (all(blocks) and
                                      (fec_metadata.get("header_version") != 4 or
                                       bool(fec_metadata.get("whole_raster_valid")))),
                            "received_blocks": blocks, "pixel_coverage": coverage,
                            "received_copies": received_copies,
                            "corrected_blocks": corrected,
                            "color_stage": f"{checked}/{len(blocks)} CRC-verified blocks · "
                                           f"{sum(coverage)}/{len(coverage)} provisional pixels" +
                                           (f" · {corrected} corrected" if corrected else "")}
        except ValueError:
            if metadata["avatar_mode"] == "fast_avatar_fec":
                raise
    pixels, present, copies, metadata = decode_minimal_avatar_symbols_progress(symbols)
    return pixels, {**metadata, "measured_snr_db": measured_snr_db,
                    "acquisition": acquisition,
                    "raw_avatar": True, "exact": False,
                    "pixel_coverage": present, "received_copies": copies,
                    "color_stage": f"{sum(present)}/{metadata['width'] * metadata['height']} pixels · {copies} copies · unverified"}


def pack_palette(palette: list[tuple[int, int, int]]) -> bytes:
    bits = []
    for rgb in palette:
        for v in rgb:
            bits.extend((int(v) >> shift) & 1 for shift in (3, 2, 1, 0))
    out = bytearray()
    for i in range(0, len(bits), 8):
        b = 0
        for bit in bits[i:i + 8]:
            b = (b << 1) | bit
        out.append(b)
    return bytes(out)


def unpack_palette(data: bytes) -> list[tuple[int, int, int]]:
    bits = [(byte >> shift) & 1 for byte in data for shift in range(7, -1, -1)]
    vals = [sum(bits[i + j] << (3 - j) for j in range(4)) for i in range(0, 96, 4)]
    return [tuple(vals[i:i + 3]) for i in range(0, 24, 3)]


def pack_bits(values: Iterable[int]) -> bytes:
    bits = [int(v) & 1 for v in values]
    bits.extend([0] * (-len(bits) % 8))
    return bytes(sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits), 8))


def unpack_bits(data: bytes, count: int) -> list[int]:
    bits = [(byte >> shift) & 1 for byte in data for shift in range(7, -1, -1)]
    return bits[:count]


def quadrant_bitplane(pixels: list[int], quadrant: int, bit: int, *, skip_preview_anchors: bool = False) -> bytes:
    qx, qy = quadrant % 2, quadrant // 2
    x0, y0 = qx * 16, qy * 16
    values = []
    for y in range(y0, y0 + 16):
        for x in range(x0, x0 + 16):
            if skip_preview_anchors and x % 4 == 0 and y % 4 == 0:
                continue
            values.append((pixels[y * 32 + x] >> bit) & 1)
    return pack_bits(values)


def luminance(rgb) -> float:
    return (299 * rgb[0] + 587 * rgb[1] + 114 * rgb[2]) / 1000


def progressive_palette(card: Card) -> tuple[list, list[int]]:
    """Brightness order makes index prefixes describe image structure."""
    permutation = sorted(range(8), key=lambda i: (luminance(card.palette[i]), i))
    inverse = {old: new for new, old in enumerate(permutation)}
    return [card.palette[i] for i in permutation], [inverse[i] for i in card.pixels]


def thumbnail_gray(card: Card) -> list[int]:
    # Average each 4x4 cell so thin marks contribute even off the sample grid.
    values = []
    for y in range(0, 32, 4):
        for x in range(0, 32, 4):
            mean = sum(luminance(card.palette[card.pixels[(y+dy)*32+x+dx]])
                       for dy in range(4) for dx in range(4)) / 16
            values.append(min(3, max(0, round(mean / 5))))
    return values


def make_packets(card: Card, mode: str = "full") -> list[bytes]:
    card.validate()
    call, grid = card.callsign.upper().encode("ascii"), card.grid.upper().encode("ascii")
    if mode not in {"full", "standard"}:
        raise ValueError("mode must be 'standard' or 'full'")
    palette, indices = progressive_palette(card)
    shades = [round(luminance(rgb)) for rgb in palette]
    shade_bytes = bytes((shades[i]<<4)|shades[i+1] for i in range(0,8,2))
    identity = bytes((len(call),len(grid))) + call + grid + shade_bytes + b"\xff"
    thumb = thumbnail_gray(card)
    bw_preview = pack_bits(v >= 2 for v in thumb)
    gray_preview = pack_bits(bit for v in thumb for bit in ((v >> 1) & 1, v & 1))
    bodies = [(TYPE_ID, 0, identity), (TYPE_PREVIEW, 1, bw_preview)]
    if mode == "standard":
        # Fixed canvas and shared 8-color palette make a compact indexed image
        # sufficient. Each quadrant is independently CRC/FEC protected so a
        # partial receive still has useful structure and intact regions.
        bodies.append((TYPE_COMPACT_PALETTE, 2,
                       bytes(channel for rgb in card.palette for channel in rgb)))
        for q in range(4):
            qx, qy = q % 2, q // 2
            values = [card.pixels[y * 32 + x]
                      for y in range(qy * 16, qy * 16 + 16)
                      for x in range(qx * 16, qx * 16 + 16)]
            bodies.append((TYPE_COMPACT_RASTER, 3 + q,
                           pack_bits((value >> bit) & 1 for value in values
                                     for bit in (2, 1, 0))))
        bodies.append((TYPE_END, 7, b"\x07"))
        return [HEADER.pack(MAGIC, (VERSION << 4) | kind, card.card_id & 0xFFFF,
                            seq, len(payload), 0) + payload
                + struct.pack(">H", crc16(HEADER.pack(MAGIC, (VERSION << 4) | kind,
                    card.card_id & 0xFFFF, seq, len(payload), 0) + payload))
                for kind, seq, payload in bodies]
    # Pixels arrive in brightness order. Once their indices are known, the
    # remaining three stages refine only eight palette swatches, not 1024 RGBs.
    bodies += [(TYPE_STRUCTURE, 2+q, bw_preview + quadrant_bitplane(indices,q,2)) for q in range(4)]
    bodies += [(TYPE_GRAY, 6+q, gray_preview + quadrant_bitplane(indices,q,1)) for q in range(4)]
    bodies += [(TYPE_LUMA, 10+q, gray_preview + quadrant_bitplane(indices,q,0)) for q in range(4)]
    for kind, seq, precision in ((TYPE_COLOR4,14,1),(TYPE_COLOR8,15,2),(TYPE_FINAL,16,4)):
        maximum = (1<<precision)-1
        values = [round(channel*maximum/15) for rgb in palette for channel in rgb]
        data = pack_bits((value>>bit)&1 for value in values for bit in range(precision-1,-1,-1))
        bodies.append((kind,seq,gray_preview+data))
    bodies.append((TYPE_GRAY_PREVIEW,17,gray_preview))
    # A distinct end marker lets an automatic receiver stop only after it has
    # captured the sender's complete single stream, including all refinements.
    bodies.append((TYPE_END,18,b"\x07"))
    packets = []
    for kind, seq, payload in bodies:
        head = HEADER.pack(MAGIC, (VERSION << 4) | kind, card.card_id & 0xFFFF, seq, len(payload), 0)
        raw = head + payload
        packets.append(raw + struct.pack(">H", crc16(raw)))
    return packets


def make_beacon_packets(card: Card, mode: str = "full") -> list[bytes]:
    """Station identity, independently readable B/W base, and end marker."""
    packets = make_packets(card, mode)
    return packets[:2] + [packets[-1]]


def make_ack_packet(card_id: int, callsign: str) -> bytes:
    """Create a small receipt confirmation for a verified card burst."""
    call = callsign.upper().encode("ascii")
    if len(call) > 12:
        raise ValueError("callsign must be at most 12 ASCII bytes")
    payload = b"\x01" + bytes((len(call),)) + call
    header = HEADER.pack(MAGIC, (VERSION << 4) | TYPE_ACK, card_id & 0xFFFF, 0, len(payload), 0)
    raw = header + payload
    return raw + struct.pack(">H", crc16(raw))


def transmission_packets(packets: list[bytes]) -> list[bytes]:
    """Repeat identity at each four-region boundary and close with the base."""
    if not packets:
        return []
    identity = next((p for p in packets if HEADER.unpack(p[:HEADER_SIZE])[1] & 15 == TYPE_ID), None)
    terminator = next((p for p in packets if HEADER.unpack(p[:HEADER_SIZE])[1] & 15 == TYPE_END), None)
    result = []
    content = [p for p in packets if p is not terminator]
    for i, packet in enumerate(content):
        if identity is not None and i > 2 and (i-2) % 4 == 0:
            result.append(identity)
        result.append(packet)
    if identity is not None:
        result.append(identity)
    if terminator is not None:
        result.extend((terminator, terminator))
    return result


def packet_symbols(packet: bytes) -> tuple[int, ...]:
    head, body = packet[:HEADER_SIZE], packet[HEADER_SIZE:]
    head_ascii = head.hex().upper().encode("ascii")
    body_ascii = base64.b85encode(body)
    return (FRAME_SYNC + encode_bytes(head_ascii, HEADER_INTERLEAVE_DEPTH)
            + encode_bytes(body_ascii, BODY_INTERLEAVE_DEPTH))


def all_symbols(packets: list[bytes]) -> tuple[int, ...]:
    if not packets:
        return ()
    return tuple(s for p in transmission_packets(packets) for s in packet_symbols(p))


def synthesize(symbols: Iterable[int], sample_rate: int = SAMPLE_RATE, baud: float | None = None,
               frequency_offset_hz: float = 0.0, profile: str | ModemProfile | None = None) -> np.ndarray:
    selected = resolve_profile(profile)
    if profile is not None and baud is not None and baud != selected.baud:
        raise ValueError("baud conflicts with selected modem profile")
    rate = selected.baud if profile is not None or baud is None else baud
    tones = selected.tones_hz if profile is not None or baud is None else TONES_HZ
    sps = sample_rate / rate
    phase = 0.0
    syms = tuple(symbols)
    out = np.empty(round(len(syms) * sps), dtype=np.float32)
    for i, tone in enumerate(syms):
        begin, end = round(i * sps), round((i + 1) * sps)
        n = np.arange(end - begin, dtype=np.float64)
        freq = tones[tone] + frequency_offset_hz
        seg = 0.72 * np.sin(2 * np.pi * freq * n / sample_rate + phase)
        phase = (phase + 2 * np.pi * freq * len(n) / sample_rate) % (2 * np.pi)
        out[begin:end] = seg
    return out


def write_wav(path: Path, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    pcm = np.clip(audio * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(sample_rate); f.writeframes(pcm.tobytes())


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as f:
        channels, width, sr, frames = f.getnchannels(), f.getsampwidth(), f.getframerate(), f.getnframes()
        raw = f.readframes(frames)
    if width != 2:
        raise ValueError("prototype receiver currently reads signed 16-bit PCM WAV")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, sr


def demodulate(audio: np.ndarray, sample_rate: int, baud: int | None = None,
               start_sample: int = 0, frequency_offset_hz: float = 0.0,
               profile: str | ModemProfile | None = None) -> tuple[int, ...]:
    metrics = demodulate_metrics(audio, sample_rate, baud, start_sample, frequency_offset_hz, profile=profile)
    return tuple(int(i) for i in np.argmax(metrics, axis=1))


def demodulate_metrics(audio: np.ndarray, sample_rate: int, baud: int | None = None,
                       start_sample: int = 0, frequency_offset_hz: float = 0.0,
                       symbol_period_samples: float | None = None,
                       profile: str | ModemProfile | None = None) -> np.ndarray:
    selected = resolve_profile(profile)
    if profile is not None and baud is not None and baud != selected.baud:
        raise ValueError("baud conflicts with selected modem profile")
    rate = selected.baud if profile is not None or baud is None else baud
    tones = selected.tones_hz if profile is not None or baud is None else TONES_HZ
    sps = symbol_period_samples or sample_rate / rate
    n = int(np.ceil((len(audio) - start_sample) / sps))
    if n == 0:
        return np.empty((0, len(tones)), dtype=np.float64)
    symbols = np.arange(n)
    starts = start_sample + np.rint(symbols * sps).astype(int)
    ends = np.minimum(len(audio), start_sample + np.rint((symbols + 1) * sps).astype(int))
    lengths = ends - starts
    metrics = np.zeros((n, len(tones)), dtype=np.float64)
    # Clock recovery produces occasional 959/960/961-sample symbols. Group
    # equal-length windows so correlation remains vectorized.
    for length in np.unique(lengths):
        rows = np.flatnonzero(lengths == length)
        chunks = np.asarray(audio[starts[rows, None] + np.arange(length)], dtype=np.float64)
        t = np.arange(length, dtype=np.float64) / sample_rate
        kernels = np.exp(-2j * np.pi * (np.asarray(tones) + frequency_offset_hz)[:, None] * t)
        metrics[rows] = np.abs(chunks @ kernels.T) ** 2
    return metrics


def _estimate_snr_db(audio: np.ndarray, metrics: np.ndarray, start_sample: int,
                     symbol_period_samples: float, profile: ModemProfile,
                     frequency_offset_hz: float) -> int | None:
    """Estimate in-band SNR from tone correlation and received sample power."""
    samples = np.asarray(audio, dtype=np.float64)
    if not len(metrics):
        return None
    symbols = np.argmax(metrics, axis=1)
    starts = start_sample + np.rint(np.arange(len(symbols)) * symbol_period_samples).astype(int)
    ends = np.minimum(len(samples), start_sample +
                      np.rint((np.arange(len(symbols)) + 1) * symbol_period_samples).astype(int))
    lengths = ends - starts
    usable = lengths > 0
    if not np.any(usable):
        return None
    rows = np.flatnonzero(usable)
    observed_power = 0.0
    signal_power = 0.0
    observed_count = 0
    for length in np.unique(lengths[usable]):
        group = rows[lengths[rows] == length]
        offsets = np.arange(int(length))
        chunks = samples[starts[group, None] + offsets]
        observed_power += float(np.square(chunks).sum())
        observed_count += chunks.size
        selected_metrics = metrics[group, symbols[group]]
        signal_power += float((2.0 * selected_metrics / (length * length)).sum()) * length
    if not observed_count:
        return None
    signal_power /= observed_count
    total_power = observed_power / observed_count
    noise_power = max(total_power - signal_power, 1e-12)
    ratio_db = 10.0 * np.log10(max(signal_power, 1e-12) / noise_power)
    return int(np.clip(np.rint(ratio_db), -30, 60))


@dataclass
class Received:
    packet_type: int
    card_id: int
    seq: int
    payload: bytes
    corrected_bits: int


def parse_symbols(symbols: tuple[int, ...], tone_metrics: np.ndarray | None = None) -> tuple[list[Received], list[str]]:
    if tone_metrics is not None:
        return parse_symbols_combined(symbols, tone_metrics)
    found, errors = [], []
    i = 0
    sync_len = len(FRAME_SYNC)
    while i <= len(symbols) - sync_len:
        if symbols[i:i + sync_len] != FRAME_SYNC:
            i += 1; continue
        start = i + sync_len
        try:
            head_count = coded_symbols_for_bytes(HEADER_WIRE_SIZE)
            head_data = symbols[start:start + head_count]
            header_ascii, fixes = decode_bytes(head_data, HEADER_WIRE_SIZE, HEADER_INTERLEAVE_DEPTH)
            magic, vt, card_id, seq, length, reserved = unarmour_header(header_ascii)
            if magic != MAGIC or vt >> 4 != VERSION or length > MAX_PAYLOAD or reserved != 0:
                i += 1; continue
            nbytes = HEADER_WIRE_SIZE + b85_size(length + 2)
            frame_count = coded_frame_symbols(nbytes)
            frame_data = symbols[start:start + frame_count]
            raw, fixed = decode_frame(frame_data, nbytes)
            check = struct.unpack(">H", raw[-2:])[0]
            if crc16(raw[:-2]) != check:
                errors.append(f"seq {seq}: CRC failed")
                i = start + frame_count
                continue
            packet = Received(vt & 15, card_id, seq, raw[HEADER_SIZE:-2], fixed)
            if not any((item.card_id, item.packet_type, item.seq) == (card_id, vt & 15, seq) for item in found):
                found.append(packet)
            i = start + frame_count
        except (ValueError, struct.error, IndexError):
            i += 1
    return found, errors


def parse_symbols_combined(symbols: tuple[int, ...], tone_metrics: np.ndarray) -> tuple[list[Received], list[str]]:
    """Combine normalized tone evidence from repeated copies of each packet."""
    return decode_packet_evidence(collect_packet_evidence(symbols, tone_metrics))


def collect_packet_evidence(symbols: tuple[int, ...], tone_metrics: np.ndarray) -> dict[tuple[int, int, int, int], dict]:
    combined: dict[tuple[int, int, int, int], dict] = {}
    i = 0
    while i <= len(symbols) - len(FRAME_SYNC):
        if symbols[i:i + len(FRAME_SYNC)] != FRAME_SYNC:
            i += 1
            continue
        start = i + len(FRAME_SYNC)
        try:
            head_count = coded_symbols_for_bytes(HEADER_WIRE_SIZE)
            header_ascii, _ = decode_bytes_soft(tone_metrics[start:start + head_count],
                                                 HEADER_WIRE_SIZE, HEADER_INTERLEAVE_DEPTH)
            magic, vt, card_id, seq, length, reserved = unarmour_header(header_ascii)
            if magic != MAGIC or vt >> 4 != VERSION or length > MAX_PAYLOAD or reserved != 0:
                i += 1
                continue
            nbytes = HEADER_WIRE_SIZE + b85_size(length + 2)
            end = start + coded_frame_symbols(nbytes)
            if end > len(tone_metrics):
                break
            key = (card_id, vt, seq, nbytes)
            evidence = np.asarray(tone_metrics[start:end], dtype=np.float64)
            evidence = evidence / np.maximum(evidence.sum(axis=1, keepdims=True), 1e-15)
            log_evidence = np.log(evidence + 1e-15)
            if key in combined:
                item = combined[key]
                item["log_likelihood"] += log_evidence
                item["copies"] += 1
            else:
                combined[key] = {"card_id":card_id,"version_type":vt,"seq":seq,"nbytes":nbytes,
                                 "log_likelihood":log_evidence,"copies":1,"valid_raw":None,
                                 "valid_corrected":0}
            # Keep this copy even if the body cannot yet be decoded. Later
            # passes may supply enough soft evidence to recover it.
            try:
                single_raw, single_corrected = decode_frame_soft(evidence, nbytes)
                if crc16(single_raw[:-2]) == struct.unpack(">H", single_raw[-2:])[0] and combined[key]["valid_raw"] is None:
                    combined[key]["valid_raw"] = single_raw.hex()
                    combined[key]["valid_corrected"] = single_corrected
            except (ValueError, UnicodeDecodeError, struct.error, IndexError):
                pass
            i = end
        except (ValueError, struct.error, IndexError):
            i += 1
    return combined


def decode_packet_evidence(evidence: dict[tuple[int, int, int, int], dict]) -> tuple[list[Received], list[str]]:
    found, errors = [], []
    for (card_id, vt, seq, nbytes), item in evidence.items():
        try:
            if item.get("valid_raw"):
                raw = bytes.fromhex(item["valid_raw"])
                corrected = int(item.get("valid_corrected", 0))
            else:
                log_likelihood = np.asarray(item["log_likelihood"], dtype=np.float64)
                combined_metrics = np.exp(log_likelihood - log_likelihood.max(axis=1, keepdims=True))
                raw, corrected = decode_frame_soft(combined_metrics, nbytes)
        except (ValueError, UnicodeDecodeError, struct.error, IndexError):
            errors.append(f"seq {seq}: packet body undecodable after {item['copies']} copy/copies")
            continue
        check = struct.unpack(">H", raw[-2:])[0]
        if crc16(raw[:-2]) != check:
            errors.append(f"seq {seq}: CRC failed after {item['copies']} copy/copies")
            continue
        found.append(Received(vt & 15, card_id, seq, raw[HEADER_SIZE:-2], corrected))
    return found, errors


def merge_evidence_states(existing: dict, incoming: dict[tuple[int, int, int, int], dict]) -> dict:
    if existing.get("format") != "pixel-qso-soft-evidence" or existing.get("version") != 5:
        raise ValueError("unsupported soft-evidence state file")
    merged = {}
    for item in existing.get("packets", []):
        key = (int(item["card_id"]), int(item["version_type"]), int(item["seq"]), int(item["nbytes"]))
        if key in merged:
            raise ValueError("duplicate packet key in soft-evidence state")
        values = np.asarray(item["log_likelihood"], dtype=np.float64)
        if values.shape != (coded_frame_symbols(key[3]), 4):
            raise ValueError("invalid soft-evidence dimensions")
        merged[key] = {**item, "log_likelihood":values}
    for key, item in incoming.items():
        if key not in merged:
            merged[key] = {**item, "log_likelihood":item["log_likelihood"].copy()}
            continue
        old = merged[key]
        old["log_likelihood"] += item["log_likelihood"]
        old["copies"] = int(old["copies"]) + int(item["copies"])
        if not old.get("valid_raw") and item.get("valid_raw"):
            old["valid_raw"] = item["valid_raw"]
            old["valid_corrected"] = item.get("valid_corrected", 0)
    packets = [{**item, "log_likelihood":np.asarray(item["log_likelihood"]).tolist()}
               for item in merged.values()]
    return {"format":"pixel-qso-soft-evidence","version":5,"packets":packets}


def new_evidence_state(incoming: dict[tuple[int, int, int, int], dict]) -> dict:
    return merge_evidence_states({"format":"pixel-qso-soft-evidence","version":5,"packets":[]}, incoming)


def acquire(audio: np.ndarray, sample_rate: int, baud: int | None = None,
            profile: str | ModemProfile | None = None) -> tuple[int, float, float]:
    """Acquire start/carrier, then estimate fractional samples per symbol."""
    selected = resolve_profile(profile)
    if profile is not None and baud is not None and baud != selected.baud:
        raise ValueError("baud conflicts with selected modem profile")
    rate = selected.baud if profile is not None or baud is None else baud
    tones = selected.tones_hz if profile is not None or baud is None else TONES_HZ
    narrow_avatar = len(tones) == 8 and rate <= 100
    sps = sample_rate // rate
    if len(audio) < sps * (len(FRAME_SYNC) + 1):
        return 0, 0.0, float(sps)
    block = max(1, sample_rate // 1000)
    rms = np.sqrt(np.mean(np.asarray(audio[:len(audio)//block*block]).reshape(-1, block) ** 2, axis=1))
    peak = float(rms.max(initial=0.0))
    if peak == 0:
        return 0, 0.0, float(sps)
    active = np.flatnonzero(rms >= peak * 0.12)
    onset = int(active[0] * block) if len(active) else 0
    # Recordings may begin inside a refinement. Locate the next preamble
    # anywhere in the receive window, rather than assuming the audio onset
    # itself is a frame boundary. Live reception bounds this window; restricting
    # acquisition to its first 35 seconds misses later on-air transmissions.
    decimation = max(1, sample_rate // 8000)
    search = np.asarray(audio[::decimation], dtype=np.float64)
    search_rate = sample_rate / decimation
    bounds = np.rint(np.arange(len(FRAME_SYNC)+1)*search_rate/rate).astype(int)
    length = int(bounds[-1])
    if len(search) >= length:
        frequencies = np.concatenate([np.full(bounds[i+1]-bounds[i], tones[tone])
                                      for i, tone in enumerate(FRAME_SYNC)])
        phases = 2*np.pi*np.r_[0, np.cumsum(frequencies[:-1])]/search_rate
        times = np.arange(length)/search_rate
        fft_size = 1 << (len(search)+length-2).bit_length()
        spectrum = np.fft.fft(search, fft_size)
        best = np.zeros(len(search)-length+1)
        energy_sum = np.r_[0.0, np.cumsum(search*search)]
        energy = energy_sum[length:] - energy_sum[:-length]
        # A slow preamble loses correlation at offsets between coarse bins;
        # over a long window this can let a payload pattern outrank the sync.
        search_step = 0.5 if narrow_avatar else 2.0
        for shift in np.arange(-20.0,20.1,search_step):
            reference = np.exp(1j*(phases+2*np.pi*shift*times))
            corr = np.fft.ifft(spectrum*np.fft.fft(np.conj(reference[::-1]),fft_size))
            score = np.abs(corr[length-1:len(search)]) / np.sqrt(np.maximum(energy,1e-12)*length)
            score[energy < max(float(energy.max())*1e-6,1e-12)] = 0
            best = np.maximum(best,score)
        # Slow avatar syncs give broad near-equal peaks; choosing an early
        # shoulder can lock before the actual preamble after leading silence.
        peak_floor = 0.9999 if narrow_avatar else 0.985
        peaks = np.flatnonzero(best >= float(best.max()) * peak_floor)
        if len(peaks) and best.max() > 0.05:
            onset = int(peaks[0])*decimation
    candidates = range(max(0, onset - block), min(len(audio) - sps * len(PREAMBLE), onset + block + 1))
    shifts = np.arange(-20.0, 20.1, 2.0)
    n = np.arange(sps, dtype=np.float64) / sample_rate
    scored = []
    denom_floor = 1e-12
    for shift in shifts:
        freqs = np.asarray([tones[tone] + shift for tone in PREAMBLE])
        kernels = np.exp(-2j * np.pi * freqs[:, None] * n)
        for start in candidates:
            chunks = np.asarray(audio[start:start + sps * len(PREAMBLE)], dtype=np.float64).reshape(len(PREAMBLE), sps)
            projections = np.abs(np.sum(chunks * kernels, axis=1))
            norms = np.sqrt(np.sum(chunks * chunks, axis=1))
            score = float(np.mean(projections / np.maximum(norms, denom_floor)))
            scored.append((score, start, float(shift)))
    best_score = max(item[0] for item in scored)
    # The alternating preamble has a broad timing peak. Within 0.5% of the
    # maximum, prefer the candidate nearest the envelope onset to avoid a late
    # lock that would shave samples from the final packet.
    near_best = [item for item in scored if item[0] >= best_score * 0.995]
    eligible_starts = {item[1] for item in near_best}
    best_start = min(eligible_starts, key=lambda start: abs(start - onset))
    best_shift = max((item for item in scored if item[1] == best_start), key=lambda item: item[0])[2]
    # Jointly fit start and period to a phase-continuous template for the full
    # sync. Independent per-symbol tone scores have a broad, biased clock peak.
    expected = FRAME_SYNC

    def template_score(start: int, period: float, carrier_shift: float) -> float:
        phase = 0.0
        sine_parts, cosine_parts = [], []
        bounds = start + np.rint(np.arange(len(expected) + 1) * period).astype(int)
        for index, tone in enumerate(expected):
            length = int(bounds[index + 1] - bounds[index])
            freq = tones[tone] + carrier_shift
            angle = 2 * np.pi * freq * np.arange(length, dtype=np.float64) / sample_rate + phase
            sine_parts.append(np.sin(angle)); cosine_parts.append(np.cos(angle))
            phase = (phase + 2 * np.pi * freq * length / sample_rate) % (2 * np.pi)
        sine_ref, cosine_ref = np.concatenate(sine_parts), np.concatenate(cosine_parts)
        if bounds[-1] > len(audio):
            return -np.inf
        segment = np.asarray(audio[bounds[0]:bounds[-1]], dtype=np.float64)
        norm = max(float(np.linalg.norm(segment)), 1e-12)
        return float(np.hypot(np.dot(segment, sine_ref), np.dot(segment, cosine_ref)) /
                     (norm * np.sqrt(len(segment))))

    starts = range(max(0, best_start - 2), best_start + 3)
    coarse_periods = np.linspace(sps * 0.995, sps * 1.005, 101)
    candidates = [(template_score(start, float(period), best_shift), start, float(period))
                  for start in starts for period in coarse_periods]
    coarse_score, coarse_start, coarse_period = max(candidates)
    fine_periods = np.unique(np.r_[np.linspace(max(sps * 0.995, coarse_period - sps * 0.0001),
                                               min(sps * 1.005, coarse_period + sps * 0.0001), 21),
                                           float(sps)])
    fine_starts = range(max(0, coarse_start - 1), coarse_start + 2)
    # At high baud the alternating preamble is short, so its coarse CFO peak
    # can be several hertz off. Broaden that search only for those profiles.
    fine_shifts = (np.arange(-20.0, 20.01, 1.0) if rate >= 300 else
                   np.arange(best_shift - 2.0, best_shift + 2.01, 0.25))
    refined = [(template_score(start, float(period), float(carrier)), start, float(period), float(carrier))
               for start in fine_starts for period in fine_periods for carrier in fine_shifts]
    best_clock_score, recovered_start, recovered_period, recovered_shift = max(refined)
    # The short sync has a flat clock peak. When the nominal sample clock
    # scores essentially as well, keep the exact generated rate; longer
    # packet anchors can subsequently track capture-device clock drift.
    # At low tone frequencies, the energy-only coarse CFO estimate can
    # miss the true carrier by more than the local refinement window.
    nominal_shifts = np.arange(-20.0, 20.01, 0.25) if narrow_avatar else fine_shifts
    nominal = [(template_score(start, float(sps), float(carrier)), start, float(carrier))
               for start in fine_starts for carrier in nominal_shifts]
    nominal_score, nominal_start, nominal_shift = max(nominal)
    # Short syncs become much less informative about sub-sample timing at
    # high symbol rates. Their best isolated fit can be biased just enough to
    # walk out of a long card. Keep the known nominal sample clock when it is
    # close, then let the multi-frame anchor fit below track capture-clock drift.
    # Slow avatars also need this tolerance: their short sync fit can bias
    # timing enough to walk out of a long card at any audio placement.
    nominal_floor = 0.95 if rate >= 300 or narrow_avatar else 0.999
    if nominal_score >= best_clock_score * nominal_floor:
        recovered_start, recovered_period, recovered_shift = nominal_start, float(sps), nominal_shift
    # Below the coherent matched-filter floor, a long search can fit a noise
    # peak and report a plausible but false clock. Fall back to nominal timing.
    if best_clock_score < 0.05:
        recovered_start, recovered_period = best_start, float(sps)
        recovered_shift = best_shift
    elif best_clock_score < 0.3 and float(sps).is_integer():
        # At low SNR the short sync can bias the clock fit by a fraction of a
        # symbol. Prefer the exact digital sample clock until longer anchors
        # can refine it; all shipped rates divide common 8/48 kHz audio rates.
        recovered_period = float(sps)
    return recovered_start, float(recovered_shift), recovered_period


def select_capture_profile(audio: np.ndarray, sr: int) -> ModemProfile:
    """Choose a profile from CRC-validated frames in a short capture prefix."""
    probe = audio[:min(len(audio), 20 * sr)]
    ranked = []
    for index, candidate in enumerate(PROFILES):
        evidence, start, shift, period = receive_capture_evidence(probe, sr, profile=candidate)
        packets, errors = decode_packet_evidence(evidence)
        if packets:
            return candidate
        ranked.append(((len(packets), len(evidence), -len(errors), -index), candidate))
    return max(ranked, key=lambda item: item[0])[1]


def decode_wav_details(path: Path, profile: str | ModemProfile | None = "auto") -> tuple[list[Received], list[str], int, int, float, float]:
    audio, sr = read_wav(path)
    selected = select_capture_profile(audio, sr) if profile is None or profile == "auto" else resolve_profile(profile)
    evidence, start, shift, symbol_period = receive_capture_evidence(audio, sr, profile=selected)
    packets, errors = decode_packet_evidence(evidence)
    return packets, errors, sr, start, shift, symbol_period


def receive_capture_evidence(audio: np.ndarray, sr: int, *, phase_search: bool = False,
                             profile: str | ModemProfile | None = None) -> tuple[dict, int, float, float]:
    selected = resolve_profile(profile)
    start, shift, symbol_period = acquire(audio, sr, profile=selected)
    # A short preamble only estimates the clock coarsely. Fit it again across
    # independently found frame boundaries so timing does not drift over a card.
    initial = demodulate_metrics(audio,sr,start_sample=start,frequency_offset_hz=shift,
                                 symbol_period_samples=symbol_period, profile=selected)
    hard = tuple(int(v) for v in np.argmax(initial,axis=1))
    anchors = [i for i in range(len(hard)-len(FRAME_SYNC)+1) if hard[i:i+len(FRAME_SYNC)] == FRAME_SYNC]
    if len(anchors)>=4 and anchors[-1]-anchors[0]>5*sr/symbol_period:
        bounds = np.rint(np.arange(len(FRAME_SYNC)+1)*symbol_period).astype(int)
        frequencies = np.concatenate([np.full(bounds[i+1]-bounds[i],selected.tones_hz[tone]+shift)
                                      for i,tone in enumerate(FRAME_SYNC)])
        reference = np.exp(2j*np.pi*np.r_[0,np.cumsum(frequencies[:-1])]/sr)
        points = []
        for index in anchors:
            predicted = round(start+index*symbol_period)
            low = max(0,predicted-round(symbol_period/2))
            high = min(len(audio),predicted+round(symbol_period/2)+len(reference))
            segment = audio[low:high]
            if len(segment)<len(reference): continue
            score = np.abs(np.correlate(segment,reference,mode="valid"))
            points.append((index,low+int(np.argmax(score))))
        if len(points)>=2:
            x,y = np.asarray(points,dtype=float).T
            # The coarse clock may have slipped one hard-decision symbol
            # between distant frames. Recover integer gaps from local timing.
            x = x[0] + np.r_[0,np.cumsum(np.rint(np.diff(y)/symbol_period))]
            fitted_period,fitted_start = np.polyfit(x,y,1)
            residual = np.abs(y-(fitted_start+x*fitted_period))
            if np.max(residual)<symbol_period/4 and abs(fitted_period-sr/selected.baud)<sr/selected.baud*0.005:
                start,symbol_period = max(0,round(fitted_start)),float(fitted_period)
    best_start = start
    if phase_search:
        # A complete first identity frame fits in this prefix. Search timing
        # there, then demodulate the complete recording just once.
        probe_end = min(len(audio), start + round(20 * sr))
        candidates = []
        for candidate_start in range(max(0, start - 8), start + 9):
            metrics = demodulate_metrics(audio[:probe_end], sr, start_sample=candidate_start,
                                         frequency_offset_hz=shift, symbol_period_samples=symbol_period,
                                         profile=selected)
            hard_symbols = tuple(int(i) for i in np.argmax(metrics, axis=1))
            evidence = collect_packet_evidence(hard_symbols, metrics)
            packets, _ = decode_packet_evidence(evidence)
            rank = (len(packets), len(evidence), -abs(candidate_start - start))
            candidates.append((rank, candidate_start))
        _, best_start = max(candidates, key=lambda item: item[0])
    metrics = demodulate_metrics(audio, sr, start_sample=best_start,
                                 frequency_offset_hz=shift, symbol_period_samples=symbol_period,
                                 profile=selected)
    hard_symbols = tuple(int(i) for i in np.argmax(metrics, axis=1))
    evidence = collect_packet_evidence(hard_symbols, metrics)
    return evidence, best_start, shift, symbol_period


def decode_wav(path: Path, profile: str | ModemProfile | None = "auto") -> tuple[list[Received], list[str], int]:
    packets, errors, sr, _, _, _ = decode_wav_details(path, profile)
    return packets, errors, sr


def decode_wav_with_state(path: Path, state_path: Path,
                          profile: str | ModemProfile | None = "auto") -> tuple[list[Received], list[str], int, dict]:
    audio, sr = read_wav(path)
    selected = select_capture_profile(audio, sr) if profile is None or profile == "auto" else resolve_profile(profile)
    incoming, start, shift, symbol_period = receive_capture_evidence(audio, sr, phase_search=True, profile=selected)
    fresh_packets, fresh_errors = decode_packet_evidence(incoming)
    if state_path.exists():
        state = json.loads(state_path.read_text())
        state = merge_evidence_states(state, incoming)
    else:
        state = new_evidence_state(incoming)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_name(state_path.name + ".tmp")
    temp_path.write_text(json.dumps(state, separators=(",", ":")) + "\n")
    temp_path.replace(state_path)
    indexed = {}
    for item in state["packets"]:
        key = (int(item["card_id"]), int(item["version_type"]), int(item["seq"]), int(item["nbytes"]))
        indexed[key] = {**item, "log_likelihood":np.asarray(item["log_likelihood"], dtype=np.float64)}
    packets, errors = decode_packet_evidence(indexed)
    details = {"start_seconds":start/sr,"carrier_offset_hz":shift,"estimated_baud":sr/symbol_period,
               "profile":selected.key,
               "state_path":str(state_path),"packet_keys":len(state["packets"]),
               "accumulated_packet_copies":sum(int(item["copies"]) for item in state["packets"]),
               "fresh_valid_packets":[{"type":p.packet_type,"card_id":p.card_id,"seq":p.seq,
                                         "payload_hex":p.payload.hex()}
                                      for p in fresh_packets],
               "fresh_identity_evidence_card_ids":sorted({int(key[0]) for key in incoming
                    if (int(key[1]) & 15) == TYPE_ID and int(key[2]) == 0}),
               "fresh_errors":fresh_errors}
    return packets, errors, sr, details


def reconstruct(packets: list[Received]) -> tuple[dict, list[list[tuple[int,int,int]]]]:
    identity = next((p for p in reversed(packets) if p.packet_type == TYPE_ID),None)
    selected = identity.card_id if identity else (packets[-1].card_id if packets else None)
    relevant = [p for p in packets if p.card_id == selected]
    meta = {"callsign":"","grid":"","card_id":selected,"palette":None,
            "preview_received":False}
    shades = None
    if identity:
        d=identity.payload
        if len(d)>=7:
            cl,gl=d[:2]; end=2+cl+gl+4
            if len(d)==end+1:
                shades=[v for byte in d[end-4:end] for v in (byte>>4,byte&15)]
                meta.update(callsign=d[2:2+cl].decode("ascii","replace"),
                            grid=d[2+cl:2+cl+gl].decode("ascii","replace"))
    thumb=None; thumb_depth=0; planes=[{}, {}, {}]; palette=None; precision=0
    for packet in relevant:
        kind,seq,d=packet.packet_type,packet.seq,packet.payload
        if kind==TYPE_PREVIEW and len(d)==8:
            if thumb_depth<1:
                thumb=[v*15 for v in unpack_bits(d,64)];thumb_depth=1
        elif kind==TYPE_STRUCTURE and 2<=seq<=5 and len(d)==40:
            if thumb_depth<1:
                thumb=[v*15 for v in unpack_bits(d[:8],64)];thumb_depth=1
            planes[0][seq-2]=unpack_bits(d[8:],256)
        elif kind == TYPE_COMPACT_PALETTE and len(d) == 24:
            palette = [tuple(d[i:i + 3]) for i in range(0, 24, 3)]
            precision = 4
        elif kind == TYPE_COMPACT_RASTER and 3 <= seq <= 6 and len(d) == 96:
            raster = unpack_bits(d, 768)
            q = seq - 3
            for i in range(256):
                prefix = (raster[3*i] << 2) | (raster[3*i + 1] << 1) | raster[3*i + 2]
                y, x = (q // 2) * 16 + i // 16, (q % 2) * 16 + i % 16
                # The palette packet may arrive after a raster quadrant.
                planes[0].setdefault(100 + q, [None] * 256)[i] = prefix
        elif kind in (TYPE_GRAY,TYPE_LUMA,TYPE_COLOR4,TYPE_COLOR8,TYPE_FINAL,TYPE_GRAY_PREVIEW):
            expected={TYPE_GRAY:48,TYPE_LUMA:48,TYPE_COLOR4:19,TYPE_COLOR8:22,TYPE_FINAL:28,TYPE_GRAY_PREVIEW:16}[kind]
            if len(d)!=expected: continue
            bits=unpack_bits(d[:16],128)
            thumb=[5*((bits[i]<<1)|bits[i+1]) for i in range(0,128,2)];thumb_depth=2
            if kind==TYPE_GRAY and 6<=seq<=9: planes[1][seq-6]=unpack_bits(d[16:],256)
            elif kind==TYPE_LUMA and 10<=seq<=13: planes[2][seq-10]=unpack_bits(d[16:],256)
            elif kind in (TYPE_COLOR4,TYPE_COLOR8,TYPE_FINAL):
                depth={TYPE_COLOR4:1,TYPE_COLOR8:2,TYPE_FINAL:4}[kind]
                if depth>precision:
                    bits=unpack_bits(d[16:],24*depth)
                    values=[round(15*sum(bits[i+j]<<(depth-1-j) for j in range(depth))/((1<<depth)-1))
                            for i in range(0,24*depth,depth)]
                    palette=[tuple(values[i:i+3]) for i in range(0,24,3)];precision=depth
    image=[[(0,0,0) for _ in range(32)] for _ in range(32)]
    if thumb is not None:
        meta["preview_received"]=True
        for y in range(32):
            for x in range(32):
                v=thumb[(y//4)*8+x//4];image[y][x]=(v,v,v)
    depths=[]
    for q in range(4):
        raster = planes[0].get(100 + q)
        if raster is not None:
            for i, index in enumerate(raster):
                if index is not None:
                    y, x = (q // 2) * 16 + i // 16, (q % 2) * 16 + i % 16
                    if palette:
                        image[y][x] = tuple(palette[index])
                    else:
                        v = round(index * 15 / 7); image[y][x] = (v, v, v)
    for q in range(4):
        depth=0
        while depth<3 and q in planes[depth]: depth+=1
        depths.append(depth)
        if not depth: continue
        for i in range(256):
            y,x=(q//2)*16+i//16,(q%2)*16+i%16
            prefix=0
            for plane in range(depth): prefix=(prefix<<1)|planes[plane][q][i]
            first,last=prefix*(1<<(3-depth)),(prefix+1)*(1<<(3-depth))
            if palette:
                choices=palette[first:last]
                image[y][x]=tuple(round(sum(c[k] for c in choices)/len(choices)) for k in range(3))
            elif depth>=2 and shades:
                v=round(sum(shades[first:last])/(last-first));image[y][x]=(v,v,v)
            elif depth==1:
                v=prefix*15;image[y][x]=(v,v,v)
    exact=precision==4 and (all(d==3 for d in depths) or all(100 + q in planes[0] for q in range(4)))
    stage=("final image" if exact else "more accurate color" if precision>=2 and any(depths)
           else "color" if precision and any(depths) else "grayscale" if thumb_depth==2 or any(d>=2 for d in depths)
           else "black-and-white")
    meta.update(palette=palette,color_stage=stage,exact=exact,base_depth=thumb_depth,
                structure_regions=sorted(planes[0]),grayscale_regions=sorted(planes[1]),
                luma_regions=sorted(planes[2]),color4_regions=sorted(planes[0]) if precision else [],
                color8_regions=sorted(planes[2]) if precision==4 else [],palette_precision=precision)
    return meta,image


def is_complete_card(packets: list[Received]) -> bool:
    """True only when a CRC-valid end marker and exact image coexist."""
    if not packets:
        return False
    meta, _image = reconstruct(packets)
    card_id = meta.get("card_id")
    if card_id is None or not meta.get("exact"):
        return False
    has_identity = any(p.card_id == card_id and p.packet_type == TYPE_ID for p in packets)
    has_end = any(p.card_id == card_id and p.packet_type == TYPE_END and p.payload == b"\x07"
                  for p in packets)
    return bool(has_identity and has_end)


def example_card() -> Card:
    # Small radio tower under a starry sky, drawn with the modem's eight fixed swatches.
    palette = [(0, 1, 4), (3, 8, 13), (7, 6, 14), (13, 3, 4), (4, 12, 15), (15, 9, 4), (11, 15, 6), (15, 14, 11)]
    px = [0] * 1024
    def put(x, y, c):
        if 0 <= x < 32 and 0 <= y < 32: px[y*32+x] = c
    # Horizon and stars
    for y in range(24, 32):
        for x in range(32): put(x, y, 7 if (x+y) % 7 else 2)
    for x, y in ((3,4),(8,7),(25,3),(28,10),(5,14),(20,8),(13,2),(29,17)):
        put(x,y,1)
    # Sun/moon and tower beacon
    for y in range(5, 12):
        for x in range(20, 27):
            if (x-23)**2+(y-8)**2 <= 10: put(x,y,4)
    # Tower legs, crossbars and beacon
    for y in range(9, 27):
        width=max(1,(27-y)//5)
        for d in range(-width,width+1): put(15+d,y,2 if d==0 else 1)
        if y in (14,19,24):
            for x in range(15-width,16+width): put(x,y,6 if (x+y)%2 else 1)
    for y in range(7, 12):
        for x in range(13, 18):
            if abs(x-15)+abs(y-9) <= 3: put(x,y,5 if (x+y)%2 else 3)
    # foreground grass
    for x in range(32):
        put(x,27,3)
        if x%3==0: put(x,26,3)
    return Card("W7PXQ", "CN87", palette, px, 0x51A7)


def from_json(path: Path) -> Card:
    obj = json.loads(path.read_text())
    pixels = obj.get("pixels")
    pal = obj.get("palette")
    if pixels is None or pal is None:
        raise ValueError("card JSON needs `pixels` (1,024 indices) and `palette` (8 RGB444 triples); export these from Pixel QSO")
    return Card(str(obj.get("callsign", "N0CALL")), str(obj.get("grid", "")), [tuple(map(int,c)) for c in pal], list(map(int,pixels)), int(obj.get("card_id", 0xC0DE)), int(obj.get("width", 32)), int(obj.get("height", 32)))


def save_svg(path: Path, image: list[list[int]], palette: list[tuple[int,int,int]], title: str, callsign: str, grid: str, status: str) -> None:
    from html import escape
    rects = "".join(f'<rect x="{x}" y="{y}" width="1" height="1" fill="#{image[y][x][0]*17:02x}{image[y][x][1]*17:02x}{image[y][x][2]*17:02x}"/>' for y in range(32) for x in range(32))
    callsign, grid, title, status = map(escape, (callsign, grid, title, status))
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="800" height="560" viewBox="0 0 800 560"><rect width="800" height="560" fill="#101812"/><rect x="18" y="18" width="764" height="524" rx="15" fill="#17231d" stroke="#52705a" stroke-width="2"/><text x="48" y="66" fill="#c5f36b" font-family="monospace" font-size="17">PIXEL QSO / RECEIVER PREVIEW</text><text x="48" y="116" fill="#edf1e8" font-family="monospace" font-size="25">{callsign or '—'}</text><text x="48" y="145" fill="#96a89a" font-family="monospace" font-size="16">GRID {grid or '—'} · {title}</text><svg x="50" y="178" width="448" height="320" viewBox="0 0 32 32" shape-rendering="crispEdges">{rects}</svg><text x="535" y="218" fill="#c5f36b" font-family="monospace" font-size="15">{status}</text><text x="535" y="254" fill="#a7b4a9" font-family="monospace" font-size="13">32 × 32 · 8 COLOURS</text><text x="535" y="281" fill="#a7b4a9" font-family="monospace" font-size="13">RGB444 PALETTE</text><text x="535" y="308" fill="#a7b4a9" font-family="monospace" font-size="13">3 BITS / PIXEL</text><text x="50" y="520" fill="#91a095" font-family="monospace" font-size="13">{status}</text></svg>'''
    path.write_text(svg)


def run_demo(out_dir: Path) -> None:
    """Measured progressive cutoffs and arbitrary-entry examples for this wire format."""
    out_dir.mkdir(parents=True, exist_ok=True)
    card = example_card()
    packets = make_packets(card)
    scheduled = transmission_packets(packets)
    sr = 8000
    symbols = all_symbols(packets)
    audio = synthesize(symbols, sample_rate=sr, profile=DEFAULT_PROFILE)
    (out_dir / "w7pxq-card.json").write_text(json.dumps({
        "format":"pixel-qso-card","version":1,"card_id":card.card_id,
        "callsign":card.callsign,"grid":card.grid,"palette":card.palette,"pixels":card.pixels,
    },indent=2)+"\n")
    write_wav(out_dir / "sent-w7pxq.wav",audio,sr)
    stage_ends = {}
    position = 0
    ends = {5:"bw",13:"gray",14:"color",15:"color-refined",16:"full",18:"complete"}
    for packet in scheduled:
        position += len(packet_symbols(packet))
        seq = HEADER.unpack(packet[:HEADER_SIZE])[3]
        if seq in ends: stage_ends[ends[seq]] = position
    cases = [(name,audio[:round(end*sr/BAUD)]) for name,end in stage_ends.items()]
    # A receiver that begins in the middle of a later packet must acquire
    # the next frame and recover its embedded grayscale base.
    late_windows = []
    for fraction in (0.25,0.5,0.75):
        offset = round((len(audio)*fraction)+0.137*sr)
        name = f"late-{round(fraction*100)}"
        cases.append((name,audio[offset:offset+45*sr]))
        late_windows.append(name)
    rng = np.random.default_rng(1)
    cases.append(("noisy",(0.04*audio+rng.normal(0,0.15,len(audio))).astype(np.float32)))
    reports = []
    source_pixels = [card.palette[i] for i in card.pixels]
    for name,capture in cases:
        path = out_dir / f"received-{name}-capture.wav"
        write_wav(path,capture,sr)
        received,errors,_,start,shift,period = decode_wav_details(path, profile=DEFAULT_PROFILE)
        meta,image = reconstruct(received)
        if name == "full" and [v for row in image for v in row] != source_pixels:
            raise RuntimeError("clean full capture did not reconstruct exact pixels")
        if name in late_windows and not meta["preview_received"]:
            raise RuntimeError(f"{name}: no late-entry base image decoded")
        save_svg(out_dir / f"received-{name}.svg",image,meta["palette"] or card.palette,
                 "FIELD CARD",meta["callsign"],meta["grid"],meta["color_stage"])
        item = {"name":name,"capture":path.name,"duration_seconds":round(len(capture)/sr,2),
                "valid_packets":len(received),"color_stage":meta["color_stage"],"exact":meta["exact"],
                "preview_received":meta["preview_received"],"base_depth":meta["base_depth"],
                "packet_errors":errors,"acquired_start_seconds":round(start/sr,4),
                "carrier_offset_hz":shift,"estimated_baud":round(sr/period,4)}
        reports.append(item)
        print(f"{name}: {len(received)} packets, {meta['color_stage']}, base depth {meta['base_depth']}",flush=True)
    report = {"wire_version":VERSION,"sample_rate":sr,"profile":DEFAULT_PROFILE,
              "baud":resolve_profile(DEFAULT_PROFILE).baud,"tones_hz":resolve_profile(DEFAULT_PROFILE).tones_hz,
              "unique_packets":len(packets),"transmitted_packets":len(scheduled),
              "duration_seconds":round(len(audio)/sr,2),"received_examples":reports,
              "note":"Synthetic audio only; no radio channel or hardware is involved."}
    (out_dir / "examples.json").write_text(json.dumps(report,indent=2)+"\n")
    rows = ["# Progressive card examples","",f"Wire version {VERSION}; {resolve_profile(DEFAULT_PROFILE).baud} baud 4-FSK.","",
            "Each later refinement includes a complete grayscale thumbnail for late reception.","",
            "| Capture | Seconds received | Packets | Result |", "| --- | ---: | ---: | --- |"]
    rows += [f"| {r['name']} | {r['duration_seconds']} | {r['valid_packets']} | {r['color_stage']} |" for r in reports]
    rows += ["","Generated and decoded from synthetic WAVs; these are not over-the-air measurements.",""]
    (out_dir / "examples.md").write_text("\n".join(rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    demo = sub.add_parser("demo", help="generate a sent example and simulated receive captures")
    demo.add_argument("--out", type=Path, default=Path(__file__).with_name("examples"))
    enc = sub.add_parser("encode", help="encode a Pixel QSO JSON card to a 4-FSK WAV")
    enc.add_argument("card", type=Path); enc.add_argument("wav", type=Path)
    enc.add_argument("--profile", choices=[p.key for p in PROFILES], default=DEFAULT_PROFILE,
                     help="choose the modem bandwidth/duration profile (default: standard)")
    dec = sub.add_parser("decode", help="decode a prototype 16-bit mono WAV to a packet report")
    dec.add_argument("wav", type=Path); dec.add_argument("json", type=Path)
    dec.add_argument("--state", type=Path, help="accumulate soft packet evidence across receive sessions")
    dec.add_argument("--profile", choices=["auto"]+[p.key for p in PROFILES], default="auto",
                     help="receive profile, or auto-detect from CRC-valid frames")
    args = parser.parse_args()
    if args.cmd == "demo":
        run_demo(args.out)
    elif args.cmd == "encode":
        card = from_json(args.card); packets = make_packets(card); audio = synthesize(all_symbols(packets), profile=args.profile); write_wav(args.wav, audio)
        print(f"Wrote {args.wav}: one {len(packets)}-packet progressive card, {len(audio)/SAMPLE_RATE:.2f} s at {resolve_profile(args.profile).baud} baud")
    elif args.cmd == "decode":
        state_details = None
        if args.state:
            packets, errors, sr, state_details = decode_wav_with_state(args.wav, args.state, args.profile)
        else:
            packets, errors, sr = decode_wav(args.wav, args.profile)
        meta, image = reconstruct(packets)
        result = {"sample_rate":sr,"valid_packets":[{"type":p.packet_type,"card_id":p.card_id,"seq":p.seq,"corrected_bits":p.corrected_bits} for p in packets],"errors":errors,"card":meta,"pixels":image}
        if state_details:
            result["evidence_state"] = state_details
        args.json.write_text(json.dumps(result,indent=2)+"\n")
        print(f"Decoded {len(packets)} valid packets; {len(errors)} rejected packets; wrote {args.json}")

if __name__ == "__main__":
    main()

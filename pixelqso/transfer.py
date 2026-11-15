"""Portable PixelQSO PQI2 v1 framing and reassembly (standard library only)."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import struct
import zlib

MAGIC = b"PQI2"
VERSION = 1
# magic, version, reserved flags, card id, dimensions, palette, message, SNR,
# callsign, grid, byte offset, total packed bytes, whole-raster CRC32.
HEADER = struct.Struct(">4sBBHBBBBb12s8sIII8s")
MESSAGE_CODES = {"card": 0, "cq": 1, "exchange": 2, "73": 3}
MESSAGE_NAMES = {v: k for k, v in MESSAGE_CODES.items()}
PALETTE_BITS = {8: 0, 16: 1, 32: 2, 2: 3}
BITS_PER_PALETTE = (3, 4, 5, 1)
PALETTES = (
    ((0, 0, 0), (15, 15, 15), (0, 0, 15), (0, 15, 15),
     (0, 15, 0), (15, 15, 0), (15, 0, 0), (15, 0, 15)),
    tuple((r, g, b) for r in (0, 5, 10, 15) for g in (0, 15) for b in (0, 15)),
    tuple((r, g, b) for r in (0, 5, 10, 15) for g in (0, 5, 10, 15) for b in (0, 15)),
    ((0, 0, 0), (15, 15, 15)),
)


def fragment(card, packed: bytes, offset: int, chunk: bytes, message_type="card", snr_db=None) -> bytes:
    """Build one independently framed application fragment."""
    packed, chunk = bytes(packed), bytes(chunk)
    if message_type not in MESSAGE_CODES or not 0 <= offset < len(packed):
        raise ValueError("invalid card fragment identity or offset")
    if not chunk or offset + len(chunk) > len(packed):
        raise ValueError("fragment range is outside packed raster")
    if snr_db is not None and not -127 <= int(snr_db) <= 127:
        raise ValueError("SNR must be -127..127 dB")
    try:
        call = card.callsign.upper().encode("ascii")
        grid = card.grid.upper().encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("card identity must be ASCII") from exc
    if len(call) > 12 or len(grid) > 8:
        raise ValueError("callsign or grid is too long for the transfer header")
    return HEADER.pack(MAGIC, VERSION, 0, card.card_id & 0xffff, card.width,
                       card.height, PALETTE_BITS[len(card.palette)],
                       MESSAGE_CODES[message_type], -128 if snr_db is None else int(snr_db),
                       call.ljust(12, b"\0"), grid.ljust(8, b"\0"), offset,
                       len(packed), zlib.crc32(packed),
                       hashlib.blake2s(packed, digest_size=8).digest()) + chunk


@dataclass
class Assembly:
    metadata: tuple
    total: int
    crc32: int
    image_id: bytes | None = None
    data: bytearray = field(init=False)
    present: bytearray = field(init=False)
    checked_ranges: set = field(default_factory=set)

    def __post_init__(self):
        if not 1 <= self.total <= 65536:
            raise ValueError("invalid packed raster length")
        self.data = bytearray(self.total)
        self.present = bytearray(self.total)

    def add(self, offset: int, chunk: bytes) -> bool:
        chunk = bytes(chunk)
        if not chunk or offset < 0 or offset + len(chunk) > self.total:
            raise ValueError("fragment range is outside packed raster")
        # Validate the entire range before changing the cached image. A late
        # overlap conflict must not leave earlier bytes from this fragment in
        # an assembly that will continue receiving later copies.
        for i, value in enumerate(chunk, offset):
            if self.present[i] and self.data[i] != value:
                raise ValueError("conflicting fragment bytes for the same card identity")
        for i, value in enumerate(chunk, offset):
            self.data[i] = value
            self.present[i] = 1
        return self.exact

    @property
    def exact(self) -> bool:
        if not all(self.present) or zlib.crc32(self.data) != self.crc32:
            return False
        return (self.image_id is None or
                hashlib.blake2s(self.data, digest_size=8).digest() == self.image_id)

    @property
    def coverage(self) -> float:
        return sum(self.present) / self.total

    def pixels_and_coverage(self) -> tuple[list[int], list[bool]]:
        width, height, palette_id = self.metadata[1:4]
        bits = BITS_PER_PALETTE[palette_id]
        pixels = []
        coverage = []
        for index in range(width * height):
            bit_start = index * bits
            value = 0
            covered = True
            for bit in range(bits):
                absolute = bit_start + bit
                byte_index, within = divmod(absolute, 8)
                value = (value << 1) | ((self.data[byte_index] >> (7 - within)) & 1)
                covered &= bool(self.present[byte_index])
            pixels.append(value)
            coverage.append(covered)
        return pixels, coverage


class CardTransferReceiver:
    """Retain interleaved content assemblies; CRC gates verified promotion."""
    def __init__(self, max_assemblies=24):
        self.assemblies: OrderedDict[tuple, Assembly] = OrderedDict()
        self.max_assemblies = max(1, int(max_assemblies))

    def feed(self, sender: str, frame: bytes) -> tuple[Assembly, dict]:
        if len(frame) <= HEADER.size:
            raise ValueError("short PixelQSO transfer frame")
        (magic, version, flags, card_id, width, height, palette_id, kind, snr,
         call, grid, offset, total, crc, image_id) = HEADER.unpack_from(frame)
        if (magic != MAGIC or version != VERSION or flags != 0 or
                not 1 <= width <= 64 or not 1 <= height <= 64 or
                palette_id not in (0, 1, 2, 3) or kind not in MESSAGE_NAMES):
            raise ValueError("unsupported or malformed PixelQSO transfer header")
        bits = BITS_PER_PALETTE[palette_id]
        expected = (width * height * bits + 7) // 8
        if total != expected:
            raise ValueError("packed raster size does not match dimensions and palette")
        chunk_size = len(frame) - HEADER.size
        if not chunk_size or offset + chunk_size > total:
            raise ValueError("fragment range is outside packed raster")
        metadata = (card_id, width, height, palette_id, call, grid, crc, image_id)
        # Message stage and advertised SNR may change on a later CQ copy.
        key = (sender.upper(), None if image_id else card_id, width, height,
               palette_id, call, grid, image_id)
        assembly = self.assemblies.get(key)
        new_assembly = assembly is None
        if assembly is None:
            assembly = Assembly(metadata, total, crc, image_id)
        elif assembly.total != total or assembly.crc32 != crc:
            raise ValueError("conflicting raster metadata for card identity")
        # Assembly.add checks the full range and every overlap before it writes
        # bytes. Do not insert a new identity or refresh cache recency until that
        # validation succeeds, so rejected frames leave the receiver unchanged.
        assembly.add(offset, frame[HEADER.size:])
        assembly.checked_ranges.add((offset, chunk_size))
        if new_assembly:
            self.assemblies[key] = assembly
        else:
            self.assemblies.move_to_end(key)
        while len(self.assemblies) > self.max_assemblies:
            self.assemblies.popitem(last=False)
        pixels, coverage = assembly.pixels_and_coverage()
        return assembly, {"callsign": call.split(b"\0", 1)[0].decode("ascii"),
                          "grid": grid.split(b"\0", 1)[0].decode("ascii"),
                          "card_id": card_id, "width": width, "height": height,
                          "palette_id": palette_id, "message_type": MESSAGE_NAMES[kind],
                          "application_protocol_version": VERSION,
                          "image_crc32": crc, "image_id": image_id.hex(),
                          "whole_raster_crc32_valid": (all(assembly.present) and
                                                        zlib.crc32(assembly.data) == crc),
                          "content_tag_valid": (all(assembly.present) and
                                                hashlib.blake2s(assembly.data, digest_size=8).digest() == image_id),
                          "snr_db": None if snr == -128 else snr,
                          "avatar_burst": True, "exact": assembly.exact,
                          "bits_per_pixel": bits, "pixel_coverage": coverage,
                          "crc_blocks_passed": len(assembly.checked_ranges),
                          "crc_blocks_total": None,
                          "crc_block_kind": "transport_fragment",
                          # A KISS frame delivered by Data2G is already checked
                          # at the modem transport layer. Pixels fully present
                          # in those checked bytes are trusted regions even
                          # before the complete raster CRC/content tag passes.
                          "verified_pixel_coverage": coverage,
                          "pixels": pixels,
                          "color_stage": "verified" if assembly.exact else
                          f"{round(assembly.coverage * 100)}% received"}


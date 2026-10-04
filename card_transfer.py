"""Transport-independent, versioned card fragments for KISS or other links."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import OrderedDict
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
PALETTE_BITS = {8: 0, 16: 1, 32: 2}
BITS_PER_PALETTE = (3, 4, 5)


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
    data: bytearray = field(init=False)
    present: bytearray = field(init=False)

    def __post_init__(self):
        if not 1 <= self.total <= 65536:
            raise ValueError("invalid packed raster length")
        self.data = bytearray(self.total)
        self.present = bytearray(self.total)

    def add(self, offset: int, chunk: bytes) -> bool:
        chunk = bytes(chunk)
        if not chunk or offset < 0 or offset + len(chunk) > self.total:
            raise ValueError("fragment range is outside packed raster")
        for i, value in enumerate(chunk, offset):
            if self.present[i] and self.data[i] != value:
                raise ValueError("conflicting fragment bytes for the same card identity")
            self.data[i] = value
            self.present[i] = 1
        return self.exact

    @property
    def exact(self) -> bool:
        return all(self.present) and zlib.crc32(self.data) == self.crc32

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
                palette_id not in (0, 1, 2) or kind not in MESSAGE_NAMES):
            raise ValueError("unsupported or malformed PixelQSO transfer header")
        bits = BITS_PER_PALETTE[palette_id]
        expected = (width * height * bits + 7) // 8
        if total != expected:
            raise ValueError("packed raster size does not match dimensions and palette")
        metadata = (card_id, width, height, palette_id, call, grid, crc, image_id)
        # Message stage and advertised SNR may change on a later CQ copy.
        key = (sender.upper(), card_id, width, height, palette_id, call, grid, image_id)
        assembly = self.assemblies.get(key)
        if assembly is None:
            assembly = self.assemblies[key] = Assembly(metadata, total, crc)
        elif assembly.total != total:
            raise ValueError("conflicting total size for card identity")
        self.assemblies.move_to_end(key)
        assembly.add(offset, frame[HEADER.size:])
        while len(self.assemblies) > self.max_assemblies:
            self.assemblies.popitem(last=False)
        pixels, coverage = assembly.pixels_and_coverage()
        return assembly, {"callsign": call.split(b"\0", 1)[0].decode("ascii"),
                          "grid": grid.split(b"\0", 1)[0].decode("ascii"),
                          "card_id": card_id, "width": width, "height": height,
                          "palette_id": palette_id, "message_type": MESSAGE_NAMES[kind],
                          "image_crc32": crc, "image_id": image_id.hex(),
                          "snr_db": None if snr == -128 else snr,
                          "avatar_burst": True, "exact": assembly.exact,
                          "bits_per_pixel": bits, "pixel_coverage": coverage,
                          "pixels": pixels,
                          "color_stage": "verified" if assembly.exact else
                          f"{round(assembly.coverage * 100)}% received"}

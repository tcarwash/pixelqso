"""Audio backend boundary for PixelQSO card bursts.

A backend accepts the same card and returns 48 kHz audio or a common card
report. Optional modem dependencies are imported only when selected.
"""
from __future__ import annotations

import math
import importlib.util
import struct
from dataclasses import dataclass
from typing import Protocol

import numpy as np
import cardmodem as pixel

AUDIO_RATE = pixel.SAMPLE_RATE


class CardBackend(Protocol):
    key: str
    label: str
    checked: bool
    def encode(self, card: pixel.Card, repeats: int, message_type: str, snr_db: int | None) -> np.ndarray: ...
    def estimate_seconds(self, card: pixel.Card, repeats: int) -> float: ...
    def decode(self, audio: np.ndarray, sample_rate: int) -> dict: ...
    def minimum_audio_seconds(self) -> float: ...


def _report(pixels: list[int], decoded: dict, sample_rate: int, profile: str) -> dict:
    card = {**decoded, "palette": decoded.get("palette") or
            [list(color) for color in pixel.MINIMAL_AVATAR_PALETTE], "preview_received": True}
    return {"sample_rate": sample_rate, "valid_packets": [], "fresh_packets": [],
            "receive_profile": profile, "errors": [], "card": card, "pixels": pixels}


@dataclass(frozen=True)
class AvatarBackend:
    key: str
    label: str
    checked: bool

    def encode(self, card, repeats, message_type, snr_db):
        cycle = (pixel.minimal_avatar_resilient_cycle_symbols(card, message_type=message_type, snr_db=snr_db)
                 if self.checked else pixel.minimal_avatar_symbols(card, message_type=message_type, snr_db=snr_db))
        return pixel.synthesize(pixel.repeat_symbol_stream(cycle, repeats), profile=pixel.MINIMAL_AVATAR_PROFILE)

    def estimate_seconds(self, card, repeats):
        duration = (pixel.minimal_avatar_resilient_duration(card=card) if self.checked
                    else pixel.minimal_avatar_duration(card=card))
        return repeats * duration

    def decode(self, audio, sample_rate):
        pixels, decoded = pixel.decode_minimal_avatar_audio_auto(audio, sample_rate)
        return _report(pixels, decoded, sample_rate, pixel.MINIMAL_AVATAR_PROFILE.key)

    def minimum_audio_seconds(self):
        symbols = len(pixel.FRAME_SYNC) + pixel.AVATAR_META_TONE_COUNT + (132 if self.checked else 32)
        return symbols / pixel.MINIMAL_AVATAR_PROFILE.baud


# One independently CRC-checked Data2G codeword carries one card fragment.
# Header: magic, version, card id, dimensions, palette, message, SNR,
# identity, fragment index/count and CRC of the whole packed raster.
FRAGMENT = struct.Struct(">4sBHBBBBb12s8sBBH")
FRAGMENT_MAGIC = b"PQD2"
FRAGMENT_VERSION = 1
MESSAGE_CODES = {"card": 0, "cq": 1, "exchange": 2, "73": 3}
MESSAGE_NAMES = {v: k for k, v in MESSAGE_CODES.items()}


def _data2g():
    try:
        from data2g import codes, config, modem
        from scipy.signal import resample_poly
    except ImportError as exc:
        raise RuntimeError("Data2G backend needs the optional extra: uv sync --extra data2g") from exc
    return codes, config, modem, resample_poly


def _indices(packed: bytes, count: int, bits_per_pixel: int) -> list[int]:
    bits = [int(byte >> shift) & 1 for byte in packed for shift in range(7, -1, -1)]
    return [sum(bits[p * bits_per_pixel + j] << (bits_per_pixel - 1 - j)
                for j in range(bits_per_pixel)) for p in range(count)]


@dataclass(frozen=True)
class Data2GBackend:
    key: str
    label: str
    submode: str
    checked: bool = True

    def _layout(self, card):
        codes, config, _modem, _resample = _data2g()
        spec = config.SUBMODES[self.submode]
        capacity = codes.payload_bytes(spec) - FRAGMENT.size
        if capacity <= 0:
            raise ValueError(f"Data2G {self.submode} cannot fit a card fragment header")
        packed = pixel.minimal_avatar_payload(card)
        count = math.ceil(len(packed) / capacity)
        if not 1 <= count <= 255 or count > config.MAX_CODEWORDS:
            raise ValueError(f"Card needs {count} Data2G codewords; choose a larger submode or canvas")
        return spec, packed, capacity, count, codes.payload_bytes(spec)

    def _payloads(self, card, message_type, snr_db):
        spec, packed, capacity, count, wire_size = self._layout(card)
        try:
            call = card.callsign.upper().encode("ascii")
            grid = card.grid.upper().encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Data2G card identity must be ASCII") from exc
        if message_type not in MESSAGE_CODES:
            raise ValueError(f"Unsupported card stage {message_type!r}")
        if snr_db is not None and not -127 <= int(snr_db) <= 127:
            raise ValueError("SNR must be -127..127 dB")
        image_crc = pixel.crc16(packed)
        payloads = []
        for index in range(count):
            piece = packed[index * capacity:(index + 1) * capacity]
            header = FRAGMENT.pack(FRAGMENT_MAGIC, FRAGMENT_VERSION, card.card_id & 0xffff,
                                   card.width, card.height, {8: 0, 16: 1, 32: 2}[len(card.palette)],
                                   MESSAGE_CODES[message_type], -128 if snr_db is None else int(snr_db),
                                   call.ljust(12, b"\0"), grid.ljust(8, b"\0"), index, count, image_crc)
            payloads.append((header + piece).ljust(wire_size, b"\0"))
        return spec, payloads

    def encode(self, card, repeats, message_type, snr_db):
        _codes, config, dmodem, resample_poly = _data2g()
        if not 1 <= repeats <= 20:
            raise ValueError("Copies must be 1..20")
        spec, payloads = self._payloads(card, message_type, snr_db)
        cycle = dmodem.modulate(payloads, spec)
        # Qt's radio/test audio path is 48 kHz; Data2G's waveform is 8 kHz.
        return resample_poly(np.tile(cycle, repeats), AUDIO_RATE, config.FS).astype(np.float32)

    def estimate_seconds(self, card, repeats):
        _codes, _config, dmodem, _resample = _data2g()
        _spec, _packed, _capacity, count, _wire_size = self._layout(card)
        return repeats * dmodem.burst_seconds(self.submode_spec(), count)

    def submode_spec(self):
        _codes, config, _dmodem, _resample = _data2g()
        return config.SUBMODES[self.submode]

    def minimum_audio_seconds(self):
        _codes, config, dmodem, _resample = _data2g()
        return dmodem.burst_seconds(config.SUBMODES[self.submode], 1)

    def decode(self, audio, sample_rate):
        codes, config, dmodem, resample_poly = _data2g()
        from data2g import tnc
        source = np.asarray(audio, dtype=np.float32)
        if sample_rate != config.FS:
            divisor = math.gcd(sample_rate, config.FS)
            source = resample_poly(source, config.FS // divisor, sample_rate // divisor).astype(np.float32)
        groups = {}
        copies = 0
        choices = [item.submode for item in BACKENDS.values() if isinstance(item, Data2GBackend)]
        receiver = tnc.Receiver(dmodem.Accept.of(choices), cpm_grids=(), blank=False)
        for offset in range(0, len(source), config.FS // 2):
            events = receiver.feed(source[offset:offset + config.FS // 2])
            for event_type, event in events:
                if event_type != "burst" or event["rx"] is None:
                    continue
                burst = dmodem.decode_received(event["rx"])
                spec = burst.submode
                if spec.name not in choices:
                    continue
                capacity = codes.payload_bytes(spec) - FRAGMENT.size
                if capacity <= 0:
                    continue
                copies += 1
                for payload, good in zip(burst.payloads, burst.crc_ok):
                    if not good or len(payload) < FRAGMENT.size:
                        continue
                    try:
                        magic, version, card_id, width, height, palette_id, kind, snr, call, grid, index, count, image_crc = FRAGMENT.unpack_from(payload)
                    except struct.error:
                        continue
                    if (magic != FRAGMENT_MAGIC or version != FRAGMENT_VERSION or
                        not 1 <= width <= 64 or not 1 <= height <= 64 or
                        palette_id >= len(pixel.AVATAR_PALETTES) or kind not in MESSAGE_NAMES or
                        not 1 <= count <= config.MAX_CODEWORDS or index >= count):
                        continue
                    bits = pixel.AVATAR_PALETTE_BITS[palette_id]
                    total_bytes = (width * height * bits + 7) // 8
                    if count != math.ceil(total_bytes / capacity):
                        continue
                    key = (card_id, width, height, palette_id, kind, snr, call, grid, image_crc, spec.name)
                    group = groups.setdefault(key, {"fragments": {}, "capacity": capacity, "count": count,
                                                    "measured_snr_db": round(burst.snr_db)})
                    start = index * capacity
                    piece = payload[FRAGMENT.size:FRAGMENT.size + min(capacity, total_bytes - start)]
                    group["fragments"][index] = piece
                # A complete burst is needed before its interleaved codewords decode.
        if not groups:
            raise ValueError("No checked Data2G card fragment received yet")
        key, group = max(groups.items(), key=lambda pair: len(pair[1]["fragments"]))
        card_id, width, height, palette_id, kind, snr, call, grid, image_crc, submode = key
        bits = pixel.AVATAR_PALETTE_BITS[palette_id]
        total_bytes = (width * height * bits + 7) // 8
        packed = bytearray(total_bytes)
        present_bytes = [False] * total_bytes
        received = [False] * group["count"]
        for index, piece in group["fragments"].items():
            start = index * group["capacity"]
            packed[start:start + len(piece)] = piece
            present_bytes[start:start + len(piece)] = [True] * len(piece)
            received[index] = True
        pixels = _indices(packed, width * height, bits)
        coverage = [all(present_bytes[j] for j in range((i * bits) // 8, ((i + 1) * bits + 7) // 8))
                    for i in range(width * height)]
        exact = all(received) and pixel.crc16(packed) == image_crc
        callsign = call.split(b"\0", 1)[0].decode("ascii", "replace")
        locator = grid.split(b"\0", 1)[0].decode("ascii", "replace")
        metadata = {"callsign": callsign, "grid": locator, "card_id": card_id,
                    "width": width, "height": height, "palette_id": palette_id,
                    "palette": [list(color) for color in pixel.AVATAR_PALETTES[palette_id]],
                    "message_type": MESSAGE_NAMES[kind], "snr_db": None if snr == -128 else snr,
                    "measured_snr_db": max(-127, min(127, group["measured_snr_db"])),
                    "avatar_mode": self.key, "avatar_burst": True, "exact": exact,
                    "received_blocks": received, "pixel_coverage": coverage,
                    "received_copies": copies, "color_stage": f"{sum(received)}/{len(received)} checked fragments"}
        return _report(pixels, metadata, sample_rate, f"data2g:{submode}")


@dataclass(frozen=True)
class WeakSignalBackend:
    key: str = "experimental_qpsk_5s"
    label: str = "Weak signal · 32×32 · experimental"
    checked: bool = True
    max_repeats: int = 1

    def encode(self, card, repeats, message_type, snr_db):
        import weak_signal_modem as weak
        return weak.encode(card, repeats, message_type, snr_db)

    def estimate_seconds(self, card, repeats):
        import weak_signal_modem as weak
        weak.packet(card)
        if repeats != 1:
            raise ValueError("Experimental five-second mode supports one burst")
        return weak.seconds()

    def minimum_audio_seconds(self):
        import weak_signal_modem as weak
        return weak.seconds() - .2

    def decode(self, audio, sample_rate):
        import weak_signal_modem as weak
        pixels, metadata = weak.decode(audio, sample_rate)
        return _report(pixels, metadata, sample_rate, self.key)


@dataclass(frozen=True)
class WeakSignalCombinedBackend(WeakSignalBackend):
    key: str = "experimental_qpsk_combined"
    label: str = "Weak signal · combined copies · experimental"
    max_repeats: int = 8

    def encode(self, card, repeats, message_type, snr_db):
        import weak_signal_modem as weak
        if not 1 <= repeats <= self.max_repeats:
            raise ValueError("Combined weak-signal mode supports 1..8 copies")
        return np.tile(weak.encode(card, 1, message_type, snr_db), repeats)

    def estimate_seconds(self, card, repeats):
        import weak_signal_modem as weak
        weak.packet(card)
        if not 1 <= repeats <= self.max_repeats:
            raise ValueError("Combined weak-signal mode supports 1..8 copies")
        return repeats * weak.seconds()


BACKENDS: dict[str, CardBackend] = {
    "experimental_qpsk_5s": WeakSignalBackend(),
    "experimental_qpsk_combined": WeakSignalCombinedBackend(),
    "fast_avatar": AvatarBackend("fast_avatar", "Fast · unverified", False),
    "fast_avatar_fec": AvatarBackend("fast_avatar_fec", "Resilient", True),
    "data2g_1200_robust": Data2GBackend("data2g_1200_robust", "Data2G 1.2 kHz · robust · experimental", "qpsk-r1/2"),
    "data2g_1200_fast": Data2GBackend("data2g_1200_fast", "Data2G 1.2 kHz · fast · experimental", "16qam-r1/2"),
}


def get_backend(key: str) -> CardBackend:
    return BACKENDS[key]


def is_card_backend(key: str) -> bool:
    return key in BACKENDS


def is_available(key: str) -> bool:
    if key in {"experimental_qpsk_5s", "experimental_qpsk_combined"}:
        return (importlib.util.find_spec("data2g") is not None
                and importlib.util.find_spec("scipy") is not None)
    return not key.startswith("data2g_") or (importlib.util.find_spec("data2g") is not None
                                             and importlib.util.find_spec("torch") is not None)

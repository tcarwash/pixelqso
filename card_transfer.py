"""Transport-independent, versioned card fragments for KISS or other links."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections import OrderedDict
import copy
import hashlib
import struct
import zlib
from types import MappingProxyType
from collections.abc import Mapping

import cardmodem

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
    image_id: bytes | None = None
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
                palette_id not in (0, 1, 2) or kind not in MESSAGE_NAMES):
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
                          # A KISS frame delivered by Data2G is already checked
                          # at the modem transport layer. Pixels fully present
                          # in those checked bytes are trusted regions even
                          # before the complete raster CRC/content tag passes.
                          "verified_pixel_coverage": coverage,
                          "pixels": pixels,
                          "color_stage": "verified" if assembly.exact else
                          f"{round(assembly.coverage * 100)}% received"}


class CardPreviewAssembler:
    """Qt-independent bounded accumulator for candidate and checked image data."""

    def __init__(self, max_images=12):
        self.states = {}
        self.current_key = None
        self.current_state = None
        self.sequence = 0
        self.window = 0
        self.max_images = max(1, int(max_images))

    def begin_window(self):
        self.window += 1

    def clear_current(self):
        self.current_key = None
        self.current_state = None

    def snapshot(self):
        state = self.current_state
        if state is None:
            return None
        return PreviewSnapshot(
            key=state["key"],
            card=_freeze(state["card"]),
            pixels=tuple(state["pixels"]),
            coverage=tuple(state["coverage"]),
            verified_pixels=tuple(state["verified_pixels"]),
        )

    def merge(self, report):
        """Keep the best accumulated pixels for one burst identity in the live view."""
        incoming_card = report.get("card") or {}
        if not (incoming_card.get("raw_avatar") or incoming_card.get("avatar_burst")):
            return copy.deepcopy(report), True
        width, height = int(incoming_card.get("width", 32)), int(incoming_card.get("height", 32))
        count = width * height
        mode = incoming_card.get("avatar_mode", "raw")
        protocol_version = incoming_card.get(
            "header_version", incoming_card.get("application_protocol_version", "legacy"))
        palette = tuple(tuple(rgb) for rgb in incoming_card.get("palette", cardmodem.MINIMAL_AVATAR_PALETTE))
        content_id = incoming_card.get("image_id", incoming_card.get("image_crc32"))
        key = (str(incoming_card.get("callsign", "")).upper(),
               str(incoming_card.get("grid", "")).upper(), protocol_version,
               incoming_card.get("card_id") if content_id is None else None,
               width, height, palette, mode if content_id is None else None,
               content_id)
        state = self.states.get(key)
        reset_state = state is None
        incoming_checksums = incoming_card.get("block_checksums")
        if not reset_state and incoming_checksums:
            old_checksums = state.get("block_checksums", [])
            if len(old_checksums) != len(incoming_checksums) or any(
                    old is not None and new is not None and old != new
                    for old, new in zip(old_checksums, incoming_checksums)):
                raise ValueError("conflicting checked-block checksums for the same image identity")
        changed = reset_state or self.current_key != key
        if reset_state:
            state = {"key": key, "pixels": [0] * count, "coverage": [False] * count,
                     "verified_pixels": [False] * count,
                     "pixel_votes": [[0] * len(palette) for _ in range(count)],
                     "received_blocks": [False] * len(incoming_card.get("received_blocks") or []),
                     "block_checksums": list(incoming_checksums or []),
                     "copies": 0, "window_id": self.window, "card": {}}

        pixels = report.get("pixels") or []
        coverage = incoming_card.get("pixel_coverage")
        if coverage is None:
            coverage = [True] * min(count, len(pixels))
        copies = int(incoming_card.get("received_copies", 0) or 0)
        acquisition_start = (incoming_card.get("acquisition") or {}).get("start_sample")
        more_evidence = (self.window > state["window_id"] or copies > state["copies"] or
                         (acquisition_start is not None and acquisition_start != state.get("last_start_sample")))
        incoming_verified = [False] * count
        if incoming_card.get("exact"):
            for index in range(min(count, len(coverage))):
                incoming_verified[index] = bool(coverage[index])
        elif incoming_card.get("verified_pixel_coverage") is not None:
            verified_coverage = incoming_card["verified_pixel_coverage"]
            for index in range(min(count, len(coverage), len(verified_coverage))):
                incoming_verified[index] = bool(verified_coverage[index])
        blocks = incoming_card.get("received_blocks")
        if blocks is not None:
            pixels_per_block = max(1, (61 * 6) // int(incoming_card.get("bits_per_pixel", 3)))
            for block_index, verified in enumerate(blocks):
                if verified:
                    begin = block_index * pixels_per_block
                    incoming_verified[begin:min(count, begin + pixels_per_block)] = [True] * max(0, min(count, begin + pixels_per_block) - begin)
        for index in range(min(count, len(pixels), len(coverage))):
            if (incoming_verified[index] and state["verified_pixels"][index] and
                    coverage[index] and state["pixels"][index] != pixels[index]):
                raise ValueError("conflicting verified pixels for the same image identity")
        # Only commit identity, recency, and image data after the entire incoming
        # checked region has passed overlap validation.
        if reset_state:
            self.states[key] = state
        self.current_state = state
        self.current_key = key
        self.sequence += 1
        state["last_seen_order"] = self.sequence
        if len(self.states) > self.max_images:
            stale = min((candidate for candidate in self.states.items()
                         if candidate[0] != key), key=lambda candidate: candidate[1].get("last_seen_order", 0),
                        default=None)
            if stale is not None:
                del self.states[stale[0]]
        for index in range(min(count, len(pixels), len(coverage))):
            if not coverage[index]:
                continue
            if incoming_verified[index] and not state["verified_pixels"][index]:
                state["pixels"][index] = pixels[index]
                state["coverage"][index] = True
                state["verified_pixels"][index] = True
                changed = True
            elif incoming_verified[index]:
                # Checked block payload is authoritative even if already verified.
                if state["pixels"][index] != pixels[index]:
                    state["pixels"][index] = pixels[index]
                    changed = True
                state["verified_pixels"][index] = True
            elif (not state["verified_pixels"][index] and
                  (more_evidence or not state["coverage"][index])):
                value = int(pixels[index])
                if 0 <= value < len(state["pixel_votes"][index]):
                    weight = max(1, copies - state["copies"]) if more_evidence else 1
                    state["pixel_votes"][index][value] += weight
                    previous = state["pixels"][index]
                    best_votes = max(state["pixel_votes"][index])
                    # Keep the existing candidate on ties; change only when new
                    # evidence makes another palette value more likely.
                    if (not state["coverage"][index] or
                            state["pixel_votes"][index][value] == best_votes and
                            state["pixel_votes"][index][value] > state["pixel_votes"][index][previous]):
                        state["pixels"][index] = value
                    elif best_votes > state["pixel_votes"][index][previous]:
                        state["pixels"][index] = state["pixel_votes"][index].index(best_votes)
                    state["coverage"][index] = True
                    changed = True

        if blocks is not None:
            if len(state["received_blocks"]) != len(blocks):
                state["received_blocks"] = [False] * len(blocks)
            for index, verified in enumerate(blocks):
                if verified and not state["received_blocks"][index]:
                    state["received_blocks"][index] = True
                    changed = True
            if incoming_checksums:
                if len(state["block_checksums"]) != len(incoming_checksums):
                    state["block_checksums"] = [None] * len(incoming_checksums)
                for index, checksum in enumerate(incoming_checksums):
                    if checksum is not None:
                        state["block_checksums"][index] = checksum
        if copies > state["copies"]:
            state["copies"] = copies
            changed = True
        if acquisition_start is not None:
            state["last_start_sample"] = acquisition_start
        state["window_id"] = self.window
        state["card"].update(incoming_card)
        merged_card = {**state["card"], "pixel_coverage": state["coverage"],
                       "verified_pixel_coverage": state["verified_pixels"],
                       "received_blocks": state["received_blocks"] if blocks is not None else None,
                       "received_copies": state["copies"]}
        if blocks is not None:
            checked = sum(state["received_blocks"])
            merged_card["color_stage"] = (f"{checked}/{len(state['received_blocks'])} CRC-verified blocks · "
                                           f"{sum(state['coverage'])}/{count} preview pixels")
            merged_card["exact"] = bool(state["received_blocks"]) and all(state["received_blocks"])
            merged_card["corrected_blocks"] = max(int(state["card"].get("corrected_blocks", 0) or 0),
                                                   int(incoming_card.get("corrected_blocks", 0) or 0))
            state["card"]["exact"] = merged_card["exact"]
        else:
            merged_card["color_stage"] = (f"{sum(state['coverage'])}/{count} pixels · "
                                           f"{state['copies']} copies · unverified")
            if incoming_card.get("exact") and all(state["coverage"]):
                merged_card["exact"] = True
                merged_card["color_stage"] = f"{count}/{count} pixels · whole-card CRC verified"
        state["card"]["pixel_coverage"] = state["coverage"]
        merged_report = {**report, "card": merged_card, "pixels": state["pixels"]}
        return copy.deepcopy(merged_report), changed


class CardAssemblyService:
    """Application-session owner for transport reassembly and live previews."""

    def __init__(self, *, max_transport_assemblies=24, max_preview_images=12):
        self.transport = CardTransferReceiver(max_assemblies=max_transport_assemblies)
        self.previews = CardPreviewAssembler(max_images=max_preview_images)

    def feed_transport_frame(self, sender: str, frame: bytes):
        return self.transport.feed(sender, frame)

    def merge_preview(self, report):
        incoming = copy.deepcopy(report)
        incoming_card = incoming.get("card") or {}
        if incoming_card:
            evidence = self.assess(incoming)
            incoming_card["exact"] = evidence.verified_complete
        snapshot, changed = self.previews.merge(incoming)
        card = snapshot.get("card") or {}
        decision = self.assess(snapshot)
        if card:
            card["exact"] = decision.verified_complete
            card["verification_scope"] = decision.scope
        identity = (self.previews.current_key if
                    card.get("raw_avatar") or card.get("avatar_burst") else None)
        if identity is None and card:
            identity = (str(card.get("callsign", "")).upper(),
                        str(card.get("grid", "")).upper(),
                        card.get("image_id", card.get("card_id")))
        snapshot["assembly_event"] = asdict(AssemblyEvent(
            identity=repr(identity) if identity is not None else None,
            verified_complete=decision.verified_complete,
            verification_scope=decision.scope,
            candidate_pixels=decision.candidate_pixels,
            verified_pixels=decision.verified_pixels,
        ))
        return snapshot, changed

    @staticmethod
    def assess(report):
        """Apply backend-specific evidence rules without trusting preview coverage."""
        card = report.get("card") or {}
        coverage = card.get("pixel_coverage") or []
        candidate_pixels = sum(bool(value) for value in coverage)
        if card.get("avatar_burst"):
            blocks = card.get("received_blocks")
            if card.get("header_version") == 4:
                verified = (bool(blocks) and all(bool(value) for value in blocks) and
                            bool(card.get("whole_raster_valid")))
                scope = "whole_raster_crc32_and_content_tag"
            elif blocks is not None:
                verified = bool(blocks) and all(bool(value) for value in blocks)
                scope = "all_crc_checked_blocks"
            elif card.get("image_crc32") is not None:
                if card.get("image_id") is not None:
                    verified = (bool(card.get("exact")) and bool(coverage) and all(coverage) and
                                bool(card.get("whole_raster_crc32_valid")) and
                                bool(card.get("content_tag_valid")))
                    scope = ("whole_raster_crc32_and_content_tag" if verified or not any(
                        card.get("verified_pixel_coverage") or []) else
                        "checked_data2g_fragment_regions")
                else:
                    verified = bool(card.get("exact")) and bool(coverage) and all(coverage)
                    scope = "whole_raster_crc32"
            else:
                verified = False
                scope = "candidate_pixels_only"
        else:
            verified = bool(card.get("exact"))
            scope = "backend_card_integrity" if verified else "candidate_or_partial"
        verified_pixels = (candidate_pixels if verified else
                           sum(bool(value) for value in (card.get("verified_pixel_coverage") or [])))
        return VerificationDecision(verified, scope, candidate_pixels, verified_pixels)

    def begin_receive_window(self):
        self.previews.begin_window()

    def clear_current_preview(self):
        self.previews.clear_current()

    def preview_snapshot(self):
        snapshot = self.previews.snapshot()
        if snapshot is None:
            return None
        card = dict(snapshot.card)
        decision = self.assess({"card": card})
        card["exact"] = decision.verified_complete
        card["verification_scope"] = decision.scope
        return PreviewSnapshot(snapshot.key, _freeze(card), snapshot.pixels,
                               snapshot.coverage, snapshot.verified_pixels)


@dataclass(frozen=True)
class PreviewSnapshot:
    key: tuple
    card: Mapping
    pixels: tuple[int, ...]
    coverage: tuple[bool, ...]
    verified_pixels: tuple[bool, ...]


@dataclass(frozen=True)
class VerificationDecision:
    verified_complete: bool
    scope: str
    candidate_pixels: int
    verified_pixels: int


@dataclass(frozen=True)
class AssemblyEvent:
    """Service-owned completion evidence attached to every merged report."""
    identity: str | None
    verified_complete: bool
    verification_scope: str
    candidate_pixels: int
    verified_pixels: int


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value

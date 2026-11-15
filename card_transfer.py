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

from pixelqso.transfer import (
    MAGIC, VERSION, HEADER, MESSAGE_CODES, MESSAGE_NAMES, PALETTE_BITS,
    BITS_PER_PALETTE, fragment, Assembly, CardTransferReceiver,
)


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
               incoming_card.get("card_id"),
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
        previous_stage = (state.get("card", {}).get("message_type")
                          if state is not None else None)
        changed = (reset_state or self.current_key != key or
                   incoming_card.get("animation") != (state.get("card", {}).get("animation") if state else None) or
                   incoming_card.get("reception_report") != (state.get("card", {}).get("reception_report") if state else None) or
                   incoming_card.get("crc_blocks_passed") != (state.get("card", {}).get("crc_blocks_passed") if state else None) or
                   (incoming_card.get("message_type") is not None and
                    incoming_card.get("message_type") != previous_stage))
        diagnostics = incoming_card.get("diagnostics") or {}
        acquisition_start = diagnostics.get("acquisition_start_sample")
        candidate_revision = (
            diagnostics.get("receive_session_id", 0),
            tuple(diagnostics.get("copy_revisions") or ()), acquisition_start,
            int(incoming_card.get("received_copies", 0) or 0),
            tuple(bool(value) for value in (incoming_card.get("pixel_coverage") or [])),
            tuple(report.get("pixels") or ()),
        )
        if reset_state:
            state = {"key": key, "pixels": [0] * count, "coverage": [False] * count,
                     "verified_pixels": [False] * count,
                     "received_blocks": [False] * len(incoming_card.get("received_blocks") or []),
                     "block_checksums": list(incoming_checksums or []),
                     "copies": 0, "window_id": self.window, "card": {},
                     "candidate_revision": None, "integrity": {}}

        pixels = report.get("pixels") or []
        coverage = incoming_card.get("pixel_coverage")
        if coverage is None:
            coverage = [True] * min(count, len(pixels))
        copies = int(incoming_card.get("received_copies", 0) or 0)
        new_candidate_revision = candidate_revision != state["candidate_revision"]
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
            bits_per_pixel = max(1, int(incoming_card.get("bits_per_pixel", 3)))
            data_symbols_per_block = cardmodem.AVATAR_RS_K
            if int(incoming_card.get("header_version", 3)) == 4:
                data_symbols_per_block -= cardmodem.AVATAR_V4_BLOCK_OVERHEAD
            pixels_per_block = max(1, (data_symbols_per_block * 6) // bits_per_pixel)
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
            elif not state["verified_pixels"][index]:
                value = int(pixels[index])
                if not 0 <= value < len(palette):
                    continue
                previous = state["pixels"][index]
                was_covered = state["coverage"][index]
                if new_candidate_revision or not was_covered:
                    state["pixels"][index] = value
                state["coverage"][index] = True
                changed = changed or not was_covered or state["pixels"][index] != previous

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
        if new_candidate_revision:
            state["candidate_revision"] = candidate_revision
        state["window_id"] = self.window
        state["card"].update(incoming_card)
        # Integrity claims are monotonic for this immutable content key. A
        # later partial snapshot may omit or clear a flag, but cannot revoke a
        # previously checked whole-raster result.
        for name in ("whole_raster_valid", "whole_raster_crc32_valid",
                     "content_tag_valid", "exact"):
            if incoming_card.get(name):
                state["integrity"][name] = True
            elif name not in state["integrity"]:
                state["integrity"][name] = False
            state["card"][name] = state["integrity"][name]
        merged_card = {**state["card"], "pixel_coverage": state["coverage"],
                       "verified_pixel_coverage": state["verified_pixels"],
                       "pixels_received": sum(bool(value) for value in state["coverage"]),
                       "pixels_verified": sum(bool(value) for value in state["verified_pixels"]),
                       "block_checksums": state["block_checksums"],
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
        from card_animation import LinkCache
        self.animation_links = LinkCache()
        from card_feedback import FeedbackCache
        self.feedback = FeedbackCache()
        self.transport = CardTransferReceiver(max_assemblies=max_transport_assemblies)
        self.previews = CardPreviewAssembler(max_images=max_preview_images)

    def feed_transport_frame(self, sender: str, frame: bytes):
        from card_feedback import MAGIC as FEEDBACK_MAGIC
        if frame.startswith(FEEDBACK_MAGIC):
            self.feedback.add(frame)
            return None, {"reception_metadata_only": True}
        from card_animation import MAGIC, decode_link
        if frame.startswith(MAGIC):
            self.animation_links.add(*decode_link(frame))
            return None, {"animation_metadata_only": True}
        return self.transport.feed(sender, frame)

    def merge_preview(self, report):
        incoming = copy.deepcopy(report)
        incoming_card = incoming.get("card") or {}
        if incoming_card:
            evidence = self.assess(incoming)
            incoming_card["exact"] = evidence.verified_complete
            self.animation_links.attach(incoming)
            self.feedback.attach(incoming)
        snapshot, changed = self.previews.merge(incoming)
        card = snapshot.get("card") or {}
        decision = self.assess(snapshot)
        if card:
            card["exact"] = decision.verified_complete
            card["verification_scope"] = decision.scope
            self.animation_links.attach(snapshot)
            self.feedback.attach(snapshot)
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

    def discard_current_preview(self):
        """Forget the active provisional card so its next copy starts clean."""
        snapshot = self.previews.snapshot()
        if snapshot is not None:
            card = snapshot.card
            call = str(card.get("callsign", "")).upper().encode("ascii", "ignore")
            grid = str(card.get("grid", "")).upper().encode("ascii", "ignore")
            image_id = card.get("image_id")
            try:
                image_id = bytes.fromhex(image_id) if isinstance(image_id, str) else image_id
            except ValueError:
                image_id = None
            for key in list(self.transport.assemblies):
                if (len(key) >= 8 and (not call or key[5] == call.ljust(12, b"\0")) and
                        (not grid or key[6] == grid.ljust(8, b"\0")) and
                        (image_id is None or key[7] == image_id)):
                    del self.transport.assemblies[key]
            self.previews.states.pop(snapshot.key, None)
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

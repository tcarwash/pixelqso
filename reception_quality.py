"""Display-only receive confidence; never changes decoded pixels or CRC verdicts.

A full-looking picture is not necessarily a verified picture. We keep coverage,
CRC-checked regions and whole-card integrity as three separate facts.
"""
from __future__ import annotations
from dataclasses import dataclass


def _mask(coverage, count):
    if not isinstance(coverage, (list, tuple)):
        return None
    # Some older sources omit the mask or report a truncated mask. Missing
    # positions are not evidence of reception or successful verification.
    return tuple(bool(coverage[i]) if i < len(coverage) else False
                 for i in range(count))


@dataclass(frozen=True)
class ReceptionQuality:
    total_pixels: int
    whole_verified: bool
    heard: tuple[bool, ...] | None
    checked: tuple[bool, ...] | None

    @property
    def heard_percent(self):
        if self.whole_verified:
            return 100
        if self.heard is None:
            return None
        return round(100 * sum(self.heard) / self.total_pixels)

    @property
    def checked_percent(self):
        if self.whole_verified:
            return 100
        if self.checked is None:
            return None
        return round(100 * sum(self.checked) / self.total_pixels)

    @property
    def caption(self):
        if self.whole_verified:
            return "✓ VERIFIED · 100%"
        if self.heard_percent is None:
            return "◌ RAW PREVIEW · NOT VERIFIED"
        checked = (f"{self.checked_percent}% CRC" if self.checked_percent is not None
                   else "CRC pending")
        return f"◌ PREVIEW · {self.heard_percent}% heard · {checked}"

    @property
    def detail(self):
        if self.whole_verified:
            return "Whole card integrity verified. Displayed pixels are confirmed."
        heard = (f"{self.heard_percent}% heard" if self.heard_percent is not None
                 else "Heard pixel coverage not reported")
        checked = (f"{self.checked_percent}% CRC-verified regions"
                   if self.checked_percent is not None else
                   "No verified-region coverage reported")
        return (f"{heard} · {checked}. This is the raw decoded picture: visible "
                "pixels may be incorrect until CRC verification succeeds.")


def reception_quality(card, *, verified_complete=False):
    width = max(1, int(card.get("width") or 32))
    height = max(1, int(card.get("height") or 32))
    total = width * height
    return ReceptionQuality(
        total, bool(verified_complete),
        _mask(card.get("pixel_coverage"), total),
        _mask(card.get("verified_pixel_coverage"), total))


def archivable_partial(report, threshold_percent=25):
    """Only archive identifiable pictures with sufficient raw pixel coverage.

    Unknown/missing coverage is not zero, but it cannot meet a numerical
    threshold. CRC proof is deliberately *not* required for a provisional save.
    Whole-card verified reports are handled by the application's existing
    verified receive path and do not depend on this threshold.
    """
    card = (report or {}).get("card") or {}
    pixels = (report or {}).get("pixels")
    if pixels is None or not hasattr(pixels, "__len__") or len(pixels) == 0:
        return False
    callsign = str(card.get("callsign") or "").strip().upper()
    if (not 3 <= len(callsign) <= 16 or callsign in
            {"UNKNOWN", "STATION", "N0CALL", "NO-CALL", "NOCALL"} or
            not callsign.isascii() or
            any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-" for ch in callsign) or
            not any(ch.isalpha() for ch in callsign) or
            not any(ch.isdigit() for ch in callsign) or
            card.get("card_id") is None):
        return False
    quality = reception_quality(card)
    # Older decoders may supply only CRC-checked region coverage. Every
    # verified pixel has necessarily been heard, so it is a safe lower bound.
    known_pixels = quality.heard if quality.heard is not None else quality.checked
    if known_pixels is None:
        return False
    cutoff = max(1, min(100, int(threshold_percent)))
    return sum(known_pixels) * 100 >= cutoff * quality.total_pixels

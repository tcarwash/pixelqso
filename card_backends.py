"""Audio backend boundary for PixelQSO card bursts.

A backend accepts the same card and returns 48 kHz audio or a common card
report. Optional modem dependencies are imported only when selected.
"""
from __future__ import annotations

import math
import importlib.util
from dataclasses import dataclass, replace
from typing import Protocol

import numpy as np
import cardmodem as pixel
from card_transfer import HEADER as TRANSFER_HEADER

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
    profile: pixel.ModemProfile = pixel.MINIMAL_AVATAR_PROFILE

    def encode(self, card, repeats, message_type, snr_db):
        cycle = (pixel.minimal_avatar_resilient_cycle_symbols(card, message_type=message_type, snr_db=snr_db)
                 if self.checked else pixel.minimal_avatar_symbols(card, message_type=message_type, snr_db=snr_db))
        return pixel.synthesize(pixel.repeat_symbol_stream(cycle, repeats), profile=self.profile)

    def estimate_seconds(self, card, repeats):
        duration = (pixel.minimal_avatar_resilient_duration(card=card) if self.checked
                    else pixel.minimal_avatar_duration(card=card))
        return repeats * duration * pixel.MINIMAL_AVATAR_PROFILE.baud / self.profile.baud

    def decode(self, audio, sample_rate):
        if sample_rate == AUDIO_RATE:
            # All avatar tones fit comfortably in 8 kHz audio. Filter before
            # decimation so live and recorded acquisition use the same path.
            taps = np.arange(-48, 49)
            kernel = np.sinc(taps / 8) * np.hamming(len(taps))
            kernel /= kernel.sum()
            audio = np.convolve(np.asarray(audio), kernel, mode="same")[::6]
            decode_rate = 8000
        else:
            decode_rate = sample_rate
        pixels, decoded = pixel.decode_minimal_avatar_audio_auto(audio, decode_rate, profile=self.profile)
        if self.profile != pixel.MINIMAL_AVATAR_PROFILE:
            decoded["avatar_mode"] = self.key
            decoded["audio_tones_hz"] = list(self.profile.tones_hz)
        return _report(pixels, decoded, sample_rate, self.profile.key)

    def minimum_audio_seconds(self):
        symbols = len(pixel.FRAME_SYNC) + pixel.AVATAR_META_TONE_COUNT + (132 if self.checked else 32)
        return symbols / self.profile.baud


# PixelQSO transfer frames carry fragments with whole-card CRC32 integrity.
# Header: magic, version, card id, dimensions, palette, message, SNR,
# identity, fragment index/count and CRC of the whole packed raster.
@dataclass(frozen=True)
class Data2GHostBackend:
    """Mode metadata for the separately running Data2G host."""
    key: str
    label: str
    mode: object | None = None
    checked: bool = False
    max_repeats: int = 20

    @property
    def mode_name(self):
        return getattr(self.mode, "name", "")

    def encode(self, *_args):
        raise RuntimeError("Data2G card frames are submitted through the configured host connection")

    def estimate_seconds(self, card, repeats):
        if self.mode is None:
            raise RuntimeError("Connect to a Data2G host to discover this mode")
        if not 1 <= repeats <= self.max_repeats:
            raise ValueError(f"Copies must be 1..{self.max_repeats}")
        packed = pixel.minimal_avatar_payload(card)
        app_bytes = 256 - TRANSFER_HEADER.size
        mode = self.mode
        if mode.bytes_per_codeword <= 0 or mode.max_codewords < 1:
            raise ValueError("Data2G host advertised invalid mode capacity")
        per_codeword = ((mode.seconds_at_max - mode.seconds_at_one) /
                        max(1, mode.max_codewords - 1))
        estimate = 0.0
        for offset in range(0, len(packed), app_bytes):
            app_frame_bytes = TRANSFER_HEADER.size + min(app_bytes, len(packed) - offset)
            # Data2G's broadcast stream adds a two-byte frame length before
            # coding. ACKMODE serialization means each PixelQSO frame gets its
            # own burst; the host may combine codewords from that frame only.
            codewords = math.ceil((app_frame_bytes + 2) / mode.bytes_per_codeword)
            if codewords > mode.max_codewords:
                raise ValueError("PixelQSO fragment exceeds the host mode's maximum burst capacity")
            estimate += mode.seconds_at_one + max(0, codewords - 1) * per_codeword
        return repeats * estimate

    def decode(self, *_args):
        raise RuntimeError("Data2G receive frames are delivered through the configured host connection")

    def minimum_audio_seconds(self):
        return self.mode.seconds_at_one if self.mode is not None else 0.0


def host_mode_backends(modes):
    """Choose compatible robust/fast presets from the host's advertised modes.

    Prefer the usual 1.2-kHz profiles, then adapt to narrower host bandwidth
    caps (whose mode names have prefixes such as ``n10-`` and ``n4-``).
    """
    modes = [m for m in modes if m.bytes_per_codeword > 0 and m.max_codewords > 0
             and math.ceil((256 + 2) / m.bytes_per_codeword) <= m.max_codewords]

    def fraction(mode):
        try:
            value = mode.name.rsplit("r", 1)[1].split("/", 1)
            return int(value[0]) / int(value[1])
        except (IndexError, ValueError, ZeroDivisionError):
            return None

    def family_modes(family):
        return [m for m in modes if family in m.name.lower() and "ack" not in m.name.lower()]

    robust_candidates = family_modes("qpsk")
    fast_candidates = family_modes("16qam")
    robust = next((m for m in robust_candidates if m.name == "qpsk-r1/2"), None)
    if robust is None:
        same_rate = [m for m in robust_candidates if m.name.endswith("qpsk-r1/2")]
        if same_rate:
            robust = max(same_rate, key=lambda m: (m.bandwidth_hz, m.bytes_per_codeword))
    if robust is None and robust_candidates:
        robust = min(robust_candidates, key=lambda m: (
            fraction(m) if fraction(m) is not None else 2.0,
            -m.bandwidth_hz, -m.bytes_per_codeword))

    fast = next((m for m in fast_candidates if m.name == "16qam-r1/2"), None)
    if fast is None and fast_candidates:
        fast = max(fast_candidates, key=lambda m: (
            fraction(m) if fraction(m) is not None else 0.0,
            m.bandwidth_hz, m.bytes_per_codeword))

    result = {}
    for key, role, mode in (("data2g_1200_robust", "robust", robust),
                            ("data2g_1200_fast", "fast", fast)):
        if mode is None:
            continue
        bandwidth = (f"{mode.bandwidth_hz / 1000:g} kHz" if mode.bandwidth_hz >= 1000
                     else f"{mode.bandwidth_hz:g} Hz")
        result[key] = Data2GHostBackend(key, f"Data2G · {bandwidth} · {role}", mode)
    return result


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
    "fast_avatar": AvatarBackend("fast_avatar", "Fast · unverified · experimental", False),
    "fast_avatar_fec": AvatarBackend("fast_avatar_fec", "Resilient · 400 baud · experimental", True),
    **{f"resilient_{baud}": AvatarBackend(
        f"resilient_{baud}", f"Resilient · ≈{9 * baud} Hz · {baud} baud", True,
        pixel.ModemProfile(f"avatar-{baud}", f"Narrow avatar · {baud} baud", baud,
                           tuple((i + 1) * baud for i in range(8)), 9 * baud))
       for baud in (100, 50, 25)},
    "data2g_1200_robust": Data2GHostBackend("data2g_1200_robust", "Data2G · 1.2 kHz · robust"),
    "data2g_1200_fast": Data2GHostBackend("data2g_1200_fast", "Data2G · 1.2 kHz · fast"),
}

@dataclass(frozen=True)
class ModeSpec:
    """Policy metadata shared by selection, receive, APIs, and tooling."""
    key: str
    fallback_label: str
    experimental: bool
    execution_path: str
    availability: str = "installed"

    @property
    def label(self) -> str:
        # Host discovery can refine the displayed bandwidth without changing
        # the stable mode key or the policy metadata.
        backend = BACKENDS.get(self.key)
        return backend.label if backend is not None else self.fallback_label


# Single mode catalog. Runtime host discovery fills in the Data2G backends in
# BACKENDS; this policy remains the source of ordering, visibility, execution
# path, and dependency/connection requirements.
MODE_REGISTRY: dict[str, ModeSpec] = {
    "data2g_1200_robust": ModeSpec("data2g_1200_robust", "Data2G · robust", False, "data2g_host", "host"),
    "data2g_1200_fast": ModeSpec("data2g_1200_fast", "Data2G · fast", False, "data2g_host", "host"),
    "resilient_100": ModeSpec("resilient_100", "Resilient · 900 Hz · 100 baud", False, "pixelqso_audio"),
    "resilient_50": ModeSpec("resilient_50", "Resilient · 450 Hz · 50 baud", False, "pixelqso_audio"),
    "resilient_25": ModeSpec("resilient_25", "Resilient · 225 Hz · 25 baud", False, "pixelqso_audio"),
    "fast_avatar_fec": ModeSpec("fast_avatar_fec", "Resilient · 400 baud · experimental", True, "pixelqso_audio"),
    "fast_avatar": ModeSpec("fast_avatar", "Fast · experimental", True, "pixelqso_audio"),
    "experimental_qpsk_5s": ModeSpec("experimental_qpsk_5s", "Weak signal · experimental", True, "pixelqso_audio", "scipy"),
    "experimental_qpsk_combined": ModeSpec("experimental_qpsk_combined", "Weak signal combined · experimental", True, "pixelqso_audio", "scipy"),
    "standard": ModeSpec("standard", "Legacy packet · experimental", True, "pixelqso_audio"),
}
MODE_ORDER = tuple(MODE_REGISTRY)
EXPERIMENTAL_MODE_KEYS = frozenset(key for key, spec in MODE_REGISTRY.items() if spec.experimental)
LEGACY_MODE_KEY = "standard"
DEFAULT_MODE_KEY = "resilient_100"


def is_experimental_mode(key: str) -> bool:
    spec = MODE_REGISTRY.get(key)
    return bool(spec and spec.experimental)


def available_modes(*, include_experimental: bool = False) -> list[tuple[str, CardBackend]]:
    """Available modes in registry order, subject to shared policy."""
    return [(key, BACKENDS[key]) for key, spec in MODE_REGISTRY.items()
            if key in BACKENDS and (include_experimental or not spec.experimental)
            and is_available(key)]


def normal_default_mode(keys=None, *, host_connected: bool = False) -> str:
    """Choose a selectable default without preferring an unconfigured host."""
    present = set(keys if keys is not None else (key for key, _ in available_modes()))
    if host_connected and "data2g_1200_robust" in present:
        return "data2g_1200_robust"
    if DEFAULT_MODE_KEY in present:
        return DEFAULT_MODE_KEY
    return next((key for key in MODE_ORDER if key in present and not is_experimental_mode(key)),
                "standard")

def get_backend(key: str, audio_placement: str | float = "near_carrier") -> CardBackend:
    backend = BACKENDS[key]
    if not key.startswith("resilient_"):
        return backend
    baud = backend.profile.baud
    if audio_placement == "near_carrier":
        low = baud
    elif audio_placement == "centered":
        low = 1500 - 3.5 * baud
    else:
        low = float(audio_placement)
    if not math.isfinite(low) or not 25 <= low <= 3000 - 8 * baud:
        raise ValueError("Audio placement must keep the narrow signal between 25 and 3000 Hz")
    profile = replace(backend.profile, tones_hz=tuple(low + i * baud for i in range(8)))
    return replace(backend, profile=profile)


def is_card_backend(key: str) -> bool:
    return key in BACKENDS


def audio_backend_keys(*, include_experimental: bool = True) -> tuple[str, ...]:
    """Locally synthesized modes for benchmark/replay tools, in registry order."""
    return tuple(key for key, spec in MODE_REGISTRY.items()
                 if spec.execution_path == "pixelqso_audio"
                 and (include_experimental or not spec.experimental)
                 and key in BACKENDS and is_available(key))


def is_available(key: str) -> bool:
    spec = MODE_REGISTRY.get(key)
    if spec is None:
        return False
    if spec.availability == "scipy":
        return importlib.util.find_spec("scipy") is not None
    if spec.availability == "host":
        backend = BACKENDS.get(key)
        return isinstance(backend, Data2GHostBackend) and backend.mode is not None
    return key == LEGACY_MODE_KEY or key in BACKENDS

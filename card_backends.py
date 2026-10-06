"""Audio backend boundary for PixelQSO card bursts.

A backend accepts the same card and returns 48 kHz audio or a common card
report. Optional modem dependencies are imported only when selected.
"""
from __future__ import annotations

import math
import importlib.util
from dataclasses import dataclass, replace
from typing import Protocol
from urllib.parse import quote, unquote

import numpy as np
import cardmodem as pixel
from card_transfer import HEADER as TRANSFER_HEADER

AUDIO_RATE = pixel.SAMPLE_RATE
DATA2G_PREFERRED_FRAME_BYTES = 256
DATA2G_LENGTH_PREFIX_BYTES = 2
DATA2G_MAX_CONTROL_CODEWORDS = 4
DATA2G_MODE_KEY_PREFIX = "data2g_mode_"

# Display names only; persistent IDs and on-air formats stay stable.
EXPERIMENTAL_MODE_LABELS = {
    "experimental_8fsk_ldpc": "8-FSK · soft LDPC · single burst",
    "experimental_qpsk_equalized": "QPSK · LDPC + adaptive equalizer",
    "experimental_8fsk_outer_rs": "8-FSK · strong RS + outer parity",
    "experimental_16fsk_ldpc": "16-FSK · soft LDPC · single burst",
    **{f"resilient_{baud}": f"8-FSK · RS + soft Chase · {baud} baud · ≈{9 * baud} Hz"
       for baud in (100, 50, 25)},
    "fast_avatar_fec": "8-FSK · RS + soft Chase · 400 baud · block CRC",
    "fast_avatar_fec_v4": "8-FSK · RS + soft Chase · 400 baud · v4 CRC32",
    "fast_avatar": "8-FSK · raw pixels · 400 baud · unchecked",
    "experimental_qpsk_5s": "QPSK · soft LDPC · single burst",
    "experimental_qpsk_combined": "QPSK · soft LDPC + copy combining",
    "standard": "4-FSK · soft Viterbi · progressive packets",
}

EXPERIMENTAL_MODE_DESCRIPTIONS = {
    "experimental_8fsk_ldpc": "One 5.005-second, 32×32 eight-color burst. "
        "600-baud Gray-labelled 8-FSK, interleaved rate-matched LDPC, pilot timing, and whole-card CRC32. "
        "Fixed tones 450–2550 Hz; requires a receiver supporting this experimental format.",
    "experimental_qpsk_equalized": "One 4.622-second, 32×32 eight-color QPSK burst. "
        "Soft LDPC with a 21-tap fractionally spaced adaptive equalizer, training, pilot and "
        "confidence-gated decision-directed NLMS. Uses the existing QPSK wire format.",
    "experimental_8fsk_outer_rs": "One 5.480-second, 32×32 eight-color burst. "
        "400-baud 8-FSK with shortened RS(63,53), soft Chase recovery and per-shard CRC16. "
        "Outer systematic RS(16,12) reconstructs up to four missing shards; whole-card CRC32 gates completion. "
        "Checked pixels appear early. Fixed tones 800–2200 Hz; new experimental wire format.",
    "experimental_16fsk_ldpc": "One 5.640-second, 32×32 eight-color burst. "
        "400-baud Gray-labelled 16-FSK, interleaved rate-matched LDPC, pilot timing and whole-card CRC32. "
        "Fixed tones 375–2625 Hz; overlapping tones trade bandwidth for decoder difficulty. New experimental wire format.",
    **{key: "8-tone FSK with shortened Reed–Solomon RS(63,61), bounded soft-decision "
       "Chase recovery, and per-block CRC16. Provisional pixels appear before block verification."
       for key in ("resilient_100", "resilient_50", "resilient_25", "fast_avatar_fec")},
    "fast_avatar_fec_v4": "8-tone FSK with Reed–Solomon and soft Chase recovery. "
        "Version 4 binds blocks to the image and verifies the whole raster with CRC32 and a content tag.",
    "fast_avatar": "8-tone FSK with a protected identity header and hard-decision raw image pixels. "
        "Image pixels have no FEC or checksum verification.",
    "experimental_qpsk_5s": "Shaped QPSK with soft-decision LDPC decoding and whole-card CRC32. "
        "Sends one 4.622-second burst; the receiver can combine copies when needed.",
    "experimental_qpsk_combined": "Shaped QPSK with soft-decision LDPC decoding and whole-card CRC32. "
        "Sends repeated bursts; the receiver combines noise-weighted soft bit evidence when needed.",
    "standard": "4-tone FSK with rate-1/2 K=7 convolutional coding, soft-decision Viterbi decoding, "
        "packet CRC16, and soft combining across repeated packets. Image detail arrives progressively.",
}


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
    wire_version: int = 3

    def encode(self, card, repeats, message_type, snr_db):
        cycle = (pixel.minimal_avatar_resilient_cycle_symbols(
                    card, message_type=message_type, snr_db=snr_db,
                    wire_version=self.wire_version)
                 if self.checked else pixel.minimal_avatar_symbols(card, message_type=message_type, snr_db=snr_db))
        return pixel.synthesize(pixel.repeat_symbol_stream(cycle, repeats), profile=self.profile)

    def estimate_seconds(self, card, repeats):
        if self.checked:
            symbol_count = len(pixel.minimal_avatar_resilient_cycle_symbols(
                card, wire_version=self.wire_version))
            return repeats * symbol_count / self.profile.baud
        duration = pixel.minimal_avatar_duration(card=card)
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
        header = (pixel.AVATAR_META_V4_TONE_COUNT if self.wire_version == 4
                  else pixel.AVATAR_META_TONE_COUNT)
        # A protected identity is useful before the first image block finishes.
        symbols = len(pixel.FRAME_SYNC) + header
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
    disabled_reason: str | None = None

    @property
    def mode_name(self):
        return getattr(self.mode, "name", "")

    @property
    def max_frame_bytes(self):
        return data2g_max_frame_bytes(self.mode)

    @property
    def usable(self):
        return self.mode is not None and self.disabled_reason is None and self.max_frame_bytes is not None

    def encode(self, *_args):
        raise RuntimeError("Data2G card frames are submitted through the configured host connection")

    def estimate_seconds(self, card, repeats):
        if self.mode is None:
            raise RuntimeError("Connect to a Data2G host to discover this mode")
        if self.disabled_reason:
            raise ValueError(self.disabled_reason)
        if not 1 <= repeats <= self.max_repeats:
            raise ValueError(f"Copies must be 1..{self.max_repeats}")
        packed = pixel.minimal_avatar_payload(card)
        frame_limit = self.max_frame_bytes
        if frame_limit is None:
            raise ValueError("Data2G mode cannot fit the Pixel QSO card header and image data")
        app_bytes = frame_limit - TRANSFER_HEADER.size
        mode = self.mode
        per_codeword = ((mode.seconds_at_max - mode.seconds_at_one) /
                        max(1, mode.max_codewords - 1))
        estimate = 0.0
        for offset in range(0, len(packed), app_bytes):
            frame_bytes = TRANSFER_HEADER.size + min(app_bytes, len(packed) - offset)
            # Data2G's broadcast stream adds a two-byte frame length before
            # coding. Reserve the host's maximum four control codewords; ACKMODE
            # serialization gives each PixelQSO frame its own burst.
            codewords = (DATA2G_MAX_CONTROL_CODEWORDS +
                         math.ceil((frame_bytes + DATA2G_LENGTH_PREFIX_BYTES) /
                                   mode.bytes_per_codeword))
            if codewords > mode.max_codewords:
                raise ValueError("PixelQSO fragment exceeds the host mode's maximum burst capacity")
            estimate += mode.seconds_at_one + max(0, codewords - 1) * per_codeword
        return repeats * estimate

    def decode(self, *_args):
        raise RuntimeError("Data2G receive frames are delivered through the configured host connection")

    def minimum_audio_seconds(self):
        return self.mode.seconds_at_one if self.mode is not None else 0.0


def data2g_mode_key(mode_name: str) -> str:
    """Return a stable, collision-safe UI/settings key for a host mode name."""
    return DATA2G_MODE_KEY_PREFIX + quote(str(mode_name), safe="")


def is_data2g_mode_key(mode_key: str) -> bool:
    """Whether a persisted key names a dynamically discovered host mode."""
    return isinstance(mode_key, str) and mode_key.startswith(DATA2G_MODE_KEY_PREFIX)


def data2g_mode_name(mode_key: str) -> str:
    """Recover the exact host mode name from a dynamic settings key."""
    if not is_data2g_mode_key(mode_key):
        return str(mode_key)
    return unquote(mode_key[len(DATA2G_MODE_KEY_PREFIX):])


def data2g_max_frame_bytes(mode, preferred=DATA2G_PREFERRED_FRAME_BYTES):
    """Safe app-frame limit after Data2G's stream prefix and control reserve."""
    if mode is None:
        return None
    try:
        preferred = int(preferred)
        bytes_per_codeword = int(mode.bytes_per_codeword)
        max_codewords = int(mode.max_codewords)
        if (preferred < TRANSFER_HEADER.size + 1 or bytes_per_codeword <= 0 or
                max_codewords <= DATA2G_MAX_CONTROL_CODEWORDS or
                not math.isfinite(float(mode.seconds_at_one)) or
                not math.isfinite(float(mode.seconds_at_max)) or
                mode.seconds_at_one <= 0 or mode.seconds_at_max < mode.seconds_at_one):
            return None
        data_capacity = ((max_codewords - DATA2G_MAX_CONTROL_CODEWORDS) *
                         bytes_per_codeword - DATA2G_LENGTH_PREFIX_BYTES)
        if data_capacity < TRANSFER_HEADER.size + 1:
            return None
        return min(int(preferred), data_capacity)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def host_mode_backends(modes, supported_names=None):
    """Build a backend entry for every mode the host advertises.

    `supported_names` should come from BCAST MODE validation on the opened
    group, since MODES includes modes that may not fit that group's control.
    Rejected or undersized modes remain present with a disabled reason.
    """
    allowed = None if supported_names is None else set(supported_names)
    result = {}
    for mode in sorted(modes, key=lambda item: (item.bandwidth_hz, item.name)):
        frame_limit = data2g_max_frame_bytes(mode)
        if allowed is not None and mode.name not in allowed:
            reason = "The host refused BCAST MODE for this group's control fields."
        elif frame_limit is None:
            reason = "Not enough codeword capacity for the Pixel QSO header and one image byte."
        else:
            reason = None
        key = data2g_mode_key(mode.name)
        bandwidth = (f"{mode.bandwidth_hz / 1000:g} kHz" if mode.bandwidth_hz >= 1000
                     else f"{mode.bandwidth_hz:g} Hz")
        capacity = f"{frame_limit} B/frame" if frame_limit is not None else "not usable"
        label = f"Data2G · {mode.name} · {bandwidth} · {capacity}"
        result[key] = Data2GHostBackend(key, label, mode, disabled_reason=reason)
    return result


def recommended_data2g_modes(backends):
    """Small HF shortlist, resolved only from usable advertised host modes."""
    recommended = []
    used = set()
    for role, suffix, target, ceiling in (
            ("Everyday HF", "qpsk-r1/2", 1200, None),
            ("Narrow HF", "qpsk-r1/2", 500, 500),
            ("Faster · strong path", "16qam-r1/2", 1200, None)):
        candidates = [(key, backend) for key, backend in backends.items()
                      if backend.usable and key not in used and
                      (backend.mode_name == suffix or backend.mode_name.endswith("-" + suffix)) and
                      (ceiling is None or backend.mode.bandwidth_hz <= ceiling)]
        if not candidates:
            continue
        key, _backend = min(candidates, key=lambda item: (
            abs(item[1].mode.bandwidth_hz - target), item[1].mode_name))
        used.add(key)
        recommended.append((key, role))
    if not recommended:
        key = preferred_data2g_mode_key(backends, "robust")
        if key in backends and backends[key].usable:
            recommended.append((key, "HF starting point"))
    return recommended


def preferred_data2g_mode_key(backends, preset="robust"):
    """Resolve legacy Robust/Fast preferences against a dynamic host catalog."""
    usable = [(key, backend) for key, backend in backends.items() if backend.usable]
    if not usable:
        return "data2g_1200_robust"
    entries = usable
    if preset not in {"robust", "fast"}:
        return next((key for key, backend in entries if backend.mode_name == preset),
                    entries[0][0])

    def fraction(mode):
        try:
            rate = mode.name.rsplit("r", 1)[1].split("/", 1)
            return int(rate[0]) / int(rate[1])
        except (AttributeError, IndexError, ValueError, ZeroDivisionError):
            return None

    candidates = [(key, backend) for key, backend in entries
                  if ("qpsk" in backend.mode_name.lower() if preset == "robust"
                      else "16qam" in backend.mode_name.lower())]
    if not candidates:
        return entries[0][0]
    if preset == "robust":
        same_rate = next((key for key, backend in candidates
                          if backend.mode_name.endswith("qpsk-r1/2")), None)
        if same_rate is not None:
            return same_rate
        return min(candidates, key=lambda item: (
            fraction(item[1].mode) if fraction(item[1].mode) is not None else 2.0,
            -item[1].mode.bandwidth_hz))[0]
    same_rate = next((key for key, backend in candidates
                      if backend.mode_name.endswith("16qam-r1/2")), None)
    if same_rate is not None:
        return same_rate
    return max(candidates, key=lambda item: (
        fraction(item[1].mode) if fraction(item[1].mode) is not None else 0.0,
        item[1].mode.bandwidth_hz))[0]


@dataclass(frozen=True)
class WeakSignalBackend:
    key: str = "experimental_qpsk_5s"
    label: str = EXPERIMENTAL_MODE_LABELS["experimental_qpsk_5s"]
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
    label: str = EXPERIMENTAL_MODE_LABELS["experimental_qpsk_combined"]
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


@dataclass(frozen=True)
class ExperimentalBurstBackend:
    """New single-burst experiments; codecs stay lazy and CPU-only."""
    key: str
    checked: bool = True
    max_repeats: int = 1

    @property
    def label(self):
        return EXPERIMENTAL_MODE_LABELS[self.key]

    def encode(self, card, repeats, message_type, snr_db):
        if repeats != 1:
            raise ValueError("This experimental modem sends one burst")
        import experimental_burst_modem as burst
        return burst.encode(self.key, card, message_type, snr_db)

    def estimate_seconds(self, card, repeats):
        if repeats != 1:
            raise ValueError("This experimental modem sends one burst")
        import weak_signal_modem as weak
        import experimental_burst_modem as burst
        weak.packet(card)
        return burst.seconds(self.key)

    def minimum_audio_seconds(self):
        import experimental_burst_modem as burst
        return burst.minimum_seconds(self.key)

    def decode(self, audio, sample_rate):
        import experimental_burst_modem as burst
        return burst.decode(self.key, audio, sample_rate)


# The two legacy keys are retained only to migrate saved preferences and to
# represent the not-yet-discovered Data2G selection before a host connects.
BACKENDS: dict[str, CardBackend] = {
    **{key: ExperimentalBurstBackend(key) for key in (
        "experimental_8fsk_ldpc", "experimental_qpsk_equalized",
        "experimental_8fsk_outer_rs", "experimental_16fsk_ldpc")},
    "experimental_qpsk_5s": WeakSignalBackend(),
    "experimental_qpsk_combined": WeakSignalCombinedBackend(),
    "fast_avatar": AvatarBackend("fast_avatar", EXPERIMENTAL_MODE_LABELS["fast_avatar"], False),
    "fast_avatar_fec": AvatarBackend("fast_avatar_fec", EXPERIMENTAL_MODE_LABELS["fast_avatar_fec"], True),
    "fast_avatar_fec_v4": AvatarBackend("fast_avatar_fec_v4", EXPERIMENTAL_MODE_LABELS["fast_avatar_fec_v4"], True,
                                         wire_version=4),
    **{f"resilient_{baud}": AvatarBackend(
        f"resilient_{baud}", EXPERIMENTAL_MODE_LABELS[f"resilient_{baud}"], True,
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
    adapter_key: str | None = None
    supports_audio_placement: bool = False

    @property
    def adapter(self) -> str:
        if self.adapter_key:
            return self.adapter_key
        return "data2g_host" if self.execution_path == "data2g_host" else "experimental_local"

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
    **{key: ModeSpec(key, EXPERIMENTAL_MODE_LABELS[key], True, "pixelqso_audio", "scipy")
       for key in ("experimental_8fsk_ldpc", "experimental_qpsk_equalized",
                   "experimental_8fsk_outer_rs", "experimental_16fsk_ldpc")},
    "resilient_100": ModeSpec("resilient_100", EXPERIMENTAL_MODE_LABELS["resilient_100"], True, "pixelqso_audio", supports_audio_placement=True),
    "resilient_50": ModeSpec("resilient_50", EXPERIMENTAL_MODE_LABELS["resilient_50"], True, "pixelqso_audio", supports_audio_placement=True),
    "resilient_25": ModeSpec("resilient_25", EXPERIMENTAL_MODE_LABELS["resilient_25"], True, "pixelqso_audio", supports_audio_placement=True),
    "fast_avatar_fec": ModeSpec("fast_avatar_fec", EXPERIMENTAL_MODE_LABELS["fast_avatar_fec"], True, "pixelqso_audio"),
    "fast_avatar_fec_v4": ModeSpec("fast_avatar_fec_v4", EXPERIMENTAL_MODE_LABELS["fast_avatar_fec_v4"], True, "pixelqso_audio"),
    "fast_avatar": ModeSpec("fast_avatar", EXPERIMENTAL_MODE_LABELS["fast_avatar"], True, "pixelqso_audio"),
    "experimental_qpsk_5s": ModeSpec("experimental_qpsk_5s", EXPERIMENTAL_MODE_LABELS["experimental_qpsk_5s"], True, "pixelqso_audio", "scipy"),
    "experimental_qpsk_combined": ModeSpec("experimental_qpsk_combined", EXPERIMENTAL_MODE_LABELS["experimental_qpsk_combined"], True, "pixelqso_audio", "scipy"),
    "standard": ModeSpec("standard", EXPERIMENTAL_MODE_LABELS["standard"], True, "pixelqso_audio"),
}
MODE_ORDER = tuple(MODE_REGISTRY)
EXPERIMENTAL_MODE_KEYS = frozenset(key for key, spec in MODE_REGISTRY.items() if spec.experimental)
LEGACY_MODE_KEY = "standard"
DEFAULT_MODE_KEY = "data2g_1200_robust"


def register_data2g_mode_backends(backends):
    """Replace the dynamic Data2G catalog after a successful host discovery."""
    global MODE_ORDER
    for key, spec in list(MODE_REGISTRY.items()):
        if spec.execution_path == "data2g_host":
            MODE_REGISTRY.pop(key, None)
            # Static preset keys remain as the pre-discovery fallback. Dynamic
            # keys belong to the last host catalog and must not remain
            # selectable/decodable after a reconnect replaces that catalog.
            if is_data2g_mode_key(key):
                BACKENDS.pop(key, None)
    for key, backend in backends.items():
        BACKENDS[key] = backend
        MODE_REGISTRY[key] = ModeSpec(key, backend.label, False, "data2g_host", "host",
                                      "data2g_host")
    MODE_ORDER = tuple(MODE_REGISTRY)


def is_experimental_mode(key: str) -> bool:
    spec = MODE_REGISTRY.get(key)
    return bool(spec and spec.experimental)


def available_modes(*, include_experimental: bool = False) -> list[tuple[str, CardBackend]]:
    """Available modes in registry order, subject to shared policy."""
    return [(key, BACKENDS[key]) for key, spec in MODE_REGISTRY.items()
            if key in BACKENDS and (include_experimental or not spec.experimental)
            and is_available(key)]


def normal_default_mode(keys=None, *, host_connected: bool = False) -> str:
    """Choose a Data2G default; never silently fall back to a local modem."""
    present = set(keys if keys is not None else (key for key, _ in available_modes()))
    if host_connected:
        return next((key for key in MODE_ORDER
                     if key in present and is_data2g_mode_key(key)), DEFAULT_MODE_KEY)
    return DEFAULT_MODE_KEY

def get_backend(key: str, audio_placement: str | float = "near_carrier") -> CardBackend:
    backend = BACKENDS[key]
    spec = MODE_REGISTRY.get(key)
    if spec is None or not spec.supports_audio_placement:
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


def mode_supports_audio_placement(key: str) -> bool:
    spec = MODE_REGISTRY.get(key)
    return bool(spec and spec.supports_audio_placement)


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
        return (spec.adapter == "data2g_host" and
                bool(getattr(backend, "usable", False)))
    return key == LEGACY_MODE_KEY or key in BACKENDS

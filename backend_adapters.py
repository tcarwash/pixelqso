"""Internal contract between Pixel QSO's card exchange and modem services.

Adapters describe ownership and integrity explicitly. Data2G transports checked
application frames; the experimental adapter wraps Pixel QSO's local image
modems, which decode their own waveform and may expose provisional pixels.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class AdapterKind(str, Enum):
    FRAME_TRANSPORT = "frame_transport"
    LOCAL_IMAGE_MODEM = "local_image_modem"


class IntegrityScope(str, Enum):
    CANDIDATE = "candidate"
    CHECKED_REGION = "checked_region"
    CHECKED_FRAME = "checked_frame"
    VERIFIED_IMAGE = "verified_image"


@dataclass(frozen=True)
class AdapterCapabilities:
    kind: AdapterKind
    audio_owner: str
    radio_owner: str
    receive_model: str
    tx_completion: str
    integrity: IntegrityScope
    provisional_pixels: bool
    spectrum: bool
    receive_all_modes: bool


@dataclass(frozen=True)
class ReceiveEvidence:
    """Normalized evidence scope; payload meaning stays adapter-specific."""
    scope: IntegrityScope
    source: str
    identity: str | None = None
    location: tuple[int, int, int, int] | None = None
    payload: bytes | tuple[int, ...] | None = None


@dataclass(frozen=True)
class StopResult:
    queued_cancelled: int
    in_flight_may_complete: bool


class BackendAdapter(Protocol):
    key: str
    capabilities: AdapterCapabilities


class Data2GFrameAdapter:
    key = "data2g_host"
    capabilities = AdapterCapabilities(
        kind=AdapterKind.FRAME_TRANSPORT,
        audio_owner="data2g_host",
        radio_owner="data2g_host",
        receive_model="host_selected",
        tx_completion="host_ackmode_local_completion",
        integrity=IntegrityScope.CHECKED_FRAME,
        provisional_pixels=False,
        spectrum=False,
        receive_all_modes=False,
    )

    @staticmethod
    def discover(session, timeout=None):
        modes = session.start(timeout=timeout)
        try:
            return modes, session.check_broadcast_modes(mode.name for mode in modes)
        except Exception:
            session.close()
            raise

    @staticmethod
    def select_mode(session, mode_name):
        session.set_mode(mode_name)

    @staticmethod
    def submit_frame(session, tag, frame):
        session.send_frame(tag, frame)

    @staticmethod
    def disconnect(session, timeout=5.0):
        return session.close(timeout=timeout)

    @staticmethod
    def request_stop(queued_frames, in_flight_tag):
        cancelled = len(queued_frames)
        queued_frames.clear()
        # The supported host API has no cancellation command for an ACKMODE
        # frame already handed to KISS, so its RF outcome remains uncertain.
        return StopResult(cancelled, in_flight_tag is not None)


class ExperimentalLocalAdapter:
    key = "experimental_local"
    capabilities = AdapterCapabilities(
        kind=AdapterKind.LOCAL_IMAGE_MODEM,
        audio_owner="pixelqso",
        radio_owner="pixelqso_cat_or_test_audio",
        receive_model="local_waveform_decode",
        tx_completion="local_audio_completion",
        integrity=IntegrityScope.CHECKED_REGION,
        provisional_pixels=True,
        spectrum=True,
        receive_all_modes=True,
    )

    @staticmethod
    def encode(backend, card, repeats, message_type, snr_db):
        return backend.encode(card, repeats, message_type, snr_db)

    @staticmethod
    def decode(backend, audio, sample_rate):
        return backend.decode(audio, sample_rate)


DATA2G_ADAPTER = Data2GFrameAdapter()
EXPERIMENTAL_ADAPTER = ExperimentalLocalAdapter()
ADAPTERS: dict[str, BackendAdapter] = {
    DATA2G_ADAPTER.key: DATA2G_ADAPTER,
    EXPERIMENTAL_ADAPTER.key: EXPERIMENTAL_ADAPTER,
}


def register_adapter(adapter: BackendAdapter) -> None:
    """Register an internal adapter implementation for a known mode family."""
    key = str(getattr(adapter, "key", "")).strip()
    capabilities = getattr(adapter, "capabilities", None)
    if not key or not isinstance(capabilities, AdapterCapabilities):
        raise ValueError("adapter must declare a stable key and AdapterCapabilities")
    if key in ADAPTERS and ADAPTERS[key] is not adapter:
        raise ValueError(f"adapter key {key!r} is already registered")
    ADAPTERS[key] = adapter


def adapter_for_backend_selection(selection: str, mode_adapter: str | None = None) -> BackendAdapter:
    """Resolve product backend choice independently from the selected mode."""
    if selection == "data2g":
        return DATA2G_ADAPTER
    if mode_adapter and mode_adapter in ADAPTERS:
        return ADAPTERS[mode_adapter]
    return EXPERIMENTAL_ADAPTER

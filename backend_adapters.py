"""Internal contract between Pixel QSO's card exchange and modem services.

Adapters describe ownership and integrity explicitly. Data2G transports checked
application frames; the experimental adapter wraps Pixel QSO's local image
modems, which decode their own waveform and may expose provisional pixels.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import cardmodem as modem
from card_transfer import HEADER as CARD_TRANSFER_HEADER, fragment as card_fragment
from data2g_transport import (Data2GError, Data2GSession,
                              normalize_data2g_callsign)


class AdapterKind(str, Enum):
    FRAME_TRANSPORT = "frame_transport"
    LOCAL_IMAGE_MODEM = "local_image_modem"


class IntegrityScope(str, Enum):
    CANDIDATE = "candidate"
    CHECKED_REGION = "checked_region"
    CHECKED_FRAME = "checked_frame"
    VERIFIED_IMAGE = "verified_image"


class DecodeFailureKind(str, Enum):
    NO_MATCH = "no_match"
    BACKEND_ERROR = "backend_error"


@dataclass(frozen=True)
class DecodeFailure:
    adapter_key: str
    kind: DecodeFailureKind
    message: str
    retryable: bool


@dataclass(frozen=True)
class DecodeOutcome:
    report: dict | None = None
    failure: DecodeFailure | None = None

    def __post_init__(self):
        if (self.report is None) == (self.failure is None):
            raise ValueError("decode outcome must contain exactly one report or failure")


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


class ReceiveStartError(RuntimeError):
    def __init__(self, title: str, message: str):
        super().__init__(message)
        self.title = title
        self.message = message


@dataclass
class LocalReceiveHandle:
    audio_source: object | None = None
    audio_stream: object | None = None
    test_socket: object | None = None
    test_mode: bool = False


class BackendAdapter(Protocol):
    key: str
    capabilities: AdapterCapabilities


class FrameTransportAdapterProtocol(BackendAdapter, Protocol):
    def create_session(self, host, command_port, kiss_port, **options): ...
    def discover(self, session, timeout=None): ...
    def frame_card(self, card, max_frame_bytes, message_type, snr_db=None): ...
    def is_connected(self, session) -> bool: ...
    def group_port(self, session) -> int | None: ...
    def select_mode(self, session, mode_name): ...
    def submit_frame(self, session, tag, frame): ...
    def disconnect(self, session, timeout=5.0): ...
    def request_stop(self, queued_frames, in_flight_tag) -> StopResult: ...


class LocalImageModemAdapterProtocol(BackendAdapter, Protocol):
    def encode(self, backend, card, repeats, message_type, snr_db): ...
    def decode(self, backend, audio, sample_rate): ...
    def decode_capture(self, backend, audio, sample_rate) -> DecodeOutcome: ...
    def start_receive(self, *, device, audio_format, parent, audio_source_factory,
                      test_port=None, test_socket_factory=None, test_bind=None): ...
    def stop_receive(self, handle: LocalReceiveHandle | None): ...


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
    def validate_station_call(callsign: str | None) -> str | None:
        """Check the pinned Data2G BCAST FROM call field before connecting."""
        return normalize_data2g_callsign(callsign)

    @staticmethod
    def create_session(host, command_port, kiss_port, **options):
        """Create the adapter's host session without exposing its type to the app."""
        return Data2GSession(host, command_port, kiss_port, **options)

    @staticmethod
    def discover(session, timeout=None):
        modes = session.start(timeout=timeout)
        try:
            checks = session.check_broadcast_modes(mode.name for mode in modes)
            expected = {mode.name for mode in modes}
            if (not isinstance(checks, dict) or set(checks) != expected or
                    any(result is not None and
                        (not isinstance(result, str) or not result.strip())
                        for result in checks.values())):
                raise Data2GError(
                    "Data2G host returned an incomplete or malformed BCAST MODE validation catalog")
            return modes, checks
        except Exception:
            session.close()
            raise

    @staticmethod
    def frame_card(card, max_frame_bytes, message_type, snr_db=None):
        """Build bounded Pixel QSO application frames for a checked card."""
        frame_limit = int(max_frame_bytes)
        chunk_size = frame_limit - CARD_TRANSFER_HEADER.size
        if chunk_size < 1:
            raise ValueError(
                "Data2G frame capacity cannot fit the Pixel QSO header and image data")
        packed = modem.minimal_avatar_payload(card)
        return [card_fragment(card, packed, offset,
                              packed[offset:offset + chunk_size],
                              message_type, snr_db)
                for offset in range(0, len(packed), chunk_size)]

    @staticmethod
    def is_connected(session) -> bool:
        return bool(session is not None and session.connected)

    @staticmethod
    def group_port(session) -> int | None:
        return getattr(session, "port", None) if session is not None else None

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
    def encode_legacy(card, repeats, message_type, snr_db, mode, profile, *, beacon=False):
        """Encode the experimental legacy packet mode through its adapter."""
        if card.width != 32 or card.height != 32 or len(card.palette) != 8:
            raise ValueError(
                "The experimental legacy packet card supports only 32×32 with 8 colors. "
                "Use a burst transmission for other canvas formats.")
        packets = (modem.make_beacon_packets(card, mode) if beacon else
                   modem.make_packets(card, mode))
        symbols = modem.repeat_symbol_stream(modem.all_symbols(packets), int(repeats))
        from modem_envelope import transmit
        payload = modem.synthesize(modem.all_symbols(packets), profile=profile)
        return transmit('standard', card, payload, repeats, message_type, snr_db)

    @staticmethod
    def decode(backend, audio, sample_rate):
        return backend.decode(audio, sample_rate)

    @classmethod
    def decode_capture(cls, backend, audio, sample_rate, *, copies=None):
        try:
            if copies is not None and hasattr(backend, 'decode_payload'):
                from modem_envelope import decode_capture
                return DecodeOutcome(report=decode_capture(backend.key,audio,sample_rate,
                    backend.decode_payload,copies=copies,
                    copy_decoder=getattr(backend,'decode_payload_copies',None),
                    announced_decoder=getattr(backend,'decode_announced_copies',None)))
            return DecodeOutcome(report=backend.decode(audio, sample_rate))
        except ValueError as exc:
            # Decoder ValueErrors describe an absent or incomplete waveform
            # match; input selection/configuration errors happen before this
            # call and remain ordinary backend errors.
            return DecodeOutcome(failure=DecodeFailure(
                cls.key, DecodeFailureKind.NO_MATCH, str(exc), True))
        except Exception as exc:
            return DecodeOutcome(failure=DecodeFailure(
                cls.key, DecodeFailureKind.BACKEND_ERROR, str(exc), False))

    @classmethod
    def decode_legacy_wav(cls, path, state_path, profile="auto"):
        """Decode the legacy packet modem through the same adapter contract."""
        try:
            packets, errors, sample_rate, state = modem.decode_wav_with_state(
                path, state_path, profile)
            card, pixels = modem.reconstruct(packets)
            if card.get("card_id") is None:
                return DecodeOutcome(failure=DecodeFailure(
                    cls.key, DecodeFailureKind.NO_MATCH,
                    "No legacy packet card data received yet", True))
            report = {
                "sample_rate": sample_rate,
                "valid_packets": [{"type": packet.packet_type, "card_id": packet.card_id,
                                   "seq": packet.seq, "corrected_bits": packet.corrected_bits,
                                   "payload_hex": packet.payload.hex()} for packet in packets],
                "fresh_packets": state.get("fresh_valid_packets", []),
                "fresh_identity_evidence_card_ids": state.get(
                    "fresh_identity_evidence_card_ids", []),
                "receive_profile": state.get("profile"), "errors": errors,
                "card": card, "pixels": pixels, "evidence_state": state,
                "complete": modem.is_complete_card(packets),
            }
            return DecodeOutcome(report=report)
        except Exception as exc:
            return DecodeOutcome(failure=DecodeFailure(
                cls.key, DecodeFailureKind.BACKEND_ERROR, str(exc), False))

    @classmethod
    def decode_legacy_audio(cls, audio, sample_rate, profile="auto", *, copies=None):
        """Decode identity-separated copies and soft-combine checked packets."""
        from modem_envelope import decode_capture
        def payload_decoder(captures, rate):
            # Every legacy tone is below 2.2 kHz. Filter once before acquisition
            # instead of fitting thousands of 48 kHz sync templates per profile.
            if rate > 8000:
                from scipy.signal import resample_poly
                import math
                common = math.gcd(rate,8000)
                up,down=8000//common,rate//common
                captures = [resample_poly(capture,up,down) for capture in captures]
                rate = 8000
            acquired = None
            if profile == "auto":
                selected, acquired = modem.select_capture_evidence(captures[0], rate)
                if len(captures[0]) > 20*rate:
                    acquired = None  # The selector probes only a prefix.
            else:
                selected = modem.resolve_profile(profile)
            evidence = {}
            for index,capture in enumerate(captures):
                incoming, _, _, _ = (acquired if index == 0 and acquired is not None else
                    modem.receive_capture_evidence(capture, rate, profile=selected))
                for key, item in (incoming or {}).items():
                    if key not in evidence:
                        evidence[key] = item
                    else:
                        evidence[key]['log_likelihood'] += item['log_likelihood']
                        evidence[key]['copies'] += item['copies']
                        if item.get('valid_raw') and not evidence[key].get('valid_raw'):
                            evidence[key]['valid_raw'] = item['valid_raw']
                            evidence[key]['valid_corrected'] = item['valid_corrected']
            packets, errors = modem.decode_packet_evidence(evidence)
            card, pixels = modem.reconstruct(packets)
            if card.get('card_id') is None:
                raise ValueError('No legacy packet card data received yet')
            card['received_copies'] = max((item['copies'] for item in evidence.values()), default=1)
            return dict(valid_packet_count=len(packets), card=card, pixels=pixels,
                fresh_packets=[dict(type=p.packet_type,card_id=p.card_id,seq=p.seq,
                                    payload_hex=p.payload.hex()) for p in packets],
                receive_profile=selected.key, decoder_errors=errors,
                complete=modem.is_complete_card(packets))
        try:
            report = decode_capture('standard', audio, sample_rate,
                lambda x, rate: payload_decoder([x], rate), copies=copies,
                copy_decoder=payload_decoder)
            return DecodeOutcome(report=report)
        except ValueError as exc:
            return DecodeOutcome(failure=DecodeFailure(
                cls.key, DecodeFailureKind.NO_MATCH, str(exc), True))
        except Exception as exc:
            return DecodeOutcome(failure=DecodeFailure(
                cls.key, DecodeFailureKind.BACKEND_ERROR, str(exc), False))

    @staticmethod
    def start_receive(*, device, audio_format, parent, audio_source_factory,
                      test_port=None, test_socket_factory=None, test_bind=None):
        """Acquire local input through this adapter and return an owned handle."""
        if test_port is not None:
            if test_socket_factory is None or test_bind is None:
                raise ValueError("test audio requires socket construction and bind callbacks")
            try:
                port = int(test_port)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ReceiveStartError(
                    "Local audio link unavailable", "The local test-audio port is invalid.") from exc
            if not 1 <= port <= 65535:
                raise ReceiveStartError(
                    "Local audio link unavailable", "The local test-audio port is outside 1..65535.")
            sock = None
            try:
                sock = test_socket_factory(parent)
                if not test_bind(sock, port):
                    detail = sock.errorString() if hasattr(sock, "errorString") else "bind failed"
                    raise RuntimeError(detail)
            except Exception as exc:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
                    if hasattr(sock, "deleteLater"):
                        try:
                            sock.deleteLater()
                        except Exception:
                            pass
                raise ReceiveStartError(
                    "Local audio link unavailable",
                    f"Could not open the local test-audio port {port}: {exc}") from exc
            return LocalReceiveHandle(test_socket=sock, test_mode=True)
        if device is None:
            raise ReceiveStartError(
                "No audio input", "Not listening · select an audio input in Station settings.")
        source = None
        try:
            if not device.isFormatSupported(audio_format):
                raise ReceiveStartError(
                    "Audio format unsupported",
                    "Not listening · the selected input does not support 48 kHz mono 16-bit audio.")
            source = audio_source_factory(device, audio_format, parent)
            stream = source.start()
        except ReceiveStartError:
            raise
        except Exception as exc:
            if source is not None:
                try:
                    source.stop()
                except Exception:
                    pass
            raise ReceiveStartError(
                "Audio failed", f"Not listening · could not start the selected audio input: {exc}") from exc
        if not stream:
            try:
                source.stop()
            except Exception:
                pass
            raise ReceiveStartError(
                "Audio failed", "Not listening · could not open the selected audio input.")
        return LocalReceiveHandle(audio_source=source, audio_stream=stream)

    @staticmethod
    def stop_receive(handle: LocalReceiveHandle | None):
        if handle is None:
            return ()
        source, sock = handle.audio_source, handle.test_socket
        handle.audio_source = handle.audio_stream = handle.test_socket = None
        errors = []
        if source is not None:
            try:
                source.stop()
            except Exception as exc:
                errors.append(f"audio input: {exc}")
        if sock is not None:
            try:
                sock.close()
            except Exception as exc:
                errors.append(f"test audio socket: {exc}")
            finally:
                if hasattr(sock, "deleteLater"):
                    sock.deleteLater()
        return tuple(errors)


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
    required_operations = (
        ("create_session", "discover", "frame_card", "is_connected", "group_port", "select_mode", "submit_frame",
         "disconnect", "request_stop")
        if capabilities.kind is AdapterKind.FRAME_TRANSPORT else
        ("encode", "decode", "decode_capture", "start_receive", "stop_receive"))
    missing = [name for name in required_operations if not callable(getattr(adapter, name, None))]
    if missing:
        raise ValueError("adapter is missing required operations: " + ", ".join(missing))
    if key in ADAPTERS and ADAPTERS[key] is not adapter:
        raise ValueError(f"adapter key {key!r} is already registered")
    ADAPTERS[key] = adapter


def adapter_for_backend_selection(selection: str, mode_adapter: str | None = None) -> BackendAdapter:
    """Resolve product backend choice independently from the selected mode."""
    if selection == "data2g":
        return DATA2G_ADAPTER
    if selection == "experimental":
        if mode_adapter and mode_adapter in ADAPTERS:
            return ADAPTERS[mode_adapter]
        return EXPERIMENTAL_ADAPTER
    raise ValueError(f"unknown modem backend selection: {selection!r}")

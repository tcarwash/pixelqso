"""Receive SNR metadata, expressed relative to a 2500 Hz noise bandwidth."""
import math
import struct

REFERENCE_BANDWIDTH_HZ = 2500
KISS_RX_SNR = 0x0D  # PixelQSO managed-host extension; ordinary KISS data is unchanged.
RX_SNR = struct.Struct(">4s32sfH")


def set_measured_snr(card, value, method, bandwidth_hz=REFERENCE_BANDWIDTH_HZ):
    if value is None:
        return False
    try:
        value = float(value)
        bandwidth_hz = float(bandwidth_hz)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value) or not math.isfinite(bandwidth_hz) or bandwidth_hz <= 0:
        return False
    value += 10 * math.log10(bandwidth_hz / REFERENCE_BANDWIDTH_HZ)
    card.update(measured_snr_db=max(-127, min(127, round(value))),
                measured_snr_bandwidth_hz=REFERENCE_BANDWIDTH_HZ,
                measured_snr_method=method)
    return True


def encode_rx_snr(digest, value, bandwidth_hz):
    return RX_SNR.pack(b"PQS1", bytes.fromhex(digest), float(value), int(bandwidth_hz))


def decode_rx_snr(payload):
    if len(payload) != RX_SNR.size:
        raise ValueError("Invalid receive SNR metadata length")
    magic, digest, value, bandwidth = RX_SNR.unpack(payload)
    if magic != b"PQS1" or not math.isfinite(value) or not bandwidth:
        raise ValueError("Invalid receive SNR metadata")
    return digest.hex(), value, bandwidth


def snr_lines(card, *, sent=False):
    lines = []
    measured = card.get("measured_snr_db")
    if measured is not None:
        reference = card.get("measured_snr_bandwidth_hz")
        suffix = f"; {reference} Hz reference" if reference else ""
        lines.append(f"Receive SNR: {float(measured):+g} dB (estimate{suffix})")
    reported = card.get("snr_db")
    if reported is not None:
        label = "Sent SNR report" if sent else "Their SNR report"
        lines.append(f"{label}: {float(reported):+g} dB")
    return lines or ["SNR: Not available"]

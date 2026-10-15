"""Launch Data2G with TX-advertised burst lengths enabled on receive.

Older Data2G engines cap receive admission at 16 seconds, even when a legal
256-byte KISS frame needs longer. Keep the compatibility override for those
hosts. Upstream PR #65 supplies receive_accept itself and needs no override.
The wrapper also supplies PixelQSO's frame-associated receive-SNR metadata.
"""
from __future__ import annotations

import sys
import hashlib
import math
import os
import socket
import struct
from collections import defaultdict, deque

from snr import KISS_RX_SNR, encode_rx_snr


def waterfall_audio_datagram(data, channels: int, sample_rate: int) -> bytes:
    """Encode the host's left-channel float capture for the UI waterfall."""
    import numpy as np
    samples = np.frombuffer(data, dtype=np.float32)[::max(1, int(channels))]
    return struct.pack("!I", int(sample_rate)) + samples.astype("<f4", copy=False).tobytes()


def enable_waterfall_audio(port: int) -> None:
    """Mirror local-host capture blocks to PixelQSO's read-only waterfall."""
    from data2g.host import Capture

    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    original_take = Capture._take

    def take(self, data, nframes, timing, status):
        result = original_take(self, data, nframes, timing, status)
        try:
            # The host decodes the left channel. Mirror exactly that channel;
            # never open the audio device a second time in the GUI.
            sender.sendto(waterfall_audio_datagram(data, self.nin, self.rate),
                          ("127.0.0.1", port))
        except (OSError, TypeError, ValueError):
            pass  # Waterfall is optional and must never interrupt radio RX.
        return result

    Capture._take = take


def enable_advertised_receive_lengths() -> None:
    """Retain long-frame reception on old hosts; respect PR #65 on new ones."""
    from data2g import modem
    from data2g.arq import engine

    if callable(getattr(engine, "receive_accept", None)):
        return

    original = modem.Accept.of.__func__
    if getattr(original, "_pixelqso_full_receive", False):
        return

    def of(cls, names=None, max_secs=None, min_score=0.0):
        # Keep explicit submode restrictions, and all score checks. Drop only
        # the extra seconds cap; Accept.of still bounds each mode by the count
        # its protected header can represent.
        if names is None:
            max_secs = None
        return original(cls, names, max_secs, min_score)

    of._pixelqso_full_receive = True
    modem.Accept.of = classmethod(of)


def enable_receive_snr():
    """Send frame-associated PHY measurements ahead of data on the same KISS socket."""
    from data2g.kisslink import KissLink
    from data2g.tnc import KissServer, kiss_encode
    from data2g.arq import phy
    from data2g.config import SNR_REF_BW_HZ

    original_receive = KissLink.on_burst
    original_broadcast = KissServer.broadcast
    if getattr(original_receive, "_pixelqso_receive_snr", False):
        return
    pending = defaultdict(deque)

    def on_burst(self, received, rx=None):
        old_events = len(getattr(self, "events", []))
        frames = original_receive(self, received, rx)
        heard = [event.split() for event in getattr(self, "events", [])[old_events:]
                 if event.startswith("BCAST ") and len(event.split()) >= 3 and event.split()[2] == "HEARD"]
        if frames or heard:
            try:
                value = float(phy.measure(received)["snr_est"])
                if math.isfinite(value):
                    if sum(map(len, pending.values())) + len(frames or []) > 256:
                        pending.clear()
                    for port, frame in frames or []:
                        digest = hashlib.sha256(frame).hexdigest()
                        pending[(port, digest)].append(encode_rx_snr(digest, value, SNR_REF_BW_HZ))
                    if not frames:
                        mode = getattr(received.get("spec"), "name", "unknown")
                        count = int(received.get("n_cw", 0))
                        for event in heard:
                            call = event[3] if len(event) > 3 else "-"
                            self.events.append(f"BCAST {event[1]} RXMETADATA {value} "
                                               f"{int(SNR_REF_BW_HZ)} {call} {mode} {count}")
            except (KeyError, TypeError, ValueError, OverflowError):
                pass  # Measurement failure must not discard a decoded frame.
        return frames

    def broadcast(self, data, port=0):
        key = (port, hashlib.sha256(data).hexdigest())
        records = pending.get(key)
        if not records:
            return original_broadcast(self, data, port)
        metadata = records.popleft()
        if not records:
            del pending[key]
        wire = kiss_encode(metadata, port, KISS_RX_SNR) + kiss_encode(data, port)
        with self.lock:
            for client in list(self.clients):
                try:
                    client.sendall(wire)
                except OSError:
                    self.clients.discard(client)

    on_burst._pixelqso_receive_snr = True
    KissLink.on_burst = on_burst
    KissServer.broadcast = broadcast


def main() -> int:
    waterfall_port = None
    if "--pixelqso-waterfall-port" in sys.argv:
        index = sys.argv.index("--pixelqso-waterfall-port")
        try:
            waterfall_port = int(sys.argv[index + 1])
        except (IndexError, ValueError):
            raise SystemExit("--pixelqso-waterfall-port needs a UDP port")
        del sys.argv[index:index + 2]
    enable_advertised_receive_lengths()
    enable_receive_snr()
    if waterfall_port:
        enable_waterfall_audio(waterfall_port)
    from data2g.host import main as data2g_host_main

    sys.argv = ["data2g-host", *sys.argv[1:]]
    data2g_host_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Launch Data2G with TX-advertised burst lengths enabled on receive.

Data2G advertises up to 64 codewords per header, but its default live Engine
constructs an Accept table capped at 16 seconds. PixelQSO can transmit longer
frames using those same advertised modes. Apply the compatibility override in
this child process only, then hand control to Data2G's normal host CLI.
"""
from __future__ import annotations

import sys
import hashlib
import math
from collections import defaultdict, deque

from snr import KISS_RX_SNR, encode_rx_snr


def enable_advertised_receive_lengths() -> None:
    """Make Engine's receive table honor its protocol codeword maximum."""
    from data2g import modem

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
    enable_advertised_receive_lengths()
    enable_receive_snr()
    from data2g.host import main as data2g_host_main

    sys.argv = ["data2g-host", *sys.argv[1:]]
    data2g_host_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

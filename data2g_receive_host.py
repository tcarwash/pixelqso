"""Launch the upstream Data2G host with PixelQSO receive-SNR metadata."""
from __future__ import annotations

import sys
import hashlib
import math
from collections import defaultdict, deque

from snr import KISS_RX_SNR, encode_rx_snr


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
    enable_receive_snr()
    from data2g.host import main as data2g_host_main

    sys.argv = ["data2g-host", *sys.argv[1:]]
    data2g_host_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

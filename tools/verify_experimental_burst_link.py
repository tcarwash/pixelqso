"""Exercise the real Qt transmit pump, UDP audio input, and live decoder.

Uses temporary app data and loopback UDP only; no audio devices, CAT or PTT.
Run with QT_QPA_PLATFORM=offscreen .venv/bin/python tools/verify_experimental_burst_link.py
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch

import numpy as np
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import cardmodem as pixel
import experimental_burst_modem as burst


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("work/four-experimental-bursts-20261005/live-link.json"))
    args = parser.parse_args()
    qt = QApplication.instance() or QApplication([])
    windows, results = [], []
    ports = [free_udp_port(), free_udp_port()]
    while ports[1] == ports[0]:
        ports[1] = free_udp_port()
    with tempfile.TemporaryDirectory(prefix="pixelqso-burst-link-") as folder:
        try:
            for index in range(2):
                env = {"PIXELQSO_DATA_DIR": str(Path(folder) / str(index)),
                       "PIXELQSO_TEST_LINK": "1", "PIXELQSO_TEST_RX_PORT": str(ports[index]),
                       "PIXELQSO_TEST_TX_PORT": str(ports[1 - index])}
                with patch.dict(os.environ, env), patch("app.QTimer.singleShot"):
                    window = app.MainWindow()
                    window.show_experimental_modes.setChecked(True)
                    window.backend_combo.setCurrentIndex(window.backend_combo.findData("experimental"))
                    window.audio_test_mode.setChecked(True)
                    window.auto_connect_cat.setChecked(False)
                    window.receive_all_modes.setChecked(False)
                    window.resize(1320, 900)
                    window.show()
                    windows.append(window)
            tx, rx = windows
            received = []
            record = rx._record_received_card
            def capture(report):
                received.append(report)
                record(report)
            rx._record_received_card = capture
            for index, key in enumerate(burst.MODE_KEYS):
                card = pixel.example_card()
                card.card_id = 0x6B00 + index
                card.pixels = np.random.default_rng(935 + index).integers(0, 8, 1024).tolist()
                with patch("app.QTimer.singleShot"):
                    tx.mode_combo.setCurrentIndex(tx.mode_combo.findData(key))
                    rx.mode_combo.setCurrentIndex(rx.mode_combo.findData(key))
                if rx.rx_timer.isActive():
                    rx.rx_timer.stop()
                    rx._stop_local_receive_capture()
                rx.start_receive()
                tx._chosen_card = lambda: card
                count = len(received)
                started = time.monotonic()
                tx.transmit_exchange()
                deadline = started + 25
                activity_seen, preview_seen = False, False
                while time.monotonic() < deadline:
                    qt.processEvents()
                    activity_seen |= "RECEIVING" in rx.listen_indicator.text()
                    preview_seen |= (not rx.receive_preview_box.isHidden() and
                                     rx.receive_progress.maximum() == 100 and
                                     0 < rx.receive_progress.value() < 100)
                    if len(received) > count and not tx.tx_timer.isActive():
                        break
                    time.sleep(.005)
                assert len(received) == count + 1, f"{key}: no single verified receive: {rx.rx_status.text()}"
                report = received[-1]
                expected = pixel.unpack_indices(pixel.minimal_avatar_payload(card), 1024)
                assert report["pixels"] == expected, f"{key}: different pixels"
                assert report["assembly_event"]["verified_complete"], f"{key}: unverified receive"
                assert tx.repeat_count.value() == 1 and rx.repeat_count.value() == 1
                assert not tx.ptt_active and not rx.ptt_active
                assert not tx.rig.connected() and not rx.rig.connected()
                assert tx.data2g_session is None and rx.data2g_session is None
                row = {"mode": key, "verified": True, "pixels_match": True,
                       "activity_seen": activity_seen, "partial_preview_seen": preview_seen,
                       "elapsed_seconds": time.monotonic() - started,
                       "airtime_seconds": burst.seconds(key), "session_wall_cards": rx.session_wall_count}
                results.append(row)
                print(json.dumps(row), flush=True)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({"scope": "Two Qt windows, real transmit/UDP/live receive paths; no RF or sound-device validation",
                                              "trials": results}, indent=2) + "\n")
            rx.grab().save(str(args.output.parent / "live-received-card.png"))
        finally:
            for window in windows:
                window.rx_timer.stop()
                window._stop_local_receive_capture()
                window.close()
            qt.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

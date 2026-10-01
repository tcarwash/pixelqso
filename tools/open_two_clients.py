#!/usr/bin/env python3
"""Open two independent Pixel QSO application windows for UI and audio testing.

The launcher keeps each station's cards, QSO log, and saved station identity in
separate directories. It starts both real app.py processes and keeps them open
until both windows close (or Ctrl+C is pressed here).
"""
from __future__ import annotations
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
sys.path.insert(0, str(ROOT))
import cardmodem as modem


def write_demo_card(folder: Path, callsign: str, grid: str, card_id: int) -> None:
    card = modem.example_card()
    card.callsign, card.grid, card.card_id = callsign, grid, card_id
    cards_dir = folder / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    data = {"callsign": card.callsign, "grid": card.grid, "palette": card.palette,
            "pixels": card.pixels, "card_id": card.card_id,
            "base_pixels": card.pixels, "stamp_layers": []}
    filename = "".join(char for char in callsign if char.isalnum() or char in "-_") or "station"
    (cards_dir / f"{filename}-demo.json").write_text(json.dumps(data, indent=2) + "\n")


def reserve_local_ports() -> tuple[int, int]:
    sockets = []
    try:
        for _ in range(2):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return sockets[0].getsockname()[1], sockets[1].getsockname()[1]
    finally:
        for sock in sockets: sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path.home() / "PixelQSO-Two-Client-Test",
                        help="base folder for isolated client A and B data")
    parser.add_argument("--callsigns", nargs=2, default=("W7PXQ", "K6TEST"), metavar=("A", "B"))
    parser.add_argument("--grids", nargs=2, default=("CN87", "CM87"), metavar=("A", "B"))
    parser.add_argument("--no-demo-cards", action="store_true", help="do not add starter cards to either library")
    args = parser.parse_args()
    if not APP.is_file():
        parser.error(f"Pixel QSO app source not found: {APP}")
    base = args.data_dir.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    port_a, port_b = reserve_local_ports()
    stations = []
    for index, (callsign, grid) in enumerate(zip(args.callsigns, args.grids)):
        folder = base / ("station-a" if index == 0 else "station-b")
        folder.mkdir(parents=True, exist_ok=True)
        if not args.no_demo_cards:
            write_demo_card(folder, callsign.strip().upper(), grid.strip().upper(), 0x5100 + index)
        env = os.environ.copy()
        env.update({"PIXELQSO_DATA_DIR": str(folder),
                    "PIXELQSO_CALLSIGN": callsign.strip().upper(),
                    "PIXELQSO_GRID": grid.strip().upper(),
                    "PIXELQSO_AUDIO_TEST": "1",
                    "PIXELQSO_PROFILE": "turbo",
                    "PIXELQSO_TEST_LINK": "1",
                    "PIXELQSO_TEST_RX_PORT": str(port_a if index == 0 else port_b),
                    "PIXELQSO_TEST_TX_PORT": str(port_b if index == 0 else port_a)})
        log = (folder / "app.log").open("w", encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(APP)], cwd=ROOT, env=env,
                                   stdout=log, stderr=subprocess.STDOUT)
        stations.append((process, log, callsign.strip().upper(), folder))
        print(f"Started {callsign.upper()} window: pid {process.pid}; data {folder}", flush=True)
        time.sleep(0.4)
    print(f"Software audio link connected on localhost UDP ports {port_a} ↔ {port_b}; CAT, PTT, and audio devices are not required.", flush=True)
    print("Choose Automatic card exchange in both windows; set station A to Initiate and station B to Respond.", flush=True)
    print("Close both windows to finish, or press Ctrl+C here to close them.", flush=True)
    try:
        while any(process.poll() is None for process, _log, _call, _folder in stations):
            time.sleep(0.5)
    except KeyboardInterrupt:
        for process, _log, _call, _folder in stations:
            if process.poll() is None: process.terminate()
        for process, _log, _call, _folder in stations:
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill()
    finally:
        for _process, log, _call, _folder in stations:
            log.close()
    return max((process.returncode or 0 for process, _log, _call, _folder in stations), default=0)


if __name__ == "__main__":
    raise SystemExit(main())

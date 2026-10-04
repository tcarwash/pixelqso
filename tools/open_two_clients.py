#!/usr/bin/env python3
"""Open two independent Pixel QSO application windows for UI and audio testing.

The launcher keeps each station's cards, QSO log, and saved station identity in
separate directories. It starts both real app.py processes and keeps them open
until both windows close (or Ctrl+C is pressed here).
"""
from __future__ import annotations
import argparse
import json
import math
import os
import socket
import subprocess
import sys
import time
import uuid
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
sys.path.insert(0, str(ROOT))
import cardmodem as modem
from on_air import SessionCollector
from tools.on_air_test import token_file
from tools.websdr_loopback import WebSDRLoopback


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
    parser.add_argument("--callsigns", nargs=2, default=("AG7SU", "K6TEST"), metavar=("A", "B"))
    parser.add_argument("--grids", nargs=2, default=("CN85", "CM87"), metavar=("A", "B"))
    parser.add_argument("--no-demo-cards", action="store_true", help="do not add starter cards to either library")
    parser.add_argument("--on-air", action="store_true", help="A transmits and B receives through WebSDR; no software audio link")
    parser.add_argument("--websdr-url", help="Open this receiver in the browser; tune and enable audio there")
    parser.add_argument("--manual-loopback", action="store_true", help="Use an existing browser/audio route instead of creating a dedicated one")
    parser.add_argument("--browser", help="Chrome/Chromium executable for automatic WebSDR loopback")
    parser.add_argument("--rx-device", help="Exact Qt input-device description of the browser monitor/loopback")
    parser.add_argument("--tx-device", help="Exact Qt output-device description of the radio audio output")
    host_group = parser.add_mutually_exclusive_group()
    host_group.add_argument("--data2g-hosts", nargs=2, metavar=("A", "B"),
                            help="Remote Data2G command hosts for TX station A and RX station B")
    host_group.add_argument("--data2g-local-hosts", action="store_true",
                            help="Start a managed Data2G host inside each station")
    parser.add_argument("--data2g-command-ports", nargs=2, type=int, metavar=("A", "B"),
                        help="Data2G command TCP ports for stations A and B")
    parser.add_argument("--data2g-kiss-ports", nargs=2, type=int, metavar=("A", "B"),
                        help="Data2G KISS TCP ports for stations A and B")
    parser.add_argument("--data2g-mode", choices=("robust", "fast"), default="robust",
                        help="Data2G broadcast preset to select at both clients (default: robust)")
    parser.add_argument("--session", help="Shared test-session ID (a fresh ID is created by default)")
    parser.add_argument("--collector", help="Optional remote collector base URL (LAN/NetBird or HTTPS)")
    parser.add_argument("--token-file", type=Path, help="Credential file shared with the remote collector")
    parser.add_argument("--rx-filter-hz", nargs=2, type=float, metavar=("LOW", "HIGH"), help="Record the actual WebSDR audio-filter edges")
    parser.add_argument("--list-audio-devices", action="store_true", help="List exact Qt device names and exit")
    args = parser.parse_args()
    if args.list_audio_devices:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtMultimedia import QMediaDevices
        qt = QApplication([])
        for label, devices in (("Input", QMediaDevices.audioInputs()), ("Output", QMediaDevices.audioOutputs())):
            for device in devices:
                print(f"{label}: {device.description()}")
        return 0
    if args.collector and not args.token_file:
        parser.error("--collector requires --token-file")
    if args.rx_filter_hz and not (all(math.isfinite(x) for x in args.rx_filter_hz) and
                                 0 <= args.rx_filter_hz[0] < args.rx_filter_hz[1] <= 24000):
        parser.error("Receiver filter edges must satisfy 0 <= LOW < HIGH <= 24000 Hz")
    if (args.websdr_url or args.collector or args.rx_device or args.tx_device or args.manual_loopback or args.browser) and not args.on_air:
        parser.error("WebSDR/audio/collector options require --on-air")
    use_data2g = bool(args.data2g_hosts or args.data2g_local_hosts)
    if use_data2g and not args.on_air:
        parser.error("Data2G host options require --on-air")
    if args.data2g_hosts and args.tx_device:
        parser.error("Data2G owns radio audio output; configure it on station A's Data2G host")
    command_ports = (tuple(args.data2g_command_ports) if args.data2g_command_ports else
                     (8300, 8400) if args.data2g_local_hosts else (8300, 8300))
    kiss_ports = (tuple(args.data2g_kiss_ports) if args.data2g_kiss_ports else
                  (8100, 8200) if args.data2g_local_hosts else (8100, 8100))
    if use_data2g and (any(not 1 <= port <= 65535 for port in (*command_ports, *kiss_ports)) or
                       any(port >= 65535 for port in command_ports)):
        parser.error("Data2G ports must be between 1 and 65535")
    if use_data2g and any(command == kiss for command, kiss in zip(command_ports, kiss_ports)):
        parser.error("Each Data2G host needs distinct command and KISS ports")
    if args.data2g_local_hosts and len({*command_ports, command_ports[0] + 1,
                                        command_ports[1] + 1, *kiss_ports}) != 6:
        parser.error("Local station hosts need six distinct command, data, and KISS ports")
    if args.data2g_hosts and (args.data2g_hosts[0], command_ports[0], kiss_ports[0]) == (
            args.data2g_hosts[1], command_ports[1], kiss_ports[1]):
        parser.error("Station A and B need separate Data2G host instances and endpoints")
    if not APP.is_file():
        parser.error(f"Pixel QSO app source not found: {APP}")
    base = args.data_dir.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    collector = None
    session = args.session or uuid.uuid4().hex
    if args.on_air:
        from on_air import identifier
        identifier(session)
        credential = args.token_file or (base / "on-air-session.token")
        if args.collector:
            collector_url = args.collector
        else:
            collector = SessionCollector(base / "collector", token_file(credential))
            collector_url = f"http://127.0.0.1:{collector.port}"
        print(f"On-air session: {session}; recordings: {base / 'sessions' / session}", flush=True)
        if args.websdr_url and (args.manual_loopback or args.rx_device):
            webbrowser.open(args.websdr_url)
    port_a, port_b = reserve_local_ports()
    stations = []
    loopback = None
    try:
        if args.on_air and not args.manual_loopback and not args.rx_device:
            loopback = WebSDRLoopback(args.browser)
            args.rx_device = loopback.description
            loopback.launch(args.websdr_url or "https://www.websdr.org/",
                            base / "browser-profiles" / uuid.uuid4().hex)
            print(f"Automatic browser audio route ready: {loopback.description}", flush=True)
        for index, (callsign, grid) in enumerate(zip(args.callsigns, args.grids)):
            folder = (base / "on-air-stations" if args.on_air else base) / ("station-a" if index == 0 else "station-b")
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
            env["PIXELQSO_DATA2G_LOCAL_HOST"] = "1" if args.data2g_local_hosts else "0"
            if args.data2g_hosts:
                env.update({"PIXELQSO_DATA2G_HOST": args.data2g_hosts[index],
                            "PIXELQSO_DATA2G_COMMAND_PORT": str(command_ports[index]),
                            "PIXELQSO_DATA2G_KISS_PORT": str(kiss_ports[index]),
                            "PIXELQSO_DATA2G_MODE": args.data2g_mode})
            elif args.data2g_local_hosts:
                env.update({"PIXELQSO_DATA2G_HOST": "127.0.0.1",
                            "PIXELQSO_DATA2G_COMMAND_PORT": str(command_ports[index]),
                            "PIXELQSO_DATA2G_KISS_PORT": str(kiss_ports[index]),
                            "PIXELQSO_DATA2G_MODE": args.data2g_mode})
            if args.on_air:
                receiver = {"kind": "Data2G host receiving WebSDR audio" if use_data2g else "WebSDR browser audio",
                            "audio_filter_hz": args.rx_filter_hz}
                receiver["loopback"] = "automatic dedicated browser" if loopback else "operator-routed"
                if use_data2g and (loopback or args.rx_device):
                    receiver["host_audio_input"] = loopback.description if loopback else args.rx_device
                if args.websdr_url:
                    from urllib.parse import urlsplit, urlunsplit
                    url = urlsplit(args.websdr_url)
                    receiver["site"] = urlunsplit((url.scheme, url.hostname or "", url.path, "", ""))
                config = {"session_id": session, "station_id": "tx" if index == 0 else "websdr-rx",
                          "output_dir": str(base / "sessions"), "collector_url": collector_url,
                          "token_file": str(credential.resolve()),
                           "source": ("data2g-host-radio-tx" if use_data2g and index == 0 else
                                      "data2g-host-websdr-rx" if use_data2g else
                                      "radio-tx-reference" if index == 0 else "websdr-loopback (operator-routed)"),
                          "receiver": receiver if index else {}}
                config_path = folder / "on-air.json"
                config_path.write_text(json.dumps(config, indent=2) + "\n")
                env.update(PIXELQSO_AUDIO_TEST="0" if index == 0 else "1", PIXELQSO_TEST_LINK="0",
                           PIXELQSO_AUDIO_ROLE="radio-tx" if index == 0 else "websdr-rx",
                           PIXELQSO_ON_AIR_CONFIG=str(config_path))
                if index == 0 and args.tx_device:
                    env["PIXELQSO_OUTPUT_DEVICE"] = args.tx_device
                if index == 1 and args.rx_device and args.data2g_local_hosts:
                    env["PIXELQSO_INPUT_DEVICE"] = args.rx_device
                elif index == 1 and args.rx_device and not use_data2g:
                    env["PIXELQSO_INPUT_DEVICE"] = args.rx_device
            log = (folder / "app.log").open("w", encoding="utf-8")
            process = subprocess.Popen([sys.executable, str(APP)], cwd=ROOT, env=env,
                                       stdout=log, stderr=subprocess.STDOUT)
            stations.append((process, log, callsign.strip().upper(), folder))
            print(f"Started {callsign.upper()} window: pid {process.pid}; data {folder}", flush=True)
            time.sleep(0.4)
        if args.on_air:
            if args.data2g_hosts:
                print(f"Data2G {args.data2g_mode} mode: A={args.data2g_hosts[0]}:{command_ports[0]} TX, "
                      f"B={args.data2g_hosts[1]}:{command_ports[1]} RX. Each host must be a separate running Data2G instance.", flush=True)
                print("Configure radio/CAT/PTT/audio on host A. Tune/start WebSDR audio and select its loopback as host B's Data2G audio input" +
                      (f" ({loopback.description})." if loopback else
                       f" ({args.rx_device})." if args.rx_device else "; select the routed browser input on host B."), flush=True)
                print("Both PixelQSO windows connect to their host automatically and select the requested Data2G mode. Host B owns WebSDR audio capture; PixelQSO does not capture a duplicate local stream.", flush=True)
            elif args.data2g_local_hosts:
                print(f"Starting two local Data2G hosts in {args.data2g_mode} mode on command ports "
                      f"{command_ports[0]}/{command_ports[1]} and KISS ports {kiss_ports[0]}/{kiss_ports[1]}.", flush=True)
                print("Station A's host owns radio audio/PTT. Station B's host uses the selected WebSDR loopback input" +
                      (f" ({loopback.description})." if loopback else
                       f" ({args.rx_device})." if args.rx_device else "; choose an input in Station settings."), flush=True)
            else:
                print("A: configure Hamlib/CAT and radio output; transmit manually when ready. B is receive-only. Tune/start WebSDR audio in the browser." +
                      (" RX input is selected automatically." if loopback else " Select the browser loopback INPUT in B."), flush=True)
            print("Software audio transport is OFF. The local IP collector carries measurements only." +
                  (" Host events and local Data2G host recordings are saved per station." if use_data2g else
                   " Raw WAV/NPZ and JSONL events are saved per station."), flush=True)
        else:
            print(f"Software audio link connected on localhost UDP ports {port_a} ↔ {port_b}; CAT, PTT, and audio devices are not required.", flush=True)
            print("Choose Automatic card exchange in both windows; set station A to Initiate and station B to Respond.", flush=True)
        print("Close both windows to finish, or press Ctrl+C here to close them.", flush=True)
        while any(process.poll() is None for process, _log, _call, _folder in stations):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for process, _log, _call, _folder in stations:
            if process.poll() is None: process.terminate()
        for process, _log, _call, _folder in stations:
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        for _process, log, _call, _folder in stations:
            log.close()
        if collector:
            collector.close()
        if loopback:
            loopback.close()
    return max((process.returncode or 0 for process, _log, _call, _folder in stations), default=0)


if __name__ == "__main__":
    raise SystemExit(main())

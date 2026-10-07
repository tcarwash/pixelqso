#!/usr/bin/env python3
"""Launch paired PixelQSO TX and Northern Utah WebSDR RX windows."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.websdr_loopback import WebSDRLoopback

DEFAULT_URL = "https://www.sdrutah.org/"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--callsign", default=os.environ.get("PIXELQSO_CALLSIGN", "AG7SU"),
                        help="callsign prefilled in the TX window (default: AG7SU)")
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="WebSDR page opened in Chrome (default: Northern Utah landing page)")
    parser.add_argument("--browser", help="Chrome/Chromium executable")
    parser.add_argument("--rx-data-dir", type=Path,
                        help="isolated RX settings directory (defaults under app data)")
    parser.add_argument("--manual-loopback", action="store_true",
                        help="skip virtual audio/browser routing; configure RX input manually")
    args = parser.parse_args()

    python = sys.executable
    app = ROOT / "app.py"
    if not app.is_file():
        parser.error(f"PixelQSO app entry point not found: {app}")

    loopback = None
    browser_profile = Path.home() / ".local/share/PixelQSO/websdr-browser"
    children: list[subprocess.Popen] = []
    try:
        if not args.manual_loopback:
            loopback = WebSDRLoopback(browser=args.browser)
            loopback.launch(args.url, browser_profile, extra_args=("--autoplay-policy=no-user-gesture-required",))
            print(f"WebSDR Chrome audio routes to {loopback.description}", flush=True)
        else:
            browser = args.browser or shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
            if not browser:
                parser.error("Chrome/Chromium not found; pass --browser")
            children.append(subprocess.Popen([browser, "--new-window", args.url], start_new_session=True))

        tx_env = os.environ.copy()
        tx_env["PIXELQSO_CALLSIGN"] = args.callsign.upper()
        tx_env["PIXELQSO_AUDIO_ROLE"] = "station"
        children.append(subprocess.Popen([python, str(app)], cwd=ROOT, env=tx_env,
                                         start_new_session=True))

        rx_env = os.environ.copy()
        rx_env["PIXELQSO_AUDIO_ROLE"] = "websdr-rx"
        rx_env["PIXELQSO_DATA_DIR"] = str(args.rx_data_dir or (
            Path.home() / ".local/share/PixelQSO/websdr-rx"))
        if loopback:
            rx_env["PIXELQSO_INPUT_DEVICE"] = loopback.description
        else:
            rx_env.pop("PIXELQSO_INPUT_DEVICE", None)
        children.append(subprocess.Popen([python, str(app)], cwd=ROOT, env=rx_env,
                                         start_new_session=True))
        print(f"TX window callsign: {args.callsign.upper()}", flush=True)
        print("RX window is receive-only. Close this launcher with Ctrl+C to stop both windows and release loopback.", flush=True)

        while True:
            for child in children:
                code = child.poll()
                if code is not None:
                    print(f"A launched window/browser exited with status {code}; stopping the session.", flush=True)
                    return code
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        for child in reversed(children):
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for child in children:
            if child.poll() is None:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        if loopback:
            loopback.close()


if __name__ == "__main__":
    raise SystemExit(main())

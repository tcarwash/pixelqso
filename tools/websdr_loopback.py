"""Session-scoped Linux browser audio -> explicit Qt capture input."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
import uuid


class WebSDRLoopback:
    def __init__(self, browser=None):
        self.pactl = shutil.which("pactl")
        if not self.pactl:
            raise RuntimeError("Automatic loopback requires pactl and a running PipeWire/PulseAudio server; use --manual-loopback otherwise")
        candidates = (browser,) if browser else ("google-chrome", "chromium", "chromium-browser")
        self.browser = next((shutil.which(name) for name in candidates if name and shutil.which(name)), None)
        if not self.browser:
            raise RuntimeError("Automatic loopback needs Chrome/Chromium; pass --browser or use --manual-loopback")
        suffix = uuid.uuid4().hex[:10]
        self.sink = "pixelqso_websdr_" + suffix
        self.source = self.sink + "_rx"
        self.description = "PixelQSO-WebSDR-RX-" + suffix
        self.modules = []
        self.process = None
        self.browser_log = None
        self.stop = threading.Event()
        self.watcher = None
        self.routing_errors = 0
        try:
            self.modules.append(self._pulse("load-module", "module-null-sink",
                "sink_name=" + self.sink, "rate=48000", "channels=2",
                "sink_properties=device.description=" + self.sink).strip())
            # Qt intentionally excludes monitor sources from its input list.
            # A remapped source exposes the monitor as a normal capture input.
            self.modules.append(self._pulse("load-module", "module-remap-source",
                "master=" + self.sink + ".monitor", "source_name=" + self.source,
                "rate=48000", "channels=1", "channel_map=mono",
                "source_properties=device.description=" + self.description).strip())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                sources = json.loads(self._pulse("--format=json", "list", "sources"))
                if any(item["name"] == self.source for item in sources):
                    break
                time.sleep(.05)
            else:
                raise RuntimeError("Virtual WebSDR capture source did not become available")
        except Exception:
            self.close()
            raise

    def _pulse(self, *args):
        result = subprocess.run([self.pactl, *args], capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise RuntimeError("PipeWire/PulseAudio loopback operation failed; check pactl info")
        return result.stdout

    def launch(self, url, profile, extra_args=()):
        if self.process:
            raise RuntimeError("Browser already started")
        profile = Path(profile).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        self.browser_log = (profile / "browser.log").open("w")
        env = os.environ.copy()
        env["PULSE_SINK"] = self.sink
        env["PULSE_PROP"] = "application.id=" + self.sink
        self.process = subprocess.Popen([self.browser, "--user-data-dir=" + str(profile),
                "--no-first-run", "--no-default-browser-check", "--new-window", *extra_args, url],
                env=env, stdout=self.browser_log, stderr=subprocess.STDOUT, start_new_session=True)
        self.watcher = threading.Thread(target=self._route, name="WebSDR browser routing", daemon=True)
        self.watcher.start()

    def _route(self):
        while not self.stop.wait(.25):
            if self.process.poll() is not None:
                return
            try:
                self.route_once()
            except (RuntimeError, ValueError, OSError, StopIteration, subprocess.TimeoutExpired):
                self.routing_errors += 1

    def route_once(self):
        """Correct restored browser routes, touching only our process session."""
        sinks = json.loads(self._pulse("--format=json", "list", "sinks"))
        target = next(item["index"] for item in sinks if item["name"] == self.sink)
        streams = json.loads(self._pulse("--format=json", "list", "sink-inputs"))
        for stream in streams:
            properties = stream.get("properties", {})
            owned = properties.get("application.id") == self.sink
            if not owned and self.process:
                try:
                    owned = os.getsid(int(properties.get("application.process.id", "-1"))) == self.process.pid
                except (OSError, ValueError):
                    owned = False
            if owned and stream["sink"] != target:
                self._pulse("move-sink-input", str(stream["index"]), self.sink)

    def close(self):
        self.stop.set()
        if self.watcher:
            self.watcher.join(timeout=6)
        if self.process and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        if self.browser_log:
            self.browser_log.close()
        for module in reversed(self.modules):
            try:
                self._pulse("unload-module", module)
            except (RuntimeError, subprocess.TimeoutExpired):
                self.routing_errors += 1
        self.modules.clear()

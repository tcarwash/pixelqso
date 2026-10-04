"""Opt-in on-air recording and an annotation-only IP back channel.

Nothing received over IP is fed to a modem. Audio and decode events are
written by a bounded background queue, never on the Qt/audio thread.
"""
from __future__ import annotations

import hashlib
import copy
import hmac
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

import numpy as np
import cardmodem as modem


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("Session and station identifiers must use letters, digits, '-' or '_'")
    return value


def card_reference(card):
    mapping = modem._avatar_palette_map(card)
    pixels = bytes(mapping[int(p)] for p in card.pixels)
    return {"callsign": card.callsign.upper(), "grid": card.grid.upper(), "card_id": card.card_id & 65535,
            "width": card.width, "height": card.height,
            "palette_id": modem._avatar_palette_id(card),
            "pixel_sha256": hashlib.sha256(pixels).hexdigest()}


def decoded_reference(report):
    card = report.get("card") or {}
    pixels = report.get("pixels")
    if pixels is None:
        return None
    try:
        raster = bytes(pixels)
    except (TypeError, ValueError):
        return None  # Legacy previews can contain RGB lists instead of indices.
    return {key: card.get(key) for key in ("callsign", "grid", "card_id", "width", "height", "palette_id")} | {
        "pixel_sha256": hashlib.sha256(raster).hexdigest(),
        "exact": bool(card.get("exact"))}


def signal_data(audio, rate):
    """Uncalibrated digital levels and spectra; never call these RF dBm/SNR."""
    x = np.asarray(audio, dtype=np.float32)
    if not len(x) or rate <= 0 or not np.isfinite(x).all():
        raise ValueError("Audio must contain finite samples at a positive sample rate")
    hop = max(1, round(rate / 4))
    frames = []
    for start in range(0, len(x), hop):
        part = x[start:start + hop]
        frames.append({"sample_start": start, "sample_count": len(part),
                       "rms": float(np.sqrt(np.mean(part.astype(float) ** 2))),
                       "peak": float(np.max(np.abs(part))),
                       "dc": float(np.mean(part)),
                       "clipped_fraction": float(np.mean(np.abs(part) >= 32760 / 32768))})
    size = min(8192, 2 ** int(math.floor(math.log2(max(2, min(len(x), rate))))))
    window = np.hanning(size)
    spectra = []
    positions = list(range(0, len(x), hop))
    for start in positions:
        part = np.pad(x[start:start + size], (0, max(0, size - len(x[start:start + size]))))
        spectra.append(np.abs(np.fft.rfft(part * window)) ** 2 / max(float(np.sum(window ** 2)), 1e-12))
    return {"samples": len(x), "seconds": len(x) / rate, "sample_rate": rate,
            "rms": float(np.sqrt(np.mean(x.astype(float) ** 2))),
            "peak": float(np.max(np.abs(x))), "frames": frames,
            "level_units": "linear digital full scale; uncalibrated"}, {
                "sample_positions": np.asarray(positions),
                "frequency_hz": np.fft.rfftfreq(size, 1 / rate),
                "power": np.asarray(spectra, dtype=np.float32)}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Do not forward the session credential to another host.


class SessionCollector:
    """Authenticated append-only events, with no radio/control endpoints."""
    def __init__(self, folder, token, host="127.0.0.1", port=0):
        if len(token) < 24:
            raise ValueError("Use a generated session credential")
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, code, payload):
                raw = json.dumps(payload, allow_nan=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                return hmac.compare_digest(self.headers.get("Authorization", "").encode(),
                                           ("Bearer " + owner.token).encode())

            def do_GET(self):
                if not self.authorized():
                    return self.reply(401, {"error": "Session credential required"})
                parts = self.path.split("/")
                if len(parts) != 4 or parts[1] != "sessions" or parts[3] != "events":
                    return self.reply(404, {"error": "Unknown endpoint"})
                try:
                    session = identifier(parts[2])
                    path = owner.folder / (session + ".jsonl")
                    # Reads are bounded; a larger session is reviewed from disk.
                    with owner.lock:
                        if path.exists() and path.stat().st_size > 8 * 1024 * 1024:
                            return self.reply(413, {"error": "Export this session from collector disk"})
                        events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
                    self.reply(200, {"events": events})
                except ValueError:
                    self.reply(400, {"error": "Invalid session"})

            def do_POST(self):
                if not self.authorized():
                    return self.reply(401, {"error": "Session credential required"})
                if self.path != "/events":
                    return self.reply(404, {"error": "Unknown endpoint"})
                try:
                    self.connection.settimeout(3)
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024 * 1024:
                        return self.reply(413, {"error": "Event size limit exceeded"})
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("Truncated event")
                    event = json.loads(raw)
                    session = identifier(event["session_id"])
                    identifier(event["station_id"])
                    identifier(event["event_id"])
                    if event.get("schema") != 1 or not isinstance(event.get("kind"), str):
                        raise ValueError("Invalid event")
                    event["collector_utc_ns"] = time.time_ns()
                    line = json.dumps(event, allow_nan=False) + "\n"
                    with owner.lock:
                        with (owner.folder / (session + ".jsonl")).open("a") as file:
                            file.write(line)
                    self.reply(200, {"accepted": event["event_id"]})
                except (ValueError, KeyError, TypeError, TimeoutError):
                    self.reply(400, {"error": "Invalid event"})

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SessionRecorder:
    def __init__(self, config):
        self.config = dict(config)
        self.session = identifier(config["session_id"])
        self.station = identifier(config["station_id"])
        self.folder = Path(config["output_dir"]) / self.session / self.station
        self.folder.mkdir(parents=True, exist_ok=True)
        self.url = config.get("collector_url", "").rstrip("/")
        self.token = ""
        if self.url:
            url = urlsplit(self.url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query or url.fragment:
                raise ValueError("Collector must be an http(s) base URL without credentials or query parameters")
            self.token = Path(config["token_file"]).read_text().strip()
            if len(self.token) < 24:
                raise ValueError("Invalid session credential")
        self.jobs = queue.Queue(maxsize=32)
        self.lock = threading.Lock()
        self.pending_bytes = 0
        self.dropped = 0
        self.errors = 0
        self.network_errors = 0
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="PixelQSO capture", daemon=True)
        self.thread.start()

    @classmethod
    def from_environment(cls):
        path = os.environ.get("PIXELQSO_ON_AIR_CONFIG")
        return cls(json.loads(Path(path).read_text())) if path else None

    def emit(self, kind, details=None, pcm=None, rate=48000):
        event = {"schema": 1, "session_id": self.session, "station_id": self.station,
                 "event_id": uuid.uuid4().hex, "kind": kind,
                 "utc_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
                 "source": self.config.get("source", "radio-audio"),
                 "details": copy.deepcopy(details or {})}
        size = len(pcm) if pcm else 0
        with self.lock:
            if self.closed or self.pending_bytes + size > 64 * 1024 * 1024:
                self.dropped += 1
                return None
            try:
                self.jobs.put_nowait((event, pcm, rate, size))
            except queue.Full:
                self.dropped += 1
                return None
            self.pending_bytes += size
        return event["event_id"]

    def _run(self):
        opener = build_opener(ProxyHandler({}), NoRedirect())
        while True:
            job = self.jobs.get()
            if job is None:
                self.jobs.task_done()
                return
            event, pcm, rate, size = job
            try:
                if pcm:
                    stem = f'{event["utc_ns"]}-{event["kind"]}-{event["event_id"]}'
                    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
                    with wave.open(str(self.folder / (stem + ".wav")), "wb") as file:
                        file.setnchannels(1); file.setsampwidth(2); file.setframerate(rate)
                        file.writeframes(pcm)
                    levels, spectra = signal_data(audio, rate)
                    np.savez_compressed(self.folder / (stem + ".npz"), **spectra)
                    event["audio"] = {**levels, "wav": stem + ".wav", "spectra": stem + ".npz"}
                with (self.folder / "events.jsonl").open("a") as file:
                    file.write(json.dumps(event, allow_nan=False) + "\n")
                if self.url:
                    request = Request(self.url + "/events", data=json.dumps(event, allow_nan=False).encode(),
                                      headers={"Authorization": "Bearer " + self.token,
                                               "Content-Type": "application/json"})
                    try:
                        with opener.open(request, timeout=2) as response:
                            response.read(4096)
                    except Exception:
                        self.network_errors += 1  # Local evidence remains available.
            except Exception:
                self.errors += 1
            finally:
                with self.lock:
                    self.pending_bytes -= size
                self.jobs.task_done()

    def close(self, timeout=10):
        with self.lock:
            self.closed = True
        try:
            self.jobs.put(None, timeout=timeout)
        except queue.Full:
            return False
        self.thread.join(timeout)
        return not self.thread.is_alive()

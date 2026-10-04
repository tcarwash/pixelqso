#!/usr/bin/env python3
"""Record WebSDR audio, replay captures, and review shared on-air sessions."""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import cardmodem as modem
from card_backends import audio_backend_keys, get_backend
from on_air import SessionCollector, SessionRecorder, signal_data, decoded_reference, identifier
from on_air import NoRedirect


def read_config(path):
    return json.loads(Path(path).read_text())


def token_file(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            file.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text().strip()


def analyze(wav, output, mode, placement, config=None):
    if mode not in audio_backend_keys():
        raise ValueError("Data2G host modes receive KISS frames; replay local modem WAVs with an audio backend")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    audio, rate = modem.read_wav(wav)
    levels, spectra = signal_data(audio, rate)
    np.savez_compressed(output / "spectra.npz", **spectra)
    backend = get_backend(mode, placement)
    started = time.monotonic()
    report, error = None, None
    try:
        report = backend.decode(audio, rate)
    except ValueError as exc:
        error = str(exc)
    result = {"source_wav": str(Path(wav).resolve()), "sample_rate": rate,
              "source": "recorded-audio; RF provenance supplied by operator",
              "modem": mode, "placement": placement,
              "decode_seconds": time.monotonic() - started,
              "audio": levels, "report": report, "error": error,
              "reference": decoded_reference(report) if report else None}
    if report and (report.get("card") or {}).get("acquisition") and hasattr(backend, "profile"):
        acquired = report["card"]["acquisition"]
        scale = rate / acquired["sample_rate"]
        metrics = modem.demodulate_metrics(audio, rate, start_sample=round(acquired["start_sample"] * scale),
                    frequency_offset_hz=acquired["frequency_offset_hz"],
                    symbol_period_samples=acquired["symbol_period_samples"] * scale,
                    profile=backend.profile)
        np.savez_compressed(output / "symbol_metrics.npz", metrics=metrics,
                            tones_hz=np.asarray(backend.profile.tones_hz),
                            symbols=np.argmax(metrics, axis=1))
        result["symbol_metrics"] = "symbol_metrics.npz (raw-capture tone energies using recovered timing)"
    (output / "analysis.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    if config:
        recorder = SessionRecorder(config)
        recorder.emit("offline_decode", result)
        if not recorder.close():
            raise RuntimeError("Session recorder did not finish")
    print(json.dumps({"result": "exact" if report and report["card"].get("exact") else "partial" if report else "no decode",
                      "output": str(output), "error": error}, indent=2))
    return result


def session_report(folder, output):
    paths = list(Path(folder).rglob("events.jsonl"))
    # Also accept the collector's SESSION.jsonl files.
    paths += [p for p in Path(folder).glob("*.jsonl") if p not in paths]
    events = {}
    for path in paths:
        for line in path.read_text().splitlines():
            if line.strip():
                item = json.loads(line)
                events[item["event_id"]] = item
    rows = sorted(events.values(), key=lambda e: e["utc_ns"])
    transmissions = [e for e in rows if e["kind"] in {"tx_start", "host_tx_start"}]
    matches = []
    for event in rows:
        if event["kind"] not in {"rx_decode", "offline_decode", "host_rx_card"}:
            continue
        reference = event["details"].get("reference")
        if not reference or not reference.get("exact"):
            continue
        candidates = [tx for tx in transmissions if tx["session_id"] == event["session_id"]
                      and tx["utc_ns"] <= event["utc_ns"]
                      and all((tx["details"].get("reference") or {}).get(k) == reference.get(k)
                              for k in ("callsign", "grid", "card_id", "width", "height", "palette_id", "pixel_sha256"))]
        if candidates:
            tx = max(candidates, key=lambda e: e["utc_ns"])
            matches.append({"tx_event_id": tx["event_id"], "rx_event_id": event["event_id"],
                            "verified_reference_match": True,
                            "observed_seconds": (event["utc_ns"] - tx["utc_ns"]) / 1e9})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "session.json").write_text(json.dumps({"events": rows, "matches": matches}, indent=2) + "\n")
    table = []
    for event in rows:
        details = event["details"]
        report = details.get("report") or {}
        card = report.get("card") or {}
        values = (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(event["utc_ns"] / 1e9)),
                  event["station_id"], event["kind"],
                  card.get("color_stage") or details.get("modem") or details.get("error") or "",
                  card.get("measured_snr_db", ""),
                  (card.get("acquisition") or {}).get("frequency_offset_hz", ""))
        table.append("<tr>" + "".join("<td>" + html.escape(str(value)) + "</td>" for value in values) + "</tr>")
    page = """<!doctype html><meta charset="utf-8"><title>Pixel QSO on-air session</title>
<style>body{font:15px system-ui;background:#11161a;color:#e8eee9;margin:32px}table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #35464e;text-align:left}p{max-width:85ch}</style>
<h1>Pixel QSO on-air session</h1><p>Raw audio is local; the IP link carries annotations only.
Levels are digital full scale, not calibrated RF power. Modem SNR estimates retain the decoder's definition.
UTC ordering across machines depends on clock synchronization. Observed TX-to-decode times include
WebSDR, browser, audio and decoder buffering; they are not RF propagation delay.</p>"""
    page += f"<p>{len(rows)} events; {len(matches)} exact receive events matched a TX raster hash and identity.</p>"
    page += "<table><tr><th>UTC</th><th>Station</th><th>Event</th><th>Status</th><th>Modem SNR</th><th>CFO Hz</th></tr>" + "".join(table) + "</table>"
    (output / "report.html").write_text(page)
    print(f"Session report: {output / 'report.html'}")
    return rows, matches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a client config for a local or remote session")
    init.add_argument("--config", type=Path, required=True)
    init.add_argument("--output-dir", type=Path, required=True)
    init.add_argument("--session", default=None)
    init.add_argument("--station", required=True)
    init.add_argument("--collector", default="")
    init.add_argument("--token-file", type=Path)
    serve = commands.add_parser("serve", help="Start an authenticated telemetry collector")
    serve.add_argument("--output-dir", type=Path, required=True)
    serve.add_argument("--token-file", type=Path, required=True)
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    capture = commands.add_parser("capture", help="Capture WebSDR browser audio from a loopback input")
    capture.add_argument("--device", required=True, help="sounddevice input index or name; select a monitor/loopback")
    capture.add_argument("--seconds", type=float, default=90)
    capture.add_argument("--rate", type=int, default=48000)
    capture.add_argument("--channels", type=int, choices=(1, 2), default=1)
    capture.add_argument("--wav", type=Path, required=True)
    devices = commands.add_parser("devices", help="List capture inputs (requires loopback extra)")
    replay = commands.add_parser("analyze", help="Replay a downloaded or loopback WebSDR PCM WAV")
    replay.add_argument("--wav", type=Path, required=True)
    replay.add_argument("--output-dir", type=Path, required=True)
    replay.add_argument("--mode", choices=audio_backend_keys(), required=True)
    replay.add_argument("--placement", choices=("near_carrier", "centered", "custom"), default="near_carrier")
    replay.add_argument("--low-hz", type=int, default=300)
    replay.add_argument("--config", type=Path)
    review = commands.add_parser("report", help="Merge station/collector events into a review report")
    review.add_argument("--session-dir", type=Path, required=True)
    review.add_argument("--output-dir", type=Path, required=True)
    fetch = commands.add_parser("fetch", help="Fetch peer events from the authenticated session collector")
    fetch.add_argument("--config", type=Path, required=True)
    fetch.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "init":
        if args.collector and not args.token_file:
            parser.error("--collector requires --token-file")
        config = {"session_id": identifier(args.session or uuid.uuid4().hex),
                  "station_id": identifier(args.station), "output_dir": str(args.output_dir.resolve()),
                  "collector_url": args.collector, "source": "radio-audio"}
        if args.token_file:
            token_file(args.token_file)
            config["token_file"] = str(args.token_file.resolve())
        args.config.parent.mkdir(parents=True, exist_ok=True)
        args.config.write_text(json.dumps(config, indent=2) + "\n")
        print(f"Client config: {args.config}; session: {config['session_id']}")
    elif args.command == "serve":
        collector = SessionCollector(args.output_dir, token_file(args.token_file), args.bind, args.port)
        print(f"Collector listening on {args.bind}:{collector.port}; credentials stay in the token file", flush=True)
        try:
            while True:
                time.sleep(.5)
        except KeyboardInterrupt:
            collector.close()
    elif args.command == "devices":
        import sounddevice as sd
        print(sd.query_devices())
    elif args.command == "capture":
        if not 0 < args.seconds <= 3600 or not 8000 <= args.rate <= 96000:
            parser.error("Capture requires 0–3600 seconds and 8000–96000 Hz")
        import sounddevice as sd
        device = int(args.device) if args.device.isdigit() else args.device
        args.wav.parent.mkdir(parents=True, exist_ok=True)
        import wave
        overflows = 0
        with wave.open(str(args.wav), "wb") as file:
            file.setnchannels(args.channels); file.setsampwidth(2); file.setframerate(args.rate)
            with sd.InputStream(device=device, samplerate=args.rate, channels=args.channels, dtype="int16") as stream:
                remaining = round(args.seconds * args.rate)
                while remaining:
                    count = min(remaining, args.rate // 4)
                    data, overflow = stream.read(count)
                    overflows += int(overflow)
                    file.writeframes(data.astype("<i2").tobytes())
                    remaining -= count
        args.wav.with_suffix(".capture.json").write_text(json.dumps({"source": "browser-loopback",
                    "utc_finished_ns": time.time_ns(), "input_overflows": overflows, "sample_rate": args.rate,
                    "channels": args.channels}, indent=2) + "\n")
        print(f"Captured {args.wav}; input overflows: {overflows}")
    elif args.command == "analyze":
        analyze(args.wav, args.output_dir, args.mode, args.low_hz if args.placement == "custom" else args.placement,
                read_config(args.config) if args.config else None)
    elif args.command == "report":
        session_report(args.session_dir, args.output_dir)
    elif args.command == "fetch":
        from urllib.request import Request, build_opener, ProxyHandler
        config = read_config(args.config)
        session = identifier(config["session_id"])
        request = Request(config["collector_url"].rstrip("/") + f"/sessions/{session}/events",
                          headers={"Authorization": "Bearer " + Path(config["token_file"]).read_text().strip()})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
            events = json.load(response)["events"]
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / (session + ".jsonl")).write_text("".join(json.dumps(event) + "\n" for event in events))
        print(f"Fetched {len(events)} peer events into {args.output_dir}")


if __name__ == "__main__":
    main()

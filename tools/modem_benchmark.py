#!/usr/bin/env python3
"""Compare application modem backends over seeded channels or captured WAVs."""
from __future__ import annotations
import argparse
import csv
import hashlib
import html
import json
import math
import platform
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cardmodem as modem
from card_backends import BACKENDS, is_available
from two_client_loopback import frequency_shift, load_card
from tools.synthetic_channel import ReceiveFilter, noise_sigma

PROFILES = {
    "clean": {},
    "awgn": {},
    "tuning": {"cfo_hz": 80, "clock_ppm": 100},
    "fading": {"fade_depth": .95, "fade_hz": .7},
    "echo": {"echo_ms": 2, "echo_gain": .6},
    "interference": {"tone_hz": 1500, "tone_ratio": 1},
    "clipping": {"clip_level": .4},
    "late-entry": {"drop_start": .15},
    "mixed": {"cfo_hz": 40, "clock_ppm": 50, "fade_depth": .8,
              "fade_hz": .5, "echo_ms": 1, "echo_gain": .4},
}


def channel(samples, spec, snr, seed, *, snr_reference_hz=2500., rx_filter=None):
    rng = np.random.default_rng(seed)
    x = np.asarray(samples, dtype=np.float64).copy()
    # Equal average transmit power for all backends. SNR uses this reference,
    # before fading/echo/clipping; added noise remains fixed through fades.
    rms = float(np.sqrt(np.mean(x*x)))
    if not math.isfinite(rms) or rms == 0:
        raise ValueError("empty or silent transmit waveform")
    x *= .1 / rms
    ratio = 1 + spec.get("clock_ppm", 0) / 1e6
    if ratio != 1:
        x = np.interp(np.arange(0, len(x), ratio), np.arange(len(x)), x)
    x = frequency_shift(x, modem.SAMPLE_RATE, spec.get("cfo_hz", 0)).astype(float)
    t = np.arange(len(x)) / modem.SAMPLE_RATE
    phase = rng.uniform(0, 2*np.pi)
    x *= 1 - spec.get("fade_depth", 0) * (.5 + .5*np.sin(2*np.pi*spec.get("fade_hz", 0)*t + phase))
    delay = round(spec.get("echo_ms", 0) * modem.SAMPLE_RATE / 1000)
    if 0 < delay < len(x):
        x[delay:] += spec.get("echo_gain", 0) * x[:-delay].copy()
    level = spec.get("clip_level")
    if level is not None:
        x = np.clip(x, -.1*level, .1*level)
    interference = .1 * math.sqrt(2) * spec.get("tone_ratio", 0) * np.sin(2*np.pi*spec.get("tone_hz", 0)*t + phase)
    dropped = int(len(x)*spec.get("drop_start", 0))
    interference = interference[dropped:]
    x = x[int(len(x)*spec.get("drop_start", 0)):]
    # Receiver leading/trailing silence includes channel noise, aiding flush.
    pad = modem.SAMPLE_RATE
    sigma = noise_sigma(snr, snr_reference_hz, modem.SAMPLE_RATE)
    signal = np.pad(x, (pad, pad))
    interference = np.pad(interference, (pad, pad))
    noise = rng.normal(0, sigma, len(signal))
    rx_filter = rx_filter or ReceiveFilter(modem.SAMPLE_RATE)
    signal = rx_filter.apply(signal)
    interference = rx_filter.apply(interference)
    noise = rx_filter.apply(noise)
    active = slice(pad, pad + len(x))
    signal_power = float(np.mean(signal[active] ** 2))
    noise_power = float(np.mean(noise[active] ** 2))
    reference_noise_power = noise_power * snr_reference_hz / rx_filter.enbw_hz
    measured_reference = (10 * math.log10(signal_power / reference_noise_power)
                          if signal_power > 0 and reference_noise_power > 0 else None)
    measured_passband = (10 * math.log10(signal_power / noise_power)
                         if signal_power > 0 and noise_power > 0 else None)
    measurements = {"snr_reference_hz": snr_reference_hz,
                    "snr_db_2500": snr if snr_reference_hz == 2500 else None,
                    "snr_db_sampled": (snr + 10 * math.log10(snr_reference_hz / (modem.SAMPLE_RATE / 2))
                                       if snr is not None else None),
                    "measured_snr_db_reference": measured_reference,
                    "measured_snr_db_rx": measured_passband,
                    "rx_signal_power": signal_power, "rx_noise_power": noise_power,
                    "rx_interference_power": float(np.mean(interference[active] ** 2))}
    return (signal + interference + noise).astype(np.float32), measurements


def evaluate(backend, card, audio, rate):
    start = time.perf_counter()
    row = {"identity_ok": False, "full_recovery": False, "verified_recovery": False,
           "pixel_accuracy": 0., "coverage": 0., "false_verified": False, "error": None}
    try:
        result = backend.decode(audio, rate)
        info, actual = result["card"], result["pixels"]
        expected = [modem._avatar_palette_map(card)[int(p)] for p in card.pixels]
        coverage = info.get("pixel_coverage", [True]*len(actual))
        hits = sum(i < len(actual) and i < len(coverage) and coverage[i] and actual[i] == value
                   for i, value in enumerate(expected))
        identity = (info.get("callsign") == card.callsign and info.get("grid") == card.grid
                    and info.get("width") == card.width and info.get("height") == card.height)
        full = identity and actual == expected and all(coverage) and len(coverage) == len(expected)
        row.update(identity_ok=identity, full_recovery=full,
                   verified_recovery=bool(info.get("exact")) and full,
                   false_verified=bool(info.get("exact")) and not full,
                   pixel_accuracy=hits/len(expected),
                   coverage=sum(bool(v) for v in coverage[:len(expected)])/len(expected))
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    row["decode_seconds"] = time.perf_counter() - start
    return row


def wilson(successes, n):
    z = 1.96
    p = successes/n
    center = (p + z*z/(2*n))/(1 + z*z/n)
    radius = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/(1 + z*z/n)
    return [max(0, center-radius), min(1, center+radius)]


def report(rows, destination, metadata):
    destination.mkdir(parents=True, exist_ok=True)
    groups = {}
    for row in rows:
        key = (row["source"], row["backend"], row["profile"], row["snr_db"],
               row["snr_reference_hz"], row["repeats"], row["card_hash"])
        groups.setdefault(key, []).append(row)
    summary = []
    for key, trials in groups.items():
        n = len(trials)
        successes = sum(r["full_recovery"] for r in trials)
        airtime = sum(r["airtime_seconds"] for r in trials)
        def mean_available(field):
            values = [r[field] for r in trials if r.get(field) is not None]
            return sum(values)/len(values) if values else None
        summary.append(dict(zip(("source", "backend", "profile", "snr_db", "snr_reference_hz", "repeats", "card_hash"), key),
                            trials=n, recovery_rate=successes/n, recovery_ci95=wilson(successes, n),
                            verified_rate=sum(r["verified_recovery"] for r in trials)/n,
                            mean_pixel_accuracy=sum(r["pixel_accuracy"] for r in trials)/n,
                            mean_coverage=sum(r["coverage"] for r in trials)/n,
                            mean_airtime_seconds=airtime/n,
                            within_5s=all(r["airtime_seconds"] <= 5 for r in trials),
                            mean_measured_snr_db_reference=mean_available("measured_snr_db_reference"),
                            mean_measured_snr_db_rx=mean_available("measured_snr_db_rx"),
                            mean_decode_seconds=sum(r["decode_seconds"] for r in trials)/n,
                            useful_pixel_bits_per_second=sum(r["payload_bits"] for r in trials if r["full_recovery"])/airtime,
                            false_verified=sum(r["false_verified"] for r in trials)))
    document = {"schema_version": 2, "metadata": metadata, "summary": summary, "trials": rows}
    (destination / "results.json").write_text(json.dumps(document, indent=2, allow_nan=False)+"\n")
    for name, records in (("trials", rows), ("summary", summary)):
        if records:
            with (destination / f"{name}.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(records[0]))
                writer.writeheader()
                writer.writerows(records)
    columns = ["source", "backend", "profile", "snr_db", "snr_reference_hz", "repeats", "trials", "recovery_rate",
               "recovery_ci95", "verified_rate", "mean_pixel_accuracy", "mean_coverage",
               "mean_airtime_seconds", "within_5s", "mean_measured_snr_db_reference", "mean_measured_snr_db_rx",
               "useful_pixel_bits_per_second", "mean_decode_seconds", "false_verified"]
    def cell(value):
        return html.escape(f"{value:.3f}" if isinstance(value, float) else str(value))
    table = "".join("<tr>"+"".join(f"<td>{cell(r[c])}</td>" for c in columns)+"</tr>" for r in summary)
    (destination / "report.html").write_text('<!doctype html><meta charset="utf-8"><title>Pixel QSO modem comparison</title>'
        '<style>body{font:14px system-ui;margin:24px}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #ccc}th{background:#eee}</style>'
        '<h1>Pixel QSO modem comparison</h1><p>Synthetic channels are approximations. Synthetic snr_db references '
        +html.escape(str(metadata["snr_reference_hz"]))+' Hz of white-noise power against 0.1 transmit RMS before impairments. '
        'Signal and noise pass through the same receive FIR. Its nominal passband is '
        +html.escape(str(metadata["receive_filter"]["low_hz"]))+'–'
        +html.escape(str(metadata["receive_filter"]["high_hz"]))+' Hz. Measured reference SNR corrects for the FIR equivalent noise bandwidth; measured receive SNR includes all noise passed by that FIR. Recorded captures retain their manifest SNR definitions and are not filtered. Recovery requires matching identity, dimensions and every expected pixel. Verified recovery additionally requires receiver integrity checks.</p>'
        '<p>95% Wilson intervals describe trial counts; few trials and correlated field captures limit conclusions. Goodput counts only complete cards and includes unsuccessful airtime.</p>'
        '<table><thead><tr>'+''.join(f'<th>{c}</th>' for c in columns)+'</tr></thead><tbody>'+table+'</tbody></table>'
        '<h2>Run metadata</h2><pre>'+html.escape(json.dumps(metadata, indent=2))+'</pre>')
    print(f"Wrote {len(rows)} trials to {destination / 'report.html'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backends", nargs="+", choices=list(BACKENDS), default=list(BACKENDS))
    parser.add_argument("--profiles", nargs="+", choices=list(PROFILES), default=["clean", "awgn", "mixed"])
    parser.add_argument("--snr-db", nargs="+", type=float, default=[12, 6, 0, -6])
    parser.add_argument("--snr-reference-hz", type=float, default=2500,
                        help="noise reference bandwidth for --snr-db (default: 2500 Hz)")
    parser.add_argument("--rx-low-hz", type=float, default=100, help="receive FIR lower edge (default: 100 Hz)")
    parser.add_argument("--rx-high-hz", type=float, default=3100, help="receive FIR upper edge (default: 3100 Hz)")
    parser.add_argument("--rx-filter-taps", type=int, default=1025)
    parser.add_argument("--repeats", nargs="+", type=int, default=[1, 3])
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--card", type=Path)
    parser.add_argument("--canvas", type=int, choices=[16, 32, 64], default=32,
                        help="generated eight-color card size; ignored with --card")
    parser.add_argument("--captures", type=Path, help="JSON manifest of real receive WAV trials")
    parser.add_argument("--save-audio", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "work" / "benchmark")
    args = parser.parse_args()
    if args.trials < 1 or any(not 1 <= r <= 20 for r in args.repeats) or any(not math.isfinite(s) for s in args.snr_db):
        parser.error("positive trials, repeats 1..20 and finite SNR required")
    if not math.isfinite(args.snr_reference_hz) or not 0 < args.snr_reference_hz <= modem.SAMPLE_RATE / 2:
        parser.error("SNR reference must be positive and no greater than Nyquist")
    try:
        rx_filter = ReceiveFilter(modem.SAMPLE_RATE, args.rx_low_hz, args.rx_high_hz, args.rx_filter_taps)
        for snr in args.snr_db:
            if not 0 < noise_sigma(snr, args.snr_reference_hz, modem.SAMPLE_RATE) < math.inf:
                raise ValueError("SNR gives an unrepresentable noise amplitude")
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))
    if args.card:
        card = load_card(args.card)
    else:
        card = modem.Card("W7PXQ", "CN87", list(modem.AVATAR_PALETTES[0]),
                          [(x*3+y*5)%8 for y in range(args.canvas) for x in range(args.canvas)],
                          0x51A7, args.canvas, args.canvas)
    card.validate()
    def fingerprint(c):
        return hashlib.sha256(json.dumps(vars(c), sort_keys=True).encode()).hexdigest()
    rows = []
    metadata = {"python": platform.python_version(), "numpy": np.__version__, "platform": platform.platform(),
                "seed": args.seed, "profiles": PROFILES, "argv": sys.argv[1:], "skipped": [],
                "snr_reference": "0.1 transmit RMS / white noise in snr_reference_hz, before impairments",
                "snr_reference_hz": args.snr_reference_hz, "sample_rate": modem.SAMPLE_RATE,
                "receive_filter": rx_filter.spec,
                "channel_order": "TX normalization, impairments, AWGN and receive padding, receive FIR",
                "measurement_window": "transmission after truncation; receive padding excluded",
                "benchmark_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "channel_helper_sha256": hashlib.sha256((ROOT / "tools" / "synthetic_channel.py").read_bytes()).hexdigest(),
                "capture_manifest": None}
    metadata["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                for name in ("cardmodem.py", "card_backends.py", "weak_signal_modem.py")
                                if (ROOT / name).exists()}
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    metadata["git_revision"] = revision.stdout.strip()
    metadata["git_dirty"] = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout)
    args.output.mkdir(parents=True, exist_ok=True)
    if not args.captures:
        (args.output / "expected-card.json").write_text(json.dumps(vars(card), indent=2)+"\n")
    try:
        if args.captures:
            entries = json.loads(args.captures.read_text())
            metadata["capture_manifest"] = entries
            for entry in entries:
                if not is_available(entry["backend"]):
                    raise RuntimeError(f"Capture backend {entry['backend']} requires its optional dependencies")
            for index, entry in enumerate(entries):
                backend = BACKENDS[entry["backend"]]
                expected = load_card(args.captures.parent / entry["card"])
                audio, rate = modem.read_wav(args.captures.parent / entry["wav"])
                airtime = float(entry["airtime_seconds"])
                if not math.isfinite(airtime) or airtime <= 0:
                    raise ValueError("capture airtime_seconds must be positive and finite")
                row = dict(source="recorded", backend=backend.key, profile=entry["path"], snr_db=entry.get("snr_db"),
                           snr_reference_hz=entry.get("snr_reference_hz"),
                           repeats=entry["repeats"], card_hash=fingerprint(expected), trial=index, seed=None,
                           airtime_seconds=airtime, encode_seconds=None,
                           payload_bits=len(expected.pixels)*modem.AVATAR_PALETTE_BITS[{8:0,16:1,32:2}[len(expected.palette)]],
                           audio_hash=hashlib.sha256((args.captures.parent / entry["wav"]).read_bytes()).hexdigest())
                row.update(evaluate(backend, expected, audio, rate))
                rows.append(row)
                print(f"recorded {index}: {backend.key} recovered={row['full_recovery']}", flush=True)
        else:
            for key in args.backends:
                if not is_available(key):
                    metadata["skipped"].append(key + ": optional dependency unavailable")
                    continue
                backend = BACKENDS[key]
                for repeats in args.repeats:
                    if repeats > getattr(backend, "max_repeats", 20):
                        metadata["skipped"].append(f"{key}: {repeats} copies exceed backend maximum")
                        continue
                    start = time.perf_counter()
                    clean = backend.encode(card, repeats, "card", None)
                    encode_seconds = time.perf_counter() - start
                    for profile in args.profiles:
                        for snr in ([None] if profile == "clean" else args.snr_db):
                            for trial in range(args.trials):
                                seed = args.seed + trial
                                audio, measurements = channel(clean, PROFILES[profile], snr, seed,
                                                              snr_reference_hz=args.snr_reference_hz,
                                                              rx_filter=rx_filter)
                                row = dict(source="synthetic", backend=key, profile=profile, snr_db=snr,
                                           repeats=repeats, card_hash=fingerprint(card), trial=trial, seed=seed,
                                           airtime_seconds=len(clean)/modem.SAMPLE_RATE, encode_seconds=encode_seconds,
                                           payload_bits=len(card.pixels)*modem.AVATAR_PALETTE_BITS[{8:0,16:1,32:2}[len(card.palette)]],
                                           audio_hash=hashlib.sha256(audio.tobytes()).hexdigest())
                                row.update(measurements)
                                row.update(evaluate(backend, card, audio, modem.SAMPLE_RATE))
                                rows.append(row)
                                if args.save_audio:
                                    modem.write_wav(args.output / f"{key}-{profile}-{snr}-{repeats}-{trial}.wav", audio)
                                print(f"{key} {profile} SNR={snr} copies={repeats} trial={trial}: recovered={row['full_recovery']}", flush=True)
    finally:
        if rows:
            report(rows, args.output, metadata)
    if not rows:
        parser.error("no trials ran; check backend dependencies or manifest")


if __name__ == "__main__":
    main()

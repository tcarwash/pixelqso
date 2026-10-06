"""Small reproducible single-burst channel comparison, with JSON/HTML output.

Run: .venv/bin/python tools/benchmark_experimental_bursts.py --output work/bursts
This measures synthetic CPU decoding, not radio or audio-device performance.
"""
import argparse
from collections import defaultdict
import html
import json
from pathlib import Path
import sys
import time

import numpy as np
from scipy.signal import hilbert

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cardmodem as pixel
import experimental_burst_modem as burst
from backend_adapters import EXPERIMENTAL_ADAPTER, DecodeFailureKind
from card_backends import get_backend
from card_transfer import CardAssemblyService


def impair(audio, profile, snr_db, seed):
    source = np.asarray(audio, float)
    source = source / max(float(np.sqrt(np.mean(source ** 2))), 1e-12) * .1
    rng = np.random.default_rng(seed)
    if profile in {"echo", "mixed"}:
        delay = 96  # 2 ms at 48 kHz
        source = source + np.pad(.6 * source[:-delay], (delay, 0))
    if profile in {"fading", "mixed"}:
        t = np.arange(len(source)) / 48000
        source *= .55 + .45 * np.sin(2 * np.pi * 1.3 * t + .3)
    if profile == "tuning":
        source = (hilbert(source) * np.exp(2j * np.pi * 50 * np.arange(len(source)) / 48000)).real
        source = np.interp(np.arange(len(source)) * 1.00008, np.arange(len(source)), source)
    if profile in {"awgn", "mixed"}:
        # SNR is referenced to 2500 Hz and pre-impairment transmit RMS .1.
        sigma = .1 * 10 ** (-snr_db / 20) * np.sqrt(48000 / (2 * 2500))
        source += rng.normal(0, sigma, len(source))
    return np.pad(source, (16320, 12320))


def trial(key, audio, expected, profile, seed, snr_db):
    started = time.perf_counter()
    outcome = EXPERIMENTAL_ADAPTER.decode_capture(get_backend(key), audio, 48000)
    seconds = time.perf_counter() - started
    row = {"mode": key, "profile": profile, "seed": seed, "snr_db_2500": snr_db,
           "decode_seconds": seconds, "verified": False, "false_verified": False,
           "coverage_pixels": 0, "diagnostics": {}}
    if outcome.failure:
        row.update(error=outcome.failure.message, failure_kind=outcome.failure.kind.value)
        return row
    report = outcome.report
    service = CardAssemblyService()
    report, _ = service.merge_preview(report)
    card = report.get("card") or {}
    verified = bool(report["assembly_event"]["verified_complete"])
    matches = (expected is not None and report["pixels"] == expected["pixels"] and
               card.get("callsign") == expected["callsign"] and card.get("grid") == expected["grid"])
    row.update(verified=verified and matches, false_verified=verified and not matches,
               coverage_pixels=sum(card.get("pixel_coverage") or []),
               diagnostics=card.get("diagnostics") or report.get("diagnostics") or {},
               activity_only=bool(report.get("receive_activity")))
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--noise-controls", type=int, default=3)
    parser.add_argument("--snr-db", type=float, default=6)
    parser.add_argument("--modes", nargs="+", default=[*burst.MODE_KEYS,
                        "fast_avatar_fec_v4", "experimental_qpsk_5s"])
    args = parser.parse_args()
    if args.trials < 1 or args.noise_controls < 0:
        parser.error("trials must be positive and noise controls nonnegative")
    args.output.mkdir(parents=True, exist_ok=True)
    profiles = ("clean", "awgn", "tuning", "echo", "fading", "mixed")
    rows, descriptions = [], {}
    for key in args.modes:
        backend = get_backend(key)
        descriptions[key] = {"label": backend.label}
        for index in range(args.trials):
            card = pixel.example_card()
            card.card_id = 0x5700 + index
            if index:
                card.pixels = np.random.default_rng(907 + index).integers(0, 8, 1024).tolist()
            audio = backend.encode(card, 1, "cq", -9)
            descriptions[key]["airtime_seconds"] = len(audio) / 48000
            expected = {"pixels": pixel.unpack_indices(pixel.minimal_avatar_payload(card), 1024),
                        "callsign": card.callsign, "grid": card.grid}
            for profile in profiles:
                seed = 1030 + index
                source = impair(audio, profile, args.snr_db, seed)
                rows.append(trial(key, source, expected, profile, seed,
                                 args.snr_db if profile in {"awgn", "mixed"} else None))
        for index in range(args.noise_controls):
            seed = 9180 + index
            source = np.random.default_rng(seed).normal(0, .1, len(audio))
            rows.append(trial(key, source, None, "noise_only", seed, None))
        print(f"{key}: {sum(row['verified'] for row in rows if row['mode'] == key)} verified recoveries", flush=True)
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["mode"], row["profile"])].append(row)
    summary = [{"mode": key, "profile": profile, "trials": len(group),
                "verified": sum(row["verified"] for row in group),
                "false_verified": sum(row["false_verified"] for row in group),
                "mean_decode_seconds": sum(row["decode_seconds"] for row in group) / len(group)}
               for (key, profile), group in grouped.items()]
    details = {"scope": "Synthetic single-burst captures; no HF, live audio-device or packaged-app validation",
               "snr_reference_hz": 2500, "transmit_rms": .1, "sample_rate": 48000,
               "channel_definitions": {"echo": "0.6-amplitude echo delayed 2 ms",
                 "fading": "gain 0.55 + 0.45 sin(2 pi 1.3 t + 0.3)",
                 "mixed": "echo + fading + AWGN; noise referenced before path impairment",
                 "tuning": "+50 Hz CFO, +80 ppm sample-clock error"},
               "modes": descriptions, "summary": summary, "trials": rows}
    (args.output / "results.json").write_text(json.dumps(details, indent=2) + "\n")
    table = []
    for key in args.modes:
        cells = [html.escape(descriptions[key]["label"]), f"{descriptions[key]['airtime_seconds']:.3f}s"]
        for profile in (*profiles, "noise_only"):
            group = grouped[(key, profile)]
            cells.append(f"{sum(row['verified'] for row in group)}/{len(group)}")
        table.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    headers = ("Mode", "Airtime", *profiles, "noise-only verified")
    false = sum(row["false_verified"] for row in rows)
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>Single-burst modem comparison</title>
<style>body{{font:16px system-ui;background:#10191d;color:#e0eee8;margin:40px}}table{{border-collapse:collapse}}td,th{{padding:12px;border:1px solid #405452;text-align:left}}th{{background:#203933}}a{{color:#7ce0be}}</style>
<h1>Single-burst experimental modem comparison</h1>
<p>Synthetic captures only. Each recovery requires matching identity, all pixels, and receiver verification.</p>
<p>AWGN/mixed SNR: {args.snr_db:g} dB in 2500 Hz, referenced before channel impairments. Equal transmit RMS; airtime and energy differ.</p>
<p>Echo: amplitude 0.6, delay 2 ms. Fading: deep 1.3 Hz sinusoidal amplitude variation. Tuning: +50 Hz / +80 ppm.</p>
<table><tr>{''.join(f'<th>{html.escape(header)}</th>' for header in headers)}</tr>{''.join(table)}</table>
<p>{len(rows)} attempts; {false} false verified cards. Small samples describe these fixtures, not HF reliability.</p>
<p><a href="results.json">Full results, CPU timings, diagnostics, and failed attempts</a></p></html>"""
    (args.output / "report.html").write_text(page)
    backend_errors = sum(row.get("failure_kind") == DecodeFailureKind.BACKEND_ERROR.value for row in rows)
    print(json.dumps({"attempts": len(rows), "false_verified": false,
                      "backend_errors": backend_errors, "output": str(args.output)}))
    return int(bool(false or backend_errors))


if __name__ == "__main__":
    raise SystemExit(main())

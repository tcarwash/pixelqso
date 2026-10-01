#!/usr/bin/env python3
"""Repeatable AWGN reception sweep for the Pixel QSO reference modem."""
import argparse
import json
from pathlib import Path

import numpy as np

from cardmodem import (
    SAMPLE_RATE, acquire, collect_packet_evidence, decode_packet_evidence,
    demodulate_metrics, parse_symbols, read_wav, reconstruct,
)

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE / "examples"


def measure(snr_db: float, seed: int, clean: np.ndarray) -> dict:
    signal = 0.04 * clean
    signal_power = float(np.mean(signal * signal))
    noise_std = (signal_power / (10.0 ** (snr_db / 10.0))) ** 0.5
    audio = (signal + np.random.default_rng(seed).normal(0.0, noise_std, len(signal))).astype(np.float32)
    start, offset, period = acquire(audio, SAMPLE_RATE)
    metrics = demodulate_metrics(audio, SAMPLE_RATE, start_sample=start,
                                 frequency_offset_hz=offset, symbol_period_samples=period)
    hard_symbols = tuple(int(value) for value in np.argmax(metrics, axis=1))
    hard_packets, hard_errors = parse_symbols(hard_symbols)
    evidence = collect_packet_evidence(hard_symbols, metrics)
    soft_packets, soft_errors = decode_packet_evidence(evidence)
    card, _ = reconstruct(soft_packets)
    return {
        "snr_db": snr_db,
        "seed": seed,
        "soft_valid_packets": len(soft_packets),
        "hard_valid_packets": len(hard_packets),
        "soft_rejected_packets": len(soft_errors),
        "hard_rejected_packets": len(hard_errors),
        "color_stage": card["color_stage"],
        "estimated_baud": round(SAMPLE_RATE / period, 5),
        "estimated_carrier_offset_hz": offset,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=EXAMPLES / "awgn-sweep.json")
    args = parser.parse_args()
    clean, sample_rate = read_wav(EXAMPLES / "sent-w7pxq.wav")
    if sample_rate != SAMPLE_RATE:
        raise ValueError(f"expected {SAMPLE_RATE} Hz source WAV, got {sample_rate}")
    trials = [measure(snr, seed, clean) for snr in (-17.0, -15.0) for seed in range(5)]
    groups = {}
    for snr in (-17.0, -15.0):
        group = [row for row in trials if row["snr_db"] == snr]
        groups[str(int(snr))] = {
            "trials": group,
            "mean_soft_valid_packets": round(float(np.mean([r["soft_valid_packets"] for r in group])), 2),
            "mean_hard_valid_packets": round(float(np.mean([r["hard_valid_packets"] for r in group])), 2),
            "full_8_color_receptions": sum(r["color_stage"] == "8-color" for r in group),
        }
    report = {
        "source": "sent-w7pxq.wav",
        "channel": "additive white Gaussian noise added to signal attenuated to 0.04 amplitude",
        "seed_policy": "NumPy default_rng seeds 0 through 4 at each SNR",
        "receiver_clock_confidence_floor": 0.05,
        "results": groups,
        "warning": "Synthetic software channel measurements; not a radio-path performance claim.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: {k:v for k,v in value.items() if k != "trials"}
                      for key, value in groups.items()}, indent=2))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()

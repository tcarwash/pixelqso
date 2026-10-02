#!/usr/bin/env python3
"""Exercise one-way repeated Pixel QSO bursts over a synthetic or audio channel.

Examples:
  python3 tools/two_client_loopback.py --repeats 3
  python3 tools/two_client_loopback.py --canvas 16 --colors 32 --repeats 4 --snr-db 8
  python3 tools/two_client_loopback.py --audio-loopback --tx-device 3 --rx-device 4

These checks cover the local codec and audio path; they do not establish RF
performance or on-air compliance.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cardmodem as modem
from tools.synthetic_channel import ReceiveFilter, noise_sigma


def load_card(path: Path) -> modem.Card:
    data = json.loads(path.read_text())
    card = modem.Card(data["callsign"], data["grid"], [tuple(c) for c in data["palette"]],
                      data["pixels"], data.get("card_id", 0xC0DE),
                      data.get("width", 32), data.get("height", 32))
    card.validate()
    return card


def frequency_shift(samples: np.ndarray, rate: int, offset: float) -> np.ndarray:
    if not offset:
        return samples
    spectrum = np.fft.fft(samples)
    h = np.zeros(len(samples)); h[0] = 1
    if len(samples) % 2 == 0:
        h[1:len(samples)//2] = 2; h[len(samples)//2] = 1
    else:
        h[1:(len(samples)+1)//2] = 2
    analytic = np.fft.ifft(spectrum * h)
    time = np.arange(len(samples), dtype=np.float64) / rate
    return np.real(analytic * np.exp(2j * np.pi * offset * time)).astype(np.float32)


def impair(samples: np.ndarray, *, snr_db: float | None, cfo_hz: float, clock_ppm: float,
           drop_start: float, drop_end: float, seed: int, snr_reference_hz: float = 2500.,
           rx_filter: ReceiveFilter | None = None) -> np.ndarray:
    result = np.asarray(samples, dtype=np.float64).copy()
    power = float(np.mean(result * result))
    if not (0 <= drop_start < 1 and 0 <= drop_end < 1 and drop_start + drop_end < 1):
        raise ValueError("drop-start/drop-end must be fractions whose sum is below 1")
    left = int(len(result) * drop_start)
    right = len(result) - int(len(result) * drop_end)
    result = result[left:right]
    if clock_ppm:
        ratio = 1.0 + clock_ppm / 1_000_000.0
        source = np.arange(0, len(result), ratio)
        result = np.interp(source, np.arange(len(result)), result).astype(np.float32)
    result = frequency_shift(result, modem.SAMPLE_RATE, cfo_hz)
    if snr_db is not None:
        sigma = noise_sigma(snr_db, snr_reference_hz, modem.SAMPLE_RATE, power)
        result += np.random.default_rng(seed).normal(0, sigma, len(result))
    rx_filter = rx_filter or ReceiveFilter(modem.SAMPLE_RATE)
    return rx_filter.apply(result).astype(np.float32)


def card_with_identity(source: modem.Card, callsign: str, grid: str, card_id: int,
                       canvas: int, colors: int) -> modem.Card:
    palette_id = {8: 0, 16: 1, 32: 2}[colors]
    palette = list(modem.AVATAR_PALETTES[palette_id])
    pixels = [((x*3 + y*5) ^ (x//3) ^ (y//2)) % colors
              for y in range(canvas) for x in range(canvas)]
    if source.width == canvas and source.height == canvas and len(source.palette) == colors:
        palette, pixels = list(source.palette), list(source.pixels)
    return modem.Card(callsign, grid, palette, pixels, card_id, canvas, canvas)


def transmit_receive(sender: modem.Card, receiver: modem.Card, *, mode: str, repeats: int,
                     args, seed: int) -> dict:
    cycle = (modem.minimal_avatar_symbols(sender) if mode == "fast" else
             modem.minimal_avatar_resilient_cycle_symbols(sender))
    samples = modem.synthesize(cycle * repeats, profile=modem.MINIMAL_AVATAR_PROFILE)
    if args.audio_loopback:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise SystemExit("Install sounddevice for --audio-loopback: python3 -m pip install sounddevice") from exc
        received = sd.playrec(samples, samplerate=modem.SAMPLE_RATE, channels=1,
                              dtype="float32", device=(args.rx_device, args.tx_device),
                              blocking=True).reshape(-1)
    else:
        received = samples
    received = impair(received, snr_db=args.snr_db, cfo_hz=args.cfo_hz,
                      clock_ppm=args.clock_ppm, drop_start=args.drop_start,
                      drop_end=args.drop_end, seed=seed,
                      snr_reference_hz=args.snr_reference_hz, rx_filter=args.receive_filter)
    try:
        decoded_pixels, detail = modem.decode_minimal_avatar_audio_auto(received, modem.SAMPLE_RATE)
        exact = bool(detail.get("exact"))
        readable = bool(decoded_pixels)
        pixel_match = decoded_pixels == [modem._avatar_palette_map(sender)[int(value)]
                                         for value in sender.pixels]
        recovered = exact and pixel_match
        transfer_passed = recovered if detail.get("avatar_mode") == "fast_avatar_fec" else bool(
            detail.get("callsign") == sender.callsign and pixel_match)
    except (ValueError, RuntimeError, ZeroDivisionError) as exc:
        decoded_pixels, detail = [], {}
        readable = exact = recovered = False
        transfer_passed = pixel_match = False
        decode_error = str(exc)
    else:
        decode_error = None
    result = {
        "sender": sender.callsign,
        "receiver": receiver.callsign,
        "transmission_type": detail.get("avatar_mode", mode),
        "canvas": f"{sender.width}x{sender.height}",
        "colors": len(sender.palette),
        "repeats_sent": repeats,
        "cycle_seconds": round(len(cycle) / modem.MINIMAL_AVATAR_PROFILE.baud, 3),
        "total_airtime_seconds": round(len(samples) / modem.SAMPLE_RATE, 3),
        "identity_received": detail.get("callsign") == sender.callsign,
        "readable_pixels": readable,
        "pixel_match_unverified": bool(pixel_match) if detail.get("avatar_mode") != "fast_avatar_fec" else None,
        "all_resilient_blocks_verified": all(detail.get("received_blocks", [])) if detail.get("avatar_mode") == "fast_avatar_fec" else None,
        "exact_verified_recovery": bool(recovered),
        "transfer_passed": bool(transfer_passed),
        "pixel_integrity": "checked" if detail.get("avatar_mode") == "fast_avatar_fec" else "unverified",
        "received_blocks": detail.get("received_blocks"),
        "impairments": {"snr_db": args.snr_db, "snr_reference_hz": args.snr_reference_hz,
                        "snr_reference": "received waveform power before synthetic impairments",
                        "receive_filter": args.receive_filter.spec, "cfo_hz": args.cfo_hz,
                        "clock_ppm": args.clock_ppm, "drop_start": args.drop_start,
                        "drop_end": args.drop_end},
        "decode_error": decode_error,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fast", "resilient"), default="resilient",
                        help="fast sends unchecked pixels; resilient adds checked FEC blocks")
    parser.add_argument("--repeats", type=int, default=3,
                        help="number of identical one-way burst copies (1..20)")
    parser.add_argument("--canvas", type=int, choices=(16, 32, 64), default=32)
    parser.add_argument("--colors", type=int, choices=(8, 16, 32), default=8)
    parser.add_argument("--snr-db", type=float, default=None, help="synthetic AWGN SNR in --snr-reference-hz")
    parser.add_argument("--snr-reference-hz", type=float, default=2500)
    parser.add_argument("--rx-low-hz", type=float, default=100)
    parser.add_argument("--rx-high-hz", type=float, default=3100)
    parser.add_argument("--rx-filter-taps", type=int, default=1025)
    parser.add_argument("--cfo-hz", type=float, default=0.0, help="synthetic carrier offset")
    parser.add_argument("--clock-ppm", type=float, default=0.0, help="synthetic sample-clock drift")
    parser.add_argument("--drop-start", type=float, default=0.0, help="fraction of the opening waveform missed")
    parser.add_argument("--drop-end", type=float, default=0.0, help="fraction of the ending waveform missed")
    parser.add_argument("--audio-loopback", action="store_true", help="play/record using sound-card loopback")
    parser.add_argument("--tx-device", type=int, help="sounddevice output index")
    parser.add_argument("--rx-device", type=int, help="sounddevice input index")
    parser.add_argument("--output", type=Path, help="write the full JSON report")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("--repeats must be between 1 and 20")
    try:
        args.receive_filter = ReceiveFilter(modem.SAMPLE_RATE, args.rx_low_hz, args.rx_high_hz,
                                            args.rx_filter_taps)
        sigma = noise_sigma(args.snr_db, args.snr_reference_hz, modem.SAMPLE_RATE)
        if args.snr_db is not None and not 0 < sigma < math.inf:
            raise ValueError("SNR gives an unrepresentable noise amplitude")
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))
    if args.audio_loopback and (args.tx_device is None or args.rx_device is None):
        parser.error("--audio-loopback requires --tx-device and --rx-device")
    example = ROOT / "examples" / "w7pxq-card.json"
    source = load_card(example) if example.is_file() else modem.example_card()
    a = card_with_identity(source, "W7PXQ", "CN87", 0x51A7, args.canvas, args.colors)
    b = card_with_identity(source, "K6TEST", "CM87", 0x52A6, args.canvas, args.colors)
    transfers = [transmit_receive(a, b, mode=args.mode, repeats=args.repeats, args=args, seed=1),
                 transmit_receive(b, a, mode=args.mode, repeats=args.repeats, args=args, seed=2)]
    summary = {"mode": "audio loopback" if args.audio_loopback else "synthetic two-client channel",
               "transmission_type": args.mode, "repeats": args.repeats,
               "canvas": f"{args.canvas}x{args.canvas}", "colors": args.colors,
               "transfers": transfers,
               "warning": "Synthetic and local audio results do not establish RF performance or regulatory compliance."}
    print(json.dumps(summary, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if all(item["transfer_passed"] for item in transfers) else 1


if __name__ == "__main__":
    raise SystemExit(main())

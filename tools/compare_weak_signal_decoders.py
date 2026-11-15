"""Paired QPSK decode comparison: committed baseline versus working tree.

Example:
    .venv/bin/python tools/compare_weak_signal_decoders.py \
        --snr-db -10 -8 -6 -4 -2 --seeds 5 --profiles awgn hf_mild

Both decoders receive the same waveform and the same seeded channel capture.
The baseline receiver is loaded from HEAD; the working-tree encoder is used
after confirming that its generated waveform matches the baseline encoder.
"""
import argparse
import importlib.util
import math
import subprocess
import tempfile
from pathlib import Path

import numpy as np

import cardmodem
import weak_signal_modem as current
from hf_channel import PROFILES, simulate


def _load_baseline(ref):
    source = subprocess.check_output(
        ['git', 'show', f'{ref}:weak_signal_modem.py'], text=True)
    with tempfile.TemporaryDirectory(prefix='pixelqso-baseline-') as tmp:
        path = Path(tmp) / 'weak_signal_modem.py'
        path.write_text(source)
        spec = importlib.util.spec_from_file_location('weak_signal_modem_previous', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _exact_decode(receiver, capture, expected):
    try:
        pixels, metadata = receiver.decode(capture)
        return bool(metadata.get('exact')) and pixels == expected
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default='HEAD',
                        help='git ref for previous receiver source (default: HEAD)')
    parser.add_argument('--snr-db', type=float, nargs='+',
                        default=[-10, -8, -6, -4, -2, 0])
    parser.add_argument('--seeds', type=int, default=5)
    parser.add_argument('--copies', type=int, default=1,
                        help='repeat the same burst this many times with 100 ms gaps')
    parser.add_argument('--seed-start', type=int, default=1,
                        help='first channel seed (default: 1)')
    parser.add_argument('--profiles', nargs='+', choices=PROFILES, default=['awgn'])
    args = parser.parse_args()
    if args.seeds < 1 or args.copies < 1:
        parser.error('--seeds and --copies must be positive')

    previous = _load_baseline(args.baseline_ref)
    card = cardmodem.example_card()
    previous_audio = previous.encode(card, 1)
    current_audio = current.encode(card, 1)
    if previous_audio.shape != current_audio.shape or not np.array_equal(
            previous_audio, current_audio):
        raise SystemExit('baseline/current encoders differ; paired decode comparison is invalid')
    audio = current_audio * (.1 / math.sqrt(float(np.mean(current_audio ** 2))))
    if args.copies > 1:
        audio = np.concatenate([
            part for index in range(args.copies)
            for part in ((np.zeros(4800), audio) if index else (audio,))
        ])
    expected = cardmodem.unpack_indices(cardmodem.minimal_avatar_payload(card), 1024)

    for profile in args.profiles:
        for snr in args.snr_db:
            old_ok = new_ok = old_only = new_only = 0
            for seed in range(args.seed_start, args.seed_start + args.seeds):
                capture, _ = simulate(audio, 48000, profile=profile,
                                      snr_db=snr, seed=seed)
                old = _exact_decode(previous, capture, expected)
                new = _exact_decode(current, capture, expected)
                old_ok += old
                new_ok += new
                old_only += old and not new
                new_only += new and not old
            print(f'{profile:12s} {snr:>5g} dB copies={args.copies}  '
                  f'HEAD={old_ok}/{args.seeds}  '
                  f'current={new_ok}/{args.seeds}  '
                  f'current-only={new_only}  HEAD-only={old_only}')


if __name__ == '__main__':
    main()

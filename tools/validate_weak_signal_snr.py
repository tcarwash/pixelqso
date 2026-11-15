"""Measure exact QPSK recovery across seeded synthetic low-SNR channels.

Example:
    .venv/bin/python tools/validate_weak_signal_snr.py --snr-db -10 --seeds 5

The channel SNR is input power divided by noise power over the simulator's
2.5 kHz reference bandwidth. Profiles are illustrative simulations, not RF
performance claims.
"""
import argparse
import math

import numpy as np

import cardmodem
import weak_signal_modem
from hf_channel import PROFILES, simulate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snr-db', type=float, default=-10)
    parser.add_argument('--seeds', type=int, default=5,
                        help='number of deterministic channel seeds (default: 5)')
    parser.add_argument('--profiles', nargs='+', choices=PROFILES,
                        default=['awgn', 'hf_mild', 'hf_moderate'])
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error('--seeds must be positive')

    card = cardmodem.example_card()
    audio = weak_signal_modem.encode(card, 1)
    audio *= .1 / math.sqrt(float(np.mean(audio ** 2)))
    for profile in args.profiles:
        successes = 0
        for seed in range(1, args.seeds + 1):
            capture, _ = simulate(audio, 48000, profile=profile,
                                  snr_db=args.snr_db, seed=seed)
            try:
                pixels, metadata = weak_signal_modem.decode(capture)
                exact = bool(metadata.get('exact')) and pixels == cardmodem.unpack_indices(
                    cardmodem.minimal_avatar_payload(card), 1024)
            except (ValueError, KeyError):
                exact = False
            successes += exact
            print(f'{profile} seed={seed}: {"exact" if exact else "no exact decode"}')
        print(f'{profile} at {args.snr_db:g} dB: {successes}/{args.seeds} exact')


if __name__ == '__main__':
    main()

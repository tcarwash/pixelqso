"""Reproducible diverse-card threshold campaign using the actual backends."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cardmodem as pixel
import modem_benchmark as benchmark
import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--trials', type=int, default=20)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = ['cardmodem.py', 'card_backends.py', 'app.py', 'weak_signal_modem.py',
              'tools/modem_benchmark.py', 'tools/synthetic_channel.py', 'tools/weak_signal_campaign.py']
    hashes = {}
    for name in source:
        p = ROOT/name
        hashes[name] = hashlib.sha256(p.read_bytes()).hexdigest()
        copy = args.output/'source-snapshot'/name
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, copy)
    rng = np.random.default_rng(33481)
    y, x = np.indices((32,32))
    rasters = [np.zeros((32,32),int), (x+y)%8, (x//4+y//4)%8,
               rng.integers(0,8,(32,32)), ((x-16)**2+(y-16)**2)//32%8]
    cards = []
    for index, raster in enumerate(rasters):
        card = pixel.Card(['W7PXQ', 'N0CALL', 'K1ABC/P', 'VE7TEST', 'W6LONGCALL'][index],
                          ['CN87', 'AA00', 'FN31AA', 'CN89AB', 'DM04'][index],
                          list(pixel.AVATAR_PALETTES[0]), raster.ravel().tolist(), 400+index)
        path = args.output/f'card-{index}.json'
        path.write_text(json.dumps(vars(card), indent=2)+'\n')
        cards.append(path.resolve())
    cases = [(index, backend) for index in range(5)
             for backend in ['experimental_qpsk_5s', 'fast_avatar_fec']]
    def run(case):
        index, backend = case
        destination = args.output/'runs'/f'{index}-{backend}'
        destination.parent.mkdir(parents=True, exist_ok=True)
        levels = ['-3','-4','-4.5','-5','-5.5'] if backend == 'experimental_qpsk_5s' else ['-3','-4','-4.5','-5','-5.5','5','6','7']
        command = [sys.executable, str(ROOT/'tools/modem_benchmark.py'), '--card', str(cards[index]),
                   '--backends', backend, '--repeats', '1', '--profiles', 'awgn',
                   '--snr-db', *levels, '--trials', str(args.trials), '--seed', str(55000+1000*index),
                   '--output', str(destination.resolve())]
        with (destination.parent/f'{index}-{backend}.log').open('w') as log:
            subprocess.run(command, cwd=ROOT, env=dict(os.environ, OPENBLAS_NUM_THREADS='1',
                           OMP_NUM_THREADS='1'), stdout=log, stderr=subprocess.STDOUT, check=True)
        print('Finished', case, flush=True)
        return json.loads((destination/'results.json').read_text())
    with ThreadPoolExecutor(max_workers=3) as pool:
        runs = list(pool.map(run, cases))
    metadata = dict(runs[0]['metadata'], source_sha256=hashes, corpus_cards=[str(p) for p in cards],
                    subruns=[run['metadata'] for run in runs], benchmark_processes=3)
    benchmark.report([row for run in runs for row in run['trials']], args.output, metadata)


if __name__ == '__main__':
    main()

"""Compare grid header acquisition on identical HF captures and clock errors.

Header-only evidence; this does not prove image decoding or hardware timing.
"""
import argparse
import json
from pathlib import Path
import sys
import time
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import modem_envelope as envelope
import compressed_fsk_modem as compressed
from hf_channel import simulate


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('work/grid-fsk/header-benchmark.json'))
    parser.add_argument('--seeds',type=int,default=3)
    parser.add_argument('--snr',type=float,default=-6.)
    args=parser.parse_args()
    key='experimental_16fsk_grid'; identity=b'g'*16
    header=envelope.header_audio(key,compressed.payload_samples(key,192),identity,copies=3)
    header=header*.1/np.sqrt(np.mean(header**2))
    rows=[]
    for profile in ('awgn','hf_mild','hf_moderate'):
        for seed in range(args.seeds):
            capture,metadata=simulate(header,48000,profile=profile,snr_db=args.snr,seed=1000+seed)
            for error in (None,0.,.05,.15):
                started=time.perf_counter()
                # Simulator adds a 300 ms leading guard. True waveform UTC=1000.
                copies=envelope.find_copies(capture,48000,
                    capture_start_utc=None if error is None else 999.7+error)
                valid=any(c.mode==key and c.identity==identity.hex() for c in copies)
                assert all(c.mode==key and c.identity==identity.hex() for c in copies), 'wrong header admitted'
                row=dict(profile=profile,seed=1000+seed,clock_error_seconds=error,
                         valid_header=valid,acquisition_seconds=time.perf_counter()-started)
                rows.append(row);print(json.dumps(row),flush=True)
    noise=np.random.default_rng(101).normal(0,.1,48000*3)
    assert not envelope.find_copies(noise,48000,capture_start_utc=1000)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(dict(scope='Synthetic header-only; no RF, image, or hardware timing proof',
        snr_db=args.snr,noise_bandwidth_hz=2500,rows=rows,noise_false_headers=0),indent=2)+'\n')

if __name__=='__main__': main()

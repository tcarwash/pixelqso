"""Measure lossless packet sizes and actual waveform lengths for card fixtures."""
import argparse
import json
import math
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cardmodem as pixel
import compressed_fsk_modem as compressed
from card_backends import get_backend
from modem_envelope import HEADER_SECONDS


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('work/compressed-fsk/sizes'))
    parser.add_argument('--baseline-copies',type=int,default=3)
    args=parser.parse_args()
    if not 1<=args.baseline_copies<=20:parser.error('baseline copies must be 1..20')
    args.output.mkdir(parents=True,exist_ok=True)
    rows=[]
    for pattern in ('example','flat','stripes','random'):
        card=pixel.example_card()
        if pattern!='example':card.palette=list(pixel.MINIMAL_AVATAR_PALETTE)
        if pattern=='flat':card.pixels=[2]*1024
        if pattern=='stripes':card.pixels=[(x//4)%8 for y in range(32) for x in range(32)]
        if pattern=='random':card.pixels=np.random.default_rng(1000).integers(0,8,1024).tolist()
        packet,capacity,codec=compressed.pack(card,'cq',None)
        restored,_=compressed.unpack(packet.ljust(capacity,b'\0'))
        for key,native in compressed.MODES.items():
            backend=get_backend(key);baseline=get_backend(native.replace('_ldpc','_grouped'))
            old=baseline.encode(card,args.baseline_copies,'cq',None)
            audio=backend.encode(card,args.baseline_copies,'cq',None)
            old_seconds=len(old)/48000;seconds=len(audio)/48000
            assert abs(seconds-backend.estimate_seconds(card,args.baseline_copies))<1e-6
            assert seconds<=old_seconds+1e-6
            payload=compressed.payload_samples(key,capacity)/48000
            count=min(20,math.floor((old_seconds-HEADER_SECONDS+1e-8)/payload))
            rows.append(dict(pattern=pattern,mode=key,codec=compressed.CODECS[codec],
                packet_bytes=len(packet),original_packet_bytes=len(restored),coding_capacity=capacity,
                coded_bits=compressed.code(capacity).n,baseline_copies=args.baseline_copies,
                baseline_seconds=old_seconds,compressed_seconds=seconds,
                copies_within_baseline_airtime=count,budget_used_seconds=HEADER_SECONDS+count*payload))
    (args.output/'results.json').write_text(json.dumps({'scope':'Generated TX waveform lengths and lossless packet roundtrips; no channel test','rows':rows},indent=2)+'\n')
    lines=['# Lossless compression and actual airtime','',
           '| Card | Modem | Codec | Packet bytes | Coding bytes | Original seconds | Compressed seconds | Copies in original budget |',
           '|---|---|---|---:|---:|---:|---:|---:|']
    for row in rows:
        lines.append(f"| {row['pattern']} | {row['mode']} | {row['codec']} | {row['packet_bytes']} | {row['coding_capacity']} | {row['baseline_seconds']:.3f} | {row['compressed_seconds']:.3f} | {row['copies_within_baseline_airtime']} |")
    lines+=['',f'Both duration columns use {args.baseline_copies} copies and include one 1.6-second protected group header. The last column is capped at 20 copies. Coding buckets include padding; packet byte savings do not translate directly into airtime or sensitivity.','']
    (args.output/'report.md').write_text('\n'.join(lines))
    print('\n'.join(lines))

if __name__=='__main__':main()

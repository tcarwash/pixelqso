"""Measure receive snapshot and PCM conversion cost at realistic capture lengths."""
import json
from pathlib import Path
import statistics
import time
import numpy as np
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import pcm16_to_float32


def main():
    rows=[]
    for seconds in (4.38,30,90):
        raw=np.random.default_rng(1).integers(-32768,32768,round(48000*seconds),dtype=np.int16).tobytes()
        reference=np.frombuffer(raw,dtype='<i2').astype(np.float32)/32768.0
        actual=pcm16_to_float32(raw)
        assert np.array_equal(reference,actual)
        for name,convert in (
            ('bytearray_snapshot_plus_two_float_arrays',lambda:np.frombuffer(bytes(bytearray(raw)),dtype='<i2').astype(np.float32)/32768.),
            ('bytearray_snapshot_plus_in_place_float_normalize',lambda:pcm16_to_float32(bytes(bytearray(raw))))):
            times=[]
            for _ in range(7):
                start=time.perf_counter();result=convert();times.append(time.perf_counter()-start);del result
            row=dict(capture_seconds=seconds,path=name,median_ms=round(statistics.median(times)*1000,3),
                     p95_ms=round(float(np.percentile(times,95))*1000,3))
            rows.append(row);print(json.dumps(row),flush=True)
    output=Path('work/rx-latency/buffer-benchmark.json');output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(rows,indent=2)+'\n')

if __name__=='__main__':main()

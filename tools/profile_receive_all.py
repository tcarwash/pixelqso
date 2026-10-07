"""Profile one live receive-all decode from a mono WAV capture."""
import argparse
import cProfile
import io
import json
import pstats
from pathlib import Path
import sys
import time
import wave
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture',type=Path)
    parser.add_argument('--selected-mode',default='standard')
    parser.add_argument('--include-experimental',action='store_true')
    parser.add_argument('--output',type=Path,default=Path('work/rx-latency/profile.json'))
    args=parser.parse_args()
    with wave.open(str(args.capture),'rb') as source:
        if source.getnchannels()!=1 or source.getsampwidth()!=2:
            raise ValueError('expected mono 16-bit PCM WAV')
        rate=source.getframerate()
        audio=np.frombuffer(source.readframes(source.getnframes()),dtype='<i2').astype(np.float32)/32768
    profiler=cProfile.Profile();started=time.perf_counter()
    outcome=profiler.runcall(app.decode_all_card_backends,audio,rate,
        preferred_mode=args.selected_mode,include_experimental=args.include_experimental)
    elapsed=time.perf_counter()-started
    stats=pstats.Stats(profiler).strip_dirs().sort_stats('cumtime')
    profile_path=args.output.with_suffix('.prof');args.output.parent.mkdir(parents=True,exist_ok=True)
    profiler.dump_stats(str(profile_path));stats.stream=io.StringIO();stats.print_stats(50)
    result=dict(capture=str(args.capture),sample_rate=rate,audio_seconds=len(audio)/rate,
        selected_mode=args.selected_mode,receive_all=bool(args.include_experimental),
        elapsed_seconds=elapsed,verified=bool(outcome.report and outcome.report.get('card',{}).get('exact')),
        result_mode=(outcome.report or {}).get('receive_profile'),
        failure=str(outcome.failure) if outcome.failure else None,
        top_functions=stats.stream.getvalue(),profile=str(profile_path))
    args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))

if __name__=='__main__':main()

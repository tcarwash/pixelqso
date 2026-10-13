"""Profile bounded successful modem decodes; writes timings, cProfile, and provenance."""
import argparse
import cProfile
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import pstats
import resource
import statistics
import subprocess
import sys
import time

os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import scipy
import cardmodem as modem
from card_backends import get_backend
from card_transfer import CardAssemblyService
from backend_adapters import EXPERIMENTAL_ADAPTER
from modem_envelope import find_copies
from tools.benchmark_experimental_bursts import impair


def saved_payloads(folder):
    manifest = folder / 'mode-scan-2080.0-2237.0.json'
    recording = folder / 'mode-scan-2080.0-2237.0.f16'
    meta = json.loads(manifest.read_text())
    audio = np.fromfile(recording, dtype=np.float16).astype(np.float64)
    selected = []
    for copy in (c for c in meta['copies'] if c['mode'] == 'resilient_50'):
        previous = next((c for c in selected if abs(c['payload_start_s']-copy['payload_start_s']) < 1), None)
        if previous is None:
            selected.append(copy)
        elif copy['diagnostics']['acquisition_score'] > previous['diagnostics']['acquisition_score']:
            selected[selected.index(previous)] = copy
    captures = []
    for copy in selected:
        first = round((copy['payload_start_s']-meta['start_s'])*8000)-400
        last = round((copy['payload_end_s']-meta['start_s'])*8000)+400
        captures.append(audio[max(0, first):min(len(audio), last)])
    expected = json.loads((ROOT/'docs/rx-evidence/2026-10-12/expected-card.json').read_text())['pixels']
    if not captures or any(not len(capture) for capture in captures):
        raise RuntimeError('Saved fixture has no usable resilient_50 payloads')
    provenance = {'recording': str(recording), 'sha256': hashlib.sha256(recording.read_bytes()).hexdigest(),
                  'physical_copies': len(captures), 'reference': 'saved HF capture; no new RF reception'}
    return captures, 8000, expected, provenance


def rss_mib():
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith('VmRSS:'):
            return int(line.split()[1])/1024


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['resilient_100', 'resilient_50'], required=True)
    parser.add_argument('--scenario', choices=['clean', 'awgn', 'saved-hf'], default='clean')
    parser.add_argument('--pipeline', choices=['full', 'payload'], default='full')
    parser.add_argument('--snr-db', type=float, default=-6)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--saved-root', type=Path, default=ROOT/'work/rx-analysis-20261012')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error('--runs must be positive')
    backend = get_backend(args.mode, 300)
    if args.scenario == 'saved-hf':
        if args.mode != 'resilient_50' or args.pipeline != 'payload':
            parser.error('saved HF fixture is resilient_50 and contains admitted payload copies')
        captures, rate, expected, provenance = saved_payloads(args.saved_root)
        decode = lambda: backend.decode_payload_copies(captures, rate)
        duration = sum(len(capture)/rate for capture in captures)
    else:
        card = modem.example_card()
        card.card_id = 0x6101
        expected = modem.unpack_indices(modem.minimal_avatar_payload(card), 1024)
        audio = impair(backend.encode(card, 1, 'cq', -9), args.scenario, args.snr_db, 1030)
        rate = 48000
        duration = len(audio)/rate
        provenance = {'reference': 'synthetic seeded single transmission', 'seed': 1030,
                      'snr_db_2500': args.snr_db if args.scenario == 'awgn' else None,
                      'audio_sha256': hashlib.sha256(audio.tobytes()).hexdigest()}
        if args.pipeline == 'payload':
            copies = find_copies(audio, rate)
            captures = [np.pad(audio[c.start:c.end], (2400, 2400)) for c in copies if c.mode == args.mode]
            if not captures:
                raise RuntimeError('No protected payload boundaries found')
            decode = lambda: backend.decode_payload_copies(captures, rate)
            provenance['payload_copies'] = len(captures)
        else:
            def decode():
                outcome = EXPERIMENTAL_ADAPTER.decode_capture(backend, audio, rate)
                if outcome.failure:
                    raise RuntimeError(outcome.failure.message)
                return outcome.report

    def checked():
        report = decode()
        decision = CardAssemblyService.assess(report)
        if not decision.verified_complete or report.get('pixels') != expected:
            card = report.get('card') or {}
            raise RuntimeError(f"Decode failed verification: exact={decision.verified_complete}, "
                               f"blocks={card.get('received_blocks')}")
        return report

    rows = []
    before = rss_mib()
    load_before = os.getloadavg()
    for index in range(args.runs):
        modem._AVATAR_DECODE_CACHE.clear()
        wall, cpu = time.perf_counter(), time.process_time()
        report = checked()
        rows.append({'wall_s': time.perf_counter()-wall, 'cpu_s': time.process_time()-cpu,
                     'cache': 'cleared', 'verified': True})
        print(f'{args.mode} {args.scenario} {args.pipeline}: run {index+1} {rows[-1]["wall_s"]:.3f}s', flush=True)
    profiler = cProfile.Profile()
    modem._AVATAR_DECODE_CACHE.clear()
    started = time.perf_counter()
    profiler.runcall(checked)
    profiled_s = time.perf_counter()-started
    wall, cpu = time.perf_counter(), time.process_time()
    checked()
    cached = {'wall_s': time.perf_counter()-wall, 'cpu_s': time.process_time()-cpu}
    args.output.mkdir(parents=True, exist_ok=True)
    stem = f'{args.mode}-{args.scenario}-{args.pipeline}'
    profile_file = args.output / f'{stem}.prof'
    profiler.dump_stats(str(profile_file))
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats('cumulative')
    stats.print_stats(35)
    (args.output/f'{stem}-hotspots.txt').write_text(stream.getvalue())
    top = []
    for (filename, line, function), (primitive, total, own, cumulative, callers) in sorted(
            stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:35]:
        top.append({'file': filename, 'line': line, 'function': function,
                    'calls': total, 'self_s': own, 'cumulative_s': cumulative})
    core = ['cardmodem.py', 'card_backends.py', 'modem_envelope.py', 'experimental_fec.py',
            'snr.py', 'backend_adapters.py']
    result = {'mode': args.mode, 'scenario': args.scenario, 'pipeline': args.pipeline,
        'sample_rate': rate, 'audio_s': duration, 'runs': rows,
        'median_wall_s': statistics.median(row['wall_s'] for row in rows),
        'median_cpu_s': statistics.median(row['cpu_s'] for row in rows),
        'profiled_wall_s': profiled_s, 'identical_buffer_cached_run': cached,
        'rss_before_mib': before, 'process_peak_rss_mib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
        'memory_scope': 'process high-water RSS includes imports and capture preparation; not isolated decoder allocations',
        'verified': True, 'matched_expected_pixels': True, 'provenance': provenance,
        'measured_snr_db': report['card'].get('measured_snr_db'),
        'profile': str(profile_file), 'hotspots': top,
        'environment': {'python': platform.python_version(), 'numpy': np.__version__, 'scipy': scipy.__version__,
                        'machine': platform.machine(), 'cpu_affinity_count': len(os.sched_getaffinity(0)),
                        'load_before': load_before, 'load_after': os.getloadavg()},
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_hashes': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in core}}
    (args.output/f'{stem}.json').write_text(json.dumps(result, indent=2)+'\n')
    print(f'Median {result["median_wall_s"]:.3f}s; process peak {result["process_peak_rss_mib"]:.1f} MiB', flush=True)


if __name__ == '__main__':
    main()

"""TX modem -> seeded HF audio channel -> RX modem, exact verified card scoring."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import resource
import numpy as np
from scipy.io import wavfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cardmodem as pixel
from card_backends import get_backend, MODE_REGISTRY
from backend_adapters import EXPERIMENTAL_ADAPTER
from card_transfer import CardAssemblyService
from reception_quality import reception_quality
from tools.hf_channel import PROFILES, simulate

def digest(audio):
    return hashlib.sha256(np.asarray(audio, dtype=np.float64).tobytes()).hexdigest()


def failure_stage(row):
    if row['verified']:
        return 'verified'
    if row['false_verified']:
        return 'false_verified'
    error = row.get('error', '').lower()
    if row.get('failure_kind') == 'backend_error':
        return 'backend_error'
    if 'different modem' in error:
        return 'wrong_mode'
    if any(term in error for term in ('preamble', 'header', 'acqui', 'waveform', 'no burst')):
        return 'acquisition'
    if any(term in error for term in ('parity', 'fec', 'ldpc', 'viterbi')):
        return 'fec'
    if any(term in error for term in ('crc', 'content', 'identity', 'checksum')):
        return 'integrity'
    if any(term in error for term in ('timing', 'carrier', 'clock')):
        return 'synchronization'
    return 'incomplete'


def trial(mode, audio, expected, profile, seed, snr, *, id_markers='on',
          audio_placement='near_carrier', interlaced=False):
    started = time.perf_counter()
    cpu_started = time.process_time()
    outcome = (EXPERIMENTAL_ADAPTER.decode_legacy_audio(audio, 48000, profile="standard")
               if mode == "standard" else
               EXPERIMENTAL_ADAPTER.decode_capture(
                   get_backend(mode, audio_placement), audio, 48000))
    row = dict(mode=mode, profile=profile, seed=seed, snr_db_2500=snr,
               decode_seconds=time.perf_counter()-started, verified=False,
               cpu_seconds=time.process_time()-cpu_started,
               peak_process_rss_kb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
               id_markers=id_markers, audio_placement=audio_placement,
               interlaced=interlaced,
               exact_match=False, false_verified=False,
               heard_percent=None, crc_verified_percent=None)
    case = json.dumps(dict(mode=mode, profile=profile, seed=seed, snr=snr,
                           expected=expected), sort_keys=True, separators=(',', ':'))
    row['case_id'] = hashlib.sha256(case.encode()).hexdigest()
    if outcome.failure:
        row.update(error=outcome.failure.message, failure_kind=outcome.failure.kind.value)
        row['stage'] = failure_stage(row)
        return row
    report, _ = CardAssemblyService().merge_preview(outcome.report)
    card = report.get('card') or {}
    expected_pixels = expected['pixels'] if expected is not None else None
    if mode == 'standard' and expected is not None:
        expected_pixels = [[tuple(pixel.MINIMAL_AVATAR_PALETTE[index])
                            for index in expected['pixels'][y*32:(y+1)*32]] for y in range(32)]
    matches = (expected is not None and report.get('pixels') == expected_pixels and
               card.get('callsign') == expected['callsign'] and card.get('grid') == expected['grid'])
    verified = bool(report['assembly_event']['verified_complete'])
    quality = reception_quality(card, verified_complete=verified)
    row.update(verified=verified and matches, exact_match=matches,
               false_verified=verified and not matches,
               coverage_pixels=sum(card.get('pixel_coverage') or []),
               heard_percent=quality.heard_percent,
               crc_verified_percent=quality.checked_percent,
               diagnostics=card.get('diagnostics') or report.get('diagnostics') or {})
    row['stage'] = failure_stage(row)
    return row

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=Path('work/hf-benchmark'))
    p.add_argument('--modes', nargs='+', default=[key for key, spec in MODE_REGISTRY.items() if spec.experimental])
    p.add_argument('--profiles', nargs='+', choices=PROFILES, default=list(PROFILES))
    p.add_argument('--snr-db', nargs='+', type=float, default=[-6])
    p.add_argument('--trials', type=int, default=3)
    p.add_argument('--seed', type=int, default=1000)
    p.add_argument('--card-pattern', choices=('random','example','flat','stripes'), default='random')
    p.add_argument('--repeats', type=int, default=1)
    p.add_argument('--cfo-hz', type=float, default=0)
    p.add_argument('--clock-ppm', type=float, default=0)
    p.add_argument('--drift-hz-per-second', type=float, default=0)
    p.add_argument('--save-wavs', action='store_true')
    p.add_argument('--native-payload', action='store_true',
                   help='Test unannounced native experimental FSK payloads (no common header)')
    p.add_argument('--id-markers', choices=('on', 'off'), default='on')
    p.add_argument('--audio-placement', choices=('near_carrier', 'centered'),
                   default='near_carrier')
    p.add_argument('--interlaced', action='store_true',
                   help='Rotate coded blocks across repeated experimental burst copies')
    p.add_argument('--replay-manifest', type=Path,
                   help='Decode immutable WAV fixtures from a prior --save-wavs run')
    a = p.parse_args()
    if a.trials < 1 or a.repeats < 1:
        p.error('trials and repeats must be positive')
    if a.native_payload and a.id_markers != 'off':
        p.error('--native-payload requires --id-markers off')
    if a.native_payload and a.interlaced:
        p.error('--interlaced requires the protected copy headers used by modem backends')
    if a.interlaced and a.id_markers != 'on':
        p.error('--interlaced requires --id-markers on')
    if 'standard' in a.modes and a.id_markers == 'off' and not a.replay_manifest:
        p.error('legacy standard mode does not support marker-free transmit')
    a.output.mkdir(parents=True, exist_ok=True)
    rows, channels = [], []
    if a.replay_manifest:
        manifest = json.loads(a.replay_manifest.read_text())
        for item in manifest['captures']:
            path = a.replay_manifest.parent / item['wav']
            rate, audio = wavfile.read(path)
            if rate != 48000 or digest(audio) != item['capture_sha256']:
                raise ValueError(f'Capture rate or digest changed: {path}')
            row = trial(item['mode'], audio, item['expected'], item['profile'],
                        item['seed'], item['snr_db_2500'], id_markers=item['id_markers'],
                        audio_placement=item.get('audio_placement', 'near_carrier'),
                        interlaced=item.get('interlaced', False))
            row['capture_sha256'] = item['capture_sha256']
            row['airtime_seconds'] = item['airtime_seconds']
            rows.append(row)
            print(f'{path.name}: verified={row["verified"]} stage={row["stage"]}', flush=True)
    else:
      captures = []
      revision = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True,
                                text=True, check=True).stdout.strip()
      working_diff = subprocess.run(['git', 'diff', '--binary'], capture_output=True,
                                    check=True).stdout
      working_diff_sha256 = hashlib.sha256(working_diff).hexdigest()
      harness_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
      if a.save_wavs:
          (a.output / 'captures').mkdir(exist_ok=True)
      for mode in a.modes:
        if a.native_payload:
            import experimental_burst_modem as burst
            if mode not in burst.FORMATS:
                p.error('--native-payload requires a native experimental FSK mode')
        backend = None if mode == "standard" else get_backend(mode, a.audio_placement)
        if a.interlaced and not getattr(backend, 'supports_interlaced_copies', False):
            p.error(f'{mode} does not support interlaced copies')
        for i in range(a.trials):
            card = pixel.example_card()
            card.card_id = 0x5700 + i
            if a.card_pattern == 'random':
                card.pixels = np.random.default_rng(a.seed + i).integers(0, 8, 1024).tolist()
            elif a.card_pattern == 'flat':
                card.pixels = [i % 8]*1024
            elif a.card_pattern == 'stripes':
                card.pixels = [((x//4)+i)%8 for y in range(32) for x in range(32)]
            if a.card_pattern != 'example':
                card.palette = list(pixel.MINIMAL_AVATAR_PALETTE)
            encoded = (np.tile(burst.encode(mode,card,'cq',-9),a.repeats) if a.native_payload else
                       EXPERIMENTAL_ADAPTER.encode_legacy(card, a.repeats, 'cq', -9, 'standard', 'standard')
                       if mode == 'standard' else backend.encode(card, a.repeats, 'cq', -9,
                                                   **({'interlaced': True} if a.interlaced else {}),
                                                   include_id_markers=a.id_markers == 'on'))
            audio = np.asarray(encoded, float)
            audio *= .1 / max(np.sqrt(np.mean(audio ** 2)), 1e-12)
            expected = dict(pixels=pixel.unpack_indices(pixel.minimal_avatar_payload(card), 1024),
                            callsign=card.callsign, grid=card.grid)
            for profile in a.profiles:
                for snr in a.snr_db:
                    seed = a.seed + i
                    y, meta = simulate(audio, 48000, profile=profile, snr_db=snr, seed=seed,
                                       cfo_hz=a.cfo_hz, clock_ppm=a.clock_ppm,
                                       drift_hz_per_second=a.drift_hz_per_second)
                    row = trial(mode, y, expected, profile, seed, snr,
                                id_markers=a.id_markers,
                                audio_placement=a.audio_placement,
                                interlaced=a.interlaced)
                    row['airtime_seconds'] = len(audio) / 48000
                    row['capture_sha256'] = digest(y)
                    rows.append(row)
                    channels.append(meta)
                    if a.save_wavs:
                        name = f'captures/{mode}-{profile}-{snr:g}-{seed}.wav'
                        wavfile.write(a.output / name, 48000, y)
                        captures.append(dict(wav=name, mode=mode, profile=profile,
                            seed=seed, snr_db_2500=snr, id_markers=a.id_markers,
                            audio_placement=a.audio_placement,
                            interlaced=a.interlaced,
                            card_sha256=hashlib.sha256(pixel.minimal_avatar_payload(card)).hexdigest(),
                            waveform_sha256=digest(audio), capture_sha256=digest(y),
                            expected=expected, channel=meta, source_revision=revision,
                            working_diff_sha256=working_diff_sha256,
                            harness_sha256=harness_sha256,
                            airtime_seconds=len(audio)/48000))
                    print(f'{mode} {profile} {snr:g} dB seed={seed}: verified={row["verified"]}', flush=True)
            # One noise-only false-acceptance control per card/mode.
            noise = np.random.default_rng(a.seed + i).normal(0, .1, len(audio) + 28800)
            row = trial(mode, noise, None, 'noise_only', a.seed + i, None,
                        id_markers=a.id_markers,
                        audio_placement=a.audio_placement)
            row['capture_sha256'] = digest(noise)
            rows.append(row)
            if a.save_wavs:
                name = f'captures/{mode}-noise-{a.seed+i}.wav'
                wavfile.write(a.output / name, 48000, noise)
                captures.append(dict(wav=name, mode=mode, profile='noise_only',
                    seed=a.seed+i, snr_db_2500=None, id_markers=a.id_markers,
                    audio_placement=a.audio_placement,
                    interlaced=a.interlaced,
                    expected=None, capture_sha256=digest(noise),
                    source_revision=revision, working_diff_sha256=working_diff_sha256,
                    harness_sha256=harness_sha256, airtime_seconds=0))
        (a.output / 'trials.partial.json').write_text(json.dumps(rows, indent=2) + '\n')
      if a.save_wavs:
          (a.output / 'manifest.json').write_text(json.dumps(dict(schema=1,
              captures=captures), indent=2) + '\n')
    summary = []
    for mode, profile, snr in dict.fromkeys((r['mode'], r['profile'], r['snr_db_2500']) for r in rows):
        group = [r for r in rows if (r['mode'], r['profile'], r['snr_db_2500']) == (mode, profile, snr)]
        summary.append(dict(mode=mode, profile=profile, snr_db_2500=snr, trials=len(group),
                            verified=sum(r['verified'] for r in group),
                            exact_match=sum(r['exact_match'] for r in group),
                            false_verified=sum(r['false_verified'] for r in group),
                            mean_decode_seconds=float(np.mean([r['decode_seconds'] for r in group]))))
    (a.output / 'results.json').write_text(json.dumps(dict(
        scope='Synthetic offline audio; illustrative Gaussian-scatter presets, not certified ITU or RF measurements',
        arguments={k: str(v) if isinstance(v, Path) else v for k, v in vars(a).items()},
        channels=channels, summary=summary, trials=rows), indent=2) + '\n')
    with (a.output / 'summary.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    return int(any(r['false_verified'] or r.get('failure_kind') == 'backend_error' for r in rows))

if __name__ == '__main__':
    raise SystemExit(main())

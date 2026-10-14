"""Replay protected acquisitions from the 7066 kHz on-air recording.

Run with the repository venv: python docs/rx-evidence/2026-10-14/replay.py RECORDING_DIR OUTPUT_DIR
The adjacent headers.json contains independently CRC/RS-validated header scans.
Each group is keyed by mode and protected content digest, including across RX restarts.
"""
import os
os.environ['OPENBLAS_NUM_THREADS'] = '1'
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly
from card_backends import get_backend
from modem_envelope import AnnouncedCopy, decode_capture

recordings, output = map(Path, sys.argv[1:])
output.mkdir(parents=True, exist_ok=True)
headers = json.loads(Path(__file__).with_name('headers.json').read_text())
events = [json.loads(line) for line in (recordings / 'events.jsonl').read_text().splitlines()]
order = {stream: i for i, stream in enumerate(dict.fromkeys(
    e['details']['rx_stream_id'] for e in events if e['kind'] == 'rx_audio'))}
headers.sort(key=lambda h: (order[h['stream']], h['start']))
streams = {}
coverage = {}
for stream in dict.fromkeys(header['stream'] for header in headers):
    chunks = [e for e in events if e['kind'] == 'rx_audio' and e['details']['rx_stream_id'] == stream]
    size = max(e['details']['sample_end'] for e in chunks)
    audio = np.zeros(size, np.float32)
    present = np.zeros(size, bool)
    for event in chunks:
        rate, samples = wavfile.read(recordings / event['audio']['wav'])
        assert rate == 48000 and samples.dtype == np.int16
        start, end = (event['details'][key] for key in ('sample_start', 'sample_end'))
        assert len(samples) == end - start
        audio[start:end] = samples.astype(np.float32) / 32768
        present[start:end] = True
    streams[stream] = resample_poly(audio, 1, 6)
    coverage[stream] = present

summary = []
for mode, identity in dict.fromkeys((h['mode'], h['identity']) for h in headers):
    parts, copies, seen = [], [], []
    offset = 0
    for header in headers:
        if (header['mode'], header['identity']) != (mode, identity):
            continue
        stream, start = header['stream'], header['start']
        if any(s == stream and abs(t - start) < 4000 for s, t in seen):
            continue
        seen.append((stream, start))
        length = round(header['diagnostics']['payload_samples'] / 6)
        if start < 0 or (start + length) * 6 > len(coverage[stream]):
            continue
        if not coverage[stream][start * 6:(start + length) * 6].all():
            raise ValueError('Recording gap in announced payload')
        part = streams[stream][start:start + length]
        parts.append(part)
        copies.append(AnnouncedCopy(mode, identity, offset, offset + len(part), header['diagnostics']))
        offset += len(part)
    if not copies:
        continue
    backend = get_backend(mode, 300)
    try:
        report = decode_capture(mode, np.concatenate(parts), 8000, backend.decode,
            copies=copies, copy_decoder=getattr(backend, 'decode_payload_copies', None),
            announced_decoder=getattr(backend, 'decode_announced_copies', None))
        card = report.get('card') or {}
        name = f'{mode}-{identity}.json'
        (output / name).write_text(json.dumps(report, indent=2))
        result = dict(mode=mode, identity=identity, copies=len(copies),
                      exact=card.get('exact'), coverage=sum(card.get('pixel_coverage') or []), report=name)
    except ValueError as error:
        result = dict(mode=mode, identity=identity, error=str(error))
    summary.append(result)
    print(json.dumps(result), flush=True)
(output / 'summary.json').write_text(json.dumps(summary, indent=2))

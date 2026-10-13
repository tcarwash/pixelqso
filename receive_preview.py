"""Display-only resilient raster recovery when the native identity is unavailable."""
import numpy as np
from scipy.signal import resample_poly
import cardmodem as modem


def unidentified_preview(backend, audio, sample_rate, copies=()):
    """Never supply an identity or verified pixels from an unchecked header.

    Require strong native sync and a plausible, confident v4 layout prefix.
    This only supports the systematic resilient avatar format; LDPC formats
    cannot be rendered by treating their encoded bits as image pixels.
    """
    if backend.key not in ('resilient_100', 'resilient_50', 'resilient_25', 'fast_avatar_fec_v4'):
        return None
    matching = [copy for copy in copies if copy.mode == backend.key]
    candidates = [(np.asarray(audio)[c.start:c.end], c) for c in matching]
    if not candidates and not copies:
        candidates = [(np.asarray(audio), None)]
    best = None
    for signal, copy in candidates:
        if not len(signal):
            continue
        rate = 8000
        signal = resample_poly(signal, rate, int(sample_rate)) if sample_rate != rate else signal
        signal = np.pad(signal, (400, 400))
        if copy is not None:
            start, offset, period = modem._announced_avatar_acquisition(signal, rate, backend.profile)
        else:
            try:
                start, offset, period = modem.acquire(signal, rate, profile=backend.profile)
            except ValueError:
                continue
        metrics = modem.demodulate_metrics(signal, rate, start_sample=start,
            frequency_offset_hz=offset, symbol_period_samples=period, profile=backend.profile)
        tones = np.argmax(metrics, axis=1)
        sync_count = len(modem.FRAME_SYNC)
        if copy is None and len(tones) >= sync_count:
            scores = np.zeros(len(tones)-sync_count+1, dtype=int)
            for index, tone in enumerate(modem.FRAME_SYNC):
                scores += tones[index:index+len(scores)] == tone
            sync_at = int(np.argmax(scores))
            metrics, tones = metrics[sync_at:], tones[sync_at:]
        if len(tones) < sync_count + 16:
            continue
        if np.count_nonzero(tones[:sync_count] == modem.FRAME_SYNC) < sync_count - 2:
            continue
        prefix = metrics[sync_count:sync_count+16]
        sorted_prefix = np.sort(prefix, axis=1)
        margins = (sorted_prefix[:, -1]-sorted_prefix[:, -2])/np.maximum(sorted_prefix[:, -1], 1e-12)
        if np.min(margins) < .35:
            continue
        values = modem._tones_to_gf64(tones[sync_count:sync_count+16])
        raw = modem._gf64_to_bytes(values, 6)
        magic, version, width, height, palette_id = raw[:2], *raw[2:6]
        if magic != b'PQ' or version != 4 or (width, height) != (32, 32):
            continue
        if palette_id >= len(modem.AVATAR_PALETTES):
            continue
        bpp = modem.AVATAR_PALETTE_BITS[palette_id]
        pixels_per_block = (modem.AVATAR_RS_K-modem.AVATAR_V4_BLOCK_OVERHEAD)*6//bpp
        pixels, coverage = [0]*1024, [False]*1024
        cursor = sync_count + modem.AVATAR_META_V4_TONE_COUNT
        for first in range(0, 1024, pixels_per_block):
            count = min(pixels_per_block, 1024-first)
            data_count = (count*bpp+5)//6 + modem.AVATAR_V4_BLOCK_OVERHEAD
            available = min(max(0, len(tones)-cursor), data_count*2)
            available -= available % 2
            raw_values = modem._tones_to_gf64(tones[cursor:cursor+available])
            raw_values = raw_values[modem.AVATAR_V4_BLOCK_OVERHEAD:]
            received = min(count, len(raw_values)*6//bpp)
            if received:
                pixels[first:first+received] = modem._gf64_to_pixels(raw_values, received, bpp)
                coverage[first:first+received] = [True]*received
            cursor += (data_count+2)*2+6
        if not any(coverage):
            continue
        report = {'receive_activity': 'Tentative image · identity header not verified',
            'unidentified_preview': True, 'receive_profile': backend.key,
            'pixels': pixels, 'card': {'width': 32, 'height': 32,
                'palette': [list(rgb) for rgb in modem.AVATAR_PALETTES[palette_id]],
                'pixel_coverage': coverage, 'verified_pixel_coverage': [False]*1024,
                'exact': False, 'metadata_verified': False,
                'color_stage': 'Tentative pixels · identity unavailable'}}
        if copy is not None:
            report['copy_header'] = dict(copy.diagnostics, content_id=copy.identity)
            report['receive_identity'] = (copy.diagnostics.get('receive_session_id', 0), backend.key, copy.identity)
        if best is None or sum(coverage) > sum(best['card']['pixel_coverage']):
            best = report
    return best

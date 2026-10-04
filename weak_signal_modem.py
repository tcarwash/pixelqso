"""Experimental fixed 32x32 card modem: shaped QPSK and rate-0.24 LDPC."""
from functools import lru_cache
import math
import struct
import zlib

import numpy as np
from scipy.signal import fftconvolve, resample_poly, find_peaks
import cardmodem as pixel

FS = 8000
SPS = 4
CARRIER = 1500
K, N = 3376, 14080
HEADER = struct.Struct('>4s6BH12s8sBb')
KINDS = {'card': 0, 'cq': 1, 'exchange': 2, '73': 3}
PREAMBLE = 512
BLOCKS, DATA, PILOTS = 110, 64, 8
GUARD = 1600


@lru_cache(None)
def code():
    from weak_signal_ldpc import fixed_code
    return fixed_code()


@lru_cache(None)
def constants():
    rng = np.random.Generator(np.random.PCG64(0x50515731))
    training = (1 - 2 * rng.integers(0, 2, (PREAMBLE, 2))).dot([1, 1j]) / math.sqrt(2)
    pilots = (1 - 2 * rng.integers(0, 2, (BLOCKS, PILOTS, 2))).dot([1, 1j]) / math.sqrt(2)
    permutation = rng.permutation(N)
    whitening = rng.integers(0, 2, K, dtype=np.uint8)
    t = np.arange(-24, 25) / SPS
    alpha = .25
    h = np.empty_like(t)
    for i, x in enumerate(t):
        if x == 0:
            h[i] = 1 + alpha * (4 / np.pi - 1)
        elif abs(abs(x) - 1 / (4 * alpha)) < 1e-10:
            h[i] = alpha / math.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha)) + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = (np.sin(np.pi*x*(1-alpha)) + 4*alpha*x*np.cos(np.pi*x*(1+alpha))) / (np.pi*x*(1-(4*alpha*x)**2))
    h /= np.linalg.norm(h)
    return training, pilots, permutation, whitening, h


def packet(card, message_type='card', snr_db=None):
    card.validate()
    if (card.width, card.height, len(card.palette)) != (32, 32, 8):
        raise ValueError('Experimental weak-signal mode requires a 32x32 eight-color card')
    call, grid = card.callsign.upper().encode('ascii'), card.grid.upper().encode('ascii')
    if not 1 <= len(call) <= 12 or not 1 <= len(grid) <= 8:
        raise ValueError('Identity requires 1..12 callsign and 1..8 grid characters')
    if message_type not in KINDS or (snr_db is not None and not -127 <= int(snr_db) <= 127):
        raise ValueError('Invalid message type or SNR')
    body = HEADER.pack(b'PQW1', 1, 32, 32, 0, len(call), len(grid), card.card_id & 65535,
                       call.ljust(12, b'\0'), grid.ljust(8, b'\0'), KINDS[message_type],
                       -128 if snr_db is None else int(snr_db)) + pixel.minimal_avatar_payload(card)
    return body + struct.pack('>I', zlib.crc32(body))


def encode(card, repeats=1, message_type='card', snr_db=None):
    if repeats != 1:
        raise ValueError('Experimental five-second mode supports one burst')
    training, pilots, permutation, whitening, h = constants()
    bits = np.unpackbits(np.frombuffer(packet(card, message_type, snr_db), np.uint8)) ^ whitening
    coded = code().encode(bits[None, :])[0][permutation].astype(float)
    symbols = ((1 - 2*coded[0::2]) + 1j*(1 - 2*coded[1::2])) / math.sqrt(2)
    blocks = np.concatenate((pilots, symbols.reshape(BLOCKS, DATA)), axis=1)
    stream = np.concatenate((training, blocks.ravel()))
    impulses = np.zeros(len(stream)*SPS, complex)
    impulses[::SPS] = stream
    base = np.convolve(impulses, h)
    audio = np.sqrt(2) * (base * np.exp(2j*np.pi*CARRIER*np.arange(len(base))/FS)).real
    audio *= .8 / np.max(abs(audio))
    return resample_poly(np.pad(audio, (GUARD, GUARD)), 6, 1).astype(np.float32)


def seconds():
    return ((PREAMBLE + BLOCKS*(DATA+PILOTS))*SPS + 48 + 2*GUARD)/FS


def decode_ldpc(llrs, iterations=100):
    """CPU sum-product decoder, including NR punctures and known filler bits."""
    c = code()
    rows, cols = c.edges
    channel = np.zeros(c.n_cols)
    channel[c.sent] = np.clip(llrs, -30, 30)
    channel[c.k:c.kb*c.z] = 50
    messages = np.zeros(len(rows))
    posterior = channel.copy()
    for iteration in range(iterations):
        outgoing = np.clip(posterior[cols] - messages, -50, 50)
        negative = outgoing < 0
        signs = np.bincount(rows, weights=negative, minlength=c.mb*c.z).astype(int) & 1
        phi = -np.log(np.tanh(np.clip(abs(outgoing), 1e-8, 40)/2))
        sums = np.bincount(rows, weights=phi, minlength=c.mb*c.z)
        messages = (1 - 2*(signs[rows] ^ negative)) * (-np.log(np.tanh(np.clip(sums[rows]-phi, 1e-8, 40)/2)))
        posterior = channel + np.bincount(cols, weights=messages, minlength=c.n_cols)
        hard = (posterior < 0).astype(np.uint8)
        if c.syndrome_ok(hard[None, :])[0]:
            return hard[:K], iteration+1
    raise ValueError('Experimental LDPC parity checks did not converge')


def unpack(bits, diagnostics):
    body = np.packbits(bits ^ constants()[3]).tobytes()
    if zlib.crc32(body[:-4]) != struct.unpack('>I', body[-4:])[0]:
        raise ValueError('Experimental whole-card CRC failed')
    magic, version, width, height, palette, nc, ng, ident, call, grid, kind, snr = HEADER.unpack_from(body)
    if (magic, version, width, height, palette) != (b'PQW1', 1, 32, 32, 0) or kind not in KINDS.values() or not 1 <= nc <= 12 or not 1 <= ng <= 8:
        raise ValueError('Invalid experimental card metadata')
    if any(call[nc:]) or any(grid[ng:]):
        raise ValueError('Invalid experimental identity padding')
    pixels = pixel.unpack_indices(body[HEADER.size:-4], 1024)
    metadata = dict(callsign=call[:nc].decode('ascii'), grid=grid[:ng].decode('ascii'),
                    card_id=ident, width=32, height=32, palette_id=0,
                    message_type=next(k for k,v in KINDS.items() if v == kind),
                    snr_db=None if snr == -128 else snr, exact=True,
                    avatar_mode='experimental_qpsk_5s', avatar_burst=True,
                    received_blocks=[True], pixel_coverage=[True]*1024,
                    received_copies=1, color_stage='CRC checked card', diagnostics=diagnostics)
    return pixels, metadata


def acquire(source):
    """Find nonoverlapping bursts without assuming their spacing or identity."""
    training, _, _, _, h = constants()
    if len(source) < PREAMBLE*SPS:
        raise ValueError('Incomplete experimental burst')
    time = np.arange(len(source))/FS
    base = np.sqrt(2)*source*np.exp(-2j*np.pi*CARRIER*time)
    matched = fftconvolve(base, h)
    candidates = []
    # Blind acquisition: all four sampling phases and a bounded carrier search.
    for phase in range(SPS):
        samples = matched[phase::SPS]
        energy = np.convolve(abs(samples)**2, np.ones(PREAMBLE), 'valid')
        for offset in range(-100, 101, 4):
            shifted = samples*np.exp(-2j*np.pi*offset*np.arange(len(samples))/2000)
            corr = fftconvolve(shifted, training[::-1].conj(), 'valid')
            score = abs(corr)**2 / np.maximum(energy*PREAMBLE, 1e-20)
            positions, _ = find_peaks(score, height=.08, distance=PREAMBLE)
            for position in positions:
                candidates.append((score[position], int(position)*SPS+phase, offset))
    if not candidates:
        raise ValueError('No experimental preamble acquired')
    selected = []
    frame_samples = (PREAMBLE + BLOCKS*(PILOTS+DATA))*SPS
    for candidate in sorted(candidates, reverse=True):
        if all(abs(candidate[1]-other[1]) >= frame_samples for other in selected):
            selected.append(candidate)
        if len(selected) >= 20:
            break
    return sorted(selected, key=lambda item: item[1])


def soft_frame(source, candidate):
    """Return noise-weighted bit evidence; never release unchecked pixels."""
    training, pilots, permutation, _, h = constants()
    score, start, offset = candidate
    capture_start = start
    first = max(0, start - len(h)*2)
    last = start + (PREAMBLE + BLOCKS*(PILOTS+DATA))*SPS + len(h)*2
    source = source[first:last]
    start -= first
    time = np.arange(len(source))/FS
    base = np.sqrt(2)*source*np.exp(-2j*np.pi*CARRIER*time)
    matched = fftconvolve(base, h)
    # Refine frequency coherently against the full training sequence.
    positions = start + np.arange(PREAMBLE)*SPS
    best = None
    for fine in np.arange(offset-3, offset+3.01, .25):
        values = matched[positions] * np.exp(-2j*np.pi*fine*positions/FS)
        gain = np.mean(values*training.conj())
        metric = abs(gain)
        if best is None or metric > best[0]:
            best = metric, fine, gain
    offset = best[1]
    matched = fftconvolve(base*np.exp(-2j*np.pi*offset*time), h)
    # Pilot coherence jointly selects fractional timing and sample-clock drift.
    best = None
    pilot_symbols = PREAMBLE + np.arange(BLOCKS)[:,None]*(PILOTS+DATA) + np.arange(PILOTS)
    for ppm in range(-140, 141, 20):
        step = SPS*(1+ppm/1e6)
        for fraction in np.arange(-1, 1.01, .25):
            indices = start + fraction + pilot_symbols*step
            if indices[-1,-1] >= len(matched):
                continue
            values = np.interp(indices.ravel(), np.arange(len(matched)), matched).reshape(BLOCKS,PILOTS)
            gains = np.mean(values*pilots.conj(), axis=1)
            metric = np.sum(abs(gains)**2)
            if best is None or metric > best[0]:
                best = metric, step, fraction
    if best is None:
        raise ValueError('Incomplete experimental burst')
    _, step, fraction = best
    indices = start + fraction + (PREAMBLE + np.arange(BLOCKS*(PILOTS+DATA)))*step
    if indices[-1] >= len(matched):
        raise ValueError('Incomplete experimental burst')
    blocks = np.interp(indices, np.arange(len(matched)), matched).reshape(BLOCKS,PILOTS+DATA)
    gains = np.mean(blocks[:,:PILOTS]*pilots.conj(), axis=1)
    smooth = np.convolve(np.pad(gains, (4,4), mode='edge'), np.ones(9)/9, 'valid')
    variance = np.mean(abs(blocks[:,:PILOTS]-smooth[:,None]*pilots)**2)
    values = blocks[:,PILOTS:]*smooth[:,None].conj()
    llr = np.empty(N)
    llr[0::2] = 2*math.sqrt(2)*values.real.ravel()/max(variance,1e-10)
    llr[1::2] = 2*math.sqrt(2)*values.imag.ravel()/max(variance,1e-10)
    ordered = np.empty(N)
    ordered[permutation] = llr
    estimated_snr = 10*np.log10(max(float(np.mean(abs(smooth)**2)), 1e-20)/max(variance,1e-20)*2000/2500)
    return ordered, dict(acquisition_score=float(score), carrier_offset_hz=float(offset),
                         clock_ppm=(step/SPS-1)*1e6,
                         start_seconds=capture_start/FS,
                         estimated_snr_db_2500=float(estimated_snr))


def decode(audio, sample_rate=48000):
    divisor = math.gcd(sample_rate, FS)
    source = resample_poly(np.asarray(audio, float), FS//divisor, sample_rate//divisor)
    frames = []
    error = ValueError('Incomplete experimental burst')
    for candidate in acquire(source):
        try:
            frames.append(soft_frame(source, candidate))
        except ValueError as exc:
            error = exc
    if not frames:
        raise error
    # Try the strongest individual frame first, then accumulate evidence.
    # Different payloads must still pass whole-card CRC; no pixel guess is saved.
    frames.sort(key=lambda item: item[1]['acquisition_score'], reverse=True)
    attempts = [(frames[0][0], frames[:1])]
    for count in range(2, len(frames)+1):
        attempts.append((np.sum([frame[0] for frame in frames[:count]], axis=0), frames[:count]))
    attempts.extend((frame[0], [frame]) for frame in frames[1:])
    for llrs, used in attempts:
        try:
            bits, iterations = decode_ldpc(llrs)
            diagnostics = dict(used[0][1], ldpc_iterations=iterations,
                               combined_copies=len(used), acquired_copies=len(frames),
                               copy_diagnostics=[frame[1] for frame in used])
            pixels, metadata = unpack(bits, diagnostics)
        except ValueError as exc:
            error = exc
            continue
        metadata['received_copies'] = len(used)
        metadata['color_stage'] = f'CRC checked card · {len(used)} combined copies'
        snr = np.mean([frame[1]['estimated_snr_db_2500'] for frame in used])
        metadata['measured_snr_db'] = max(-127, min(127, round(snr)))
        return pixels, metadata
    raise error

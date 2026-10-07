"""Single-burst experimental FSK codecs and an adaptive QPSK receiver.

FSK formats use distinct seeded acquisition sequences, Gray-labelled tones,
pilot-aided timing, and whole-card CRC32. All four modes currently carry the
same exact 32x32 eight-colour card envelope as weak_signal_modem.
"""
from dataclasses import dataclass
from functools import lru_cache
import math
import struct
import zlib

import numpy as np
from scipy.signal import fftconvolve, hilbert, resample_poly, find_peaks
from scipy.special import logsumexp, i0e

import cardmodem as pixel
import weak_signal_modem as weak
from copy_combining import SoftCopy, recovery_attempts, diagnostics_for, hypothesis_attempts, MAX_COPIES, ADMITTED_IDENTITY
from experimental_fec import (OUTER_DATA, OUTER_PARITY, SHARD_BYTES,
                              outer_encode, outer_recover, rs_encode, rs_decode)

FS = 12000
TRAINING = 64
GUARD_SECONDS = .1
CODE_BITS = 8192
PACKET_BYTES = weak.K // 8


@dataclass(frozen=True)
class FSKFormat:
    key: str
    tones: int
    baud: int
    spacing: int
    lowest: int
    seed: int
    outer: bool = False
    code_bits: int = CODE_BITS

    @property
    def frequencies(self):
        return self.lowest + self.spacing * np.arange(self.tones)

    @property
    def bits_per_symbol(self):
        return self.tones.bit_length() - 1

    @property
    def block_lengths(self):
        if self.outer:
            return (124,) * (OUTER_DATA + OUTER_PARITY)
        count = math.ceil(self.code_bits / self.bits_per_symbol)
        return tuple(min(128, count - start) for start in range(0, count, 128))

    @property
    def symbol_count(self):
        return TRAINING + sum(length + 4 for length in self.block_lengths)

    @property
    def seconds(self):
        return self.symbol_count / self.baud + 2 * GUARD_SECONDS


FORMATS = {
    "experimental_8fsk_ldpc": FSKFormat("experimental_8fsk_ldpc", 8, 600, 300, 450, 0x8F51),
    "experimental_16fsk_ldpc": FSKFormat("experimental_16fsk_ldpc", 16, 400, 150, 375, 0x16F51),
    "experimental_8fsk_outer_rs": FSKFormat("experimental_8fsk_outer_rs", 8, 400, 200, 800, 0x8F52, True),
}
QPSK_KEY = "experimental_qpsk_equalized"
MODE_KEYS = (*FORMATS, QPSK_KEY)


@lru_cache(None)
def format_constants(key):
    return format_constants_for(FORMATS[key])


@lru_cache(None)
def format_constants_for(fmt):
    rng = np.random.default_rng(fmt.seed)
    training = rng.integers(0, fmt.tones, TRAINING)
    pilots = rng.integers(0, fmt.tones, (len(fmt.block_lengths), 4))
    permutation = rng.permutation(fmt.code_bits)
    labels = np.arange(fmt.tones) ^ (np.arange(fmt.tones) >> 1)
    return training, pilots, permutation, labels, np.argsort(labels)


def _complex_wave(symbols, fmt):
    frequencies = np.repeat(fmt.frequencies[np.asarray(symbols, dtype=int)], FS // fmt.baud)
    phase = np.concatenate(([0.0], np.cumsum(2 * np.pi * frequencies[:-1] / FS)))
    return np.exp(1j * phase)


def _resample(audio, sample_rate, target):
    if sample_rate <= 0 or sample_rate != int(sample_rate):
        raise ValueError("invalid audio sample rate")
    source = np.asarray(audio, dtype=float)
    if source.ndim != 1 or not np.all(np.isfinite(source)):
        raise ValueError("audio must contain finite mono samples")
    divisor = math.gcd(int(sample_rate), target)
    return resample_poly(source, target // divisor, int(sample_rate) // divisor)


def _outer_words(packet):
    words = []
    for index, shard in enumerate(outer_encode(packet)):
        body = bytes((index,)) + shard
        body += struct.pack(">H", pixel.crc16(body))
        values = pixel._bytes_to_gf64(body)
        words.append(np.asarray(pixel._gf64_to_tones(rs_encode(values)), dtype=int))
    return words


def _ldpc_symbols(packet, fmt):
    _training, _pilots, permutation, _labels, inverse = format_constants(fmt.key)
    bits = np.unpackbits(np.frombuffer(packet, np.uint8)) ^ weak.constants()[3]
    coded = weak.code().encode(bits[None, :])[0][:CODE_BITS][permutation]
    bps = fmt.bits_per_symbol
    coded = np.pad(coded, (0, (-len(coded)) % bps)).reshape(-1, bps)
    values = coded.dot(1 << np.arange(bps - 1, -1, -1))
    return inverse[values]


def encode(key, card, message_type="card", snr_db=None):
    packet = weak.packet(card, message_type, snr_db)
    if key == QPSK_KEY:
        return weak.encode(card, 1, message_type, snr_db)
    fmt = FORMATS[key]
    training, pilots, _permutation, _labels, inverse = format_constants(key)
    if fmt.outer:
        blocks = [inverse[word] for word in _outer_words(packet)]
    else:
        symbols = _ldpc_symbols(packet, fmt)
        blocks, offset = [], 0
        for length in fmt.block_lengths:
            blocks.append(symbols[offset:offset + length])
            offset += length
    stream = [training]
    for pilot, block in zip(pilots, blocks):
        stream.extend((pilot, block))
    waveform = .75 * _complex_wave(np.concatenate(stream), fmt).real
    waveform = np.pad(waveform, (int(GUARD_SECONDS * FS),) * 2)
    return resample_poly(waveform, 4, 1).astype(np.float32)


def activity(key, diagnostics):
    return {"card": {}, "pixels": [], "sample_rate": FS,
            "receive_profile": key, "valid_packet_count": 0,
            "receive_activity": "Receiving experimental burst · synchronization acquired",
            "diagnostics": diagnostics}


def acquire_candidates(source, fmt):
    """Noncoherent training detection tolerates phase changes and fading.

    Accumulate known-tone energies rather than requiring one constant channel
    phase over all 64 training symbols. The null mean is 1 / tone_count; a
    bounded candidate list is provisional until RS/LDPC and CRC validate it.
    """
    training = format_constants_for(fmt)[0]
    step = FS // fmt.baud
    length = len(training) * step
    if len(source) < length:
        raise ValueError("experimental FSK training sequence is incomplete")
    analytic = hilbert(source)
    if np.max(abs(analytic)) < 1e-12:
        raise ValueError("no experimental FSK signal energy")
    t = np.arange(step) / FS
    candidates = []
    # The score's noise distribution is independent of input gain.
    threshold = 1 / fmt.tones + 3.5 * math.sqrt(
        (fmt.tones - 1) / (fmt.tones**2 * (fmt.tones + 1) * TRAINING))
    frame_samples = (fmt.symbol_count * step)
    for offset in range(-120, 121, 20):
        bank = np.asarray([abs(fftconvolve(analytic,
            np.exp(-2j * np.pi * (tone + offset) * t)[::-1], 'valid'))**2
            for tone in fmt.frequencies])
        for phase in sorted(set(int(v) for v in np.linspace(0, step-1, 4))):
            energies = bank[:, phase::step]
            count = energies.shape[1] - TRAINING + 1
            if count <= 0:
                continue
            selected = np.zeros(count)
            # Normalize each training symbol independently: deep fades cannot
            # let a single loud symbol dominate synchronization.
            normalized = energies / np.maximum(energies.sum(axis=0), 1e-20)
            for i, tone in enumerate(training):
                selected += normalized[tone, i:i+count]
            score = selected / TRAINING
            peaks, _ = find_peaks(np.pad(score, (1, 1)), height=threshold,
                                  distance=max(1, fmt.symbol_count // 2))
            for index in peaks - 1:
                candidates.append((float(score[index]), int(index * step + phase), offset))
    if not candidates:
        raise ValueError("no matching experimental FSK synchronization")
    selected = []
    for candidate in sorted(candidates, reverse=True):
        if all(abs(candidate[1] - other[1]) >= frame_samples for other in selected):
            selected.append(candidate)
        if len(selected) >= MAX_COPIES:
            break
    return analytic, selected


def _refine_candidate(analytic, start, offset, fmt, radius=.25, frequency_radius=20):
    training = format_constants_for(fmt)[0]
    first = max(0, math.floor(start - (radius+1)*FS/fmt.baud))
    segment = analytic[first:min(len(analytic), math.ceil(start+(TRAINING+radius+1)*FS/fmt.baud))]
    best = None
    for fraction in np.arange(-radius, radius+.01, .125):
        trial = max(0, start + fraction*FS/fmt.baud - first)
        for shift in np.arange(offset-frequency_radius, offset+frequency_radius+.01,
                               10 if frequency_radius > 20 else 2):
            corrected = segment * np.exp(-2j*np.pi*shift*np.arange(len(segment))/FS)
            rows = _tone_metrics(corrected, trial, fmt, FS/fmt.baud, np.arange(TRAINING))
            if len(rows) != TRAINING:
                continue
            score = float(np.sum(rows[np.arange(TRAINING), training]))
            if best is None or score > best[0]:
                best = score, trial + first, float(shift)
    if best is not None and frequency_radius > 20:
        for shift in np.arange(best[2]-6,best[2]+6.01,2):
            corrected = segment * np.exp(-2j*np.pi*shift*np.arange(len(segment))/FS)
            rows = _tone_metrics(corrected,best[1]-first,fmt,FS/fmt.baud,np.arange(TRAINING))
            score = float(np.sum(rows[np.arange(TRAINING),training]))
            if score > best[0]:
                best = score,best[1],float(shift)
    return (start, offset) if best is None else (best[1], best[2])


def acquire(source, fmt):
    analytic, candidates = acquire_candidates(source, fmt)
    score, start, offset = candidates[0]
    start, offset = _refine_candidate(analytic, start, offset, fmt)
    shifted = analytic * np.exp(-2j*np.pi*offset*np.arange(len(analytic))/FS)
    return shifted, start, {"acquisition_score": score, "carrier_offset_hz": offset,
                           "start_sample": start, "sample_rate": FS,
                           "acquisition_method": "noncoherent known-tone training"}


def _tone_metrics(analytic, start, fmt, step, symbol_indices=None):
    sps = FS // fmt.baud
    count = min(fmt.symbol_count, max(0, int((len(analytic) - start) / step)))
    indices = np.arange(count) if symbol_indices is None else np.asarray(symbol_indices)
    positions = start + (indices[:, None] + np.arange(sps)[None, :] / sps) * step
    symbols = np.interp(positions.ravel(), np.arange(len(analytic)), analytic).reshape(len(indices), sps)
    templates = np.exp(2j * np.pi * fmt.frequencies[:, None] * np.arange(sps) / FS)
    return abs(symbols @ templates.conj().T / sps) ** 2


def demodulate_candidates(analytic, start, fmt, *, track_drift=True):
    _training, pilots, _permutation, _labels, _inverse = format_constants_for(fmt)
    pilot_positions, pilot_tones, cursor = [], [], TRAINING
    for known, length in zip(pilots, fmt.block_lengths):
        pilot_positions.extend(range(cursor, cursor + 4))
        pilot_tones.extend(known)
        cursor += 4 + length
    ranked = []
    for ppm in (-160, -80, 0, 80, 160):
        step = FS / fmt.baud * (1 + ppm / 1e6)
        # Correlation peaks shift under multipath. Independently choose the
        # symbol boundaries using distributed pilots, not the preamble alone.
        for fraction in (-.375, -.25, -.125, 0, .125, .25, .375):
            trial_start = max(0, start + fraction * FS / fmt.baud)
            present = (np.asarray(pilot_positions) + 1) * step + trial_start <= len(analytic)
            indices = np.asarray(pilot_positions)[present]
            tones = np.asarray(pilot_tones)[present]
            if not len(indices):
                continue
            rows = _tone_metrics(analytic, trial_start, fmt, step, indices)
            normalized = rows[np.arange(len(rows)), tones] / np.maximum(rows.sum(axis=1), 1e-15)
            score = float(np.mean(normalized))
            ranked.append((score, ppm, trial_start, step))
    if not ranked:
        raise ValueError("waiting for experimental FSK pilots")
    selected = []
    for score,ppm,trial_start,step in sorted(ranked,key=lambda entry:entry[0],reverse=True):
        end = trial_start + fmt.symbol_count*step
        if any(max(abs(trial_start-other[2]),abs(end-(other[2]+fmt.symbol_count*other[3])))
               < .1*FS/fmt.baud for other in selected):
            continue
        selected.append((score,ppm,trial_start,step))
        if len(selected) == 3:
            break
    for rank,(score,ppm,trial_start,step) in enumerate(selected):
        evidence,diagnostics = _fsk_likelihood(analytic,start,fmt,pilot_positions,pilot_tones,
                                             score,ppm,trial_start,step)
        yield evidence,{**diagnostics,'timing_candidate':rank,
                        'retained_timing_candidates':len(selected),
                        'frequency_tracking':'static','frequency_drift_hz_per_second':0.}
    if track_drift:
        tracked = _track_frequency(analytic,fmt,selected[0],pilot_positions,pilot_tones)
        if tracked is not None:
            corrected,tracking = tracked
            for rank,(score,ppm,trial_start,step) in enumerate(selected):
                evidence,diagnostics = _fsk_likelihood(corrected,start,fmt,pilot_positions,pilot_tones,
                                                     score,ppm,trial_start,step)
                yield evidence,{**diagnostics,**tracking,'timing_candidate':rank,
                                'retained_timing_candidates':len(selected)}


def demodulate(analytic, start, fmt):
    return next(demodulate_candidates(analytic,start,fmt))


def announced_fsk_candidates(analytic,fmt,*,radius=2):
    """Preserve narrow acquisition, then retry a wider residual CFO search.

    Opening-header CFO is already removed. Later copies can accumulate extra
    offset under drift; wider search must not replace a valid narrow hypothesis.
    """
    primary = None
    for frequency_radius in (20,120):
        start,offset = _refine_candidate(analytic,(GUARD_SECONDS+.05)*FS,0,fmt,
                                        radius=radius,frequency_radius=frequency_radius)
        if primary is not None and abs(offset-primary[1])<10 and abs(start-primary[0])<.125*FS/fmt.baud:
            continue
        if primary is None:
            primary = start,offset
        shifted = analytic*np.exp(-2j*np.pi*offset*np.arange(len(analytic))/FS)
        for evidence,timing in demodulate_candidates(shifted,start,fmt):
            yield evidence,{**timing,'start_sample':start,'carrier_offset_hz':offset,
                            'frequency_search_radius_hz':frequency_radius,
                            'acquisition_method':'protected boundary + known-tone training'}


def _track_frequency(analytic,fmt,timing,pilot_positions,pilot_tones):
    """Bounded linear residual-CFO fit to known symbols, never data decisions.

    Unknown phase is discarded per symbol so this remains a noncoherent FSK
    receiver. Static hypotheses are always tried first; a noisy drift fit cannot
    remove their evidence. The estimate is local to one physical copy.
    """
    _,_,start,step = timing
    positions = np.asarray(pilot_positions)
    present = start+(positions+1)*step <= len(analytic)
    if present.sum() < 8 or np.ptp(positions[present])*step/FS < .25:
        return None
    training = format_constants_for(fmt)[0]
    indices = np.concatenate((np.arange(TRAINING),positions[present]))
    tones = np.concatenate((training,np.asarray(pilot_tones)[present]))
    t = (np.arange(len(analytic))-start)/FS
    def evaluate(offset,slope):
        corrected = analytic*np.exp(-2j*np.pi*(offset*t+.5*slope*t*t))
        rows = _tone_metrics(corrected,start,fmt,step,indices)
        score = float(np.mean(rows[np.arange(len(rows)),tones]/np.maximum(rows.sum(axis=1),1e-20)))
        return score
    baseline = evaluate(0,0)
    best = baseline,0.,0.
    for offset in (-20.,0.,20.):
        for slope in (-40.,-20.,-10.,0.,10.,20.,40.):
            if abs(offset)+abs(slope)*fmt.seconds > 240:
                continue
            score = evaluate(offset,slope)
            if score > best[0]:
                best = score,offset,slope
    coarse = best
    for offset in (coarse[1]-10,coarse[1],coarse[1]+10):
        for slope in (coarse[2]-5,coarse[2],coarse[2]+5):
            if abs(offset)>30 or abs(slope)>40 or abs(offset)+abs(slope)*fmt.seconds>240:
                continue
            score = evaluate(offset,slope)
            if score > best[0]:
                best = score,offset,slope
    score,offset,slope = best
    if score < baseline+.0005 or (offset == 0 and slope == 0):
        return None
    corrected = analytic*np.exp(-2j*np.pi*(offset*t+.5*slope*t*t))
    return corrected,{'frequency_tracking':'training + pilot linear drift',
                      'frequency_drift_hz_per_second':slope,
                      'residual_frequency_offset_hz':offset,
                      'frequency_fit_score':score,'static_frequency_fit_score':baseline}


def _fsk_likelihood(analytic,start,fmt,pilot_positions,pilot_tones,score,ppm,trial_start,step):
    metrics = _tone_metrics(analytic, trial_start, fmt, step)
    # Noise is estimated from tones at least one symbol-rate away from the
    # known pilot, avoiding leakage from the overlapping nearest tones. For
    # complex Gaussian noise, correlation energy is exponential: median/ln(2)
    # estimates its mean. A weakest-half median systematically understates it.
    present = np.asarray(pilot_positions) < len(metrics)
    pilot_rows = metrics[np.asarray(pilot_positions)[present]]
    known = np.asarray(pilot_tones)[present]
    distant = abs(np.arange(fmt.tones)[None,:]-known[:,None]) >= 2
    noise = max(float(np.median(pilot_rows[distant])) / np.log(2), 1e-8)
    signal = max(float(np.mean(pilot_rows[np.arange(len(known)),known]))-noise, 0.)
    # Noncoherent Gaussian-channel tone likelihood, with unknown phase:
    # log I0(2 sqrt(signal_energy * received_energy) / noise_energy).
    # Unlike raw energy/noise this does not give noise spikes excessive weight.
    argument = 2*np.sqrt(signal*np.maximum(metrics,0))/noise
    evidence = np.log(np.maximum(i0e(argument),1e-300))+argument
    return evidence, {"clock_ppm": ppm, "pilot_score": score,
                      "fractional_timing_samples": trial_start-start,
                      "noise_energy": noise, "pilot_signal_energy": signal,
                      "tone_likelihood": "noncoherent log-I0"}


def _whole_report(key, packet, diagnostics):
    bits = np.unpackbits(np.frombuffer(packet, np.uint8)) ^ weak.constants()[3]
    pixels, metadata = weak.unpack(bits, diagnostics)
    packed = packet[weak.HEADER.size:-4]
    # Explicitly declare the whole-raster verification to the shared assembly
    # service; chunk coverage alone is never proof of a complete new-format card.
    metadata.update(avatar_mode=key, received_blocks=None,
                    palette=[list(color) for color in pixel.MINIMAL_AVATAR_PALETTE],
                    application_protocol_version=1, image_crc32=zlib.crc32(packed),
                    whole_raster_crc32_valid=True, received_copies=diagnostics.get("combined_copies", 1),
                    color_stage="single-burst card · whole-card CRC32 verified")
    return {"card": metadata, "pixels": pixels, "receive_profile": key,
            "sample_rate": FS, "valid_packet_count": 1, "errors": []}


def _ldpc_evidence(key, evidence):
    fmt = FORMATS[key]
    _training, _pilots, permutation, labels, _inverse = format_constants(key)
    cursor, data = TRAINING, []
    for length in fmt.block_lengths:
        cursor += 4
        data.append(evidence[cursor:cursor + length])
        cursor += length
    metrics = np.concatenate(data)
    if len(metrics) < math.ceil(CODE_BITS / fmt.bits_per_symbol):
        raise ValueError("experimental FSK burst is incomplete")
    llrs = np.column_stack([
        logsumexp(metrics[:, (labels & (1 << bit)) == 0], axis=1) -
        logsumexp(metrics[:, (labels & (1 << bit)) != 0], axis=1)
        for bit in range(fmt.bits_per_symbol - 1, -1, -1)]).ravel()[:CODE_BITS]
    ordered = np.zeros(weak.N)
    ordered[permutation] = llrs
    return ordered


def _decode_ldpc(key, evidence, diagnostics):
    try:
        ordered = _ldpc_evidence(key, evidence)
    except ValueError:
        return activity(key, diagnostics)
    return _decode_ldpc_llrs(key, ordered, diagnostics)


def _decode_ldpc_llrs(key, ordered, diagnostics):
    bits, iterations = weak.decode_ldpc(ordered)
    packet = np.packbits(bits ^ weak.constants()[3]).tobytes()
    return _whole_report(key, packet, {**diagnostics, "ldpc_iterations": iterations,
                         "information_bits": weak.K, "coded_bits": CODE_BITS})


def _checked_shard(metrics, index, labels):
    hard = labels[np.argmax(metrics, axis=1)].astype(int).tolist()
    codeword = pixel._tones_to_gf64(hard)

    def checked(values):
        decoded, corrected = rs_decode(values)
        body = pixel._gf64_to_bytes(decoded, 39)
        if body[0] != index or pixel.crc16(body[:-2]) != int.from_bytes(body[-2:], "big"):
            raise ValueError("outer-parity shard checksum failed")
        return body[1:-2], corrected

    try:
        return checked(codeword)
    except ValueError:
        # Bounded soft Chase: test alternatives for the two least-certain
        # GF(64) symbols; acceptance still requires RS syndrome and CRC16.
        ranked = np.argsort(metrics, axis=1)
        confidence = np.sort(metrics, axis=1)[:, -1] - np.sort(metrics, axis=1)[:, -2]
        positions = np.argsort(confidence.reshape(-1, 2).sum(axis=1))[:2]
        alternatives = []
        for position in positions:
            high = labels[ranked[position * 2, -2:]]
            low = labels[ranked[position * 2 + 1, -2:]]
            alternatives.append(list(dict.fromkeys(int(a) * 8 + int(b) for a in high for b in low)))
        for first in alternatives[0]:
            for second in alternatives[1]:
                candidate = codeword[:]
                candidate[int(positions[0])], candidate[int(positions[1])] = first, second
                try:
                    return checked(candidate)
                except ValueError:
                    continue
        raise ValueError("no checked outer-parity shard")


def _partial_outer(key, shards, diagnostics):
    data = bytearray(OUTER_DATA * SHARD_BYTES)
    present = [False] * len(data)
    for index, shard in shards.items():
        if index < OUTER_DATA:
            start = index * SHARD_BYTES
            data[start:start + SHARD_BYTES] = shard
            present[start:start + SHARD_BYTES] = [True] * SHARD_BYTES
    if not all(present[:weak.HEADER.size]):
        return activity(key, diagnostics)
    magic, version, width, height, palette, nc, ng, ident, call, grid, kind, snr = weak.HEADER.unpack_from(data)
    if (magic != b"PQW1" or version != 1 or (width, height, palette) != (32, 32, 0) or
            not 1 <= nc <= 12 or not 1 <= ng <= 8 or kind not in weak.KINDS.values() or
            any(call[nc:]) or any(grid[ng:])):
        raise ValueError("invalid outer-parity identity")
    coverage = [all(present[weak.HEADER.size + (i * 3 // 8):
                              weak.HEADER.size + ((i * 3 + 2) // 8) + 1]) for i in range(1024)]
    pixels = pixel.unpack_indices(data[weak.HEADER.size:weak.HEADER.size + 384], 1024)
    metadata = dict(callsign=call[:nc].decode("ascii"), grid=grid[:ng].decode("ascii"),
        card_id=ident, width=32, height=32, palette_id=0,
        message_type=next(k for k, v in weak.KINDS.items() if v == kind),
        snr_db=None if snr == -128 else snr, avatar_mode=key, avatar_burst=True,
        palette=[list(color) for color in pixel.MINIMAL_AVATAR_PALETTE],
        application_protocol_version=1, exact=False,
        received_copies=1, pixel_coverage=coverage, verified_pixel_coverage=coverage,
        outer_checked_shards=sorted(shards), diagnostics=diagnostics,
        color_stage=f"{len(shards)}/16 checked shards · waiting for outer parity")
    return {"card": metadata, "pixels": pixels, "receive_profile": key,
            "sample_rate": FS, "valid_packet_count": len(shards), "errors": []}


def _decode_outer(key, evidence, diagnostics):
    labels = format_constants(key)[3]
    shards, corrected, cursor = {}, 0, TRAINING
    for index, length in enumerate(FORMATS[key].block_lengths):
        cursor += 4
        if cursor + length > len(evidence):
            break
        try:
            shard, count = _checked_shard(evidence[cursor:cursor + length], index, labels)
            shards[index] = shard
            corrected += count
        except ValueError:
            pass
        cursor += length
    diagnostics = {**diagnostics, "checked_shards": len(shards), "corrected_rs_symbols": corrected}
    if len(shards) >= OUTER_DATA:
        recovered = outer_recover(shards)[:PACKET_BYTES]
        missing = [i for i in range(OUTER_DATA) if i not in shards]
        return _whole_report(key, recovered, {**diagnostics, "outer_recovered_shards": missing})
    return _partial_outer(key, shards, diagnostics)


def decode(key, audio, sample_rate=48000):
    if key == QPSK_KEY:
        return decode_equalized(audio, sample_rate)
    fmt = FORMATS[key]
    source = _resample(audio, sample_rate, FS)
    analytic, candidates = acquire_candidates(source, fmt)
    frames, partial = [], None
    error = ValueError("no checked experimental FSK card")
    for score, start, offset in candidates:
        start, offset = _refine_candidate(analytic, start, offset, fmt)
        diagnostic = dict(acquisition_score=score, carrier_offset_hz=offset,
                          start_sample=start, sample_rate=FS,
                          acquisition_method="noncoherent known-tone training")
        if len(analytic) - start < (TRAINING + 4) * FS / fmt.baud:
            partial = (partial or activity(key, diagnostic)) if score >= .35 or ADMITTED_IDENTITY.get() is not None else partial
            continue
        shifted = analytic * np.exp(-2j*np.pi*offset*np.arange(len(analytic))/FS)
        evidence, timing = demodulate(shifted, start, fmt)
        diagnostic.update(timing)
        try:
            report = (_decode_outer(key, evidence, diagnostic) if fmt.outer else
                      _decode_ldpc(key, evidence, diagnostic))
            if report.get('card', {}).get('exact'):
                return report
            if (report.get('card') or score >= .35 or ADMITTED_IDENTITY.get() is not None) and (partial is None or sum(report.get('card', {}).get('pixel_coverage') or []) > sum(partial.get('card', {}).get('pixel_coverage') or [])):
                partial = report
        except ValueError as exc:
            error = exc
        if len(evidence) < fmt.symbol_count:
            continue
        # Outer RS combines tone log evidence; LDPC combines bit LLRs.
        frame = SoftCopy(evidence[:fmt.symbol_count] if fmt.outer else
                         _ldpc_evidence(key, evidence), diagnostic)
        variants = [frame]
        for alternate,timing in list(demodulate_candidates(shifted,start,fmt))[1:]:
            if len(alternate) >= fmt.symbol_count:
                variants.append(SoftCopy(alternate[:fmt.symbol_count] if fmt.outer else
                    _ldpc_evidence(key,alternate),{**diagnostic,**timing}))
        frames.append(variants)
    for evidence, used in hypothesis_attempts(frames):
        diagnostic = diagnostics_for(used, len(candidates))
        try:
            report = (_decode_outer(key, evidence, diagnostic) if fmt.outer else
                      _decode_ldpc_llrs(key, evidence, diagnostic))
            if report.get('card', {}).get('exact'):
                return report
            if partial is None or sum(report.get('card', {}).get('pixel_coverage') or []) > sum(partial.get('card', {}).get('pixel_coverage') or []):
                partial = report
        except ValueError as exc:
            error = exc
    if partial is not None:
        return partial
    raise error


def _adaptive_soft_frame(source, candidate):
    baseline, diagnostic = weak.soft_frame(source, candidate)
    training, pilots, permutation, _whitening, h = weak.constants()
    start = candidate[1]
    step = weak.SPS * (1 + diagnostic["clock_ppm"] / 1e6)
    fraction = diagnostic.get("fractional_timing_samples", 0)
    time = np.arange(len(source)) / weak.FS
    base = np.sqrt(2) * source * np.exp(-2j * np.pi *
        (weak.CARRIER + diagnostic["carrier_offset_hz"]) * time)
    matched = fftconvolve(base, h)
    count = weak.PREAMBLE + weak.BLOCKS * (weak.PILOTS + weak.DATA)
    offsets = (np.arange(21) - 10) * (weak.SPS / 2)
    positions = start + fraction + np.arange(count)[:, None] * step + offsets
    if positions[-1, -1] >= len(matched):
        raise ValueError("equalized QPSK burst is incomplete")
    vectors = np.interp(positions.ravel(), np.arange(len(matched)), matched).reshape(count, 21)
    # Fractionally spaced, regularized training solve, followed by pilot and
    # confidence-gated decision-directed NLMS throughout this same burst.
    train = vectors[20:weak.PREAMBLE - 20]
    target = training[20:weak.PREAMBLE - 20]
    gram = train.conj().T @ train
    ridge = max(float(np.trace(gram).real / len(gram)) * .005, 1e-8)
    weights = np.linalg.solve(gram + ridge * np.eye(21), train.conj().T @ target)
    output, pilot_errors, cursor = [], [], weak.PREAMBLE
    for block in range(weak.BLOCKS):
        known_outputs = []
        for j in range(weak.PILOTS):
            vector = vectors[cursor]
            value = vector @ weights
            desired = pilots[block, j]
            known_outputs.append(value)
            weights += .08 * vector.conj() * (desired - value) / max(float(np.vdot(vector, vector).real), 1e-8)
            cursor += 1
        gain = np.mean(np.asarray(known_outputs) * pilots[block].conj())
        if abs(gain) < .05:
            gain = .05 + 0j
        pilot_errors.extend(abs(np.asarray(known_outputs) / gain - pilots[block]) ** 2)
        for j in range(weak.DATA):
            vector = vectors[cursor]
            value = vector @ weights / gain
            output.append(value)
            decision = ((1 if value.real >= 0 else -1) +
                        1j * (1 if value.imag >= 0 else -1)) / math.sqrt(2)
            if abs(value - decision) < .35:
                weights += .012 * vector.conj() * (decision * gain - vector @ weights) / max(float(np.vdot(vector, vector).real), 1e-8)
            cursor += 1
    variance = max(float(np.mean(pilot_errors)), .01)
    values = np.asarray(output)
    llrs = np.empty(weak.N)
    llrs[0::2] = 2 * math.sqrt(2) * values.real / variance
    llrs[1::2] = 2 * math.sqrt(2) * values.imag / variance
    ordered = np.empty(weak.N)
    ordered[permutation] = llrs
    return ordered, {**diagnostic, "equalizer": "21-tap fractionally spaced training + pilot/DD NLMS",
                     "equalizer_tap_spacing_seconds": 1 / 4000,
                     "equalizer_pilot_mse": variance}, baseline


def decode_equalized(audio, sample_rate=48000):
    source = _resample(audio, sample_rate, weak.FS)
    if not np.any(source):
        raise ValueError("no experimental QPSK signal energy")
    candidates = sorted(weak.acquire(source), reverse=True)
    error = ValueError("no complete equalized QPSK card")
    incomplete = None
    frame_samples = (weak.PREAMBLE + weak.BLOCKS * (weak.PILOTS + weak.DATA)) * weak.SPS
    frames = {"adaptive_equalizer": [], "pilot_receiver_fallback": []}
    for candidate in candidates:
        if len(source) < candidate[1] + frame_samples:
            incomplete = activity(QPSK_KEY, {"acquisition_score": candidate[0]})
            continue
        try:
            llrs, diagnostics, baseline = _adaptive_soft_frame(source, candidate)
            for receiver, evidence in (("adaptive_equalizer", llrs), ("pilot_receiver_fallback", baseline)):
                frame = SoftCopy(evidence, diagnostics)
                frames[receiver].append(frame)
                try:
                    bits, iterations = weak.decode_ldpc(evidence)
                    packet = np.packbits(bits ^ weak.constants()[3]).tobytes()
                    return _whole_report(QPSK_KEY, packet, {
                        **diagnostics_for([frame], len(candidates)), "ldpc_iterations": iterations,
                        "successful_receiver": receiver})
                except ValueError as exc:
                    error = exc
        except ValueError as exc:
            error = exc
    for receiver, copies in frames.items():
        for evidence, used in recovery_attempts(copies):
            if len(used) < 2:
                continue
            try:
                bits, iterations = weak.decode_ldpc(evidence)
                packet = np.packbits(bits ^ weak.constants()[3]).tobytes()
                return _whole_report(QPSK_KEY, packet, {
                    **diagnostics_for(used, len(candidates)), "ldpc_iterations": iterations,
                    "successful_receiver": receiver})
            except ValueError as exc:
                error = exc
    if incomplete is not None:
        return incomplete
    raise error


def decode_copies(key, captures, sample_rate=48000, *, grouped=False):
    """Use protected payload boundaries for independent copy acquisition."""
    frames, partial = [], None
    error=ValueError('no CRC-valid announced experimental card')
    if key == QPSK_KEY:
        receivers={'adaptive_equalizer':[], 'pilot_receiver_fallback':[]}
        for audio in captures:
            source=_resample(audio,sample_rate,weak.FS)
            try:
                candidate=weak.announced_candidate(source)
                evidence, diagnostic, baseline=_adaptive_soft_frame(source,candidate)
                receivers['adaptive_equalizer'].append(SoftCopy(evidence,diagnostic))
                receivers['pilot_receiver_fallback'].append(SoftCopy(baseline,diagnostic))
            except ValueError as exc:
                error=exc
        for receiver,copies in receivers.items():
            for evidence,used in recovery_attempts(copies):
                try:
                    bits,iterations=weak.decode_ldpc(evidence)
                    packet=np.packbits(bits^weak.constants()[3]).tobytes()
                    return _whole_report(key,packet,{**diagnostics_for(used,len(copies)),
                        'ldpc_iterations':iterations,'successful_receiver':receiver,
                        'acquisition_method':'protected boundary + local QPSK training'})
                except ValueError as exc:
                    error=exc
        if not any(receivers.values()):
            return activity(key,{'announced_copies':len(captures)})
        raise error
    fmt=FORMATS[key]
    for copy_index, audio in enumerate(captures):
        source=_resample(audio,sample_rate,FS)
        if len(source)<(GUARD_SECONDS+.05+(TRAINING+4)/fmt.baud)*FS:
            partial=partial or activity(key,{'announced_copies':len(captures)})
            continue
        analytic=hilbert(source)
        radius = max(2, (.002 + copy_index*fmt.seconds*.0002)*fmt.baud) if grouped else 2
        variants = []
        for evidence,timing in announced_fsk_candidates(analytic,fmt,radius=radius):
            diagnostic = timing
            try:
                if len(evidence)<fmt.symbol_count:
                    partial=partial or (_decode_outer(key,evidence,diagnostic) if fmt.outer else activity(key,diagnostic))
                    continue
                variants.append(SoftCopy(evidence[:fmt.symbol_count] if fmt.outer else _ldpc_evidence(key,evidence),diagnostic))
            except ValueError as exc:
                error=exc
        if variants: frames.append(variants)
    for evidence,used in hypothesis_attempts(frames):
        try:
            diagnostic=diagnostics_for(used,len(frames))
            report=(_decode_outer(key,evidence,diagnostic) if fmt.outer else _decode_ldpc_llrs(key,evidence,diagnostic))
            if report.get('card',{}).get('exact'):
                return report
            if partial is None or sum(report.get('card',{}).get('pixel_coverage') or [])>sum(partial.get('card',{}).get('pixel_coverage') or []):
                partial=report
        except ValueError as exc:
            error=exc
    if partial is not None:
        return partial
    raise error


def seconds(key):
    return weak.seconds() if key == QPSK_KEY else FORMATS[key].seconds


def minimum_seconds(key):
    return weak.PREAMBLE / 2000 if key == QPSK_KEY else TRAINING / FORMATS[key].baud

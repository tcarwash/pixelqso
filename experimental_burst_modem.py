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
from scipy.signal import fftconvolve, hilbert, resample_poly
from scipy.special import logsumexp

import cardmodem as pixel
import weak_signal_modem as weak
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
        count = math.ceil(CODE_BITS / self.bits_per_symbol)
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
    fmt = FORMATS[key]
    rng = np.random.default_rng(fmt.seed)
    training = rng.integers(0, fmt.tones, TRAINING)
    pilots = rng.integers(0, fmt.tones, (len(fmt.block_lengths), 4))
    permutation = rng.permutation(CODE_BITS)
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


def acquire(source, fmt):
    """Mode-specific correlation; bounded CFO search, no noise-only promotion."""
    training = format_constants(fmt.key)[0]
    reference = _complex_wave(training, fmt)
    if len(source) < len(reference):
        raise ValueError("experimental FSK training sequence is incomplete")
    analytic = hilbert(source)
    energy = fftconvolve(abs(analytic) ** 2, np.ones(len(reference)), "valid")
    if float(np.max(energy)) < 1e-12:
        raise ValueError("no experimental FSK signal energy")
    best = None
    time = np.arange(len(reference)) / FS
    for offset in range(-120, 121, 10):
        template = reference * np.exp(2j * np.pi * offset * time)
        correlation = fftconvolve(analytic, template[::-1].conj(), "valid")
        score = abs(correlation) ** 2 / np.maximum(energy * len(reference), 1e-16)
        at = int(np.argmax(score))
        if best is None or score[at] > best[0]:
            best = float(score[at]), at, offset
    if best[0] < .32:
        raise ValueError("no matching experimental FSK synchronization")
    score, start, coarse = best
    segment = analytic[start:start + len(reference)]
    metrics = [(abs(np.vdot(reference * np.exp(2j * np.pi * offset * time), segment)), offset)
               for offset in np.arange(coarse - 6, coarse + 6.01, .5)]
    offset = max(metrics)[1]
    analytic *= np.exp(-2j * np.pi * offset * np.arange(len(analytic)) / FS)
    return analytic, start, {"acquisition_score": score, "carrier_offset_hz": float(offset),
                             "start_sample": start, "sample_rate": FS}


def _tone_metrics(analytic, start, fmt, step, symbol_indices=None):
    sps = FS // fmt.baud
    count = min(fmt.symbol_count, max(0, int((len(analytic) - start) / step)))
    indices = np.arange(count) if symbol_indices is None else np.asarray(symbol_indices)
    positions = start + (indices[:, None] + np.arange(sps)[None, :] / sps) * step
    symbols = np.interp(positions.ravel(), np.arange(len(analytic)), analytic).reshape(len(indices), sps)
    templates = np.exp(2j * np.pi * fmt.frequencies[:, None] * np.arange(sps) / FS)
    return abs(symbols @ templates.conj().T / sps) ** 2


def demodulate(analytic, start, fmt):
    _training, pilots, _permutation, _labels, _inverse = format_constants(fmt.key)
    pilot_positions, pilot_tones, cursor = [], [], TRAINING
    for known, length in zip(pilots, fmt.block_lengths):
        pilot_positions.extend(range(cursor, cursor + 4))
        pilot_tones.extend(known)
        cursor += 4 + length
    best = None
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
            if best is None or score > best[0]:
                best = score, ppm, trial_start, step
    if best is None:
        raise ValueError("waiting for experimental FSK pilots")
    score, ppm, trial_start, step = best
    metrics = _tone_metrics(analytic, trial_start, fmt, step)
    # Soft evidence remains unquantized. Overlapping-tone leakage is excluded
    # from the noise scale by estimating the weaker half of each pilot bank.
    pilot_rows = metrics[np.asarray(pilot_positions)[np.asarray(pilot_positions) < len(metrics)]]
    noise = max(float(np.median(np.sort(pilot_rows, axis=1)[:, :fmt.tones // 2])), 1e-5)
    scale = max(noise, float(np.median(np.max(pilot_rows, axis=1))) * .055)
    return metrics / scale, {"clock_ppm": ppm, "pilot_score": score,
                             "fractional_timing_samples": trial_start - start}


def _whole_report(key, packet, diagnostics):
    bits = np.unpackbits(np.frombuffer(packet, np.uint8)) ^ weak.constants()[3]
    pixels, metadata = weak.unpack(bits, diagnostics)
    packed = packet[weak.HEADER.size:-4]
    # Explicitly declare the whole-raster verification to the shared assembly
    # service; chunk coverage alone is never proof of a complete new-format card.
    metadata.update(avatar_mode=key, received_blocks=None,
                    palette=[list(color) for color in pixel.MINIMAL_AVATAR_PALETTE],
                    application_protocol_version=1, image_crc32=zlib.crc32(packed),
                    whole_raster_crc32_valid=True, received_copies=1,
                    color_stage="single-burst card · whole-card CRC32 verified")
    return {"card": metadata, "pixels": pixels, "receive_profile": key,
            "sample_rate": FS, "valid_packet_count": 1, "errors": []}


def _decode_ldpc(key, evidence, diagnostics):
    fmt = FORMATS[key]
    _training, _pilots, permutation, labels, _inverse = format_constants(key)
    cursor, data = TRAINING, []
    for length in fmt.block_lengths:
        cursor += 4
        data.append(evidence[cursor:cursor + length])
        cursor += length
    metrics = np.concatenate(data)
    if len(metrics) < math.ceil(CODE_BITS / fmt.bits_per_symbol):
        return activity(key, diagnostics)
    llrs = np.column_stack([
        logsumexp(metrics[:, (labels & (1 << bit)) == 0], axis=1) -
        logsumexp(metrics[:, (labels & (1 << bit)) != 0], axis=1)
        for bit in range(fmt.bits_per_symbol - 1, -1, -1)]).ravel()[:CODE_BITS]
    ordered = np.zeros(weak.N)
    ordered[permutation] = llrs
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
    analytic, start, diagnostics = acquire(source, fmt)
    if len(analytic) - start < (TRAINING + 4) * FS / fmt.baud:
        return activity(key, diagnostics)
    evidence, timing = demodulate(analytic, start, fmt)
    diagnostics.update(timing)
    if fmt.outer:
        return _decode_outer(key, evidence, diagnostics)
    return _decode_ldpc(key, evidence, diagnostics)


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
    for candidate in candidates:
        if len(source) < candidate[1] + frame_samples:
            incomplete = activity(QPSK_KEY, {"acquisition_score": candidate[0]})
            continue
        try:
            llrs, diagnostics, baseline = _adaptive_soft_frame(source, candidate)
            for receiver, evidence in (("adaptive_equalizer", llrs), ("pilot_receiver_fallback", baseline)):
                try:
                    bits, iterations = weak.decode_ldpc(evidence)
                    packet = np.packbits(bits ^ weak.constants()[3]).tobytes()
                    return _whole_report(QPSK_KEY, packet, {
                        **diagnostics, "ldpc_iterations": iterations, "successful_receiver": receiver})
                except ValueError as exc:
                    error = exc
        except ValueError as exc:
            error = exc
    if incomplete is not None:
        return incomplete
    raise error


def seconds(key):
    return weak.seconds() if key == QPSK_KEY else FORMATS[key].seconds


def minimum_seconds(key):
    return weak.PREAMBLE / 2000 if key == QPSK_KEY else TRAINING / FORMATS[key].baud

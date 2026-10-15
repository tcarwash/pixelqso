"""Versioned, common 100-baud mode/copy header for local experimental modems.

Stable mode IDs are wire protocol IDs, never UI ordering. Two independently
protected markers bracket each payload; either surviving marker identifies it.
Legacy unannounced captures remain readable via native modem acquisition.
"""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
import struct
import zlib
import numpy as np
from scipy.fft import fft, ifft, next_fast_len
from scipy.signal import hilbert, resample_poly, find_peaks
import cardmodem as pixel
from experimental_fec import rs_encode, rs_decode
from copy_combining import MAX_COPIES, ADMITTED_IDENTITY
from snr import set_measured_snr

RATE = 8000
BAUD = 100
TONES = np.arange(800, 1600, 100)
TRAINING = 32
BODY = struct.Struct('>4sBBI16s')
VERSION = 1
GROUP_VERSION = 2
COMPRESSED_VERSION = 3
INTERLACED_VERSION = 4
ANIMATION_VERSION = 5  # Optional sidecar; never replaces a normal copy header.
COMPRESSED_MODES = frozenset((
    'experimental_8fsk_compressed', 'experimental_16fsk_compressed', 'experimental_16fsk_grid'))
GROUP_MODES = {
    'experimental_8fsk_grouped': 'experimental_8fsk_ldpc',
    'experimental_16fsk_grouped': 'experimental_16fsk_ldpc',
}
PARITY = 20
DATA_SYMBOLS = (BODY.size + 4) * 8 // 6
CODE_SYMBOLS = DATA_SYMBOLS + PARITY
SYMBOLS = TRAINING + CODE_SYMBOLS * 2
GUARD = .04
HEADER_SECONDS = SYMBOLS / BAUD + 2 * GUARD
PROFILE = pixel.ModemProfile('copy-header-v1', 'Copy header', BAUD, tuple(TONES), 900)
MODE_IDS = {
    'standard': 1, 'fast_avatar': 2, 'fast_avatar_fec': 3,
    'fast_avatar_fec_v4': 4, 'resilient_100': 5, 'resilient_50': 6,
    'resilient_25': 7, 'experimental_qpsk_5s': 8,
    'experimental_qpsk_combined': 9, 'experimental_qpsk_equalized': 10,
    'experimental_8fsk_ldpc': 11, 'experimental_8fsk_outer_rs': 12,
    'experimental_16fsk_ldpc': 13,
    'experimental_8fsk_grouped': 14, 'experimental_16fsk_grouped': 15,
    'experimental_8fsk_compressed': 16, 'experimental_16fsk_compressed': 17, 'experimental_16fsk_grid': 18,
    'experimental_4fsk_orthogonal': 19, 'experimental_8fsk_orthogonal': 20,
    'experimental_8fsk_musical_25': 21,
}
ID_MODES = {v: k for k, v in MODE_IDS.items()}
# Fixed native LDPC payload sizes are part of the grouped v2 specification.
GROUP_PAYLOAD_SAMPLES = {'experimental_8fsk_grouped': 240240,
                         'experimental_16fsk_grouped': 270720}


def group_version(key):
    return COMPRESSED_VERSION if key in COMPRESSED_MODES else GROUP_VERSION


def valid_group_length(key, samples):
    if key in COMPRESSED_MODES:
        from compressed_fsk_modem import capacity_for_samples
        try:
            capacity_for_samples(key,samples)
            return True
        except ValueError:
            return False
    return key in GROUP_MODES and samples == GROUP_PAYLOAD_SAMPLES[key]


@lru_cache(None)
def constants():
    rng = np.random.default_rng(0x50514348)
    return rng.integers(0, 8, TRAINING), rng.permutation(CODE_SYMBOLS * 2)


def _content_digest(key, callsign, grid, card_id, width, height, colors, message_type, snr_db):
    data = dict(mode=key, callsign=callsign.upper(), grid=grid.upper(),
                card_id=int(card_id)&65535, width=width, height=height, colors=colors)
    if key != 'standard':
        data.update(message_type=message_type, snr_db=snr_db)
    return hashlib.blake2s(json.dumps(data, sort_keys=True, separators=(',', ':')).encode(),
                          digest_size=16).digest()


def content_id(key, card, message_type, snr_db):
    if key == 'standard':
        colors = [list(card.palette[index]) for index in card.pixels]
    else:
        palette_id = pixel._avatar_palette_id(card)
        palette = pixel.AVATAR_PALETTES[palette_id]
        indices = pixel._unpack_indices(pixel.minimal_avatar_payload(card), len(card.pixels),
                                       pixel.AVATAR_PALETTE_BITS[palette_id])
        colors = [list(palette[index]) for index in indices]
    digest = _content_digest(key, card.callsign, card.grid, card.card_id, card.width, card.height,
                             colors, message_type, snr_db)
    if key in COMPRESSED_MODES:
        from compressed_fsk_modem import pack
        packet,_,_ = pack(card,message_type,snr_db)
        digest = hashlib.blake2s(digest + hashlib.blake2s(packet,digest_size=16).digest(),
                                 digest_size=16).digest()
    return digest


def report_content_id(key, report):
    card = report['card']
    pixels = report['pixels']
    if key == 'standard':
        colors = [list(rgb) for row in pixels for rgb in row]
    else:
        colors = [list(card['palette'][index]) for index in pixels]
    digest = _content_digest(key, card['callsign'], card['grid'], card['card_id'],
                             card.get('width',32), card.get('height',32), colors,
                             card.get('message_type','card'), card.get('snr_db'))
    if key in COMPRESSED_MODES:
        try:
            wire_digest = bytes.fromhex(card['diagnostics']['compressed_packet_digest'])
        except (KeyError,TypeError,ValueError) as exc:
            raise ValueError('missing compressed wire identity') from exc
        if len(wire_digest) != 16:
            raise ValueError('invalid compressed wire identity')
        digest = hashlib.blake2s(digest+wire_digest,digest_size=16).digest()
    return digest


def header_symbols(key, payload_samples, identity, *, trailer=False, copies=None, copy_index=None):
    if key not in MODE_IDS or not 0 < payload_samples <= 48000 * 1800 or len(identity) != 16:
        raise ValueError('invalid copy header')
    if (key in GROUP_MODES or key in COMPRESSED_MODES) and copies is None:
        raise ValueError('grouped modes require a protected copy count')
    mode = MODE_IDS[key] | (128 if trailer else 0)
    version = VERSION
    if copies is not None:
        if not valid_group_length(key,payload_samples) or trailer or int(copies) != copies or not 1 <= copies <= MAX_COPIES:
            raise ValueError('invalid grouped header')
        version = group_version(key)
        payload_samples |= (int(copies)-1) << 27
    if copy_index is not None:
        if (copies is not None or int(copy_index) != copy_index or
                not 0 <= int(copy_index) < MAX_COPIES or payload_samples >= (1 << 27)):
            raise ValueError('invalid interlaced copy header')
        version = INTERLACED_VERSION
        payload_samples |= int(copy_index) << 27
    body = BODY.pack(b'PQCH', version, mode, payload_samples, identity)
    return _protected_symbols(body)


def _protected_symbols(body):
    wire = body + struct.pack('>I', zlib.crc32(body))
    coded = rs_encode(pixel._bytes_to_gf64(wire), PARITY)
    tones = np.asarray(pixel._gf64_to_tones(coded))
    training, permutation = constants()
    return np.concatenate((training, tones[permutation]))


def animation_audio(key, card):
    from card_animation import encode_link, LINK
    frame = encode_link(card)
    if frame is None:
        return np.empty(0, np.float32)
    _, _, first, duration, first_digest, second_digest = LINK.unpack(frame[:-4])
    body = BODY.pack(b'PQCH', ANIMATION_VERSION, MODE_IDS[key],
                     (first << 16) | duration, first_digest + second_digest)
    audio = pixel.synthesize(_protected_symbols(body), sample_rate=RATE, profile=PROFILE)
    return resample_poly(np.pad(audio, (round(GUARD*RATE),)*2), 6, 1).astype(np.float32)


def _animation_framed(key, card, audio):
    marker = animation_audio(key, card)
    if not len(marker):
        return audio
    rms = np.sqrt(np.mean(np.asarray(audio, float)**2))
    marker *= rms / max(np.sqrt(np.mean(marker.astype(float)**2)), 1e-12)
    return np.concatenate((marker, audio, marker))


def header_audio(key, payload_samples, identity, *, trailer=False, copies=None, copy_index=None):
    audio = pixel.synthesize(header_symbols(key, payload_samples, identity, trailer=trailer,
                                           copies=copies, copy_index=copy_index),
                             sample_rate=RATE, profile=PROFILE)
    return resample_poly(np.pad(audio, (round(GUARD*RATE),)*2), 6, 1).astype(np.float32)


def transmit(key, card, payload, copies, message_type='card', snr_db=None):
    if int(copies) != copies or not 1 <= copies <= MAX_COPIES:
        raise ValueError(f'copy count must be between 1 and {MAX_COPIES}')
    identity = content_id(key, card, message_type, snr_db)
    prefix = header_audio(key, len(payload), identity)
    suffix = header_audio(key, len(payload), identity, trailer=True)
    # Header and payload have comparable RMS. Do not distort the payload.
    rms = np.sqrt(np.mean(np.asarray(payload, float)**2))
    for marker in (prefix, suffix):
        marker *= rms / max(np.sqrt(np.mean(marker.astype(float)**2)), 1e-12)
    return np.tile(_animation_framed(key, card, np.concatenate((prefix, payload, suffix))), int(copies))


def transmit_interlaced(key, card, copies, payload_for_copy, message_type='card', snr_db=None):
    """Frame independently interlaced payload copies with protected sequence indices."""
    if int(copies) != copies or not 1 <= copies <= MAX_COPIES:
        raise ValueError(f'copy count must be between 1 and {MAX_COPIES}')
    identity = content_id(key, card, message_type, snr_db)
    frames = []
    expected_samples = None
    for index in range(int(copies)):
        payload = np.asarray(payload_for_copy(index), dtype=np.float32)
        if payload.ndim != 1 or not len(payload):
            raise ValueError('interlaced payload must be a nonempty mono waveform')
        if expected_samples is None:
            expected_samples = len(payload)
        elif len(payload) != expected_samples:
            raise ValueError('interlaced payload copies must have equal duration')
        prefix = header_audio(key, len(payload), identity, copy_index=index)
        suffix = header_audio(key, len(payload), identity, trailer=True, copy_index=index)
        rms = np.sqrt(np.mean(payload.astype(float)**2))
        for marker in (prefix, suffix):
            marker *= rms / max(np.sqrt(np.mean(marker.astype(float)**2)), 1e-12)
        frames.append(_animation_framed(key, card, np.concatenate((prefix, payload, suffix))))
    return np.concatenate(frames)


def transmit_group(key, card, payload, copies, message_type='card', snr_db=None):
    if int(copies) != copies or not 1 <= copies <= MAX_COPIES:
        raise ValueError(f'copy count must be between 1 and {MAX_COPIES}')
    if not valid_group_length(key,len(payload)):
        raise ValueError('grouped payload does not match a supported format')
    prefix = header_audio(key, len(payload), content_id(key, card, message_type, snr_db), copies=copies)
    rms = np.sqrt(np.mean(np.asarray(payload, float)**2))
    prefix *= rms / max(np.sqrt(np.mean(prefix.astype(float)**2)), 1e-12)
    return _animation_framed(key, card, np.concatenate((prefix, np.tile(payload, int(copies)))))


def decode_header(metrics, *, soft=False):
    if metrics.shape != (CODE_SYMBOLS * 2, 8):
        raise ValueError('incomplete common copy header')
    _, permutation = constants()
    ordered = np.empty_like(metrics)
    ordered[permutation] = metrics
    hard = np.argmax(ordered, axis=1)
    coded = list(pixel._tones_to_gf64(hard))
    try:
        data, corrected = rs_decode(coded, PARITY)
    except ValueError:
        if not soft:
            raise
        # Bounded Chase trials on uncertain GF64 symbols. Each accepted
        # candidate must still satisfy RS and CRC32 plus version/mode checks.
        # Grouped payloads have fixed native lengths. Use that protocol
        # knowledge to aid RS recovery; enumerate both modes and bounded counts,
        # never content identities. A candidate still needs the original CRC.
        normalized = ordered / np.maximum(ordered.sum(axis=1, keepdims=True), 1e-20)
        # Rank all legal v2/v3 prefixes by received evidence. Do not enumerate
        # identities or CRCs, and cap RS attempts independently of profile count.
        prefixes = header_prefixes()
        prefix_tones = np.stack((prefixes >> 3, prefixes & 7),axis=-1).reshape(len(prefixes), -1)
        scores = np.log(np.maximum(normalized[np.arange(prefix_tones.shape[1]), prefix_tones],1e-20)).sum(axis=1)
        aided = [[(i,int(value)) for i,value in enumerate(prefixes[index])
                  if int(value) != coded[i]] for index in np.argsort(scores)[-12:][::-1]]
        alternatives = []
        for i in range(CODE_SYMBOLS):
            likelihood = normalized[2*i, np.arange(64)>>3] + normalized[2*i+1, np.arange(64)&7]
            ranked = np.argsort(likelihood)[::-1]
            alternatives.append((likelihood[ranked[0]]-likelihood[ranked[1]], i,
                                 [int(v) for v in ranked if int(v) != coded[i]][:2]))
        alternatives.sort(key=lambda v:v[0])
        changes = [[(i,v)] for _,i,values in alternatives[:10] for v in values]
        for left in range(6):
            for right in range(left+1,6):
                _,i,a = alternatives[left]; _,j,b = alternatives[right]
                changes.extend([[(i,u),(j,v)] for u in a for v in b])
        for trial in aided + changes:
            received = coded[:]
            for i,value in trial:
                received[i] = value
            try:
                candidate, corrected = rs_decode(received, PARITY)
                candidate_wire = pixel._gf64_to_bytes(candidate, BODY.size+4)
                if zlib.crc32(candidate_wire[:-4]) == int.from_bytes(candidate_wire[-4:],'big'):
                    data = candidate
                    break
            except ValueError:
                continue
        else:
            raise ValueError('no CRC-valid copy header after soft recovery')
    wire = pixel._gf64_to_bytes(data, BODY.size + 4)
    body, crc = wire[:-4], int.from_bytes(wire[-4:], 'big')
    if zlib.crc32(body) != crc:
        raise ValueError('copy header checksum failed')
    magic, version, mode, samples, identity = BODY.unpack(body)
    if magic == b'PQCH' and version == ANIMATION_VERSION and mode in ID_MODES:
        from card_animation import validate_link
        link = validate_link(dict(first_card_id=samples >> 16, frame_ms=samples & 65535,
                                  first_digest=identity[:8].hex()))
        return dict(mode=ID_MODES[mode], animation_link=(identity[8:].hex(), link),
                    version=version)
    copies = 1
    copy_index = None
    if version == INTERLACED_VERSION:
        copy_index, samples = samples >> 27, samples & ((1 << 27)-1)
        if copy_index >= MAX_COPIES:
            raise ValueError('unsupported interlaced copy index')
    if version in (GROUP_VERSION,COMPRESSED_VERSION):
        copies, samples = (samples >> 27) + 1, samples & ((1 << 27)-1)
        if copies > MAX_COPIES or not valid_group_length(ID_MODES.get(mode),samples) or version != group_version(ID_MODES.get(mode)):
            raise ValueError('unsupported grouped header')
        if mode & 128:
            raise ValueError('group header cannot carry a trailer')
    if version == VERSION and (ID_MODES.get(mode & 127) in GROUP_MODES or ID_MODES.get(mode & 127) in COMPRESSED_MODES):
        raise ValueError('grouped modes require a supported group header')
    if magic != b'PQCH' or version not in (VERSION, GROUP_VERSION, COMPRESSED_VERSION, INTERLACED_VERSION) or mode & 127 not in ID_MODES or not 0 < samples <= 48000*1800:
        raise ValueError('unsupported copy header')
    return dict(mode=ID_MODES[mode & 127], trailer=bool(mode & 128),
                payload_samples=samples, identity=identity.hex(), version=version,
                corrected_symbols=corrected, group_copies=copies, copy_index=copy_index)


@lru_cache(None)
def header_prefixes():
    lengths = list(GROUP_PAYLOAD_SAMPLES.items())
    from compressed_fsk_modem import CAPACITIES, payload_samples
    lengths += [(key,payload_samples(key,capacity))
                for key in sorted(COMPRESSED_MODES) for capacity in CAPACITIES]
    prefixes = []
    for key,samples in lengths:
        for count in range(1,MAX_COPIES+1):
            prefix = struct.pack('>4sBBI',b'PQCH',group_version(key),MODE_IDS[key],
                                 samples | ((count-1)<<27))
            bits = np.unpackbits(np.frombuffer(prefix,np.uint8))[:78].reshape(-1,6)
            prefixes.append(bits @ (1 << np.arange(5,-1,-1)))
    return np.asarray(prefixes)


def _header_metrics(analytic, start, offset, count=SYMBOLS):
    step = RATE // BAUD
    positions = start + np.arange(count*step)
    samples = np.interp(positions,np.arange(len(analytic)),analytic).reshape(count,step)
    templates = np.exp(-2j*np.pi*(TONES[:,None]+offset)*np.arange(step)/RATE)
    return abs(samples @ templates.T / step)**2


def header_snr_db(analytic, start, offset):
    """Estimate header SNR using known training tones and off-tone noise bins."""
    step = RATE // BAUD
    positions = start + np.arange(TRAINING * step)
    if positions[0] < 0 or positions[-1] >= len(analytic):
        return None
    left = np.floor(positions).astype(int)
    fraction = positions - left
    samples = analytic[left] * (1-fraction) + analytic[np.minimum(left+1, len(analytic)-1)] * fraction
    chunks = samples.reshape(TRAINING, step)
    chunks *= np.exp(-2j * np.pi * offset * np.arange(step) / RATE)
    power = abs(np.fft.fft(chunks, axis=1) / step)**2
    training, _ = constants()
    bins = (TONES[training] / BAUD).astype(int)
    # Neighbor bins exclude leakage from clock error, fading, and imperfect CFO.
    mask = np.broadcast_to((np.arange(step) >= 3) & (np.arange(step) <= 28), power.shape).copy()
    for delta in (-1, 0, 1):
        mask[np.arange(TRAINING), bins + delta] = False
    noise = float(np.mean(power[mask]))
    signal = float(np.mean(power[np.arange(TRAINING), bins])) - noise
    if not np.isfinite(noise) or noise <= 0 or signal <= 0:
        return None
    return 10 * np.log10(signal / (noise * 2500 / BAUD))


@lru_cache(None)
def header_known_symbols():
    # The first 30 magic bits form ten tone symbols; their fixed permutation
    # scatters them through the RS body. They are common to every wire version.
    training,permutation = constants()
    tones = np.asarray(pixel._gf64_to_tones(pixel._bytes_to_gf64(b'PQCH')))[:10]
    positions = np.flatnonzero(permutation < 10)
    return np.concatenate((np.arange(TRAINING),positions+TRAINING)),np.concatenate((training,tones[permutation[positions]]))


def _refine_header(analytic,start,offset):
    positions,known = header_known_symbols()
    ranked = []
    step = RATE // BAUD
    for shift in (-10,0,10):
        for fraction in (-.25,-.125,0,.125,.25):
            at = start + fraction*step
            if at < 0 or at+SYMBOLS*step > len(analytic):
                continue
            rows = _header_metrics(analytic,at,offset+shift)
            normalized = rows[positions,known]/np.maximum(rows.sum(axis=1)[positions],1e-20)
            score = .5*(np.mean(normalized[:TRAINING])+np.mean(normalized[TRAINING:]))
            ranked.append((float(score),at,offset+shift))
    return sorted(ranked,reverse=True)[:3]


@dataclass
class AnnouncedCopy:
    mode: str
    identity: str
    start: int
    end: int
    diagnostics: dict


class AnnouncedCopies(list):
    def __init__(self, copies=(), animation_links=()):
        super().__init__(copies)
        self.animation_links = list(animation_links)


def find_copies(audio, sample_rate, *, capture_start_utc=None, grid_window=.1):
    """Acquire the common header once, without selecting a payload decoder.

    Header candidates are bounded and require RS + CRC32 + supported version
    and mode. Keep a complete payload or an in-progress prefix-delimited one.
    """
    x = np.asarray(audio, float)
    if sample_rate <= 0 or sample_rate != int(sample_rate) or x.ndim != 1 or not np.all(np.isfinite(x)):
        raise ValueError('invalid copy capture')
    divisor = math.gcd(int(sample_rate), RATE)
    source = resample_poly(x, RATE//divisor, int(sample_rate)//divisor)
    step = RATE // BAUD
    if len(source) < (TRAINING + CODE_SYMBOLS*2) * step or not np.any(source):
        return []
    analytic = hilbert(source)
    training, _ = constants()
    known_positions,known_tones = header_known_symbols()
    candidates = []
    grid_candidates = []
    if capture_start_utc is not None:
        from grid_fsk_modem import near_grid
        near_grid(capture_start_utc, grid_window)  # Validate the hint.
    # Header timing is inspected at four phases per 100-baud symbol. Compute
    # correlations once for every possible carrier bin, rather than running
    # duplicate FFTs for each carrier-offset/tone combination. The CFO grid
    # and modem tones are all 20 Hz multiples.
    phase_step=step//4
    first_frequency=int(TONES.min()-120)
    frequency_step=20
    frequencies=np.arange(first_frequency,int(TONES.max()+120)+1,frequency_step)
    fft_length=next_fast_len(len(analytic)+step-1)
    signal_spectrum=fft(analytic,n=fft_length,workers=4)
    correlation_length=len(analytic)-step+1
    tone_batch=max(1,min(len(frequencies),8_000_000//fft_length))
    tone_energy=np.empty((len(frequencies),len(range(0,correlation_length,phase_step))),dtype=float)
    t=np.arange(step)/RATE
    for first in range(0,len(frequencies),tone_batch):
        tones=frequencies[first:first+tone_batch]
        templates=np.asarray([np.exp(-2j*np.pi*tone*t)[::-1] for tone in tones])
        template_spectrum=fft(templates,n=fft_length,axis=1,workers=4)
        template_spectrum*=signal_spectrum[None,:]
        correlations=ifft(template_spectrum,axis=1,workers=4)
        tone_energy[first:first+len(tones)]=abs(
            correlations[:,step-1:len(analytic):phase_step])**2
    threshold = 1/8 + 2.75*math.sqrt(7/(64*9*TRAINING))
    for offset in range(-120, 121, 20):
        tone_indices=((TONES+offset-first_frequency)//frequency_step).astype(int)
        bank=tone_energy[tone_indices,:]
        for phase in range(0, step, phase_step):
            energies = bank[:, phase//phase_step::4]
            count = energies.shape[1] - TRAINING + 1
            if count <= 0:
                continue
            norm = energies / np.maximum(energies.sum(axis=0), 1e-20)
            score = sum(norm[tone, i:i+count] for i, tone in enumerate(training)) / TRAINING
            positions, _ = find_peaks(np.pad(score, (1,1)), height=threshold, distance=SYMBOLS//2)
            for at in positions - 1:
                candidates.append((float(score[at]), int(at*step+phase), offset))
            if capture_start_utc is not None:
                # A smaller search region permits additional weak hypotheses.
                # These are admitted only for the grid modem after RS/CRC.
                weak_threshold = 1/8 + 2*math.sqrt(7/(64*9*TRAINING))
                positions, _ = find_peaks(np.pad(score, (1,1)), height=weak_threshold,
                                         distance=SYMBOLS//2)
                for at in positions-1:
                    start = int(at*step+phase)
                    if near_grid(capture_start_utc + start/RATE - GUARD, grid_window):
                        grid_candidates.append((float(score[at]), start, offset))
            # Also detect using existing magic tones dispersed through the
            # header. This can survive a fade concentrated on the preamble.
            complete = energies.shape[1]-SYMBOLS+1
            if complete > 0:
                magic = sum(norm[tone,i:i+complete] for i,tone in
                            zip(known_positions[TRAINING:],known_tones[TRAINING:]))/10
                joint = .5*(score[:complete]+magic)
                joint_threshold = 1/8+2.75*.5*math.sqrt(7/576*(1/TRAINING+1/10))
                positions,_ = find_peaks(np.pad(joint,(1,1)),height=joint_threshold,distance=SYMBOLS//2)
                for at in positions-1:
                    candidates.append((float(joint[at]),int(at*step+phase),offset))
    # Retain different CFO/timing hypotheses until the header validates.
    markers = []
    checked_positions = []
    ranked = [(rank,item,False) for rank,item in enumerate(sorted(candidates, reverse=True)[:MAX_COPIES*12])]
    if capture_start_utc is not None:
        from grid_fsk_modem import near_grid
        # A soft prior only: every original candidate remains eligible.
        ranked.sort(key=lambda item: not near_grid(
            capture_start_utc + item[1][1]/RATE - GUARD, grid_window))
    ranked += [(rank, item, True) for rank, item in enumerate(
        sorted(grid_candidates, reverse=True)[:24])]
    for rank, (score, start, offset), grid_only in ranked:
        if any(abs(start-at) < SYMBOLS*step//2 for at in checked_positions):
            continue
        last = start + SYMBOLS*step
        if last > len(source):
            continue
        hypotheses = [(score,start,offset)]
        if rank < 24:
            hypotheses += _refine_header(analytic,start,offset)
        header = None
        for refined_score,at,shift in hypotheses:
            full_metrics = _header_metrics(analytic,at,shift)
            normalized = full_metrics / np.maximum(full_metrics.sum(axis=1,keepdims=True),1e-20)
            # The preamble peaks alone produce many noise candidates. Check
            # the dispersed, protocol-known magic symbols before invoking the
            # Python RS decoder and its bounded soft-recovery attempts.
            known_score=float(np.mean(normalized[known_positions,known_tones]))
            if known_score < .20:
                continue
            metrics = full_metrics[TRAINING:]
            try:
                header = decode_header(metrics, soft=rank < 24)
                if grid_only and header['mode'] != 'experimental_16fsk_grid':
                    continue
                score,start,offset = refined_score,at,shift
                break
            except ValueError:
                continue
        if header is None:
            continue
        checked_positions.append(start)
        markers.append((start, header, score, offset))
    links = [header['animation_link'] for _, header, _, _ in markers if 'animation_link' in header]
    copies = []
    for start, header, score, offset in markers:
        if 'animation_link' in header:
            continue
        header = {**header, 'animation_links': links}
        header = {**header, 'estimated_snr_db_2500': header_snr_db(analytic, start, offset)}
        payload_length = header['payload_samples'] * RATE / 48000
        boundary = (start - GUARD*RATE if header['trailer'] else
                    start + SYMBOLS*step + GUARD*RATE)
        first = boundary-payload_length if header['trailer'] else boundary
        last = boundary if header['trailer'] else boundary+payload_length
        if header['version'] in (GROUP_VERSION,COMPRESSED_VERSION):
            # Native payload training and guards independently synchronize each
            # copy. Only the opening marker carries mode and content identity.
            for index in range(header['group_copies']):
                begin = first + index*payload_length
                finish = min(len(source), begin+payload_length)
                if begin >= len(source):
                    break
                copies.append(AnnouncedCopy(header['mode'], header['identity'],
                    max(0, round(begin*sample_rate/RATE)),
                    min(len(x), round(finish*sample_rate/RATE)),
                    {**header, 'copy_index': index, 'acquisition_score': score,
                     'carrier_offset_hz': offset}))
            continue
        if first < -step or first >= len(source):
            continue
        first, last = max(0, first), min(len(source), last)
        original_first = round(first*sample_rate/RATE)
        original_last = min(len(x), round(last*sample_rate/RATE))
        if any(c.identity == header['identity'] and abs(c.start-original_first) < sample_rate/BAUD for c in copies):
            continue
        copies.append(AnnouncedCopy(header['mode'], header['identity'], original_first,
                    original_last, {**header, 'acquisition_score': score,
                    'carrier_offset_hz': offset}))
    return AnnouncedCopies(sorted(copies, key=lambda c: c.start)[:MAX_COPIES], links)


def groups(copies, key):
    grouped = {}
    for copy in copies:
        if copy.mode == key:
            grouped.setdefault(copy.identity, []).append(copy)
    return list(grouped.values())


def decode_capture(key, audio, sample_rate, decoder, copies=None, copy_decoder=None, announced_decoder=None):
    announced = find_copies(audio, sample_rate) if copies is None else copies
    from card_animation import LinkCache
    animation_links = LinkCache()
    for digest, link in getattr(announced, 'animation_links', ()):
        animation_links.add(digest, link)
    for copy in announced:
        for digest, link in copy.diagnostics.get('animation_links', ()):
            animation_links.add(digest, link)
    matched = groups(announced, key)
    if not announced:
        if key in GROUP_MODES or key in COMPRESSED_MODES:
            raise ValueError('waiting for protected grouped header')
        report = decoder(audio, sample_rate)  # historical native waveform
        animation_links.attach(report)
        return report
    if not matched:
        raise ValueError('common header announces a different modem')
    best, best_start, error = None, None, ValueError('announced payload is incomplete')
    for group in matched:
        # When a receive window contains more than one card, prefer the newest
        # protected acquisition. Earlier incomplete (or even complete) cards
        # must not mask a later card that is still being received.
        group_start = max(int(copy.diagnostics.get(
            'acquisition_start_sample', round(copy.start * RATE / sample_rate)))
            for copy in group)
        payloads = []
        complete_payload = False
        for copy in group:
            signal = np.asarray(audio)[copy.start:copy.end]
            expected_samples = math.ceil(
                int(copy.diagnostics.get('payload_samples', 0)) * sample_rate / 48000)
            if expected_samples > 0 and len(signal) >= expected_samples:
                complete_payload = True
            shift = copy.diagnostics['carrier_offset_hz']
            if shift and len(signal):
                signal = (hilbert(signal) * np.exp(-2j*np.pi*shift*np.arange(len(signal))/sample_rate)).real
            payloads.append(np.pad(signal, (round(.05*sample_rate),)*2))
        pooled = np.concatenate(payloads)
        token = ADMITTED_IDENTITY.set((key, group[0].identity))
        try:
            report = (announced_decoder(payloads,sample_rate,group) if announced_decoder is not None else
                      copy_decoder(payloads, sample_rate) if copy_decoder is not None else
                      decoder(pooled, sample_rate))
            card = report.get('card', {})
            if card.get('exact') and report_content_id(key, report).hex() != group[0].identity:
                raise ValueError('decoded card does not match the protected copy identity')
            if key == 'experimental_8fsk_musical_25':
                coverage = card.get('pixel_coverage') or []
                pixels = report.get('pixels') or []
                expected_pixels = int(card.get('width', 32)) * int(card.get('height', 32))
                if complete_payload and len(pixels) == expected_pixels:
                    matches_identity = report_content_id(key, report).hex() == group[0].identity
                    if matches_identity:
                        received_coverage = list(coverage) if coverage else [True] * expected_pixels
                        recovered_count = sum(bool(value) for value in received_coverage)
                        card['received_pixel_coverage'] = received_coverage
                        card['pixel_coverage'] = [True] * expected_pixels
                        card['verified_pixel_coverage'] = [True] * expected_pixels
                        card['content_checksum_valid'] = True
                        card['exact'] = True
                        card['verification_scope'] = 'whole_card_content_digest'
                        card['color_stage'] = (
                            'whole-card checksum verified' if recovered_count == expected_pixels else
                            f'whole-card checksum verified · {recovered_count}/{expected_pixels} pixels decoded directly')
                    elif coverage and all(coverage):
                        card['content_checksum_valid'] = False
                        card['exact'] = False
                        card['color_stage'] = f'{expected_pixels}/{expected_pixels} pixels · whole-card checksum failed · unverified'
                    else:
                        card['content_checksum_valid'] = None
                        card['exact'] = False
                        card['color_stage'] = (
                            f'{sum(bool(value) for value in coverage)}/{expected_pixels} pixels · '
                            'checksum inconclusive with missing pixels · unverified')
                else:
                    card['content_checksum_valid'] = None
        except ValueError as exc:
            error = exc
            continue
        finally:
            ADMITTED_IDENTITY.reset(token)
        card = report.get('card') or {}
        animation_links.attach(report)
        estimates = [copy.diagnostics.get('estimated_snr_db_2500') for copy in group]
        estimates = [float(value) for value in estimates if value is not None and np.isfinite(value)]
        if estimates:
            set_measured_snr(card, 10 * np.log10(np.mean(10 ** (np.asarray(estimates) / 10))),
                             'common_header_training')
        diagnostic = dict(card.get('diagnostics') or {})
        diagnostic.update(copy_header_version=group[0].diagnostics.get('version', VERSION), announced_mode=key,
                          content_id=group[0].identity, announced_copies=len(group),
                          copy_revisions=tuple(sorted((
                              int(copy.diagnostics.get('receive_session_id', 0)),
                              int(copy.diagnostics.get('acquisition_start_sample',
                                  round(copy.start * RATE / sample_rate))),
                              int(copy.diagnostics.get('copy_revision', copy.end-copy.start)))
                              for copy in group)))
        diagnostic['receive_session_id'] = int(group[0].diagnostics.get('receive_session_id', 0))
        card['diagnostics'] = diagnostic
        report['card'] = card
        report['copy_header'] = diagnostic
        diagnostic['acquisition_start_sample'] = group_start
        report['copy_header'] = diagnostic
        if best is None or best_start is None or group_start >= best_start:
            best = report
            best_start = group_start
    if best is not None:
        return best
    raise error

class CopyCaptureCache:
    """Bounded live copy storage beyond the rolling audio window.

    Store payloads at 8 kHz float32, not raw 48 kHz capture audio. Replace an
    in-progress copy with its longer snapshot; never count snapshots as copies.
    """
    def __init__(self, max_bytes=256*1024*1024, max_groups=4):
        from card_animation import LinkCache
        self.animation_links = LinkCache()
        from threading import RLock
        self.max_bytes=max_bytes; self.max_groups=max_groups
        self.lock=RLock(); self.entries={}; self.serial=0
        self.header_scan_sample=None

    def clear(self):
        with self.lock:
            self.animation_links.links.clear()
            self.entries.clear()
            self.header_scan_sample=None

    def header_scan_ranges(self, capture_start_sample, sample_count, sample_rate,
                           *, max_chunk_seconds=8.0):
        """Return bounded overlapping ranges since the previous live scan."""
        buffer_start=int(capture_start_sample)
        buffer_end=buffer_start+int(sample_count)
        rate=int(sample_rate)
        overlap=round((HEADER_SECONDS+.2)*rate)
        chunk=max(overlap+1,round(float(max_chunk_seconds)*rate))
        min_header=round((TRAINING+CODE_SYMBOLS*2)*(RATE/BAUD)*rate/RATE)
        with self.lock:
            cursor=self.header_scan_sample
            if cursor is None:
                # If RX is enabled after audio has accumulated, begin with a
                # recent tail instead of rescanning the entire rolling buffer.
                cursor=max(buffer_start,buffer_end-round(16*rate))
            cursor=max(buffer_start,min(cursor,buffer_end))
            begin=max(buffer_start,cursor-overlap)
            if buffer_end-begin<min_header:
                return []
            ranges=[]; position=begin
            while position<buffer_end:
                end=min(buffer_end,position+chunk)
                ranges.append((position-buffer_start,end-buffer_start))
                if end>=buffer_end:
                    break
                position=end-overlap
            self.header_scan_sample=buffer_end
            return ranges

    @staticmethod
    def _append_payload(frame, audio, sample_rate, capture_start_sample):
        payload_samples=frame['diagnostics'].get('payload_samples')
        if payload_samples is None:
            return
        samples=np.asarray(audio)
        start_absolute=round(frame['start']*sample_rate)
        start_index=start_absolute-int(capture_start_sample)
        if start_index<0 or start_index>=len(samples):
            return
        expected_input=round(int(payload_samples)*sample_rate/48000)
        available=min(expected_input,len(samples)-start_index)
        divisor=math.gcd(int(sample_rate),RATE)
        up,down=RATE//divisor,int(sample_rate)//divisor
        seen=int(frame.get('source_samples_seen',0))
        end_input=available if available>=expected_input else (available//down)*down
        if end_input<=seen:
            return
        overlap=max(down,round(.02*sample_rate))
        overlap=(overlap//down)*down
        begin_input=max(0,((seen-overlap)//down)*down)
        source=samples[start_index+begin_input:start_index+end_input]
        converted=resample_poly(source,up,down).astype(np.float32)
        begin_output=begin_input*up//down
        old_output=round(seen*up/down)
        shift=frame['diagnostics'].get('carrier_offset_hz',0)
        if shift and len(converted):
            indices=begin_output+np.arange(len(converted))
            converted=(hilbert(converted)*np.exp(-2j*np.pi*shift*indices/RATE)).real.astype(np.float32)
        discard=max(0,old_output-begin_output)
        addition=converted[discard:]
        expected_output=round(end_input*up/down)
        target=max(0,expected_output-old_output)
        if len(addition)>target:
            addition=addition[:target]
        if len(addition):
            frame['audio']=np.concatenate((frame['audio'],addition))
            frame['source_samples_seen']=end_input

    def update(self, audio, sample_rate, copies, capture_start_sample=0):
        with self.lock:
            for digest, link in getattr(copies, 'animation_links', ()):
                self.animation_links.add(digest, link)
            for copy in copies:
                for digest, link in copy.diagnostics.get('animation_links', ()):
                    self.animation_links.add(digest, link)
                key=(copy.mode,copy.identity)
                self.serial+=1
                group=self.entries.setdefault(key,dict(frames=[],touched=self.serial))
                group['touched']=self.serial
                absolute=(capture_start_sample+copy.start)/sample_rate
                # The opening and closing protected markers describe the same
                # payload, but weak marker timing can place their inferred
                # starts a few hundred milliseconds apart. Keep one best
                # capture per physical payload so duplicate markers do not get
                # counted as independent soft evidence.
                old=next((f for f in group['frames'] if abs(f['start']-absolute)<.5),None)
                if old is not None:
                    previous_score=float(old['diagnostics'].get('acquisition_score',0))
                    new_score=float(copy.diagnostics.get('acquisition_score',0))
                    if new_score>previous_score:
                        old['start']=absolute
                        old['diagnostics']={**copy.diagnostics,
                                            'acquisition_start_sample': round(absolute*RATE)}
                        old['audio']=np.empty(0,dtype=np.float32)
                        old['source_samples_seen']=0
                if old is None:
                    frame=dict(start=absolute,audio=np.empty(0,dtype=np.float32),
                               diagnostics={**copy.diagnostics,
                                            'acquisition_start_sample': round(absolute*RATE)},
                               source_samples_seen=0)
                    group['frames'].append(frame)
                else:
                    frame=old
                    diagnostic=dict(copy.diagnostics)
                    diagnostic['acquisition_start_sample']=round(absolute*RATE)
                    if len(frame['audio']):
                        diagnostic['carrier_offset_hz']=frame['diagnostics'].get(
                            'carrier_offset_hz',diagnostic.get('carrier_offset_hz',0))
                    frame['diagnostics']=diagnostic
                if 'payload_samples' not in copy.diagnostics:
                    signal=np.asarray(audio)[copy.start:copy.end]
                    if old is None or len(signal)/sample_rate>len(frame['audio'])/RATE:
                        divisor=math.gcd(int(sample_rate),RATE)
                        payload=resample_poly(signal,RATE//divisor,int(sample_rate)//divisor).astype(np.float32)
                        shift=copy.diagnostics.get('carrier_offset_hz',0)
                        if shift and len(payload):
                            payload=(hilbert(payload)*np.exp(-2j*np.pi*shift*np.arange(len(payload))/RATE)).real.astype(np.float32)
                        frame['audio']=payload
                group['frames']=sorted(group['frames'],key=lambda f:f['start'])[-MAX_COPIES:]
            for group in self.entries.values():
                for frame in group['frames']:
                    self._append_payload(frame,audio,sample_rate,capture_start_sample)
            while len(self.entries)>self.max_groups or sum(f['audio'].nbytes for g in self.entries.values() for f in g['frames'])>self.max_bytes:
                key=min(self.entries,key=lambda k:self.entries[k]['touched'])
                del self.entries[key]

    def materialize(self):
        with self.lock:
            parts=[]; copies=[]; start=0
            for (mode,identity), group in self.entries.items():
                for frame in group['frames']:
                    payload=frame['audio']; parts.append(payload)
                    diagnostic={**frame['diagnostics'],'carrier_offset_hz':0,
                                'animation_links': [(digest, link) for digest, link in
                                                    self.animation_links.links.items() if link],
                                'acquisition_start_sample': round(frame['start']*RATE),
                                'copy_revision': len(payload),
                                'acquisition_id': (int(frame['diagnostics'].get('receive_session_id', 0)),
                                                   round(frame['start']*RATE), mode, identity)}
                    copies.append(AnnouncedCopy(mode,identity,start,start+len(payload),diagnostic))
                    start+=len(payload)
            return (np.concatenate(parts) if parts else np.empty(0,np.float32)), copies

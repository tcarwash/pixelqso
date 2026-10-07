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
from scipy.signal import fftconvolve, hilbert, resample_poly, find_peaks
import cardmodem as pixel
from experimental_fec import rs_encode, rs_decode
from copy_combining import MAX_COPIES, ADMITTED_IDENTITY

RATE = 8000
BAUD = 100
TONES = np.arange(800, 1600, 100)
TRAINING = 32
BODY = struct.Struct('>4sBBI16s')
VERSION = 1
GROUP_VERSION = 2
COMPRESSED_VERSION = 3
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


def header_symbols(key, payload_samples, identity, *, trailer=False, copies=None):
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
    body = BODY.pack(b'PQCH', version, mode, payload_samples, identity)
    wire = body + struct.pack('>I', zlib.crc32(body))
    coded = rs_encode(pixel._bytes_to_gf64(wire), PARITY)
    tones = np.asarray(pixel._gf64_to_tones(coded))
    training, permutation = constants()
    return np.concatenate((training, tones[permutation]))


def header_audio(key, payload_samples, identity, *, trailer=False, copies=None):
    audio = pixel.synthesize(header_symbols(key, payload_samples, identity, trailer=trailer, copies=copies),
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
    return np.tile(np.concatenate((prefix, payload, suffix)), int(copies))


def transmit_group(key, card, payload, copies, message_type='card', snr_db=None):
    if int(copies) != copies or not 1 <= copies <= MAX_COPIES:
        raise ValueError(f'copy count must be between 1 and {MAX_COPIES}')
    if not valid_group_length(key,len(payload)):
        raise ValueError('grouped payload does not match a supported format')
    prefix = header_audio(key, len(payload), content_id(key, card, message_type, snr_db), copies=copies)
    rms = np.sqrt(np.mean(np.asarray(payload, float)**2))
    prefix *= rms / max(np.sqrt(np.mean(prefix.astype(float)**2)), 1e-12)
    return np.concatenate((prefix, np.tile(payload, int(copies))))


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
    copies = 1
    if version in (GROUP_VERSION,COMPRESSED_VERSION):
        copies, samples = (samples >> 27) + 1, samples & ((1 << 27)-1)
        if copies > MAX_COPIES or not valid_group_length(ID_MODES.get(mode),samples) or version != group_version(ID_MODES.get(mode)):
            raise ValueError('unsupported grouped header')
        if mode & 128:
            raise ValueError('group header cannot carry a trailer')
    if version == VERSION and (ID_MODES.get(mode & 127) in GROUP_MODES or ID_MODES.get(mode & 127) in COMPRESSED_MODES):
        raise ValueError('grouped modes require a supported group header')
    if magic != b'PQCH' or version not in (VERSION, GROUP_VERSION, COMPRESSED_VERSION) or mode & 127 not in ID_MODES or not 0 < samples <= 48000*1800:
        raise ValueError('unsupported copy header')
    return dict(mode=ID_MODES[mode & 127], trailer=bool(mode & 128),
                payload_samples=samples, identity=identity.hex(), version=version,
                corrected_symbols=corrected, group_copies=copies)


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
    t = np.arange(step) / RATE
    threshold = 1/8 + 2.75*math.sqrt(7/(64*9*TRAINING))
    for offset in range(-120, 121, 20):
        bank = np.asarray([abs(fftconvolve(analytic,
            np.exp(-2j*np.pi*(tone+offset)*t)[::-1], 'valid'))**2 for tone in TONES])
        for phase in range(0, step, step//4):
            energies = bank[:, phase::step]
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
            metrics = _header_metrics(analytic,at,shift)[TRAINING:]
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
    copies = []
    for start, header, score, offset in markers:
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
    return sorted(copies, key=lambda c: c.start)[:MAX_COPIES]


def groups(copies, key):
    grouped = {}
    for copy in copies:
        if copy.mode == key:
            grouped.setdefault(copy.identity, []).append(copy)
    return list(grouped.values())


def decode_capture(key, audio, sample_rate, decoder, copies=None, copy_decoder=None, announced_decoder=None):
    announced = find_copies(audio, sample_rate) if copies is None else copies
    matched = groups(announced, key)
    if not announced:
        if key in GROUP_MODES or key in COMPRESSED_MODES:
            raise ValueError('waiting for protected grouped header')
        return decoder(audio, sample_rate)  # historical native waveform
    if not matched:
        raise ValueError('common header announces a different modem')
    best, error = None, ValueError('announced payload is incomplete')
    for group in matched:
        payloads = []
        for copy in group:
            signal = np.asarray(audio)[copy.start:copy.end]
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
            if report.get('card',{}).get('exact') and report_content_id(key, report).hex() != group[0].identity:
                raise ValueError('decoded card does not match the protected copy identity')
        except ValueError as exc:
            error = exc
            continue
        finally:
            ADMITTED_IDENTITY.reset(token)
        card = report.get('card') or {}
        diagnostic = dict(card.get('diagnostics') or {})
        diagnostic.update(copy_header_version=group[0].diagnostics.get('version', VERSION), announced_mode=key,
                          content_id=group[0].identity, announced_copies=len(group))
        card['diagnostics'] = diagnostic
        report['card'] = card
        report['copy_header'] = diagnostic
        if card.get('exact'):
            return report
        if best is None or sum(card.get('pixel_coverage') or []) > sum(best.get('card',{}).get('pixel_coverage') or []):
            best = report
    if best is not None:
        return best
    raise error

class CopyCaptureCache:
    """Bounded live copy storage beyond the rolling audio window.

    Store payloads at 8 kHz float32, not raw 48 kHz capture audio. Replace an
    in-progress copy with its longer snapshot; never count snapshots as copies.
    """
    def __init__(self, max_bytes=256*1024*1024, max_groups=4):
        from threading import RLock
        self.max_bytes=max_bytes; self.max_groups=max_groups
        self.lock=RLock(); self.entries={}; self.serial=0

    def clear(self):
        with self.lock:
            self.entries.clear()

    def update(self, audio, sample_rate, copies, capture_start_sample=0):
        with self.lock:
            for copy in copies:
                key=(copy.mode,copy.identity)
                self.serial+=1
                group=self.entries.setdefault(key,dict(frames=[],touched=self.serial))
                group['touched']=self.serial
                absolute=(capture_start_sample+copy.start)/sample_rate
                old=next((f for f in group['frames'] if abs(f['start']-absolute)<1/BAUD),None)
                signal=np.asarray(audio)[copy.start:copy.end]
                if old is not None and len(signal)/sample_rate<=len(old['audio'])/RATE:
                    continue
                divisor=math.gcd(int(sample_rate),RATE)
                payload=resample_poly(signal,RATE//divisor,int(sample_rate)//divisor).astype(np.float32)
                shift=copy.diagnostics['carrier_offset_hz']
                if shift and len(payload):
                    payload=(hilbert(payload)*np.exp(-2j*np.pi*shift*np.arange(len(payload))/RATE)).real.astype(np.float32)
                frame=dict(start=absolute,audio=payload,diagnostics=copy.diagnostics)
                if old is None:
                    group['frames'].append(frame)
                else:
                    group['frames'][group['frames'].index(old)]=frame
                group['frames']=sorted(group['frames'],key=lambda f:f['start'])[-MAX_COPIES:]
            while len(self.entries)>self.max_groups or sum(f['audio'].nbytes for g in self.entries.values() for f in g['frames'])>self.max_bytes:
                key=min(self.entries,key=lambda k:self.entries[k]['touched'])
                del self.entries[key]

    def materialize(self):
        with self.lock:
            parts=[]; copies=[]; start=0
            for (mode,identity), group in self.entries.items():
                for frame in group['frames']:
                    payload=frame['audio']; parts.append(payload)
                    diagnostic={**frame['diagnostics'],'carrier_offset_hz':0}
                    copies.append(AnnouncedCopy(mode,identity,start,start+len(payload),diagnostic))
                    start+=len(payload)
            return (np.concatenate(parts) if parts else np.empty(0,np.float32)), copies

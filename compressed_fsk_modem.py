"""Lossless variable-length grouped FSK: bounded image codecs and shortened LDPC.

Wire v3 uses a protected group length to select one of six coding profiles.
No profiles, compression lengths or identities are inferred from damaged data.
"""
from dataclasses import replace
from functools import lru_cache
import hashlib
import struct
import zlib
import numpy as np
from scipy.signal import hilbert, resample_poly
from scipy.special import logsumexp
import cardmodem as pixel
import experimental_burst_modem as burst
import weak_signal_modem as weak
from weak_signal_ldpc import FixedCode
from copy_combining import SoftCopy, recovery_attempts, hypothesis_attempts, diagnostics_for

MODES = {'experimental_16fsk_grid': 'experimental_16fsk_ldpc',
         'experimental_8fsk_compressed': 'experimental_8fsk_ldpc',
         'experimental_16fsk_compressed': 'experimental_16fsk_ldpc'}
CAPACITIES = (128, 192, 256, 320, 384, 422)
CODE_BITS = (2496, 3744, 4992, 6240, 7456, 8192)
LENGTH = struct.Struct('>H')
CODECS = {0: 'raw', 1: 'deflate', 2: 'pixel_runs', 3: 'row_xor_deflate'}
IMAGE_BYTES = 384


def _deflate(data):
    encoder = zlib.compressobj(9, wbits=-15)
    return encoder.compress(data) + encoder.flush()


def _inflate(data):
    decoder = zlib.decompressobj(wbits=-15)
    try:
        result = decoder.decompress(data, IMAGE_BYTES+1)
    except zlib.error as exc:
        raise ValueError('invalid compressed image') from exc
    if (len(result) != IMAGE_BYTES or not decoder.eof or
            decoder.unused_data or decoder.unconsumed_tail):
        raise ValueError('compressed image has invalid length or trailing data')
    return result


def _row_xor(data, inverse=False):
    rows = np.frombuffer(data, np.uint8).reshape(32,12).copy()
    if inverse:
        for y in range(1,32):
            rows[y] ^= rows[y-1]
    else:
        rows[1:] ^= rows[:-1].copy()
    return rows.tobytes()


def _runs(data):
    pixels = pixel.unpack_indices(data, 1024)
    encoded = bytearray()
    at = 0
    while at < len(pixels):
        end = at+1
        while end < min(len(pixels),at+32) and pixels[end] == pixels[at]:
            end += 1
        encoded.append(((end-at-1)<<3) | pixels[at])
        at = end
    return bytes(encoded)


def _unruns(data):
    pixels = []
    for value in data:
        count = (value>>3)+1
        if len(pixels)+count > 1024:
            raise ValueError('pixel run exceeds image bounds')
        pixels.extend([value&7]*count)
    if len(pixels) != 1024:
        raise ValueError('pixel runs do not fill the image')
    return pixel._pack_indices(pixels,3)


def pack(card, message_type='card', snr_db=None):
    """Select a deterministic lossless representation; raw is never expanded."""
    native = weak.packet(card,message_type,snr_db)
    image = native[weak.HEADER.size:-4]
    candidates = [(native,0)]
    for codec,data in ((1,_deflate(image)), (2,_runs(image)),
                       (3,_deflate(_row_xor(image)))):
        packet = (b'PQC1' + bytes((codec,)) + native[5:weak.HEADER.size] +
                  LENGTH.pack(len(data)) + data + native[-4:])
        candidates.append((packet,codec))
    packet,codec = min(candidates,key=lambda candidate:(len(candidate[0]),candidate[1]))
    capacity = next(size for size in CAPACITIES if len(packet) <= size)
    return packet,capacity,codec


def unpack(data):
    """Expand a bounded coding block into the original CRC-protected packet."""
    if len(data) not in CAPACITIES:
        raise ValueError('unsupported compressed coding block')
    if data.startswith(b'PQW1'):
        if len(data) != 422:
            raise ValueError('raw card requires the full coding profile')
        native,codec,used = data,0,422
    elif data.startswith(b'PQC1'):
        codec = data[4]
        length = LENGTH.unpack_from(data,weak.HEADER.size)[0]
        end = weak.HEADER.size + LENGTH.size + length
        if codec not in (1,2,3) or length > IMAGE_BYTES or end+4 > len(data) or any(data[end+4:]):
            raise ValueError('invalid compressed packet framing')
        body = data[weak.HEADER.size+LENGTH.size:end]
        image = _unruns(body) if codec == 2 else _inflate(body)
        if codec == 3:
            image = _row_xor(image,inverse=True)
        native = b'PQW1\x01' + data[5:weak.HEADER.size] + image + data[end:end+4]
        used = end+4
    else:
        raise ValueError('unsupported compressed card version')
    # Validate both the expanded image and its original metadata before release.
    weak.unpack(np.unpackbits(np.frombuffer(native,np.uint8)) ^ weak.constants()[3], {})
    return native,dict(compression=CODECS[codec],packet_bytes=used,
                       original_packet_bytes=422,coding_capacity_bytes=len(data),
                       compressed_packet_digest=hashlib.blake2s(data[:used],digest_size=16).hexdigest())


@lru_cache(None)
def code(capacity):
    if capacity not in CAPACITIES:
        raise ValueError('unsupported LDPC capacity')
    result = FixedCode()
    result.k = capacity*8
    result.n = CODE_BITS[CAPACITIES.index(capacity)]
    # The mother graph is unchanged; shortening makes trailing information
    # columns known zero. Its parity-core inverse is shared read-only.
    result._core_inverse = weak.code()._core_inverse
    return result


@lru_cache(None)
def whitening(capacity):
    return np.random.default_rng(0x50434300+capacity).integers(0,2,capacity*8,dtype=np.uint8)


@lru_cache(None)
def format_for(key,capacity):
    if key not in MODES or capacity not in CAPACITIES:
        raise ValueError('unsupported compressed FSK profile')
    base = burst.FORMATS[MODES[key]]
    return replace(base,key=key,seed=0x50434300+base.tones*1024+capacity,
                   code_bits=CODE_BITS[CAPACITIES.index(capacity)])


def payload_samples(key,capacity):
    fmt = format_for(key,capacity)
    return (fmt.symbol_count*(burst.FS//fmt.baud) + 2*round(burst.GUARD_SECONDS*burst.FS))*4


def capacity_for_samples(key,samples):
    for capacity in CAPACITIES:
        if payload_samples(key,capacity) == samples:
            return capacity
    raise ValueError('unsupported compressed payload length')


def encode_payload(key,card,message_type='card',snr_db=None):
    packet,capacity,_ = pack(card,message_type,snr_db)
    fmt = format_for(key,capacity)
    training,pilots,permutation,labels,inverse = burst.format_constants_for(fmt)
    info = np.unpackbits(np.frombuffer(packet.ljust(capacity,b'\0'),np.uint8)) ^ whitening(capacity)
    coded = code(capacity).encode(info[None,:])[0][permutation]
    bps = fmt.bits_per_symbol
    coded = np.pad(coded,(0,(-len(coded))%bps)).reshape(-1,bps)
    tones = inverse[coded.dot(1<<np.arange(bps-1,-1,-1))]
    stream,at = [training],0
    for pilot,length in zip(pilots,fmt.block_lengths):
        stream.extend((pilot,tones[at:at+length]));at += length
    waveform = .75*burst._complex_wave(np.concatenate(stream),fmt).real
    waveform = np.pad(waveform,(round(burst.GUARD_SECONDS*burst.FS),)*2)
    return resample_poly(waveform,4,1).astype(np.float32)


def decode_copies(key,captures,sample_rate,announcements):
    """Decode profiles admitted by the common header, with no blind guessing."""
    profiles = {}
    for audio,announced in zip(captures,announcements):
        capacity = capacity_for_samples(key,announced.diagnostics['payload_samples'])
        profiles.setdefault(capacity,[]).append(audio)
    error = ValueError('no complete CRC-valid compressed card')
    for capacity,audios in profiles.items():
        fmt = format_for(key,capacity)
        _,_,permutation,labels,_ = burst.format_constants_for(fmt)
        frames = []
        for index,audio in enumerate(audios):
            source = burst._resample(audio,sample_rate,burst.FS)
            # Header delimitation can admit growing snapshots. Do not treat a
            # partial codeword as a completed compressed block.
            if len(source) < (.1+fmt.seconds-.02)*burst.FS:
                continue
            analytic = hilbert(source)
            radius = max(2,(.002+index*fmt.seconds*.0002)*fmt.baud)
            try:
                variants = []
                for evidence,timing in burst.announced_fsk_candidates(analytic,fmt,radius=radius):
                    if len(evidence) < fmt.symbol_count:
                        continue
                    blocks,at = [],burst.TRAINING
                    for length in fmt.block_lengths:
                        at += 4;blocks.append(evidence[at:at+length]);at += length
                    metrics = np.concatenate(blocks)
                    llrs = np.column_stack([
                        logsumexp(metrics[:,(labels&(1<<bit))==0],axis=1) -
                        logsumexp(metrics[:,(labels&(1<<bit))!=0],axis=1)
                        for bit in range(fmt.bits_per_symbol-1,-1,-1)]).ravel()[:fmt.code_bits]
                    ordered = np.empty(fmt.code_bits);ordered[permutation] = llrs
                    variants.append(SoftCopy(ordered,timing))
                if variants: frames.append(variants)
            except ValueError as exc:
                error = exc
        for evidence,used in hypothesis_attempts(frames):
            try:
                bits,iterations = weak.decode_ldpc(evidence,code_instance=code(capacity))
                native,info = unpack(np.packbits(bits ^ whitening(capacity)).tobytes())
                return burst._whole_report(key,native,{**diagnostics_for(used,len(frames)),
                    **info,'ldpc_iterations':iterations,'coded_bits':fmt.code_bits,
                    'information_bits':capacity*8})
            except ValueError as exc:
                error = exc
    if not any(len(x) >= sample_rate*(payload_samples(key,c)/48000+.08)
               for c,xs in profiles.items() for x in xs):
        return burst.activity(key,{'announced_copies':len(captures)})
    raise error

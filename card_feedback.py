"""Checked reception reports carried alongside Exchange and RR73 image cards."""
from collections import OrderedDict
import copy
import hashlib
import json
import re
import struct
import zlib

MAGIC = b'PQRP'
SUBJECT = struct.Struct('>12sHBB')
COUNTS = struct.Struct('>8sHHHH')
UNKNOWN = 65535


def validate_report(report):
    if not isinstance(report, dict):
        raise ValueError('Reception report must be an object')
    call = report.get('callsign', '')
    if not isinstance(call, str):
        raise ValueError('Reception report requires an identified station')
    call = call.upper()
    if not re.fullmatch(r'[A-Z0-9/]{1,12}', call):
        raise ValueError('Reception report requires an identified station')
    result = dict(callsign=call)
    for field, maximum in (('card_id', 65535), ('pixels_received', 65025),
                           ('pixels_verified', 65025), ('pixels_total', 65025)):
        value = report.get(field)
        if field == 'pixels_verified' and value is None:
            value = report.get('pixels_total', 0) if report.get('verified') else 0
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f'Invalid reception report {field}')
        result[field] = value
    if (not result['pixels_total'] or result['pixels_received'] > result['pixels_total'] or
            result['pixels_verified'] > result['pixels_received']):
        raise ValueError('Invalid reception report pixel count')
    for field in ('crc_blocks_passed', 'crc_blocks_total'):
        value = report.get(field)
        if value is not None and (type(value) is not int or not 0 <= value < UNKNOWN):
            raise ValueError(f'Invalid reception report {field}')
        result[field] = value
    passed, total = result['crc_blocks_passed'], result['crc_blocks_total']
    if total is not None and (passed is None or passed > total):
        raise ValueError('Invalid reception report CRC block count')
    verified = report.get('verified', False)
    if type(verified) is not bool:
        raise ValueError('Invalid reception report verification flag')
    if verified and (result['pixels_verified'] != result['pixels_total'] or
                     total is not None and passed != total):
        raise ValueError('Incomplete reception report cannot claim whole-card verification')
    result['verified'] = verified
    kind = report.get('block_kind', 'crc_block')
    if kind not in ('crc_block', 'transport_fragment'):
        raise ValueError('Unknown reception block type')
    result['block_kind'] = kind
    return result


def make_report(received):
    received = received or {}
    card = (received or {}).get('card') or {}
    total = int(card.get('width', 32)) * int(card.get('height', 32))
    coverage = card.get('pixel_coverage')
    verified = bool((received.get('assembly_event') or {}).get('verified_complete') or card.get('exact'))
    count = sum(bool(value) for value in coverage[:total]) if coverage is not None else (total if verified else 0)
    verified_coverage = card.get('verified_pixel_coverage')
    verified_count = (sum(bool(value) for value in verified_coverage[:total])
                      if verified_coverage is not None else (total if verified else 0))
    blocks = card.get('received_blocks')
    if blocks is not None:
        passed, block_total = sum(bool(value) for value in blocks), len(blocks)
    else:
        passed, block_total = card.get('crc_blocks_passed'), card.get('crc_blocks_total')
    return validate_report(dict(callsign=card.get('callsign'), card_id=card.get('card_id'),
        pixels_received=count, pixels_verified=verified_count, pixels_total=total, crc_blocks_passed=passed,
        crc_blocks_total=block_total, verified=verified,
        block_kind=card.get('crc_block_kind', 'crc_block')))


def report_lines(card, *, sent=False):
    report = card.get('reception_report')
    if not report:
        return []
    try:
        r = validate_report(report)
    except (ValueError, TypeError):
        return []
    subject = f"{r['callsign']} card {r['card_id']:04X}"
    prefix = 'Sent reception report for' if sent else 'Their reception of'
    pixels = (f"{r['pixels_verified']}/{r['pixels_total']} pixels CRC-verified · "
              f"{r['pixels_received']}/{r['pixels_total']} pixels received")
    passed, total = r['crc_blocks_passed'], r['crc_blocks_total']
    if r['block_kind'] == 'transport_fragment':
        blocks = f'{passed} CRC-checked transport fragments' if passed is not None else 'CRC-checked fragments: unavailable'
    else:
        blocks = ('CRC blocks: unavailable' if passed is None else
                  f'{passed}/{total} blocks passed CRC' if total is not None else f'{passed} blocks passed CRC (total unknown)')
    status = 'whole card verified' if r['verified'] else 'RETRY NEEDED · card not verified'
    return [f'{prefix} {subject}: {pixels} · {blocks} · {status}']


def encode_report(report, binding, message_type):
    r = validate_report(report)
    if message_type not in ('exchange', '73') or len(binding) != 8:
        raise ValueError('Reception reports belong to Exchange or RR73')
    flags = int(r['verified']) | (2 if message_type == '73' else 0) | (4 if r['block_kind'] == 'transport_fragment' else 0)
    body = SUBJECT.pack(r['callsign'].encode().ljust(12, b'\0'), r['card_id'], flags, 2)
    # Version 2 reports the number of pixels covered by checked data in the
    # legacy pixel-count slot; v1 decoders still read the original candidate count.
    body += COUNTS.pack(binding, r['pixels_verified'], r['pixels_total'],
                        UNKNOWN if r['crc_blocks_passed'] is None else r['crc_blocks_passed'],
                        UNKNOWN if r['crc_blocks_total'] is None else r['crc_blocks_total'])
    return MAGIC + body + struct.pack('>I', zlib.crc32(body))


def decode_report(frame):
    if len(frame) != 40 or frame[:4] != MAGIC or zlib.crc32(frame[4:-4]) != int.from_bytes(frame[-4:], 'big'):
        raise ValueError('Invalid reception report checksum or length')
    call, card_id, flags, version = SUBJECT.unpack(frame[4:20])
    if version not in (1, 2) or flags & ~7:
        raise ValueError('Unsupported reception report')
    binding, pixels, total, passed, blocks = COUNTS.unpack(frame[20:-4])
    try:
        call = call.rstrip(b'\0').decode('ascii')
    except UnicodeError as exc:
        raise ValueError('Invalid reception report callsign') from exc
    report = validate_report(dict(callsign=call, card_id=card_id, pixels_received=pixels,
        pixels_verified=(pixels if version == 2 else (total if flags & 1 else 0)),
        pixels_total=total, crc_blocks_passed=None if passed == UNKNOWN else passed,
        crc_blocks_total=None if blocks == UNKNOWN else blocks, verified=bool(flags & 1),
        block_kind='transport_fragment' if flags & 4 else 'crc_block'))
    return binding.hex(), ('73' if flags & 2 else 'exchange'), report


def data2g_binding(card, message_type=None, snr_db=None):
    """Bind feedback to checked identity fields available before full raster RX."""
    if not isinstance(card, dict):
        import cardmodem as modem
        packed = modem.minimal_avatar_payload(card)
        meta = dict(callsign=card.callsign, card_id=card.card_id,
                    image_id=hashlib.blake2s(packed, digest_size=8).hexdigest(),
                    width=card.width, height=card.height, palette_id=modem._avatar_palette_id(card),
                    message_type=message_type, snr_db=None if snr_db is None else int(snr_db))
    else:
        meta = card
    identity = [str(meta['callsign']).upper(), int(meta['card_id']), meta['image_id'],
                meta['width'], meta['height'], meta['palette_id'], meta['message_type'], meta.get('snr_db')]
    return hashlib.blake2s(json.dumps(identity, separators=(',', ':')).encode(), digest_size=8).digest()


class FeedbackCache:
    def __init__(self, limit=128):
        self.limit = limit
        self.reports = OrderedDict()
        self.parts = OrderedDict()

    def frames(self):
        return [encode_report(report, bytes.fromhex(binding), stage).hex()
                for (binding, stage), report in self.reports.items()]

    def add(self, frame, *, fallback=False):
        binding, stage, report = decode_report(frame)
        if fallback and (binding, stage) in self.reports:
            return
        self.reports[(binding, stage)] = report
        self.reports.move_to_end((binding, stage))
        while len(self.reports) > self.limit:
            self.reports.popitem(last=False)

    def add_part(self, mode, token, index, part):
        if index not in (0, 1) or len(part) != 32:
            return
        key = (mode, token)
        chunks = self.parts.setdefault(key, {})
        chunks[index] = bytes.fromhex(part)
        self.parts.move_to_end(key)
        while len(self.parts) > self.limit:
            self.parts.popitem(last=False)
        if len(chunks) == 2:
            body = chunks[0] + chunks[1]
            try:
                self.add(MAGIC + body + struct.pack('>I', token))
            except ValueError:
                return

    def attach(self, received, binding=None):
        card = received.get('card') or {}
        stage = card.get('message_type')
        if stage not in ('exchange', '73'):
            return False
        try:
            if binding is None:
                if card.get('application_protocol_version'):
                    binding = data2g_binding(card).hex()
                else:
                    binding = (received.get('copy_header') or card.get('diagnostics') or {}).get('content_id', '')[:16]
            if isinstance(binding, bytes):
                binding = binding.hex()
            report = self.reports.get((binding, stage))
            if report is not None:
                changed = card.get('reception_report') != report
                card['reception_report'] = copy.deepcopy(report)
                return changed
        except (KeyError, ValueError, TypeError):
            pass
        return False

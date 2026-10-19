"""Small, checked broadcast requests for card retransmissions."""
import re
import struct
import zlib
from collections import OrderedDict

MAGIC = b'PQMR'
BODY = struct.Struct('>4sBB12sH12s')
UNVERIFIED = 0
ANIMATION_PARTNER = 1
REASONS = {UNVERIFIED, ANIMATION_PARTNER}


def validate_request(request):
    if not isinstance(request, dict):
        raise ValueError('Request must be an object')
    requester = str(request.get('requester', '')).upper()
    target = str(request.get('target', '')).upper()
    if not re.fullmatch(r'[A-Z0-9/]{1,12}', requester):
        raise ValueError('Requesting station call sign is invalid')
    if not re.fullmatch(r'[A-Z0-9/]{1,12}', target):
        raise ValueError('Target station call sign is invalid')
    card_id = request.get('card_id')
    reason = request.get('reason', UNVERIFIED)
    if type(card_id) is not int or not 0 <= card_id <= 65535 or reason not in REASONS:
        raise ValueError('Request card ID or reason is invalid')
    result = dict(requester=requester, target=target, card_id=card_id, reason=reason)
    if reason == ANIMATION_PARTNER:
        related = request.get('related_id')
        # The companion ID is optional on receive: the first wire slice only
        # carries the requested frame ID and reason. Locally authored requests
        # include it for confirmation and deduplication context.
        if related is not None:
            if type(related) is not int or not 0 <= related <= 65535:
                raise ValueError('Animation request related frame ID is invalid')
            result['related_id'] = related
    return result


def encode_request(request):
    """Encode one request into a two-part, CRC-protected 36-byte record."""
    value = validate_request(request)
    reason = value['reason']
    body = BODY.pack(MAGIC, 1, reason, value['target'].encode().ljust(12, b'\0'),
                     value['card_id'], value['requester'].encode().ljust(12, b'\0'))
    return body + struct.pack('>I', zlib.crc32(body))


def decode_request(frame):
    if len(frame) != BODY.size + 4 or zlib.crc32(frame[:-4]) != int.from_bytes(frame[-4:], 'big'):
        raise ValueError('Invalid request checksum or length')
    magic, version, reason, target, card_id, requester = BODY.unpack(frame[:-4])
    if magic != MAGIC or version != 1 or reason not in REASONS:
        raise ValueError('Unsupported card request')
    try:
        value = dict(target=target.rstrip(b'\0').decode('ascii'), card_id=card_id,
                     requester=requester.rstrip(b'\0').decode('ascii'), reason=reason)
    except UnicodeError as exc:
        raise ValueError('Request identity is not ASCII') from exc
    return validate_request(value)


def request_parts(request):
    """Return the two 16-byte header fragments and their CRC token."""
    frame = encode_request(request)
    token = int.from_bytes(frame[-4:], 'big')
    return token, (frame[4:20], frame[20:36])


class RequestCache:
    def __init__(self, limit=64):
        self.limit = limit
        self.parts = OrderedDict()
        self.requests = OrderedDict()
        self.received_requests = []

    def add_part(self, token, index, part):
        if index not in (0, 1) or len(part) != 16:
            return None
        key = int(token)
        chunks = self.parts.setdefault(key, {})
        chunks[index] = bytes(part)
        self.parts.move_to_end(key)
        while len(self.parts) > self.limit:
            self.parts.popitem(last=False)
        if len(chunks) != 2:
            return None
        frame = MAGIC + chunks[0] + chunks[1] + struct.pack('>I', key)
        try:
            request = decode_request(frame)
        except ValueError:
            self.parts.pop(key, None)
            return None
        identity = (request['requester'], request['target'], request['card_id'], request['reason'])
        self.requests[identity] = request
        self.received_requests.append(request)
        self.received_requests = self.received_requests[-self.limit:]
        self.requests.move_to_end(identity)
        while len(self.requests) > self.limit:
            self.requests.popitem(last=False)
        self.parts.pop(key, None)
        return request

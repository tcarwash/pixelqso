"""Optional, one-way two-frame links; ordinary card images remain independent."""
from collections import OrderedDict
import copy
import hashlib
import json
import struct
import zlib

LINK = struct.Struct('>4sBHH8s8s')
MAGIC = b'PQAN'
DEFAULT_FRAME_MS = 500


def validate_link(link, own_id=None):
    if link is None:
        return None
    if not isinstance(link, dict):
        raise ValueError('Animation reference must be an object')
    first = link.get('first_card_id')
    duration = link.get('frame_ms', DEFAULT_FRAME_MS)
    if type(first) is not int or not 0 <= first <= 65535:
        raise ValueError('Animation first card ID must be 0..65535')
    if own_id is not None and first == own_id:
        raise ValueError('An animation needs two different card IDs')
    if type(duration) is not int or not 100 <= duration <= 10000:
        raise ValueError('Animation frame duration must be 100..10000 ms')
    digest = link.get('first_digest')
    if not isinstance(digest, str) or len(digest) != 16:
        raise ValueError('Animation requires the first card artwork fingerprint')
    try:
        if len(bytes.fromhex(digest)) != 8:
            raise ValueError('Fingerprint must be eight bytes')
    except ValueError as exc:
        raise ValueError('Invalid animation artwork fingerprint') from exc
    return dict(first_card_id=first, first_digest=digest.lower(), frame_ms=duration)


def artwork_digest(value):
    """Fingerprint station, ID, dimensions and shared-palette on-air artwork.

    Quantizing RGB through the shared palette makes legacy RGB reports and
    indexed avatar/Data2G reports comparable, independent of message stage.
    """
    import cardmodem as modem
    if not isinstance(value, dict):
        card = value
        meta = vars(card)
        pixels = card.pixels
    else:
        meta = value.get('card') or value
        pixels = value.get('pixels', meta.get('pixels'))
    width, height = int(meta.get('width', 32)), int(meta.get('height', 32))
    palette = meta.get('palette') or modem.AVATAR_PALETTES[int(meta.get('palette_id', 0))]
    target = modem.AVATAR_PALETTES[{8: 0, 16: 1, 32: 2}[len(palette)]]
    if pixels is None:
        raise ValueError('Animation artwork is missing')
    if len(pixels) == height and pixels and isinstance(pixels[0], (list, tuple)):
        colors = [tuple(round(int(channel)/17) for channel in rgb) for row in pixels for rgb in row]
    else:
        colors = [tuple(palette[int(index)]) for index in pixels]
    if len(colors) != width * height:
        raise ValueError('Animation artwork is incomplete')
    mapping = {rgb: min(range(len(target)), key=lambda i:
                sum((int(rgb[k])-target[i][k])**2 for k in range(3))) for rgb in set(colors)}
    identity = [str(meta.get('callsign', '')).upper(), int(meta['card_id']), width, height, len(target)]
    return hashlib.blake2s(json.dumps(identity, separators=(',', ':')).encode() +
                           bytes(mapping[rgb] for rgb in colors), digest_size=8).hexdigest()


def encode_link(card):
    link = validate_link(getattr(card, 'animation', None), card.card_id)
    if link is None:
        return None
    body = LINK.pack(MAGIC, 1, link['first_card_id'], link['frame_ms'],
                     bytes.fromhex(link['first_digest']), bytes.fromhex(artwork_digest(card)))
    return body + struct.pack('>I', zlib.crc32(body))


def decode_link(frame):
    if len(frame) != LINK.size + 4 or zlib.crc32(frame[:-4]) != int.from_bytes(frame[-4:], 'big'):
        raise ValueError('Invalid animation reference checksum or length')
    magic, version, first, duration, first_digest, second_digest = LINK.unpack(frame[:-4])
    if magic != MAGIC or version != 1:
        raise ValueError('Unsupported animation reference')
    link = validate_link(dict(first_card_id=first, frame_ms=duration, first_digest=first_digest.hex()))
    return second_digest.hex(), link


class LinkCache:
    """Bounded sidecar cache. Conflicting references fail closed for that artwork."""
    def __init__(self, limit=128):
        self.limit = limit
        self.links = OrderedDict()

    def add(self, digest, link):
        link = validate_link(link)
        if digest in self.links and self.links[digest] != link:
            self.links[digest] = None
        else:
            self.links[digest] = link
        self.links.move_to_end(digest)
        while len(self.links) > self.limit:
            self.links.popitem(last=False)

    def attach(self, report):
        card = report.get('card') or {}
        if not card.get('exact'):
            return
        try:
            link = self.links.get(artwork_digest(report))
            if link:
                card['animation'] = validate_link(link, card.get('card_id'))
        except (ValueError, TypeError, KeyError, IndexError):
            return


def verified(report):
    return bool(report.get('sent_by_station') or (report.get('card') or {}).get('exact') or
                (report.get('assembly_event') or {}).get('verified_complete'))


def same_artwork(first, second):
    try:
        return artwork_digest(first) == artwork_digest(second)
    except (ValueError, KeyError, TypeError, IndexError):
        return False


def partner(second, candidates):
    meta = second.get('card') or {}
    try:
        link = validate_link(meta.get('animation'), meta.get('card_id'))
    except (ValueError, TypeError):
        return None
    if not link or not verified(second):
        return None
    matches = []
    for first in candidates:
        card = first.get('card') or {}
        if (not verified(first) or card.get('animation') or
                str(card.get('callsign', '')).upper() != str(meta.get('callsign', '')).upper() or
                card.get('card_id') != link['first_card_id'] or
                (card.get('width', 32), card.get('height', 32)) !=
                (meta.get('width', 32), meta.get('height', 32))):
            continue
        try:
            if artwork_digest(first) == link['first_digest']:
                matches.append(first)
        except (ValueError, TypeError, KeyError, IndexError):
            continue
    return matches[-1] if matches else None


def group_events(events, candidates=()):
    """Presentation only: retain the original saved card and reception records."""
    groups, hidden = {}, set()
    for index, (direction, second) in enumerate(events):
        if direction == 'sent_cards':
            # Sent snapshots are locally known artwork, not received evidence.
            second = {**second, 'sent_by_station': True}
        available = [report for d, report in events if d == direction]
        if direction == 'sent_cards':
            available = [{**report, 'sent_by_station': True} for report in available]
        if direction == 'received_cards':
            available += list(candidates)
        first = partner(second, available)
        if first is not None:
            grouped = copy.copy(second)
            grouped['animation_frames'] = [first, second]
            groups[index] = grouped
            # A single first frame can anchor several independent second frames.
            hidden.update(i for i, (d, report) in enumerate(events)
                          if d == direction and verified({**report, 'sent_by_station': d == 'sent_cards'})
                          and (report is first or (report.get('card') or {}).get('card_id') ==
                               (first.get('card') or {}).get('card_id') and same_artwork(report, first)))
    return [(direction, groups.get(i, report)) for i, (direction, report) in enumerate(events)
            if i not in hidden]


def frame_details(report, number):
    from snr import snr_lines
    card = report.get('card') or {}
    frequency = report.get('frequency_hz')
    return '\n'.join((f"Frame {number} · {card.get('callsign', 'Unknown')} · {card.get('grid', '')}",
        f"Card ID: {int(card.get('card_id', 0)):04X} · " +
        ('Sent by this station' if report.get('sent_by_station') else 'Whole card verified'),
        f"Received: {report.get('logged_at', 'time not recorded')}",
        f"Mode: {report.get('receive_profile', 'not recorded')}",
        f"Frequency: {float(frequency)/1e6:.6f} MHz" if frequency is not None else 'Frequency: not recorded',
        *snr_lines(card)))


def details(report):
    frames = report.get('animation_frames')
    if frames:
        duration = frames[1]['card']['animation']['frame_ms']
        return f'TWO-FRAME ANIMATION · {duration} ms per frame\n\n' + '\n\n'.join(
            frame_details(frame, i+1) for i, frame in enumerate(frames))
    link = (report.get('card') or {}).get('animation')
    if link:
        return f"Animation frame 2 · waiting for verified first card {link['first_card_id']:04X}"
    return ''

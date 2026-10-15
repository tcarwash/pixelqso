"""Optional, identity-bound block checks for the unchanged E-major raster."""
from collections import OrderedDict
from contextvars import ContextVar
import struct
import zlib

MODE = 'experimental_8fsk_musical_25'
VERSIONS = (8, 9, 10, 11)
BLOCK_PIXELS = 64
MAX_BLOCKS = 16
# Four protected common-envelope markers carry this fixed 64-byte table.
PACKET = struct.Struct('>4s16sHH16H4sI')
ADMITTED_TABLE = ContextVar('musical_crc_table', default=None)


def block_crc(index, pixels):
    from cardmodem import crc16
    return crc16(index.to_bytes(2, 'big') + bytes(pixels))


def encode_table(card, identity):
    import cardmodem as pixel
    count = card.width * card.height
    if not 0 < count <= BLOCK_PIXELS * MAX_BLOCKS:
        return None
    palette = pixel._avatar_palette_id(card)
    values = pixel._unpack_indices(pixel.minimal_avatar_payload(card), count,
                                   pixel.AVATAR_PALETTE_BITS[palette])
    checks = [block_crc(i//BLOCK_PIXELS, values[i:i+BLOCK_PIXELS])
              for i in range(0, count, BLOCK_PIXELS)]
    checks += [0] * (MAX_BLOCKS-len(checks))
    packet = PACKET.pack(b'PQBC', bytes(identity), count, BLOCK_PIXELS, *checks, b'\0'*4, 0)
    return packet[:-4] + struct.pack('>I', zlib.crc32(packet[:-4]))


def decode_table(packet):
    if len(packet) != PACKET.size:
        raise ValueError('incomplete musical CRC table')
    magic, identity, count, size, *rest = PACKET.unpack(packet)
    checks, reserved, crc = rest[:MAX_BLOCKS], rest[MAX_BLOCKS], rest[MAX_BLOCKS+1]
    if (magic != b'PQBC' or size != BLOCK_PIXELS or not 0 < count <= size*MAX_BLOCKS
            or reserved != b'\0'*4 or zlib.crc32(packet[:-4]) != crc
            or any(checks[(count+size-1)//size:])):
        raise ValueError('invalid musical CRC table')
    return dict(identity=identity.hex(), count=count, block_pixels=size,
                checks=checks[:(count+size-1)//size])


class TableCache:
    """Bound incomplete multipart tables and retain only fully checked tables."""
    def __init__(self, limit=32):
        self.limit = limit
        self.parts = OrderedDict()
        self.tables = OrderedDict()

    def add_part(self, mode, token, index, data):
        if mode != MODE or index not in range(4):
            return
        try:
            chunk = bytes.fromhex(data)
            if len(chunk) != 16:
                return
        except (TypeError, ValueError):
            return
        key = (mode, token)
        parts = self.parts.setdefault(key, {})
        # Conflicting chunks cannot produce a mixed table.
        if index in parts and parts[index] != chunk:
            self.parts.pop(key, None)
            return
        parts[index] = chunk
        self.parts.move_to_end(key)
        while len(self.parts) > self.limit:
            self.parts.popitem(last=False)
        if len(parts) == 4:
            packet = b''.join(parts[i] for i in range(4))
            if int.from_bytes(packet[-4:], 'big') == token:
                self.add(packet)
            self.parts.pop(key, None)

    def add(self, packet):
        try:
            table = decode_table(packet)
        except (ValueError, struct.error):
            return
        identity = table['identity']
        previous = self.tables.get(identity)
        if previous is not None and previous != packet:
            # Never choose between conflicting CRC tables for one identity.
            self.tables[identity] = None
        elif identity not in self.tables:
            self.tables[identity] = bytes(packet)
        self.tables.move_to_end(identity)
        while len(self.tables) > self.limit:
            self.tables.popitem(last=False)

    def get(self, identity):
        packet = self.tables.get(identity)
        return decode_table(packet) if packet else None

    def frames(self):
        return [packet.hex() for packet in self.tables.values() if packet]


class BlockCollector:
    """Union CRC-passed blocks across physical copies and clock hypotheses."""
    def __init__(self, table):
        self.table = table
        self.blocks = {}
        self.conflicts = set()
        self.signature = None

    def update(self, report):
        pixels, card = report
        count = self.table['count']
        if len(pixels) != count or card.get('width', 32)*card.get('height', 32) != count:
            return
        signature = tuple(card.get(k) for k in ('callsign', 'grid', 'card_id', 'palette_id',
                                                'message_type', 'snr_db'))
        if self.signature is None:
            self.signature = signature
        if signature != self.signature:
            return
        coverage = card.get('pixel_coverage') or []
        for index, check in enumerate(self.table['checks']):
            start = index * BLOCK_PIXELS
            end = min(start+BLOCK_PIXELS, count)
            values = tuple(pixels[start:end])
            if len(coverage) < end or not all(coverage[start:end]) or block_crc(index, values) != check:
                continue
            if index in self.blocks and self.blocks[index] != values:
                self.conflicts.add(index)
                self.blocks.pop(index, None)
            elif index not in self.conflicts:
                self.blocks[index] = values

    def apply(self, report):
        pixels, card = report
        pixels = list(pixels)
        card = dict(card)
        coverage = [False] * len(pixels)
        flags = [False] * len(self.table['checks'])
        for index, values in self.blocks.items():
            start = index * BLOCK_PIXELS
            pixels[start:start+len(values)] = values
            coverage[start:start+len(values)] = [True]*len(values)
            flags[index] = True
        card.update(pixel_coverage=coverage, verified_pixel_coverage=coverage,
                    received_blocks=flags, total_blocks=len(flags),
                    block_crc_scope='musical_crc16_sidecar')
        card['color_stage'] = (f'{sum(flags)}/{len(flags)} blocks CRC verified · '
                               'whole-card verification pending')
        return pixels, card

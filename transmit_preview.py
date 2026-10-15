"""Outgoing artwork progress from played audio or host-completed fragments."""
from bisect import bisect_right
import copy
import math
import cardmodem as modem


def wire_artwork(card, mode):
    if mode == 'standard':
        palette, pixels = list(card.palette), list(card.pixels)
    else:
        palette_id = modem._avatar_palette_id(card)
        palette = list(modem.AVATAR_PALETTES[palette_id])
        pixels = modem._unpack_indices(modem.minimal_avatar_payload(card), len(card.pixels),
                                      modem.AVATAR_PALETTE_BITS[palette_id])
    return {'card': {'callsign': card.callsign, 'grid': card.grid, 'card_id': card.card_id,
                     'width': card.width, 'height': card.height, 'palette': palette}, 'pixels': pixels}


class LocalTransmitPreview:
    """Raster checkpoints for serial modes; waveform progress for coded bursts.

    The preview describes output playback, never remote reception. Whole-card
    encoders spread image evidence throughout a burst, so use a global reveal
    rather than inventing a row/region transmission order for those modes.
    """
    def __init__(self, card, mode, backend, copies, message_type, snr_db, samples):
        from modem_envelope import (HEADER_SECONDS, metadata_header_count, block_crc_header_count, GROUP_MODES)
        from card_backends import AvatarBackend
        self.card = copy.deepcopy(card)
        self.report = wire_artwork(card, mode)
        self.copies = max(1, int(copies))
        self.seconds = samples / modem.SAMPLE_RATE
        self.serial = isinstance(backend, AvatarBackend)
        self.raw = self.serial and not backend.checked
        self.checkpoints = []
        metadata_seconds = metadata_header_count(card, mode)*HEADER_SECONDS/2
        crc_extra = block_crc_header_count(card, mode)*HEADER_SECONDS
        self.crc_prefix = crc_extra/2
        self.crc_suffix = crc_extra/2
        self.prefix = metadata_seconds + HEADER_SECONDS
        self.suffix = metadata_seconds + HEADER_SECONDS
        if mode in GROUP_MODES:
            self.cycle_seconds = (self.seconds-self.prefix-metadata_seconds)/self.copies
            self.suffix = 0
            self.grouped = True
        else:
            self.cycle_seconds = max(0, self.seconds-self.crc_prefix-self.crc_suffix)/self.copies
            self.grouped = False
        if self.serial:
            bpp = modem.AVATAR_PALETTE_BITS[modem._avatar_palette_id(card)]
            self.bpp = bpp; self.baud = backend.profile.baud
            header = len(modem.FRAME_SYNC) + len(modem._encode_avatar_metadata(
                card, resilient=backend.checked, message_type=message_type,
                snr_db=snr_db, wire_version=backend.wire_version))
            self.native_header_seconds = header/self.baud
            if backend.checked:
                overhead = modem.AVATAR_V4_BLOCK_OVERHEAD if backend.wire_version == 4 else 0
                per_block = ((modem.AVATAR_RS_K-overhead)*6)//bpp
                elapsed = self.native_header_seconds
                for start in range(0, len(card.pixels), per_block):
                    count = min(per_block, len(card.pixels)-start)
                    values = [0] * (overhead + math.ceil(count*bpp/6))
                    elapsed += len(modem._avatar_block_symbols(values))/self.baud
                    self.checkpoints.append((elapsed, start+count))

    def state(self, played_samples):
        seconds = max(0, played_samples)/modem.SAMPLE_RATE
        count = len(self.card.pixels)
        progress = min(1.0, seconds/max(self.seconds, 1e-9))
        if seconds >= self.seconds:
            return dict(report=self.report, coverage=[True]*count, opacity=1.0, progress=1.0,
                        copy=self.copies, phase='Card audio complete', detail='Local playback · remote receipt unconfirmed')
        if self.grouped:
            position = max(0, seconds-self.prefix)
            copy_index = min(self.copies-1, int(position/max(self.cycle_seconds, 1e-9)))
            native = position-copy_index*self.cycle_seconds
            payload_seconds = self.cycle_seconds
            pending = seconds < self.prefix
        else:
            position_after_crc = max(0, seconds-self.crc_prefix)
            copy_index = min(self.copies-1, int(position_after_crc/max(self.cycle_seconds, 1e-9)))
            position = position_after_crc-copy_index*self.cycle_seconds
            native = position-self.prefix
            payload_seconds = max(1e-9, self.cycle_seconds-self.prefix-self.suffix)
            final_crc = seconds >= self.crc_prefix + self.cycle_seconds*self.copies
            pending = seconds < self.crc_prefix or native < 0
        if not self.grouped and final_crc and seconds < self.seconds:
            covered, opacity = count, 1.0
            phase, detail = 'Sending final block CRC table', 'Outgoing raster complete · remote receipt unconfirmed'
        elif pending:
            covered, opacity = 0, .12
            phase, detail = ('Sending block CRC table' if seconds < self.crc_prefix else 'Sending identity and report metadata'), 'Local audio playback'
        elif self.serial:
            if self.raw:
                tones = max(0, math.floor((native-self.native_header_seconds)*self.baud+1e-7))
                covered = min(count, tones*3//self.bpp)
                phase = 'Sending image pixels'
            else:
                ends = [when for when, _ in self.checkpoints]
                completed = bisect_right(ends, native+1e-7)
                covered = self.checkpoints[completed-1][1] if completed else 0
                phase = f'Sending image blocks · {completed}/{len(self.checkpoints)} played'
            opacity = 1.0
            detail = 'Outgoing raster · follows audio playback'
        else:
            covered = None
            opacity = .12 + .88*min(1, max(0, native/payload_seconds))
            phase, detail = 'Sending whole-card encoded burst', 'Image brightens with playback progress'
        return dict(report=self.report, coverage=None if covered is None else [i < covered for i in range(count)],
                    opacity=opacity, progress=progress, copy=copy_index+1, phase=phase, detail=detail)


class Data2GTransmitPreview:
    """Reveal only image bytes in frames the local host confirmed transmitted."""
    def __init__(self, card, frames_per_copy, total_frames):
        from card_transfer import CardTransferReceiver
        self.report = wire_artwork(card, 'data2g')
        self.per_copy = max(1, frames_per_copy)
        self.total = max(1, total_frames)
        self.completed = 0; self.copy = 1
        self.coverage = [False]*len(card.pixels)
        self.receiver = CardTransferReceiver()
        self.phase = 'Waiting for host transmission'

    def acknowledge(self, frame):
        from card_transfer import MAGIC, CardTransferReceiver
        if self.completed and self.completed % self.per_copy == 0:
            self.copy += 1
            self.receiver = CardTransferReceiver()
            self.coverage = [False]*len(self.coverage)
        self.completed += 1
        if frame.startswith(MAGIC):
            _, metadata = self.receiver.feed('LOCAL-TX', frame)
            self.report = {'card': {**self.report['card'], **metadata}, 'pixels': metadata['pixels']}
            self.coverage = list(metadata['pixel_coverage'])
            self.phase = 'Host transmitted image fragment'
        else:
            self.phase = 'Host transmitted card metadata'

    def state(self):
        return dict(report=self.report, coverage=self.coverage, opacity=1.0,
                    progress=min(1, self.completed/self.total), copy=self.copy,
                    phase=self.phase, detail='Local host confirmation · remote receipt unconfirmed')

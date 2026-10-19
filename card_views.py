"""Trading-card presentation for the local contact collection."""
from PySide6.QtCore import Qt, QSize, QRectF, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap, QIcon, QFont, QImage
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget, QListWidgetItem, QSizePolicy, QStackedWidget
from ui_theme import COLORS, SPACING
from snr import snr_lines
from card_animation import group_events, details as animation_details, frame_details
import time


def incoming_card_text(report, mode_label=None):
    """Show decoded metadata without requiring image pixels or whole-card proof."""
    card = report.get('card') or {}
    diagnostic = report.get('copy_header') or card.get('diagnostics') or report.get('diagnostics') or {}
    call, grid = card.get('callsign'), card.get('grid')
    lines = ['Incoming: ' + ' · '.join(str(value) for value in (call, grid) if value)
             if call or grid else 'Incoming card · station identity pending']
    identity = []
    if card.get('card_id') is not None:
        identity.append(f"Card ID: {int(card['card_id']):04X}")
    if card.get('message_type'):
        identity.append(f"Stage: {stage_label(report)}")
    if card.get('width') and card.get('height'):
        identity.append(f"Image: {card['width']} × {card['height']}")
    if card.get('palette'):
        identity.append(f"Colors: {len(card['palette'])}")
    elif card.get('palette_id') is not None:
        identity.append(f"Palette ID: {card['palette_id']}")
    if identity:
        lines.append(' · '.join(identity))
    mode = mode_label or report.get('receive_profile') or diagnostic.get('announced_mode')
    if mode:
        lines.append(f"RX mode: {mode}")
    if card.get('measured_snr_db') is not None or card.get('snr_db') is not None or card.get('reception_report'):
        lines.extend(snr_lines(card))
    if report.get('frequency_hz') is not None:
        lines.append(f"Frequency: {float(report['frequency_hz'])/1e6:.6f} MHz")
    progress = []
    blocks = card.get('received_blocks')
    if blocks:
        progress.append(f"CRC-checked blocks: {sum(bool(value) for value in blocks)}/{len(blocks)}")
    coverage = card.get('pixel_coverage')
    if coverage:
        progress.append(f"Image received: {round(100*sum(bool(value) for value in coverage)/len(coverage))}%")
    copies = card.get('received_copies', diagnostic.get('announced_copies'))
    if copies is not None:
        progress.append(f"Copies: {copies}")
    if progress:
        lines.append(' · '.join(progress))
    content = card.get('image_id') or diagnostic.get('content_id')
    if content:
        lines.append(f"Content ID: {str(content)[:16]}")
    if diagnostic.get('payload_samples') is not None:
        lines.append(f"Announced payload: {float(diagnostic['payload_samples'])/48000:g} seconds")
    if diagnostic.get('corrected_symbols') is not None:
        lines.append(f"Protected header corrections: {diagnostic['corrected_symbols']}")
    if diagnostic.get('codewords'):
        lines.append(f"Burst codewords: {diagnostic['codewords']}")
    timing = []
    if diagnostic.get('carrier_offset_hz') is not None:
        timing.append(f"Frequency offset: {float(diagnostic['carrier_offset_hz']):+g} Hz")
    if diagnostic.get('clock_ppm') is not None:
        timing.append(f"Clock estimate: {float(diagnostic['clock_ppm']):+g} ppm")
    if timing:
        lines.append(' · '.join(timing))
    lines.append('Whole card verified' if card.get('exact') else 'Image incomplete · waiting for more data')
    return '\n'.join(lines)


def deck_events(entry):
    events = [(direction, card) for direction in ('sent_cards', 'received_cards') for card in entry.get(direction, [])]
    return sorted(events, key=lambda event: int(event[1].get('order', 0)))


class AnimationDetailsDialog(QDialog):
    def __init__(self, report, image_for_card, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Two-frame animation details")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        duration = report['animation_frames'][1]['card']['animation']['frame_ms']
        heading = QLabel(f"Two-frame animation · {duration} ms per frame")
        heading.setObjectName('sectionTitle'); heading.setMaximumHeight(40)
        layout.addWidget(heading)
        frames = QHBoxLayout()
        for number, frame in enumerate(report['animation_frames'], 1):
            column = QVBoxLayout()
            thumbnail = QLabel(); image = image_for_card(frame)
            if image is not None:
                thumbnail.setPixmap(QPixmap.fromImage(image).scaled(160, 160, Qt.AspectRatioMode.KeepAspectRatio))
            thumbnail.setAccessibleName(f"Frame {number} artwork")
            column.addWidget(thumbnail, 0, Qt.AlignmentFlag.AlignCenter)
            text = QLabel(frame_details(frame, number)); text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            column.addWidget(text); frames.addLayout(column)
        layout.addLayout(frames, 1)
        close = QPushButton("Close"); close.clicked.connect(self.accept); layout.addWidget(close)


def stage_label(card):
    meta = card.get('card') or {}
    kind = meta.get('message_type', 'card')
    return {
        'cq': 'CQ',
        'exchange': 'EXCHANGE',
        'report73': 'RR73 · REPORT',
        'final73': '73 · FINAL',
        # Older contact archives use a single 73 stage; infer the report form
        # only when an SNR value was recorded with it.
        '73': 'RR73 · REPORT' if meta.get('snr_db') is not None or meta.get('reception_report') else '73 · GOODBYE',
    }.get(kind, 'IMAGE CARD')


def format_card_id(card):
    value = (card.get('card') or card).get('card_id')
    if value is None:
        return 'Not available'
    try:
        return f"0x{int(value) & 0xFFFF:04X}"
    except (TypeError, ValueError):
        return str(value)


def card_export_metadata(direction, report):
    """Compact identity and both measured/reported SNR for PNG captions."""
    card = report.get('card') or {}
    parts = [f"Card ID {format_card_id(card)}"]
    measured = card.get('measured_snr_db')
    reported = card.get('snr_db')
    if measured is not None:
        parts.append(f"Measured {float(measured):+g} dB")
    if reported is not None:
        parts.append(f"Reported {float(reported):+g} dB")
    if measured is None and reported is None:
        parts.append('SNR unavailable')
    return ' · '.join(parts)


def deck_image(entry, image_for_card):
    """Render every stage in order as a shareable contact sheet."""
    events = deck_events(entry)
    if not events:
        raise ValueError('This contact has no saved cards to export.')
    columns = min(4, len(events))
    rows = (len(events) + columns - 1) // columns
    image = QImage(columns * 260 + 32, rows * 380 + 116, QImage.Format.Format_RGB32)
    if image.isNull():
        raise ValueError('This contact is too large to render as one PNG.')
    image.fill(QColor(COLORS['card_art_background']))
    painter = QPainter(image)
    try:
        painter.setPen(QColor(COLORS['text']))
        font = QFont(); font.setPixelSize(24); font.setBold(True); painter.setFont(font)
        painter.drawText(QRectF(24, 16, image.width()-48, 34), Qt.AlignmentFlag.AlignLeft,
                         f"Contact with {entry.get('peer_callsign', 'Unknown')}")
        font.setPixelSize(14); font.setBold(False); painter.setFont(font)
        painter.drawText(QRectF(24, 54, image.width()-48, 30), Qt.AlignmentFlag.AlignLeft,
                         f"{entry.get('peer_grid', '')} · {entry.get('started_at', '')} · {entry.get('status', 'Saved')}")
        for index, (direction, card) in enumerate(events):
            x, y = 16 + (index % columns)*260, 96 + (index // columns)*380
            meta = card.get('card') or {}
            provisional = direction == 'received_cards' and not bool(card.get('exact'))
            duplicate = direction == 'received_cards' and bool(card.get('duplicate_receipt'))
            tile = card_art(image_for_card(card), str(meta.get('callsign', 'Unknown')),
                            ('DUPLICATE · ' if duplicate else '') + stage_label(card) +
                            (' · PROVISIONAL' if provisional else ''),
                            QSize(240, 300))
            painter.drawPixmap(x+10, y, tile)
            direction_label = 'Sent' if direction == 'sent_cards' else 'Received'
            painter.drawText(QRectF(x, y+301, 260, 18), Qt.AlignmentFlag.AlignCenter,
                             f"{index+1} · {direction_label} · {stage_label(card)}")
            font.setPixelSize(10); painter.setFont(font)
            painter.drawText(QRectF(x, y+319, 260, 18), Qt.AlignmentFlag.AlignCenter,
                             card_export_metadata(direction, card))
            font.setPixelSize(10); painter.setFont(font)
            timestamp = card.get('logged_at') or 'Event time not recorded'
            painter.drawText(QRectF(x, y+338, 260, 18), Qt.AlignmentFlag.AlignCenter,
                             timestamp)
            font.setPixelSize(14); painter.setFont(font)
    finally:
        painter.end()
    return image


def single_card_image(direction, report, image_for_card):
    """Render one deck card as a PNG with its identity and signal metadata."""
    meta = report.get('card') or {}
    art = image_for_card(report)
    if art is None or art.isNull():
        raise ValueError('This card has no usable image data.')
    width, card_height, footer = 680, 800, 112
    image = QImage(width, card_height + footer, QImage.Format.Format_RGB32)
    if image.isNull():
        raise ValueError('This card is too large to export.')
    image.fill(QColor(COLORS['card_art_background']))
    painter = QPainter(image)
    try:
        pixmap = card_art(art, str(meta.get('callsign') or 'Unknown station'),
                          stage_label(report), QSize(640, card_height))
        painter.drawPixmap(20, 0, pixmap)
        painter.setPen(QColor(COLORS['text']))
        font = QFont(); font.setPixelSize(18); font.setBold(True); painter.setFont(font)
        direction_label = 'SENT' if direction == 'sent_cards' else 'RECEIVED'
        painter.drawText(QRectF(24, card_height+6, width-48, 24),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         f"{direction_label} · {stage_label(report)}")
        font.setPixelSize(14); font.setBold(False); painter.setFont(font)
        painter.drawText(QRectF(24, card_height+32, width-48, 22),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         card_export_metadata(direction, report))
        painter.drawText(QRectF(24, card_height+58, width-48, 22),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         f"{'Sent' if direction == 'sent_cards' else 'Received'} · "
                         f"{report.get('logged_at') or 'time not recorded'}")
    finally:
        painter.end()
    return image


def card_art(image, title, subtitle='', size=QSize(240, 300), stacked=False):
    pixmap = QPixmap(size); pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size.width()/240, size.height()/300)
    width, height = 240, 300
    margin = 18 if stacked else 6
    if stacked:
        for offset in (12, 6):
            p.setPen(QColor(COLORS['card_stack_border'])); p.setBrush(QColor(COLORS['card_stack']))
            p.drawRoundedRect(QRectF(margin-offset/2, margin-offset, width-2*margin, height-2*margin), 13, 13)
    p.setPen(QColor(COLORS['card_frame_border'])); p.setBrush(QColor(COLORS['card_frame']))
    p.drawRoundedRect(QRectF(margin, margin, width-2*margin, height-2*margin), 18, 18)
    art = QRectF(margin+12, margin+12, width-2*margin-24, height-2*margin-76)
    p.fillRect(art, QColor(COLORS['card_art_background']))
    if image is not None and not image.isNull():
        scaled = QPixmap.fromImage(image).scaled(art.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
        p.drawPixmap(int(art.center().x()-scaled.width()/2), int(art.center().y()-scaled.height()/2), scaled)
    p.setPen(QColor(COLORS['card_title'])); font = QFont(); font.setPixelSize(max(13, width//17)); font.setBold(True); p.setFont(font)
    p.drawText(QRectF(margin+13, height-margin-57, width-2*margin-26, 24), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
    font.setBold(False); font.setPixelSize(max(11, width//22)); p.setFont(font); p.setPen(QColor(COLORS['card_subtitle']))
    p.drawText(QRectF(margin+13, height-margin-31, width-2*margin-26, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, subtitle)
    p.end(); return pixmap


class ArtworkView(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def __init__(self):
        super().__init__()
        self.original = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumHeight(240)

    def set_artwork(self, pixmap):
        self.original = pixmap
        self.resize_artwork()

    def resize_artwork(self):
        if self.original is not None:
            self.setPixmap(self.original.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.FastTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resize_artwork()


class DeckDialog(QDialog):
    """Open a contact as a large card with a browsable chronological hand."""
    def __init__(self, entry, image_for_card, add_received_card=None, parent=None,
                 export_contact=None, export_card=None):
        super().__init__(parent)
        self.events = group_events(deck_events(entry), entry.get('animation_candidates', ()))
        self.image_for_card = image_for_card
        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self._advance_animation)
        self.animation_frames = []
        self.add_received_card = add_received_card
        self.export_contact = export_contact
        self.export_card = export_card
        self.entry = entry
        received_archive = entry.get('deck_type') == 'received_archive'
        callsign = entry.get('peer_callsign', 'Unknown')
        self.setWindowTitle(
            f"Received cards from {callsign}" if received_archive else f"Contact with {callsign}")
        self.resize(780, 760)
        layout = QVBoxLayout(self); layout.setSpacing(SPACING['md'])
        heading = QLabel(callsign)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        metadata = QLabel(
            (f"{entry.get('peer_grid', '')}  ·  All received cards across sessions  ·  "
             f"{len(entry.get('received_cards', []))} unique cards") if received_archive else
            f"{entry.get('peer_grid', '')}  ·  {entry.get('started_at', '')}  ·  "
            f"{entry.get('status', 'Saved')}")
        metadata.setObjectName("supportingText")
        layout.addWidget(metadata)
        self.card_faces = QStackedWidget()
        self.art = ArtworkView()
        self.card_faces.addWidget(self.art)
        self.card_back = QLabel()
        self.card_back.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card_back.setWordWrap(True)
        self.card_back.setObjectName("supportingText")
        self.card_back.setStyleSheet(
            "background:#17231d; border:1px solid #52705a; border-radius:12px; padding:24px; "
            "font-size:16px; color:#edf1e8;")
        self.card_faces.addWidget(self.card_back)
        layout.addWidget(self.card_faces, 1)
        self.details = QLabel(); self.details.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details.setWordWrap(True)
        self.details.setAccessibleDescription(
            "Direction, message stage, recorded time, and signal report for the selected card.")
        layout.addWidget(self.details)
        self.flip_button = QPushButton("Flip card")
        self.flip_button.setAccessibleName("Flip selected QSO card to its receive details")
        self.flip_button.clicked.connect(self.flip_card)
        self.art.clicked.connect(self.flip_card)
        layout.addWidget(self.flip_button, 0, Qt.AlignmentFlag.AlignCenter)
        self.pause_animation_button = QPushButton('Pause animation')
        self.pause_animation_button.setCheckable(True)
        self.pause_animation_button.toggled.connect(self._pause_animation)
        layout.addWidget(self.pause_animation_button, 0, Qt.AlignmentFlag.AlignCenter)
        row = QHBoxLayout(); self.previous = QPushButton('← Previous'); self.next = QPushButton('Next →')
        self.previous.clicked.connect(lambda: self.strip.setCurrentRow(self.strip.currentRow()-1))
        self.next.clicked.connect(lambda: self.strip.setCurrentRow(self.strip.currentRow()+1))
        self.previous.setShortcut('Left'); self.next.setShortcut('Right')
        row.addWidget(self.previous); self.counter = QLabel(); self.counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.counter.setAccessibleDescription("Current card position in this contact's chronological history.")
        row.addWidget(self.counter, 1); row.addWidget(self.next); layout.addLayout(row)
        self.strip = QListWidget(); self.strip.setViewMode(QListWidget.ViewMode.IconMode); self.strip.setMovement(QListWidget.Movement.Static)
        self.strip.setFlow(QListWidget.Flow.LeftToRight); self.strip.setWrapping(False)
        self.strip.setIconSize(QSize(80, 100)); self.strip.setGridSize(QSize(112, 135)); self.strip.setFixedHeight(155)
        self.strip.setAccessibleName(
            f"Received cards from {callsign}" if received_archive else
            "Contact cards sent and received")
        self.strip.setAccessibleDescription(
            "All received cards for this callsign, ordered by first reception. Select a card to view its details."
            if received_archive else
            "Chronological list of cards in this contact. Select a card to view its details.")
        for direction, card in self.events:
            label = 'TWO-FRAME ANIMATION' if card.get('animation_frames') else stage_label(card)
            direction_label = 'Sent' if direction == 'sent_cards' else 'Received'
            provisional = direction == 'received_cards' and not bool((card.get('card') or {}).get('exact'))
            duplicate = direction == 'received_cards' and bool(card.get('duplicate_receipt'))
            item = QListWidgetItem(direction_label + (' · Duplicate receipt' if duplicate else '') +
                                   (' · Provisional' if provisional else ''))
            subtitle = ('DUPLICATE · ' if duplicate else '') + label + (' · PROVISIONAL' if provisional else '')
            item.setIcon(QIcon(card_art(image_for_card(card), label, subtitle, size=QSize(80, 100))))
            logged_at = card.get('logged_at')
            timestamp = f"Recorded {logged_at}" if logged_at else "Event time not recorded"
            accessible_label = f"{direction_label} {label}" + (" · duplicate receipt" if duplicate else "") + (" · provisional" if provisional else "") + f" · {timestamp}"
            item.setToolTip(accessible_label)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, accessible_label)
            self.strip.addItem(item)
        self.strip.currentRowChanged.connect(self.show_card); layout.addWidget(self.strip)
        actions = QHBoxLayout()
        self.copy_contact_button = QPushButton(
            'Copy received archive PNG' if received_archive else 'Copy contact PNG')
        self.copy_contact_button.clicked.connect(self._copy_contact_png)
        actions.addWidget(self.copy_contact_button)
        self.export_contact_button = QPushButton(
            'Export received archive…' if received_archive else 'Export contact…')
        self.export_contact_button.clicked.connect(self._export_contact)
        self.export_contact_button.setVisible(self.export_contact is not None)
        actions.addWidget(self.export_contact_button)
        self.export_card_button = QPushButton('Export selected card PNG/GIF…')
        self.export_card_button.clicked.connect(self._export_selected_card)
        self.export_card_button.setVisible(self.export_card is not None)
        actions.addWidget(self.export_card_button)
        self.add_button = QPushButton('Add received card to My Cards')
        self.add_button.clicked.connect(self._add_current_received)
        actions.addWidget(self.add_button)
        close = QPushButton('Close deck'); close.clicked.connect(self.accept); actions.addWidget(close)
        layout.addLayout(actions)
        self.share_status = QLabel('')
        self.share_status.setWordWrap(True)
        self.share_status.setAccessibleDescription(
            "Result of copying or exporting the contact card history.")
        layout.addWidget(self.share_status)
        self.strip.setCurrentRow(0)

    def _copy_contact_png(self):
        try:
            QApplication.clipboard().setImage(deck_image(self.entry, self.image_for_card))
            self.share_status.setText('Contact PNG copied. Paste it into a message or image editor.')
        except Exception as exc:
            self.share_status.setText(f"Couldn't copy contact PNG: {exc}")

    def flip_card(self):
        index = self.strip.currentRow()
        if 0 <= index < len(self.events) and self.events[index][1].get('animation_frames'):
            AnimationDetailsDialog(self.events[index][1], self.image_for_card, self).exec()
            return
        show_back = self.card_faces.currentWidget() is self.art
        self.card_faces.setCurrentWidget(self.card_back if show_back else self.art)
        self.flip_button.setText("Show card front" if show_back else "Flip card")
        self.flip_button.setAccessibleName(
            "Show selected QSO card front" if show_back else
            "Flip selected QSO card to its receive details")

    def _export_contact(self):
        if self.export_contact is None:
            return
        target = self.export_contact()
        if target:
            self.share_status.setText(f"Contact exported to {target}")

    def _export_selected_card(self):
        index = self.strip.currentRow()
        if self.export_card is None or not 0 <= index < len(self.events):
            return
        direction, card = self.events[index]
        target = self.export_card(direction, card)
        if target:
            self.share_status.setText(f"Card exported to {target}")

    def _add_current_received(self):
        index = self.strip.currentRow()
        if self.add_received_card is None or not 0 <= index < len(self.events):
            return
        direction, card = self.events[index]
        if direction != 'received_cards' or not bool(card.get('exact')):
            return
        result = self.add_received_card(card)
        if result:
            self.add_button.setText('Already in My Cards' if result == 'already' else 'Added to My Cards')
            self.add_button.setEnabled(False)

    def show_card(self, index):
        if not 0 <= index < len(self.events): return
        direction, card = self.events[index]; meta = card.get('card') or {}
        self.animation_timer.stop()
        self.animation_frames = []
        self.pause_animation_button.setChecked(False)
        self.pause_animation_button.setVisible(bool(card.get('animation_frames')))
        self.art.set_artwork(card_art(self.image_for_card(card), meta.get('callsign', 'Unknown'), stage_label(card), QSize(320, 400)))
        self.card_faces.setCurrentWidget(self.art)
        self.flip_button.setText("Flip card")
        self.flip_button.setEnabled(True)
        self.flip_button.setAccessibleName("Flip selected QSO card to its details")
        if card.get('animation_frames'):
            self.animation_frames = [card_art(self.image_for_card(frame), meta.get('callsign', 'Unknown'),
                                      'TWO-FRAME ANIMATION', QSize(320, 400))
                                     for frame in card['animation_frames']]
            self.animation_frame_ms = meta['animation']['frame_ms']
            self.animation_timer.start(min(100, self.animation_frame_ms))
            self.art.set_artwork(self.animation_frames[0])
            self.flip_button.setText("Animation details · both frames")
            self.flip_button.setAccessibleName("Show details of both animation frames")
        detail = ('Sent by you' if direction == 'sent_cards' else 'Received')
        if direction == 'received_cards' and card.get('duplicate_receipt'):
            detail += ' · Duplicate receipt'
        detail += f" · {stage_label(card)}"
        if card.get('logged_at'):
            detail += f" · Recorded {card['logged_at']}"
        else:
            detail += " · Event time not recorded in this log"
        detail += " · " + " · ".join(snr_lines(meta, sent=direction == 'sent_cards'))
        self.details.setText(detail)
        coverage = meta.get('pixel_coverage') or []
        received = sum(bool(value) for value in coverage)
        received_percent = round(received * 100 / len(coverage)) if coverage else 0
        verified_coverage = meta.get('verified_pixel_coverage') or []
        verified = sum(bool(value) for value in verified_coverage)
        verified_percent = round(verified * 100 / len(verified_coverage)) if verified_coverage else 0
        integrity = ("Sent by this station" if direction == 'sent_cards' else
                     "WHOLE CARD VERIFIED" if meta.get('exact') else
                     f"PROVISIONAL · {received_percent}% received · {verified_percent}% of image regions verified")
        direction_label = 'SENT BY YOU' if direction == 'sent_cards' else 'RECEIVED'
        if direction == 'received_cards' and card.get('duplicate_receipt'):
            direction_label = 'DUPLICATE RECEIPT'
        self.card_back.setText("\n".join((
            direction_label,
            stage_label(card),
            f"From: {meta.get('callsign') or 'Unknown station'}",
            f"Grid: {meta.get('grid') or 'Not available'}",
            f"Card ID: {format_card_id(meta)}",
            integrity,
            *snr_lines(meta, sent=direction == 'sent_cards'),
            f"Received: {card.get('logged_at') or 'time not recorded'}",
        )))
        animation_text = animation_details(card)
        if animation_text:
            self.card_back.setText(self.card_back.text() + '\n\n' + animation_text)
            self.details.setText(self.details.text() + ' · ' +
                                 ('Two-frame animation' if card.get('animation_frames') else animation_text))
        self.add_button.setVisible(
            direction == 'received_cards' and bool(meta.get('exact')) and
            self.add_received_card is not None)
        self.add_button.setText('Add received card to My Cards')
        self.add_button.setEnabled(True)
        self.counter.setText(f'{index+1} / {len(self.events)}')
        self.previous.setEnabled(index > 0); self.next.setEnabled(index+1 < len(self.events))

    def _advance_animation(self):
        if self.animation_frames and self.isVisible() and self.card_faces.currentWidget() is self.art:
            self.art.set_artwork(self.animation_frames[int(time.monotonic()*1000/self.animation_frame_ms) % 2])

    def _pause_animation(self, paused):
        self.pause_animation_button.setText('Resume animation' if paused else 'Pause animation')
        if paused:
            self.animation_timer.stop()
        elif self.animation_frames:
            self.animation_timer.start(100)

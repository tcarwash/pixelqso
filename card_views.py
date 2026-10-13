"""Trading-card presentation for the local contact collection."""
from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QColor, QPainter, QPixmap, QIcon, QFont, QImage
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget, QListWidgetItem, QSizePolicy
from ui_theme import COLORS, SPACING


def deck_events(entry):
    events = [(direction, card) for direction in ('sent_cards', 'received_cards') for card in entry.get(direction, [])]
    return sorted(events, key=lambda event: int(event[1].get('order', 0)))


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
        '73': 'RR73 · REPORT' if meta.get('snr_db') is not None else '73 · GOODBYE',
    }.get(kind, 'IMAGE CARD')


def deck_image(entry, image_for_card):
    """Render every stage in order as a shareable contact sheet."""
    events = deck_events(entry)
    if not events:
        raise ValueError('This contact has no saved cards to export.')
    columns = min(4, len(events))
    rows = (len(events) + columns - 1) // columns
    image = QImage(columns * 260 + 32, rows * 350 + 116, QImage.Format.Format_RGB32)
    if image.isNull():
        raise ValueError('This contact is too large to render as one PNG.')
    image.fill(QColor(COLORS['card_art_background']))
    painter = QPainter(image)
    try:
        painter.setPen(QColor(COLORS['card_title']))
        font = QFont(); font.setPixelSize(24); font.setBold(True); painter.setFont(font)
        painter.drawText(QRectF(24, 16, image.width()-48, 34), Qt.AlignmentFlag.AlignLeft,
                         f"Contact with {entry.get('peer_callsign', 'Unknown')}")
        font.setPixelSize(14); font.setBold(False); painter.setFont(font)
        painter.drawText(QRectF(24, 54, image.width()-48, 30), Qt.AlignmentFlag.AlignLeft,
                         f"{entry.get('peer_grid', '')} · {entry.get('started_at', '')} · {entry.get('status', 'Saved')}")
        for index, (direction, card) in enumerate(events):
            x, y = 16 + (index % columns)*260, 96 + (index // columns)*350
            meta = card.get('card') or {}
            tile = card_art(image_for_card(card), str(meta.get('callsign', 'Unknown')),
                            stage_label(card), QSize(240, 300))
            painter.drawPixmap(x+10, y, tile)
            direction_label = 'Sent' if direction == 'sent_cards' else 'Received'
            painter.drawText(QRectF(x, y+301, 260, 18), Qt.AlignmentFlag.AlignCenter,
                             f"{index+1} · {direction_label} · {stage_label(card)}")
            font.setPixelSize(11); painter.setFont(font)
            timestamp = card.get('logged_at') or 'Event time not recorded'
            painter.drawText(QRectF(x, y+319, 260, 18), Qt.AlignmentFlag.AlignCenter,
                             timestamp)
            font.setPixelSize(14); painter.setFont(font)
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
    p.drawRoundedRect(QRectF(margin, margin, width-2*margin, height-2*margin), 12, 12)
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
                                                Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resize_artwork()


class DeckDialog(QDialog):
    """Open a contact as a large card with a browsable chronological hand."""
    def __init__(self, entry, image_for_card, add_received_card=None, parent=None,
                 export_contact=None):
        super().__init__(parent)
        self.events = deck_events(entry); self.image_for_card = image_for_card
        self.add_received_card = add_received_card
        self.export_contact = export_contact
        self.entry = entry
        self.setWindowTitle(f"Contact with {entry.get('peer_callsign', 'Unknown')}")
        self.resize(780, 760)
        layout = QVBoxLayout(self); layout.setSpacing(SPACING['md'])
        heading = QLabel(entry.get('peer_callsign', 'Unknown'))
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        metadata = QLabel(
            f"{entry.get('peer_grid', '')}  ·  {entry.get('started_at', '')}  ·  "
            f"{entry.get('status', 'Saved')}")
        metadata.setObjectName("supportingText")
        layout.addWidget(metadata)
        self.art = ArtworkView(); layout.addWidget(self.art, 1)
        self.details = QLabel(); self.details.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details.setAccessibleDescription(
            "Direction, message stage, recorded time, and signal report for the selected card.")
        layout.addWidget(self.details)
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
        self.strip.setAccessibleName("Contact cards sent and received")
        self.strip.setAccessibleDescription(
            "Chronological list of cards in this contact. Select a card to view its details.")
        for direction, card in self.events:
            label = stage_label(card)
            direction_label = 'Sent' if direction == 'sent_cards' else 'Received'
            item = QListWidgetItem(direction_label)
            item.setIcon(QIcon(card_art(image_for_card(card), label, size=QSize(80, 100))))
            logged_at = card.get('logged_at')
            timestamp = f"Recorded {logged_at}" if logged_at else "Event time not recorded"
            accessible_label = f"{direction_label} {label} · {timestamp}"
            item.setToolTip(accessible_label)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, accessible_label)
            self.strip.addItem(item)
        self.strip.currentRowChanged.connect(self.show_card); layout.addWidget(self.strip)
        actions = QHBoxLayout()
        self.copy_contact_button = QPushButton('Copy contact PNG')
        self.copy_contact_button.clicked.connect(self._copy_contact_png)
        actions.addWidget(self.copy_contact_button)
        self.export_contact_button = QPushButton('Export contact…')
        self.export_contact_button.clicked.connect(self._export_contact)
        self.export_contact_button.setVisible(self.export_contact is not None)
        actions.addWidget(self.export_contact_button)
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

    def _export_contact(self):
        if self.export_contact is None:
            return
        target = self.export_contact()
        if target:
            self.share_status.setText(f"Contact exported to {target}")

    def _add_current_received(self):
        index = self.strip.currentRow()
        if self.add_received_card is None or not 0 <= index < len(self.events):
            return
        direction, card = self.events[index]
        if direction != 'received_cards':
            return
        result = self.add_received_card(card)
        if result:
            self.add_button.setText('Already in My Cards' if result == 'already' else 'Added to My Cards')
            self.add_button.setEnabled(False)

    def show_card(self, index):
        if not 0 <= index < len(self.events): return
        direction, card = self.events[index]; meta = card.get('card') or {}
        self.art.set_artwork(card_art(self.image_for_card(card), meta.get('callsign', 'Unknown'), stage_label(card), QSize(320, 400)))
        detail = ('Sent by you' if direction == 'sent_cards' else 'Received')
        detail += f" · {stage_label(card)}"
        if card.get('logged_at'):
            detail += f" · Recorded {card['logged_at']}"
        else:
            detail += " · Event time not recorded in this log"
        if meta.get('snr_db') is not None: detail += f"  ·  Report {meta['snr_db']:+d} dB"
        self.details.setText(detail)
        self.add_button.setVisible(direction == 'received_cards' and self.add_received_card is not None)
        self.add_button.setText('Add received card to My Cards')
        self.add_button.setEnabled(True)
        self.counter.setText(f'{index+1} / {len(self.events)}')
        self.previous.setEnabled(index > 0); self.next.setEnabled(index+1 < len(self.events))

"""Trading-card presentation for the local contact collection."""
from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QColor, QPainter, QPixmap, QIcon, QFont
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget, QListWidgetItem, QSizePolicy


def deck_events(entry):
    events = [(direction, card) for direction in ('sent_cards', 'received_cards') for card in entry.get(direction, [])]
    return sorted(events, key=lambda event: int(event[1].get('order', 0)))


def stage_label(card):
    meta = card.get('card') or {}
    kind = meta.get('message_type', 'card')
    return {'cq': 'CQ', 'exchange': 'EXCHANGE', '73': '73 · REPORT' if meta.get('snr_db') is not None else '73 · GOODBYE'}.get(kind, 'IMAGE CARD')


def card_art(image, title, subtitle='', size=QSize(240, 300), stacked=False):
    pixmap = QPixmap(size); pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size.width()/240, size.height()/300)
    width, height = 240, 300
    margin = 18 if stacked else 6
    if stacked:
        for offset in (12, 6):
            p.setPen(QColor('#4e6958')); p.setBrush(QColor('#28353c'))
            p.drawRoundedRect(QRectF(margin-offset/2, margin-offset, width-2*margin, height-2*margin), 13, 13)
    p.setPen(QColor('#688570')); p.setBrush(QColor('#1e292f'))
    p.drawRoundedRect(QRectF(margin, margin, width-2*margin, height-2*margin), 12, 12)
    art = QRectF(margin+12, margin+12, width-2*margin-24, height-2*margin-76)
    p.fillRect(art, QColor('#101b16'))
    if image is not None and not image.isNull():
        scaled = QPixmap.fromImage(image).scaled(art.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
        p.drawPixmap(int(art.center().x()-scaled.width()/2), int(art.center().y()-scaled.height()/2), scaled)
    p.setPen(QColor('#d9efc6')); font = QFont(); font.setPixelSize(max(13, width//17)); font.setBold(True); p.setFont(font)
    p.drawText(QRectF(margin+13, height-margin-57, width-2*margin-26, 24), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
    font.setBold(False); font.setPixelSize(max(11, width//22)); p.setFont(font); p.setPen(QColor('#9fafaa'))
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
    def __init__(self, entry, image_for_card, add_received_card=None, parent=None):
        super().__init__(parent)
        self.events = deck_events(entry); self.image_for_card = image_for_card
        self.add_received_card = add_received_card
        self.setWindowTitle(f"Contact with {entry.get('peer_callsign', 'Unknown')}")
        self.resize(780, 760)
        layout = QVBoxLayout(self); layout.setSpacing(12)
        heading = QLabel(entry.get('peer_callsign', 'Unknown')); heading.setStyleSheet('font-size:28px;font-weight:700;color:#c9e8a8'); layout.addWidget(heading)
        layout.addWidget(QLabel(f"{entry.get('peer_grid', '')}  ·  {entry.get('started_at', '')}  ·  {entry.get('status', 'Saved')}"))
        self.art = ArtworkView(); layout.addWidget(self.art, 1)
        self.details = QLabel(); self.details.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(self.details)
        row = QHBoxLayout(); self.previous = QPushButton('← Previous'); self.next = QPushButton('Next →')
        self.previous.clicked.connect(lambda: self.strip.setCurrentRow(self.strip.currentRow()-1))
        self.next.clicked.connect(lambda: self.strip.setCurrentRow(self.strip.currentRow()+1))
        self.previous.setShortcut('Left'); self.next.setShortcut('Right')
        row.addWidget(self.previous); self.counter = QLabel(); self.counter.setAlignment(Qt.AlignmentFlag.AlignCenter); row.addWidget(self.counter, 1); row.addWidget(self.next); layout.addLayout(row)
        self.strip = QListWidget(); self.strip.setViewMode(QListWidget.ViewMode.IconMode); self.strip.setMovement(QListWidget.Movement.Static)
        self.strip.setFlow(QListWidget.Flow.LeftToRight); self.strip.setWrapping(False)
        self.strip.setIconSize(QSize(80, 100)); self.strip.setGridSize(QSize(112, 135)); self.strip.setFixedHeight(155)
        for direction, card in self.events:
            label = stage_label(card)
            item = QListWidgetItem('Sent' if direction == 'sent_cards' else 'Received')
            item.setIcon(QIcon(card_art(image_for_card(card), label, size=QSize(80, 100))))
            item.setToolTip(label); self.strip.addItem(item)
        self.strip.currentRowChanged.connect(self.show_card); layout.addWidget(self.strip)
        actions = QHBoxLayout()
        self.add_button = QPushButton('Add received card to My Cards')
        self.add_button.clicked.connect(self._add_current_received)
        actions.addWidget(self.add_button)
        close = QPushButton('Close deck'); close.clicked.connect(self.accept); actions.addWidget(close)
        layout.addLayout(actions)
        self.strip.setCurrentRow(0)

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
        detail = 'Sent by you' if direction == 'sent_cards' else 'Received'
        if meta.get('snr_db') is not None: detail += f"  ·  Report {meta['snr_db']:+d} dB"
        self.details.setText(detail)
        self.add_button.setVisible(direction == 'received_cards' and self.add_received_card is not None)
        self.add_button.setText('Add received card to My Cards')
        self.add_button.setEnabled(True)
        self.counter.setText(f'{index+1} / {len(self.events)}')
        self.previous.setEnabled(index > 0); self.next.setEnabled(index+1 < len(self.events))

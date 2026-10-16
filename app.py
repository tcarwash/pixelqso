#!/usr/bin/env python3
"""Pixel QSO desktop application for editable, self-identifying image bursts."""
from __future__ import annotations

import json
import hashlib
import signal
import copy
import math
import os
import re
import subprocess
import sys
import threading
import tempfile
import time
import wave
from pathlib import Path

# Small DSP operations are slower when every receiver worker also starts a
# full BLAS thread team. Set this before NumPy loads its native libraries.
for _thread_env in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[_thread_env] = os.environ.get("PIXELQSO_DSP_THREADS", "1")

import numpy as np
from PySide6.QtCore import Qt, QRect, QSize, QObject, QTimer, QEvent, QRunnable, QThreadPool, Signal, QStandardPaths, QMimeData, QPoint, QUrl, QSortFilterProxyModel, QBuffer, QByteArray, QIODevice, QSettings, QSignalBlocker
from PySide6.QtGui import QAction, QColor, QPainter, QPen, QImage, QPixmap, QFont, QIcon, QKeySequence, QShortcut, QDrag, QStandardItem, QStandardItemModel
from PySide6.QtNetwork import QTcpSocket, QUdpSocket, QHostAddress
from PySide6.QtMultimedia import QAudio, QAudioDevice, QAudioFormat, QAudioSink, QAudioSource, QMediaDevices
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QListWidget, QListWidgetItem,
    QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QInputDialog,
    QMessageBox, QPushButton, QSpinBox, QTabWidget, QTextEdit, QDialog,
    QVBoxLayout, QWidget, QButtonGroup, QScrollArea, QTableView, QAbstractItemView, QFrame, QCheckBox, QGroupBox,
    QStackedWidget, QProgressBar, QMenu, QSizePolicy, QSplitter)

import cardmodem as modem
from card_ids import allocate_card_id
from card_animation import (artwork_digest, validate_link, partner as animation_partner,
                            details as animation_details)
from animation_editor import AnimationAuthor
from card_feedback import make_report as make_reception_report, report_lines as reception_report_lines
from transmit_preview import LocalTransmitPreview, Data2GTransmitPreview
from snr import set_measured_snr, snr_lines
from backend_adapters import (AdapterKind, DATA2G_ADAPTER, DecodeFailure,
                              DecodeFailureKind, DecodeOutcome,
                              EXPERIMENTAL_ADAPTER, ReceiveStartError,
                              adapter_for_backend_selection)
from card_backends import (BACKENDS, DEFAULT_MODE_KEY, LEGACY_MODE_KEY, MODE_REGISTRY, Data2GHostBackend,
                           TX_DISABLED_MODE_KEYS,
                           available_modes, get_backend,
                           data2g_mode_name, host_mode_backends, is_card_backend, is_experimental_mode,
                           is_data2g_mode_key, mode_supports_audio_placement,
                           preferred_data2g_mode_key, recommended_data2g_modes,
                           register_data2g_mode_backends, EXPERIMENTAL_MODE_DESCRIPTIONS)
from card_transfer import CardAssemblyService
from data2g_transport import Data2GMode
from data2g_runtime import (data2g_audio_device_selector,
                            data2g_host_audio_devices,
                            data2g_host_audio_selector_for_name, int_setting,
                            local_host_arguments, local_host_command,
                            validate_data2g_ports)
from on_air import SessionRecorder, card_reference, decoded_reference
from card_views import (DeckDialog, card_art, deck_events, deck_image,
                        incoming_card_text, single_card_image, AnimationDetailsDialog)
from webserver import CompanionServer
from ui_theme import (COLORS, SPACING, build_app_stylesheet, palette_swatch_style,
                      semantic_text_style, status_label_style)

APP_ICON_PATH = Path(__file__).resolve().with_name("icon.png")
STARTER_CARDS_PATH = Path(__file__).resolve().parent / "examples" / "starter-cards"
DEFAULT_STAMPS_PATH = Path(__file__).resolve().parent / "examples" / "stamps"
DATA2G_ACK_TIMEOUT_MS = 15_000
DATA2G_BUSY_ACK_TIMEOUT_MS = 5 * 60_000
DATA = Path.home() / ".local" / "share" / "PixelQSO"
AUTO_REPLY_GUARD_MS = 600
PALETTE = [(0, 0, 0), (15, 15, 15), (0, 0, 15), (0, 15, 15),
           (0, 15, 0), (15, 15, 0), (15, 0, 0), (15, 0, 15)]
PALETTE_NAMES = ("Black", "White", "Blue", "Cyan", "Green", "Yellow", "Red", "Magenta")
_BASE_APPLICATION_FONT = None


def normalize_card_palette(card):
    """Map imported arbitrary palettes onto the protocol's shared 8 colors."""
    if len(card.palette) in (8, 16, 32):
        return card
    source = []
    for color in card.palette:
        if len(color) != 3:
            return modem.Card(card.callsign, card.grid, list(PALETTE),
                              [0] * (card.width * card.height), card.card_id,
                              card.width, card.height, copy.deepcopy(card.animation))
        source.append(tuple(
            max(0, min(15, round(int(channel) / 17) if int(channel) > 15 else int(channel)))
            for channel in color))
    if not source:
        source = [(0, 0, 0)]
    mapped_pixels = []
    for index in card.pixels:
        rgb = source[int(index)]
        mapped_pixels.append(min(
            range(len(PALETTE)),
            key=lambda target: sum((rgb[k] - PALETTE[target][k]) ** 2 for k in range(3))))
    return modem.Card(card.callsign, card.grid, list(PALETTE), mapped_pixels,
                      card.card_id, card.width, card.height, copy.deepcopy(card.animation))

CW_MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".",
    "F": "..-.", "G": "--.", "H": "....", "I": "..", "J": ".---",
    "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---",
    "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-",
    "U": "..-", "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--",
    "Z": "--..", "0": "-----", "1": ".----", "2": "..---",
    "3": "...--", "4": "....-", "5": ".....", "6": "-....",
    "7": "--...", "8": "---..", "9": "----.", "/": "-..-.",
    "?": "..--..", ".": ".-.-.-", ",": "--..--", "=": "-...-",
    "+": ".-.-.", "-": "-....-",
}


def morse_audio(message, sample_rate=48_000, words_per_minute=20, tone_hz=700):
    """Render a short station ID as keyed audio for local modem transmission."""
    text = " ".join(str(message or "").upper().split())
    if not text:
        return np.zeros(0, dtype=np.float32)
    unsupported = sorted(set(text) - set(CW_MORSE) - {" "})
    if unsupported:
        raise ValueError("CW ID contains unsupported characters: " + " ".join(unsupported))
    dot = 1.2 / max(5, min(60, int(words_per_minute)))
    tone_parts = []
    silence_samples = lambda units: round(units * dot * sample_rate)
    for index, character in enumerate(text):
        if character == " ":
            continue
        pattern = CW_MORSE[character]
        for mark_index, mark in enumerate(pattern):
            count = max(1, round((dot if mark == "." else 3 * dot) * sample_rate))
            samples = np.arange(count, dtype=np.float32) / sample_rate
            envelope = np.ones(count, dtype=np.float32)
            ramp = min(count // 2, round(0.005 * sample_rate))
            if ramp:
                edge = np.linspace(0.0, 1.0, ramp, endpoint=False, dtype=np.float32)
                envelope[:ramp] = edge
                envelope[-ramp:] = edge[::-1]
            tone_parts.append(0.55 * envelope * np.sin(2 * np.pi * tone_hz * samples))
            if mark_index + 1 < len(pattern):
                tone_parts.append(np.zeros(silence_samples(1), dtype=np.float32))
        next_index = index + 1
        if next_index < len(text):
            gap = 7 if text[next_index] == " " else 3
        else:
            gap = 5
        tone_parts.append(np.zeros(silence_samples(gap), dtype=np.float32))
    return np.concatenate(tone_parts) if tone_parts else np.zeros(0, dtype=np.float32)


def pcm16_to_float32(samples):
    """Decode a private PCM16 snapshot using one float allocation."""
    audio=np.frombuffer(samples,dtype="<i2").astype(np.float32)
    audio *= np.float32(1.0/32768.0)
    return audio


def pcm16_audio_bytes(audio):
    """Convert modem audio to PCM without hard-clipping OFDM peaks."""
    samples = np.asarray(audio, dtype=np.float32)
    if not np.isfinite(samples).all():
        raise ValueError("Transmission audio contains non-finite samples")
    peak = float(np.max(np.abs(samples), initial=0.0))
    if peak > 0.90:
        samples = samples * (0.90 / peak)
    return np.rint(samples * 32767).clip(-32768, 32767).astype("<i2").tobytes()


def default_card(callsign="N0CALL", grid="AA00", *, card_id=None) -> modem.Card:
    pixels = [0] * 1024
    return modem.Card(callsign, grid, PALETTE.copy(), pixels,
                      allocate_card_id(DATA) if card_id is None else card_id)


def received_card_key(report):
    card = report.get("card") or {}
    # A retransmission remains the same card when its measured SNR or the
    # currently reconstructed pixel coverage changes.
    return (str(card.get("callsign", "")).upper(), card.get("card_id"),
            card.get("message_type", "card"))


def session_wall_card_key(report):
    """Stable identity for one card while provisional snapshots improve."""
    return received_card_key(report)


def same_received_card(left, right):
    """Match improving provisional snapshots without merging known new artwork."""
    if received_card_key(left) != received_card_key(right):
        return False
    a, b = left.get('card') or {}, right.get('card') or {}
    if (a.get('width', 32), a.get('height', 32)) != (b.get('width', 32), b.get('height', 32)):
        return False
    first = a.get('image_id') or a.get('image_crc32')
    second = b.get('image_id') or b.get('image_crc32')
    if first is not None and second is not None:
        return first == second
    if a.get('exact') and b.get('exact') and left.get('pixels') is not None and right.get('pixels') is not None:
        return left['pixels'] == right['pixels']
    return True


class ReceiveStatusLabel(QLabel):
    """Keep decoder progress text out of the surrounding layout's sizing."""

    def sizeHint(self):
        return QSize(0, self.fontMetrics().lineSpacing() * 3 + 4)

    def minimumSizeHint(self):
        return self.sizeHint()

    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)


class RigctlClient(QObject):
    """Small asynchronous client for a Hamlib rigctld or compatible daemon."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket = QTcpSocket(self)
        self.socket.connected.connect(self._connected)
        self.socket.disconnected.connect(self._disconnected)
        self.socket.readyRead.connect(self._read)
        self.socket.errorOccurred.connect(self._error)
        self.queue = []
        self.pending = None
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self._timeout)
        self.state_changed = None

    def connect_to(self, host: str, port: int, callback):
        self.state_changed = callback
        self.socket.abort()
        self.socket.connectToHost(host, port)

    def disconnect(self):
        self.socket.disconnectFromHost()

    def connected(self):
        return self.socket.state() == QTcpSocket.SocketState.ConnectedState

    def request(self, command: str, lines: int, callback):
        self.queue.append((command, lines, callback))
        self._next()

    def _connected(self):
        if self.state_changed: self.state_changed(True, "Connected to rig control service.")
        self._next()

    def _disconnected(self):
        self.timer.stop(); self.pending = None; self.queue.clear()
        if self.state_changed: self.state_changed(False, "Rig control disconnected.")

    def _error(self, _error):
        if self.state_changed: self.state_changed(False, self.socket.errorString())

    def _next(self):
        if self.pending or not self.queue or not self.connected(): return
        command, lines, callback = self.queue.pop(0)
        self.pending = [command, lines, callback, []]
        self.socket.write(("\\" + command + "\n").encode("ascii"))
        self.timer.start(3000)

    def _read(self):
        if not self.pending: return
        self.pending[3].extend(bytes(self.socket.readAll()).decode("utf-8", "replace").splitlines())
        values = self.pending[3]
        if values and values[-1].startswith("RPRT "):
            self._finish(values)
        elif len(values) >= self.pending[1]:
            self._finish(values[:self.pending[1]])

    def _finish(self, result):
        self.timer.stop()
        _command, _lines, callback, _received = self.pending
        self.pending = None
        callback(result)
        self._next()

    def _timeout(self):
        if self.pending:
            command, _lines, callback, received = self.pending
            self.pending = None
            callback(["ERROR: timed out"] if not received else received)
            self._next()


class DecodeSignals(QObject):
    finished = Signal(object)
    preview = Signal(object)
    progress = Signal(object)


class Data2GSignals(QObject):
    connect_finished = Signal(object, object, object, object)
    frame = Signal(object, int, object)
    ack = Signal(object, int, object)
    status = Signal(object, str)
    error = Signal(object, str)
    closed = Signal(object)


class Data2GConnectWorker(QRunnable):
    def __init__(self, session, signals, timeout=None):
        super().__init__()
        self.session, self.signals, self.timeout = session, signals, timeout
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()

    def run(self):
        try:
            modes, checked = DATA2G_ADAPTER.discover(self.session, self.timeout)
            if not self._cancelled.is_set():
                self.signals.connect_finished.emit(self.session, modes, None, checked)
        except Exception as exc:
            if not self._cancelled.is_set():
                self.signals.connect_finished.emit(self.session, None, str(exc), None)


class RxAudioWaterfall(QWidget):
    """Display recent spectra and levels from samples entering the decoder."""
    FFT_SIZE = 8192
    DISPLAY_HZ = 4000
    COLUMNS = 512
    ROWS = 128

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(112)
        self.setMaximumHeight(190)
        self.device_name = "Waiting for receive audio"
        self.sample_rate = modem.SAMPLE_RATE
        self._tail = np.empty(0, dtype=np.float32)
        self._window = np.hanning(self.FFT_SIZE).astype(np.float32)
        self._window_norm = 2.0 / float(np.sum(self._window))
        self._rows = np.zeros((self.ROWS, self.COLUMNS, 3), dtype=np.uint8)
        self._rows[:] = (8, 15, 20)
        self._level_text = "Waiting for samples"
        stops = np.asarray(((5, 12, 24), (12, 51, 81), (19, 137, 150),
                            (205, 205, 92), (255, 105, 48)), dtype=np.float32)
        positions = np.linspace(0, len(stops) - 1, 256)
        lo = np.floor(positions).astype(int)
        hi = np.minimum(lo + 1, len(stops) - 1)
        frac = (positions - lo)[:, None]
        self._palette = np.rint(stops[lo] * (1 - frac) + stops[hi] * frac).astype(np.uint8)

    def reset(self, device_name, sample_rate=modem.SAMPLE_RATE):
        self.device_name = str(device_name or "Audio input")
        self.sample_rate = int(sample_rate)
        self._tail = np.empty(0, dtype=np.float32)
        self._rows[:] = (8, 15, 20)
        self._level_text = "Waiting for samples"
        self.update()

    def feed_pcm16(self, data):
        if not data:
            return
        pcm = np.frombuffer(data, dtype="<i2")
        if not len(pcm):
            return
        recent = pcm[-max(1, self.sample_rate // 5):].astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(recent * recent)))
        peak = float(np.max(np.abs(recent)))
        clipped = float(np.mean(np.abs(recent) >= (32760.0 / 32768.0)))
        def dbfs(value):
            return "−∞" if value <= 1e-8 else f"{20 * math.log10(value):.1f}"
        self._level_text = (f"RMS {dbfs(rms)} dBFS   Peak {dbfs(peak)} dBFS   "
                            f"Clipped {100 * clipped:.2f}%")

        samples = pcm.astype(np.float32) / 32768.0
        frame = np.concatenate((self._tail, samples))
        if len(frame) < self.FFT_SIZE:
            self._tail = frame
            self.update()
            return
        frame = frame[-self.FFT_SIZE:]
        self._tail = frame
        spectrum = np.abs(np.fft.rfft(frame * self._window)) * self._window_norm
        limit = min(len(spectrum) - 1,
                    int(self.DISPLAY_HZ * self.FFT_SIZE / self.sample_rate))
        bins = np.linspace(0, limit, self.COLUMNS)
        amplitude = np.interp(bins, np.arange(limit + 1), spectrum[:limit + 1])
        db = 20 * np.log10(np.maximum(amplitude, 1e-8))
        indices = np.clip(np.rint((db + 100.0) * (255.0 / 80.0)), 0, 255).astype(int)
        self._rows[1:] = self._rows[:-1]
        self._rows[0] = self._palette[indices]
        self.update()

    def feed_float32(self, data):
        """Accept mono float audio mirrored from the Data2G host capture."""
        if not data:
            return
        samples = np.frombuffer(data, dtype="<f4")
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
        self.feed_pcm16(pcm.tobytes())

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS["waterfall_background"]))
        fm = painter.fontMetrics()
        name = fm.elidedText(self.device_name, Qt.TextElideMode.ElideMiddle,
                             max(100, self.width() - 300))
        painter.setPen(QColor(COLORS["waterfall_text"]))
        painter.drawText(8, 17, f"RX AUDIO  ·  {name}")
        painter.setPen(QColor(COLORS["waterfall_text_secondary"]))
        painter.drawText(max(8, self.width() - fm.horizontalAdvance(self._level_text) - 8),
                         17, self._level_text)

        graph = self.rect().adjusted(42, 24, -8, -22)
        image_data = self._rows.tobytes()
        image = QImage(image_data, self.COLUMNS, self.ROWS, self.COLUMNS * 3,
                       QImage.Format.Format_RGB888)
        painter.drawImage(graph, image)
        waterfall_grid = QColor(COLORS["waterfall_grid"])
        waterfall_grid.setAlpha(95)
        painter.setPen(QPen(waterfall_grid, 1))
        for khz in range(5):
            x = graph.left() + round(graph.width() * khz / 4)
            painter.drawLine(x, graph.top(), x, graph.bottom())
            painter.setPen(QColor(COLORS["waterfall_text_secondary"]))
            painter.drawText(x - 12, self.height() - 5, f"{khz}k")
            waterfall_grid = QColor(COLORS["waterfall_grid"])
            waterfall_grid.setAlpha(95)
            painter.setPen(QPen(waterfall_grid, 1))
        for db_label in (-20, -60, -100):
            y = graph.top() + round(graph.height() * (-20 - db_label) / 80)
            painter.drawLine(graph.left(), y, graph.right(), y)
        painter.setPen(QColor(COLORS["waterfall_text_secondary"]))
        painter.drawText(3, graph.top() + 4, "−20")
        painter.drawText(3, graph.center().y() + 4, "−60")
        painter.drawText(3, graph.bottom(), "−100")
        painter.end()


def order_receive_candidates(backends, preferred_key=None):
    """Try the user's selected modem first, then the earliest usable decoder."""
    ranked=[]
    for index,(key,backend) in enumerate(backends):
        if key == LEGACY_MODE_KEY and backend is None:
            minimum_seconds=1.0  # enough to inspect the legacy preamble
        else:
            try:
                minimum_seconds=float(backend.minimum_audio_seconds())
                if not np.isfinite(minimum_seconds) or minimum_seconds < 0:
                    minimum_seconds=float("inf")
            except (AttributeError,TypeError,ValueError,OverflowError):
                minimum_seconds=float("inf")
        ranked.append((key,backend,(key != preferred_key,minimum_seconds,index)))
    return [(key,backend) for key,backend,_ in sorted(ranked,key=lambda item:item[2])]


def newest_protected_acquisition(copies):
    """Keep copies for the newest announced content identity only.

    Older payloads remain useful while they are the newest candidate, but must
    not keep winning after a later card header has announced a new identity.
    """
    groups = {}
    for copy in copies or ():
        groups.setdefault((copy.mode, copy.identity), []).append(copy)
    if not groups:
        return [], None
    def acquisition_time(copy):
        return int(copy.diagnostics.get("acquisition_start_sample", copy.start))
    newest_key, newest_group = max(groups.items(),
                                   key=lambda item: max(acquisition_time(c) for c in item[1]))
    return newest_group, newest_key[0]


def _receive_report_rank(report):
    """Rank reports monotonically: whole-card proof, checked pixels, candidates."""
    card = report.get("card") or {}
    coverage = card.get("pixel_coverage") or []
    verified = card.get("verified_pixel_coverage") or []
    diagnostics = card.get("diagnostics") or {}
    revisions = diagnostics.get("copy_revisions") or ()
    evidence_samples = sum(int(item[-1]) for item in revisions if item)
    copy_count = max(int(card.get("received_copies", 0) or 0), len(revisions))
    if card.get("exact"):
        return (3, sum(bool(value) for value in coverage), sum(bool(v) for v in verified),
                evidence_samples, copy_count)
    blocks = card.get("received_blocks") or []
    verified_count = max(sum(bool(value) for value in verified),
                         sum(bool(value) for value in blocks))
    return (2 if verified_count else 1,
            verified_count if verified_count else sum(bool(value) for value in coverage),
            evidence_samples, copy_count)


def decode_all_card_backends(audio, sample_rate, audio_placement="near_carrier",
                             preferred_mode=None, include_experimental=False,
                             on_preview=None, copy_cache=None, capture_start_sample=0, announced_copies=None,
                             live_snapshot=False, on_mode_attempt=None, capture_session_id=0):
    """Try the selected format first, then fall back to all installed formats."""
    # A verified common header routes RX before costly native decoder trials.
    # An unannounced historical capture retains the blind receive-all path.
    copies = announced_copies
    if include_experimental:
        from modem_envelope import find_copies, decode_capture
        copies = find_copies(audio, sample_rate) if announced_copies is None else announced_copies
        if copy_cache is not None:
            copy_cache.update(audio,sample_rate,copies,capture_start_sample)
            cached_audio,cached_copies=copy_cache.materialize()
            if cached_copies and any(copy.diagnostics.get('acquisition_start_sample', 0) >= 0
                                     for copy in cached_copies):
                audio,sample_rate,copies=cached_audio,8000,cached_copies
        if copies:
            for copy in copies:
                copy.diagnostics["receive_session_id"] = capture_session_id
                copy.diagnostics["acquisition_start_sample"] = int(
                    copy.diagnostics.get("acquisition_start_sample",
                        round((capture_start_sample + copy.start) * 8000 / sample_rate)))
                copy.diagnostics["acquisition_id"] = (
                    capture_session_id, copy.diagnostics["acquisition_start_sample"],
                    copy.mode, copy.identity)
            copies, newest_mode = newest_protected_acquisition(copies)
            preview, failure = None, None

            def consider_preview(report, fallback_start=None):
                nonlocal preview
                is_activity = bool(report.get('receive_activity'))
                previous_activity = bool(preview and preview.get('receive_activity'))
                if (preview is None or (previous_activity and not is_activity) or
                        (not is_activity and preview is not None and not previous_activity and
                         _receive_report_rank(report) > _receive_report_rank(preview))):
                    preview = report
                    if on_preview is not None:
                        on_preview(report)

            for key in dict.fromkeys(copy.mode for copy in copies):
                if on_mode_attempt is not None:
                    on_mode_attempt(key)
                if key == LEGACY_MODE_KEY:
                    legacy_copies=[copy for copy in copies if copy.mode==key]
                    outcome=local_adapter_for_mode(key).decode_legacy_audio(
                        audio,sample_rate,copies=legacy_copies)
                    if outcome.failure:
                        failure=outcome.failure
                        continue
                    report=outcome.report
                    latest_start = max(int(copy.diagnostics.get(
                        "acquisition_start_sample", round(copy.start * 8000 / sample_rate)))
                        for copy in legacy_copies)
                    report.setdefault('copy_header', {}).update(
                        acquisition_start_sample=latest_start,
                        receive_session_id=capture_session_id)
                    consider_preview(report, latest_start)
                    continue
                if key not in BACKENDS:
                    continue
                placements = (list(dict.fromkeys((audio_placement, 'near_carrier', 'centered')))
                              if mode_supports_audio_placement(key) else [audio_placement])
                if key == preferred_mode and preferred_mode in BACKENDS:
                    placements.sort(key=lambda placement: placement != audio_placement)
                for placement in placements:
                    backend = get_backend(key, placement)
                    try:
                        report = decode_capture(key, audio, sample_rate,
                            lambda x, rate: backend.decode_payload(x, rate), copies=copies,
                            copy_decoder=(lambda xs, rate: backend.decode_payload_copies(xs, rate))
                            if hasattr(backend, 'decode_payload_copies') else None,
                            announced_decoder=getattr(backend,'decode_announced_copies',None))
                        consider_preview(report)
                    except ValueError as exc:
                        failure = DecodeFailure(EXPERIMENTAL_ADAPTER.key, DecodeFailureKind.NO_MATCH, str(exc), True)
            if preview is not None:
                return DecodeOutcome(report=preview)
            if live_snapshot:
                diagnostic = {**copies[0].diagnostics, "announced_mode": newest_mode,
                              "content_id": copies[0].identity, "announced_copies": len(copies)}
                metadata = {}
                set_measured_snr(metadata, diagnostic.get("estimated_snr_db_2500"),
                                 "common_header_training")
                return DecodeOutcome(report={
                    "card": metadata, "copy_header": diagnostic,
                    "receive_activity": "New protected transmission detected · waiting for card data",
                    "receive_profile": newest_mode,
                    "pivot_receive": True,
                    "receive_identity": (capture_session_id, copies[0].mode, copies[0].identity),
                    "receive_session_id": capture_session_id,
                    "acquisition_start_sample": max(int(copy.diagnostics.get(
                        "acquisition_start_sample", round(copy.start * 8000 / sample_rate)))
                        for copy in copies),
                })
            return DecodeOutcome(failure=failure or DecodeFailure(
                EXPERIMENTAL_ADAPTER.key, DecodeFailureKind.NO_MATCH,
                'Announced payload is incomplete', True))
        # No announced header can still be a valid legacy/native waveform.
        # Continue to bounded blind decoders instead of ending live RX here.
    preview = None
    last_failure = None
    backends = available_modes(include_experimental=include_experimental)
    backends = [(key, backend) for key, backend in backends
                if MODE_REGISTRY.get(key) is not None and
                MODE_REGISTRY[key].adapter != DATA2G_ADAPTER.key]
    preferred_key = {"fast_avatar": "fast_avatar_fec",
                     "experimental_qpsk_combined": "experimental_qpsk_5s"}.get(
                         preferred_mode, preferred_mode)
    backends = order_receive_candidates(backends, preferred_key)
    if live_snapshot and any(key == preferred_key for key, _ in backends):
        # Protected headers above still discover every current modem. Without
        # one, keep legacy/native probing to the selected mode and a bounded
        # recent window so blind searches cannot stall the next header scan.
        backends = [(key, backend) for key, backend in backends if key == preferred_key]
        backend = backends[0][1]
        lookback = max(12.0, min(60.0, backend.estimate_seconds(modem.example_card(), 1) + 4.0))
        audio = audio[-round(lookback * sample_rate):]
    for key, backend in backends:
        if getattr(backend, "requires_common_header", False):
            continue  # Header-routed group decoders have no blind native format.
        if key in {"fast_avatar", "experimental_qpsk_combined"}:
            continue  # The resilient avatar and single QPSK decoders accept either wire variant.
        if len(audio) < backend.minimum_audio_seconds() * sample_rate:
            continue
        placements = (list(dict.fromkeys((audio_placement, "near_carrier", "centered")))
                      if mode_supports_audio_placement(key) else [audio_placement])
        for placement in placements:
            if on_mode_attempt is not None:
                on_mode_attempt(key)
            outcome = local_adapter_for_mode(key).decode_capture(
                get_backend(key, placement), audio, sample_rate, copies=copies or [])
            if outcome.failure:
                if outcome.failure.kind is DecodeFailureKind.NO_MATCH:
                    last_failure = outcome.failure
                    continue
                return outcome
            report = outcome.report
            if report["card"].get("exact"):
                return DecodeOutcome(report=report)
            if preview is None or (preview.get("receive_activity") and not report.get("receive_activity")):
                preview = report
                if on_preview is not None:
                    on_preview(report)
    if preview is not None:
        return DecodeOutcome(report=preview)
    return DecodeOutcome(failure=last_failure or DecodeFailure(
        EXPERIMENTAL_ADAPTER.key, DecodeFailureKind.NO_MATCH,
        "No matching card frame received yet", True))


def decode_avatar_report(audio, sample_rate, mode="fast_avatar_fec"):
    return local_adapter_for_mode(mode).decode_capture(get_backend(mode), audio, sample_rate)


def local_adapter_for_mode(mode):
    """Resolve decoder ownership from the registered mode contract."""
    spec = MODE_REGISTRY.get(mode)
    return adapter_for_backend_selection("experimental", spec.adapter if spec else None)


class DecodeWorker(QRunnable):
    def __init__(self, path: Path, state_path: Path, profile="auto", mode="standard",
                 audio_placement="near_carrier", preferred_mode=None,
                 include_experimental=False):
        super().__init__()
        self.path = path
        self.state_path = state_path
        self.profile = profile
        self.mode = mode
        self.audio_placement = audio_placement
        self.preferred_mode = preferred_mode
        self.include_experimental = include_experimental
        self.signals = DecodeSignals()

    def run(self):
        try:
            if self.mode == LEGACY_MODE_KEY:
                outcome = local_adapter_for_mode(self.mode).decode_legacy_wav(
                    self.path, self.state_path, self.profile)
                self.signals.finished.emit((str(self.path), outcome.report, outcome.failure))
                return
            audio, sr = modem.read_wav(self.path)
            if self.mode == "auto":
                outcome = decode_all_card_backends(audio, sr, self.audio_placement,
                                                   self.preferred_mode, self.include_experimental)
                if (outcome.failure and
                        outcome.failure.kind is DecodeFailureKind.NO_MATCH):
                    self.signals.finished.emit((str(self.path), None, outcome.failure))
                    return
                if outcome.failure:
                    self.signals.finished.emit((str(self.path), None, outcome.failure))
                    return
                if outcome.report.get("receive_activity"):
                    raise ValueError("Synchronization found, but the capture has no decoded card data yet")
                self.signals.finished.emit((str(self.path), outcome.report, None))
                return
            if is_card_backend(self.mode):
                outcome = local_adapter_for_mode(self.mode).decode_capture(
                    get_backend(self.mode, self.audio_placement), audio, sr)
                if outcome.report and outcome.report.get("receive_activity"):
                    raise ValueError("Synchronization found, but the capture has no decoded card data yet")
                self.signals.finished.emit((str(self.path), outcome.report, outcome.failure))
                return
            raise ValueError(f"No decoder is registered for mode {self.mode!r}")
        except Exception as exc:
            self.signals.finished.emit((str(self.path), None, str(exc)))


class LiveDecodeWorker(QRunnable):
    """Decode a snapshot of the in-memory receive buffer while it is recording."""
    def __init__(self, samples: bytes, sample_rate: int, profile: str, generation: int,
                 mode="standard", audio_placement="near_carrier", preferred_mode=None,
                 include_experimental=False, copy_cache=None, capture_start_sample=0,
                 capture_start_utc=None, receive_session_id=0):
        super().__init__()
        self.copy_cache=copy_cache
        self.capture_start_sample=capture_start_sample
        self.capture_start_utc=capture_start_utc
        self.receive_session_id=receive_session_id
        self.samples = samples
        self.sample_rate = sample_rate
        self.profile = profile
        self.generation = generation
        self.mode = mode
        self.audio_placement = audio_placement
        self.preferred_mode = preferred_mode
        self.include_experimental = include_experimental
        self.signals = DecodeSignals()

    def _finish(self, report, complete, error, started):
        stats = {"mode": self.mode, "preferred_mode": self.preferred_mode,
                 "audio_seconds": len(self.samples) / (2 * self.sample_rate),
                 "decode_seconds": time.perf_counter() - started}
        self.signals.finished.emit((self.generation, report, complete, error, stats))

    def run(self):
        started = time.perf_counter()
        try:
            audio = pcm16_to_float32(self.samples)
            announced = None
            if (self.copy_cache is not None and
                    (self.mode == "auto" or self.include_experimental or self.copy_cache.entries)):
                from modem_envelope import find_copies
                ranges=self.copy_cache.header_scan_ranges(
                    self.capture_start_sample,len(audio),self.sample_rate)
                announced=[]
                for scan_start,scan_end in ranges:
                    scan_utc=(None if self.capture_start_utc is None else
                              self.capture_start_utc+scan_start/self.sample_rate)
                    found=find_copies(audio[scan_start:scan_end],self.sample_rate,
                                      capture_start_utc=scan_utc)
                    with self.copy_cache.lock:
                        for digest, link in getattr(found, 'animation_links', ()):
                            self.copy_cache.animation_links.add(digest, link)
                        for part in getattr(found, 'feedback_parts', ()):
                            self.copy_cache.feedback.add_part(*part)
                    for copy in found:
                        copy.start+=scan_start
                        copy.end+=scan_start
                        copy.diagnostics["acquisition_start_sample"] = round(
                            (self.capture_start_sample + copy.start) * 8000 / self.sample_rate)
                        copy.diagnostics["receive_session_id"] = self.receive_session_id
                        copy.diagnostics["acquisition_id"] = (
                            self.receive_session_id,
                            copy.diagnostics["acquisition_start_sample"], copy.mode, copy.identity)
                    announced.extend(found)
            if (self.mode == "auto" or announced or
                    (self.copy_cache is not None and (self.include_experimental or self.copy_cache.entries))):
                outcome = decode_all_card_backends(audio, self.sample_rate, self.audio_placement,
                                                   self.preferred_mode, self.include_experimental or self.mode == "auto",
                                                   on_preview=lambda report: self.signals.preview.emit(
                                                       (self.generation, report)),
                                                   on_mode_attempt=lambda mode: self.signals.progress.emit(
                                                       (self.generation, mode)),
                                                   copy_cache=self.copy_cache,
                                                   capture_start_sample=self.capture_start_sample, announced_copies=announced,
                                                   live_snapshot=True,
                                                   capture_session_id=self.receive_session_id)
                if outcome.failure:
                    self._finish(None, False, outcome.failure, started)
                    return
                result = outcome.report
                result.setdefault("valid_packet_count", 0)
                if result.get("receive_activity"):
                    self._finish(result, False, None, started)
                    return
                if not isinstance(result.get("card"), dict):
                    self._finish(None, False, DecodeFailure(
                        EXPERIMENTAL_ADAPTER.key, DecodeFailureKind.BACKEND_ERROR,
                        "Decoder returned neither card data nor receive activity", False), started)
                    return
                self._finish(result, bool(result["card"].get("exact")), None, started)
                return
            if self.mode == LEGACY_MODE_KEY:
                outcome = local_adapter_for_mode(self.mode).decode_legacy_audio(
                    audio, self.sample_rate, self.profile)
                if outcome.failure:
                    self._finish(None, False, outcome.failure, started)
                    return
                result = outcome.report
                self._finish(result, bool(result.get("complete")), None, started)
                return
            if is_card_backend(self.mode):
                outcome = local_adapter_for_mode(self.mode).decode_capture(
                    get_backend(self.mode, self.audio_placement), audio, self.sample_rate)
                if outcome.failure:
                    self._finish(None, False, outcome.failure, started)
                    return
                result = outcome.report
                card = result["card"]
                result.setdefault("valid_packet_count", 0)
                result.setdefault("fresh_packets", [])
                self._finish(result, bool(card.get("exact")), None, started)
                return
            self._finish(None, False, DecodeFailure(
                EXPERIMENTAL_ADAPTER.key, DecodeFailureKind.NO_MATCH,
                f"No decoder is registered for mode {self.mode!r}", False), started)
        except Exception as exc:
            self._finish(None, False, str(exc), started)


class StampShelf(QListWidget):
    """Stamp list that can drag saved PNG artwork onto the card canvas."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_position = None

    def mousePressEvent(self, event):
        self._press_position = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton) or self._press_position is None:
            return super().mouseMoveEvent(event)
        if (event.position().toPoint()-self._press_position).manhattanLength() < QApplication.startDragDistance():
            return super().mouseMoveEvent(event)
        item = self.itemAt(self._press_position)
        if item is None:
            return super().mouseMoveEvent(event)
        self.setCurrentItem(item)
        mime = QMimeData(); mime.setUrls([QUrl.fromLocalFile(item.data(Qt.ItemDataRole.UserRole))])
        drag = QDrag(self); drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class CardCanvas(QWidget):
    TOOL_HELP = {
        "select": "Drag a box around pixels. Drag the box to move it, drag its corner to resize, or save it to My stamps.",
        "draw": "Draw with the selected palette color; right-click erases.",
        "erase": "Erase pixels to transparency.",
        "fill": "Fill a connected area with the selected palette color.",
        "pick": "Pick the palette color from a pixel.",
        "move": ("Select a placed stamp with Space or Enter at the cursor, then move it "
                 "one pixel at a time with the arrow keys; Escape deselects it. You can "
                 "also drag a stamp with the mouse and drag its lower-right handle to resize."),
    }
    changed = Signal()
    image_dropped = Signal(QImage)
    placed_stamp_removed = Signal()
    def __init__(self, card: modem.Card):
        super().__init__()
        self.card = card
        self.color_index = 1
        self.tool = "draw"
        self.keyboard_cursor = (0, 0)
        self.show_grid = True
        self.onion_image = None
        self.onion_opacity = 0.3
        self.last_paint_pixel = None
        self.keyboard_space_down = False
        self.keyboard_stroke_changed = False
        self.keyboard_count = ""
        self.keyboard_g_pending = False
        self.keyboard_command = None
        self.undo_stack = []
        self.redo_stack = []
        self.base_pixels = list(card.pixels)
        self.stamp_layers = []
        self.selection_rect = QRect()
        self.selection_handle = QRect()
        self.selection_origin = None
        self.selected_layer = None
        self.hover_layer = None
        self.hover_remove_rect = QRect()
        self.hover_convert_rect = QRect()
        self.hover_resize_rect = QRect()
        self.drag_layer = None
        self.drag_offset = (0, 0)
        self.resize_layer = None
        self.resize_origin_point = None
        self.resize_original = None
        self.setAcceptDrops(True)
        self.setMinimumSize(320, 320)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self.setAccessibleName("Card drawing canvas")
        self.update_accessible_description()

    def update_accessible_description(self):
        action = self.TOOL_HELP.get(self.tool, "Edit the card artwork.")
        x, y = self.keyboard_cursor
        if self.tool == "move" and self.selected_layer is not None:
            action += " Arrow keys move the selected stamp one pixel; Escape deselects it."
        self.setAccessibleDescription(
            f"{self.card.width} by {self.card.height} pixel card. {action} "
            f"Keyboard cursor at column {x + 1}, row {y + 1}; selected palette color "
            f"{self.color_index + 1}. Tab to focus, use arrows or hjkl to move. Hold "
            "Space or i while moving to draw a stroke; Escape cancels i mode. Number "
            "counts repeat movement; 0/A jump to row start, $/a to row end, gg/G to "
            "column ends. :wq saves and closes; :q closes with the usual unsaved-change prompt. "
            "n/N cycles colors forward/backward, "
            "f fills the selection or connected region, u undoes, U redoes, and x deletes "
            "the pixel under the cursor. Enter applies "
            "the selected tool.")

    def sizeHint(self):
        return QSize(640, 640)

    def card_position(self, point):
        side = min(self.width(), self.height())
        cell = side / max(self.card.width, self.card.height)
        drawn_width, drawn_height = cell*self.card.width, cell*self.card.height
        ox, oy = (self.width()-drawn_width)/2, (self.height()-drawn_height)/2
        x, y = math.floor((point.x()-ox)/cell), math.floor((point.y()-oy)/cell)
        return (x, y) if 0 <= x < self.card.width and 0 <= y < self.card.height else (None, None)

    def pixel_position(self, x, y):
        side = min(self.width(), self.height())
        cell = side / max(self.card.width, self.card.height)
        ox = (self.width()-cell*self.card.width)/2
        oy = (self.height()-cell*self.card.height)/2
        return QPoint(int(ox+x*cell+cell/2), int(oy+y*cell+cell/2))

    def _update_hover_remove_rect(self):
        if self.hover_layer is None and self.selected_layer is not None:
            self.hover_layer = self.selected_layer
        if self.hover_layer is None or self.hover_layer >= len(self.stamp_layers):
            self.hover_remove_rect = QRect()
            self.hover_convert_rect = QRect()
            self.hover_resize_rect = QRect()
            return
        side = min(self.width(), self.height())
        cell = side / max(self.card.width, self.card.height)
        drawn_width, drawn_height = cell*self.card.width, cell*self.card.height
        ox, oy = (self.width()-drawn_width)/2, (self.height()-drawn_height)/2
        layer = self.stamp_layers[self.hover_layer]
        stamp_right = int(ox + (layer["x"] + layer["width"]) * cell)
        stamp_top = int(oy + layer["y"] * cell)
        button_size = max(20, min(30, int(cell * 0.9)))
        control_right = min(self.width(), max(2 * button_size + 4, stamp_right))
        control_top = min(max(0, stamp_top), max(0, self.height() - button_size))
        self.hover_remove_rect = QRect(control_right-button_size, control_top, button_size, button_size)
        self.hover_convert_rect = QRect(control_right-2*button_size-4, control_top, button_size, button_size)
        resize_size = max(14, min(20, int(cell * 0.75)))
        stamp_bottom = int(oy + (layer["y"] + layer["height"]) * cell)
        resize_rect = QRect(stamp_right-resize_size, stamp_bottom-resize_size,
                            resize_size, resize_size)
        self.hover_resize_rect = (QRect() if resize_rect.intersects(self.hover_remove_rect)
                                  else resize_rect)

    def paintEvent(self, _event):
        p = QPainter(self)
        side = min(self.width(), self.height())
        cell = side / max(self.card.width, self.card.height)
        ox = (self.width()-cell*self.card.width)/2
        oy = (self.height()-cell*self.card.height)/2
        for y in range(self.card.height):
            for x in range(self.card.width):
                rgb = self.card.palette[self.card.pixels[y * self.card.width + x]]
                p.fillRect(QRect(int(ox+x*cell), int(oy+y*cell), int(cell+1), int(cell+1)),
                           QColor(*(v * 17 for v in rgb)))
        if self.onion_image is not None:
            p.save()
            p.setOpacity(self.onion_opacity)
            p.drawImage(QRect(round(ox), round(oy), round(cell*self.card.width), round(cell*self.card.height)),
                        self.onion_image)
            p.restore()
        p.setPen(QPen(QColor(COLORS["editor_grid"]), 0.5))
        for x in range(self.card.width+1) if self.show_grid else ():
            p.drawLine(int(ox+x*cell), int(oy), int(ox+x*cell), int(oy+cell*self.card.height))
        for y in range(self.card.height+1) if self.show_grid else ():
            p.drawLine(int(ox), int(oy+y*cell), int(ox+cell*self.card.width), int(oy+y*cell))
        if self.selected_layer is not None and self.selected_layer < len(self.stamp_layers):
            layer = self.stamp_layers[self.selected_layer]
            p.setPen(QPen(QColor(COLORS["editor_selection"]), 2))
            p.drawRect(QRect(int(ox+layer['x']*cell), int(oy+layer['y']*cell),
                             int(layer['width']*cell), int(layer['height']*cell)))
        self._update_hover_remove_rect()
        if self.hover_layer is not None and self.hover_layer < len(self.stamp_layers):
            layer = self.stamp_layers[self.hover_layer]
            p.setPen(QPen(QColor("#8cc8ff"), 1))
            p.setBrush(QColor("#174b75"))
            p.drawEllipse(self.hover_convert_rect)
            p.setPen(QPen(QColor("white"), 1))
            icon = self.hover_convert_rect.adjusted(6, 6, -6, -6)
            for row in range(2):
                for col in range(2):
                    p.drawRect(icon.left()+col*max(4, icon.width()//2),
                               icon.top()+row*max(4, icon.height()//2), 3, 3)
            p.setPen(QPen(QColor(COLORS["stamp_remove_border"]), 1))
            remove_background = QColor(COLORS["stamp_remove_background"])
            remove_background.setAlpha(245)
            p.setBrush(remove_background)
            p.drawEllipse(self.hover_remove_rect)
            inset = max(6, self.hover_remove_rect.width() // 3)
            left = self.hover_remove_rect.left() + inset
            right = self.hover_remove_rect.right() - inset
            top = self.hover_remove_rect.top() + inset
            bottom = self.hover_remove_rect.bottom() - inset
            p.setPen(QPen(QColor(COLORS["stamp_control_glyph"]), max(2, self.hover_remove_rect.width() // 9),
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(left, top, right, bottom)
            p.drawLine(right, top, left, bottom)
            if not self.hover_resize_rect.isNull():
                p.setPen(QPen(QColor(COLORS["stamp_resize_border"]), 1))
                resize_background = QColor(COLORS["stamp_resize_background"])
                resize_background.setAlpha(245)
                p.setBrush(resize_background)
                p.drawRect(self.hover_resize_rect)
                inset = max(3, self.hover_resize_rect.width() // 4)
                p.setPen(QPen(QColor(COLORS["stamp_resize_glyph"]), 2))
                p.drawLine(self.hover_resize_rect.left()+inset,
                           self.hover_resize_rect.bottom()-inset,
                           self.hover_resize_rect.right()-inset,
                           self.hover_resize_rect.top()+inset)
        else:
            self.hover_remove_rect = QRect()
            self.hover_convert_rect = QRect()
            self.hover_resize_rect = QRect()
        if not self.selection_rect.isNull():
            box = self.selection_rect
            screen = QRect(round(ox+box.x()*cell), round(oy+box.y()*cell),
                           round(box.width()*cell), round(box.height()*cell))
            p.setPen(QPen(QColor("#8cc8ff"), 2, Qt.PenStyle.DashLine)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(screen.adjusted(1, 1, -1, -1))
            self.selection_handle = QRect(screen.right()-14, screen.bottom()-14, 14, 14)
            p.fillRect(self.selection_handle, QColor("#174b75"))
        if self.hasFocus():
            x, y = self.keyboard_cursor
            cursor_rect = QRect(int(ox+x*cell), int(oy+y*cell),
                                max(1, int(cell)), max(1, int(cell)))
            cursor_color = QColor(*(v * 17 for v in self.card.palette[self.color_index]))
            p.setPen(QPen(cursor_color, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(cursor_rect.adjusted(1, 1, -1, -1))
            p.setPen(QPen(QColor(COLORS["window"]), 1))
            p.drawRect(cursor_rect.adjusted(2, 2, -2, -2))

    def keyPressEvent(self, event):
        key_text = event.text().lower()
        if self.keyboard_command is not None:
            if event.key() == Qt.Key.Key_Escape:
                self.keyboard_command = None
            elif event.key() == Qt.Key.Key_Backspace:
                self.keyboard_command = self.keyboard_command[:-1]
            elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                command = self.keyboard_command
                self.keyboard_command = None
                owner = self.window().parent()
                if command == "wq" and hasattr(owner, "save_and_close_editor"):
                    owner.save_and_close_editor()
                elif command == "q" and hasattr(self.window(), "reject"):
                    self.window().reject()
            elif event.text() and event.text().isprintable():
                self.keyboard_command += event.text()
            event.accept(); return
        if event.text() == ":" and not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                                                  Qt.KeyboardModifier.AltModifier |
                                                                  Qt.KeyboardModifier.MetaModifier)):
            self.keyboard_command = ""
            event.accept(); return
        if key_text == "u" and not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                                           Qt.KeyboardModifier.AltModifier |
                                                           Qt.KeyboardModifier.MetaModifier)):
            (self.undo if event.text() == "u" else self.redo)()
            event.accept(); return
        if key_text == "x" and not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                                           Qt.KeyboardModifier.AltModifier |
                                                           Qt.KeyboardModifier.MetaModifier)):
            self.delete_selection_or_pixel()
            event.accept(); return
        if key_text == "n" and not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                                           Qt.KeyboardModifier.AltModifier |
                                                           Qt.KeyboardModifier.MetaModifier)):
            direction = -1 if event.text() == "N" else 1
            self.color_index = (self.color_index + direction) % len(self.card.palette)
            self.update_accessible_description()
            self.update()
            self.changed.emit()
            event.accept(); return
        if key_text == "f" and not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                                           Qt.KeyboardModifier.AltModifier |
                                                           Qt.KeyboardModifier.MetaModifier)):
            self.fill_selection_or_region()
            event.accept(); return
        if key_text.isdigit() and not event.modifiers() and len(key_text) == 1:
            if self.keyboard_count or key_text != "0":
                self.keyboard_count = (self.keyboard_count + key_text)[-3:]
                event.accept(); return
        if key_text == "g" and not event.modifiers():
            if event.text() == "G":
                self.keyboard_g_pending = False
            elif self.keyboard_g_pending:
                self.keyboard_g_pending = False
                self._move_keyboard_steps(0, -self.keyboard_cursor[1])
                self.keyboard_count = ""
                event.accept(); return
            else:
                self.keyboard_g_pending = True
                event.accept(); return
        if self.keyboard_g_pending:
            self.keyboard_g_pending = False
        if key_text == "i" and not event.modifiers():
            self.keyboard_count = ""
            self.keyboard_space_down = not self.keyboard_space_down
            self.keyboard_stroke_changed = False
            if self.keyboard_space_down and self.tool == "draw":
                self._paint_keyboard_pixel(remember=True)
            event.accept(); return
        if event.key() == Qt.Key.Key_Space and self.tool == "draw":
            if not self.keyboard_space_down:
                self.keyboard_space_down = True
                self.keyboard_stroke_changed = False
                self._paint_keyboard_pixel(remember=True)
            event.accept(); return
        vim_directions = {"h": (-1, 0), "j": (0, 1), "k": (0, -1), "l": (1, 0)}
        if event.text() in ("H", "J", "K", "L") and not self.selection_rect.isNull():
            dx, dy = vim_directions[key_text]
            count = self._take_keyboard_count()
            self.move_selection(dx * count, dy * count)
            event.accept(); return
        if key_text in vim_directions:
            dx, dy = vim_directions[key_text]
            count = self._take_keyboard_count()
            self._move_keyboard_steps(dx, dy, count)
            event.accept()
            return
        if key_text in ("0", "$", "a", "g") or event.key() == Qt.Key.Key_G:
            count = self._take_keyboard_count()
            destination_x = (0 if key_text == "0" or event.text() == "A" else
                             self.card.width - 1 if key_text in ("$", "a") else self.keyboard_cursor[0])
            destination_y = self.card.height - 1 if event.key() == Qt.Key.Key_G else self.keyboard_cursor[1]
            self._move_keyboard_steps(destination_x-self.keyboard_cursor[0], destination_y-self.keyboard_cursor[1], count or 1)
            event.accept(); return
        if key_text == "v":
            if self.selection_origin is None:
                x, y = self.keyboard_cursor
                self.selection_origin = (x, y)
                self.selection_rect = QRect(x, y, 1, 1)
            else:
                self.selection_origin = None
            self.changed.emit(); self.update(); event.accept(); return
        if key_text == "y" and not self.selection_rect.isNull():
            self.copy_selection_to_clipboard()
            event.accept(); return
        if key_text == "d":
            self.delete_selection_or_pixel(copy_to_clipboard=True)
            event.accept(); return
        if key_text == "p":
            image = QApplication.clipboard().image()
            if not image.isNull():
                if image.width() > self.card.width or image.height() > self.card.height:
                    image = image.scaled(self.card.width, self.card.height,
                                         Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.FastTransformation)
                self.add_stamp(image)
                layer = self.stamp_layers[self.selected_layer]
                x, y = self.keyboard_cursor
                layer["x"] = min(x, self.card.width-layer["width"])
                layer["y"] = min(y, self.card.height-layer["height"])
                self._render_layers()
            event.accept(); return
        directions = {
            Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1),
        }
        if event.key() in directions:
            dx, dy = directions[event.key()]
            count = self._take_keyboard_count()
            if self.tool == "move" and self.selected_layer is not None:
                layer = self.stamp_layers[self.selected_layer]
                new_x = min(max(0, layer["x"] + dx * count), self.card.width - layer["width"])
                new_y = min(max(0, layer["y"] + dy * count), self.card.height - layer["height"])
                if (new_x, new_y) != (layer["x"], layer["y"]):
                    self.remember_edit()
                    layer["x"], layer["y"] = new_x, new_y
                    self.keyboard_cursor = (new_x, new_y)
                    self._render_layers()
                self.update_accessible_description()
                event.accept()
                return
            self._move_keyboard_steps(dx, dy, count)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and not self.selection_rect.isNull():
            self.keyboard_space_down = False; self.keyboard_count = ""; self.keyboard_g_pending = False
            self.selection_rect = QRect(); self.selection_handle = QRect(); self.selection_origin = None
            self.changed.emit(); self.update(); event.accept(); return
        if event.key() == Qt.Key.Key_Escape and self.keyboard_space_down:
            self.keyboard_space_down = False; self.keyboard_stroke_changed = False
            self.keyboard_count = ""; self.keyboard_g_pending = False
            self.update(); event.accept(); return
        if event.key() == Qt.Key.Key_Escape and self.selected_layer is not None:
            self.selected_layer = None
            self.update_accessible_description()
            self.update()
            self.changed.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            x, y = self.keyboard_cursor
            if self.tool == "select":
                if self.selection_origin is None:
                    self.selection_origin = (x, y); self.selection_rect = QRect(x, y, 1, 1)
                else:
                    self.selection_origin = None
                self.changed.emit(); self.update(); event.accept(); return
            index = y * self.card.width + x
            if self.tool == "pick":
                self.color_index = self.card.pixels[index]
                self.tool = "draw"
                self.update_accessible_description()
                self.changed.emit()
            elif self.tool == "move":
                hit = self._hit_layer(x, y)
                self.selected_layer = hit
                self.hover_layer = hit
                if hit is not None:
                    layer = self.stamp_layers[hit]
                    self.keyboard_cursor = (layer["x"], layer["y"])
                self._update_hover_remove_rect()
                self.update_accessible_description()
                self.update()
                self.changed.emit()
            elif self.tool == "fill":
                target = self.base_pixels[index]
                if target != self.color_index:
                    self.remember_edit()
                    pending = [(x, y)]
                    while pending:
                        px, py = pending.pop()
                        if not (0 <= px < self.card.width and 0 <= py < self.card.height):
                            continue
                        position = py * self.card.width + px
                        if self.base_pixels[position] != target:
                            continue
                        self.base_pixels[position] = self.color_index
                        pending.extend(((px-1, py), (px+1, py), (px, py-1), (px, py+1)))
                    self._render_layers()
            else:
                self.remember_edit()
                self.base_pixels[index] = 0 if self.tool == "erase" else self.color_index
                self._render_layers()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            self.keyboard_space_down = False
            self.keyboard_stroke_changed = False
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _paint_keyboard_pixel(self, remember=False):
        x, y = self.keyboard_cursor
        index = y * self.card.width + x
        value = self.color_index
        if self.base_pixels[index] == value:
            return
        if remember:
            self.remember_edit()
        self.base_pixels[index] = value
        self.keyboard_stroke_changed = True
        self._render_layers()

    def fill_selection_or_region(self):
        rect = self.selection_rect.intersected(QRect(0, 0, self.card.width, self.card.height))
        if not rect.isNull() and not rect.isEmpty():
            indexes = [y * self.card.width + x
                       for y in range(rect.top(), rect.bottom() + 1)
                       for x in range(rect.left(), rect.right() + 1)]
            targets = [i for i in indexes if self.base_pixels[i] != self.color_index]
            if not targets:
                return
            self.remember_edit()
            for index in targets:
                self.base_pixels[index] = self.color_index
            self._render_layers()
            return

        x, y = self.keyboard_cursor
        index = y * self.card.width + x
        target = self.base_pixels[index]
        if target == self.color_index:
            return
        self.remember_edit()
        pending = [(x, y)]
        while pending:
            px, py = pending.pop()
            if not (0 <= px < self.card.width and 0 <= py < self.card.height):
                continue
            position = py * self.card.width + px
            if self.base_pixels[position] != target:
                continue
            self.base_pixels[position] = self.color_index
            pending.extend(((px-1, py), (px+1, py), (px, py-1), (px, py+1)))
        self._render_layers()

    def _move_keyboard_cursor(self, dx, dy):
        x, y = self.keyboard_cursor
        self.keyboard_cursor = (min(max(0, x + dx), self.card.width - 1),
                                min(max(0, y + dy), self.card.height - 1))
        if self.selection_origin is not None:
            ax, ay = self.selection_origin; bx, by = self.keyboard_cursor
            self.selection_rect = QRect(min(ax, bx), min(ay, by), abs(ax-bx)+1, abs(ay-by)+1)
            self.changed.emit()
        self.update_accessible_description()
        self.update()

    def _take_keyboard_count(self):
        count = int(self.keyboard_count) if self.keyboard_count else 1
        self.keyboard_count = ""
        return max(1, min(count, max(self.card.width, self.card.height)))

    def _move_keyboard_steps(self, dx, dy, count=1):
        for _ in range(count):
            before = self.keyboard_cursor
            self._move_keyboard_cursor(dx, dy)
            if self.keyboard_cursor == before:
                break
            if self.keyboard_space_down and self.tool == "draw":
                self._paint_keyboard_pixel(remember=not self.keyboard_stroke_changed)

    def copy_selection_to_clipboard(self):
        rect = self.selection_rect.intersected(QRect(0, 0, self.card.width, self.card.height))
        if rect.isEmpty():
            return
        image = QImage(rect.width(), rect.height(), QImage.Format.Format_ARGB32)
        for y in range(rect.height()):
            for x in range(rect.width()):
                value = self.card.pixels[(rect.y()+y)*self.card.width+rect.x()+x]
                image.setPixelColor(x, y, QColor(*(channel*17 for channel in self.card.palette[value])))
        QApplication.clipboard().setImage(image)

    def move_selection(self, dx, dy):
        index = self.lift_selection()
        if index is None:
            return
        layer = self.stamp_layers[index]
        x = min(max(0, layer["x"] + dx), self.card.width-layer["width"])
        y = min(max(0, layer["y"] + dy), self.card.height-layer["height"])
        if (x, y) != (layer["x"], layer["y"]):
            self.remember_edit()
            layer["x"], layer["y"] = x, y
            self.keyboard_cursor = (x, y)
            self._render_layers()

    def delete_selection_or_pixel(self, copy_to_clipboard=False):
        rect = self.selection_rect.intersected(QRect(0, 0, self.card.width, self.card.height))
        if rect.isEmpty():
            x, y = self.keyboard_cursor
            rect = QRect(x, y, 1, 1)
        if copy_to_clipboard:
            image = QImage(rect.width(), rect.height(), QImage.Format.Format_ARGB32)
            for y in range(rect.height()):
                for x in range(rect.width()):
                    value = self.card.pixels[(rect.y()+y)*self.card.width+rect.x()+x]
                    image.setPixelColor(x, y, QColor(*(channel*17 for channel in self.card.palette[value])))
            QApplication.clipboard().setImage(image)
        self.remember_edit()
        for y in range(rect.top(), rect.bottom()+1):
            for x in range(rect.left(), rect.right()+1):
                self.base_pixels[y*self.card.width+x] = 0
                for layer in self.stamp_layers:
                    sx, sy = x-layer["x"], y-layer["y"]
                    if 0 <= sx < layer["width"] and 0 <= sy < layer["height"]:
                        layer["pixels"][sy*layer["width"]+sx] = None
        self.selection_rect = QRect(); self.selection_handle = QRect(); self.selection_origin = None
        self._render_layers()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    def set_card(self, card, base_pixels=None, stamp_layers=None):
        self.card = card
        self.base_pixels = list(base_pixels if base_pixels is not None else card.pixels)
        if len(self.base_pixels) != card.width*card.height or any(not isinstance(value, int) or not 0 <= value < len(card.palette) for value in self.base_pixels):
            raise ValueError("Saved card base pixels are invalid")
        self.stamp_layers = []
        for layer in stamp_layers or []:
            width, height = int(layer["width"]), int(layer["height"])
            values = list(layer["pixels"])
            if not 1 <= width <= card.width or not 1 <= height <= card.height or len(values) != width*height:
                raise ValueError("Saved stamp layer dimensions are invalid")
            if any(value is not None and (not isinstance(value, int) or not 0 <= value < len(card.palette)) for value in values):
                raise ValueError("Saved stamp layer colors are invalid")
            self.stamp_layers.append({"x": min(max(0, int(layer["x"])), card.width-width),
                                      "y": min(max(0, int(layer["y"])), card.height-height),
                                      "width": width, "height": height, "pixels": values})
        self.selected_layer = None
        self.selection_rect = QRect(); self.selection_handle = QRect()
        self.selection_origin = None
        self.hover_layer = self.resize_layer = None
        self.hover_remove_rect = QRect(); self.hover_convert_rect = QRect(); self.hover_resize_rect = QRect()
        self.drag_layer = None
        self.undo_stack.clear(); self.redo_stack.clear()
        self._render_layers()
        self.update_accessible_description()

    def snapshot(self):
        return copy.deepcopy((self.card.width, self.card.height, self.card.palette,
                              self.base_pixels, self.stamp_layers))

    def remember_edit(self):
        snapshot = self.snapshot()
        if not self.undo_stack or self.undo_stack[-1] != snapshot:
            self.undo_stack.append(snapshot)
            self.undo_stack = self.undo_stack[-100:]
        self.redo_stack.clear()

    def restore_edit(self, source, destination):
        if not source: return
        destination.append(self.snapshot())
        width, height, palette, base, layers = source.pop()
        self.card.width, self.card.height, self.card.palette = width, height, palette
        self.base_pixels, self.stamp_layers = base, layers
        self.selected_layer = self.drag_layer = self.hover_layer = None
        self.selection_rect = QRect(); self.selection_handle = QRect(); self.selection_origin = None
        self.color_index = min(self.color_index, len(palette)-1)
        self._render_layers()
        self.update_accessible_description()

    def undo(self):
        self.restore_edit(self.undo_stack, self.redo_stack)

    def redo(self):
        self.restore_edit(self.redo_stack, self.undo_stack)

    def layer_data(self):
        return [{"x": int(layer["x"]), "y": int(layer["y"]),
                 "width": int(layer["width"]), "height": int(layer["height"]),
                 "pixels": list(layer["pixels"])} for layer in self.stamp_layers]

    def _render_layers(self):
        self.card.pixels = list(self.base_pixels)
        canvas_width, canvas_height = self.card.width, self.card.height
        for layer in self.stamp_layers:
            for sy in range(layer["height"]):
                y = layer["y"] + sy
                if not 0 <= y < canvas_height: continue
                for sx in range(layer["width"]):
                    x = layer["x"] + sx
                    if not 0 <= x < canvas_width: continue
                    color = layer["pixels"][sy*layer["width"]+sx]
                    if color is not None:
                        self.card.pixels[y*canvas_width+x] = int(color)
        self.update()
        self.changed.emit()

    def change_format(self, width: int, height: int, palette: list[tuple[int, int, int]]):
        self.remember_edit()
        self.selection_rect = QRect(); self.selection_handle = QRect(); self.selection_origin = None
        old_width, old_height = self.card.width, self.card.height
        old_palette = self.card.palette
        def color_map(index):
            rgb = old_palette[index]
            return min(range(len(palette)), key=lambda i: sum((rgb[k]-palette[i][k])**2 for k in range(3)))
        old_base = list(self.base_pixels)
        self.card.width, self.card.height, self.card.palette = width, height, list(palette)
        self.base_pixels = [color_map(old_base[min(old_height-1, int((y+.5)*old_height/height))*old_width+
                                                min(old_width-1, int((x+.5)*old_width/width))])
                            for y in range(height) for x in range(width)]
        resized_layers = []
        for layer in self.stamp_layers:
            new_width = max(1, min(width, round(layer["width"]*width/old_width)))
            new_height = max(1, min(height, round(layer["height"]*height/old_height)))
            new_pixels = []
            for y in range(new_height):
                sy = min(layer["height"]-1, int((y+.5)*layer["height"]/new_height))
                for x in range(new_width):
                    sx = min(layer["width"]-1, int((x+.5)*layer["width"]/new_width))
                    value = layer["pixels"][sy*layer["width"]+sx]
                    new_pixels.append(None if value is None else color_map(value))
            resized_layers.append({"x": min(width-new_width, round(layer["x"]*width/old_width)),
                                   "y": min(height-new_height, round(layer["y"]*height/old_height)),
                                   "width": new_width, "height": new_height, "pixels": new_pixels})
        self.stamp_layers = resized_layers
        self.color_index = min(self.color_index, len(palette)-1)
        self._render_layers()
        self.update_accessible_description()

    def add_stamp(self, image):
        if image.isNull(): return
        self.remember_edit()
        self.tool = "move"
        self.update_accessible_description()
        scaled = image
        if image.width() > self.card.width or image.height() > self.card.height:
            scaled = image.scaled(self.card.width, self.card.height, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
        width, height = scaled.width(), scaled.height()
        values = []
        for y in range(height):
            for x in range(width):
                color = scaled.pixelColor(x, y)
                if color.alpha() < 32:
                    values.append(None)
                    continue
                rgb = (color.red()/17, color.green()/17, color.blue()/17)
                nearest = min(range(len(self.card.palette)), key=lambda i: sum((rgb[k]-self.card.palette[i][k])**2 for k in range(3)))
                values.append(nearest)
        self.stamp_layers.append({"x": 0, "y": 0, "width": width, "height": height, "pixels": values})
        self.selected_layer = len(self.stamp_layers)-1
        self._render_layers()

    def lift_selection(self):
        rect = self.selection_rect.intersected(QRect(0, 0, self.card.width, self.card.height))
        if rect.isEmpty(): return None
        self.remember_edit()
        values = [self.card.pixels[y*self.card.width+x] for y in range(rect.top(), rect.bottom()+1)
                  for x in range(rect.left(), rect.right()+1)]
        for y in range(rect.top(), rect.bottom()+1):
            for x in range(rect.left(), rect.right()+1):
                self.base_pixels[y*self.card.width+x] = 0
                for layer in self.stamp_layers:
                    sx, sy = x-layer["x"], y-layer["y"]
                    if 0 <= sx < layer["width"] and 0 <= sy < layer["height"]:
                        layer["pixels"][sy*layer["width"]+sx] = None
        self.stamp_layers.append(dict(x=rect.x(), y=rect.y(), width=rect.width(), height=rect.height(), pixels=values))
        self.selected_layer = self.hover_layer = len(self.stamp_layers)-1
        self.selection_rect = QRect(); self.selection_handle = QRect()
        self.tool = "move"
        self._render_layers()
        return self.selected_layer

    def convert_stamp_to_pixels(self, index=None):
        index = self.selected_layer if index is None else index
        if index is None or not 0 <= index < len(self.stamp_layers):
            return False
        self.remember_edit()
        # Bake through this layer so overlapping lower stamps retain their
        # visible order when the selected stamp becomes editable canvas pixels.
        for layer in self.stamp_layers[:index + 1]:
            for sy in range(layer["height"]):
                for sx in range(layer["width"]):
                    x, y = layer["x"] + sx, layer["y"] + sy
                    color = layer["pixels"][sy * layer["width"] + sx]
                    if color is not None and 0 <= x < self.card.width and 0 <= y < self.card.height:
                        self.base_pixels[y * self.card.width + x] = int(color)
        del self.stamp_layers[:index + 1]
        self.selected_layer = self.hover_layer = None
        self.tool = "draw"
        self._render_layers()
        self.update_accessible_description()
        return True

    def _hit_layer(self, x, y):
        for index in range(len(self.stamp_layers)-1, -1, -1):
            layer = self.stamp_layers[index]
            sx, sy = x-layer["x"], y-layer["y"]
            if 0 <= sx < layer["width"] and 0 <= sy < layer["height"]:
                if layer["pixels"][sy*layer["width"]+sx] is not None:
                    return index
        return None

    def paint_at(self, point, erase=False):
        x, y = self.card_position(point)
        if x is None:
            self.last_paint_pixel = None; return
        start_x, start_y = self.last_paint_pixel or (x, y)
        steps = max(abs(x-start_x), abs(y-start_y), 1)
        for step in range(steps+1):
            px = round(start_x+(x-start_x)*step/steps)
            py = round(start_y+(y-start_y)*step/steps)
            self.base_pixels[py*self.card.width+px] = 0 if erase or self.tool == "erase" else self.color_index
        self.last_paint_pixel = (x, y)
        self._render_layers()

    def mousePressEvent(self, event):
        if event.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton): return
        self.last_paint_pixel = None
        point = event.position().toPoint()
        if self.tool == "select" and event.button() == Qt.MouseButton.LeftButton:
            x, y = self.card_position(point)
            if x is None: return
            resizing = self.selection_handle.contains(point)
            if resizing or self.selection_rect.contains(x, y):
                index = self.lift_selection()
                if index is not None:
                    layer = self.stamp_layers[index]
                    if resizing:
                        self.resize_layer = index; self.resize_origin_point = point
                        self.resize_original = {"width": layer["width"], "height": layer["height"], "pixels": list(layer["pixels"])}
                    else:
                        self.drag_layer = index; self.drag_offset = (x-layer["x"], y-layer["y"])
                return
            self.selected_layer = self.hover_layer = None
            self.selection_origin = (x, y); self.selection_rect = QRect(x, y, 1, 1)
            self.selection_handle = QRect(); self.update(); self.changed.emit(); return
        self._update_hover_remove_rect()
        if event.button() == Qt.MouseButton.LeftButton and self.hover_convert_rect.contains(point):
            self.convert_stamp_to_pixels(self.hover_layer)
            return
        if (event.button() == Qt.MouseButton.LeftButton and self.hover_layer is not None and
                self.hover_layer < len(self.stamp_layers) and self.hover_remove_rect.contains(point)):
            self.remember_edit()
            del self.stamp_layers[self.hover_layer]
            self.selected_layer = None
            self.hover_layer = None
            self._render_layers()
            self.placed_stamp_removed.emit()
            return
        if (event.button() == Qt.MouseButton.LeftButton and self.hover_layer is not None and
                self.hover_layer < len(self.stamp_layers) and self.hover_resize_rect.contains(point)):
            self.selected_layer = self.hover_layer
            self.resize_layer = self.hover_layer
            self.resize_origin_point = point
            layer = self.stamp_layers[self.resize_layer]
            self.resize_original = {
                "width": layer["width"], "height": layer["height"],
                "pixels": list(layer["pixels"]),
            }
            self.remember_edit()
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            self.changed.emit()
            self.update()
            return
        x, y = self.card_position(point)
        if x is None: return
        self.keyboard_cursor = (x, y)
        self.update_accessible_description()
        self.update()
        if self.tool == "pick":
            self.color_index = self.card.pixels[y*self.card.width+x]
            self.tool = "draw"
            self.update_accessible_description()
            self.changed.emit(); return
        if self.tool == "move":
            self.selected_layer = self._hit_layer(x, y)
            self.drag_layer = self.selected_layer
            if self.drag_layer is not None:
                self.remember_edit()
                layer = self.stamp_layers[self.drag_layer]
                self.drag_offset = (x-layer["x"], y-layer["y"])
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.changed.emit(); self.update(); return
        self.remember_edit()
        self.selected_layer = None
        if self.tool == "fill":
            target = self.base_pixels[y*self.card.width+x]
            replacement = self.color_index
            if target == replacement: return
            pending = [(x, y)]
            while pending:
                px, py = pending.pop()
                if not (0 <= px < self.card.width and 0 <= py < self.card.height): continue
                index = py*self.card.width+px
                if self.base_pixels[index] != target: continue
                self.base_pixels[index] = replacement
                pending.extend(((px-1,py), (px+1,py), (px,py-1), (px,py+1)))
            self._render_layers()
        else:
            self.paint_at(point, event.button() == Qt.MouseButton.RightButton)

    def mouseReleaseEvent(self, event):
        self.selection_origin = None
        self.last_paint_pixel = None
        self.drag_layer = None
        self.resize_layer = None
        self.resize_origin_point = None
        self.resize_original = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if self.undo_stack and self.undo_stack[-1] == self.snapshot(): self.undo_stack.pop()
        self.changed.emit()

    def mouseMoveEvent(self, event):
        if self.selection_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            x, y = self.card_position(event.position().toPoint())
            if x is not None:
                ox, oy = self.selection_origin
                self.selection_rect = QRect(min(x, ox), min(y, oy), abs(x-ox)+1, abs(y-oy)+1)
                self.update(); self.changed.emit()
            return
        if not event.buttons() & (Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton):
            point = event.position().toPoint()
            x, y = self.card_position(point)
            # The close control may cover transparent pixels or extend beyond a small stamp.
            layer = (self.hover_layer if self.hover_layer is not None and
                     (self.hover_remove_rect.contains(point) or self.hover_convert_rect.contains(point) or
                      self.hover_resize_rect.contains(point)) else
                     next((i for i in range(len(self.stamp_layers)-1, -1, -1)
                           if self.stamp_layers[i]["x"] <= x < self.stamp_layers[i]["x"] + self.stamp_layers[i]["width"]
                           and self.stamp_layers[i]["y"] <= y < self.stamp_layers[i]["y"] + self.stamp_layers[i]["height"]), None)
                     if x is not None else None)
            if layer is None:
                layer = self.selected_layer
            if layer != self.hover_layer:
                self.hover_layer = layer
                self._update_hover_remove_rect()
                self.update()
            if layer is not None and self.hover_resize_rect.contains(point):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif layer is not None and (self.hover_remove_rect.contains(point) or self.hover_convert_rect.contains(point)):
                self.setToolTip("Convert stamp and stamps beneath it to editable pixels (Undo restores stamps)" if self.hover_convert_rect.contains(point) else "Remove placed stamp")
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if self.resize_layer is not None and self.resize_original is not None:
            side = min(self.width(), self.height())
            cell = side / max(self.card.width, self.card.height)
            delta_x = (event.position().x() - self.resize_origin_point.x()) / cell
            delta_y = (event.position().y() - self.resize_origin_point.y()) / cell
            layer = self.stamp_layers[self.resize_layer]
            original = self.resize_original
            original_width, original_height = original["width"], original["height"]
            scale = (
                original_width * (original_width + delta_x) +
                original_height * (original_height + delta_y)) / (
                    original_width**2 + original_height**2)
            min_scale = max(1 / original_width, 1 / original_height)
            max_scale = min((self.card.width-layer["x"]) / original_width,
                            (self.card.height-layer["y"]) / original_height)
            scale = min(max(scale, min_scale), max_scale)
            width = max(1, min(self.card.width-layer["x"], round(original_width*scale)))
            height = max(1, min(self.card.height-layer["y"], round(original_height*scale)))
            source = original["pixels"]
            layer.update(width=width, height=height, pixels=[
                source[min(original_height-1, int((y+.5)*original_height/height))*original_width+
                       min(original_width-1, int((x+.5)*original_width/width))]
                for y in range(height) for x in range(width)])
            self._render_layers()
        elif self.tool == "move" and self.drag_layer is not None:
            x, y = self.card_position(event.position().toPoint())
            if x is not None:
                layer = self.stamp_layers[self.drag_layer]
                layer["x"] = min(max(0, x-self.drag_offset[0]), self.card.width-layer["width"])
                layer["y"] = min(max(0, y-self.drag_offset[1]), self.card.height-layer["height"])
                self._render_layers()
        elif self.tool in {"draw", "erase"}:
            self.paint_at(event.position().toPoint(), bool(event.buttons() & Qt.MouseButton.RightButton))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = [url for url in event.mimeData().urls() if url.isLocalFile()]
        if not urls:
            return
        path = Path(urls[0].toLocalFile())
        image = QImage(str(path))
        if image.isNull():
            return
        self.image_dropped.emit(image)
        event.acceptProposedAction()


class StampDrawingCanvas(QWidget):
    """Small pixel editor that keeps untouched pixels fully transparent."""
    def __init__(self, image: QImage, color: QColor, color_name: str | None = None):
        super().__init__()
        self.image = image
        self.color = color
        self.color_name = color_name or color.name()
        self.erase = False
        self.keyboard_cursor = (0, 0)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(384, 384)
        self.setMaximumSize(512, 512)
        self.setAccessibleName("Transparent stamp drawing canvas")
        self.update_accessible_description()

    def set_brush_color(self, color, color_name: str | None = None):
        self.color = color
        self.color_name = color_name or color.name()
        self.update_accessible_description()

    def update_accessible_description(self):
        x, y = self.keyboard_cursor
        color = f"{self.color_name} ({self.color.name()})"
        self.setAccessibleDescription(
            f"{self.image.width()} by {self.image.height()} pixel transparent stamp. "
            f"Keyboard cursor at column {x + 1}, row {y + 1}. Brush color {color}. "
            "Tab to focus, use arrow keys to move, Space or Enter to paint, and "
            "Delete or Backspace to erase to transparency.")

    def _pixel_at(self, point):
        side = min(self.width(), self.height())
        cell = side / max(self.image.width(), self.image.height())
        ox = (self.width() - self.image.width()*cell)/2
        oy = (self.height() - self.image.height()*cell)/2
        x, y = int((point.x()-ox)/cell), int((point.y()-oy)/cell)
        return (x, y) if 0 <= x < self.image.width() and 0 <= y < self.image.height() else (None, None)

    def _paint_pixel(self, point):
        x, y = self._pixel_at(point)
        if x is not None:
            self.keyboard_cursor = (x, y)
            self.image.setPixelColor(x, y, QColor(0, 0, 0, 0) if self.erase else self.color)
            self.update_accessible_description()
            self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS["stamp_canvas_background"]))
        side = min(self.width(), self.height())
        cell = side / max(self.image.width(), self.image.height())
        ox, oy = (self.width()-self.image.width()*cell)/2, (self.height()-self.image.height()*cell)/2
        for y in range(self.image.height()):
            for x in range(self.image.width()):
                rect = QRect(int(ox+x*cell), int(oy+y*cell), max(1, int(cell)), max(1, int(cell)))
                checker = ("stamp_canvas_checker_light" if (x+y)%2
                           else "stamp_canvas_checker_dark")
                painter.fillRect(rect, QColor(COLORS[checker]))
                color = self.image.pixelColor(x, y)
                if color.alpha():
                    painter.fillRect(rect, color)
        painter.setPen(QPen(QColor(COLORS["stamp_canvas_grid"]), 0.5))
        for x in range(self.image.width()+1):
            px = int(ox+x*cell); painter.drawLine(px, int(oy), px, int(oy+self.image.height()*cell))
        for y in range(self.image.height()+1):
            py = int(oy+y*cell); painter.drawLine(int(ox), py, int(ox+self.image.width()*cell), py)
        if self.hasFocus():
            x, y = self.keyboard_cursor
            cursor = QRect(int(ox+x*cell), int(oy+y*cell), max(1, int(cell)), max(1, int(cell)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(COLORS["focus_ring"]), 2))
            painter.drawRect(cursor.adjusted(1, 1, -1, -1))
            painter.setPen(QPen(QColor(COLORS["window"]), 1))
            painter.drawRect(cursor.adjusted(3, 3, -3, -3))

    def mousePressEvent(self, event):
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self.erase = event.button() == Qt.MouseButton.RightButton
            self._paint_pixel(event.position().toPoint())

    def keyPressEvent(self, event):
        directions = {
            Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1),
        }
        if event.key() in directions:
            dx, dy = directions[event.key()]
            x, y = self.keyboard_cursor
            self.keyboard_cursor = (min(max(0, x + dx), self.image.width() - 1),
                                    min(max(0, y + dy), self.image.height() - 1))
            self.update_accessible_description()
            self.update()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter,
                           Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            x, y = self.keyboard_cursor
            erase = event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
            self.image.setPixelColor(x, y, QColor(0, 0, 0, 0) if erase else self.color)
            self.update()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    def mouseMoveEvent(self, event):
        if event.buttons() & (Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton):
            self._paint_pixel(event.position().toPoint())


class ListenEncodeSignals(QObject):
    finished = Signal(object)


class ListenEncodeWorker(QRunnable):
    def __init__(self, card, backend, token):
        super().__init__()
        self.card, self.backend, self.token = card, backend, token
        self.signals = ListenEncodeSignals()

    def run(self):
        try:
            audio = EXPERIMENTAL_ADAPTER.encode(self.backend, self.card, 1, "cq", None)
            result = (self.token, pcm16_audio_bytes(audio), None)
        except Exception as exc:
            result = (self.token, None, str(exc))
        self.signals.finished.emit(result)


class CanvasListenDialog(QDialog):
    def __init__(self, snapshot, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Listen in")
        self.snapshot = snapshot
        self.token = 0
        self.jobs = {}
        self.sink = self.buffer = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.mode = QComboBox()
        for key, backend in available_modes(include_experimental=True):
            if key not in TX_DISABLED_MODE_KEYS and MODE_REGISTRY[key].adapter == EXPERIMENTAL_ADAPTER.key:
                self.mode.addItem(backend.label, key)
        preferred = "experimental_8fsk_musical_25"
        if parent is not None and hasattr(parent, "selected_mode"):
            preferred = parent.selected_mode()
        index = self.mode.findData(preferred)
        if index < 0:
            index = self.mode.findData("experimental_8fsk_musical_25")
        if index >= 0:
            self.mode.setCurrentIndex(index)
        self.output = QComboBox()
        default = QMediaDevices.defaultAudioOutput()
        for device in QMediaDevices.audioOutputs():
            self.output.addItem(device.description(), device)
            if device.id() == default.id():
                self.output.setCurrentIndex(self.output.count() - 1)
        self.volume = QSpinBox(); self.volume.setRange(0, 100); self.volume.setValue(25); self.volume.setSuffix(" %")
        self.volume.valueChanged.connect(lambda value: self.sink.setVolume(value / 100) if self.sink else None)
        form.addRow("Modem", self.mode); form.addRow("Audio output", self.output); form.addRow("Volume", self.volume)
        layout.addLayout(form)
        self.status = QLabel("Play the current canvas as one modem transmission.")
        self.status.setWordWrap(True); layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.play = QPushButton("Play"); self.stop_button = QPushButton("Stop"); close = QPushButton("Close")
        self.play.clicked.connect(self.start_playback); self.stop_button.clicked.connect(self.stop_playback); close.clicked.connect(self.reject)
        for button in (self.play, self.stop_button, close): buttons.addWidget(button)
        layout.addLayout(buttons)
        self.stop_button.setEnabled(False)
        self.mode.currentIndexChanged.connect(lambda _: self.stop_playback())
        self.output.currentIndexChanged.connect(lambda _: self.stop_playback())
        self.finished.connect(lambda _: self.stop_playback())
        self.resize(460, 220)

    def stop_playback(self):
        self.token += 1
        if self.sink is not None:
            self.sink.stop(); self.sink.deleteLater(); self.sink = None
        if self.buffer is not None:
            self.buffer.close(); self.buffer.deleteLater(); self.buffer = None
        self.play.setEnabled(True); self.stop_button.setEnabled(False)
        self.status.setText("Stopped")

    def start_playback(self):
        self.stop_playback()
        if self.output.currentData() is None or self.mode.currentData() is None:
            self.status.setText("Choose an available modem and audio output."); return
        try:
            card = self.snapshot()
        except Exception as exc:
            self.status.setText(str(exc)); return
        worker = ListenEncodeWorker(card, get_backend(self.mode.currentData(), 300), self.token)
        self.jobs[self.token] = worker
        worker.signals.finished.connect(self.audio_ready)
        self.play.setEnabled(False); self.stop_button.setEnabled(True)
        self.status.setText("Preparing canvas audio…")
        QThreadPool.globalInstance().start(worker)

    def audio_ready(self, result):
        token, pcm, error = result
        self.jobs.pop(token, None)
        if token != self.token or not self.isVisible(): return
        if error:
            self.status.setText(error); self.play.setEnabled(True); self.stop_button.setEnabled(False); return
        fmt = QAudioFormat(); fmt.setSampleRate(modem.SAMPLE_RATE); fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = self.output.currentData()
        if device is None or not device.isFormatSupported(fmt):
            self.status.setText("This output does not support 48 kHz mono audio.")
            self.play.setEnabled(True); self.stop_button.setEnabled(False); return
        self.buffer = QBuffer(self); self.buffer.setData(QByteArray(pcm)); self.buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.sink = QAudioSink(device, fmt, self); self.sink.setVolume(self.volume.value()/100)
        self.sink.stateChanged.connect(self.audio_state_changed)
        self.sink.start(self.buffer)
        self.status.setText(f"Playing · {len(pcm)/(2*modem.SAMPLE_RATE):.1f} seconds")

    def audio_state_changed(self, state):
        if state == QAudio.State.IdleState and self.buffer is not None and self.buffer.atEnd():
            self.stop_playback(); self.status.setText("Playback complete")
        elif state == QAudio.State.StoppedState and self.sink is not None and self.sink.error() not in (QAudio.Error.NoError, QAudio.Error.UnderrunError):
            self.status.setText(f"Audio output failed: {self.sink.error().name}")
            self.play.setEnabled(True); self.stop_button.setEnabled(False)


class ContentScrollArea(QScrollArea):
    """Let splitters shrink vertically, but never crop a form's horizontal minimum."""
    def _sync_content_width(self):
        if self.widget() is not None:
            width = (self.widget().minimumSizeHint().width() +
                     self.verticalScrollBar().sizeHint().width() + 2*self.frameWidth())
            if self.minimumWidth() != width:
                self.setMinimumWidth(width)

    def resizeEvent(self, event):
        self._sync_content_width()
        super().resizeEvent(event)

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.StyleChange,
                            QEvent.Type.FontChange):
            self._sync_content_width()
        return result

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        if self.widget() is not None:
            width = (self.widget().minimumSizeHint().width() +
                     self.verticalScrollBar().sizeHint().width() + 2*self.frameWidth())
            hint.setWidth(max(hint.width(), width))
        return hint


class EditorDialog(QDialog):
    def reject(self):
        if self.parent().confirm_editor_close():
            super().reject()


class WebCommandBridge(QObject):
    requested = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pixel QSO")
        self.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        self.resize(1120, 820)
        self.setStyleSheet(build_app_stylesheet())
        global DATA
        configured_data = os.environ.get("PIXELQSO_DATA_DIR")
        DATA = (Path(configured_data).expanduser() if configured_data else
                Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)))
        DATA.mkdir(parents=True, exist_ok=True)
        self.settings = QSettings(str(DATA / "settings.ini"), QSettings.Format.IniFormat)
        global _BASE_APPLICATION_FONT
        if _BASE_APPLICATION_FONT is None:
            _BASE_APPLICATION_FONT = QFont(QApplication.font())
        self._base_application_font = QFont(_BASE_APPLICATION_FONT)
        self._apply_interface_text_size(
            self.settings.value("ui/interface_text_size", 100, type=int))
        self.data2g_session = None
        self.data2g_modes = {}
        self.data2g_background_monitor = False
        self.data2g_connecting = False
        self.data2g_reconnect_after_settings = False
        self.rig_reconnect_after_settings = False
        self.data2g_last_host_error = None
        self.data2g_closed_during_connect = None
        self.data2g_local_starting = False
        self.data2g_closing = False
        self.data2g_handoff_pending = False
        self.data2g_handoff_deadline = 0.0
        self.data2g_local_process = None
        self.data2g_local_log = None
        self.data2g_local_log_offset = 0
        self.data2g_local_log_monitor_offset = 0
        self.data2g_local_log_pending = ""
        self.data2g_ptt_warnings = set()
        self.data2g_ptt_failed = str(
            self.settings.value("data2g/ptt_failed", "false")).lower() in {
                "1", "true", "yes", "on"}
        self._closing = False
        try:
            self.cached_data2g_modes = [Data2GMode(**item) for item in
                                        json.loads(self.settings.value("data2g/modes", "[]"))]
        except (TypeError, ValueError, json.JSONDecodeError):
            self.cached_data2g_modes = []
        # Cached capabilities are display/history only; a Data2G mode is
        # selectable only after this session rediscovers and validates it.
        self.data2g_modes = {}
        self.card_assembly = CardAssemblyService()
        # A managed-host connection worker can outlive this window while its
        # startup retry finishes. Keep the emitter owned by the worker/session
        # references so a late result cannot emit through a deleted QObject.
        self.data2g_signals = Data2GSignals()
        self.data2g_signals.connect_finished.connect(self._data2g_connect_finished)
        self.data2g_signals.frame.connect(self._data2g_rx_frame)
        self.data2g_signals.ack.connect(self._data2g_tx_ack)
        self.data2g_signals.status.connect(self._data2g_host_status)
        self.data2g_signals.error.connect(self._data2g_session_error)
        self.data2g_signals.closed.connect(self._data2g_host_closed)
        self.data2g_connect_worker = None
        self.data2g_tx_frames = []
        self.data2g_tx_waiting = None
        self.data2g_tx_sent = 0
        self.data2g_tx_total = 0
        self.data2g_tag = 0
        self.data2g_channel_busy = False
        self.data2g_last_host_error = None
        self.data2g_ack_timeout_ms = DATA2G_ACK_TIMEOUT_MS
        self.data2g_ack_timer = QTimer(self)
        self.data2g_ack_timer.setSingleShot(True)
        self.data2g_ack_timer.setInterval(DATA2G_ACK_TIMEOUT_MS)
        self.data2g_ack_timer.timeout.connect(self._data2g_ack_timeout)
        self.data2g_host_log_timer = QTimer(self)
        self.data2g_host_log_timer.setInterval(400)
        self.data2g_host_log_timer.timeout.connect(self._poll_local_data2g_host_log)
        self.data2g_waterfall_socket = QUdpSocket(self)
        self.data2g_waterfall_socket.bind(
            QHostAddress(QHostAddress.SpecialAddress.LocalHost), 0)
        self.data2g_waterfall_timer = QTimer(self)
        self.data2g_waterfall_timer.setInterval(30)
        self.data2g_waterfall_timer.timeout.connect(self._drain_data2g_waterfall_audio)
        self.on_air = SessionRecorder.from_environment()
        self.on_air_rx_buffer = bytearray()
        self.on_air_rx_samples = 0
        self.on_air_rx_stream = None
        self.on_air_decode_pcm = None
        self.on_air_tx_id = None
        if self.on_air:
            self.setWindowTitle(f"Pixel QSO · {self.on_air.station} · {self.on_air.session}")
            self.on_air.emit("client_join", {"audio_role": os.environ.get("PIXELQSO_AUDIO_ROLE", "station")})
        self.web_server = None
        self.web_bridge = WebCommandBridge(self)
        self.web_bridge.requested.connect(self._handle_web_command)
        self.web_enabled = QCheckBox("Enable mobile web app and control API (LAN)")
        self.web_enabled.setChecked(str(self.settings.value("options/web_enabled", "false")).lower() in {"1", "true", "yes", "on"})
        self.web_port = QSpinBox(); self.web_port.setRange(1024, 65535)
        self.web_port.setValue(int(self.settings.value("options/web_port", 8765)))
        self.data2g_host_input = QLineEdit(os.environ.get(
            "PIXELQSO_DATA2G_HOST", str(self.settings.value("data2g/host", "127.0.0.1"))))
        self.auto_connect_cat = QCheckBox("Connect to rigctld on startup")
        self.auto_connect_cat.setChecked(
            str(self.settings.value("radio/auto_connect_cat", "true")).lower()
            in {"1", "true", "yes", "on"})
        self.data2g_host_source = QComboBox()
        self.data2g_host_source.addItem("Remote Data2G host", "remote")
        self.data2g_host_source.addItem("Start local Data2G host", "local")
        local_host_default = self.settings.value("data2g/local_host", None)
        if local_host_default is None:
            local_host_default = not bool(os.environ.get("PIXELQSO_DATA2G_HOST"))
        local_default = os.environ.get("PIXELQSO_DATA2G_LOCAL_HOST", str(
            local_host_default)).lower() in {"1", "true", "yes", "on"}
        self.data2g_host_source.setCurrentIndex(1 if local_default else 0)
        self.data2g_host_source.currentIndexChanged.connect(self._update_data2g_host_controls)
        self.data2g_command_port = QSpinBox(); self.data2g_command_port.setRange(1, 65535)
        self.data2g_command_port.setToolTip(
            "The Data2G host also uses the next port for its data listener.")
        self.data2g_command_port.setValue(int_setting(os.environ.get(
            "PIXELQSO_DATA2G_COMMAND_PORT", self.settings.value("data2g/command_port", 8300)),
            8300))
        self.data2g_kiss_port = QSpinBox(); self.data2g_kiss_port.setRange(1, 65535)
        self.data2g_kiss_port.setToolTip(
            "Must differ from both the command port and its next data port.")
        self.data2g_kiss_port.setValue(int_setting(os.environ.get(
            "PIXELQSO_DATA2G_KISS_PORT", self.settings.value("data2g/kiss_port", 8100)),
            8100))
        self.data2g_host_bandwidth = QComboBox()
        self.data2g_host_bandwidth.addItem("2400 Hz", 2400)
        self.data2g_host_bandwidth.addItem("500 Hz", 500)
        bandwidth_index = self.data2g_host_bandwidth.findData(
            int_setting(self.settings.value("data2g/bandwidth_hz", 2400), 2400))
        self.data2g_host_bandwidth.setCurrentIndex(max(0, bandwidth_index))
        self.data2g_rig_host = QLineEdit(os.environ.get(
            "PIXELQSO_DATA2G_RIGCTLD_HOST", str(self.settings.value(
                "data2g/rigctld_host", self.settings.value("radio/rigctld_host", "127.0.0.1")))))
        self.data2g_rig_port = QSpinBox(); self.data2g_rig_port.setRange(1, 65535)
        self.data2g_rig_port.setValue(int_setting(os.environ.get(
            "PIXELQSO_DATA2G_RIGCTLD_PORT", self.settings.value(
                "data2g/rigctld_port", self.settings.value("radio/rigctld_port", 4532))),
            4532))
        self.data2g_host_status = QLabel("Not connected")
        self.data2g_host_status.setAccessibleDescription(
            "Reports Data2G connection and mode-discovery status. A connected host does not confirm audio or PTT readiness.")
        self.data2g_host_connect = QPushButton("Connect and find available modes")
        self.data2g_host_connect.clicked.connect(self._connect_data2g_host)
        if self.web_enabled.isChecked():
            QTimer.singleShot(0, self._start_web_server)
        saved_call = self.settings.value("station/callsign", "N0CALL")
        saved_grid = self.settings.value("station/grid", "AA00")
        test_mode = os.environ.get("PIXELQSO_AUDIO_TEST", str(self.settings.value("station/audio_test_mode", "false"))).lower() in {"1", "true", "yes", "on"}
        self.test_rx_port = int(os.environ.get("PIXELQSO_TEST_RX_PORT", "0") or 0)
        self.test_tx_port = int(os.environ.get("PIXELQSO_TEST_TX_PORT", "0") or 0)
        self.test_link_enabled = (os.environ.get("PIXELQSO_TEST_LINK", "").lower() in {"1", "true", "yes", "on"}
                                  and 0 < self.test_rx_port < 65536 and 0 < self.test_tx_port < 65536)
        self.station_call = QLineEdit(os.environ.get("PIXELQSO_CALLSIGN", str(saved_call)))
        self.station_call.setMaxLength(10)
        self.station_call.setPlaceholderText("e.g. W1ABC")
        self.station_grid = QLineEdit(os.environ.get("PIXELQSO_GRID", str(saved_grid)))
        self.station_grid.setMaxLength(10)
        self.station_grid.setPlaceholderText("e.g. FN42")
        self.cwid_enabled = QCheckBox("Append CW ID after Experimental transmissions")
        self.cwid_enabled.setChecked(self.settings.value("station/cwid_enabled", True, type=bool))
        self.cwid_message = QLineEdit(str(self.settings.value(
            "station/cwid_message", "PXL DE {CALL}")))
        self.cwid_message.setMaxLength(100)
        self.cwid_message.setToolTip(
            "CW ID is appended to Experimental modem audio only. Use {CALL} to insert the station call sign. "
            "Data2G creates its waveform inside the host.")
        self.cwid_enabled.setToolTip(
            "Append the configured Morse ID after local Experimental modem audio. Data2G is host-generated.")
        self.cwid_message.setEnabled(self.cwid_enabled.isChecked())
        self.cwid_enabled.toggled.connect(self.cwid_message.setEnabled)
        self.show_experimental_modes = QCheckBox("Show experimental modem modes")
        self.show_experimental_modes.setChecked(str(self.settings.value("options/show_experimental_modes", "false")).lower() in {"1", "true", "yes", "on"})
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("Data2G", "data2g")
        self.backend_combo.addItem("Experimental modems", "experimental")
        experimental_item = self.backend_combo.model().item(1)
        if experimental_item is not None:
            experimental_item.setEnabled(self.show_experimental_modes.isChecked())
        saved_backend = str(self.settings.value("options/backend", "data2g"))
        if saved_backend not in {"data2g", "experimental"}:
            saved_backend = "data2g"
            self.settings.setValue("options/backend", saved_backend)
        self.backend_combo.setCurrentIndex(max(0, self.backend_combo.findData(saved_backend)))
        if not self.show_experimental_modes.isChecked() and saved_backend == "experimental":
            self.backend_combo.setCurrentIndex(0)
            self.settings.setValue("options/backend", "data2g")
        self.data2g_background_monitor = self.backend_combo.currentData() == "experimental"
        self.backend_combo.currentIndexChanged.connect(self._on_backend_selection_changed)
        self.show_experimental_modes.toggled.connect(self._on_experimental_visibility_changed)
        self.audio_test_mode = QCheckBox("Audio-only test mode (PTT disabled)")
        self.audio_test_mode.setChecked(test_mode)
        self.audio_test_mode.toggled.connect(self.audio_test_mode_changed)
        self.card = default_card(self.station_call.text(), self.station_grid.text(),
                                 card_id=self._new_card_id())
        self.card_path = None
        self.rig = RigctlClient(self)
        self.rig_freq_read = False
        self.rig_mode = ""
        self.ptt_active = False
        self.auto_armed = False
        self.auto_consent = False
        self.auto_should_listen = False
        self.auto_peer = ""
        self.auto_peer_card_id = None
        self.auto_own_card_id = None
        self.auto_generation = 0
        self.auto_wait_state = None
        self.auto_timeout_pending = False
        self.auto_last_card_duration = 0.0
        self.auto_live_handled_cards = set()
        self.seen_received_cards = set()
        self.session_duplicate_receipts = set()
        self.session_wall_keys = set()
        self.session_wall_items = {}
        self.session_wall_count = 0
        self.duplicate_displayed_in_qso = False
        self.duplicate_preview_key = None
        self.current_preview_is_duplicate = False
        self.contact_stage = "idle"
        self.call_cq_active = False
        self.reply_report = None
        self.stopped_reply_peers = set()
        self.quick_reply_card = None
        self.quick_draw_active = False
        self.last_cq_snapshot = None
        self.tx_message_type = "card"
        self.tx_snr_db = None
        self.resume_listen_after_tx = False
        for saved_qsl in (DATA / "qsl").glob("*.json"):
            try:
                saved_report = json.loads(saved_qsl.read_text())
                saved_card = saved_report.get("card", {})
                if (CardAssemblyService.assess(saved_report).verified_complete and
                        saved_card.get("callsign") and saved_card.get("card_id") is not None):
                    self.seen_received_cards.add(received_card_key(saved_report))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        self.decode_jobs = 0
        self.tx_audio = None
        self.tx_device = None
        self.tx_bytes = b""
        self.tx_offset = 0
        self.tx_deadline = 0
        self.tx_label = "card"
        self.tx_card = None
        self.tx_quick_draw_card = None
        self.tx_timer = QTimer(self); self.tx_timer.setInterval(10 if self.test_link_enabled else 20); self.tx_timer.timeout.connect(self._pump_tx)
        self.test_tx_socket = QUdpSocket(self) if self.test_link_enabled else None
        self.test_tx_sequence = 0
        self.test_rx_socket = None
        self.test_rx_expected = None
        self.test_rx_pending = {}
        self.test_rx_final_pending = False
        self.live_decode_final = False
        self.rx_audio = None
        self.rx_device = None
        self.local_receive_handle = None
        self.local_receive_adapter = None
        self.rx_bytes = bytearray()
        self.rx_rate = modem.SAMPLE_RATE
        self.rx_live_profile = None
        self.auto_decode_paths = set()
        from modem_envelope import CopyCaptureCache
        self.copy_capture_cache = CopyCaptureCache()
        self.rx_capture_sample_offset = 0
        self.live_decode_inflight = False
        self.rx_session_id = 0
        self.live_decode_token = 0
        self.active_live_decode_token = None
        self.live_decode_workers = {}
        self.active_receive_identity = None
        self.rx_conflict_signatures = set()
        self.rx_conflict_signature_order = []
        self.completed_receive_events = {}
        self.live_decode_samples = 0
        self.rx_timer = QTimer(self); self.rx_timer.setInterval(200); self.rx_timer.timeout.connect(self._drain_rx)
        self.live_preview_timeout = QTimer(self); self.live_preview_timeout.setSingleShot(True)
        self.live_preview_timeout.timeout.connect(self._return_to_session_wall)
        self.receive_activity_timeout = QTimer(self)
        self.receive_activity_pending = False
        self.receive_activity_timeout.setSingleShot(True)
        self.receive_activity_timeout.timeout.connect(self._receive_activity_expired)
        self.auto_receive_timer = QTimer(self); self.auto_receive_timer.setSingleShot(True)
        self.auto_receive_timer.timeout.connect(self._auto_receive_timeout)
        self.cq_timer = QTimer(self); self.cq_timer.setSingleShot(True)
        self.cq_timer.timeout.connect(self._repeat_cq)
        self.decode_pool = QThreadPool.globalInstance()
        self.active_qso_log = None
        self._active_qso_path = None
        self.passive_received_logs = {}
        self.qso_log_dir = DATA / "qso-log"
        root = QWidget(); layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACING["lg"])
        self.tabs = QTabWidget()
        editor_content = self.make_card_tab()
        self.editor_dialog = EditorDialog(self)
        editor_layout = QVBoxLayout(self.editor_dialog)
        editor_layout.setContentsMargins(24, 20, 24, 20); editor_layout.setSpacing(16)
        studio_heading = QLabel("Make something worth sharing."); studio_heading.setObjectName("pageTitle")
        studio_caption = QLabel("THE PIXEL STUDIO  ·  Draw, stamp, and bring your cards to life.")
        studio_caption.setObjectName("supportingText")
        editor_layout.addWidget(studio_heading); editor_layout.addWidget(studio_caption)
        editor_layout.addWidget(editor_content, 1)
        self.animation_author.attach_shortcuts(self.editor_dialog)
        self.editor_done_button = QPushButton("Save and close"); self.editor_done_button.setObjectName("primaryAction")
        self.editor_done_button.clicked.connect(self.editor_primary_action); editor_layout.addWidget(self.editor_done_button)
        self.editor_baseline = self.editor_signature()
        self._set_editor_title("Create or edit card")
        self.editor_dialog.resize(1080, 780)
        save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self.editor_dialog)
        save_shortcut.activated.connect(self.save_card)
        self.paste_image_shortcut = QShortcut(QKeySequence("Ctrl+V"), self.canvas)
        self.paste_image_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.paste_image_shortcut.activated.connect(self.paste_card_image)
        self.tabs.addTab(self.make_exchange_tab(), "QSO")
        self.tabs.addTab(self.make_library_tab(), "My Cards")
        self.tabs.addTab(self.make_qso_log_tab(), "QSO Log")
        self.tabs.setTabText(0, "Operate")
        self.tabs.setTabText(2, "Contacts")
        self.tabs.tabBar().hide()
        self.tabs.tabBar().setFixedHeight(0)
        self.tabs.setObjectName("workspacePages")
        layout.addWidget(self._make_desktop_shell(self.tabs))
        self.setCentralWidget(root)
        view_menu = self.menuBar().addMenu("View")
        self.advanced_controls_action = QAction("Advanced controls", self)
        self.advanced_controls_action.setCheckable(True)
        self.advanced_controls_action.setChecked(
            self.settings.value("ui/advanced_controls", False, type=bool))
        self.advanced_controls_action.toggled.connect(self._set_advanced_controls)
        view_menu.addAction(self.advanced_controls_action)
        settings_menu = self.menuBar().addMenu("Settings")
        station_settings_action = QAction("Station settings…", self)
        station_settings_action.setShortcut(QKeySequence("Ctrl+,"))
        station_settings_action.triggered.connect(self.open_station_settings)
        settings_menu.addAction(station_settings_action)
        self.station_settings_action = station_settings_action
        help_menu = self.menuBar().addMenu("Help")
        about_action = QAction("About Pixel QSO", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)
        self.about_action = about_action
        self.refresh_card_library()
        self.refresh_qso_log()
        self._update_data2g_host_controls()
        if self.backend_combo.currentData() == "data2g":
            QTimer.singleShot(0, self._connect_data2g_host)
        else:
            if self._should_auto_connect_rig():
                QTimer.singleShot(0, self.connect_rig)
            QTimer.singleShot(250, self._ensure_session_receive)
            QTimer.singleShot(0, self._ensure_data2g_background_monitor)

    def _configure_workspace_splitter(self, splitter, key, sizes):
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)
        splitter.setAccessibleName(key.replace('_', ' ').title() + " columns")
        splitter.setSizes(sizes)
        saved = self.settings.value("ui/" + key + "/splitter")
        if isinstance(saved, QByteArray):
            splitter.restoreState(saved)
        splitter.splitterMoved.connect(
            lambda *_: self.settings.setValue("ui/" + key + "/splitter", splitter.saveState()))
        for index in range(1, splitter.count()):
            handle = splitter.handle(index)
            handle.setToolTip("Drag to resize columns")
            handle.setAccessibleName("Resize columns")

    def _make_desktop_shell(self, pages):
        """Navigation is presentation only; existing page and action owners stay intact."""
        shell = QWidget(); shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0); shell_layout.setSpacing(0)
        rail = QFrame(); rail.setObjectName("navigationRail"); rail.setFixedWidth(172)
        rail_layout = QVBoxLayout(rail); rail_layout.setContentsMargins(18, 28, 18, 22)
        rail_layout.setSpacing(12)
        brand = QLabel("▦  pixelqso"); brand.setObjectName("brandName")
        rail_layout.addWidget(brand)
        tagline = QLabel("SMALL ART.\nREAL CONNECTIONS."); tagline.setObjectName("eyebrow")
        rail_layout.addWidget(tagline); rail_layout.addSpacing(32)
        self.workspace_navigation = QButtonGroup(self)
        for index, label in enumerate(("Operate", "My Cards", "Contacts")):
            button = QPushButton(label); button.setCheckable(True)
            button.setObjectName("navigationItem"); button.setMinimumHeight(48)
            button.setAccessibleName(f"Open {label}")
            button.clicked.connect(lambda _checked=False, i=index: pages.setCurrentIndex(i))
            self.workspace_navigation.addButton(button, index); rail_layout.addWidget(button)
        rail_layout.addSpacing(12)
        studio = QPushButton("＋  Create a card"); studio.setObjectName("studioShortcut")
        studio.clicked.connect(self.create_library_card); rail_layout.addWidget(studio)
        rail_layout.addStretch(1)
        caption = QLabel("YOUR STATION\nYour art, on the air."); caption.setObjectName("railCaption")
        rail_layout.addWidget(caption)
        settings = QPushButton("Station settings"); settings.clicked.connect(self.open_station_settings)
        rail_layout.addWidget(settings)
        shell_layout.addWidget(rail)
        body = QWidget(); body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(24, 22, 24, 18); body_layout.setSpacing(16)
        header = QHBoxLayout()
        words = QVBoxLayout(); words.setSpacing(4)
        self.workspace_eyebrow = QLabel(); self.workspace_eyebrow.setObjectName("eyebrow")
        self.workspace_title = QLabel(); self.workspace_title.setObjectName("workspaceTitle"); self.workspace_title.setWordWrap(True)
        self.workspace_description = QLabel(); self.workspace_description.setObjectName("supportingText")
        self.workspace_description.setWordWrap(True)
        words.addWidget(self.workspace_eyebrow); words.addWidget(self.workspace_title)
        words.addWidget(self.workspace_description); header.addLayout(words, 1)
        body_layout.addLayout(header); body_layout.addWidget(pages, 1)
        shell_layout.addWidget(body, 1)
        def select_page(index):
            copy = (("ON THE AIR", "A little art. A real connection.", "Choose a card, listen for a friend, and start an exchange."),
                    ("YOUR COLLECTION", "A world in little squares.", "Create, collect, and find the next card you want to share."),
                    ("YOUR CONTACTS", "Every card has a story.", "Revisit the people and artwork you have met on the air."))[index]
            self.workspace_eyebrow.setText(copy[0]); self.workspace_title.setText(copy[1])
            self.workspace_description.setText(copy[2]); self.workspace_navigation.button(index).setChecked(True)
        pages.currentChanged.connect(select_page); select_page(pages.currentIndex())
        return shell

    def _refresh_send_workspace(self):
        if not hasattr(self, "outgoing_art") or not hasattr(self, "stage_picker"):
            return
        stage = self._outgoing_message_type()
        index = self.stage_picker.findData(stage)
        with QSignalBlocker(self.stage_picker):
            self.stage_picker.setCurrentIndex(index)
        for name, row in self.stage_rows.items():
            row.setVisible(True)
        self.stage_picker.setEnabled(not self.auto_armed)
        try:
            card = self._chosen_card()
            signature = (card.callsign, card.grid, card.width, card.height,
                         tuple(card.palette), tuple(card.pixels))
            if signature == getattr(self, "_outgoing_art_signature", None):
                return
            self._outgoing_art_signature = signature
            image = QImage(card.width, card.height, QImage.Format.Format_RGB32)
            for i, value in enumerate(card.pixels):
                image.setPixelColor(i % card.width, i // card.width,
                                   QColor(*(int(c)*17 for c in card.palette[value])))
            self.outgoing_art.setPixmap(card_art(image, card.callsign or "Your card",
                f"{card.width} × {card.height} · {len(card.palette)} colors", QSize(152, 190)))
            self.outgoing_art.setAccessibleName(f"Selected outgoing card: {card.callsign or 'current drawing'}")
        except (ValueError, OSError, KeyError, IndexError, TypeError):
            self._outgoing_art_signature = None
            self.outgoing_art.clear(); self.outgoing_art.setText("Choose a valid card to preview")

    def _show_about(self):
        QMessageBox.about(
            self, "About Pixel QSO",
            "<h3>Pixel QSO 0.4.0b1 · Beta 1</h3>"
            "<p>Exchange pixel art cards over digital radio.</p>"
            "<p>Set up your station, listen for cards, and create or share your own.</p>")

    def _station_identity_feedback(self):
        callsign = self.station_call.text().strip().upper()
        if not callsign or callsign == "N0CALL":
            return "Enter your assigned station call sign."
        try:
            DATA2G_ADAPTER.validate_station_call(callsign)
        except ValueError:
            return "Use up to 10 letters, numbers, / or - in the call sign."
        grid = self.station_grid.text().strip().upper()
        if grid and not re.fullmatch(
                r"[A-R]{2}(?:[0-9]{2}(?:[A-X]{2}(?:[0-9]{2}(?:[A-X]{2})?)?)?)?", grid):
            return "Grid square must be a valid Maidenhead locator, such as FN42."
        return "Station details look valid. Grid square is optional."

    def _update_station_identity_hint(self, *_):
        if not hasattr(self, "station_identity_hint"):
            return
        message = self._station_identity_feedback()
        ready = message.startswith("Station details look valid")
        self.station_identity_hint.setText(message)
        self.station_identity_hint.setStyleSheet(
            semantic_text_style("positive" if ready else "warning", padding="2px"))

    def make_library_tab(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(SPACING["lg"])
        heading = QHBoxLayout()
        title = QLabel("MY CARDS"); title.setObjectName("eyebrow")
        heading.addWidget(title, 1)
        create = QPushButton("＋ Create card"); create.setObjectName("primaryAction"); create.clicked.connect(self.create_library_card); heading.addWidget(create)
        self.create_library_button = create
        starter = QPushButton("Use starter card…"); starter.clicked.connect(self.open_starter_card_picker); heading.addWidget(starter)
        self.starter_library_button = starter
        open_button = QPushButton("Open selected"); open_button.clicked.connect(self._open_library_item); heading.addWidget(open_button)
        self.open_library_button = open_button
        selection_actions = QHBoxLayout(); selection_actions.addStretch(1)
        heading.removeWidget(open_button); selection_actions.addWidget(open_button)
        self.export_card_button = QPushButton("Export selected…")
        self.export_card_button.setEnabled(False)
        self.export_card_button.clicked.connect(self.export_selected_card)
        selection_actions.addWidget(self.export_card_button)
        self.copy_card_button = QPushButton("Copy PNG")
        self.copy_card_button.clicked.connect(self.copy_selected_card)
        self.copy_card_button.setEnabled(False)
        selection_actions.addWidget(self.copy_card_button)
        more_menu = QMenu(self)
        duplicate = more_menu.addAction("Duplicate card")
        duplicate.triggered.connect(self.duplicate_library_card)
        animation = more_menu.addAction("Make selected card frame 2…")
        animation.triggered.connect(self.link_library_animation)
        unlink = more_menu.addAction("Remove animation reference")
        unlink.triggered.connect(self.unlink_library_animation)
        delete = more_menu.addAction("Delete card…")
        delete.triggered.connect(self.delete_library_card)
        more = QPushButton("More"); more.setMenu(more_menu); more.setToolTip("More card actions")
        selection_actions.addWidget(more)
        self.more_card_button = more
        layout.addLayout(heading)
        layout.addLayout(selection_actions)
        library_controls = QHBoxLayout()
        self.library_search = QLineEdit(); self.library_search.setPlaceholderText("Find a card…")
        self.library_search.setAccessibleName("Search cards by name or station")
        self.library_search.setToolTip("Search card names, station details, and categories · Ctrl+F")
        library_search_shortcut = QShortcut(QKeySequence("Ctrl+F"), page)
        library_search_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        library_search_shortcut.activated.connect(
            lambda: (self.library_search.setFocus(), self.library_search.selectAll()))
        self.library_search.textChanged.connect(self.filter_card_library)
        library_controls.addWidget(self.library_search, 1)
        self.library_filter = QComboBox()
        for label, key in (("All cards", "all"), ("My cards", "my"),
                           ("Sent cards", "sent"), ("Starter cards", "starter")):
            self.library_filter.addItem(label, key)
        self.library_filter.setAccessibleName("Filter card gallery")
        self.library_filter.currentIndexChanged.connect(self.filter_card_library)
        library_controls.addWidget(self.library_filter)
        self.library_clear_button = QPushButton("Clear filters")
        self.library_clear_button.setAccessibleName("Clear card search and category filter")
        self.library_clear_button.clicked.connect(self._clear_library_filters)
        self.library_clear_button.setVisible(False)
        library_controls.addWidget(self.library_clear_button)
        layout.addLayout(library_controls)
        self.library_list = QListWidget(); self.library_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.library_list.setIconSize(QSize(190, 238)); self.library_list.setGridSize(QSize(218, 288))
        self.library_list.setMovement(QListWidget.Movement.Static); self.library_list.setSpacing(SPACING["lg"])
        self.library_list.setObjectName("collectionGallery")
        self.library_list.setAccessibleName("Card library")
        self.library_list.setToolTip(
            "Use the arrow keys to browse cards; press Enter to open the selected card.")
        self.library_list.setAccessibleDescription(
            "Use the arrow keys to browse cards. Press Enter to open the selected card; "
            "double-click also opens it.")
        self.library_list.itemClicked.connect(lambda _item: self._activate_library_list(self.library_list))
        self.library_list.itemSelectionChanged.connect(self._update_export_buttons)
        self.library_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.library_list.itemActivated.connect(lambda _item: self._open_library_item())
        self.active_library_list = self.library_list
        gallery_page = QWidget(); gallery_layout = QVBoxLayout(gallery_page)
        gallery_layout.addWidget(self.library_list, 1)
        self.library_stack = QStackedWidget()
        self.library_stack.addWidget(gallery_page)
        self.library_empty_page = QWidget(); empty_layout = QVBoxLayout(self.library_empty_page)
        empty_layout.addStretch(1)
        self.library_empty_title = QLabel("Your cards will show up here")
        self.library_empty_title.setObjectName("emptyStateTitle")
        self.library_empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.library_empty_title)
        self.library_empty_copy = QLabel(
            "Create a card from scratch or start with a template you can personalize.")
        self.library_empty_copy.setObjectName("supportingText")
        self.library_empty_copy.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.library_empty_copy.setWordWrap(True)
        empty_layout.addWidget(self.library_empty_copy)
        empty_actions = QHBoxLayout(); empty_actions.addStretch(1)
        empty_create = QPushButton("Create a card"); empty_create.clicked.connect(self.create_library_card)
        empty_actions.addWidget(empty_create)
        empty_starter = QPushButton("Use a starter card…"); empty_starter.clicked.connect(self.open_starter_card_picker)
        empty_actions.addWidget(empty_starter); empty_actions.addStretch(1)
        empty_layout.addLayout(empty_actions); empty_layout.addStretch(1)
        self.library_stack.addWidget(self.library_empty_page)
        layout.addWidget(self.library_stack, 1)
        self.library_status = QLabel(""); layout.addWidget(self.library_status)
        return page

    def make_qso_log_tab(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(SPACING["lg"])
        heading = QLabel("Contacts")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        controls = QHBoxLayout()
        self.log_search = QLineEdit(); self.log_search.setPlaceholderText("Find a callsign or grid…")
        self.log_search.setAccessibleName("Search contacts by callsign or grid")
        self.log_search.setToolTip("Find contacts by callsign or grid · Ctrl+F")
        contact_search_shortcut = QShortcut(QKeySequence("Ctrl+F"), page)
        contact_search_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        contact_search_shortcut.activated.connect(
            lambda: (self.log_search.setFocus(), self.log_search.selectAll()))
        controls.addWidget(self.log_search, 1)
        self.log_clear_button = QPushButton("Clear search")
        self.log_clear_button.setAccessibleName("Clear contact search")
        self.log_clear_button.clicked.connect(self._clear_contact_search)
        self.log_clear_button.setVisible(False)
        controls.addWidget(self.log_clear_button)
        self.log_deck_view = QComboBox()
        self.log_deck_view.addItem("Contact sessions", "sessions")
        self.log_deck_view.addItem("Received by callsign", "received_by_call")
        self.log_deck_view.setAccessibleName("Contact deck view")
        self.log_deck_view.setToolTip(
            "Show individual contact sessions or all received cards grouped by callsign.")
        self.log_deck_view.currentIndexChanged.connect(self.refresh_qso_log)
        controls.addWidget(self.log_deck_view)
        self.log_sort = QComboBox(); self.log_sort.addItems(["Newest first", "Oldest first", "Callsign A–Z"])
        self.log_sort.setAccessibleName("Sort contacts")
        controls.addWidget(self.log_sort)
        self.export_qso_button = QPushButton("Export selected QSO…")
        self.export_qso_button.setEnabled(False)
        self.export_qso_button.clicked.connect(self.export_selected_qso)
        controls.addWidget(self.export_qso_button)
        self.copy_qso_button = QPushButton("Copy PNG")
        self.copy_qso_button.clicked.connect(self.copy_selected_qso)
        self.copy_qso_button.setEnabled(False)
        controls.addWidget(self.copy_qso_button); layout.addLayout(controls)
        self.log_gallery = QListWidget(); self.log_gallery.setViewMode(QListWidget.ViewMode.IconMode)
        self.log_gallery.setMovement(QListWidget.Movement.Static)
        self.log_gallery.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.log_gallery.setIconSize(QSize(220, 275)); self.log_gallery.setGridSize(QSize(250, 325))
        self.log_gallery.setAccessibleName("Contacts")
        self.log_gallery.setToolTip(
            "Use the arrow keys to browse contacts; press Enter to open the selected contact.")
        self.log_gallery.setAccessibleDescription(
            "Use the arrow keys to browse contact decks. Press Enter to open the selected contact; "
            "double-click also opens it.")
        self.log_gallery.setSpacing(SPACING["sm"]); self.log_gallery.itemClicked.connect(self.show_log_deck)
        self.log_gallery.itemActivated.connect(self.show_log_deck)
        self.log_gallery.itemSelectionChanged.connect(self._update_export_buttons)
        contact_gallery_page = QWidget(); contact_gallery_layout = QVBoxLayout(contact_gallery_page)
        contact_gallery_layout.setContentsMargins(0, 0, 0, 0)
        contact_gallery_layout.addWidget(self.log_gallery, 1)
        self.contact_empty_page = QWidget(); contact_empty_layout = QVBoxLayout(self.contact_empty_page)
        contact_empty_layout.addStretch(1)
        self.contact_empty_title = QLabel("No contacts yet")
        self.contact_empty_title.setObjectName("emptyStateTitle")
        self.contact_empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        contact_empty_layout.addWidget(self.contact_empty_title)
        self.contact_empty_copy = QLabel(
            "Contacts appear here after you call CQ or reply and exchange cards.")
        self.contact_empty_copy.setObjectName("supportingText")
        self.contact_empty_copy.setWordWrap(True)
        self.contact_empty_copy.setAlignment(Qt.AlignmentFlag.AlignCenter)
        contact_empty_layout.addWidget(self.contact_empty_copy)
        self.contact_empty_operate_button = QPushButton("Open Operate")
        self.contact_empty_operate_button.clicked.connect(
            lambda: self.tabs.setCurrentIndex(0))
        self.contact_empty_operate_button.setObjectName("primaryAction")
        contact_empty_layout.addWidget(self.contact_empty_operate_button,
                                       alignment=Qt.AlignmentFlag.AlignHCenter)
        contact_empty_layout.addStretch(1)
        self.contact_stack = QStackedWidget()
        self.contact_stack.addWidget(contact_gallery_page)
        self.contact_stack.addWidget(self.contact_empty_page)
        layout.addWidget(self.contact_stack, 1)
        self.log_status = QLabel(); layout.addWidget(self.log_status)
        self.log_search.textChanged.connect(self.refresh_qso_log)
        self.log_sort.currentIndexChanged.connect(self.refresh_qso_log)
        return page

    def _clear_contact_search(self):
        self.log_search.clear()
        self.log_search.setFocus()

    def refresh_card_library(self):
        if not hasattr(self, "library_list"): return
        folder = DATA / "cards"; folder.mkdir(parents=True, exist_ok=True)
        sent_folder = DATA / "sent-cards"; sent_folder.mkdir(parents=True, exist_ok=True)
        selected = self.card_combo.currentData() if hasattr(self, "card_combo") else None
        stage_selected = {stage: combo.currentData() or self.settings.value("transfer/card_" + stage)
                          for stage, combo in getattr(self, "stage_card_combos", {}).items()}
        stage_blockers = [QSignalBlocker(combo) for combo in self.stage_card_combos.values()]
        for combo in getattr(self, "stage_card_combos", {}).values():
            combo.clear(); combo.addItem("Current drawing", None)
        if hasattr(self, "card_combo"):
            self.card_combo.clear()
            self.card_combo.addItem("Current drawing", None)
        self.library_list.clear()
        card_sources = [*((path, "my") for path in folder.glob("*.json")),
                        *((path, "sent") for path in sent_folder.glob("*.json")),
                        *((path, "starter") for path in STARTER_CARDS_PATH.glob("*.json"))]
        card_sources.sort(key=lambda entry: (
            {"my": 0, "sent": 1, "starter": 2}[entry[1]], entry[0].name.casefold()))
        for path, kind in card_sources:
            try:
                data = json.loads(path.read_text()); card = modem.Card(data["callsign"], data["grid"], [tuple(c) for c in data["palette"]], data["pixels"], data.get("card_id", 0xC0DE), data.get("width", 32), data.get("height", 32), copy.deepcopy(data.get("animation")))
                preview = QImage(card.width, card.height, QImage.Format.Format_RGB32)
                for i, index in enumerate(card.pixels): preview.setPixelColor(i % card.width, i // card.width, QColor(*(int(v)*17 for v in card.palette[index])))
                section = {"my": "My card", "sent": "Sent card", "starter": "Starter card"}[kind]
                copied_from = data.get("copied_from") or {}
                origin = (f" · copied from {copied_from.get('callsign')} {copied_from.get('grid', '')}"
                          if copied_from else "")
                identity = " ".join(value for value in (card.callsign, card.grid) if value)
                card_details = (
                    f"{section} · {path.stem} · {card.callsign or 'No call sign'} · "
                    f"{card.grid or 'No grid'} · {card.width}×{card.height} · "
                    f"{len(card.palette)} colors{origin}")
                animation_note = (f" · Animation frame 2 of {card.animation['first_card_id']:04X}"
                                  if card.animation else '')
                card_details += animation_note
                item = QListWidgetItem(f"{section} · {path.stem}{animation_note}")
                item.setToolTip(card_details)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, card_details)
                item.setIcon(QIcon(card_art(preview, card.callsign, card.grid, QSize(190, 238))))
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                item.setData(Qt.ItemDataRole.UserRole + 1, kind)
                self.library_list.addItem(item)
                label = f"{identity or 'No station'} · {path.stem}"
                card_choice_help = (
                    f"{section} · {path.stem} · {card.callsign or 'No call sign'} · "
                    f"{card.grid or 'No grid'} · {card.width}×{card.height} · "
                    f"{len(card.palette)} colors")
                icon = QIcon(QPixmap.fromImage(preview))
                if kind != "starter" and hasattr(self, "card_combo"):
                    self.card_combo.addItem(icon, label, str(path))
                    index = self.card_combo.count() - 1
                    self.card_combo.setItemData(index, card_choice_help,
                                                Qt.ItemDataRole.ToolTipRole)
                    self.card_combo.setItemData(index, card_choice_help,
                                                Qt.ItemDataRole.AccessibleTextRole)
                if kind != "starter":
                    for combo in getattr(self, "stage_card_combos", {}).values():
                        combo.addItem(icon, label, str(path))
                        index = combo.count() - 1
                        combo.setItemData(index, card_choice_help,
                                          Qt.ItemDataRole.ToolTipRole)
                        combo.setItemData(index, card_choice_help,
                                          Qt.ItemDataRole.AccessibleTextRole)
            except (OSError, ValueError, KeyError, IndexError): continue
        if hasattr(self, "card_combo") and selected:
            index = self.card_combo.findData(selected)
            if index >= 0: self.card_combo.setCurrentIndex(index)
        for name, path in stage_selected.items():
            combo = self.stage_card_combos.get(name)
            if combo is not None and path:
                index = combo.findData(path)
                if index >= 0: combo.setCurrentIndex(index)
        del stage_blockers
        self.filter_card_library()
        self._refresh_transfer_summary()

    def filter_card_library(self, *_):
        if not hasattr(self, "library_list"): return
        query = self.library_search.text().strip().casefold()
        total = self.library_list.count()
        selected_filter = self.library_filter.currentData()
        self.library_search.setVisible(total > 0)
        self.library_filter.setVisible(total > 0)
        self.library_clear_button.setVisible(
            total > 0 and (bool(query) or selected_filter != "all"))
        self.create_library_button.setVisible(total > 0)
        self.starter_library_button.setVisible(total > 0)
        visible = 0
        for row in range(self.library_list.count()):
            item = self.library_list.item(row)
            kind = item.data(Qt.ItemDataRole.UserRole + 1)
            matches_filter = selected_filter == "all" or kind == selected_filter
            searchable_text = f"{item.text()} {item.toolTip()}".casefold()
            matches_query = not query or query in searchable_text
            hidden = not (matches_filter and matches_query)
            item.setHidden(hidden)
            if hidden and item.isSelected():
                self.library_list.clearSelection()
                self.library_list.setCurrentRow(-1)
            visible += not hidden
        if visible:
            self.library_status.setText(f"{visible} cards")
        elif query:
            self.library_status.setText("No matching cards")
        else:
            self.library_status.setText({
                "my": "No saved cards yet.",
                "sent": "No sent cards yet. Cards you send will appear here.",
                "starter": "No starter cards are installed.",
                "all": "",
            }.get(selected_filter, ""))
        if total == 0:
            self.library_empty_title.setText("Your cards will show up here")
            self.library_empty_copy.setText(
                "Create a card from scratch or start with a template you can personalize.")
            self.library_stack.setCurrentWidget(self.library_empty_page)
        elif visible == 0:
            if query:
                title = "No cards match"
                message = "Try a different search or category, or clear filters to see all cards."
            else:
                title, message = {
                    "my": ("No saved cards yet", "Create a card or personalize a starter card to begin your collection."),
                    "sent": ("No sent cards yet", "Cards you send will appear here."),
                    "starter": ("No starter cards installed", "You can still create a card from scratch."),
                }.get(selected_filter, ("No cards to show", "Clear filters to see the full library."))
            self.library_empty_title.setText(title)
            self.library_empty_copy.setText(message)
            self.library_stack.setCurrentWidget(self.library_empty_page)
        else:
            self.library_stack.setCurrentWidget(self.library_stack.widget(0))
        self._update_export_buttons()

    def _clear_library_filters(self):
        self.library_search.clear()
        self.library_filter.setCurrentIndex(self.library_filter.findData("all"))
        self.library_search.setFocus()

    def editor_signature(self):
        if hasattr(self, 'animation_author') and self.animation_author.frames:
            return (self.call.text(), self.grid.text(), self.animation_author.signature())
        return (self.call.text(), self.grid.text(), self.canvas.snapshot(),
                json.dumps(self.card.animation, sort_keys=True))

    def _set_editor_title(self, title):
        self.editor_dialog.setProperty("pixelqsoBaseTitle", title)
        self._update_editor_saved_state()

    def _update_editor_saved_state(self, *_):
        if not hasattr(self, "editor_dialog") or not hasattr(self, "editor_baseline"):
            return
        title = self.editor_dialog.property("pixelqsoBaseTitle")
        if not title:
            return
        dirty = self.editor_signature() != self.editor_baseline
        state = " — Unsaved changes" if dirty else ""
        self.editor_dialog.setWindowTitle(f"{title}{state}")

    def confirm_editor_close(self):
        if self.editor_signature() == getattr(self, "editor_baseline", None): return True
        choice = QMessageBox.question(self.editor_dialog, "Unsaved card", "Save your changes?",
                                      QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Save)
        if choice == QMessageBox.StandardButton.Save: return self.save_card()
        return choice == QMessageBox.StandardButton.Discard

    def save_and_close_editor(self):
        if self.save_card(): self.editor_dialog.accept()

    def editor_primary_action(self):
        if self.quick_draw_active:
            self.send_quick_draw_reply()
        else:
            self.save_and_close_editor()

    def start_quick_draw_reply(self):
        if self.auto_armed:
            return
        self.quick_draw_active = True
        self.new_card()
        self.canvas.set_card(self.card)
        self._sync_editor_controls()
        self.card_path = None
        self.editor_baseline = self.editor_signature()
        can_reply = self._reply_stage(self.reply_report) is not None
        peer = str(((self.reply_report or {}).get("card") or {}).get("callsign") or "station")
        self.editor_done_button.setText(
            f"Send reply to {peer}" if can_reply else "Use this card")
        self.editor_done_button.setToolTip(
            f"Send this card to {peer} as the next exchange stage."
            if can_reply else "Keep this drawing ready for your next manual send.")
        self._set_editor_title(
            f"Quick draw reply to {peer}" if can_reply else "Quick draw card")
        self.editor_dialog.exec()
        self.editor_baseline = self.editor_signature()
        if self.quick_draw_active:
            self.quick_draw_active = False
        self.editor_done_button.setText("Save and close")
        self.editor_done_button.setToolTip("")
        self._set_editor_title("Create or edit card")

    def send_quick_draw_reply(self):
        try:
            self.update_identity()
            self.card.validate()
            self.quick_reply_card = modem.Card(self.card.callsign, self.card.grid,
                list(self.card.palette), list(self.card.pixels), self.card.card_id,
                self.card.width, self.card.height, copy.deepcopy(self.card.animation))
            self.quick_draw_active = False
            self.editor_dialog.accept()
            if self._reply_stage(self.reply_report):
                self.reply_to_received()
            else:
                self._refresh_quick_draw_status()
        except Exception as exc:
            QMessageBox.warning(self.editor_dialog, "Couldn't send quick reply", str(exc))

    def _refresh_quick_draw_status(self):
        if not hasattr(self, "quick_draw_status"):
            return
        ready = self.quick_reply_card is not None
        self.quick_draw_status.setText("Quick-draw card buffered · ready to send again" if ready else "")
        self.clear_quick_draw_button.setVisible(ready)
        self.quick_draw_send_button.setVisible(
            ready and self.exchange_mode.currentIndex() == 0 and not self.auto_armed)

    def clear_quick_draw(self):
        self.quick_reply_card = None
        self._refresh_quick_draw_status()

    def _save_quick_draw_to_sent_library(self, card):
        if card is None:
            return False
        folders = (DATA / "sent-cards", DATA / "cards")
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in card.callsign)
        stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{card.card_id:04x}"
        target = folders[0] / f"{stem}.json"
        suffix = 2
        while any((folder / target.name).exists() for folder in folders):
            target = folders[0] / f"{stem}-{suffix}.json"
            suffix += 1
        pixels = list(card.pixels)
        data = {"callsign": card.callsign, "grid": card.grid, "palette": card.palette,
                "pixels": pixels, "card_id": card.card_id, "width": card.width,
                "height": card.height, "animation": copy.deepcopy(card.animation), "base_pixels": pixels, "stamp_layers": [],
                "archive_reason": "quick_draw_transmission"}
        serialized = json.dumps(data, indent=2) + "\n"
        target.write_text(serialized)
        (folders[1] / target.name).write_text(serialized)
        self.refresh_card_library()
        self.library_filter.setCurrentIndex(self.library_filter.findData("sent"))
        self.library_search.clear()
        self._activate_library_list(self.library_list)
        for row in range(self.library_list.count()):
            item = self.library_list.item(row)
            if Path(item.data(Qt.ItemDataRole.UserRole)) == target:
                self.library_list.setCurrentItem(item)
                item.setSelected(True)
                break
        self.library_status.setText(f"Saved quick draw to My Cards and Sent cards · {target.stem}")
        return True

    def create_library_card(self):
        self.new_card(); self.card_path = None
        self.editor_baseline = self.editor_signature()
        self._update_editor_saved_state()
        self.editor_dialog.exec()
        self.refresh_card_library()

    def open_starter_card_picker(self):
        paths = sorted(STARTER_CARDS_PATH.glob("*.json"), key=lambda path: path.stem.casefold())
        if not paths:
            QMessageBox.information(
                self, "Starter cards unavailable",
                "No starter cards are installed with this copy of Pixel QSO.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Choose a starter card")
        layout = QVBoxLayout(dialog)
        gallery = QListWidget()
        gallery.setViewMode(QListWidget.ViewMode.IconMode)
        gallery.setIconSize(QSize(190, 238))
        gallery.setGridSize(QSize(218, 288))
        gallery.setMovement(QListWidget.Movement.Static)
        gallery.setResizeMode(QListWidget.ResizeMode.Adjust)
        gallery.setSpacing(8)
        for path in paths:
            try:
                data = json.loads(path.read_text())
                card = modem.Card(
                    str(data.get("callsign", "")), str(data.get("grid", "")),
                    [tuple(color) for color in data["palette"]],
                    [int(pixel) for pixel in data["pixels"]],
                    int(data.get("card_id", 0xC0DE)), int(data.get("width", 32)),
                    int(data.get("height", 32)), copy.deepcopy(data.get("animation")))
                card.validate()
                image = QImage(card.width, card.height, QImage.Format.Format_RGB32)
                for index, color_index in enumerate(card.pixels):
                    image.setPixelColor(
                        index % card.width, index // card.width,
                        QColor(*(int(channel) * 17 for channel in card.palette[color_index])))
                title = path.stem.replace("-", " ").title()
                item = QListWidgetItem(title)
                item.setIcon(QIcon(card_art(image, title, "Starter card", QSize(190, 238))))
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                gallery.addItem(item)
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                continue
        gallery.setCurrentRow(0)
        layout.addWidget(gallery, 1)
        note = QLabel("Choose a design to personalize. It will open as a new, unsaved card.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(dialog.reject)
        use_card = QPushButton("Use this card"); use_card.setObjectName("primaryAction")
        use_card.clicked.connect(dialog.accept)
        buttons.addWidget(cancel); buttons.addWidget(use_card)
        layout.addLayout(buttons)
        dialog.resize(720, 520)
        if dialog.exec() != QDialog.DialogCode.Accepted or gallery.currentItem() is None:
            return
        self._load_starter_card(Path(gallery.currentItem().data(Qt.ItemDataRole.UserRole)))

    def _load_starter_card(self, path):
        if self.editor_dialog.isVisible() and not self.confirm_editor_close():
            return False
        try:
            data = json.loads(path.read_text())
            pixels = [int(pixel) for pixel in data["pixels"]]
            card = modem.Card(
                self.station_call.text().strip().upper() or "N0CALL",
                self.station_grid.text().strip().upper() or "AA00",
                [tuple(color) for color in data["palette"]], pixels,
                self._new_card_id(), int(data.get("width", 32)),
                int(data.get("height", 32)))
            card.validate()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, "Couldn't open starter card", str(exc))
            return False
        self.card = card
        self.card_path = None
        self.animation_author.reset()
        self.canvas.set_card(card, data.get("base_pixels", pixels), data.get("stamp_layers", []))
        self._sync_editor_controls()
        self.call.setText(card.callsign)
        self.grid.setText(card.grid)
        self._set_editor_format(card)
        self.status.setText(f"Starter card · {path.stem.replace('-', ' ').title()}")
        self.editor_baseline = self.editor_signature()
        self._set_editor_title("Create from starter card")
        self.editor_dialog.exec()
        self.editor_baseline = self.editor_signature()
        self._set_editor_title("Create or edit card")
        self.refresh_card_library()
        return True

    def open_selected_library_card(self):
        item, _widget = self._selected_library_item()
        if item:
            try:
                self._load_card_path(Path(item.data(Qt.ItemDataRole.UserRole)))
                self.editor_baseline = self.editor_signature()
                self._update_editor_saved_state()
                self.editor_dialog.exec()
                self.refresh_card_library()
            except (OSError, ValueError, KeyError) as exc:
                QMessageBox.warning(self, "Couldn't open card", str(exc))

    def _open_library_item(self):
        item, _widget = self._selected_library_item()
        if item is None:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        if item.data(Qt.ItemDataRole.UserRole + 1) == "starter":
            self._load_starter_card(path)
        else:
            self.open_selected_library_card()

    def delete_library_card(self):
        item, widget = self._selected_library_item()
        if not item: return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        if item.data(Qt.ItemDataRole.UserRole + 1) == "starter":
            return
        section = "Sent cards" if path.parent.name == "sent-cards" else "My Cards"
        if QMessageBox.question(self, "Delete card", f"Delete {item.text().splitlines()[0]} from {section}?") == QMessageBox.StandardButton.Yes:
            path.unlink(missing_ok=True); self.refresh_card_library()

    def _selected_library_item(self):
        widget = getattr(self, "active_library_list", self.library_list)
        item = widget.currentItem()
        return (item, widget) if item is not None and not item.isHidden() else (None, None)

    def _activate_library_list(self, widget):
        self.active_library_list = widget

    def duplicate_library_card(self):
        item, _source_widget = self._selected_library_item()
        if not item or item.data(Qt.ItemDataRole.UserRole + 1) == "starter":
            return
        source = Path(item.data(Qt.ItemDataRole.UserRole))
        try:
            data = json.loads(source.read_text())
            modem.Card(data["callsign"], data["grid"], [tuple(c) for c in data["palette"]],
                       data["pixels"], data.get("card_id", 0xC0DE),
                       data.get("width", 32), data.get("height", 32), copy.deepcopy(data.get("animation"))).validate()
            stem = f"{source.stem}-copy"
            target = source.with_name(stem + source.suffix)
            suffix = 2
            while target.exists():
                target = source.with_name(f"{stem}-{suffix}{source.suffix}")
                suffix += 1
            data["card_id"] = self._new_card_id()
            target.write_text(json.dumps(data, indent=2) + "\n")
            self.refresh_card_library()
            self.library_filter.setCurrentIndex(self.library_filter.findData(
                "sent" if target.parent == DATA / "sent-cards" else "my"))
            self.library_search.clear()
            for row in range(self.library_list.count()):
                candidate = self.library_list.item(row)
                if Path(candidate.data(Qt.ItemDataRole.UserRole)) == target:
                    candidate.setSelected(True)
                    self.library_list.setCurrentItem(candidate)
                    break
            self.library_status.setText(f"Created {target.stem}; the original is unchanged")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, "Couldn't duplicate card", str(exc))

    def link_library_animation(self):
        item, _ = self._selected_library_item()
        if item is None:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        if path.parent != DATA / "cards":
            QMessageBox.information(self, "Choose a card in My Cards",
                                    "Open or copy this card into My Cards before making it frame 2.")
            return
        try:
            second = json.loads(path.read_text())
            choices = []
            for candidate in sorted((DATA / "cards").glob("*.json")):
                if candidate == path:
                    continue
                try:
                    first = json.loads(candidate.read_text())
                    if (str(first.get("callsign", "")).upper() != str(second.get("callsign", "")).upper()
                            or first.get("card_id") == second.get("card_id") or first.get("animation")
                            or (first.get("width", 32), first.get("height", 32)) !=
                            (second.get("width", 32), second.get("height", 32))):
                        continue
                    choices.append((candidate, first, artwork_digest(first)))
                except (OSError, ValueError, TypeError, KeyError, IndexError):
                    continue
            if not choices:
                raise ValueError("Save another card with the same callsign and dimensions, a different card ID, and no animation reference first.")
            dialog = QDialog(self); dialog.setWindowTitle("Make a two-frame animation")
            layout = QVBoxLayout(dialog)
            help_text = QLabel(f"Frame 2: {path.stem}\nChoose the first card. Send each card normally; receivers animate after both are verified.")
            help_text.setWordWrap(True); layout.addWidget(help_text)
            form = QFormLayout(); selector = QComboBox()
            for candidate, first, digest in choices:
                selector.addItem(f"{candidate.stem} · {int(first['card_id']):04X}")
            form.addRow("Frame 1", selector)
            duration = QSpinBox(); duration.setRange(100, 10000); duration.setSingleStep(100)
            duration.setSuffix(" ms per frame")
            current = second.get("animation") or {}
            duration.setValue(current.get("frame_ms", 500)); form.addRow("Timing", duration)
            for i, (_, first, _) in enumerate(choices):
                if first['card_id'] == current.get('first_card_id'):
                    selector.setCurrentIndex(i)
            layout.addLayout(form)
            preview = QLabel(); preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setFixedSize(320, 220); layout.addWidget(preview, 0, Qt.AlignmentFlag.AlignCenter)
            timer = QTimer(dialog); state = [0]
            def draw_preview():
                first = choices[selector.currentIndex()][1]
                selected = first if state[0] == 0 else second
                image = self.card_image({"card": selected, "pixels": selected['pixels']})
                if image is not None:
                    preview.setPixmap(QPixmap.fromImage(image).scaled(200, 200, Qt.AspectRatioMode.KeepAspectRatio))
                preview.setAccessibleName(f"Animation preview · frame {state[0]+1}")
                state[0] ^= 1
            timer.timeout.connect(draw_preview)
            duration.valueChanged.connect(timer.setInterval)
            selector.currentIndexChanged.connect(lambda _: draw_preview())
            timer.start(duration.value()); draw_preview()
            actions = QHBoxLayout(); save = QPushButton("Save animation reference")
            cancel = QPushButton("Cancel"); save.clicked.connect(dialog.accept); cancel.clicked.connect(dialog.reject)
            actions.addWidget(save); actions.addWidget(cancel); layout.addLayout(actions)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            _, first, digest = choices[selector.currentIndex()]
            second['animation'] = validate_link(dict(first_card_id=int(first['card_id']),
                first_digest=digest, frame_ms=duration.value()), int(second['card_id']))
            if current and current != second['animation']:
                second['card_id'] = self._new_card_id()
            self._write_library_animation(path, second)
            self.library_status.setText("Animation saved · send frame 1 and frame 2 normally, in either order")
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            QMessageBox.warning(self, "Couldn't link animation", str(exc))

    def _write_library_animation(self, path, data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(data, indent=2) + "\n")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if self.card_path == path:
            self.card.animation = copy.deepcopy(data.get('animation'))
            self.card.card_id = data['card_id']
            self._update_editor_saved_state()
        self.refresh_card_library()

    def unlink_library_animation(self):
        item, _ = self._selected_library_item()
        if item is None:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        if path.parent != DATA / "cards":
            return
        try:
            data = json.loads(path.read_text())
            data.pop('animation', None)
            self._write_library_animation(path, data)
            self.library_status.setText("Animation reference removed · this is now an ordinary card")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Couldn't remove animation reference", str(exc))

    def _load_card_path(self, path):
        d = json.loads(path.read_text())
        self.animation_author.reset()
        self.card = modem.Card(d["callsign"], d["grid"], [tuple(c) for c in d["palette"]], d["pixels"], d.get("card_id", 0xC0DE), d.get("width", 32), d.get("height", 32), copy.deepcopy(d.get("animation")))
        self.card = normalize_card_palette(self.card)
        self.card.validate(); self.card_path = path
        self.canvas.set_card(self.card, d.get("base_pixels", self.card.pixels), d.get("stamp_layers", []))
        self._sync_editor_controls()
        self.call.setText(self.card.callsign); self.grid.setText(self.card.grid)
        self._set_editor_format(self.card)
        self.animation_author.load_pair(path, d, DATA / 'cards')

    def refresh_qso_log(self, *_):
        if not hasattr(self, "log_gallery"): return
        self.log_gallery.clear()
        try:
            self.qso_log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            message = f"Could not open QSO log folder: {exc}"
            self.log_status.setText(message)
            self._update_export_buttons()
            if hasattr(self, "exchange_log"):
                self.exchange_log.append(message)
            if hasattr(self, "rx_status"):
                self.rx_status.setText(message)
            return
        entries = []
        query = self.log_search.text().strip().casefold()
        for path in self.qso_log_dir.glob("*.json"):
            try:
                entry = json.loads(path.read_text())
                if not isinstance(entry, dict): continue
                by_call_view = (getattr(self, "log_deck_view", None) is not None and
                                self.log_deck_view.currentData() == "received_by_call")
                if (query and not by_call_view and query not in
                        (str(entry.get("peer_callsign", "")) + " " +
                         str(entry.get("peer_grid", ""))).casefold()):
                    continue
                entries.append((path, entry))
            except (OSError, ValueError): continue
        if getattr(self, "log_deck_view", None) is not None and self.log_deck_view.currentData() == "received_by_call":
            decks = self._aggregate_received_decks(entries)
            if query:
                decks = {call: entry for call, entry in decks.items()
                         if query in (call + " " + str(entry.get("peer_grid", ""))).casefold()}
            display_entries = [(f"received-callsign:{call}", entry) for call, entry in decks.items()]
        else:
            display_entries = [(str(path), entry) for path, entry in entries]
        sort = self.log_sort.currentIndex()
        display_entries.sort(key=lambda pair: str(pair[1].get(
            "peer_callsign" if sort == 2 else
            ("latest_received_at" if sort == 0 and pair[0].startswith("received-callsign:") else
             "earliest_received_at" if pair[0].startswith("received-callsign:") else "started_at"), "")),
            reverse=sort == 0)
        for source, entry in display_entries:
            events = deck_events(entry)
            image = next((image for _direction, card in events if (image := self.card_image(card)) is not None), None)
            call = str(entry.get("peer_callsign", "Unknown"))
            started = str(entry.get("started_at", ""))
            status = str(entry.get("status", "Saved"))
            count = len(events)
            label = (f"{call}\n{count} received card{'s' if count != 1 else ''} · all time"
                     if source.startswith("received-callsign:") else
                     f"{started}\n{status} · {count} card{'s' if count != 1 else ''}")
            item = QListWidgetItem(label)
            deck_kind = "RECEIVED ARCHIVE" if source.startswith("received-callsign:") else "CONTACT DECK"
            item.setIcon(QIcon(card_art(image, call, str(entry.get("peer_grid", "")) + " · " + deck_kind, QSize(220, 275), stacked=True)))
            item.setData(Qt.ItemDataRole.UserRole, source)
            item.setToolTip(f"Open {call}'s received-card archive" if source.startswith("received-callsign:")
                            else f"Open {call}'s contact deck")
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                (f"All received cards from {call} · {entry.get('peer_grid') or 'grid unknown'} · "
                 if source.startswith("received-callsign:") else
                 f"Contact with {call} · {entry.get('peer_grid') or 'grid unknown'} · ") +
                f"{started or 'date unknown'} · {status} · {count} card"
                f"{'s' if count != 1 else ''}")
            self.log_gallery.addItem(item)
        self.log_status.setText(
            (f"{len(display_entries)} callsign archive{'s' if len(display_entries) != 1 else ''}"
             if self.log_deck_view.currentData() == "received_by_call" else
             f"{len(display_entries)} contact deck{'s' if len(display_entries) != 1 else ''}")
            if display_entries else "")
        self.log_clear_button.setVisible(bool(query))
        if display_entries:
            self.contact_stack.setCurrentWidget(self.contact_stack.widget(0))
        else:
            self.contact_empty_operate_button.setVisible(
                not query and self.log_deck_view.currentData() == "sessions")
            if self.log_deck_view.currentData() == "received_by_call":
                self.contact_empty_title.setText(
                    "No matching received cards" if query else "No received cards yet")
                self.contact_empty_copy.setText(
                    "Received card history is grouped by station callsign. Try another search or switch to contact sessions.")
            else:
                self.contact_empty_title.setText(
                    "No matching contacts" if query else "No contacts yet")
                self.contact_empty_copy.setText(
                    "Try another callsign or grid, or clear your search." if query else
                    "Contacts appear here after you call CQ or reply and exchange cards.")
            self.contact_stack.setCurrentWidget(self.contact_empty_page)
        self._update_export_buttons()

    @staticmethod
    def _received_archive_key(report):
        card = report.get("card") or {}
        content = card.get("image_id", card.get("image_crc32"))
        if content is None and report.get("pixels") is not None:
            content = hashlib.blake2s(
                json.dumps(report.get("pixels"), separators=(",", ":")).encode(),
                digest_size=8).hexdigest()
        return (str(card.get("callsign", "")).upper(), card.get("card_id"),
                card.get("message_type", "card"), content)

    def _aggregate_received_decks(self, entries):
        grouped = {}
        for _path, entry in entries:
            for report in entry.get("received_cards", []):
                card = report.get("card") or {}
                call = str(card.get("callsign") or entry.get("peer_callsign") or "").strip().upper()
                if not call:
                    continue
                group = grouped.setdefault(call, {"peer_callsign": call,
                    "peer_grid": card.get("grid") or entry.get("peer_grid", ""),
                    "started_at": report.get("logged_at") or entry.get("started_at", ""),
                    "latest_received_at": report.get("logged_at") or entry.get("started_at", ""),
                    "status": "Received archive", "deck_type": "received_archive",
                    "sent_cards": [], "received_cards": []})
                existing = next((item for item in group["received_cards"]
                                if same_received_card(item, report)), None)
                logged_at = report.get("logged_at") or entry.get("started_at", "")
                if not group["started_at"] or (logged_at and logged_at < group["started_at"]):
                    group["started_at"] = logged_at
                if logged_at > group["latest_received_at"]:
                    group["latest_received_at"] = logged_at
                if existing is None:
                    grouped_report = copy.deepcopy(report)
                    grouped_report["logged_at"] = logged_at
                    grouped[call]["received_cards"].append(grouped_report)
                else:
                    attempts = int(existing.get("receive_attempts", 1)) + int(report.get("receive_attempts", 1))
                    if _receive_report_rank(report) > _receive_report_rank(existing):
                        received_at = existing.get("logged_at")
                        existing.update(copy.deepcopy(report))
                        existing["logged_at"] = received_at
                    existing["receive_attempts"] = attempts
        for group in grouped.values():
            group["received_cards"].sort(key=lambda item: item.get("logged_at", ""))
            for order, report in enumerate(group["received_cards"], 1):
                report["order"] = order
            group["status"] = f"All received cards · {len(group['received_cards'])} unique"
        return grouped

    def _load_qso_deck_source(self, source):
        source = str(source)
        if source.startswith("received-callsign:"):
            call = source.split(":", 1)[1]
            try:
                entries = []
                for path in self.qso_log_dir.glob("*.json"):
                    try:
                        entry = json.loads(path.read_text())
                        if isinstance(entry, dict):
                            entries.append((path, entry))
                    except (OSError, ValueError):
                        continue
                return self._aggregate_received_decks(entries).get(call), None
            except OSError:
                return None, None
        path = Path(source)
        return json.loads(path.read_text()), path

    def _update_export_buttons(self, *_):
        if hasattr(self, "export_card_button"):
            item, _widget = self._selected_library_item()
            selected = item is not None
            kind = item.data(Qt.ItemDataRole.UserRole + 1) if selected else None
            saved = selected and kind in {"my", "sent"}
            self.open_library_button.setVisible(selected)
            self.open_library_button.setText("Use starter card" if kind == "starter" else "Open selected")
            self.export_card_button.setVisible(saved)
            self.copy_card_button.setVisible(saved)
            self.more_card_button.setVisible(saved)
            self.export_card_button.setEnabled(saved)
            self.copy_card_button.setEnabled(saved)
        if hasattr(self, "export_qso_button"):
            selected = (hasattr(self, "log_gallery") and
                        self.log_gallery.currentItem() is not None)
            self.export_qso_button.setVisible(selected)
            self.copy_qso_button.setVisible(selected)
            self.export_qso_button.setEnabled(selected)
            self.copy_qso_button.setEnabled(selected)

    def copy_selected_card(self):
        item, _widget = self._selected_library_item()
        if item is None:
            return
        try:
            data = json.loads(Path(item.data(Qt.ItemDataRole.UserRole)).read_text())
            card = modem.Card(data["callsign"], data.get("grid", ""),
                              [tuple(color) for color in data["palette"]], data["pixels"],
                              data.get("card_id", 0xC0DE), data.get("width", 32), data.get("height", 32), copy.deepcopy(data.get("animation")))
            card.validate()
            image = self.card_image({"card": data, "pixels": data["pixels"]})
            QApplication.clipboard().setImage(image)
            self.library_status.setText("Card image copied · paste into an image editor or message")
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't copy card", str(exc))

    def copy_selected_qso(self):
        item = self.log_gallery.currentItem()
        if item is None:
            return
        try:
            entry, _path = self._load_qso_deck_source(item.data(Qt.ItemDataRole.UserRole))
            if entry is None:
                return
            QApplication.clipboard().setImage(deck_image(entry, self.card_image))
            self.log_status.setText("QSO contact sheet copied · paste into an image editor or message")
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't copy QSO", str(exc))

    def export_selected_card(self):
        item, _widget = self._selected_library_item()
        if item is None:
            return
        source = Path(item.data(Qt.ItemDataRole.UserRole))
        try:
            data = json.loads(source.read_text())
            card = modem.Card(str(data["callsign"]), str(data.get("grid", "")),
                              [tuple(map(int, color)) for color in data["palette"]],
                              list(map(int, data["pixels"])), int(data.get("card_id", 0xC0DE)),
                              int(data.get("width", 32)), int(data.get("height", 32)), copy.deepcopy(data.get("animation")))
            card.validate()
            default_name = f"{source.stem}.png"
            target_name, file_filter = QFileDialog.getSaveFileName(
                self, "Export card", str(DATA / "exports" / default_name),
                "PNG image (*.png);;Pixel QSO card (*.json)")
            if not target_name:
                return
            target = Path(target_name)
            if target.resolve() == source.resolve():
                QMessageBox.warning(self, "Couldn't export card", "Choose a different file from the saved card.")
                return
            if "PNG" in file_filter:
                image = QImage(card.width, card.height, QImage.Format.Format_RGB32)
                for index, palette_index in enumerate(card.pixels):
                    image.setPixelColor(index % card.width, index // card.width,
                                        QColor(*(channel * 17 for channel in card.palette[palette_index])))
                if target.suffix.lower() != ".png":
                    target = target.with_suffix(".png")
                target.parent.mkdir(parents=True, exist_ok=True)
                if not image.save(str(target), "PNG"):
                    raise OSError("Qt could not write the PNG image")
            else:
                if target.suffix.lower() != ".json":
                    target = target.with_suffix(".json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
            self.library_status.setText(f"Exported {target.name}")
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't export card", str(exc))

    def export_selected_qso(self):
        item = self.log_gallery.currentItem() if hasattr(self, "log_gallery") else None
        if item is None:
            return
        self._export_qso_archive(item.data(Qt.ItemDataRole.UserRole))

    def _export_qso_archive(self, source, dialog_parent=None):
        dialog_parent = dialog_parent or self
        try:
            entry, source_path = self._load_qso_deck_source(source)
            if entry is None:
                raise ValueError("No received cards were found for this callsign.")
            callsign = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                               for ch in str(entry.get("peer_callsign", "Unknown")))
            date = "".join(ch for ch in str(entry.get("started_at", ""))[:10]
                           if ch.isalnum() or ch == "-")
            received_archive = entry.get("deck_type") == "received_archive"
            prefix = "received" if received_archive else "qso"
            target_name, file_filter = QFileDialog.getSaveFileName(
                dialog_parent, "Export received-card archive" if received_archive else "Export QSO",
                str(DATA / "exports" / f"{prefix}-{callsign}-{date}.png"),
                "PNG contact sheet (*.png);;Pixel QSO contact archive (*.json)")
            if not target_name:
                return None
            target = Path(target_name)
            if source_path is not None and target.resolve() == source_path.resolve():
                QMessageBox.warning(dialog_parent, "Couldn't export QSO", "Choose a different file from the saved contact archive.")
                return None
            if "PNG" in file_filter:
                target = target.with_suffix(".png")
                target.parent.mkdir(parents=True, exist_ok=True)
                if not deck_image(entry, self.card_image).save(str(target), "PNG"):
                    raise OSError("Qt could not write the PNG contact sheet")
            else:
                target = target.with_suffix(".json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(entry, indent=2) + "\n")
            self.log_status.setText(f"Exported {'received archive' if received_archive else 'QSO'} to {target.name}")
            return target
        except Exception as exc:
            QMessageBox.warning(dialog_parent, "Couldn't export QSO", str(exc))
            return None

    def show_log_deck(self, item=None, *_):
        item = item or self.log_gallery.currentItem()
        if item is None: return
        try:
            entry, path = self._load_qso_deck_source(item.data(Qt.ItemDataRole.UserRole))
            if entry is None:
                self.log_status.setText("No received cards are saved for this callsign yet.")
                return
        except (OSError, ValueError): return
        if not deck_events(entry):
            self.log_status.setText("This contact has no saved image cards yet."); return
        entry = {**entry, 'animation_candidates': self._saved_animation_candidates()}
        provenance = path if path is not None else Path(
            f"received-{entry.get('peer_callsign', 'unknown')}")
        add_received = lambda card: self.add_received_card_to_library(card, provenance)
        deck = DeckDialog(
            entry, self.card_image, add_received, self,
            export_contact=lambda: self._export_qso_archive(
                str(path) if path is not None else
                f"received-callsign:{entry.get('peer_callsign', '')}", deck),
            export_card=lambda direction, report: self._export_deck_card_png(
                direction, report, deck))
        deck.exec()

    def _export_deck_card_png(self, direction, report, dialog_parent=None):
        dialog_parent = dialog_parent or self
        card = report.get("card") or {}
        callsign = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                           for ch in str(card.get("callsign", "Unknown")))
        card_id = card.get("card_id")
        id_label = f"{int(card_id) & 0xffff:04X}" if card_id is not None else "unknown-id"
        stage = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                        for ch in str(card.get("message_type", "card")))
        name, _ = QFileDialog.getSaveFileName(
            dialog_parent, "Export selected card PNG",
            str(DATA / "exports" / f"card-{callsign}-{id_label}-{stage}.png"),
            "PNG image (*.png)")
        if not name:
            return None
        target = Path(name).with_suffix(".png")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            image = single_card_image(direction, report, self.card_image)
            if not image.save(str(target), "PNG"):
                raise OSError("Qt could not write the card PNG")
            self.log_status.setText(f"Exported card to {target.name}")
            return target
        except Exception as exc:
            QMessageBox.warning(dialog_parent, "Couldn't export card", str(exc))
            return None

    def add_received_card_to_library(self, report, qso_path):
        source = report.get("card") or {}
        image = self.card_image(report)
        if image is None or image.isNull():
            QMessageBox.warning(self, "Couldn't save card", "This received card has no usable image data.")
            return False
        source_call = str(source.get("callsign") or "unknown").upper()
        source_grid = str(source.get("grid") or "")
        source_id = int(source.get("card_id") or 0) & 0xFFFF
        order = int(report.get("order") or 0)
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in source_call)
        target_dir = DATA / "cards"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"from-{slug}-{source_id:04x}-{order}.json"
        palette = [tuple(int(channel) for channel in color) for color in source.get("palette", PALETTE)]
        if not palette or any(len(color) != 3 for color in palette):
            palette = list(PALETTE)
        elif any(channel > 15 for color in palette for channel in color):
            palette = [tuple(min(15, round(channel / 17)) for channel in color) for color in palette]
        if len(palette) not in (2, 8, 16, 32):
            palette = list(PALETTE)
        width = int(source.get("width") or image.width())
        height = int(source.get("height") or image.height())
        width, height = max(1, min(width, image.width())), max(1, min(height, image.height()))
        pixels = []
        palette_rgb = [tuple(channel * 17 for channel in color) for color in palette]
        for y in range(height):
            for x in range(width):
                color = image.pixelColor(x, y)
                rgb = (color.red(), color.green(), color.blue())
                pixels.append(min(range(len(palette_rgb)), key=lambda i: sum((rgb[k]-palette_rgb[i][k])**2 for k in range(3))))
        fingerprint = hashlib.blake2s(bytes(pixels), digest_size=4).hexdigest()
        target = target_dir / f"from-{slug}-{source_id:04x}-{order}-{fingerprint}.json"
        if target.exists():
            self.tabs.setCurrentIndex(1)
            self.library_status.setText(f"{target.stem} is already in My Cards")
            return "already"
        callsign = self.station_call.text().strip().upper() or "N0CALL"
        grid = self.station_grid.text().strip().upper() or "AA00"
        card = modem.Card(callsign, grid, palette, pixels, self._new_card_id(), width, height)
        try:
            card.validate()
        except ValueError as exc:
            QMessageBox.warning(self, "Couldn't save card", str(exc))
            return False
        data = {"callsign": card.callsign, "grid": card.grid, "palette": card.palette,
                "pixels": card.pixels, "card_id": card.card_id, "width": card.width,
                "height": card.height, "base_pixels": card.pixels, "stamp_layers": [],
                "copied_from": {"callsign": source_call, "grid": source_grid,
                                "message_type": source.get("message_type", "card"),
                                "qso": Path(qso_path).name}}
        try:
            target.write_text(json.dumps(data, indent=2) + "\n")
        except OSError as exc:
            QMessageBox.warning(self, "Couldn't save card", str(exc))
            return False
        self.refresh_card_library()
        self.library_filter.setCurrentIndex(self.library_filter.findData("my"))
        self.library_search.clear()
        self.tabs.setCurrentIndex(1)
        for row in range(self.library_list.count()):
            item = self.library_list.item(row)
            if Path(item.data(Qt.ItemDataRole.UserRole)) == target:
                self._activate_library_list(self.library_list)
                self.library_list.setCurrentItem(item)
                item.setSelected(True)
                break
        self.library_status.setText(f"Added {target.stem} · copied from {source_call}")
        return "added"

    def make_card_tab(self):
        w = QWidget(); outer = QHBoxLayout(w)
        self.editor_splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(self.editor_splitter)
        drawing_widget = QWidget(); drawing = QVBoxLayout(drawing_widget)
        drawing.setContentsMargins(0, 0, 12, 0)
        self.editor_splitter.addWidget(drawing_widget)
        tools = QGridLayout(); self.tool_buttons = {}
        self.tool_group = QButtonGroup(self); self.tool_group.setExclusive(True)
        for title, tool in (("Draw", "draw"), ("Erase", "erase"), ("Fill", "fill"), ("Pick", "pick"), ("Move stamp", "move"), ("Box select", "select")):
            button = QPushButton(title); button.setCheckable(True)
            button.setAccessibleName(f"{title} tool")
            button.setAccessibleDescription(CardCanvas.TOOL_HELP[tool])
            button.clicked.connect(lambda _checked=False, value=tool: self.set_editor_tool(value))
            self.tool_group.addButton(button); self.tool_buttons[tool] = button
            index = len(self.tool_buttons) - 1
            tools.addWidget(button, index // 3, index % 3)
        self.tool_buttons["draw"].setChecked(True)
        drawing.addLayout(tools)
        self.canvas = CardCanvas(self.card)
        self.canvas.setToolTip(
            "Keyboard: focus the canvas; arrows or hjkl move the cursor; v starts/ends a "
            "rectangular selection, y copies it, d deletes it (or the pixel under the cursor), "
            "and p places an image from the clipboard at the cursor. Uppercase HJKL nudges "
            "a selection. f fills a selection or the connected region under the cursor. "
            "0/A jump to row start, $/a to row end, gg/G to column ends, and a number "
            "repeats movement. Type :wq then Enter to save and close, or :q then Enter "
            "to close with the usual unsaved-change prompt. "
            "Hold Space or press i to draw while moving; Escape cancels i mode. "
            "n/N cycles colors forward/backward, u undoes, U redoes, x deletes the pixel, "
            "and Enter applies "
            "Draw, Erase, Fill, or Pick. In Move stamp mode, Space/Enter selects a stamp, "
            "arrows move it, and Escape deselects. Mouse: drag stamps; hover a placed "
            "stamp to reveal its resize and remove controls.")
        drawing.addWidget(self.canvas, 1)
        history = QHBoxLayout()
        self.undo_button = QPushButton("↶ Undo"); self.undo_button.clicked.connect(self.canvas.undo)
        self.redo_button = QPushButton("↷ Redo"); self.redo_button.clicked.connect(self.canvas.redo)
        self.undo_button.setAccessibleName("Undo card edit")
        self.undo_button.setAccessibleDescription("Undo the last card edit. Keyboard shortcut: Ctrl+Z.")
        self.undo_button.setToolTip("Undo the last card edit · Ctrl+Z")
        self.redo_button.setAccessibleName("Redo card edit")
        self.redo_button.setAccessibleDescription("Redo the last undone card edit. Keyboard shortcut: Ctrl+Shift+Z or Ctrl+Y.")
        self.redo_button.setToolTip("Redo the last undone card edit · Ctrl+Shift+Z")
        history.addWidget(self.undo_button); history.addWidget(self.redo_button); history.addStretch()
        grid = QCheckBox("Grid"); grid.setChecked(True)
        grid.toggled.connect(lambda checked: (setattr(self.canvas, "show_grid", checked), self.canvas.update()))
        history.addWidget(grid); drawing.addLayout(history)
        for key, callback in (("Ctrl+Z", self.canvas.undo), ("Ctrl+Shift+Z", self.canvas.redo), ("Ctrl+Y", self.canvas.redo)):
            shortcut = QShortcut(QKeySequence(key), w); shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut); shortcut.activated.connect(callback)

        sidebar = QWidget(); side = QVBoxLayout(sidebar); side.setContentsMargins(4, 0, 4, 0)
        sidebar_scroll = ContentScrollArea(); sidebar_scroll.setWidgetResizable(True); sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        sidebar_scroll.setWidget(sidebar); sidebar_scroll.setMinimumWidth(400)
        self.editor_splitter.addWidget(sidebar_scroll)
        self.editor_splitter.setStretchFactor(0, 3); self.editor_splitter.setStretchFactor(1, 2)
        self._configure_workspace_splitter(self.editor_splitter, "editor", [680, 380])
        form = QFormLayout()
        self.call = QLineEdit(self.card.callsign); self.grid = QLineEdit(self.card.grid)
        self.call.textChanged.connect(self._update_card_identity_hint)
        self.grid.textChanged.connect(self._update_card_identity_hint)
        self.call.textChanged.connect(self._update_editor_saved_state)
        self.grid.textChanged.connect(self._update_editor_saved_state)
        form.addRow("Call sign", self.call); form.addRow("Grid", self.grid)
        side.addLayout(form)
        self.card_identity_hint = QLabel()
        self.card_identity_hint.setObjectName("supportingText")
        self.card_identity_hint.setWordWrap(True)
        self.card_identity_hint.setAccessibleName("Card identity format guidance")
        side.addWidget(self.card_identity_hint)
        self._update_card_identity_hint()
        card_identity_hint = QLabel(
            "This identity travels with the card. New cards start with Station settings; "
            "editing it here does not change your station identity.")
        card_identity_hint.setObjectName("supportingText")
        card_identity_hint.setAccessibleName("Card identity help")
        card_identity_hint.setWordWrap(True)
        side.addWidget(card_identity_hint)
        self.editor_format_expanded = False
        self.editor_format_toggle = QPushButton()
        self.editor_format_toggle.setAccessibleDescription(
            "Show or hide exact canvas dimensions and palette size.")
        self.editor_format_toggle.clicked.connect(self._toggle_editor_format)
        side.addWidget(self.editor_format_toggle)
        self.editor_format_widget = QWidget()
        format_form = QFormLayout()
        self.canvas_size_combo = QComboBox()
        for value in (16, 32, 64): self.canvas_size_combo.addItem(f"{value} × {value}", f"{value}x{value}")
        self.color_count_combo = QComboBox()
        for value in (2, 8, 16, 32): self.color_count_combo.addItem(f"{value} colors", value)
        self.canvas_size_combo.currentIndexChanged.connect(self.change_card_format)
        self.color_count_combo.currentIndexChanged.connect(self.change_card_format)
        format_form.addRow("Canvas", self.canvas_size_combo); format_form.addRow("Palette", self.color_count_combo)
        self.editor_format_widget.setLayout(format_form)
        self.editor_format_widget.setVisible(False)
        side.addWidget(self.editor_format_widget)
        self._update_editor_format_disclosure()
        self.paint_color_combo = QComboBox(); self.paint_color_combo.currentIndexChanged.connect(self.select_color)
        self.paint_color_combo.setVisible(False)
        self.palette_widget = QWidget(); self.palette_widget.setFixedWidth(146); self.palette_layout = QGridLayout(self.palette_widget)
        self.palette_layout.setContentsMargins(0, 0, 0, 0); self.palette_layout.setSpacing(6)
        side.addWidget(self.palette_widget)
        self._set_editor_format(self.card)
        stamps_heading = QLabel("My stamps")
        stamps_heading.setObjectName("sectionTitle")
        side.addWidget(stamps_heading)
        self.stamp_list = StampShelf(); self.stamp_list.setMaximumHeight(180)
        self.stamp_list.setViewMode(QListWidget.ViewMode.IconMode); self.stamp_list.setIconSize(QSize(48, 48))
        self.stamp_list.setGridSize(QSize(82, 78)); self.stamp_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.stamp_list.itemDoubleClicked.connect(lambda _item: self.stamp_selected())
        self.stamp_list.currentItemChanged.connect(lambda *_: self._sync_editor_controls())
        side.addWidget(self.stamp_list)
        self.stamp_shelf_hint = QLabel(
            "Select a saved stamp to place or edit it. Double-click places it on the card.")
        self.stamp_shelf_hint.setWordWrap(True)
        self.stamp_shelf_hint.setStyleSheet(semantic_text_style(padding="2px"))
        side.addWidget(self.stamp_shelf_hint)
        stamp_buttons = QHBoxLayout()
        make_stamp = QPushButton("Text / emoji"); make_stamp.clicked.connect(self.create_text_stamp); stamp_buttons.addWidget(make_stamp)
        qr_button = QPushButton("QR code…"); qr_button.clicked.connect(self.create_qr_card); stamp_buttons.addWidget(qr_button)
        draw_stamp = QPushButton("Draw stamp"); draw_stamp.clicked.connect(self.draw_stamp); stamp_buttons.addWidget(draw_stamp)
        import_stamp = QPushButton("Import"); import_stamp.clicked.connect(self.import_stamp); stamp_buttons.addWidget(import_stamp)
        side.addLayout(stamp_buttons)
        self.stamp_actions_widget = QWidget()
        stamp_actions = QHBoxLayout(self.stamp_actions_widget)
        stamp_actions.setContentsMargins(0, 0, 0, 0)
        self.edit_stamp_button = QPushButton("Edit stamp…")
        self.edit_stamp_button.clicked.connect(self.edit_stamp)
        stamp_actions.addWidget(self.edit_stamp_button)
        self.delete_stamp_button = QPushButton("Delete saved stamp")
        self.delete_stamp_button.clicked.connect(self.remove_stamp)
        stamp_actions.addWidget(self.delete_stamp_button)
        side.addWidget(self.stamp_actions_widget)
        self.stamp_resize_controls = QWidget()
        stamp_size_row = QHBoxLayout(self.stamp_resize_controls)
        stamp_size_row.setContentsMargins(0, 0, 0, 0)
        self.placed_stamp_width = QSpinBox(); self.placed_stamp_width.setRange(1, 64); self.placed_stamp_width.setSuffix(" px")
        self.resize_stamp_button = QPushButton("Resize placed stamp")
        self.resize_stamp_button.clicked.connect(self.resize_placed_stamp)
        stamp_size_row.addWidget(self.placed_stamp_width); stamp_size_row.addWidget(self.resize_stamp_button)
        side.addWidget(self.stamp_resize_controls)
        self.place_stamp_button = QPushButton("Place selected stamp")
        self.place_stamp_button.clicked.connect(self.stamp_selected)
        side.addWidget(self.place_stamp_button)
        self.remove_placed_stamp_button = QPushButton("Remove placed stamp")
        self.remove_placed_stamp_button.clicked.connect(self.remove_placed_stamp)
        side.addWidget(self.remove_placed_stamp_button)
        self.convert_stamp_button = QPushButton("Convert stamp to pixels")
        self.convert_stamp_button.setToolTip("Merge the selected stamp and stamps beneath it into editable pixels. Undo restores stamps.")
        self.convert_stamp_button.clicked.connect(lambda: self.canvas.convert_stamp_to_pixels())
        side.addWidget(self.convert_stamp_button)
        self.save_selection_stamp_button = QPushButton("Convert selection to stamp")
        self.save_selection_stamp_button.clicked.connect(self.save_canvas_selection_stamp)
        side.addWidget(self.save_selection_stamp_button)
        self.listen_canvas_button = QPushButton("Listen in…")
        self.listen_canvas_button.clicked.connect(self.listen_to_canvas)
        side.addWidget(self.listen_canvas_button)
        self.load_stamps()
        buttons = QGridLayout()
        for index, (label, handler) in enumerate((("New", self.new_card), ("Open…", self.open_card), ("Save", self.save_card), ("Paste image", self.paste_card_image), ("Export WAV…", self.export_wav))):
            b = QPushButton(label); b.clicked.connect(handler); buttons.addWidget(b, index // 3, index % 3)
            if label == "Save":
                self.editor_save_button = b
                self.editor_save_button.setAccessibleDescription(
                    "Save the card after its call sign and grid meet the card format limits.")
        side.addLayout(buttons)
        self._update_card_identity_hint()
        side.addStretch()
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        side.addWidget(self.status)
        self.animation_author = AnimationAuthor(self)
        drawing.insertWidget(0, self.animation_author)
        side.insertWidget(0, self.animation_author.preview_panel)
        self.call.textChanged.connect(self.animation_author.changed)
        self.grid.textChanged.connect(self.animation_author.changed)
        self.canvas.image_dropped.connect(self.place_stamp_image)
        self.canvas.changed.connect(self._refresh_send_workspace)
        self.canvas.changed.connect(self._sync_editor_controls)
        self.canvas.changed.connect(self._update_editor_saved_state)
        self.canvas.placed_stamp_removed.connect(self._on_placed_stamp_removed)
        self._sync_editor_controls()
        return w

    def _on_placed_stamp_removed(self):
        self.status.setText("Placed stamp removed from the card.")
        self._sync_editor_controls()

    def set_editor_tool(self, tool):
        self.canvas.tool = tool
        if tool != "select":
            self.canvas.selection_rect = QRect(); self.canvas.selection_handle = QRect()
        self.canvas.update_accessible_description()
        self.tool_buttons[tool].setChecked(True)

    def _sync_editor_controls(self):
        self.undo_button.setEnabled(bool(self.canvas.undo_stack))
        self.redo_button.setEnabled(bool(self.canvas.redo_stack))
        self.tool_buttons[self.canvas.tool].setChecked(True)
        index = self.canvas.selected_layer
        selected = index is not None and index < len(self.canvas.stamp_layers)
        self.placed_stamp_width.setEnabled(selected); self.resize_stamp_button.setEnabled(selected)
        self.stamp_resize_controls.setVisible(selected)
        self.remove_placed_stamp_button.setVisible(selected)
        self.convert_stamp_button.setVisible(selected)
        self.save_selection_stamp_button.setVisible(not self.canvas.selection_rect.isNull() or selected)
        saved_stamp_selected = self.stamp_list.currentItem() is not None
        self.stamp_actions_widget.setVisible(saved_stamp_selected)
        self.place_stamp_button.setVisible(saved_stamp_selected)
        self.stamp_shelf_hint.setVisible(not saved_stamp_selected)
        self.edit_stamp_button.setEnabled(saved_stamp_selected)
        self.delete_stamp_button.setEnabled(saved_stamp_selected)
        if selected:
            self.placed_stamp_width.setMaximum(self.card.width)
            self.placed_stamp_width.setValue(self.canvas.stamp_layers[index]["width"])
        if hasattr(self, "canvas_size_combo"):
            self._set_editor_format(self.card)

    def select_color(self, index):
        if index >= 0:
            self.canvas.color_index = index
            if hasattr(self, "tool_buttons"): self.set_editor_tool("draw")

    def _set_editor_format(self, card):
        if not hasattr(self, "canvas_size_combo"):
            return
        with QSignalBlocker(self.canvas_size_combo), QSignalBlocker(self.color_count_combo):
            size = (card.width, card.height)
            size_key = f"{card.width}x{card.height}"
            index = self.canvas_size_combo.findData(size_key)
            if index < 0:
                self.canvas_size_combo.addItem(f"{card.width} × {card.height} (custom)", size_key)
                index = self.canvas_size_combo.count()-1
            self.canvas_size_combo.setCurrentIndex(index)
            self.color_count_combo.setCurrentIndex(max(0, self.color_count_combo.findData(len(card.palette))))
        self._update_editor_format_disclosure()
        self._refresh_paint_colors()

    def _update_editor_format_disclosure(self):
        if not hasattr(self, "editor_format_toggle"):
            return
        size = f"{self.card.width}×{self.card.height}"
        colors = f"{len(self.card.palette)} colors"
        arrow = "▾" if self.editor_format_expanded else "▸"
        self.editor_format_toggle.setText(
            f"Advanced card format · {size} · {colors} {arrow}")

    def _toggle_editor_format(self):
        self.editor_format_expanded = not self.editor_format_expanded
        self.editor_format_widget.setVisible(self.editor_format_expanded)
        self._update_editor_format_disclosure()

    def _refresh_paint_colors(self):
        if not hasattr(self, "palette_layout"): return
        state = (tuple(self.card.palette), self.canvas.color_index)
        if getattr(self, "_palette_state", None) == state: return
        self._palette_state = state
        while self.palette_layout.count():
            item = self.palette_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        for index, rgb in enumerate(self.card.palette):
            color = QColor(*(int(v)*17 for v in rgb)).name()
            button = QPushButton(); button.setFixedSize(32, 32)
            selected = index == self.canvas.color_index
            button.setAccessibleName(f"Palette color {index + 1}")
            button.setAccessibleDescription(
                f"{color}; {'selected' if selected else 'not selected'}")
            button.setToolTip(f"Color {index+1} · {color}")
            button.setStyleSheet(palette_swatch_style(color, selected=selected))
            button.clicked.connect(lambda _checked=False, value=index: (self.select_color(value), self._refresh_paint_colors()))
            self.palette_layout.addWidget(button, index//4, index%4)

    def change_card_format(self, *_):
        if not hasattr(self, "canvas_size_combo"):
            return
        size = str(self.canvas_size_combo.currentData() or "32x32").split("x", 1)
        width, height = int(size[0]), int(size[1])
        color_count = int(self.color_count_combo.currentData() or 8)
        palette_id = {8: 0, 16: 1, 32: 2, 2: 3}[color_count]
        reductions = []
        if width < self.card.width or height < self.card.height:
            reductions.append(
                "A smaller canvas resamples the artwork and may remove edge detail. "
                "Placed stamps will also be resized.")
        if color_count < len(self.card.palette):
            reductions.append(
                "A smaller palette maps colors to the nearest available colors.")
        if reductions:
            confirmation = QMessageBox(
                QMessageBox.Icon.Warning, "Reduce card detail?",
                "\n\n".join(reductions) +
                "\n\nYou can restore the previous format immediately with Ctrl+Z.",
                QMessageBox.StandardButton.NoButton, self.editor_dialog)
            reduce_button = confirmation.addButton(
                "Reduce detail", QMessageBox.ButtonRole.AcceptRole)
            keep_button = confirmation.addButton(
                "Keep current", QMessageBox.ButtonRole.RejectRole)
            confirmation.setDefaultButton(keep_button)
            confirmation.exec()
            if confirmation.clickedButton() is not reduce_button:
                self._set_editor_format(self.card)
                return
        self.canvas.change_format(width, height, list(modem.AVATAR_PALETTES[palette_id]))
        self._refresh_paint_colors()

    def create_qr_card(self):
        value, accepted = QInputDialog.getMultiLineText(
            self.editor_dialog, "Create QR card", "Text or URL to encode:")
        if not accepted:
            return
        try:
            from qr_support import make_qr_card
            qr_card = make_qr_card(value, self.card.callsign, self.card.grid,
                                   self.card.card_id, self.card.width, self.card.height)
        except (ImportError, ValueError) as exc:
            QMessageBox.warning(self.editor_dialog, "Couldn't create QR card", str(exc))
            return
        self.canvas.remember_edit()
        self.card.palette = qr_card.palette
        self.canvas.base_pixels = list(qr_card.pixels)
        self.card.pixels = list(qr_card.pixels)
        self.canvas.stamp_layers.clear()
        self.canvas.selected_layer = self.canvas.hover_layer = None
        self.canvas.color_index = 0
        self.canvas._render_layers()
        self._set_editor_format(self.card)
        self._sync_editor_controls()
        self.status.setText("QR card created · keep all QR modules inside the canvas")

    def read_received_qr(self):
        if self.receive_qr_image is None:
            return
        try:
            from qr_support import read_qr_image
            value = read_qr_image(self.receive_qr_image)
        except ImportError as exc:
            QMessageBox.warning(self, "QR reader unavailable", str(exc))
            return
        if value is None:
            QMessageBox.information(
                self, "No QR code found",
                "No readable QR code was found in this verified card.")
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("QR code contents")
        dialog.setText(value)
        dialog.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse |
                                       Qt.TextInteractionFlag.TextSelectableByKeyboard)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.exec()

    def load_stamps(self):
        folder = DATA / "stamps"; folder.mkdir(parents=True, exist_ok=True)
        # Seed each bundled stamp once, preserving user edits and deletions.
        seeded_path = folder / ".bundled-stamps.json"
        seeded = set(json.loads(seeded_path.read_text())) if seeded_path.exists() else set()
        for path in sorted(DEFAULT_STAMPS_PATH.glob("*.png")):
            if path.name in seeded:
                continue
            destination = folder / path.name
            if not destination.exists():
                destination.write_bytes(path.read_bytes())
            seeded.add(path.name)
        seeded_path.write_text(json.dumps(sorted(seeded)))
        self.stamp_list.clear()
        for path in sorted(folder.glob("*.png")):
            image = QImage(str(path))
            if image.isNull(): continue
            item = QListWidgetItem(f"{image.width()} × {image.height()}")
            item.setToolTip(path.stem)
            item.setIcon(QIcon(QPixmap.fromImage(image).scaled(36, 36, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
            item.setData(Qt.ItemDataRole.UserRole, str(path)); self.stamp_list.addItem(item)

    @staticmethod
    def trim_transparent_edges(image):
        """Crop transparent padding so the stamp's stored size matches its art."""
        if image.isNull() or not image.hasAlphaChannel():
            return image
        left, top = image.width(), image.height()
        right = bottom = -1
        for y in range(image.height()):
            for x in range(image.width()):
                if image.pixelColor(x, y).alpha() > 0:
                    left, top = min(left, x), min(top, y)
                    right, bottom = max(right, x), max(bottom, y)
        if right < left or bottom < top:
            return QImage()
        return image.copy(left, top, right-left+1, bottom-top+1)

    def create_text_stamp(self):
        text, ok = QInputDialog.getText(self, "Create stamp", "Enter text or emoji (callsigns use a crisp 5×7 pixel font):", QLineEdit.EchoMode.Normal, self.call.text().strip().upper())
        if not ok or not text.strip(): return
        normalized = text.strip().upper()[:10]
        image = self._pixel_text_image(normalized)
        path = DATA / "stamps" / f"stamp-{int(time.time()*1000)}.png"
        if not image.save(str(path), "PNG"):
            QMessageBox.warning(self, "Couldn't create stamp", "Could not save the rendered text or emoji."); return
        self.load_stamps()
        self.stamp_list.setCurrentRow(self.stamp_list.count()-1)
        self.status.setText(f"Added “{text}” to your stamp shelf.")

    @staticmethod
    def _pixel_text_image(text):
        glyphs = {
            "A":("01110","10001","10001","11111","10001","10001","10001"),
            "B":("11110","10001","10001","11110","10001","10001","11110"),
            "C":("01111","10000","10000","10000","10000","10000","01111"),
            "D":("11110","10001","10001","10001","10001","10001","11110"),
            "E":("11111","10000","10000","11110","10000","10000","11111"),
            "F":("11111","10000","10000","11110","10000","10000","10000"),
            "G":("01111","10000","10000","10111","10001","10001","01111"),
            "H":("10001","10001","10001","11111","10001","10001","10001"),
            "I":("11111","00100","00100","00100","00100","00100","11111"),
            "J":("00111","00010","00010","00010","10010","10010","01100"),
            "K":("10001","10010","10100","11000","10100","10010","10001"),
            "L":("10000","10000","10000","10000","10000","10000","11111"),
            "M":("10001","11011","10101","10101","10001","10001","10001"),
            "N":("10001","11001","10101","10011","10001","10001","10001"),
            "O":("01110","10001","10001","10001","10001","10001","01110"),
            "P":("11110","10001","10001","11110","10000","10000","10000"),
            "Q":("01110","10001","10001","10001","10101","10010","01101"),
            "R":("11110","10001","10001","11110","10100","10010","10001"),
            "S":("01111","10000","10000","01110","00001","00001","11110"),
            "T":("11111","00100","00100","00100","00100","00100","00100"),
            "U":("10001","10001","10001","10001","10001","10001","01110"),
            "V":("10001","10001","10001","10001","10001","01010","00100"),
            "W":("10001","10001","10001","10101","10101","10101","01010"),
            "X":("10001","10001","01010","00100","01010","10001","10001"),
            "Y":("10001","10001","01010","00100","00100","00100","00100"),
            "Z":("11111","00001","00010","00100","01000","10000","11111"),
            "0":("01110","10001","10011","10101","11001","10001","01110"),
            "1":("00100","01100","00100","00100","00100","00100","01110"),
            "2":("01110","10001","00001","00010","00100","01000","11111"),
            "3":("11110","00001","00001","01110","00001","00001","11110"),
            "4":("00010","00110","01010","10010","11111","00010","00010"),
            "5":("11111","10000","10000","11110","00001","00001","11110"),
            "6":("01110","10000","10000","11110","10001","10001","01110"),
            "7":("11111","00001","00010","00100","01000","01000","01000"),
            "8":("01110","10001","10001","01110","10001","10001","01110"),
            "9":("01110","10001","10001","01111","00001","00001","01110"),
            "-":("00000","00000","00000","11111","00000","00000","00000"),
            "/":("00001","00010","00010","00100","01000","01000","10000"),
            " ":("00000",)*7,
        }
        if not all(char in glyphs for char in text):
            count = max(1, min(4, len(text)))
            image = QImage(128*count, 128, QImage.Format.Format_ARGB32); image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image); painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            font = QFont("Noto Color Emoji" if any(ord(ch) > 0x2300 for ch in text) else "monospace")
            font.setPixelSize(96); painter.setFont(font); painter.setPen(QColor(255, 255, 255))
            painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, text[:4]); painter.end()
            return MainWindow._fit_stamp_to_grid(image, 16)
        width = max(1, len(text)*6-1)
        image = QImage(width, 7, QImage.Format.Format_ARGB32); image.fill(Qt.GlobalColor.transparent)
        for char_index, char in enumerate(text):
            for y, row in enumerate(glyphs[char]):
                for x, bit in enumerate(row):
                    if bit == "1": image.setPixelColor(char_index*6+x, y, QColor(255, 255, 255))
        if width > 32:
            image = image.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
        return image

    @staticmethod
    def _fit_stamp_to_grid(image, max_dimension=16):
        """Keep imported emoji/artwork at a useful pixel-stamp scale."""
        image = MainWindow.trim_transparent_edges(image.convertToFormat(QImage.Format.Format_ARGB32))
        if image.isNull() or max(image.width(), image.height()) <= max_dimension:
            return image
        return image.scaled(max_dimension, max_dimension, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.FastTransformation)

    def draw_stamp(self):
        image = QImage(16, 16, QImage.Format.Format_ARGB32); image.fill(Qt.GlobalColor.transparent)
        self._open_stamp_editor(image, None)

    def edit_stamp(self):
        item = self.stamp_list.currentItem()
        if not item:
            self.status.setText("Choose a saved stamp to edit it.")
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        image = QImage(str(path))
        if image.isNull():
            QMessageBox.warning(self, "Missing stamp", "That saved stamp could not be opened.")
            return
        self._open_stamp_editor(image, path)

    def _open_stamp_editor(self, image, existing_path):
        dialog = QDialog(self); dialog.setWindowTitle("Edit transparent pixel stamp" if existing_path else "Draw a transparent pixel stamp")
        layout = QVBoxLayout(dialog)
        controls = QHBoxLayout()
        width = QSpinBox(); width.setRange(1, 32); width.setValue(image.width())
        height = QSpinBox(); height.setRange(1, 32); height.setValue(image.height())
        width.setAccessibleName("Stamp width in pixels")
        height.setAccessibleName("Stamp height in pixels")
        size_button = QPushButton("New size")
        size_button.setAccessibleName("Apply new stamp dimensions")
        size_button.setToolTip("Resize the stamp canvas to the width and height above.")
        color = QComboBox()
        color.setAccessibleName("Stamp brush color")
        color.setAccessibleDescription("Choose the palette color used when painting pixels.")
        for index, (rgb, color_name) in enumerate(zip(PALETTE, PALETTE_NAMES)):
            color.addItem(color_name)
            color.setItemData(index, QColor(*(channel*17 for channel in rgb)), Qt.ItemDataRole.DecorationRole)
            color.setItemData(index, f"Palette color {index+1} · {color_name}", Qt.ItemDataRole.ToolTipRole)
        color.setCurrentIndex(1)
        controls.addWidget(QLabel("Width")); controls.addWidget(width)
        controls.addWidget(QLabel("Height")); controls.addWidget(height)
        controls.addWidget(size_button); controls.addWidget(QLabel("Brush")); controls.addWidget(color)
        layout.addLayout(controls)
        canvas = StampDrawingCanvas(
            image, QColor(*(channel*17 for channel in PALETTE[color.currentIndex()])),
            PALETTE_NAMES[color.currentIndex()])
        layout.addWidget(canvas, 1)
        canvas.setToolTip("Draw with the mouse, or focus the canvas and use arrows to move; Space/Enter paints and Delete/Backspace erases.")
        canvas_instructions = QLabel(
            "Draw with the mouse, or focus the canvas and use arrow keys to move. "
            "Space/Enter paints with the selected brush; Delete/Backspace erases to transparency.")
        canvas_instructions.setWordWrap(True)
        canvas_instructions.setStyleSheet(semantic_text_style("muted", padding="2px"))
        layout.insertWidget(layout.count()-1, canvas_instructions)
        color.currentIndexChanged.connect(
            lambda index: canvas.set_brush_color(
                QColor(*(channel*17 for channel in PALETTE[index])), PALETTE_NAMES[index]))

        def resize_canvas():
            new_image = QImage(width.value(), height.value(), QImage.Format.Format_ARGB32)
            new_image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(new_image); painter.drawImage(0, 0, canvas.image); painter.end()
            canvas.image = new_image; canvas.update()
            canvas.keyboard_cursor = (
                min(canvas.keyboard_cursor[0], new_image.width()-1),
                min(canvas.keyboard_cursor[1], new_image.height()-1))
            canvas.update_accessible_description()

        size_button.clicked.connect(resize_canvas)
        buttons = QHBoxLayout(); save = QPushButton("Save stamp"); cancel = QPushButton("Cancel")
        buttons.addStretch(); buttons.addWidget(cancel); buttons.addWidget(save); layout.addLayout(buttons)
        cancel.clicked.connect(dialog.reject)

        def save_stamp():
            trimmed = self.trim_transparent_edges(canvas.image)
            if trimmed.isNull():
                QMessageBox.information(dialog, "Blank stamp", "Draw at least one pixel before saving.")
                return
            path = existing_path or (DATA / "stamps" / f"stamp-{int(time.time()*1000)}.png")
            if not trimmed.save(str(path), "PNG"):
                QMessageBox.warning(dialog, "Couldn't save stamp", "Could not save the transparent PNG.")
                return
            self.load_stamps()
            matches = [i for i in range(self.stamp_list.count()) if Path(self.stamp_list.item(i).data(Qt.ItemDataRole.UserRole)) == path]
            if matches: self.stamp_list.setCurrentRow(matches[0])
            self.status.setText(f"Saved a {trimmed.width()}×{trimmed.height()} pixel stamp." if existing_path is None else f"Updated stamp to {trimmed.width()}×{trimmed.height()} pixels.")
            dialog.accept()

        save.clicked.connect(save_stamp)
        dialog.resize(560, 620)
        dialog.exec()

    def import_stamp(self):
        name, _ = QFileDialog.getOpenFileName(self, "Import transparent stamp", str(DATA), "Images (*.png *.webp *.bmp *.jpg *.jpeg)")
        if not name: return
        image = QImage(name)
        if image.isNull():
            QMessageBox.warning(self, "Couldn't import stamp", "The selected image could not be opened."); return
        image = self._fit_stamp_to_grid(image, 16)
        if image.isNull():
            QMessageBox.warning(self, "Empty stamp", "The selected image contains no visible pixels."); return
        path = DATA / "stamps" / f"stamp-{int(time.time()*1000)}.png"
        if not image.save(str(path), "PNG"):
            QMessageBox.warning(self, "Couldn't import stamp", "Could not save the imported image."); return
        self.load_stamps(); self.stamp_list.setCurrentRow(self.stamp_list.count()-1)

    def remove_stamp(self):
        item = self.stamp_list.currentItem()
        if not item:
            self.status.setText("Choose a stamp to remove it from your shelf.")
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        try:
            path.unlink()
        except OSError as exc:
            QMessageBox.warning(self, "Couldn't remove stamp", str(exc))
            return
        label = item.text()
        self.load_stamps()
        self.status.setText(f"Removed {label} from your stamp shelf.")

    def stamp_selected(self):
        item = self.stamp_list.currentItem()
        if not item:
            QMessageBox.information(self, "Choose a stamp", "Create or import a stamp, then select it from My stamps."); return
        image = QImage(item.data(Qt.ItemDataRole.UserRole))
        if image.isNull():
            QMessageBox.warning(self, "Missing stamp", "That saved stamp could not be opened."); return
        self.place_stamp_image(image)

    def paste_card_image(self):
        image = QApplication.clipboard().image()
        if image.isNull():
            self.status.setText("No image on the clipboard. Copy an image first.")
            return
        self.place_stamp_image(image)

    def place_stamp_image(self, image):
        if image.width() > self.card.width or image.height() > self.card.height:
            image = self._fit_stamp_to_grid(image, min(16, self.card.width, self.card.height))
        self.canvas.add_stamp(image)
        self.status.setText(
            "Stamp placed · drag to move; drag the lower-right handle to resize")

        self._sync_editor_controls()

    def save_canvas_selection_stamp(self):
        rect = self.canvas.selection_rect
        index = self.canvas.selected_layer
        if not rect.isNull():
            image = QImage(rect.width(), rect.height(), QImage.Format.Format_ARGB32)
            for y in range(rect.height()):
                for x in range(rect.width()):
                    color = self.card.palette[self.card.pixels[(rect.y()+y)*self.card.width+rect.x()+x]]
                    image.setPixelColor(x, y, QColor(*(v*17 for v in color)))
        elif index is not None and 0 <= index < len(self.canvas.stamp_layers):
            layer = self.canvas.stamp_layers[index]
            image = QImage(layer["width"], layer["height"], QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            for i, value in enumerate(layer["pixels"]):
                if value is not None:
                    image.setPixelColor(i % layer["width"], i // layer["width"], QColor(*(v*17 for v in self.card.palette[value])))
        else:
            return
        path = DATA / "stamps" / f"stamp-{time.time_ns()}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not image.save(str(path), "PNG"):
            self.status.setText("Could not save stamp."); return
        self.load_stamps()
        self.status.setText(f"Saved {image.width()}×{image.height()} pixels to My stamps.")

    def listen_to_canvas(self):
        def snapshot():
            card = copy.deepcopy(self.card)
            card.callsign = self.call.text().strip().upper()
            card.grid = self.grid.text().strip().upper()
            return card
        dialog = CanvasListenDialog(snapshot, self)
        dialog.exec()

    def resize_placed_stamp(self):
        index = self.canvas.selected_layer
        if index is None: return
        layer = self.canvas.stamp_layers[index]
        width = self.placed_stamp_width.value()
        height = max(1, round(layer["height"]*width/layer["width"]))
        if height > self.card.height:
            width = max(1, round(width*self.card.height/height)); height = self.card.height
        if (width, height) == (layer["width"], layer["height"]): return
        self.canvas.remember_edit()
        pixels = [layer["pixels"][min(layer["height"]-1, int((y+.5)*layer["height"]/height))*layer["width"]+
                                   min(layer["width"]-1, int((x+.5)*layer["width"]/width))]
                  for y in range(height) for x in range(width)]
        layer.update(width=width, height=height, pixels=pixels,
                     x=min(layer["x"], self.card.width-width), y=min(layer["y"], self.card.height-height))
        self.canvas._render_layers()

    def remove_placed_stamp(self):
        index = self.canvas.selected_layer
        if index is None or not 0 <= index < len(self.canvas.stamp_layers):
            self.status.setText("Click a placed stamp first, then remove it.")
            return
        self.canvas.remember_edit()
        self.canvas.stamp_layers.pop(index)
        self.canvas.selected_layer = None
        self.canvas._render_layers()
        self.status.setText("Removed the selected stamp from the card.")
        self._sync_editor_controls()

    def update_identity(self):
        self.card.callsign = self.call.text().strip().upper()
        self.card.grid = self.grid.text().strip().upper()

    def _update_card_identity_hint(self, *_):
        if not hasattr(self, "card_identity_hint"):
            return
        for label, value, maximum in (("Call sign", self.call.text().strip(), 12),
                                      ("Grid", self.grid.text().strip(), 8)):
            try:
                size = len(value.upper().encode("ascii"))
            except UnicodeEncodeError:
                message = f"{label} must use ASCII characters."
                self.card_identity_hint.setText(message)
                self.card_identity_hint.setStyleSheet(semantic_text_style("error", padding="2px"))
                if hasattr(self, "editor_save_button"):
                    self.editor_save_button.setEnabled(False)
                    self.editor_save_button.setToolTip(f"Can't save yet · {message}")
                return
            if size > maximum:
                message = f"{label} is {size} ASCII bytes; the card format allows at most {maximum}."
                self.card_identity_hint.setText(message)
                self.card_identity_hint.setStyleSheet(semantic_text_style("error", padding="2px"))
                if hasattr(self, "editor_save_button"):
                    self.editor_save_button.setEnabled(False)
                    self.editor_save_button.setToolTip(f"Can't save yet · {message}")
                return
        self.card_identity_hint.setText(
            "Card call sign and grid fit the format limits: 12 and 8 ASCII bytes.")
        self.card_identity_hint.setStyleSheet(semantic_text_style("muted", padding="2px"))
        if hasattr(self, "editor_save_button"):
            self.editor_save_button.setEnabled(True)
            self.editor_save_button.setToolTip("Save this card.")

    def _new_card_id(self):
        reserved = getattr(self, "_allocated_card_ids", set())
        current = getattr(self, "card", None)
        if current is not None:
            reserved.add(current.card_id)
        card_id = allocate_card_id(DATA, reserved)
        reserved.add(card_id)
        self._allocated_card_ids = reserved
        return card_id

    def new_card(self):
        if hasattr(self, "editor_dialog") and self.editor_dialog.isVisible() and not self.confirm_editor_close(): return
        self.animation_author.reset()
        self.card = default_card(self.station_call.text().strip().upper() or "N0CALL",
                                 self.station_grid.text().strip().upper() or "AA00",
                                 card_id=self._new_card_id())
        self.canvas.set_card(self.card)
        self.call.setText(self.card.callsign); self.grid.setText(self.card.grid)
        self._set_editor_format(self.card)
        self._sync_editor_controls()
        self.card_path = None
        self.status.setText("New card")
        self.editor_baseline = self.editor_signature()
        self._update_editor_saved_state()

    def save_card(self):
        self.update_identity()
        try:
            if self.animation_author.frames:
                return self.animation_author.save(DATA / 'cards')
            self.card.validate()
            target = self.card_path
            if target is None:
                default_path = DATA / "cards" / f"{self.card.callsign or 'card'}-{time.strftime('%Y%m%d-%H%M%S')}.json"
                name, _ = QFileDialog.getSaveFileName(self.editor_dialog, "Save card", str(default_path), "Pixel QSO card (*.json)")
                if not name: return False
                target = Path(name)
                if target.suffix.lower() != ".json": target = target.with_suffix(".json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"callsign": self.card.callsign, "grid": self.card.grid,
                "palette": self.card.palette, "pixels": self.card.pixels, "card_id": self.card.card_id,
                "width": self.card.width, "height": self.card.height, "animation": copy.deepcopy(self.card.animation),
                "base_pixels": self.canvas.base_pixels, "stamp_layers": self.canvas.layer_data()}, indent=2))
            self.card_path = target
            self.editor_baseline = self.editor_signature()
            self._update_editor_saved_state()
            self.status.setText(f"Saved {target.name}")
            self.refresh_card_library()
            return True
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't save card", str(exc)); return False

    def open_card(self):
        if not self.confirm_editor_close(): return
        name, _ = QFileDialog.getOpenFileName(self, "Open card", str(DATA), "Pixel QSO card (*.json)")
        if not name: return
        try:
            self._load_card_path(Path(name))
            self.editor_baseline = self.editor_signature()
            self._update_editor_saved_state()
            self.status.setText(f"Opened {self.card_path.name}.")
        except Exception as exc: QMessageBox.warning(self, "Couldn't open card", str(exc))

    def export_wav(self):
        self.update_identity()
        try:
            self.card.validate()
            name, _ = QFileDialog.getSaveFileName(self, "Export transmission audio", str(DATA / "card.wav"), "WAV audio (*.wav)")
            if not name: return
            audio = self._card_tx_audio(self.card)
            modem.write_wav(Path(name), audio)
            self.status.setText(f"Created {Path(name).name}. No radio was keyed.")
            QMessageBox.information(self, "Audio ready", f"Created {name}. This is an audio file only; it has not been transmitted.")
        except Exception as exc: QMessageBox.warning(self, "Couldn't encode card", str(exc))

    def make_exchange_tab(self):
        w = QWidget(); root = QVBoxLayout(w)
        root.setContentsMargins(*([SPACING["lg"]] * 4)); root.setSpacing(SPACING["md"])
        top = QHBoxLayout()
        self.station_button = QPushButton("Station settings"); self.station_button.clicked.connect(self.open_station_settings); top.addWidget(self.station_button)
        self.station_identity = QLabel()
        self.station_identity.setObjectName("stationIdentity")
        self.station_identity.setAccessibleDescription("Current station callsign and grid square.")
        top.addWidget(self.station_identity)
        self._refresh_station_identity()
        self.rig_status = QLabel("CAT disconnected"); self.rig_status.setWordWrap(True)
        self.rig_status.setAccessibleDescription(
            "Reports the current CAT rig-control connection and radio readback state.")
        self.rig_status.setStyleSheet(
            status_label_style("muted", bold=False, letter_spaced=False))
        top.addWidget(self.rig_status, 1)
        self.test_mode_banner = QLabel("AUDIO TEST · PTT DISABLED")
        self.test_mode_banner.setObjectName("testModeBanner")
        self.test_mode_banner.setVisible(self.audio_test_mode.isChecked()); top.addWidget(self.test_mode_banner)
        root.addLayout(top)
        self.rig_host = QLineEdit(os.environ.get(
            "PIXELQSO_RIGCTLD_HOST", str(self.settings.value("radio/rigctld_host", "127.0.0.1"))))
        self.rig_host.setMaximumWidth(180)
        self.rig_port = QSpinBox(); self.rig_port.setRange(1, 65535)
        self.rig_port.setValue(int(os.environ.get(
            "PIXELQSO_RIGCTLD_PORT", self.settings.value("radio/rigctld_port", 4532))))
        self.rig_button = QPushButton("Connect CAT"); self.rig_button.clicked.connect(self.connect_rig)
        self.rig_refresh = QPushButton("Read rig frequency"); self.rig_refresh.clicked.connect(self.read_rig)
        self.output_device = QComboBox()
        for dev in QMediaDevices.audioOutputs(): self.output_device.addItem(dev.description(), dev)
        self.input_device = QComboBox()
        for dev in QMediaDevices.audioInputs(): self.input_device.addItem(dev.description(), dev)
        self.data2g_output_device = QComboBox()
        self.data2g_output_device.addItem("System default output", "")
        for device_index, name in data2g_host_audio_devices("output"):
            self.data2g_output_device.addItem(
                f"{name} [PortAudio {device_index}]", device_index)
        self.data2g_input_device = QComboBox()
        self.data2g_input_device.addItem("System default input", "")
        for device_index, name in data2g_host_audio_devices("input"):
            self.data2g_input_device.addItem(
                f"{name} [PortAudio {device_index}]", device_index)
        self.data2g_audio_config_errors = {}
        for combo, variable, setting in (
                (self.input_device, "PIXELQSO_INPUT_DEVICE", "audio/input_device"),
                (self.output_device, "PIXELQSO_OUTPUT_DEVICE", "audio/output_device"),
                (self.data2g_input_device, "PIXELQSO_DATA2G_INPUT_DEVICE", "data2g/audio_input_device"),
                (self.data2g_output_device, "PIXELQSO_DATA2G_OUTPUT_DEVICE", "data2g/audio_output_device")):
            requested_variable = variable
            requested = os.environ.get(requested_variable)
            legacy_audio_setting = {"data2g/audio_input_device": "audio/input_device",
                                    "data2g/audio_output_device": "audio/output_device"}.get(setting)
            if not requested and legacy_audio_setting:
                requested_variable = ("PIXELQSO_INPUT_DEVICE" if "input" in setting
                                      else "PIXELQSO_OUTPUT_DEVICE")
                requested = os.environ.get(requested_variable)
            if requested:
                index = combo.findText(requested)
                if index < 0:
                    index = combo.findData(requested)
                if index < 0 and setting.startswith("data2g/"):
                    matches = [i for i in range(combo.count())
                               if requested.casefold() in combo.itemText(i).casefold()]
                    if len(matches) == 1:
                        index = matches[0]
                if index < 0:
                    if setting.startswith("data2g/"):
                        self.data2g_audio_config_errors[setting] = requested_variable
                        index = -1
                        combo.setCurrentIndex(-1)
                    else:
                        raise ValueError(f"Configured audio device is unavailable: {requested_variable}")
                combo.setCurrentIndex(index)
            else:
                source_setting = (legacy_audio_setting if legacy_audio_setting and
                                  not self.settings.contains(setting + "/id") else setting)
                saved_id = str(self.settings.value(source_setting + "/id", ""))
                saved_name = str(self.settings.value(source_setting + "/name", ""))
                index = combo.findText(saved_name) if saved_name else -1
                if index < 0 and setting.startswith("data2g/") and saved_name:
                    matches = [i for i in range(combo.count())
                               if saved_name.casefold() in combo.itemText(i).casefold()]
                    if len(matches) == 1:
                        index = matches[0]
                if index < 0 and setting.startswith("data2g/") and saved_id:
                    try:
                        previous_qt_id = bytes.fromhex(saved_id).decode("utf-8").strip()
                    except (ValueError, UnicodeDecodeError):
                        previous_qt_id = ""
                    if previous_qt_id:
                        matches = [i for i in range(combo.count())
                                   if previous_qt_id.casefold() in combo.itemText(i).casefold()]
                        if len(matches) == 1:
                            index = matches[0]
                if index < 0:
                    index = next((i for i in range(combo.count())
                                  if self._audio_device_identifier(combo.itemData(i)) == saved_id), -1)
                if index < 0 and saved_name:
                    index = combo.findText(saved_name)
                if index >= 0:
                    combo.setCurrentIndex(index)
                elif setting.startswith("data2g/") and (saved_id or saved_name):
                    direction = "input" if "input" in setting else "output"
                    previous = saved_name or "saved device ID"
                    self.data2g_audio_config_errors[setting] = (
                        f"saved {direction} device '{previous}'")
                    combo.setCurrentIndex(-1)
            combo.currentIndexChanged.connect(
                lambda _index, selected=combo, key=setting: self._save_audio_device(selected, key))
            if setting.startswith("data2g/"):
                combo.currentIndexChanged.connect(
                    lambda _index, key=setting: self._data2g_audio_device_changed(key))
            if setting not in self.data2g_audio_config_errors:
                self._save_audio_device(combo, setting)
        if self.data2g_audio_config_errors:
            self.data2g_host_status.setText(self._data2g_audio_config_error_message())
        self.exchange_mode = QComboBox(); self.exchange_mode.addItems(["Manual send", "Automatic exchange", "Beacon"])
        # Keep internal stage indices stable; the unfinished standalone mode
        # is unavailable in both the popup and keyboard navigation.
        self.exchange_mode.model().item(1).setEnabled(False)
        self.exchange_mode.view().setRowHidden(1, True)
        self.exchange_mode.setToolTip(
            "Manual send sends the selected card once. Automatic exchange manages a CQ or reply sequence. "
            "Beacon repeats your card at the chosen interval.")
        self.exchange_mode.setAccessibleDescription(
            "Choose a one-time card send, an automatic CQ or reply sequence, or repeated card beacons.")
        self.exchange_role = QComboBox(); self.exchange_role.addItems(["Call-first", "Reply to CQ"])
        self.exchange_mode.currentIndexChanged.connect(self._update_exchange_controls)
        self.exchange_mode.currentIndexChanged.connect(self._refresh_transfer_summary)
        self.exchange_role.currentIndexChanged.connect(self._refresh_transfer_summary)
        self.mode_combo = QComboBox()
        self.mode_compatibility_filter = QComboBox()
        self.mode_compatibility_filter.addItem("All modes", "all")
        self.mode_compatibility_filter.addItem("Compatible with current card", "compatible")
        self.mode_compatibility_filter.setToolTip(
            "Filter modes by the current card's canvas size and palette size. "
            "All modes remain available by default.")
        self.mode_compatibility_filter.setAccessibleName("Mode compatibility filter")
        configured_mode = self.settings.value("transfer/mode", DEFAULT_MODE_KEY)
        preserved_legacy_mode = (
            configured_mode not in {"data2g_1200_robust", "data2g_1200_fast"} and
            not is_data2g_mode_key(str(configured_mode)) and
            self.settings.value("transfer/experimental_mode", None) is None and
            is_experimental_mode(str(configured_mode)))
        if (self.settings.value("transfer/data2g_mode", None) is None and
                (configured_mode in {"data2g_1200_robust", "data2g_1200_fast"} or
                 is_data2g_mode_key(str(configured_mode)))):
            self.settings.setValue("transfer/data2g_mode", configured_mode)
        if (configured_mode not in {"data2g_1200_robust", "data2g_1200_fast"} and
                not is_data2g_mode_key(str(configured_mode))
                and self.settings.value("transfer/experimental_mode", None) is None):
            self.settings.setValue("transfer/experimental_mode", configured_mode)
        configured_mode = self.settings.value(
            "transfer/data2g_mode" if self.backend_combo.currentData() == "data2g"
            else "transfer/experimental_mode", configured_mode)
        self._mode_has_explicit_selection = self.settings.value(
            "transfer/mode_explicit", False, type=bool)
        if (not self._mode_has_explicit_selection and self.settings.contains("transfer/mode")
                and configured_mode != DEFAULT_MODE_KEY):
            self._mode_has_explicit_selection = True
        self._set_mode_options(configured_mode)
        if (preserved_legacy_mode and self.backend_combo.currentData() == "data2g" and
                not self.settings.value(
                "transfer/experimental_mode_migrated", False, type=bool)):
            self.settings.setValue("transfer/experimental_mode_migrated", True)
            self.statusBar().showMessage(
                f"Your saved local modem mode {configured_mode!r} is preserved under "
                "Experimental modems. Enable Show experimental modem modes and select "
                "that backend to use it.", 15000)
        self.mode_combo.currentIndexChanged.connect(self._persist_selected_mode)
        self.mode_combo.currentIndexChanged.connect(self._mark_mode_selection_explicit)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_selection_changed)
        self.mode_combo.currentIndexChanged.connect(self._update_transfer_controls)
        self.mode_combo.currentIndexChanged.connect(self._refresh_transfer_summary)
        self.mode_combo.setToolTip(
            "Choose the digital transfer mode used to send and receive cards. "
            "This does not change the radio's operating mode or frequency.")
        self.mode_combo.setAccessibleName("Card transfer mode")
        self.mode_combo.setAccessibleDescription(
            "Select the digital mode used to send and receive cards. "
            "This is separate from the radio control mode and frequency.")
        mode_form = QFormLayout()
        mode_form.addRow("Transfer mode", self.mode_combo)
        self.transfer_mode_label = mode_form.labelForField(self.mode_combo)
        self.mode_compatibility_filter.currentIndexChanged.connect(
            lambda *_: self._set_mode_options(self.selected_mode()))
        mode_form.addRow("Show", self.mode_compatibility_filter)
        self._update_transfer_mode_visibility()
        self.receive_all_modes = QCheckBox("Receive all local audio modem types")
        self.receive_all_modes.setChecked(self.settings.value("transfer/receive_all", True, type=bool))
        self.receive_all_modes.setToolTip(
            "This applies to Pixel QSO's local audio decoders. Data2G receives decoded card frames from its host.")
        self.receive_all_modes.toggled.connect(lambda value: self.settings.setValue("transfer/receive_all", value))
        self.receive_all_modes.toggled.connect(self._update_rx_mode_controls)
        mode_form.addRow("Receive", self.receive_all_modes)
        self.receive_all_modes_label = mode_form.labelForField(self.receive_all_modes)
        saved_rx_mode = str(self.settings.value("transfer/rx_preferred_mode", "") or "")
        if saved_rx_mode not in MODE_REGISTRY:
            saved_rx_mode = self.selected_mode()
        self.rx_preferred_mode = saved_rx_mode
        self.rx_mode_lock = QCheckBox("Lock mode")
        self.rx_mode_lock.setChecked(self.settings.value("transfer/rx_mode_locked", False, type=bool))
        self.rx_mode_lock.setToolTip(
            "When unlocked, a detected modem becomes the preferred RX decoder. "
            "This does not change the TX transfer mode.")
        self.rx_mode_lock.toggled.connect(
            lambda value: self.settings.setValue("transfer/rx_mode_locked", value))
        self.rx_mode_preference_label = QLabel()
        self.rx_mode_preference_label.setObjectName("rxModePreference")
        rx_mode_row = QWidget()
        rx_mode_layout = QHBoxLayout(rx_mode_row)
        rx_mode_layout.setContentsMargins(0, 0, 0, 0)
        rx_mode_layout.addWidget(self.rx_mode_lock)
        rx_mode_layout.addWidget(self.rx_mode_preference_label, 1)
        mode_form.addRow("RX preference", rx_mode_row)
        self.rx_mode_preference_form_row = rx_mode_row
        self.rx_mode_preference_form_label = mode_form.labelForField(rx_mode_row)
        self._update_rx_mode_preference_label()
        self._update_rx_mode_controls()
        # Retain saved repetition and mode limits without exposing another
        # main-panel choice. Single-burst modes still clamp this to one.
        self.repeat_count = QSpinBox(self); self.repeat_count.hide()
        self.repeat_count.setRange(1, 20); self.repeat_count.setValue(int(self.settings.value("transfer/repeats", 1)))
        self.repeat_count.setToolTip("Send copies of the same card; RX combines soft evidence before integrity checks.")
        self.repeat_count.valueChanged.connect(
            lambda value: self.settings.setValue("transfer/repeats", value)
            if not getattr(self, "_station_settings_editing", False) else None)
        self.repeat_count.valueChanged.connect(self._refresh_transfer_summary)
        self.interlaced_copies = QCheckBox("Interlace repeated copies")
        self.interlaced_copies.setChecked(self.settings.value("transfer/interlaced_copies", False, type=bool))
        self.interlaced_copies.setToolTip(
            "Stagger FEC block order across copies so fades are less likely to damage the same blocks. "
            "Available for local modem modes that support interlaced decoding.")
        self.interlaced_copies.toggled.connect(
            lambda value: self.settings.setValue("transfer/interlaced_copies", value)
            if not getattr(self, "_station_settings_editing", False) else None)
        self.card_combo = QComboBox()
        self.card_combo.setIconSize(QSize(42, 42))
        self.card_combo.currentIndexChanged.connect(self._refresh_transfer_summary)
        self.card_combo.currentIndexChanged.connect(self._refresh_mode_capabilities)
        self.manual_card_widget = QWidget(); manual_card_layout = QHBoxLayout(self.manual_card_widget)
        manual_card_layout.setContentsMargins(0, 0, 0, 0); manual_card_layout.addWidget(self.card_combo)
        mode_form.addRow("Card", self.manual_card_widget)
        self.manual_card_label = mode_form.labelForField(self.manual_card_widget)
        self.auto_stage_group = QGroupBox("Your contact · four stages")
        # Use explicit horizontal rows for the send actions. QFormLayout treats
        # its first column as label content and can collapse widget labels
        # (notably QPushButtons) after the settings dialog rebuilds the app
        # stylesheet, leaving only the card selectors visible.
        auto_stage_layout = QVBoxLayout(self.auto_stage_group)
        auto_stage_layout.setContentsMargins(8, 8, 8, 8)
        auto_stage_layout.setSpacing(6)
        self.stage_card_combos = {}
        self.stage_send_buttons = {}
        self.stage_select_buttons = {}
        self.stage_rows = {}
        self.stage_picker = QComboBox()
        self.stage_picker.setAccessibleName("Outgoing message stage")
        for key, label in (("cq", "CQ · start a contact"), ("exchange", "Exchange · share a card"), ("report73", "RR73 · reception report"), ("final73", "73 · final card")):
            self.stage_picker.addItem(label, key)
        self.stage_picker.setCurrentIndex(1)
        self.stage_picker.currentIndexChanged.connect(lambda _index: self._select_manual_stage(self.stage_picker.currentData()))
        self.stage_picker.hide()  # Kept as a synchronized selection model; stages stay visible.
        self.manual_stage_group = QButtonGroup(self); self.manual_stage_group.setExclusive(True)
        self.manual_stage_buttons = {}
        for stage, title in (("cq", "CQ"), ("exchange", "response"),
                              ("report73", "signal report"), ("final73", "73 goodbye")):
            combo = QComboBox(); combo.setIconSize(QSize(24, 24))
            combo.currentIndexChanged.connect(self._save_auto_stage_cards)
            combo.currentIndexChanged.connect(
                lambda _index, s=stage: self._select_manual_stage(s))
            combo.currentIndexChanged.connect(self._refresh_transfer_summary)
            combo.currentIndexChanged.connect(self._refresh_mode_capabilities)
            self.stage_card_combos[stage] = combo
            button = QPushButton(title); button.setCheckable(True)
            button.setProperty("stage", stage)
            button.setProperty("stage_title", title)
            button.setProperty("normal_object_name", "stageSelector")
            button.setText(f"Send {title}")
            button.setObjectName("stageSelector")
            button.setToolTip(f"Send the assigned {title} card; while transmitting, stop it")
            button.clicked.connect(lambda checked=False, s=stage: self._send_stage_card(s))
            self.manual_stage_group.addButton(button, len(self.manual_stage_buttons))
            self.manual_stage_buttons[stage] = button
            self.stage_send_buttons[stage] = button
            select_button = QPushButton("Select")
            select_button.setToolTip(f"Select the {title} stage and its assigned card without transmitting")
            select_button.setAccessibleName(f"Select {title} stage and card")
            select_button.clicked.connect(lambda _checked=False, s=stage: self._select_manual_stage(s))
            self.stage_select_buttons[stage] = select_button
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            button.setMinimumWidth(124)
            select_button.setText(str(len(self.stage_send_buttons)))
            select_button.setFixedWidth(28)
            select_button.setObjectName("stageNumber")
            row.addWidget(select_button)
            row.addWidget(button)
            row.addWidget(combo, 1)
            row_widget = QWidget(); row_widget.setLayout(row)
            self.stage_rows[stage] = row_widget
            auto_stage_layout.addWidget(row_widget)
        self.manual_stage_buttons["exchange"].setChecked(True)
        self.manual_stage_group.buttonToggled.connect(self._refresh_transfer_summary)
        self.role_widget = QWidget(); role_form = QFormLayout(self.role_widget); role_form.setContentsMargins(0, 0, 0, 0)
        role_form.addRow("Role", self.exchange_role)
        self.frequency = QSpinBox(); self.frequency.setRange(100_000, 1_300_000_000); self.frequency.setValue(14_074_000); self.frequency.setSuffix(" Hz")
        self.frequency.setReadOnly(True)
        self.frequency.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.frequency.setToolTip("Read-only frequency reported by the connected radio.")
        f = QFormLayout(); f.addRow("Rig frequency", self.frequency)
        self.frequency_controls = QWidget(); self.frequency_controls.setLayout(f)
        transfer_group = QGroupBox("Send details")
        transfer_form = QFormLayout(transfer_group)
        transfer_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        transfer_form.addRow("Send type", self.exchange_mode)
        self.advanced_exchange_label = transfer_form.labelForField(self.exchange_mode)
        transfer_form.addRow(mode_form)
        transfer_form.addRow(self.role_widget); transfer_form.addRow(self.auto_stage_group)
        transfer_form.addRow(self.frequency_controls)
        self.exchange_status = QLabel("CQ → Reply / Exchange → RR73 → 73\nCall CQ, or click Reply on a received CQ to start automatic sequencing.")
        self.exchange_status.setWordWrap(True)
        self.exchange_status.setAccessibleDescription("Shows the current automatic exchange stage and peer status.")
        transfer_form.addRow(self.exchange_status)
        self.transfer_summary = QLabel(); self.transfer_summary.setWordWrap(True)
        self.transfer_summary.setStyleSheet(semantic_text_style("soft", padding="4px"))
        self.transfer_summary.setAccessibleDescription("Shows the selected card, outgoing stage, mode details, and estimated airtime.")
        transfer_form.addRow(self.transfer_summary)
        self.tx_result_status = QLabel()
        self.tx_result_status.setObjectName("supportingText")
        self.tx_result_status.setWordWrap(True)
        self.tx_result_status.setAccessibleName("Last transmission result")
        self.tx_result_status.setAccessibleDescription(
            "Shows the outcome of the most recent transmission attempt.")
        self.tx_result_status.setVisible(False)
        transfer_form.addRow(self.tx_result_status)
        self.tx_btn = QPushButton("Send card"); self.tx_btn.clicked.connect(self.transmit_exchange)
        self.manual_stage_group.buttonToggled.connect(self._update_send_button_label)
        self.stop_tx_btn = QPushButton("Stop transmission")
        self.stop_tx_btn.setAccessibleName("Stop current transmission")
        self.stop_tx_btn.setAccessibleDescription(
            "Stops local audio transmission or asks the Data2G host to cancel queued frames. "
            "A frame already in flight may still complete.")
        self.stop_tx_btn.setToolTip(self.stop_tx_btn.accessibleDescription())
        self.stop_tx_btn.setEnabled(False); self.stop_tx_btn.setVisible(False)
        self.stop_tx_btn.clicked.connect(self._stop_current_transmit)
        self.tx_btn.hide()
        self.tx_btn.setMinimumHeight(46)
        self.stop_tx_btn.setMinimumHeight(46)
        tx_row = QVBoxLayout(); tx_row.addWidget(self.tx_btn); tx_row.addWidget(self.stop_tx_btn)
        self.manual_tx_controls = QWidget()
        manual_tx_layout = QVBoxLayout(self.manual_tx_controls)
        manual_tx_layout.setContentsMargins(0, 0, 0, 0)
        manual_tx_layout.setSpacing(5)
        manual_tx_layout.addLayout(tx_row)
        self.tx_availability_hint = QLabel()
        self.tx_availability_hint.setWordWrap(True)
        self.tx_availability_hint.setStyleSheet(
            semantic_text_style("warning", padding="2px 4px"))
        self.tx_availability_hint.setAccessibleDescription(
            "Explains why card transmission is unavailable and what must be resolved.")
        manual_tx_layout.addWidget(self.tx_availability_hint)
        transfer_form.addRow(self.manual_tx_controls)
        auto_row = QHBoxLayout()
        self.auto_start_btn = QPushButton("Start automatic exchange"); self.auto_start_btn.clicked.connect(self.start_auto_exchange); auto_row.addWidget(self.auto_start_btn)
        self.auto_stop_btn = QPushButton("Stop exchange")
        self.auto_stop_btn.setAccessibleName("Stop automatic exchange")
        self.auto_stop_btn.setAccessibleDescription(
            "Ends the automatic CQ or reply sequence and returns the station to receiving. "
            "A Data2G frame already in flight may still complete.")
        self.auto_stop_btn.setToolTip(self.auto_stop_btn.accessibleDescription())
        self.auto_stop_btn.setEnabled(False); self.auto_stop_btn.setVisible(False)
        self.auto_stop_btn.clicked.connect(lambda _checked=False: self.stop_auto_exchange())
        auto_row.addWidget(self.auto_stop_btn)
        self.auto_controls = QWidget(); self.auto_controls.setLayout(auto_row); transfer_form.addRow(self.auto_controls)
        self.call_cq_button = QPushButton("Call CQ")
        self.call_cq_button.setCheckable(True)
        self.call_cq_button.toggled.connect(self._toggle_call_cq)
        self.cq_interval = QSpinBox(); self.cq_interval.setRange(1, 3600)
        self.cq_interval.setValue(int(self.settings.value("transfer/cq_interval_seconds", 5)))
        self.cq_interval.setSuffix(" s")
        self.cq_interval.setAccessibleName("CQ repeat interval")
        self.cq_interval.setAccessibleDescription(
            "Seconds between CQ calls while not exchanging. The first CQ sends immediately; "
            "this interval is locked while CQ calling is active.")
        self.cq_interval.valueChanged.connect(self._save_cq_interval)
        cq_row = QHBoxLayout(); cq_row.addWidget(self.call_cq_button); cq_row.addWidget(QLabel("Repeat every")); cq_row.addWidget(self.cq_interval)
        self.cq_controls = QWidget(); self.cq_controls.setLayout(cq_row); transfer_form.addRow(self.cq_controls)
        self.beacon_timer = QTimer(self)
        self.beacon_timer.timeout.connect(self.transmit_beacon)
        self.beacon_status_timer = QTimer(self)
        self.beacon_status_timer.setInterval(1000)
        self.beacon_status_timer.timeout.connect(self._refresh_beacon_status)
        self.beacon_interval = QSpinBox(); self.beacon_interval.setRange(1, 1440)
        self.beacon_interval.setValue(int(self.settings.value("transfer/beacon_interval_minutes", 5)))
        self.beacon_interval.setSuffix(" min")
        self.beacon_interval.setAccessibleName("Beacon repeat interval")
        self.beacon_interval.setAccessibleDescription(
            "Minutes between repeated card beacons. Starting a beacon sends one card immediately. "
            "The interval is locked while the beacon is active. A scheduled repeat can be skipped "
            "during an automatic exchange or while another transmission is in progress.")
        self.beacon_interval.valueChanged.connect(lambda value: self.settings.setValue("transfer/beacon_interval_minutes", value))
        self.beacon_button = QPushButton("Start beacon"); self.beacon_button.setCheckable(True)
        self.beacon_button.toggled.connect(self._toggle_beacon)
        self._update_beacon_help()
        self.beacon_status = QLabel("Beacon is off.")
        self.beacon_status.setObjectName("supportingText")
        self.beacon_status.setAccessibleName("Beacon schedule status")
        self.beacon_status.setAccessibleDescription(
            "Shows whether the beacon is active and the time remaining until its next scheduled "
            "attempt. A repeat can be skipped during an automatic exchange or while another "
            "transmission is in progress.")
        beacon_form = QFormLayout()
        beacon_form.addRow("Send card every", self.beacon_interval)
        beacon_form.addRow(self.beacon_button)
        beacon_form.addRow(self.beacon_status)
        self.beacon_controls = QWidget(); self.beacon_controls.setLayout(beacon_form); transfer_form.addRow(self.beacon_controls)
        self._refresh_operate_primary_action()
        self._update_transfer_controls()
        self._refresh_transfer_summary()
        self._set_advanced_controls(
            self.settings.value("ui/advanced_controls", False, type=bool))

        wav_button = QPushButton("Decode audio file…"); wav_button.clicked.connect(self.decode_wav)
        receive_group = QGroupBox("Receive")
        self.receive_group = receive_group
        receive_layout = QHBoxLayout(receive_group)
        self.receive_status_hint = QLabel()
        self.receive_status_hint.setWordWrap(True)
        self.receive_status_hint.setAccessibleDescription("Explains connection requirements and what receive activity has or has not confirmed.")
        receive_layout.addWidget(self.receive_status_hint, 1)
        receive_layout.addWidget(wav_button)

        left_panel = QVBoxLayout(); left_panel.setContentsMargins(0, 0, 0, 0); left_panel.setSpacing(12)
        self.outgoing_art = QLabel(); self.outgoing_art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.outgoing_art.setMinimumHeight(190)
        outgoing_label = QLabel("YOUR OUTGOING CARD"); outgoing_label.setObjectName("eyebrow")
        left_panel.addWidget(outgoing_label); left_panel.addWidget(self.outgoing_art)
        choose = QPushButton("Browse collection"); choose.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        left_panel.addWidget(choose); left_panel.addWidget(transfer_group)
        left = QWidget(); left.setLayout(left_panel)

        right_panel = QVBoxLayout()
        self.traffic_preview = QStackedWidget()
        self.receive_workspace = QWidget()
        received_layout = QVBoxLayout(self.receive_workspace)
        received_layout.setContentsMargins(0, 0, 0, 0)
        self.traffic_preview.addWidget(self.receive_workspace)
        right_panel.addWidget(self.traffic_preview, 1)
        view_heading = QHBoxLayout()
        session_title = QLabel("The receiving room"); session_title.setObjectName("sectionTitle")
        view_heading.addWidget(session_title, 1)
        self.listen_indicator = QLabel("● STARTING")
        self.listen_indicator.setAccessibleDescription("Current receiver activity state.")
        self.listen_indicator.setStyleSheet(status_label_style("muted"))
        self._refresh_listen_indicator_width()
        view_heading.addWidget(self.listen_indicator)
        received_layout.addLayout(view_heading)
        self.incoming_card_details = QLabel()
        self.incoming_card_details.setTextFormat(Qt.TextFormat.PlainText)
        self.incoming_card_details.setWordWrap(True)
        self.incoming_card_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.incoming_card_details.setAccessibleName("Incoming card metadata")
        self.incoming_card_details.setStyleSheet(semantic_text_style("soft", padding="4px"))
        self.incoming_card_details.hide()
        received_layout.addWidget(self.incoming_card_details)
        self.rx_waterfall = RxAudioWaterfall()
        self.rx_visual_stack = QStackedWidget()
        self.rx_waterfall_unavailable = QLabel(
            "Audio waterfall unavailable · Data2G receives decoded frames from its host")
        self.rx_waterfall_unavailable.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rx_waterfall_unavailable.setObjectName("receiveWaterfallUnavailable")
        self.rx_visual_stack.addWidget(self.rx_waterfall)
        self.rx_visual_stack.addWidget(self.rx_waterfall_unavailable)
        received_layout.addWidget(self.rx_visual_stack)
        self.receive_stack = QStackedWidget()
        self.session_wall_page = QWidget()
        wall_layout = QHBoxLayout(self.session_wall_page); wall_layout.setContentsMargins(0, 0, 0, 0); wall_layout.setSpacing(SPACING["sm"])
        wall_list_panel = QWidget()
        wall_list_layout = QVBoxLayout(wall_list_panel); wall_list_layout.setContentsMargins(0, 0, 0, 0)
        self.session_wall_empty = QLabel(
            '<div style="font-size:54px; color:#bda5f8">▦</div><br>'
            '<div style="font-size:24px; font-weight:600; color:#f5f1e9">A space for your next connection.</div><br>'
            '<div>Cards will appear here as they arrive.<br>'
            'Partial cards stay marked provisional until verified.</div>')
        self.session_wall_empty.setWordWrap(True)
        self.session_wall_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.session_wall_empty.setObjectName("receiverEmpty")
        self.session_wall_empty.setAccessibleName("No cards received this session yet")
        self.session_wall_empty.setAccessibleDescription(
            "Identified provisional cards appear here while integrity checks are still in progress.")
        wall_list_layout.addWidget(self.session_wall_empty, 1)
        self.session_wall = QListWidget()
        self.session_wall.setViewMode(QListWidget.ViewMode.IconMode)
        self.session_wall.setMovement(QListWidget.Movement.Static)
        self.session_wall.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.session_wall.setWordWrap(False)
        self.session_wall.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.session_wall.setIconSize(QSize(144, 144))
        self.session_wall.setGridSize(QSize(148, 148))
        self.session_wall.setSpacing(2)
        self.session_wall.setObjectName("sessionWall")
        self.session_wall.setAccessibleName("Received cards for this session")
        self.session_wall.setAccessibleDescription(
            "Newest cards appear first. A card may be provisional until whole-card integrity checks pass.")
        self.session_wall.hide()
        wall_list_layout.addWidget(self.session_wall, 1)
        wall_layout.addWidget(wall_list_panel, 1)
        self.receive_preview_box = QGroupBox("Live preview")
        self.receive_preview_box.setFixedWidth(218)
        self.receive_preview_box.setToolTip(
            "Received pixels remain visible provisionally even when a block or whole-card CRC fails. "
            "Later copies can refine them; only CRC-verified pixels are accepted as final.")
        preview_layout = QVBoxLayout(self.receive_preview_box)
        preview_layout.setContentsMargins(*([SPACING["sm"]] * 4))
        self.receive_stack.addWidget(self.session_wall_page)
        self.receive_view = QLabel("Listening for a card")
        self.receive_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.receive_view.setFixedSize(194, 194)
        self.receive_view.setAccessibleName("Received card preview")
        self.receive_view.setObjectName("receiveView")
        self.receive_duplicate_label = QLabel("DUPLICATE")
        self.receive_duplicate_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.receive_duplicate_label.setStyleSheet("color: #ef5350; font-weight: 900; letter-spacing: 1px;")
        self.receive_duplicate_label.setAccessibleName("Duplicate received card")
        self.receive_duplicate_label.setObjectName("receiveDuplicateLabel")
        self.receive_duplicate_label.hide()
        preview_layout.addWidget(self.receive_duplicate_label, 0, Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.receive_view, 0, Qt.AlignmentFlag.AlignCenter)
        self.receive_progress = QProgressBar()
        self.receive_progress.setRange(0, 100)
        self.receive_progress.setValue(0)
        self.receive_progress.setFormat("%p% received")
        self.receive_progress.setFixedWidth(184)
        self.receive_progress.setFixedHeight(14)
        self.receive_progress.setAccessibleName("Card receive progress")
        self.receive_progress.setObjectName("receiveProgress")
        preview_layout.addWidget(self.receive_progress, 0, Qt.AlignmentFlag.AlignCenter)
        self.receive_qr_button = QPushButton("Read QR code")
        self.receive_qr_button.clicked.connect(self.read_received_qr)
        self.receive_qr_button.hide()
        self.receive_qr_image = None
        preview_layout.addWidget(self.receive_qr_button, 0, Qt.AlignmentFlag.AlignCenter)
        self.discard_preview_button = QPushButton("Discard preview")
        self.discard_preview_button.setObjectName("discardPreviewButton")
        self.discard_preview_button.setToolTip(
            "Forget this in-progress card. The next transmission will start a new preview.")
        self.discard_preview_button.setAccessibleDescription(
            "Discard the current preview and treat the next transmission as a new card.")
        self.discard_preview_button.clicked.connect(self._discard_receive_preview)
        preview_layout.addWidget(self.discard_preview_button, 0, Qt.AlignmentFlag.AlignCenter)
        self.receive_preview_box.hide()
        wall_layout.addWidget(self.receive_preview_box, 0)
        self.receive_stack.setCurrentWidget(self.session_wall_page)
        received_layout.addWidget(self.receive_stack, 1)
        self.traffic_preview.addWidget(self._make_transmit_preview())
        self.rx_status = ReceiveStatusLabel("Ready to receive")
        self.rx_status.setObjectName("receiveStatus")
        self.rx_status.setWordWrap(True)
        self.rx_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.rx_status.setFixedHeight(self.rx_status.sizeHint().height())
        self.rx_status.setAccessibleDescription("Current receive progress, verification result, or recovery guidance.")
        self.rx_status.setVisible(
            self.settings.value("ui/advanced_controls", False, type=bool))
        right_panel.addWidget(self.rx_status)
        self.data2g_switch_button = QPushButton("Data2G card detected · Switch to Data2G")
        self.data2g_switch_button.setObjectName("data2gSwitchPrompt")
        self.data2g_switch_button.setAccessibleName("Switch to Data2G after detected card")
        self.data2g_switch_button.setAccessibleDescription(
            "Switches the active backend to Data2G and hands radio control over from Experimental.")
        self.data2g_switch_button.setToolTip(
            "Switch the active backend to Data2G and hand radio control over from Experimental.")
        self.data2g_switch_button.clicked.connect(self._switch_to_data2g_backend)
        self.data2g_switch_button.hide()
        right_panel.addWidget(self.data2g_switch_button)
        right_panel.addWidget(receive_group)
        self.reply_to_button = QPushButton("Reply to CQ")
        self.reply_to_button.setVisible(False)
        self.reply_to_button.clicked.connect(self.reply_to_received)
        right_panel.addWidget(self.reply_to_button)
        self.stop_responding_button = QPushButton("Stop responding")
        self.stop_responding_button.setVisible(False)
        self.stop_responding_button.setToolTip(
            "Cancel this contact and pending replies, stop transmission, and keep listening.")
        self.stop_responding_button.clicked.connect(self.stop_responding)
        right_panel.addWidget(self.stop_responding_button)
        self.quick_draw_button = QPushButton("Quick draw")
        self.quick_draw_button.setToolTip("Sketch a card while receiving or transmitting; use it for the next manual send.")
        self.quick_draw_button.clicked.connect(self.start_quick_draw_reply)
        self.quick_draw_status = QLabel("")
        self.quick_draw_status.setObjectName("quickDrawStatus")
        self.clear_quick_draw_button = QPushButton("Clear quick draw")
        self.clear_quick_draw_button.clicked.connect(self.clear_quick_draw)
        quick_draw_row = QHBoxLayout()
        quick_draw_row.addWidget(self.quick_draw_button)
        self.quick_draw_send_button = QPushButton("Send quick draw")
        self.quick_draw_send_button.setObjectName("quickDrawSendAction")
        self.quick_draw_send_button.setProperty("normal_object_name", "quickDrawSendAction")
        self.quick_draw_send_button.setToolTip("Transmit the saved quick-draw card using the selected message stage")
        self.quick_draw_send_button.clicked.connect(self._send_quick_draw_card)
        self.quick_draw_send_button.setVisible(False)
        quick_draw_row.addWidget(self.quick_draw_send_button)
        quick_draw_row.addWidget(self.quick_draw_status, 1)
        quick_draw_row.addWidget(self.clear_quick_draw_button)
        right_panel.addLayout(quick_draw_row)
        self.exchange_log = QTextEdit(); self.exchange_log.setReadOnly(True); self.exchange_log.setPlaceholderText("Exchange activity")
        self.exchange_log.setMinimumHeight(64); self.exchange_log.setMaximumHeight(140)
        self.exchange_log.document().setMaximumBlockCount(2000)
        activity_toggle = QPushButton("Show exchange activity"); activity_toggle.setCheckable(True)
        activity_toggle.setObjectName("activityDisclosure")
        activity_toggle.toggled.connect(self.exchange_log.setVisible)
        activity_toggle.toggled.connect(lambda expanded: activity_toggle.setText("Hide exchange activity" if expanded else "Show exchange activity"))
        right_panel.addWidget(activity_toggle); self.exchange_log.hide()
        right_panel.addWidget(self.exchange_log)
        right = QWidget(); right.setLayout(right_panel)
        left_scroll = QScrollArea(); left_scroll.setWidgetResizable(True); left_scroll.setWidget(left)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame); left_scroll.setMinimumWidth(300)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        sender = QWidget(); sender.setObjectName("senderWorkspace")
        sender_layout = QVBoxLayout(sender); sender_layout.setContentsMargins(12, 12, 12, 12)
        sender_layout.addWidget(left_scroll, 1)
        transfer_form.removeWidget(self.auto_stage_group)
        sender_layout.addWidget(self.auto_stage_group)
        for controls in (self.manual_tx_controls, self.auto_controls, self.cq_controls, self.beacon_controls):
            transfer_form.removeWidget(controls); sender_layout.addWidget(controls)
        left_panel.addStretch(1)
        self.operate_splitter = QSplitter(Qt.Orientation.Horizontal)
        sender.setMinimumWidth(370); right.setMinimumWidth(300)
        receive_scroll = ContentScrollArea(); receive_scroll.setWidgetResizable(True)
        receive_scroll.setFrameShape(QFrame.Shape.NoFrame)
        receive_scroll.setWidget(right)
        receive_scroll.setMinimumWidth(320)
        self.operate_splitter.addWidget(sender); self.operate_splitter.addWidget(receive_scroll)
        self.operate_splitter.setStretchFactor(0, 0); self.operate_splitter.setStretchFactor(1, 1)
        self._configure_workspace_splitter(self.operate_splitter, "operate", [390, 740])
        root.addWidget(self.operate_splitter, 1)
        self.combine = QLineEdit(str(DATA / f"receive-session-v{modem.VERSION}.json")); self.combine.setVisible(False)
        self._update_exchange_controls()
        self.audio_test_mode_changed(self.audio_test_mode.isChecked())
        return w

    def _refresh_station_identity(self):
        callsign = self.station_call.text().strip().upper()
        grid = self.station_grid.text().strip().upper()
        identity = callsign or "Station details not set"
        if callsign and grid:
            identity += f" · {grid}"
        self.station_identity.setText(identity)

    def _set_advanced_controls(self, enabled):
        """Reveal technical controls while preserving their saved values."""
        enabled = bool(enabled)
        self.settings.setValue("ui/advanced_controls", enabled)
        self.advanced_exchange_label.setVisible(enabled)
        receive_all_supported = self.selected_adapter().capabilities.receive_all_modes
        self.receive_all_modes.setVisible(enabled and receive_all_supported)
        self.receive_all_modes_label.setVisible(enabled and receive_all_supported)
        self.exchange_mode.setVisible(enabled)
        if hasattr(self, "rx_status"):
            self.rx_status.setVisible(enabled)
        if hasattr(self, "receive_group"):
            self.receive_group.setVisible(enabled)
        # Stage send controls are part of the everyday Operate workflow, not
        # an advanced-only setting. Keep their parent visible across backend
        # reconnects and advanced-control toggles.
        self.auto_stage_group.setVisible(self.exchange_mode.currentIndex() != 2)
        if hasattr(self, "rx_visual_stack"):
            self._update_backend_ui()

    def open_station_settings(self, page_index=None):
        if self._station_operation_active():
            QMessageBox.information(
                self, "Finish the current exchange",
                "Stop the active transmission, automatic exchange, or beacon before changing station settings.")
            return
        if getattr(self, "station_dialog", None) is not None:
            if page_index is not None:
                self.station_settings_tabs.setCurrentIndex(page_index)
            self._run_station_settings_dialog()
            return
        dialog = QDialog(self); self.station_dialog = dialog; dialog.setWindowTitle("Station settings")
        layout = QVBoxLayout(dialog)
        self.station_settings_tabs = QTabWidget(dialog)
        self.station_settings_tab_pages = {}
        layout.addWidget(self.station_settings_tabs, 1)

        def add_settings_page(title):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(*([SPACING["md"]] * 4))
            page_layout.setSpacing(SPACING["md"])
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(page)
            self.station_settings_tabs.addTab(scroll, title)
            self.station_settings_tab_pages[title] = scroll
            return page_layout

        station_page = add_settings_page("Station")
        identity = QGroupBox("Station identity")
        identity_form = QFormLayout(identity)
        identity_form.addRow("Call sign", self.station_call)
        identity_form.addRow("Grid square", self.station_grid)
        station_page.addWidget(identity)
        self.station_identity_hint = QLabel()
        self.station_identity_hint.setWordWrap(True)
        self.station_identity_hint.setAccessibleDescription(
            "Validation guidance for the station callsign and optional grid square.")
        self.station_call.textChanged.connect(self._update_station_identity_hint)
        self.station_grid.textChanged.connect(self._update_station_identity_hint)
        self._update_station_identity_hint()
        station_page.addWidget(self.station_identity_hint)
        identity_note = QLabel(
            "This identity appears on cards you create and is used when joining a Data2G group.")
        identity_note.setWordWrap(True)
        identity_note.setStyleSheet(semantic_text_style(padding="2px"))
        station_page.addWidget(identity_note)
        station_page.addStretch(1)

        appearance_page = add_settings_page("Appearance")
        appearance_note = QLabel(
            "Choose a larger interface text size for easier reading. "
            "Appearance changes are saved when you apply station settings.")
        appearance_note.setWordWrap(True)
        appearance_note.setStyleSheet(semantic_text_style(padding="2px"))
        appearance_page.addWidget(appearance_note)
        appearance_group = QGroupBox("Reading comfort")
        appearance_form = QFormLayout(appearance_group)
        self.interface_text_size = QComboBox()
        self.interface_text_size.addItem("Standard", 100)
        self.interface_text_size.addItem("Larger · 115%", 115)
        saved_text_size = self.settings.value("ui/interface_text_size", 100, type=int)
        self.interface_text_size.setCurrentIndex(max(
            0, self.interface_text_size.findData(saved_text_size)))
        self.interface_text_size.setAccessibleName("Interface text size")
        self.interface_text_size.setAccessibleDescription(
            "Choose standard or 115 percent interface text size. Apply to save, or Cancel to restore.")
        appearance_form.addRow("Interface text size", self.interface_text_size)
        appearance_page.addWidget(appearance_group)
        self.interface_text_preview = QLabel("Received card · W1ABC · FN42")
        self.interface_text_preview.setWordWrap(True)
        self.interface_text_preview.setStyleSheet(
            semantic_text_style("soft", padding="8px"))
        self.interface_text_preview.setAccessibleName("Interface text-size preview")
        self.interface_text_preview.setAccessibleDescription(
            "Preview text: Received card · W1ABC · FN42. It is shown at the selected interface text size; "
            "the application changes when you apply.")
        self.interface_text_size.currentIndexChanged.connect(
            self._update_interface_text_preview)
        appearance_page.addWidget(self.interface_text_preview)
        self._update_interface_text_preview()
        appearance_page.addStretch(1)

        advanced_page = add_settings_page("Advanced")
        advanced_note = QLabel(
            "Optional radio and sharing controls. Experimental modem modes remain separately opt-in.")
        advanced_note.setWordWrap(True)
        advanced_note.setStyleSheet(semantic_text_style(padding="2px"))
        advanced_page.addWidget(advanced_note)
        options = QGroupBox("Modem options")
        options_form = QFormLayout(options)
        options_form.addRow(self.audio_test_mode)
        options_form.addRow(self.cwid_enabled)
        options_form.addRow("CW ID message", self.cwid_message)
        copy_options = QWidget()
        copy_options_layout = QHBoxLayout(copy_options)
        copy_options_layout.setContentsMargins(0, 0, 0, 0)
        copy_options_layout.setSpacing(SPACING["sm"])
        copy_options_layout.addWidget(self.repeat_count)
        copy_options_layout.addWidget(self.interlaced_copies)
        copy_options_layout.addStretch(1)
        options_form.addRow("Copies per card", copy_options)
        self.repeat_count.show()
        self.repeat_count_label = options_form.labelForField(copy_options)
        advanced_page.addWidget(options)
        mobile_options = QGroupBox("Mobile control")
        mobile_form = QFormLayout(mobile_options)
        mobile_form.addRow(self.web_enabled)
        mobile_form.addRow("Web port", self.web_port)
        advanced_page.addWidget(mobile_options)
        advanced_page.addStretch(1)

        audio_page = add_settings_page("Audio & Radio")

        backend_group = QGroupBox("Transfer backend")
        backend_form = QFormLayout(backend_group)
        backend_form.addRow("Backend", self.backend_combo)
        backend_form.addRow(self.show_experimental_modes)
        backend_note = QLabel(
            "Configure the selected Data2G host or Experimental audio/radio path below before switching. "
            "A backend switch may interrupt reception.")
        backend_note.setWordWrap(True)
        backend_form.addRow(backend_note)
        audio_page.addWidget(backend_group)

        self.data2g_settings_group = QGroupBox("Data2G host")
        data2g_form = QFormLayout(self.data2g_settings_group)
        data2g_form.addRow("Connection", self.data2g_host_source)
        data2g_form.addRow("Host", self.data2g_host_input)
        data2g_form.addRow("Command port", self.data2g_command_port)
        data2g_form.addRow("KISS port", self.data2g_kiss_port)
        data2g_form.addRow("Local host bandwidth", self.data2g_host_bandwidth)
        self.data2g_radio_group = QGroupBox("Managed Data2G host radio/PTT")
        data2g_radio_form = QFormLayout(self.data2g_radio_group)
        data2g_radio_form.addRow("Host rigctld", self.data2g_rig_host)
        data2g_radio_form.addRow("Port", self.data2g_rig_port)
        data2g_radio_form.addRow("Host audio input", self.data2g_input_device)
        data2g_radio_form.addRow("Host audio output", self.data2g_output_device)
        data2g_form.addRow(self.data2g_radio_group)
        host_radio_note = QLabel(
            "Managed-host audio and rigctld settings apply only to a local server; remote "
            "hosts use their own radio settings. A successful API connection does not confirm host audio or "
            "PTT operation. Check the managed host status/log or the remote host before "
            "transmitting.")
        host_radio_note.setWordWrap(True)
        data2g_form.addRow(host_radio_note)
        data2g_form.addRow("Status", self.data2g_host_status)
        data2g_form.addRow(self.data2g_host_connect)
        audio_page.addWidget(self.data2g_settings_group)
        station = QGroupBox("Experimental modem audio")
        station_form = QFormLayout(station)
        station_form.addRow("Audio output", self.output_device); station_form.addRow("Audio input", self.input_device)
        audio_page.addWidget(station)
        self.experimental_audio_group = station
        self.cat_settings_group = QGroupBox("Experimental modem CAT / rigctld")
        cat_form = QFormLayout(self.cat_settings_group)
        cat_form.addRow("rigctld host", self.rig_host)
        cat_form.addRow("Port", self.rig_port)
        cat_buttons = QWidget(); cat_buttons_layout = QHBoxLayout(cat_buttons)
        cat_buttons_layout.setContentsMargins(0, 0, 0, 0)
        cat_buttons_layout.addWidget(self.rig_button); cat_buttons_layout.addWidget(self.rig_refresh)
        cat_form.addRow(cat_buttons)
        cat_note = QLabel("Used only by experimental local modems. Data2G modems control the radio through their host.")
        cat_note.setWordWrap(True)
        cat_note.setStyleSheet(semantic_text_style(padding="2px"))
        cat_form.addRow(cat_note)
        cat_form.addRow(self.auto_connect_cat)
        audio_page.addWidget(self.cat_settings_group)
        audio_page.addStretch(1)
        self.station_settings_tabs.tabBar().moveTab(3, 1)
        if page_index is not None:
            self.station_settings_tabs.setCurrentIndex(page_index)
        self._update_data2g_host_controls()
        self._update_backend_ui()
        self.station_settings_notice = QLabel("Changes are saved only when you apply them.")
        self.station_settings_notice.setWordWrap(True)
        self.station_settings_notice.setStyleSheet(semantic_text_style(padding="2px"))
        layout.addWidget(self.station_settings_notice)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.station_settings_apply_button = QPushButton("Apply")
        self.station_settings_apply_button.setObjectName("primaryAction")
        self.station_settings_apply_button.clicked.connect(
            lambda: self._apply_station_settings(reconnect=False))
        self.station_settings_reconnect_button = QPushButton("Apply and reconnect")
        self.station_settings_reconnect_button.clicked.connect(
            lambda: self._apply_station_settings(reconnect=True))
        self.station_settings_cancel_button = QPushButton("Cancel")
        self.station_settings_cancel_button.clicked.connect(dialog.reject)
        buttons.addWidget(self.station_settings_cancel_button)
        buttons.addWidget(self.station_settings_apply_button)
        buttons.addWidget(self.station_settings_reconnect_button)
        layout.addLayout(buttons)
        self._connect_station_settings_change_signals()
        # Keep the action row on screen even on short displays. The page
        # contents already scroll, so the dialog itself can fit the work area.
        available = dialog.screen().availableGeometry()
        width = min(900, max(560, int(available.width() * 0.92)))
        height = min(760, max(440, int(available.height() * 0.90)))
        dialog.resize(min(width, available.width()), min(height, available.height()))
        self._run_station_settings_dialog()

    def _station_operation_active(self):
        beacon_active = (getattr(self, "beacon_button", None) is not None and
                         self.beacon_button.isChecked())
        return bool(
            getattr(self, "auto_armed", False) or getattr(self, "ptt_active", False) or
            getattr(self, "tx_audio", None) is not None or
            (hasattr(self, "tx_timer") and self.tx_timer.isActive()) or
            getattr(self, "data2g_tx_waiting", None) is not None or
            getattr(self, "data2g_tx_total", 0) or getattr(self, "beacon_pending", False) or
            beacon_active)

    def _apply_interface_text_size(self, percent):
        percent = 115 if int(percent or 100) >= 115 else 100
        base_font = QFont(getattr(self, "_base_application_font", QApplication.font()))
        scale = percent / 100
        if base_font.pointSizeF() > 0:
            base_font.setPointSizeF(base_font.pointSizeF() * scale)
        elif base_font.pixelSize() > 0:
            base_font.setPixelSize(round(base_font.pixelSize() * scale))
        else:
            base_font.setPointSizeF(10 * scale)
        QApplication.setFont(base_font)
        self.setStyleSheet(build_app_stylesheet(text_scale=scale))
        self._refresh_listen_indicator_width()

    def _refresh_listen_indicator_width(self):
        if hasattr(self, "rx_status"):
            self.rx_status.setFixedHeight(self.rx_status.sizeHint().height())
        if not hasattr(self, "listen_indicator"):
            return
        receiver_states = ("● HOST DISCONNECTED", "● NOT CONNECTED", "● CONNECTING",
                           "● STARTING", "● RECEIVING SIGNAL", "● RECEIVING CARD",
                           "● DATA2G HOST RX", "● DATA2G HOST TX", "● NOT LISTENING",
                           "● TRANSMITTING", "● PROCESSING", "● LISTENING")
        state_text_width = max(
            self.listen_indicator.fontMetrics().horizontalAdvance(state)
            for state in receiver_states)
        self.listen_indicator.setMinimumWidth(state_text_width + SPACING["xl"])

    def _update_interface_text_preview(self, *_):
        if not hasattr(self, "interface_text_preview"):
            return
        percent = int(self.interface_text_size.currentData() or 100)
        scale = percent / 100
        font = QFont(self._base_application_font)
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * scale)
        elif font.pixelSize() > 0:
            font.setPixelSize(round(font.pixelSize() * scale))
        else:
            font.setPointSizeF(10 * scale)
        self.interface_text_preview.setFont(font)

    def _station_settings_values(self):
        controls = {
            "backend_combo": self.backend_combo,
            "station_call": self.station_call, "station_grid": self.station_grid,
            "cwid_enabled": self.cwid_enabled, "cwid_message": self.cwid_message,
            "interface_text_size": self.interface_text_size,
            "data2g_host_source": self.data2g_host_source,
            "data2g_host_input": self.data2g_host_input,
            "data2g_command_port": self.data2g_command_port,
            "data2g_kiss_port": self.data2g_kiss_port,
            "data2g_host_bandwidth": self.data2g_host_bandwidth,
            "data2g_rig_host": self.data2g_rig_host,
            "data2g_rig_port": self.data2g_rig_port,
            "data2g_input_device": self.data2g_input_device,
            "data2g_output_device": self.data2g_output_device,
            "output_device": self.output_device, "input_device": self.input_device,
            "repeat_count": self.repeat_count,
            "interlaced_copies": self.interlaced_copies,
            "rig_host": self.rig_host, "rig_port": self.rig_port,
            "auto_connect_cat": self.auto_connect_cat,
            "audio_test_mode": self.audio_test_mode,
            "show_experimental_modes": self.show_experimental_modes,
            "web_enabled": self.web_enabled, "web_port": self.web_port,
        }
        values = {}
        for name, widget in controls.items():
            if isinstance(widget, QLineEdit):
                values[name] = widget.text()
            elif isinstance(widget, QComboBox):
                values[name] = widget.currentIndex()
            elif isinstance(widget, QSpinBox):
                values[name] = widget.value()
            elif isinstance(widget, QCheckBox):
                values[name] = widget.isChecked()
        return {"values": values, "audio_errors": dict(self.data2g_audio_config_errors)}

    def _station_connection_keys(self):
        return ("backend_combo", "station_call", "data2g_host_source", "data2g_host_input",
                "data2g_command_port", "data2g_kiss_port", "data2g_host_bandwidth",
                "data2g_rig_host", "data2g_rig_port", "data2g_input_device",
                "data2g_output_device", "rig_host", "rig_port", "input_device",
                "output_device")

    def _station_settings_connection_changed(self, before, after):
        keys = self._station_connection_keys()
        return any(before["values"].get(key) != after["values"].get(key) for key in keys)

    def _station_settings_connection_edits_pending(self):
        if (not getattr(self, "_station_settings_editing", False) or
                getattr(self, "_station_settings_baseline", None) is None):
            return False
        return self._station_settings_connection_changed(
            self._station_settings_baseline, self._station_settings_values())

    def _station_connection_is_active(self):
        backend = self.backend_combo.currentData()
        if (getattr(self, "_station_settings_editing", False) and
                getattr(self, "_station_settings_baseline", None) is not None):
            old_index = self._station_settings_baseline["values"].get("backend_combo")
            backend = self.backend_combo.itemData(old_index)
        return (DATA2G_ADAPTER.is_connected(self.data2g_session)
                if backend == "data2g" else self.rig.connected())

    def _connect_station_settings_change_signals(self):
        controls = (self.backend_combo, self.station_call, self.station_grid, self.cwid_enabled,
                    self.cwid_message, self.data2g_host_source,
                    self.interface_text_size,
                    self.data2g_host_input, self.data2g_command_port,
                    self.data2g_kiss_port, self.data2g_host_bandwidth,
                    self.data2g_rig_host, self.data2g_rig_port,
                    self.data2g_input_device, self.data2g_output_device,
                    self.output_device, self.input_device, self.repeat_count, self.interlaced_copies, self.rig_host,
                    self.rig_port, self.auto_connect_cat, self.audio_test_mode,
                    self.show_experimental_modes, self.web_enabled, self.web_port)
        for widget in controls:
            signal = (widget.textChanged if isinstance(widget, QLineEdit) else
                      widget.currentIndexChanged if isinstance(widget, QComboBox) else
                      widget.valueChanged if isinstance(widget, QSpinBox) else
                      widget.toggled)
            signal.connect(self._update_station_settings_actions)

    def _update_station_settings_actions(self, *_):
        if not getattr(self, "_station_settings_editing", False):
            return
        current = self._station_settings_values()
        before = self._station_settings_baseline
        changed = current != before
        connection_changed = self._station_settings_connection_changed(before, current)
        reconnect = connection_changed and self._station_connection_is_active()
        active = self._station_operation_active()
        self.station_settings_apply_button.setVisible(not reconnect)
        self.station_settings_reconnect_button.setVisible(reconnect)
        self.station_settings_apply_button.setEnabled(not active)
        self.station_settings_reconnect_button.setEnabled(not active)
        self.station_settings_cancel_button.setText("Cancel" if changed else "Close")
        self.station_settings_cancel_button.setToolTip(
            "Discard pending settings edits; live connection actions remain in effect."
            if changed else "Close settings; live connection actions remain in effect.")
        if active:
            message = "Stop the active transmission, exchange, or beacon before applying station settings."
            tone = "warning"
        elif reconnect:
            message = "Applying will briefly interrupt reception, then reconnect with the updated settings."
            tone = "warning"
        elif connection_changed:
            message = "Apply or cancel connection changes before connecting; pending values are not used yet."
            tone = "warning"
        elif changed:
            message = "Your changes are ready to apply."
            tone = "muted"
        else:
            message = "No pending settings edits. Apply or Close to return to the app."
            tone = "muted"
        self.station_settings_notice.setText(message)
        self.station_settings_notice.setStyleSheet(semantic_text_style(tone, padding="2px"))
        if self.backend_combo.currentData() == "data2g":
            self._update_data2g_host_controls()
        else:
            self.rig_button.setEnabled(not connection_changed or self.rig.connected())
            self.rig_refresh.setEnabled(not connection_changed and self.rig.connected())
            self.rig_button.setToolTip(
                "Apply or cancel the pending rig connection changes before connecting."
                if connection_changed and not self.rig.connected() else "")
            self.rig_refresh.setToolTip(
                "Apply or cancel the pending rig connection changes before reading frequency."
                if connection_changed else "")
        # Backend choice is staged until Apply, but the settings page should
        # immediately show and enable the configuration for that destination.
        # Connection buttons remain guarded above until the handoff is applied.
        self._update_backend_ui()

    def _run_station_settings_dialog(self):
        if self._station_operation_active():
            return
        self._station_settings_baseline = self._station_settings_values()
        self._station_settings_editing = True
        self._station_settings_reconnect = None
        self._update_station_settings_actions()
        result = self.station_dialog.exec()
        if result != QDialog.DialogCode.Accepted:
            self._restore_station_settings(self._station_settings_baseline)
            self._station_settings_editing = False
            self._station_settings_baseline = None
            self._station_settings_reconnect = None
            self._restore_station_settings_connection_controls()
            return
        self._station_settings_editing = False
        reconnect = self._station_settings_reconnect
        backend_changed = (
            self._station_settings_baseline["values"].get("backend_combo") !=
            self.backend_combo.currentIndex())
        self._station_settings_baseline = None
        self._station_settings_reconnect = None
        self._restore_station_settings_connection_controls()
        if backend_changed:
            self._on_backend_selection_changed()
        if reconnect == "data2g":
            self._reconnect_data2g_after_settings()
        elif reconnect == "rig":
            self._reconnect_rig_after_settings()

    def _restore_station_settings_connection_controls(self):
        self._update_data2g_host_controls()
        host_busy = bool(self.data2g_connecting or self.data2g_local_starting or
                         self.data2g_closing or self.data2g_handoff_pending or
                         DATA2G_ADAPTER.is_connected(self.data2g_session))
        self.rig_button.setEnabled(not host_busy)
        self.rig_refresh.setEnabled(not host_busy and self.rig.connected())
        self._update_backend_ui()
        # Station Settings temporarily reparents several live controls. Restore
        # the Operate action visibility after both Apply and Cancel so stage
        # send buttons cannot inherit a hidden state from the dialog lifecycle.
        self._update_exchange_controls()

    def _restore_station_settings(self, snapshot):
        controls = {
            "backend_combo": self.backend_combo,
            "station_call": self.station_call, "station_grid": self.station_grid,
            "cwid_enabled": self.cwid_enabled, "cwid_message": self.cwid_message,
            "interface_text_size": self.interface_text_size,
            "data2g_host_source": self.data2g_host_source,
            "data2g_host_input": self.data2g_host_input,
            "data2g_command_port": self.data2g_command_port,
            "data2g_kiss_port": self.data2g_kiss_port,
            "data2g_host_bandwidth": self.data2g_host_bandwidth,
            "data2g_rig_host": self.data2g_rig_host,
            "data2g_rig_port": self.data2g_rig_port,
            "data2g_input_device": self.data2g_input_device,
            "data2g_output_device": self.data2g_output_device,
            "output_device": self.output_device, "input_device": self.input_device,
            "repeat_count": self.repeat_count,
            "interlaced_copies": self.interlaced_copies,
            "rig_host": self.rig_host, "rig_port": self.rig_port,
            "auto_connect_cat": self.auto_connect_cat,
            "audio_test_mode": self.audio_test_mode,
            "show_experimental_modes": self.show_experimental_modes,
            "web_enabled": self.web_enabled, "web_port": self.web_port,
        }
        blockers = [QSignalBlocker(widget) for widget in controls.values()]
        for name, widget in controls.items():
            value = snapshot["values"][name]
            if isinstance(widget, QLineEdit):
                widget.setText(value)
            elif isinstance(widget, QComboBox):
                widget.setCurrentIndex(value)
            elif isinstance(widget, QSpinBox):
                widget.setValue(value)
            elif isinstance(widget, QCheckBox):
                widget.setChecked(value)
        self.data2g_audio_config_errors = dict(snapshot["audio_errors"])
        del blockers
        self._apply_interface_text_size(self.interface_text_size.currentData())
        cwid_applicable = self.backend_combo.currentData() != "data2g"
        self.cwid_enabled.setEnabled(cwid_applicable)
        self.cwid_message.setEnabled(cwid_applicable and self.cwid_enabled.isChecked())
        self._refresh_station_identity()
        self._update_station_identity_hint()
        self.audio_test_mode_changed(self.audio_test_mode.isChecked())
        self._on_experimental_visibility_changed(self.show_experimental_modes.isChecked())
        self._update_data2g_host_controls()
        self.refresh_tx_button()

    def _apply_station_settings(self, reconnect=False):
        if self._station_operation_active():
            self._update_station_settings_actions()
            return
        if self._station_settings_values() == self._station_settings_baseline:
            self.station_dialog.accept()
            return
        try:
            DATA2G_ADAPTER.validate_station_call(self.station_call.text().strip())
        except ValueError as exc:
            self.station_settings_notice.setText(f"Enter a valid station call sign · {exc}")
            self.station_settings_notice.setStyleSheet(semantic_text_style("warning", padding="2px"))
            self.station_settings_tabs.setCurrentWidget(
                self.station_settings_tab_pages["Station"])
            self.station_call.setFocus()
            self.station_call.selectAll()
            self._update_station_identity_hint()
            return
        grid = self.station_grid.text().strip().upper()
        if grid and not re.fullmatch(
                r"[A-R]{2}(?:[0-9]{2}(?:[A-X]{2}(?:[0-9]{2}(?:[A-X]{2})?)?)?)?", grid):
            self.station_settings_notice.setText(
                "Enter a valid Maidenhead grid square, such as FN42, or leave it blank.")
            self.station_settings_notice.setStyleSheet(semantic_text_style("warning", padding="2px"))
            self.station_settings_tabs.setCurrentWidget(
                self.station_settings_tab_pages["Station"])
            self.station_grid.setFocus()
            self.station_grid.selectAll()
            self._update_station_identity_hint()
            return
        before = self._station_settings_baseline
        after = self._station_settings_values()
        connection_changed = self._station_settings_connection_changed(before, after)
        connected = self._station_connection_is_active()
        if connection_changed and connected and not reconnect:
            self._update_station_settings_actions()
            return
        if (not self.show_experimental_modes.isChecked() and
                self.backend_combo.currentData() == "experimental"):
            self.station_settings_notice.setText(
                "Select Data2G in Audio & Radio before hiding experimental modes; the active backend will be preserved.")
            self.station_settings_notice.setStyleSheet(semantic_text_style("warning", padding="2px"))
            return
        self._station_settings_editing = False
        try:
            self._on_experimental_visibility_changed(
                self.show_experimental_modes.isChecked())
            self.save_station_settings()
            self._apply_interface_text_size(self.interface_text_size.currentData())
            self._save_audio_device(self.input_device, "audio/input_device", force=True)
            self._save_audio_device(self.output_device, "audio/output_device", force=True)
            self._save_audio_device(self.data2g_input_device, "data2g/audio_input_device", force=True)
            self._save_audio_device(self.data2g_output_device, "data2g/audio_output_device", force=True)
            for name, setting in (("data2g_input_device", "data2g/audio_input_device"),
                                  ("data2g_output_device", "data2g/audio_output_device")):
                if before["values"].get(name) != after["values"].get(name):
                    self._data2g_audio_device_changed(setting)
        finally:
            self._station_settings_editing = True
        if reconnect and connection_changed and connected:
            self._station_settings_reconnect = (
                "data2g" if self.backend_combo.currentData() == "data2g" else "rig")
        self.station_dialog.accept()

    def _reconnect_data2g_after_settings(self):
        session = self.data2g_session
        if session is None or not DATA2G_ADAPTER.is_connected(session):
            return
        self.data2g_reconnect_after_settings = True
        self.data2g_closing = True
        self.data2g_host_status.setText("Applying settings · reconnecting Data2G…")
        self._update_data2g_host_controls()
        threading.Thread(target=DATA2G_ADAPTER.disconnect, args=(session,), daemon=True,
                         name="data2g-settings-reconnect").start()

    def _reconnect_rig_after_settings(self):
        if not self.rig.connected():
            return
        self.rig_reconnect_after_settings = True
        self.rig.disconnect()

    def _update_data2g_host_controls(self, *_):
        if not hasattr(self, "data2g_host_input"):
            return
        remote = self.data2g_host_source.currentData() != "local"
        connected = bool(DATA2G_ADAPTER.is_connected(self.data2g_session))
        monitor_locked = bool(connected and self.data2g_background_monitor and
                              self.backend_combo.currentData() == "experimental")
        busy = (self.data2g_connecting or self.data2g_local_starting or self.data2g_closing or
                self.data2g_handoff_pending)
        settings_available = not busy and not monitor_locked
        self.data2g_host_source.setEnabled(settings_available)
        self.data2g_host_input.setEnabled(remote and settings_available)
        self.data2g_command_port.setEnabled(settings_available)
        self.data2g_kiss_port.setEnabled(settings_available)
        self.data2g_host_bandwidth.setEnabled(not remote and settings_available)
        local_audio_ready = (remote or not self.data2g_audio_config_errors)
        pending_connection_changes = (
            self.backend_combo.currentData() == "data2g" and
            self._station_settings_connection_edits_pending())
        self.data2g_host_connect.setText(
            "Monitoring Data2G" if monitor_locked else
            "Disconnect Data2G host" if connected else
            "Connect and find available modes")
        self.data2g_host_connect.setEnabled(
            not self.data2g_connecting and not self.data2g_local_starting and
            not self.data2g_closing and
            not self.data2g_handoff_pending and
            not monitor_locked and
            (connected or not pending_connection_changes) and
            (connected or local_audio_ready) and
            (self.backend_combo.currentData() == "data2g" or connected))
        connect_help = (
            "Data2G is managed as a receive-only monitor while Experimental is selected."
            if monitor_locked else
            ("Disconnect the current Data2G session; apply or cancel changes before reconnecting."
             if connected else "Apply or cancel the pending connection changes before connecting.")
            if pending_connection_changes else
            self._data2g_audio_config_error_message() if not local_audio_ready else
            ("Disconnect from the current Data2G host."
             if connected else "Connect to the selected Data2G host and discover available modes."))
        self.data2g_host_connect.setToolTip(connect_help)
        self.data2g_host_connect.setAccessibleDescription(connect_help)
        if hasattr(self, "data2g_radio_group"):
            self.data2g_radio_group.setVisible(not remote)
            self.data2g_radio_group.setEnabled(settings_available)
        if hasattr(self, "data2g_input_device"):
            self.data2g_input_device.setEnabled(not remote and settings_available)
            self.data2g_output_device.setEnabled(not remote and settings_available)
        self._update_backend_ui()

    def _start_local_data2g_host(self):
        if self.data2g_local_process and self.data2g_local_process.poll() is None:
            return True
        try:
            import importlib.util
            if importlib.util.find_spec("data2g.host") is None:
                raise ModuleNotFoundError("data2g.host")
        except (ImportError, ModuleNotFoundError, ValueError):
            advice = "reinstall Pixel QSO with its standard Data2G runtime dependencies"
            self.data2g_host_status.setText("Local host unavailable · " + advice)
            return False
        try:
            # The managed host has its own PortAudio catalog. When its
            # dedicated selector is left at System default, pass the station
            # audio selection through by name; an empty host selector used to
            # silently omit both CLI options and ignore the selected devices.
            if (self.data2g_background_monitor and
                    self.backend_combo.currentData() == "experimental" and
                    self.input_device.currentData()):
                input_selector = data2g_host_audio_selector_for_name(
                    "input", self.input_device.currentData().description())
            else:
                input_selector = (
                    data2g_audio_device_selector(
                        self.data2g_input_device.currentData(),
                        self.data2g_input_device.currentText())
                    if self.data2g_input_device.count() else "")
            output_selector = (
                data2g_audio_device_selector(
                    self.data2g_output_device.currentData(),
                    self.data2g_output_device.currentText())
                if self.data2g_output_device.count() else "")
            if not input_selector and self.input_device.currentData():
                input_selector = data2g_host_audio_selector_for_name(
                    "input", self.input_device.currentData().description())
            if not output_selector and self.output_device.currentData():
                output_selector = data2g_host_audio_selector_for_name(
                    "output", self.output_device.currentData().description())
            args = local_host_arguments(
                command_port=self.data2g_command_port.value(),
                kiss_port=self.data2g_kiss_port.value(),
                callsign=self.station_call.text(),
                input_device=input_selector,
                output_device=output_selector,
                rig_host=self.data2g_rig_host.text().strip(),
                rig_port=self.data2g_rig_port.value(),
                bandwidth_hz=int(self.data2g_host_bandwidth.currentData()),
                record_dir=str(DATA / "data2g-recordings"))
        except ValueError as exc:
            self.data2g_host_status.setText("Local host settings are invalid · " + str(exc))
            return False
        log_path = DATA / "data2g-host.log"
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            self.data2g_local_log_offset = log_path.stat().st_size
        except OSError:
            self.data2g_local_log_offset = 0
        try:
            self.data2g_local_log = log_path.open("a", encoding="utf-8")
        except OSError as exc:
            self.data2g_host_status.setText(
                f"Could not prepare local Data2G host log: {exc}")
            return False
        try:
            if not self.data2g_waterfall_socket.localPort():
                self.data2g_host_status.setText(
                    "Could not open the local waterfall audio tap.")
                return False
            self.data2g_local_process = subprocess.Popen(
                local_host_command([*args, "--pixelqso-waterfall-port",
                                    str(self.data2g_waterfall_socket.localPort())]),
                cwd=str(DATA), stdout=self.data2g_local_log,
                stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))
        except OSError as exc:
            self.data2g_local_log.close()
            self.data2g_local_log = None
            self.data2g_host_status.setText(f"Could not start local Data2G host: {exc}")
            return False
        self.data2g_local_log_monitor_offset = self.data2g_local_log_offset
        self.data2g_waterfall_timer.start()
        self.data2g_local_log_pending = ""
        self.data2g_ptt_warnings.clear()
        self.data2g_local_starting = True
        self.data2g_host_log_timer.start()
        self.settings.setValue("data2g/local_host", True)
        self.data2g_host_status.setText("Starting local Data2G host…")
        self._update_data2g_host_controls()
        self.refresh_tx_button()
        QTimer.singleShot(500, self._connect_after_local_data2g_start)
        return False

    def _connect_after_local_data2g_start(self):
        self.data2g_local_starting = False
        if self._closing:
            self._stop_local_data2g_host()
            return
        if ((self.backend_combo.currentData() != "data2g" and
             not self.data2g_background_monitor) or
                self.data2g_host_source.currentData() != "local"):
            self._stop_local_data2g_host()
            if self.backend_combo.currentData() == "experimental":
                self._resume_experimental_backend()
            return
        self._connect_data2g_host()

    def _local_data2g_connect_failure_hint(self):
        process = self.data2g_local_process
        if process is None or self.data2g_host_source.currentData() != "local":
            return ""
        log_path = DATA / "data2g-host.log"
        try:
            size = log_path.stat().st_size
            with log_path.open("rb") as log_file:
                log_file.seek(max(self.data2g_local_log_offset, size - 12000))
                log_text = log_file.read().decode("utf-8", "replace").lower()
        except OSError:
            log_text = ""
        if any(marker in log_text for marker in
               ("address already in use", "eaddrinuse", "winerror 10048", "errno 98")):
            return "A local Data2G port is already in use; choose free command/KISS ports in Station settings."
        missing_audio_device = any(
            marker in log_text for marker in
            ("no input device matching", "no output device matching"))
        audio_open_failure = (
            any(marker in log_text for marker in
                ("pyaudio", "portaudio", "pa.open", "input device", "output device")) and
            any(marker in log_text for marker in
                ("error", "failed", "invalid", "no default", "exception", "cannot")))
        if missing_audio_device or audio_open_failure:
            return "Local Data2G audio did not start; check the selected input/output devices and data2g-host.log."
        if process.poll() is not None:
            return "The local Data2G host exited during startup; see data2g-host.log."
        return "The local Data2G listeners did not become available; see data2g-host.log."

    def _stop_local_data2g_host(self):
        self.data2g_local_starting = False
        self.data2g_waterfall_timer.stop()
        process = self.data2g_local_process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if process is not None:
            self._poll_local_data2g_host_log()
        else:
            self.data2g_host_log_timer.stop()
        self.data2g_local_process = None
        if self.data2g_local_log:
            self.data2g_local_log.close()
            self.data2g_local_log = None
        if hasattr(self, "data2g_host_source"):
            self._update_data2g_host_controls()
            self.refresh_tx_button()

    def _drain_data2g_waterfall_audio(self):
        sock = self.data2g_waterfall_socket
        if self.data2g_local_process is None:
            self.data2g_waterfall_timer.stop()
            return
        while sock.hasPendingDatagrams():
            packet = bytes(sock.receiveDatagram().data())
            if len(packet) < 8:
                continue
            rate = int.from_bytes(packet[:4], "big")
            if rate < 8000 or rate > 192000:
                continue
            self.rx_waterfall.sample_rate = rate
            self.rx_waterfall.device_name = f"Data2G local input · {rate // 1000} kHz"
            self.rx_waterfall.feed_float32(packet[4:])

    def _poll_local_data2g_host_log(self):
        process = self.data2g_local_process
        if process is None:
            self.data2g_host_log_timer.stop()
            return
        log_path = DATA / "data2g-host.log"
        try:
            with log_path.open("rb") as log_file:
                log_file.seek(self.data2g_local_log_monitor_offset)
                chunk = log_file.read(65536)
                self.data2g_local_log_monitor_offset += len(chunk)
        except OSError:
            chunk = b""
        combined = self.data2g_local_log_pending + chunk.decode("utf-8", "replace")
        lines = combined.splitlines()
        if combined.endswith(("\n", "\r")):
            self.data2g_local_log_pending = ""
        elif lines:
            self.data2g_local_log_pending = lines.pop()
        else:
            self.data2g_local_log_pending = combined
        stopped = process.poll() is not None
        if stopped:
            if self.data2g_local_log_pending:
                lines.append(self.data2g_local_log_pending)
                self.data2g_local_log_pending = ""
            self.data2g_host_log_timer.stop()
        for line in lines:
            lowered = line.lower()
            if ("ptt on failed" in lowered or
                    ("to ptt on" in lowered and "rigctld answered" in lowered)):
                warning = "on"
                message = ("Managed Data2G host could not key the radio through rigctld. "
                           "Check its rigctld connection and settings; see data2g-host.log.")
            elif ("ptt off failed" in lowered or
                  ("to ptt off" in lowered and "rigctld answered" in lowered)):
                warning = "off"
                message = ("Managed Data2G host could not confirm PTT release through rigctld. "
                           "Verify the radio is unkeyed; see data2g-host.log.")
            else:
                continue
            if warning not in self.data2g_ptt_warnings:
                self.data2g_ptt_warnings.add(warning)
                self.data2g_ptt_failed = True
                self.settings.setValue("data2g/ptt_failed", True)
                self.settings.sync()
                self._data2g_host_error(message)
                self.refresh_tx_button()
                self._update_backend_ui()
                self._refresh_transfer_summary()
                if self.auto_armed:
                    self.stop_auto_exchange(
                        "Automatic exchange stopped after a managed-host PTT failure; "
                        "verify the radio is unkeyed before reconnecting.")

    def _connect_data2g_host(self):
        if (self._closing or self.data2g_connecting or self.data2g_local_starting or
                self.data2g_closing or self.data2g_handoff_pending):
            return
        if DATA2G_ADAPTER.is_connected(self.data2g_session):
            self.data2g_closing = True
            self.data2g_host_status.setText("Disconnecting…")
            self.data2g_host_connect.setEnabled(False)
            self.refresh_tx_button()
            self._update_data2g_host_controls()
            import threading
            threading.Thread(target=DATA2G_ADAPTER.disconnect, args=(self.data2g_session,), daemon=True,
                             name="data2g-close").start()
            return
        if (self.backend_combo.currentData() == "data2g" and
                self._station_settings_connection_edits_pending()):
            self.data2g_host_status.setText(
                "Apply or cancel the pending connection changes before connecting.")
            return
        if (self.data2g_host_source.currentData() == "local" and
                self.data2g_audio_config_errors):
            self.data2g_host_status.setText(self._data2g_audio_config_error_message())
            return
        try:
            station_call = DATA2G_ADAPTER.validate_station_call(
                self.station_call.text())
        except ValueError as exc:
            self.data2g_host_status.setText(f"Invalid station call · {exc}")
            return
        try:
            validate_data2g_ports(self.data2g_command_port.value(),
                                  self.data2g_kiss_port.value())
        except ValueError as exc:
            self.data2g_host_status.setText(f"Invalid Data2G ports · {exc}")
            return
        background_monitor = (self.backend_combo.currentData() == "experimental" and
                              self.data2g_background_monitor)
        if (not background_monitor and
                self.rig.socket.state() != QTcpSocket.SocketState.UnconnectedState):
            self.data2g_handoff_pending = True
            self.data2g_handoff_deadline = time.monotonic() + 3.0
            self.data2g_host_status.setText("Disconnecting CAT before Data2G takes radio control…")
            self.data2g_host_connect.setEnabled(False)
            if self.rx_timer.isActive():
                self._discard_receive_for_tx()
            self.rig.disconnect()
            QTimer.singleShot(25, self._finish_data2g_radio_handoff)
            return
        local_host = self.data2g_host_source.currentData() == "local"
        if local_host and self.data2g_local_process and self.data2g_local_process.poll() is not None:
            hint = self._local_data2g_connect_failure_hint()
            hint = hint or "The local Data2G host exited during startup; see data2g-host.log."
            self.data2g_last_host_error = hint
            self.data2g_host_status.setText("Local Data2G host exited · " + hint)
            self.exchange_log.append("Data2G host: " + hint)
            self.rx_status.setText(hint)
            self._stop_local_data2g_host()
            self.data2g_host_connect.setEnabled(True)
            return
        if local_host and not self.data2g_local_process:
            self._start_local_data2g_host()
            return
        host = "127.0.0.1" if local_host else self.data2g_host_input.text().strip()
        if not host:
            self.data2g_host_status.setText("Enter a host name or address")
            return
        self.data2g_channel_busy = False
        self.data2g_last_host_error = None
        self.data2g_ack_timeout_ms = DATA2G_ACK_TIMEOUT_MS
        if not local_host:
            self.settings.setValue("data2g/host", host)
            self.settings.setValue("data2g/local_host", False)
        self.data2g_connecting = True
        self.data2g_host_status.setText(f"Connecting to {host}…")
        self.data2g_host_connect.setEnabled(False)
        self._update_backend_ui()
        self._update_data2g_host_controls()
        session = DATA2G_ADAPTER.create_session(
            host, self.data2g_command_port.value(), self.data2g_kiss_port.value(),
            callsign=station_call,
            connect_retry_window=6.0 if local_host else 0.0,
            on_frame=lambda port, frame: self.data2g_signals.frame.emit(session, port, frame),
            on_ack=lambda port, tag: self.data2g_signals.ack.emit(session, port, tag),
            on_status=lambda message: self.data2g_signals.status.emit(session, message),
            on_error=lambda message: self.data2g_signals.error.emit(session, message),
            on_closed=lambda: self.data2g_signals.closed.emit(session))
        self.data2g_session = session
        worker = Data2GConnectWorker(session, self.data2g_signals)
        self.data2g_connect_worker = worker
        self.decode_pool.start(worker)

    def _finish_data2g_radio_handoff(self):
        if self._closing or not self.data2g_handoff_pending:
            return
        if self.rig.socket.state() != QTcpSocket.SocketState.UnconnectedState:
            if time.monotonic() >= self.data2g_handoff_deadline:
                self.data2g_handoff_pending = False
                self.data2g_host_connect.setEnabled(True)
                self.data2g_host_status.setText("CAT did not disconnect; Data2G was not started")
                self._update_data2g_host_controls()
                return
            QTimer.singleShot(25, self._finish_data2g_radio_handoff)
            return
        self.data2g_handoff_pending = False
        self._connect_data2g_host()

    def _data2g_connect_finished(self, session, modes, error, mode_checks=None):
        if session is not self.data2g_session:
            return
        closed_during_connect = self.data2g_closed_during_connect is session
        intentional_handoff = (
            self.backend_combo.currentData() == "experimental" and
            (closed_during_connect or self.data2g_closing))
        if self.data2g_closed_during_connect is session:
            self.data2g_closed_during_connect = None
        if closed_during_connect and not error:
            error = "Data2G host disconnected during mode discovery"
        self.data2g_connect_worker = None
        self.data2g_connecting = False
        self.data2g_host_connect.setEnabled(True)
        self._update_data2g_host_controls()
        if intentional_handoff and not error:
            # Discovery may win the race with the close request. Keep the
            # Data2G session as the owner until its closed callback arrives;
            # that callback performs the local CAT/audio handoff.
            self.data2g_host_status.setText(
                "Disconnecting Data2G before Experimental modems take radio control…")
            self.refresh_tx_button()
            return
        if error:
            hint = "" if intentional_handoff else self._local_data2g_connect_failure_hint()
            visible_error = hint or error
            self.data2g_last_host_error = None if intentional_handoff else visible_error
            self.data2g_session = None
            self.data2g_closing = False
            self.data2g_modes = {}
            register_data2g_mode_backends(self.data2g_modes)
            self._stop_local_data2g_host()
            if intentional_handoff:
                self.data2g_host_status.setText(
                    "Data2G closed · Experimental modems selected")
                self.exchange_log.append(
                    "Data2G host: connection closed for Experimental modem handoff.")
                self.rx_status.setText("Experimental modem selected")
            else:
                self.data2g_host_status.setText(
                    "Connection failed: " + error + (" · " + hint if hint else ""))
                self.exchange_log.append("Data2G host: " + visible_error)
                self.rx_status.setText(visible_error)
            self._refresh_mode_options()
            if self.on_air and not intentional_handoff:
                self.on_air.emit("host_connect_failed", {"error": error})
            self.refresh_tx_button()
            if self.backend_combo.currentData() == "experimental":
                self._resume_experimental_backend()
            return
        mode_checks = mode_checks or {}
        self.data2g_modes = host_mode_backends(
            modes, (name for name, refusal in mode_checks.items() if refusal is None))
        register_data2g_mode_backends(self.data2g_modes)
        self.settings.setValue("data2g/modes", json.dumps([
            mode.__dict__ for mode in modes], separators=(",", ":")))
        self.settings.setValue("data2g/mode_refusals", json.dumps(
            {name: refusal for name, refusal in mode_checks.items() if refusal},
            separators=(",", ":")))
        usable_modes = {key: backend for key, backend in self.data2g_modes.items()
                        if backend.usable}
        names = ", ".join(backend.mode_name for backend in usable_modes.values())
        if self.on_air:
            self.on_air.emit("host_connected", {
                "port": DATA2G_ADAPTER.group_port(self.data2g_session),
                "modes": [mode.__dict__ for mode in modes],
                "supported_modes": names,
            })
        rejected_count = sum(bool(refusal) for refusal in mode_checks.values())
        capacity_rejected = sum(not backend.usable and not backend.disabled_reason.startswith(
            "The host refused") for backend in self.data2g_modes.values())
        self.data2g_host_status.setText(
            f"Connected · port {DATA2G_ADAPTER.group_port(self.data2g_session)} · "
            f"{len(usable_modes)} usable broadcast modes" +
            (f" · {rejected_count} refused for this group" if rejected_count else "") +
            (f" · {capacity_rejected} below card capacity" if capacity_rejected else "") +
            (" · background receive monitor; Experimental retains CAT" if
             self.data2g_background_monitor and
             self.backend_combo.currentData() == "experimental" else
             " · host audio/PTT readiness unverified"))
        self.data2g_host_connect.setText("Disconnect Data2G host")
        requested_mode = os.environ.get("PIXELQSO_DATA2G_MODE", "").strip()
        saved_mode = str(self.settings.value("transfer/data2g_mode", DEFAULT_MODE_KEY))
        preset = requested_mode.lower() if requested_mode.lower() in {"robust", "fast"} else None
        if preset:
            preferred = preferred_data2g_mode_key(self.data2g_modes, preset)
        elif requested_mode:
            preferred = preferred_data2g_mode_key(self.data2g_modes, requested_mode)
        elif saved_mode in {"data2g_1200_robust", "data2g_1200_fast"}:
            preferred = preferred_data2g_mode_key(
                self.data2g_modes, "robust" if saved_mode.endswith("robust") else "fast")
        elif saved_mode in self.data2g_modes:
            preferred = saved_mode
        else:
            preferred = preferred_data2g_mode_key(self.data2g_modes, "robust")
        self._refresh_mode_options(preferred)
        selected_backend = self.selected_backend()
        saved_backend = self.data2g_modes.get(saved_mode)
        if (not requested_mode and is_data2g_mode_key(saved_mode) and
                (saved_backend is None or not saved_backend.usable)):
            reason = (saved_backend.disabled_reason if saved_backend is not None else
                      "the connected host no longer advertises this mode")
            replacement = (f"Using {selected_backend.mode_name}."
                           if (self.selected_adapter().capabilities.kind is AdapterKind.FRAME_TRANSPORT and
                               selected_backend is not None and
                               getattr(selected_backend, "usable", False)) else
                           "No Data2G mode is currently usable for transmission.")
            self.statusBar().showMessage(
                f"Saved Data2G mode {data2g_mode_name(saved_mode)!r} unavailable: "
                f"{reason} {replacement}", 15000)
        if (self.selected_adapter().capabilities.kind is AdapterKind.FRAME_TRANSPORT and
                selected_backend is not None and getattr(selected_backend, "usable", False)):
            self._on_mode_selection_changed()
        elif not usable_modes:
            self.data2g_host_status.setText(
                "Connected for receive · no mode fits this broadcast group's control and frame limits · "
                "host audio/PTT readiness unverified")
        self.refresh_tx_button()

    def _data2g_host_closed(self, session):
        if session is not self.data2g_session:
            return
        if self.data2g_connecting:
            self.data2g_closed_during_connect = session
            return
        self.data2g_closing = False
        if self.on_air:
            self.on_air.emit("host_disconnected", {})
        if self.stop_tx_btn.isVisible():
            self._finish_data2g_transmission("Data2G host disconnected; any in-flight transmission status is unknown.")
        if self.auto_armed:
            self.stop_auto_exchange(
                "Automatic exchange stopped because the Data2G host disconnected.")
        self.data2g_session = None
        self.data2g_channel_busy = False
        self.data2g_switch_button.hide()
        if self.receive_activity_pending:
            self._hide_receive_preview()
        if self.backend_combo.currentData() == "data2g":
            self.listen_indicator.setText("● HOST DISCONNECTED")
        self.data2g_modes = {}
        register_data2g_mode_backends(self.data2g_modes)
        self.data2g_host_connect.setEnabled(True)
        self.data2g_host_connect.setText("Connect and find available modes")
        self.data2g_host_status.setText(
            "Host disconnected · " + self.data2g_last_host_error
            if self.data2g_last_host_error else "Not connected")
        self._refresh_mode_options()
        self._stop_local_data2g_host()
        self._update_data2g_host_controls()
        self.refresh_tx_button()
        reconnect_after_settings = self.data2g_reconnect_after_settings
        self.data2g_reconnect_after_settings = False
        if reconnect_after_settings and not self._closing:
            QTimer.singleShot(0, self._connect_data2g_host)
        if (not self._closing and self.backend_combo.currentData() == "experimental"
                and not self.data2g_ptt_failed and
                self._should_auto_connect_rig() and not self.rig.connected()):
            QTimer.singleShot(0, self.connect_rig)
        if (not self._closing and
                self.backend_combo.currentData() == "experimental" and
                not self.rx_timer.isActive()):
            QTimer.singleShot(0, self._ensure_session_receive)
        if (not self._closing and self.backend_combo.currentData() == "experimental" and
                self.data2g_background_monitor):
            QTimer.singleShot(5000, self._ensure_data2g_background_monitor)

    def _mark_mode_selection_explicit(self, *_):
        self._mode_has_explicit_selection = True
        self.settings.setValue("transfer/mode_explicit", True)

    def _persist_selected_mode(self, *_):
        key = self.selected_mode()
        self.settings.setValue("transfer/mode", key)
        setting = ("transfer/data2g_mode" if self.backend_combo.currentData() == "data2g"
                   else "transfer/experimental_mode")
        self.settings.setValue(setting, key)

    def _on_experimental_visibility_changed(self, enabled):
        item = self.backend_combo.model().item(1)
        if item is not None:
            item.setEnabled(bool(enabled))
        if getattr(self, "_station_settings_editing", False):
            self._update_station_settings_actions()
            return
        if not enabled and self.backend_combo.currentData() == "experimental":
            self.backend_combo.setCurrentIndex(0)
        self._refresh_mode_options()
        self._update_backend_ui()

    def _on_backend_selection_changed(self, *_):
        if not hasattr(self, "mode_combo"):
            return
        if getattr(self, "_station_settings_editing", False):
            self._update_station_settings_actions()
            return
        data2g = self.backend_combo.currentData() == "data2g"
        self.data2g_background_monitor = not data2g
        if not data2g and not self._confirm_data2g_ptt_recovery():
            with QSignalBlocker(self.backend_combo):
                self.backend_combo.setCurrentIndex(self.backend_combo.findData("data2g"))
            self.data2g_background_monitor = False
            return
        if data2g and self.rx_timer.isActive():
            self._discard_receive_for_tx()
        preferred = self.settings.value(
            "transfer/data2g_mode" if data2g else "transfer/experimental_mode",
            "data2g_1200_robust" if data2g else "resilient_100")
        self.settings.setValue("options/backend", "data2g" if data2g else "experimental")
        self._set_mode_options(preferred)
        self._on_mode_selection_changed()
        if (data2g and not self.data2g_connecting and
                not (DATA2G_ADAPTER.is_connected(self.data2g_session))):
            self._connect_data2g_host()
        host_active = bool(self.data2g_connecting or self.data2g_handoff_pending or
                           self.data2g_closing or
                           (DATA2G_ADAPTER.is_connected(self.data2g_session) and data2g))
        if (not data2g and (self.data2g_background_monitor or not host_active) and
                self._should_auto_connect_rig()
                and not self.rig.connected()):
            QTimer.singleShot(0, self.connect_rig)
        if not data2g:
            self._ensure_data2g_background_monitor()
        self._update_transfer_controls()
        self._refresh_transfer_summary()
        self.audio_test_mode_changed(self.audio_test_mode.isChecked())
        self._update_data2g_host_controls()
        self.refresh_tx_button()

    def _ensure_data2g_background_monitor(self):
        if (self._closing or self.backend_combo.currentData() != "experimental" or
                not self.data2g_background_monitor or self.data2g_connecting or
                self.data2g_local_starting or self.data2g_closing or
                DATA2G_ADAPTER.is_connected(self.data2g_session)):
            return
        self._connect_data2g_host()

    def _switch_to_data2g_backend(self):
        if not DATA2G_ADAPTER.is_connected(self.data2g_session):
            self.data2g_switch_button.hide()
            return
        if self._station_operation_active():
            self.statusBar().showMessage(
                "Stop the current transmission or exchange before switching to Data2G.", 8000)
            return
        with QSignalBlocker(self.backend_combo):
            self.backend_combo.setCurrentIndex(self.backend_combo.findData("data2g"))
        self._on_backend_selection_changed()
        self.data2g_switch_button.hide()

    def _data2g_session_error(self, session, message):
        # Startup failures are summarized after the connect worker completes,
        # where the managed-host log can provide a useful local audio/port hint.
        if session is self.data2g_session and not self.data2g_connecting:
            self._data2g_host_error(message)

    def _data2g_host_error(self, message):
        self.data2g_last_host_error = message
        self.exchange_log.append("Data2G host: " + message)
        self.data2g_host_status.setText("Host error: " + message)
        if getattr(self, "data2g_tx_total", 0):
            in_flight = self.data2g_tx_waiting is not None
            detail = ("The in-flight frame may have transmitted; remaining frames were not sent. "
                      if in_flight else
                      "Remaining card frames were not sent.")
            self._finish_data2g_transmission(
                f"Data2G host error: {message}. {detail}")
        if self.on_air:
            self.on_air.emit("host_error", {"error": message})

    def _data2g_host_status(self, session, message):
        if session is not self.data2g_session:
            return
        fields = message.split()
        if len(fields) == 5 and fields[0] == "PIXELQSO_RX_SNR":
            try:
                port, digest, value, bandwidth = int(fields[1]), fields[2], float(fields[3]), int(fields[4])
                measured = {}
                if (port != DATA2G_ADAPTER.group_port(session) or len(digest) != 64 or
                        not set_measured_snr(measured, value, "data2g_phy", bandwidth)):
                    return
                records = getattr(self, "data2g_rx_measurements", {})
                records[(id(session), port, digest)] = measured
                if len(records) > 256:
                    records.pop(next(iter(records)))
                self.data2g_rx_measurements = records
            except (TypeError, ValueError):
                pass
            return
        if message in {"BUSY ON", "BUSY OFF"}:
            self.data2g_channel_busy = message == "BUSY ON"
            if (self.data2g_tx_waiting is None and not self.ptt_active and
                    (self.data2g_channel_busy or self.receive_activity_pending)):
                self._show_receive_activity(
                    "Signal detected · waiting for Data2G card frames"
                    if self.data2g_channel_busy else
                    "Signal ended · Data2G is checking the received burst")
                if self.data2g_channel_busy:
                    self.receive_activity_timeout.stop()
                else:
                    self.receive_activity_timeout.start(15000)
            if self.data2g_tx_waiting is not None:
                timeout = (DATA2G_BUSY_ACK_TIMEOUT_MS if self.data2g_channel_busy
                           else self.data2g_ack_timeout_ms)
                self.data2g_ack_timer.start(timeout)
                self.rx_status.setText(
                    "Data2G host is holding the queued frame while the channel is busy"
                    if self.data2g_channel_busy else
                    "Data2G channel is clear · awaiting host ACK")
        fields = message.split()
        if (len(fields) >= 3 and fields[0] == "BCAST" and
                fields[1] == str(DATA2G_ADAPTER.group_port(session))):
            if fields[2] == "RXMETADATA" and len(fields) == 8:
                try:
                    metadata = {}
                    if not set_measured_snr(metadata, fields[3], "data2g_phy", fields[4]):
                        return
                    if fields[5] != "-":
                        metadata["callsign"] = fields[5]
                    diagnostic = {"codewords": int(fields[7])}
                    self._show_receive_activity(
                        "Data2G burst heard · waiting for complete card frames",
                        report={"card": metadata, "diagnostics": diagnostic,
                                "receive_profile": f"Data2G · {fields[6]}"})
                    self.receive_activity_timeout.start(15000)
                except (TypeError, ValueError):
                    pass
            if fields[2] == "HEARD":
                snapshot = self.card_assembly.preview_snapshot()
                if not snapshot or not snapshot.card.get("exact"):
                    call = fields[3] if len(fields) > 3 else None
                    metadata = (dict(snapshot.card) if snapshot and
                                (not call or snapshot.card.get("callsign") == call) else {})
                    if call:
                        metadata["callsign"] = call
                    self._show_receive_activity(
                        f"Receiving Data2G burst from {call or 'a station'} · checking card frames",
                        report={"card": metadata, "receive_profile": "Data2G host/KISS"})
                    self.receive_activity_timeout.start(15000)
            elif fields[2] in {"LOST", "DROPPED"}:
                self.rx_status.setText("Data2G lost card frames · waiting for another copy")
        if message.startswith(("BCAST ", "PTT ", "BUSY ", "BUFFER ", "MODE ")):
            self.exchange_log.append("Data2G: " + message)
            if self.on_air:
                self.on_air.emit("host_status", {"message": message})

    def _on_mode_selection_changed(self, *_):
        self._set_rx_preferred_mode(self.selected_mode(), detected=False)
        backend = self.selected_backend()
        if self.selected_adapter().capabilities.kind is AdapterKind.FRAME_TRANSPORT:
            # Backend choice transfers CAT authority to Data2G even when the
            # host has no transmit mode that fits the selected group.
            if self.rig.connected():
                self.rig.disconnect()
            if backend is None or not getattr(backend, "usable", False):
                if backend is not None and getattr(backend, "disabled_reason", None):
                    self.rx_status.setText(backend.disabled_reason)
                elif not self.data2g_connecting and not (DATA2G_ADAPTER.is_connected(self.data2g_session)):
                    self._connect_data2g_host()
                self.refresh_tx_button()
                self._update_backend_ui()
                return
            if DATA2G_ADAPTER.is_connected(self.data2g_session):
                DATA2G_ADAPTER.select_mode(self.data2g_session, backend.mode_name)
            elif not self.data2g_connecting:
                self._connect_data2g_host()
            if self.rx_timer.isActive():
                self._discard_receive_for_tx()
            self.listen_indicator.setText("● DATA2G HOST RX")
            self.listen_indicator.setStyleSheet(status_label_style("positive"))
            self.rx_status.setText(
                f"Receiving · RX mode: {self._receive_mode_label()} · Data2G card frames")
        elif ((self.data2g_connecting or
               DATA2G_ADAPTER.is_connected(self.data2g_session)) and
              not self.data2g_background_monitor):
            session = self.data2g_session
            self.data2g_closing = True
            self.data2g_host_status.setText(
                "Disconnecting Data2G before experimental modem takes radio control…")
            self._update_data2g_host_controls()
            import threading
            threading.Thread(target=DATA2G_ADAPTER.disconnect, args=(session,), daemon=True,
                             name="data2g-experimental-handoff").start()
        self.refresh_tx_button()
        self._update_backend_ui()
        if self.selected_adapter().capabilities.kind is not AdapterKind.FRAME_TRANSPORT:
            QTimer.singleShot(0, self._ensure_session_receive)
            self._ensure_data2g_background_monitor()

    def _resume_experimental_backend(self):
        """Acquire Experimental CAT/audio while an optional Data2G monitor listens."""
        if (self._closing or self.backend_combo.currentData() != "experimental" or
                self.data2g_connecting or self.data2g_local_starting or self.data2g_closing or
                (DATA2G_ADAPTER.is_connected(self.data2g_session) and
                 not self.data2g_background_monitor)):
            return
        if self.data2g_local_process and self.data2g_local_process.poll() is None:
            return
        if (not self.data2g_ptt_failed and self._should_auto_connect_rig() and
                not self.rig.connected()):
            QTimer.singleShot(0, self.connect_rig)
        QTimer.singleShot(0, self._ensure_session_receive)

    def _confirm_data2g_ptt_recovery(self):
        if not self.data2g_ptt_failed:
            return True
        answer = QMessageBox.warning(
            self, "Data2G PTT state is uncertain",
            "The managed Data2G host reported a PTT failure. Confirm that you have "
            "verified the radio is unkeyed before resuming radio control or "
            "transmitting.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.data2g_ptt_failed = False
        # Permit a later fault of the same on/off class to relatch the
        # interlock after this explicit recovery.
        self.data2g_ptt_warnings.clear()
        self.settings.setValue("data2g/ptt_failed", False)
        self.settings.sync()
        self._update_backend_ui()
        self.refresh_tx_button()
        self._refresh_transfer_summary()
        return True

    def _update_backend_ui(self):
        """Make controls reflect which backend currently owns reception and CAT/PTT."""
        if not hasattr(self, "receive_all_modes"):
            return
        # A pending backend change still has the previous backend's mode.
        adapter = (adapter_for_backend_selection(self.backend_combo.currentData())
                   if getattr(self, "_station_settings_editing", False)
                   else self.selected_adapter())
        capabilities = adapter.capabilities
        selected_data2g = capabilities.kind is AdapterKind.FRAME_TRANSPORT
        if hasattr(self, 'repeat_count_label'):
            self.repeat_count.setVisible(True)
            self.repeat_count_label.setVisible(True)
        if hasattr(self, "audio_test_mode"):
            self.audio_test_mode.setEnabled(not selected_data2g)
            self.audio_test_mode.setToolTip(
                "Applies only to Experimental modems. Data2G transmission uses the host's radio/PTT settings.")
        if hasattr(self, "cwid_enabled"):
            self.cwid_enabled.setEnabled(not selected_data2g)
            self.cwid_message.setEnabled(
                not selected_data2g and self.cwid_enabled.isChecked())
        host_busy = bool(self.data2g_connecting or self.data2g_local_starting or self.data2g_closing or
                         self.data2g_handoff_pending or
                         (DATA2G_ADAPTER.is_connected(self.data2g_session)))
        monitor_only = bool(not selected_data2g and self.data2g_background_monitor)
        data2g_path = selected_data2g or (host_busy and not monitor_only)
        if hasattr(self, "data2g_switch_button") and selected_data2g:
            self.data2g_switch_button.hide()
        receiver_is_busy = bool(
            getattr(self, "receive_activity_pending", False) or
            (hasattr(self, "receive_preview_box") and not self.receive_preview_box.isHidden()) or
            (hasattr(self, "stop_tx_btn") and not self.stop_tx_btn.isHidden()) or
            (hasattr(self, "rx_timer") and self.rx_timer.isActive()))
        if hasattr(self, "listen_indicator") and not receiver_is_busy:
            if selected_data2g:
                if self.data2g_connecting or self.data2g_local_starting:
                    receiver_state, receiver_tone = "● CONNECTING", "warning"
                elif DATA2G_ADAPTER.is_connected(self.data2g_session):
                    receiver_state, receiver_tone = "● WAITING FOR FRAMES", "positive"
                else:
                    receiver_state, receiver_tone = "● NOT CONNECTED", "warning"
            elif self.data2g_closing or self.data2g_handoff_pending:
                receiver_state, receiver_tone = "● SWITCHING RADIO", "warning"
            elif hasattr(self, "rx_timer") and self.rx_timer.isActive():
                receiver_state, receiver_tone = "● LISTENING", "positive"
            else:
                receiver_state, receiver_tone = "● NOT LISTENING", "warning"
            self.listen_indicator.setText(receiver_state)
            self.listen_indicator.setStyleSheet(status_label_style(receiver_tone))
        if hasattr(self, "rig_status"):
            if selected_data2g:
                if self.data2g_ptt_failed:
                    connection_text = "Data2G PTT fault · verify radio is unkeyed"
                elif self.data2g_closing or self.data2g_handoff_pending:
                    connection_text = "Releasing Data2G radio and audio…"
                elif self.data2g_connecting or self.data2g_local_starting:
                    connection_text = "Connecting to Data2G host…"
                elif DATA2G_ADAPTER.is_connected(self.data2g_session):
                    connection_text = "Data2G connected · audio/PTT readiness unverified"
                else:
                    connection_text = "Data2G not connected"
                self.rig_status.setText(connection_text)
                self.rig_status.setStyleSheet(
                    status_label_style(
                        "warning" if self.data2g_ptt_failed else "muted",
                        letter_spaced=False))
            elif self.data2g_closing or self.data2g_handoff_pending:
                self.rig_status.setText("Switching radio and audio to local modem…")
            elif (self.rig_status.text().startswith("Data2G") or
                  self.rig_status.text().startswith("Switching radio and audio")):
                self.rig_status.setText(
                    "CAT connected" if self.rig.connected() else "CAT disconnected")
                self.rig_status.setStyleSheet(
                    status_label_style("muted", bold=False, letter_spaced=False))
        if hasattr(self, "station_button"):
            connected = (DATA2G_ADAPTER.is_connected(self.data2g_session)
                         if selected_data2g else self.rig.connected())
            self.station_button.setText(
                "Station settings" if connected else "Connect station")
        if hasattr(self, "station_call"):
            self.station_call.setEnabled(not host_busy)
            self.station_call.setToolTip(
                "Disconnect the Data2G host before changing call sign; the open group uses this identity."
                if host_busy else "Used in card identity and when opening the Data2G broadcast group.")
        if hasattr(self, "receive_status_hint"):
            if self.data2g_ptt_failed:
                message = ("The Data2G host reported a PTT failure. Verify the radio is unkeyed; "
                           "radio control or the next transmission requires confirmation.")
            elif selected_data2g:
                message = ("Data2G host is connected and listening for card frames."
                           if DATA2G_ADAPTER.is_connected(self.data2g_session) else
                           "Data2G handles radio reception. Select Connect station to set up a host and receive card frames.")
            elif host_busy:
                message = ("Data2G is monitoring received cards in the background; Experimental keeps CAT control."
                           if monitor_only else
                           "Data2G radio handoff is in progress; local modem reception will start when the host disconnects.")
            else:
                message = "Always listening when a local audio input is available."
            self.receive_status_hint.setText(message)
        if hasattr(self, "auto_start_btn"):
            mode_backend = self.selected_backend()
            auto_host_ready = (selected_data2g and mode_backend is not None and
                               getattr(mode_backend, "usable", False) and
                               DATA2G_ADAPTER.is_connected(self.data2g_session))
            self.auto_start_btn.setEnabled(
                not self.auto_armed and (not selected_data2g or auto_host_ready))
            self.auto_start_btn.setToolTip(
                (getattr(mode_backend, "disabled_reason", None) or
                 "Connect to a Data2G host and select a usable broadcast mode first.")
                if selected_data2g and not auto_host_ready else "")
        advanced = self.settings.value("ui/advanced_controls", False, type=bool)
        self.receive_all_modes.setEnabled(capabilities.receive_all_modes)
        self.receive_all_modes.setVisible(advanced and capabilities.receive_all_modes)
        if self.receive_all_modes_label is not None:
            self.receive_all_modes_label.setVisible(
                advanced and capabilities.receive_all_modes)
        self._update_rx_mode_controls()
        if hasattr(self, "auto_connect_cat"):
            self.auto_connect_cat.setEnabled(
                not selected_data2g if getattr(self, "_station_settings_editing", False)
                else not data2g_path)
            self.auto_connect_cat.setToolTip(
                "Used by local modem modes; Data2G takes radio control only after switching to that backend.")
        if hasattr(self, "rx_visual_stack"):
            local_data2g_audio = bool(
                selected_data2g and self.data2g_local_process is not None and
                self.data2g_local_process.poll() is None)
            show_spectrum = capabilities.spectrum or local_data2g_audio
            self.rx_visual_stack.setVisible(show_spectrum)
            if show_spectrum:
                self.rx_visual_stack.setCurrentWidget(self.rx_waterfall)
                if local_data2g_audio and not self.data2g_waterfall_timer.isActive():
                    self.rx_waterfall.reset("Data2G local input")
        if hasattr(self, "cat_settings_group"):
            self.cat_settings_group.setVisible(not selected_data2g)
            self.cat_settings_group.setEnabled(
                not selected_data2g if getattr(self, "_station_settings_editing", False)
                else not data2g_path)
        if hasattr(self, "experimental_audio_group"):
            self.experimental_audio_group.setVisible(not selected_data2g)
        if hasattr(self, "data2g_settings_group"):
            self.data2g_settings_group.setVisible(
                selected_data2g or
                (host_busy and not getattr(self, "_station_settings_editing", False)))

    def _data2g_rx_frame(self, session, port, frame):
        if (session is not self.data2g_session or not self.data2g_session or
                port != DATA2G_ADAPTER.group_port(self.data2g_session)):
            return
        frame_ref = {"port": port, "bytes": len(frame),
                     "sha256": hashlib.sha256(frame).hexdigest()}
        if self.on_air:
            self.on_air.emit("host_rx_frame", frame_ref, binary=frame)
        try:
            _assembly, metadata = self.card_assembly.feed_transport_frame("PIXELQSO", frame)
            if metadata.get("animation_metadata_only"):
                self._refresh_animation_references()
                return
            if metadata.get("reception_metadata_only"):
                self._refresh_animation_references()
                return
            pixels = metadata.pop("pixels")
            palette_id = int(metadata["palette_id"])
            palette = [list(color) for color in modem.AVATAR_PALETTES[palette_id]]
            card = {**metadata, "palette": palette, "avatar_burst": True,
                    "received_copies": 1}
            measurement = getattr(self, "data2g_rx_measurements", {}).pop(
                (id(session), port, frame_ref["sha256"]), None)
            if measurement is not None:
                card.update(measurement)
            report = {"sample_rate": 0, "receive_profile": "Data2G host/KISS",
                      "valid_packets": [], "valid_packet_count": 1, "errors": [],
                      "card": card, "pixels": pixels}
            report, improved = self._merge_live_preview(report)
            card = report["card"]
            if (self.data2g_background_monitor and
                    self.backend_combo.currentData() == "experimental"):
                callsign = str(card.get("callsign") or "station")
                self.data2g_switch_button.setText(
                    f"Data2G card detected from {callsign} · Switch to Data2G")
                self.data2g_switch_button.show()
            self.active_receive_identity = self._report_receive_identity(report)
            verified_complete = self._assembly_verified(report)
            if self.on_air:
                self.on_air.emit("host_rx_card", {
                    **frame_ref, "callsign": card.get("callsign"),
                    "grid": card.get("grid"), "card_id": card.get("card_id"),
                    "exact": bool(card.get("exact")),
                    "reference": decoded_reference(report),
                })
            if improved:
                image = self.card_image(report)
                self._show_receive_preview(image, card=card, exact=bool(card.get("exact")),
                                           coverage=card.get("pixel_coverage"),
                                           verified_coverage=card.get("verified_pixel_coverage"),
                                           report=report)
            if verified_complete:
                key = received_card_key(report)
                if key not in self.seen_received_cards:
                    self._add_session_wall_card(report)
                    self.seen_received_cards.add(key)
                    self.reply_report = report
                    self._record_received_card(report)
                    self._handle_contact_message(report)
                    self.rx_status.setText(f"Verified Data2G card from {card.get('callsign')} · whole-card CRC passed")
                    self.exchange_log.append(f"Verified Data2G card from {card.get('callsign')} ({card.get('grid')}); whole-card CRC passed.")
                    self.save_received_report(report, "data2g")
                else:
                    self._show_duplicate_card(report)
            else:
                self.rx_status.setText(f"Data2G preview from {card.get('callsign')} · {card.get('color_stage')}")
        except (ValueError, IndexError, KeyError) as exc:
            self.exchange_log.append("Dropped invalid Data2G card frame: " + str(exc))
            if self.on_air:
                self.on_air.emit("host_rx_frame_invalid", {**frame_ref, "error": str(exc)})

    def save_received_report(self, report, prefix):
        folder = DATA / "qsl"
        card = report.get("card") or {}
        callsign = str(card.get("callsign") or "unknown")
        safe_callsign = "".join(
            character if character.isascii() and
            (character.isalnum() or character in "_-") else "_"
            for character in callsign)[:32].strip("._") or "unknown"
        name = (f"{prefix}-{safe_callsign}-{card.get('card_id', 'na')}-"
                f"{time.time_ns()}.json")
        destination = folder / name
        temporary = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=folder,
                    prefix=".data2g-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(report, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        except OSError as exc:
            message = f"Could not save received card capture: {exc}"
            self.exchange_log.append(message)
            self.rx_status.setText(message)
            return None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return destination

    def save_station_settings(self):
        callsign = self.station_call.text().strip().upper()
        grid = self.station_grid.text().strip().upper()
        self.station_call.setText(callsign)
        self.station_grid.setText(grid)
        self._refresh_station_identity()
        self.settings.setValue("station/callsign", callsign)
        self.settings.setValue("station/grid", grid)
        self.settings.setValue("station/cwid_enabled", self.cwid_enabled.isChecked())
        self.settings.setValue("station/cwid_message", self.cwid_message.text().strip())
        self.settings.setValue(
            "ui/interface_text_size", self.interface_text_size.currentData())
        self.settings.setValue("radio/rigctld_host", self.rig_host.text().strip())
        self.settings.setValue("radio/rigctld_port", self.rig_port.value())
        self.settings.setValue("data2g/rigctld_host", self.data2g_rig_host.text().strip())
        self.settings.setValue("data2g/rigctld_port", self.data2g_rig_port.value())
        self.settings.setValue("radio/auto_connect_cat", self.auto_connect_cat.isChecked())
        self.settings.setValue("data2g/host", self.data2g_host_input.text().strip())
        self.settings.setValue("data2g/local_host", self.data2g_host_source.currentData() == "local")
        self.settings.setValue("data2g/command_port", self.data2g_command_port.value())
        self.settings.setValue("data2g/kiss_port", self.data2g_kiss_port.value())
        self.settings.setValue("data2g/bandwidth_hz", int(self.data2g_host_bandwidth.currentData()))
        self.settings.setValue("station/audio_test_mode", self.audio_test_mode.isChecked())
        self.settings.setValue("options/show_experimental_modes", self.show_experimental_modes.isChecked())
        self.settings.setValue("transfer/repeats", self.repeat_count.value())
        self.settings.setValue("transfer/interlaced_copies", self.interlaced_copies.isChecked())
        self.settings.setValue("options/backend", self.backend_combo.currentData())
        self.settings.setValue("options/web_enabled", self.web_enabled.isChecked())
        self.settings.setValue("options/web_port", self.web_port.value())
        if self.web_server is not None:
            self.web_server.close(); self.web_server = None
        if self.web_enabled.isChecked():
            try: self._start_web_server()
            except OSError as exc: QMessageBox.warning(self, "Web server unavailable", str(exc))
        self._refresh_mode_options()
        if self.rx_timer.isActive():
            self._discard_receive_for_tx()
        QTimer.singleShot(250, self._ensure_session_receive)

    def _start_web_server(self):
        if self.web_server is None:
            self.web_server = CompanionServer(self._web_dispatch, self.web_port.value())

    def _web_dispatch(self, method, path, data):
        import threading
        request = {"method": method, "path": path, "data": data, "event": threading.Event()}
        self.web_bridge.requested.emit(request)
        if not request["event"].wait(8):
            raise TimeoutError("Desktop app did not respond")
        if "error" in request: raise ValueError(request["error"])
        return request.get("result", {})

    def _handle_web_command(self, request):
        try:
            method, path, data = request["method"], request["path"], request["data"]
            if method == "GET" and path == "status":
                import socket
                addresses = sorted({address[4][0] for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                                    if not address[4][0].startswith("127.")})
                host = addresses[0] if addresses else "127.0.0.1"
                request["result"] = {"callsign": self.station_call.text(), "grid": self.station_grid.text(),
                    "server": f"http://{host}:{self.web_server.port}",
                    "transmitting": bool(self.tx_audio or self.tx_timer.isActive() or self.stop_tx_btn.isVisible()),
                    "stage": str(self._outgoing_message_type())}
            elif method == "GET" and path == "cards":
                cards = []
                for folder, source in ((DATA / "cards", "My Cards"), (DATA / "sent-cards", "Sent Cards")):
                    for file in folder.glob("*.json"):
                        try:
                            d = json.loads(file.read_text()); cards.append({"source": source, **d})
                        except (OSError, ValueError): pass
                for file in self.qso_log_dir.glob("*.json"):
                    try:
                        entry=json.loads(file.read_text())
                        for bucket in ("sent_cards", "received_cards"):
                            for report in entry.get(bucket, []):
                                meta=report.get("card") or {}
                                if report.get("pixels") and meta.get("palette"):
                                    pixels = report["pixels"]
                                    if pixels and isinstance(pixels[0], list):
                                        pixels = [value for row in pixels for value in row]
                                    cards.append({"source": "QSO", "callsign": meta.get("callsign", ""),
                                        "grid": meta.get("grid", ""), "width": meta.get("width",32),
                                        "height": meta.get("height",32), "palette": meta["palette"],
                                        "pixels": pixels, "message_type": meta.get("message_type", "card")})
                    except (OSError, ValueError): pass
                request["result"] = {"cards": cards}
            elif method == "POST" and path == "stage":
                stage = str(data.get("stage", ""))
                if stage not in self.manual_stage_buttons: raise ValueError("Unknown transmit stage")
                self.exchange_mode.setCurrentIndex(0); self.manual_stage_buttons[stage].setChecked(True)
                request["result"] = {"stage": stage}
            elif method == "POST" and path == "transmit":
                if self.auto_armed: raise ValueError("Stop automatic exchange before remote manual transmit")
                if self.tx_audio or self.tx_timer.isActive() or self.ptt_active: raise ValueError("A transmission is already active")
                QTimer.singleShot(0, self.transmit_exchange)
                request["result"] = {"accepted": True}
            elif method == "POST" and path == "stop":
                if self.stop_tx_btn.isVisible(): self._stop_current_transmit()
                elif self.tx_audio or self.tx_timer.isActive() or self.ptt_active: self._finish_tx("Transmit stopped by mobile operator; releasing PTT.")
                request["result"] = {"stopped": True}
            elif method == "POST" and path == "quickdraw":
                card = modem.Card(str(data.get("callsign", self.station_call.text())).strip().upper(),
                    str(data.get("grid", self.station_grid.text())).strip().upper(),
                    [tuple(int(v) for v in color) for color in data["palette"]],
                    [int(v) for v in data["pixels"]], int(time.time_ns() & 0xffff),
                    int(data.get("width", 32)), int(data.get("height", 32)), copy.deepcopy(data.get("animation")))
                card.validate()
                self.quick_reply_card = card
                request["result"] = {"loaded": True, "card_id": card.card_id}
            else: raise ValueError("Unknown API route")
        except Exception as exc: request["error"] = str(exc)
        finally: request["event"].set()

    def audio_test_mode_changed(self, enabled):
        data2g_selected = (hasattr(self, "backend_combo") and
                           self.backend_combo.currentData() == "data2g")
        local_audio_test = bool(enabled and not data2g_selected)
        if hasattr(self, "test_mode_banner"):
            self.test_mode_banner.setVisible(local_audio_test)
        if hasattr(self, "tx_btn"):
            self.refresh_tx_button()
        if hasattr(self, "beacon_controls"):
            self.beacon_controls.setEnabled(not local_audio_test)
        if hasattr(self, "frequency_controls"):
            self.frequency_controls.setVisible(not local_audio_test and self.exchange_mode.currentIndex() != 2)
        if hasattr(self, "exchange_mode"):
            self._update_exchange_controls()
        if hasattr(self, "rig_status") and local_audio_test and self.test_link_enabled:
            self.rig_status.setText("Two-window local audio link · CAT/PTT disabled")
        elif hasattr(self, "rig_status") and not local_audio_test and not data2g_selected and not self.rig.connected():
            self.rig_status.setText("CAT disconnected")

    def _update_exchange_controls(self, *_):
        automatic = self.exchange_mode.currentIndex() == 1
        beacon = self.exchange_mode.currentIndex() == 2
        self.quick_draw_button.setVisible(not automatic and not beacon and not self.auto_armed)
        self.quick_draw_status.setVisible(not automatic and not beacon and not self.auto_armed)
        self.clear_quick_draw_button.setVisible(self.quick_reply_card is not None and not automatic and not beacon and not self.auto_armed)
        self.quick_draw_send_button.setVisible(
            self.quick_reply_card is not None and not automatic and not beacon and not self.auto_armed)
        legacy = self.selected_mode() == "standard"
        self.role_widget.setVisible(False)
        self.auto_stage_group.setVisible(not beacon)
        for button in self.manual_stage_buttons.values():
            button.setEnabled(True)
        for button in self.stage_send_buttons.values():
            button.setVisible(True)
            button.setEnabled(True)
        for combo in self.stage_card_combos.values():
            combo.setEnabled(not self.auto_armed)
        self.manual_card_widget.setVisible(beacon or (not automatic and legacy))
        self.manual_card_label.setVisible(beacon or (not automatic and legacy))
        self.auto_controls.setVisible(automatic)
        self.auto_start_btn.setVisible(False)
        self.auto_stop_btn.setVisible(self.auto_armed)
        self.exchange_status.setVisible(automatic)
        self.manual_tx_controls.setVisible(not automatic and not beacon)
        data2g_selected = (hasattr(self, "backend_combo") and
                           self.backend_combo.currentData() == "data2g")
        local_audio_test = self.audio_test_mode.isChecked() and not data2g_selected
        self.tx_btn.setVisible(False)
        self.stop_tx_btn.setVisible(self.stop_tx_btn.isEnabled())
        self.cq_controls.setVisible(not beacon and not (automatic and self.auto_armed))
        self._refresh_contact_stage_indicator()
        host_mode = (self.selected_adapter().capabilities.kind is
                     AdapterKind.FRAME_TRANSPORT)
        local_audio_test = self.audio_test_mode.isChecked() and not host_mode
        self._update_send_button_label()
        self.beacon_controls.setVisible(
            beacon and (not local_audio_test or self.beacon_button.isChecked()))
        if not beacon and self.beacon_button.isChecked():
            self.beacon_button.setChecked(False)
        self._update_beacon_help()
        self._refresh_operate_primary_action()
        self.frequency_controls.setVisible(not beacon and not local_audio_test and not host_mode)

    def _refresh_operate_primary_action(self):
        buttons = [getattr(self, name, None) for name in (
            "tx_btn", "stop_tx_btn", "auto_stop_btn", "call_cq_button",
            "reply_to_button", "beacon_button")]
        buttons = [button for button in buttons if button is not None]
        primary = None
        stop_tx = getattr(self, "stop_tx_btn", None)
        auto_stop = getattr(self, "auto_stop_btn", None)
        beacon_button = getattr(self, "beacon_button", None)
        reply_button = getattr(self, "reply_to_button", None)
        call_button = getattr(self, "call_cq_button", None)
        tx_button = getattr(self, "tx_btn", None)
        if (stop_tx is not None and not stop_tx.isHidden() and
                not self.manual_tx_controls.isHidden()):
            primary = stop_tx
        elif getattr(self, "auto_armed", False) and auto_stop is not None and not auto_stop.isHidden():
            primary = auto_stop
        elif (getattr(self, "exchange_mode", None) is not None and
              self.exchange_mode.currentIndex() == 2 and beacon_button is not None and
              not self.beacon_controls.isHidden()):
            primary = beacon_button
        elif (getattr(self, "audio_test_mode", None) is not None and
              self.audio_test_mode.isChecked() and tx_button is not None and
              not self.manual_tx_controls.isHidden()):
            primary = tx_button
        elif reply_button is not None and not reply_button.isHidden():
            primary = reply_button
        elif call_button is not None and not self.cq_controls.isHidden():
            primary = call_button
        elif tx_button is not None and not self.manual_tx_controls.isHidden():
            primary = tx_button
        for button in buttons:
            desired = "primaryAction" if button is primary else ""
            if button.objectName() != desired:
                button.setObjectName(desired)
                button.style().unpolish(button)
                button.style().polish(button)
                button.update()

    def _refresh_contact_stage_indicator(self):
        if hasattr(self, "stop_responding_button"):
            responding = self.auto_armed and bool(self.auto_peer)
            self.stop_responding_button.setVisible(responding)
            self.stop_responding_button.setText(
                f"Stop responding to {self.auto_peer}" if responding else "Stop responding")
        buttons = getattr(self, "manual_stage_buttons", None)
        if not buttons:
            return
        auto_armed = bool(getattr(self, "auto_armed", False))
        caller_cq_active = (auto_armed and getattr(self, "auto_role", None) == "caller"
                            and bool(getattr(self, "call_cq_active", False)))
        can_call_cq = caller_cq_active or (
            not auto_armed and getattr(self, "tx_btn", None) is not None and
            self.tx_btn.isEnabled())
        self.call_cq_button.setEnabled(can_call_cq)
        self.call_cq_button.setAccessibleDescription(
            "Stop the active CQ repeat and automatic exchange."
            if caller_cq_active else
            (self.tx_availability_hint.text() or
             "Connect a station before starting a CQ call.")
            if not can_call_cq and not auto_armed else
            (f"Send the first CQ immediately, then repeat every {self.cq_interval.value()} seconds "
             "between contacts.")
            if not auto_armed else
            "An automatic exchange is active. Stop it before starting a new CQ call.")
        if getattr(self, "auto_armed", False):
            state = getattr(self, "contact_stage", "idle")
            role = getattr(self, "auto_role", "responder")
            stage = {
                "send_cq": "cq", "send_exchange": "exchange",
                "await_exchange": "exchange", "await_report73": "report73",
                "send_report73": "report73", "await_final73": "final73",
                "send_final73": "final73",
            }.get(state, "exchange")
            if state == "listen_cq":
                stage = "cq" if role == "responder" else "exchange"
        else:
            self.exchange_status.setText("CQ → Reply / Exchange → RR73 → 73\nCall CQ, or click Reply on a received CQ to start automatic sequencing.")
            return
        labels = {"send_cq": "Sending CQ", "listen_cq": "Waiting for an exchange" if role == "caller" else "Waiting for a CQ",
                  "send_exchange": "Sending exchange", "await_report73": "Waiting for RR73",
                  "send_report73": "Sending RR73", "await_final73": "Waiting for final 73",
                  "send_final73": "Sending final 73", "idle": "Contact complete"}
        titles = ("CQ", "Exchange", "RR73", "73")
        current_index = {"cq": 0, "exchange": 1, "report73": 2,
                         "final73": 3}.get(stage, 0)
        if state == "idle":
            current_index = len(titles)
        timeline = "    ".join(
            (f"✓ {title}" if index < current_index else
             f"● {title}" if index == current_index else f"○ {title}")
            for index, title in enumerate(titles))
        subject = self.auto_peer or "No peer yet"
        self.exchange_status.setText(
            f"{subject} · {labels.get(state, state)}\n{timeline}")
        button = buttons.get(stage)
        if button and not button.isChecked():
            button.setChecked(True)
        self.auto_start_btn.setVisible(False)
        self.auto_stop_btn.setVisible(self.auto_armed)

    def selected_profile(self):
        return modem.DEFAULT_PROFILE

    def selected_mode(self):
        return self.mode_combo.currentData() or "resilient_100"

    def selected_backend(self):
        key = self.selected_mode()
        if (getattr(self, "backend_combo", None) is not None and
                self.backend_combo.currentData() == "data2g"):
            # Never resolve a discovered Data2G key through the process-wide
            # registry: it may belong to another window or a stale host.
            return self.data2g_modes.get(key) or (
                BACKENDS.get(key) if key in {"data2g_1200_robust", "data2g_1200_fast"}
                else None)
        backend = BACKENDS.get(key)
        if backend is not None and mode_supports_audio_placement(key):
            return get_backend(key, self.selected_audio_placement())
        return backend

    def selected_adapter(self):
        spec = MODE_REGISTRY.get(self.selected_mode())
        mode_adapter = spec.adapter if spec is not None else None
        return adapter_for_backend_selection(self.backend_combo.currentData(), mode_adapter)

    def selected_audio_placement(self):
        # Local narrow modes use a fixed lowest tone; Data2G owns its audio settings.
        return 300

    @staticmethod
    def _audio_device_identifier(device):
        if device is None:
            return ""
        if isinstance(device, str):
            return device
        try:
            return bytes(device.id()).hex()
        except (AttributeError, TypeError):
            return ""

    def _save_audio_device(self, combo, setting, *, force=False):
        if getattr(self, "_station_settings_editing", False) and not force:
            return
        device = combo.currentData()
        if device is None:
            return
        self.settings.setValue(setting + "/id", self._audio_device_identifier(device))
        self.settings.setValue(setting + "/name", combo.currentText())

    def _data2g_audio_config_error_message(self):
        details = ", ".join(self.data2g_audio_config_errors.values())
        return (f"Data2G host audio selection unavailable ({details}); "
                "choose a listed device in Station settings before starting the local host.")

    def _data2g_audio_device_changed(self, setting):
        if getattr(self, "_station_settings_editing", False):
            return
        self.data2g_audio_config_errors.pop(setting, None)
        if self.data2g_audio_config_errors:
            self.data2g_host_status.setText(self._data2g_audio_config_error_message())
        else:
            self.data2g_host_status.setText(
                "Data2G audio selection updated; connect to start the local host.")
        self._update_data2g_host_controls()

    def _show_receive_preview(self, image, *, card=None, exact=False, coverage=None,
                              verified_coverage=None, duplicate=False, report=None):
        self._update_receive_metadata(report or {"card": card or {}})
        if image is None:
            return
        self.receive_activity_timeout.stop()
        self.receive_activity_pending = False
        self.receive_progress.setRange(0, 100)
        self.listen_indicator.setText("● LISTENING" if exact else "● RECEIVING CARD")
        card = card or {}
        if (report is not None and not duplicate and card.get("callsign") and
                card.get("card_id") is not None):
            self._add_session_wall_card(report, image=image)
            self._show_reply_action(report)
            if card.get('reception_report'):
                self._refresh_animation_references()
            if not exact:
                self._record_received_card(report)
        identity = " ".join(value for value in (
            str(card.get("callsign") or "station not identified"),
            str(card.get("grid") or "")) if value)
        verification = "whole card verified" if exact else "provisional, not yet verified"
        self.current_preview_is_duplicate = bool(duplicate)
        self.receive_duplicate_label.setVisible(bool(duplicate))
        self.receive_qr_image = image.copy() if exact else None
        self.receive_qr_button.setVisible(bool(exact))
        self.receive_view.setAccessibleName(
            f"Duplicate card preview from {identity} · {verification}" if duplicate else
            f"Card preview from {identity} · {verification}")
        self.receive_view.setPixmap(QPixmap.fromImage(image).scaled(
            184, 184, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation))
        self.receive_progress.show()
        if exact:
            self.receive_progress.setValue(100)
            self.receive_progress.setFormat("100% · whole card verified")
            self.receive_progress.setToolTip("Whole card integrity verified.")
            self.receive_progress.setAccessibleDescription(
                "100 percent received. Whole card integrity verified.")
        else:
            percent = self._coverage_percent(coverage)
            verified_percent = self._coverage_percent(verified_coverage)
            self.receive_progress.setValue(percent)
            self.receive_progress.setFormat(f"{percent}% received")
            self.receive_progress.setToolTip(
                f"{percent}% received; {verified_percent}% of image regions verified. "
                "Provisional until whole-card integrity checks pass.")
            self.receive_progress.setAccessibleDescription(
                f"{percent} percent of the card image received; "
                f"{verified_percent} percent of image regions verified. "
                "The preview is provisional until whole-card integrity verification completes.")
        self.receive_preview_box.show()
        self.discard_preview_button.setEnabled(True)
        if exact and not duplicate:
            self.live_preview_timeout.start(5000)
        else:
            self.live_preview_timeout.stop()

    def _hide_receive_preview(self):
        self.receive_activity_timeout.stop()
        self.receive_activity_pending = False
        self.live_preview_timeout.stop()
        self.receive_duplicate_label.hide()
        self.receive_qr_image = None
        self.receive_qr_button.hide()
        self.current_preview_is_duplicate = False
        self.receive_preview_box.hide()

    def _discard_receive_preview(self):
        """Drop only the active card's receive evidence and await a fresh copy."""
        self.receive_activity_timeout.stop()
        self.receive_activity_pending = False
        self.live_preview_timeout.stop()
        self.card_assembly.discard_current_preview()
        # In-flight workers own the old cache. A fresh instance prevents a
        # discarded acquisition or scan cursor from leaking into new RX.
        self.copy_capture_cache = self.copy_capture_cache.__class__()
        self._clear_rx_bytes()
        self.live_decode_samples = 0
        self.live_decode_final = False
        self.test_rx_final_pending = False
        self.live_decode_token += 1
        self.active_live_decode_token = None
        self.live_decode_inflight = False
        self.active_receive_identity = None
        self.current_preview_is_duplicate = False
        self.receive_duplicate_label.hide()
        self.receive_view.clear()
        self.receive_view.setText("Listening for a card")
        self.receive_view.setAccessibleName("Waiting for a new card preview")
        self.receive_progress.setRange(0, 100)
        self.receive_progress.setValue(0)
        self.receive_progress.setFormat("Waiting for a new card")
        self.receive_progress.setAccessibleDescription(
            "The previous preview was discarded. The next transmission starts a new card.")
        self.receive_progress.show()
        self.discard_preview_button.setEnabled(False)
        self._update_receive_metadata({})
        self.receive_preview_box.show()
        self.listen_indicator.setText("● LISTENING")
        self.rx_status.setText("Preview discarded · the next transmission starts a new card")

    def _update_receive_metadata(self, report):
        if not hasattr(self, "incoming_card_details"):
            return
        mode = self._receive_mode_label(report.get("receive_profile"))
        text = incoming_card_text(report, mode)
        self.incoming_card_details.setText(text)
        self.incoming_card_details.setAccessibleDescription(text)
        self.incoming_card_details.show()

    def _show_receive_activity(self, message, *, pivot=False, report=None):
        if pivot:
            self.card_assembly.clear_current_preview()
            self._hide_receive_preview()
        self.receive_activity_pending = True
        self.listen_indicator.setText("● RECEIVING SIGNAL")
        self.rx_status.setText(f"{message} · RX mode: {self._receive_mode_label()}")
        if not self.current_preview_is_duplicate:
            self.receive_view.setAccessibleName(message)
        self.live_preview_timeout.stop()
        snapshot = None if pivot else self.card_assembly.preview_snapshot()
        self.discard_preview_button.setEnabled(snapshot is not None)
        if report is not None:
            incoming = report.get("card") or {}
            if (snapshot and not snapshot.card.get("exact") and
                    (not incoming.get("callsign") or
                     incoming.get("callsign") == snapshot.card.get("callsign")) and
                    (incoming.get("card_id") is None or
                     incoming.get("card_id") == snapshot.card.get("card_id"))):
                report = {**report, "card": {**dict(snapshot.card),
                    **{key: value for key, value in incoming.items() if value is not None}}}
            self._update_receive_metadata(report)
        elif not snapshot:
            self._update_receive_metadata({})
        if not snapshot and not self.current_preview_is_duplicate:
            self.receive_view.clear()
            self.receive_view.setText("Receiving signal…\nWaiting for card data")
            self.receive_progress.setRange(0, 0)
            self.receive_progress.setValue(0)
            self.receive_progress.setFormat("Waiting for card data")
            self.receive_progress.setAccessibleDescription(
                "Signal detected. Waiting for card data; card-image progress is not available yet.")
            self.receive_progress.hide()
        self.receive_preview_box.show()

    def _receive_activity_expired(self):
        self.receive_activity_pending = False
        snapshot = self.card_assembly.preview_snapshot()
        if snapshot and not snapshot.card.get("exact"):
            self._show_receive_preview(self.card_image(
                {"card": dict(snapshot.card), "pixels": list(snapshot.pixels)}),
                card=snapshot.card,
                coverage=snapshot.coverage, verified_coverage=snapshot.verified_pixels,
                report={"card": dict(snapshot.card), "pixels": list(snapshot.pixels),
                        "receive_profile": self.receive_mode()})
        elif snapshot and snapshot.card.get("exact") and self.current_preview_is_duplicate:
            self.receive_view.setAccessibleName(
                f"Duplicate card preview from {snapshot.card.get('callsign') or 'station not identified'}")
        elif snapshot and snapshot.card.get("exact"):
            self.receive_view.setAccessibleName(
                f"Card preview from {snapshot.card.get('callsign') or 'station not identified'} · verified")
        else:
            self._hide_receive_preview()
        self.rx_status.setText(
            f"Listening · RX mode: {self._receive_mode_label()} · waiting for card data or another copy")
        self.listen_indicator.setText("● LISTENING")

    @staticmethod
    def _coverage_percent(coverage):
        if coverage is None:
            return 0
        if isinstance(coverage, (int, float)):
            return round(max(0.0, min(1.0, float(coverage))) * 100)
        values = list(coverage)
        return round(100 * sum(bool(value) for value in values) / len(values)) if values else 0

    def _clear_exact_live_preview(self):
        snapshot = self.card_assembly.preview_snapshot()
        if snapshot and snapshot.card.get("exact"):
            self.card_assembly.clear_current_preview()

    def receive_mode(self):
        return "auto" if self.receive_all_modes.isChecked() else self.selected_mode()

    def _update_rx_mode_preference_label(self):
        if not hasattr(self, "rx_mode_preference_label"):
            return
        mode = self.rx_preferred_mode
        spec = MODE_REGISTRY.get(mode)
        label = spec.label if spec is not None else str(mode)
        self.rx_mode_preference_label.setText(label)

    def _update_rx_mode_controls(self, *_):
        if not hasattr(self, "rx_mode_lock") or not hasattr(self, "receive_all_modes"):
            return
        advanced = self.settings.value("ui/advanced_controls", False, type=bool)
        supported = self.selected_adapter().capabilities.receive_all_modes
        visible = bool(advanced and supported and self.receive_all_modes.isChecked())
        self.rx_mode_lock.setVisible(visible)
        self.rx_mode_preference_label.setVisible(visible)
        self.rx_mode_preference_form_row.setVisible(visible)
        if self.rx_mode_preference_form_label is not None:
            self.rx_mode_preference_form_label.setVisible(visible)

    def _set_rx_preferred_mode(self, mode, *, detected):
        mode = str(mode or "")
        if not mode or mode not in MODE_REGISTRY:
            return
        if detected and self.rx_mode_lock.isChecked():
            return
        self.rx_preferred_mode = mode
        self.settings.setValue("transfer/rx_preferred_mode", mode)
        self._update_rx_mode_preference_label()

    def _follow_detected_rx_mode(self, profile):
        if not profile or str(profile) == "auto":
            return
        self.rx_live_profile = profile
        self._set_rx_preferred_mode(profile, detected=True)

    def _receive_mode_label(self, profile=None):
        """Return the active RX mode, resolving receive-all to the winning decoder."""
        if profile == "auto":
            return "all enabled modes"
        # In single-mode receive, the selected transfer mode is authoritative.
        # A previously detected decoder may remain useful for receive-all,
        # but must not make the status disagree with the user's selection.
        if profile is None and self.receive_mode() == "auto":
            profile = self.rx_live_profile
        if profile:
            if is_card_backend(profile):
                return get_backend(profile).label
            if profile in MODE_REGISTRY:
                return MODE_REGISTRY[profile].label
            if str(profile).startswith("data2g:"):
                key = str(profile).split(":", 1)[1]
                backend = self.data2g_modes.get(key)
                return backend.label if backend else "Data2G " + key
            if str(profile).lower().startswith("data2g"):
                backend = self.selected_backend()
                return backend.label if backend else "Data2G host/KISS"
            try:
                return modem.resolve_profile(profile).label.split(" · ")[0]
            except (KeyError, TypeError, ValueError):
                return str(profile)
        if self.receive_mode() == "auto":
            return "all enabled modes"
        backend = self.selected_backend()
        if backend is not None:
            return backend.label
        try:
            return modem.resolve_profile(self.selected_profile()).label.split(" · ")[0]
        except (KeyError, TypeError, ValueError):
            return str(self.selected_mode())

    def _listening_status(self):
        return (f"Listening · RX mode: {self._receive_mode_label(self.receive_mode())} · "
                f"{self.session_wall_count} cards heard this session")

    def _save_auto_stage_cards(self, *_):
        for stage, combo in getattr(self, "stage_card_combos", {}).items():
            path = combo.currentData()
            if path:
                self.settings.setValue("transfer/card_" + stage, path)

    def _select_manual_stage(self, stage):
        """Select a stage/card for compatibility and summary updates, without sending."""
        button = getattr(self, "manual_stage_buttons", {}).get(stage)
        if button is None:
            return
        button.setChecked(True)
        self._refresh_transfer_summary()
        self._update_send_button_label()

    def _auto_stage_card_combo(self):
        stage = self._outgoing_message_type()
        return getattr(self, "stage_card_combos", {}).get(stage)

    def _outgoing_message_type(self):
        if self.exchange_mode.currentIndex() == 1 and getattr(self, "auto_armed", False):
            state = getattr(self, "contact_stage", "idle")
            return {"send_cq": "cq", "send_exchange": "exchange",
                    "send_report73": "report73", "send_final73": "final73"}.get(state, "exchange")
        if hasattr(self, "manual_stage_group"):
            button = self.manual_stage_group.checkedButton()
            return button.property("stage") if button else "exchange"
        return "card"

    def _send_stage_card(self, stage):
        if self.data2g_tx_total or self.data2g_tx_waiting is not None or self.tx_audio or self.tx_timer.isActive():
            self._stop_current_transmit()
            return
        if self.auto_armed:
            self.stop_auto_exchange("Automatic exchange stopped for a manual stage retransmission.")
        self.exchange_mode.setCurrentIndex(0)
        self.manual_stage_buttons[stage].setChecked(True)
        self._active_send_control = self.stage_send_buttons[stage]
        combo = self.stage_card_combos[stage]
        path = combo.currentData()
        card = None
        if path:
            data = json.loads(Path(path).read_text())
            card = modem.Card(data["callsign"], data["grid"], [tuple(c) for c in data["palette"]],
                              list(data["pixels"]), data.get("card_id", 0xC0DE),
                              data.get("width", 32), data.get("height", 32), copy.deepcopy(data.get("animation")))
        self.transmit_exchange(card_override=card)

    def _send_quick_draw_card(self):
        if self.quick_reply_card is None:
            return
        if self.data2g_tx_total or self.data2g_tx_waiting is not None or self.tx_audio or self.tx_timer.isActive():
            self._stop_current_transmit()
            return
        self.exchange_mode.setCurrentIndex(0)
        self._active_send_control = self.quick_draw_send_button
        self.transmit_exchange(card_override=self.quick_reply_card)

    def _update_active_send_control(self, transmitting):
        control = getattr(self, "_active_send_control", None)
        if control is None:
            return
        if hasattr(self, "stop_tx_btn"):
            self.stop_tx_btn.setVisible(transmitting and self.stop_tx_btn.isEnabled())
        control.setText("Stop TX" if transmitting else
                        ("Send quick draw" if control is self.quick_draw_send_button else
                         f"Send {control.property('stage_title')}"))
        control.setObjectName("stopTxAction" if transmitting else
                              str(control.property("normal_object_name") or ""))
        control.style().unpolish(control)
        control.style().polish(control)
        control.update()

    def _manual_send_button_text(self, *, local_audio_test=False):
        if local_audio_test:
            return "Send test audio"
        if self.exchange_mode.currentIndex() == 1:
            return "Transmit stage"
        stage = self._outgoing_message_type()
        label = {"card": "card", "cq": "CQ", "exchange": "exchange",
                 "report73": "RR73", "final73": "73"}.get(stage, "card")
        return f"Send {label}"

    def _update_send_button_label(self, *_):
        if not hasattr(self, "tx_btn"):
            return
        data2g_selected = (hasattr(self, "backend_combo") and
                           self.backend_combo.currentData() == "data2g")
        local_audio_test = self.audio_test_mode.isChecked() and not data2g_selected
        self.tx_btn.setText(self._manual_send_button_text(local_audio_test=local_audio_test))

    def _refresh_mode_options(self, preferred=None):
        if not hasattr(self, "mode_combo"):
            return
        self._set_mode_options(self.selected_mode() if preferred is None else preferred)
        self._update_transfer_controls()
        self._refresh_transfer_summary()

    def _current_card_capability(self):
        try:
            card = self._chosen_card()
        except Exception:
            card = getattr(self, "card", None)
        if card is None:
            return None
        return int(card.width), int(card.height), len(card.palette)

    def _mode_capability_label(self, key):
        spec = MODE_REGISTRY.get(key)
        return spec.capability_label if spec is not None else "capabilities unknown"

    def _mode_capability_tooltip(self, key, description=None):
        spec = MODE_REGISTRY.get(key)
        lines = []
        if description:
            lines.append(description)
        if spec is not None:
            lines.append(f"Supports: {spec.capability_label}")
            card = self._current_card_capability()
            if card is not None and not spec.supports_card(*card):
                lines.append(
                    f"Current card: {card[0]}×{card[1]} · {card[2]} colors (incompatible)")
        return "\n\n".join(lines)

    def _refresh_mode_capabilities(self, *_):
        """Refresh compatibility filtering after the selected card changes."""
        if not hasattr(self, "mode_combo"):
            return
        # Rebuild tooltips so an incompatible current card is explained even
        # when the user leaves the filter on All modes.
        self._set_mode_options(self.selected_mode())

    def _set_mode_options(self, preferred):
        data2g = self.backend_combo.currentData() == "data2g"
        recommendations = dict(recommended_data2g_modes(self.data2g_modes)) if data2g else {}
        show_experimental = self.show_experimental_modes.isChecked()
        if data2g:
            entries = list(self.data2g_modes.items())
            if not entries:
                entries = [("data2g_1200_robust", Data2GHostBackend(
                    "data2g_1200_robust", "Connect Data2G host to load available modes"))]
            if recommendations:
                entries = ([(key, self.data2g_modes[key]) for key in recommendations] +
                           [(key, backend) for key, backend in entries if key not in recommendations])
            fallback = "data2g_1200_robust"
        elif show_experimental:
            entries = [(key, backend) for key, backend in
                       available_modes(include_experimental=True)
                       if MODE_REGISTRY[key].adapter != DATA2G_ADAPTER.key and
                       MODE_REGISTRY[key].experimental and
                       key not in TX_DISABLED_MODE_KEYS]
            fallback = "resilient_100"
        else:
            entries = []
            fallback = "resilient_100"
        if self.mode_compatibility_filter.currentData() == "compatible":
            card = self._current_card_capability()
            entries = [(key, backend) for key, backend in entries
                       if (MODE_REGISTRY.get(key) is None or
                           card is None or MODE_REGISTRY[key].supports_card(*card))]
        visible_keys = {key for key, _backend in entries}
        selectable_keys = {key for key, backend in entries
                           if (MODE_REGISTRY.get(key) is None or
                               MODE_REGISTRY[key].adapter != DATA2G_ADAPTER.key or
                               getattr(backend, "usable", False))}
        if fallback not in visible_keys and data2g and entries:
            fallback = entries[0][0]
        selected = (preferred if preferred in selectable_keys else
                    fallback if fallback in selectable_keys else
                    next((key for key, _backend in entries if key in selectable_keys), fallback))
        with QSignalBlocker(self.mode_combo):
            self.mode_combo.clear()
            if not entries:
                self.mode_combo.addItem("No compatible modes for this card")
                self.mode_combo.model().item(0).setEnabled(False)
            for key, backend in entries:
                if recommendations and key not in recommendations and self.mode_combo.count() == len(recommendations):
                    self.mode_combo.insertSeparator(self.mode_combo.count())
                    self.mode_combo.addItem("All other Data2G modes")
                    self.mode_combo.model().item(self.mode_combo.count() - 1).setEnabled(False)
                label = MODE_REGISTRY[key].label if key == LEGACY_MODE_KEY else backend.label
                if key in recommendations:
                    label = f"★ {recommendations[key]} · {backend.label.removeprefix('Data2G · ')}"
                label = f"{label} · {self._mode_capability_label(key)}"
                self.mode_combo.addItem(label, key)
                if key in EXPERIMENTAL_MODE_DESCRIPTIONS:
                    self.mode_combo.setItemData(self.mode_combo.count() - 1,
                        self._mode_capability_tooltip(
                            key, EXPERIMENTAL_MODE_DESCRIPTIONS[key]), Qt.ItemDataRole.ToolTipRole)
                else:
                    self.mode_combo.setItemData(
                        self.mode_combo.count() - 1,
                        self._mode_capability_tooltip(key), Qt.ItemDataRole.ToolTipRole)
                if (MODE_REGISTRY.get(key) is not None and
                        MODE_REGISTRY[key].adapter == DATA2G_ADAPTER.key and
                        not getattr(backend, "usable", False)):
                    item = self.mode_combo.model().item(self.mode_combo.count() - 1)
                    if item is not None:
                        item.setEnabled(False)
                        item.setToolTip(backend.disabled_reason or "Mode is unavailable")
            self.mode_combo.setCurrentIndex(self.mode_combo.findData(selected))
        self.settings.setValue("transfer/mode", selected)
        setting = ("transfer/data2g_mode" if data2g else "transfer/experimental_mode")
        if not data2g or (self.data2g_modes and selected in selectable_keys):
            self.settings.setValue(setting, selected)
        self._mode_has_explicit_selection = selected != fallback
        self._update_transfer_mode_visibility()

    def _update_transfer_mode_visibility(self):
        """Hide the empty Data2G mode selector until the host supplies choices."""
        if not hasattr(self, "transfer_mode_label"):
            return
        unavailable = (self.backend_combo.currentData() == "data2g" and
                       not self.data2g_modes)
        self.mode_combo.setVisible(not unavailable)
        self.transfer_mode_label.setVisible(not unavailable)

    def _update_transfer_controls(self, *_):
        if hasattr(self, "repeat_count"):
            backend = self.selected_backend()
            maximum = getattr(backend, "max_repeats", 20)
            if maximum == 1 and self.repeat_count.maximum() != 1:
                self._normal_repeat_count = self.repeat_count.value()
                with QSignalBlocker(self.repeat_count):
                    self.repeat_count.setMaximum(1)
            elif maximum != 1 and self.repeat_count.maximum() == 1:
                with QSignalBlocker(self.repeat_count):
                    self.repeat_count.setMaximum(maximum)
                    self.repeat_count.setValue(getattr(self, "_normal_repeat_count", 1))
            else:
                self.repeat_count.setMaximum(maximum)
            framing = ("One protected header covers the group; airtime includes it."
                       if getattr(backend, "requires_common_header", False) else
                       "Each copy includes protected framing; airtime includes it.")
            compression = (" Compression is automatic; airtime depends on the chosen card."
                           if getattr(backend, "automatic_compression", False) else "")
            self.repeat_count.setToolTip(
                "Send copies of the same card. RX combines soft evidence and checks integrity. " +
                framing + compression)
            if hasattr(self, "interlaced_copies"):
                supported = bool(getattr(backend, "supports_interlaced_copies", False))
                # Keep the global preference editable in every mode so the
                # operator can turn it off even when the selected encoder
                # currently ignores it (or copies are temporarily set to 1).
                self.interlaced_copies.setEnabled(True)
                self.interlaced_copies.setToolTip(
                    "Stagger LDPC/RS block order across protected copies; the receiver restores each block before combining."
                    if supported and maximum > 1 else
                    "Interlaced copies apply to supported local modem modes when sending multiple copies. "
                    "This preference remains available so you can turn it off here.")
        legacy = self.selected_mode() == "standard"
        if hasattr(self, "manual_card_widget"):
            show_legacy_card = legacy and self.exchange_mode.currentIndex() != 2
            self.manual_card_widget.setVisible(show_legacy_card and self.exchange_mode.currentIndex() != 1)
            self.manual_card_label.setVisible(show_legacy_card and self.exchange_mode.currentIndex() != 1)

    def _refresh_transfer_summary(self, *_):
        if not hasattr(self, "transfer_summary"):
            return
        self._refresh_send_workspace()
        def set_summary(text, data2g=False):
            if data2g and self.data2g_ptt_failed:
                text += " · PTT fault pending: verify radio is unkeyed before transmitting"
            self.transfer_summary.setText(text)
        try:
            backend = self.selected_backend()
            data2g = (self.selected_adapter().capabilities.kind is
                      AdapterKind.FRAME_TRANSPORT)
            if data2g:
                if backend is None or getattr(backend, "mode", None) is None:
                    set_summary(
                        "Connect your station to load available transfer modes and an airtime estimate.", True)
                    return
                if backend.disabled_reason:
                    set_summary(backend.disabled_reason, True)
                    return
            card = self._chosen_card()
            capability = MODE_REGISTRY.get(self.selected_mode())
            capability_note = ""
            if capability is not None:
                capability_note = f" · modem supports {capability.capability_label}"
                if not capability.supports_card(card.width, card.height, len(card.palette)):
                    identity = " ".join(value for value in (card.callsign, card.grid) if value)
                    set_summary(
                        f"{identity} · {self._outgoing_message_type()} · "
                        f"{card.width}×{card.height} · {len(card.palette)} colors · "
                        f"incompatible with modem (supports {capability.capability_label})",
                        data2g)
                    return
            if backend is not None:
                card = self._prepare_outgoing_card(card, self._outgoing_message_type())
                total_seconds = backend.estimate_seconds(card, self.repeat_count.value())
            else:
                symbols, profile = self._card_tx_symbols(card)
                from modem_envelope import HEADER_SECONDS, metadata_header_count
                card = self._prepare_outgoing_card(card, self._outgoing_message_type())
                total_seconds = len(symbols) / profile.baud + (2+metadata_header_count(card, mode))*HEADER_SECONDS*self.repeat_count.value()
                if self.cwid_enabled.isChecked():
                    station_call = self.station_call.text().strip().upper()
                    cwid_text = self.cwid_message.text().strip().upper()
                    cwid_text = cwid_text.replace("{CALLSIGN}", station_call).replace(
                        "{CALL}", station_call)
                    if cwid_text:
                        total_seconds += (0.35 * modem.SAMPLE_RATE +
                                          len(morse_audio(cwid_text))) / modem.SAMPLE_RATE
            timing = (f"~{total_seconds:.1f}s RF airtime" if data2g else
                      f"{total_seconds:.1f}s")
            stage = self._outgoing_message_type()
            stage_label = {"card": "Card", "cq": "CQ", "exchange": "Exchange",
                           "report73": "RR73", "final73": "73"}.get(stage, "Card")
            identity = " ".join(value for value in (card.callsign, card.grid) if value)
            set_summary(
                f"{identity} · {stage_label} · {card.width}×{card.height} · "
                f"{len(card.palette)} colors · {timing}{capability_note}", data2g)
            if mode_supports_audio_placement(self.selected_mode()):
                tones = backend.profile.tones_hz
                self.transfer_summary.setText(self.transfer_summary.text() + f" · {tones[0]:g}–{tones[-1]:g} Hz audio")
            elif data2g:
                set_summary(
                    self.transfer_summary.text() +
                    " · host channel-access waits can extend elapsed time · "
                    "radio/audio/PTT are controlled by the Data2G host", True)
        except Exception as exc:
            set_summary(str(exc) if getattr(self.selected_backend(), "max_repeats", None) == 1 else
                        "Choose a card and burst type to see estimated send time.",
                        self.selected_adapter().capabilities.kind is AdapterKind.FRAME_TRANSPORT)

    def _make_transmit_preview(self):
        self.tx_preview_box = QGroupBox('↑ SENDING · outgoing card')
        self.tx_preview_box.setObjectName('transmitPreview')
        self.tx_preview_box.setAccessibleName('Outgoing transmission preview')
        self.tx_preview_box.setStyleSheet(
            'QGroupBox#transmitPreview { border:2px solid #c69a54; border-radius:9px; '
            'background:#201c16; margin-top:14px; padding:12px; } '
            'QGroupBox#transmitPreview::title { color:#ffd48a; font-weight:800; }')
        layout = QVBoxLayout(self.tx_preview_box)
        self.tx_preview_identity = QLabel(); self.tx_preview_identity.setWordWrap(True)
        self.tx_preview_identity.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.tx_preview_identity)
        self.tx_preview_phase = QLabel('Preparing outgoing card')
        self.tx_preview_phase.setWordWrap(True)
        self.tx_preview_phase.setStyleSheet('color:#ffd48a; font-weight:700;')
        layout.addWidget(self.tx_preview_phase)
        self.tx_preview_view = QLabel(); self.tx_preview_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tx_preview_view.setMinimumSize(194, 194)
        self.tx_preview_view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.tx_preview_view.setAccessibleName('Artwork being sent')
        layout.addWidget(self.tx_preview_view, 1)
        self.tx_preview_progress = QProgressBar(); self.tx_preview_progress.setRange(0, 1000)
        self.tx_preview_progress.setAccessibleName('Outgoing transmission progress')
        layout.addWidget(self.tx_preview_progress)
        self.tx_preview_detail = QLabel(); self.tx_preview_detail.setWordWrap(True)
        layout.addWidget(self.tx_preview_detail)
        notice = QLabel('LOCAL SEND PROGRESS · remote reception unconfirmed')
        notice.setWordWrap(True); notice.setObjectName('supportingText'); layout.addWidget(notice)
        return self.tx_preview_box

    def _begin_transmit_preview(self, model, mode_label, stage):
        if model is None:
            return
        self._active_tx_preview = model
        self._tx_preview_last_update = -float('inf')
        self._tx_preview_total_audio_samples = len(getattr(self, 'tx_bytes', b''))//2
        card = model.report['card']
        stage_label = {'cq': 'CQ', 'exchange': 'Exchange', 'report73': 'RR73 · report',
                       'final73': '73 · final'}.get(stage, 'Card')
        if stage == '73':
            stage_label = 'RR73 · report' if getattr(self.tx_card, 'reception_report', None) else '73 · final'
        self.tx_preview_identity.setText(
            f"{card['callsign']} · {card.get('grid', '')} · Card {card['card_id']:04X} · {stage_label} · {mode_label}")
        title = ('↑ SENDING TEST AUDIO · outgoing card' if isinstance(model, LocalTransmitPreview)
                 and self.audio_test_mode.isChecked() else '↑ SENDING · outgoing card')
        self.tx_preview_box.setTitle(title)
        self.traffic_preview.setCurrentWidget(self.tx_preview_box)
        self._render_transmit_preview(model.state(0) if isinstance(model, LocalTransmitPreview) else model.state())

    def _render_transmit_preview(self, state):
        report = {**state['report'], 'card': {**state['report']['card']}}
        if state['coverage'] is not None:
            report['card']['pixel_coverage'] = state['coverage']
        image = self.card_image(report)
        if image is not None:
            if state.get('opacity', 1) < 1:
                faded = QImage(image.size(), QImage.Format.Format_RGB32); faded.fill(QColor('#201c16'))
                painter = QPainter(faded); painter.setOpacity(state['opacity']); painter.drawImage(0, 0, image); painter.end()
                image = faded
            size = QSize(max(194, self.tx_preview_view.width()), max(194, self.tx_preview_view.height()))
            self.tx_preview_view.setPixmap(QPixmap.fromImage(image).scaled(size,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
        self.tx_preview_view.setAccessibleName('Outgoing artwork · ' + state['phase'])
        self.tx_preview_phase.setText(f"Copy {state['copy']} · {state['phase']}")
        self.tx_preview_detail.setText(state['detail'])
        percent = max(0, min(1000, round(state['progress']*1000)))
        self.tx_preview_progress.setValue(percent)
        label = 'host-confirmed frames' if isinstance(getattr(self, '_active_tx_preview', None), Data2GTransmitPreview) else 'audio playback'
        self.tx_preview_progress.setFormat(f'{percent/10:.0f}% · {label}')

    def _update_local_transmit_preview(self, *, force=False):
        model = getattr(self, '_active_tx_preview', None)
        if not isinstance(model, LocalTransmitPreview):
            return
        now = time.monotonic()
        if not force and now-self._tx_preview_last_update < .1:
            return
        self._tx_preview_last_update = now
        if self.test_link_enabled and self.audio_test_mode.isChecked():
            samples = self.tx_offset//2
        elif self.tx_audio is not None:
            samples = min(self.tx_offset//2, round(self.tx_audio.processedUSecs()*modem.SAMPLE_RATE/1e6))
        else:
            samples = 0
        state = model.state(samples)
        if samples >= round(model.seconds*modem.SAMPLE_RATE) and self._tx_preview_total_audio_samples > samples:
            state['phase'] = 'Sending CW identification'
        state['progress'] = min(1, samples/max(1, self._tx_preview_total_audio_samples))
        self._render_transmit_preview(state)

    def _end_transmit_preview(self, completed=False):
        if getattr(self, '_active_tx_preview', None) is None:
            return
        if completed:
            self.tx_preview_progress.setValue(1000)
            self.tx_preview_progress.setFormat('100% · local send complete')
        self.tx_preview_phase.setText('Local send complete' if completed else 'Stopped · outgoing card')
        self._active_tx_preview = None
        self.traffic_preview.setCurrentWidget(self.receive_workspace)

    def _card_tx_audio(self, card, *, beacon=False):
        if self.selected_mode() == LEGACY_MODE_KEY:
            raise ValueError(
                "The Soft Viterbi modem is receive-only. Select another transfer mode to transmit.")
        stage = "card" if beacon else self._outgoing_message_type()
        card = self._prepare_outgoing_card(card, stage)
        wire_type = {"report73": "73", "final73": "73"}.get(stage, stage)
        snr_db = self.tx_snr_db if stage in {"exchange", "report73"} else None
        backend = self.selected_backend()
        self._prepared_tx_preview = None
        adapter = self.selected_adapter()
        if adapter.capabilities.kind is AdapterKind.FRAME_TRANSPORT:
            raise ValueError(
                "Data2G creates the radio waveform inside its host and does not export "
                "a local WAV. Select Experimental modems to export local transmission audio.")
        if backend is not None:
            if adapter.capabilities.kind is not AdapterKind.LOCAL_IMAGE_MODEM:
                raise ValueError(f"Selected modem adapter {adapter.key!r} cannot encode local audio")
            audio = adapter.encode(backend, card, self.repeat_count.value(), wire_type, snr_db,
                                   interlaced=self.interlaced_copies.isChecked())
            self._prepared_tx_preview = LocalTransmitPreview(card, self.selected_mode(), backend,
                self.repeat_count.value(), wire_type, snr_db, len(audio))
            return audio
        encode_legacy = getattr(adapter, "encode_legacy", None)
        if not callable(encode_legacy):
            raise ValueError(f"Selected modem adapter {adapter.key!r} does not support legacy packet encoding")
        audio = encode_legacy(card, self.repeat_count.value(), wire_type, snr_db,
                              self.selected_mode(), self.selected_profile(), beacon=beacon)
        self._prepared_tx_preview = LocalTransmitPreview(card, self.selected_mode(), None,
            self.repeat_count.value(), wire_type, snr_db, len(audio))
        return audio

    def _append_cwid_audio(self, audio):
        """Append the configured CW ID to locally generated modem audio."""
        self.tx_cwid_appended = False
        if not self.cwid_enabled.isChecked():
            return audio
        station_call = self.station_call.text().strip().upper()
        message = self.cwid_message.text().strip().upper()
        message = message.replace("{CALLSIGN}", station_call).replace("{CALL}", station_call)
        if not message:
            return audio
        cwid = morse_audio(message, sample_rate=modem.SAMPLE_RATE)
        if not len(cwid):
            return audio
        separator = np.zeros(round(0.35 * modem.SAMPLE_RATE), dtype=np.float32)
        self.tx_cwid_appended = True
        return np.concatenate((np.asarray(audio, dtype=np.float32), separator, cwid))

    def _card_tx_symbols(self, card, *, beacon=False):
        mode = self.selected_mode()
        if card.width != 32 or card.height != 32 or len(card.palette) != 8:
            raise ValueError("The experimental legacy packet card supports only 32×32 with 8 colors. Use a burst transmission for other canvas formats.")
        packets = (modem.make_beacon_packets(card, mode) if beacon else modem.make_packets(card, mode))
        cycle = modem.all_symbols(packets)
        return modem.repeat_symbol_stream(cycle, self.repeat_count.value()), (
            self.selected_profile())

    def _should_auto_connect_rig(self):
        backend = self.selected_backend()
        return (self.auto_connect_cat.isChecked() and not self.audio_test_mode.isChecked()
                and not self.test_link_enabled and
                self.selected_adapter().capabilities.radio_owner != "data2g_host")

    def connect_rig(self):
        if self.rig.connected():
            self.rig.disconnect()
            return
        if (self.backend_combo.currentData() == "experimental" and
                self._station_settings_connection_edits_pending()):
            self.rig_status.setText(
                "Apply or cancel the pending rig connection changes before connecting.")
            return
        if (self.data2g_connecting or self.data2g_local_starting or self.data2g_closing or
                self.data2g_handoff_pending or
                DATA2G_ADAPTER.is_connected(self.data2g_session)):
            self.rig_status.setText(
                "CAT connection blocked while Data2G owns or is acquiring radio/audio")
            return
        if not self._confirm_data2g_ptt_recovery():
            return
        self.settings.setValue("radio/rigctld_host", self.rig_host.text().strip())
        self.settings.setValue("radio/rigctld_port", self.rig_port.value())
        self.rig_status.setText("Connecting to rig control service…")
        self.rig.connect_to(self.rig_host.text().strip(), self.rig_port.value(), self.rig_changed)

    def refresh_tx_button(self):
        backend_switch_locked = bool(
            self.auto_armed or self.ptt_active or self.tx_audio is not None or
            self.tx_timer.isActive() or getattr(self, "data2g_tx_waiting", None) is not None or
            getattr(self, "data2g_local_starting", False) or
            getattr(self, "data2g_closing", False) or
            getattr(self, "data2g_handoff_pending", False) or
            getattr(self, "data2g_tx_total", 0) or
            (hasattr(self, "stop_tx_btn") and self.stop_tx_btn.isVisible()))
        switching_locked = backend_switch_locked or bool(
            getattr(self, "data2g_connecting", False))
        if hasattr(self, "backend_combo"):
            # Keep the backend choice available while discovery is in flight:
            # selecting Experimental closes that pending Data2G session, then
            # resumes local CAT/audio only after the host releases ownership.
            self.backend_combo.setEnabled(not backend_switch_locked)
        if hasattr(self, "mode_combo"):
            self.mode_combo.setEnabled(not switching_locked)
        if hasattr(self, "show_experimental_modes"):
            # Revealing the alternate backend is part of choosing it; allow
            # this preference to change during discovery, while keeping the
            # actual mode selector locked until discovery or handoff completes.
            self.show_experimental_modes.setEnabled(not backend_switch_locked)
        test_ready = (self.audio_test_mode.isChecked() and
                      (self.test_link_enabled or self.output_device.currentData()))
        data2g_selected = (hasattr(self, "backend_combo") and
                           self.backend_combo.currentData() == "data2g")
        host_backend = self.selected_backend()
        data2g_connected = bool(
            data2g_selected and self.data2g_session is not None and
            DATA2G_ADAPTER.is_connected(self.data2g_session))
        host_ready = (data2g_selected and host_backend is not None and
                      getattr(host_backend, "usable", False) and data2g_connected)
        radio_ready = (host_ready if data2g_selected else test_ready or self.rig.connected())
        ready = (radio_ready and not self.auto_armed and not self.ptt_active
                 and self.tx_audio is None and not self.tx_timer.isActive()
                 and not getattr(self, "data2g_tx_total", 0)
                 and not getattr(self, "data2g_connecting", False)
                 and not getattr(self, "data2g_closing", False)
                 and not getattr(self, "data2g_handoff_pending", False)
                 and self.rig.pending is None and not self.rig.queue
                 and not getattr(self, "beacon_pending", False))
        if os.environ.get("PIXELQSO_AUDIO_ROLE") == "websdr-rx":
            ready = False
        self.tx_btn.setEnabled(ready)
        local_audio_test = (self.audio_test_mode.isChecked() and not data2g_selected)
        self.tx_btn.setText(self._manual_send_button_text(local_audio_test=local_audio_test))
        reason = ""
        if not ready and not self.auto_armed:
            if self.data2g_ptt_failed:
                reason = "Data2G reported a PTT fault. Verify that the radio is unkeyed, then review the recovery prompt in Station settings."
            elif data2g_selected and self.data2g_connecting:
                reason = "Connecting to the Data2G host. Sending will be available after mode discovery finishes."
            elif data2g_selected and not host_ready:
                if not data2g_connected:
                    reason = "Connect station to connect to a Data2G host before sending."
                elif host_backend is not None and getattr(host_backend, "disabled_reason", None):
                    reason = host_backend.disabled_reason
                elif host_backend is None:
                    reason = "The Data2G host is connected but has not reported a usable card mode yet."
                else:
                    reason = "The selected Data2G mode is not ready for card transmission."
            elif local_audio_test and not test_ready:
                reason = "Choose an audio output in Connect station before sending test audio."
            elif not data2g_selected and not self.rig.connected() and not test_ready:
                reason = "Connect radio control in Connect station before transmitting."
            elif not data2g_selected and not (self.input_device.currentData() and
                                               self.output_device.currentData()) and not test_ready:
                reason = "Choose both an audio input and output in Connect station before transmitting."
            elif getattr(self, "data2g_connecting", False):
                reason = "Wait for the Data2G connection to finish."
            elif self.rig.pending is not None or self.rig.queue:
                reason = "Waiting for the radio control service to finish its current request."
            else:
                reason = "The radio is busy. Wait for the current operation to finish."
        self.tx_availability_hint.setText(reason)
        self.tx_availability_hint.setVisible(bool(reason))
        self.tx_btn.setToolTip(reason)
        if self.auto_armed:
            cq_help = (
                "Stop the active CQ repeat and automatic exchange."
                if self.auto_role == "caller" and self.call_cq_active else
                "An automatic exchange is active. Stop it before starting a CQ call.")
        else:
            cq_help = reason or (
                "Starts the automatic call-first exchange: send a CQ now, then repeat every "
                f"{self.cq_interval.value()} seconds between contacts.")
        self.call_cq_button.setToolTip(cq_help)
        if hasattr(self, "beacon_button"):
            self.beacon_button.setEnabled(
                self.beacon_button.isChecked() or (ready and not local_audio_test))
        self._refresh_operate_primary_action()

    def rig_changed(self, connected, message):
        self.rig_status.setText(message)
        self.rig_button.setText("Disconnect CAT" if connected else "Connect CAT")
        self.refresh_tx_button()
        if not connected: self.rig_freq_read = False
        if not connected and self.auto_armed and not self.audio_test_mode.isChecked():
            self.stop_auto_exchange(log_message="CAT disconnected; automatic exchange stopped. Verify the radio is unkeyed manually.")
        if not connected and not self.audio_test_mode.isChecked() and (self.tx_audio or self.ptt_active):
            if self.tx_timer.isActive(): self.tx_timer.stop()
            if self.tx_audio: self.tx_audio.stop()
            self.tx_audio = None; self.tx_device = None
            self._end_transmit_preview(completed=False)
            self.stop_tx_btn.setEnabled(False)
            self.exchange_log.append("CAT disconnected during transmission. Audio stopped; verify the radio is unkeyed manually.")
        if connected: self.read_rig()
        elif self.rig_reconnect_after_settings and not self._closing:
            self.rig_reconnect_after_settings = False
            QTimer.singleShot(0, self.connect_rig)
        # CAT owns transmit control and frequency readback, not audio capture.
        # A failed connection or disconnect must still leave idle RX running.
        if (not self._closing and
                self.selected_adapter().capabilities.kind is not AdapterKind.FRAME_TRANSPORT):
            QTimer.singleShot(0, self._ensure_session_receive)
        if getattr(self, "_station_settings_editing", False):
            self._update_station_settings_actions()

    def read_rig(self, on_complete=None):
        if not self.rig.connected():
            self.rig_freq_read = False; self.rig_mode = ""
            self.rig_status.setText("Connect CAT first to read frequency.")
            if on_complete: on_complete(False)
            return
        def got_frequency(result):
            try:
                if not result or result[0].startswith(("ERROR", "RPRT")):
                    raise ValueError("CAT returned no frequency")
                value = int(float(result[0]))
                self.frequency.setValue(value)
                self.rig_freq_read = True
                self.rig.request("get_mode", 2, lambda mode: self._show_rig_mode(value, mode, on_complete))
            except (ValueError, IndexError):
                self.rig_freq_read = False; self.rig_mode = ""
                self.rig_status.setText("Frequency read failed: " + " ".join(result))
                if on_complete: on_complete(False)
        self.rig.request("get_freq", 1, got_frequency)

    def _show_rig_mode(self, frequency, response, on_complete=None):
        self.rig_mode = response[0].upper() if response and not response[0].startswith(("ERROR", "RPRT")) else ""
        self.rig_status.setText(f"CAT connected · {frequency/1_000_000:.6f} MHz · {self.rig_mode or 'mode unavailable'}")
        if on_complete: on_complete(bool(self.rig_freq_read and self.rig_mode))

    def _chosen_card(self):
        stage_combo = (None if self.exchange_mode.currentIndex() == 2 else self._auto_stage_card_combo()
                       if self.exchange_mode.currentIndex() == 1 else
                       self.stage_card_combos.get(self._outgoing_message_type()))
        selected_path = stage_combo.currentData() if stage_combo is not None else None
        if stage_combo is None and hasattr(self, "card_combo"):
            selected_path = self.card_combo.currentData()
        if selected_path:
            data = json.loads(Path(selected_path).read_text())
            card = modem.Card(data["callsign"], data["grid"], [tuple(c) for c in data["palette"]],
                              list(data["pixels"]), data.get("card_id", 0xC0DE), data.get("width", 32), data.get("height", 32), copy.deepcopy(data.get("animation")))
            card = normalize_card_palette(card)
        else:
            card = modem.Card(self.call.text().strip().upper(), self.grid.text().strip().upper(),
                              list(self.card.palette), list(self.card.pixels), self.card.card_id,
                              self.card.width, self.card.height, copy.deepcopy(self.card.animation))
        card.validate()
        return card

    def _remember_tx_card(self, card):
        self.tx_card = copy.deepcopy(card)
        quick = self.quick_reply_card
        self.tx_quick_draw_card = (self.tx_card if quick is not None and not self.auto_armed
                                   and self.exchange_mode.currentIndex() == 0
                                   and quick.card_id == card.card_id
                                   and quick.callsign == card.callsign and quick.grid == card.grid
                                   and quick.pixels == card.pixels else None)

    def _prepare_outgoing_card(self, card, stage):
        """Freeze the current receive evidence for this one outgoing reply."""
        result = copy.deepcopy(card)
        result.reception_report = None
        received = self.reply_report
        subject = (received or {}).get('card') or {}
        if stage in {'exchange', 'report73'} and subject.get('callsign') and subject.get('card_id') is not None:
            result.reception_report = make_reception_report(received)
        return result

    def _toggle_call_cq(self, enabled):
        self.call_cq_active = bool(enabled)
        self.call_cq_button.setText("Calling CQ · stop" if enabled else "Call CQ")
        self.cq_interval.setEnabled(not enabled)
        if enabled:
            self.exchange_mode.setCurrentIndex(1)
            self.exchange_role.setCurrentIndex(0)
            if not self.auto_armed:
                self.start_auto_exchange()
                if not self.auto_armed:
                    self.call_cq_active = False
                    with QSignalBlocker(self.call_cq_button): self.call_cq_button.setChecked(False)
                    self.call_cq_button.setText("Call CQ")
                    self.cq_interval.setEnabled(True)
            else:
                self.contact_stage = "send_cq"
                self._refresh_contact_stage_indicator()
                self.transmit_exchange()
        else:
            self.cq_timer.stop()
            if self.auto_role == "caller" and self.auto_armed:
                self.stop_auto_exchange("CQ calling stopped.")
            self.contact_stage = "idle"
            self._refresh_contact_stage_indicator()

    def _save_cq_interval(self, value):
        self.settings.setValue("transfer/cq_interval_seconds", value)
        self._refresh_contact_stage_indicator()
        self.refresh_tx_button()

    def _repeat_cq(self):
        if not self.call_cq_active or not self.auto_armed or self.auto_role != "caller":
            return
        if self.rx_timer.isActive():
            self._discard_receive_for_tx()
        self.contact_stage = "send_cq"
        self._refresh_contact_stage_indicator()
        self.transmit_exchange()

    def _stop_local_receive_capture(self):
        handle = getattr(self, "local_receive_handle", None)
        adapter = getattr(self, "local_receive_adapter", None)
        errors = ()
        try:
            if handle is not None and adapter is not None:
                result = adapter.stop_receive(handle)
                errors = ((result,) if isinstance(result, str) else tuple(result or ()))
        except Exception as exc:
            errors = (f"receive adapter: {exc}",)
        finally:
            self.local_receive_handle = None
            self.local_receive_adapter = None
            self.rx_audio = None
            self.rx_device = None
            self.test_rx_socket = None
        for error in errors:
            if hasattr(self, "exchange_log"):
                self.exchange_log.append(f"Local receive resource release issue: {error}")

    def _discard_receive_for_tx(self):
        self._flush_on_air_rx()
        self.rx_timer.stop()
        self.live_preview_timeout.stop()
        self.auto_receive_timer.stop()
        self._stop_local_receive_capture()
        self.card_assembly.begin_receive_window()
        self.copy_capture_cache = self.copy_capture_cache.next_window()
        self.rx_capture_sample_offset = 0
        self._clear_exact_live_preview()
        self._clear_rx_bytes()
        self.test_rx_pending = {}; self.test_rx_expected = None
        self.test_rx_final_pending = False; self.live_decode_final = False
        if hasattr(self, "receive_stack"):
            self.receive_stack.setCurrentWidget(self.session_wall_page)

    def _pause_receive_for_tx(self):
        if self.rx_timer.isActive():
            self._discard_receive_for_tx()
        self.listen_indicator.setText("● TRANSMITTING")
        self.listen_indicator.setStyleSheet(status_label_style("warning"))

    def _ensure_session_receive(self):
        if self.rx_timer.isActive() or self.tx_timer.isActive() or self.ptt_active:
            return
        if self.selected_adapter().capabilities.kind is AdapterKind.FRAME_TRANSPORT:
            self.listen_indicator.setText("● DATA2G HOST RX")
            self.listen_indicator.setStyleSheet(status_label_style("positive"))
            return
        if self.auto_armed and self.auto_wait_state not in {"listening", "peer_response", "initial_card"}:
            return
        self.start_receive(quiet=True)

    def reply_to_received(self):
        report = self.reply_report
        card = (report or {}).get("card") or {}
        if not card:
            return
        if self._reply_stage(report) is None:
            self.reply_to_button.hide()
            self._refresh_operate_primary_action()
            return
        self.stopped_reply_peers.discard(str(card.get("callsign") or "").strip().upper())
        stage = card.get("message_type", "card")
        if stage == "cq":
            if self.auto_armed:
                return
            if self._assembly_verified(report):
                self.exchange_mode.setCurrentIndex(1)
                self.exchange_role.setCurrentIndex(1)
                self.start_auto_exchange(reply_report=report)
                return
            next_stage = 'exchange'
        elif stage == 'card':
            next_stage = 'exchange'
        elif stage == "exchange":
            next_stage = "report73"
        elif stage == "73" and (card.get("snr_db") is not None or card.get('reception_report')):
            next_stage = "final73"
        else:
            self.reply_to_button.setVisible(False)
            self._refresh_operate_primary_action()
            return
        if next_stage in self.manual_stage_buttons:
            self.manual_stage_buttons[next_stage].setChecked(True)
        self.tx_snr_db = card.get("measured_snr_db") if next_stage in {"exchange", "report73"} else None
        self.reply_to_button.setVisible(False)
        self._refresh_operate_primary_action()
        self.contact_stage = "send_" + next_stage
        self.exchange_mode.setCurrentIndex(0)
        self.transmit_exchange()

    def _feedback_matches_contact(self, card):
        feedback = card.get('reception_report')
        return (not self.auto_armed or not feedback or
                feedback.get('callsign') == self.station_call.text().strip().upper() and
                feedback.get('card_id') == self.auto_own_card_id)

    def _show_reply_action(self, report):
        card = report.get("card") or {}
        if str(card.get("callsign") or "").strip().upper() in self.stopped_reply_peers:
            return
        if str(card.get('callsign') or '').upper() == self.station_call.text().strip().upper():
            return
        if self.auto_armed and self.auto_peer and str(card.get('callsign') or '').upper() != self.auto_peer:
            return
        if self.auto_armed and not self._assembly_verified(report):
            return
        if not self._feedback_matches_contact(card):
            return
        self.reply_report = report
        if self.auto_armed:
            self.reply_to_button.setVisible(False)
            self.quick_draw_button.setVisible(False)
            self.quick_draw_button.setText("Quick draw")
            self.quick_draw_button.setToolTip(
                "Sketch a card while receiving or transmitting; use it for the next manual send.")
            self._refresh_operate_primary_action()
            return
        kind = card.get("message_type", "card")
        peer = str(card.get("callsign") or "station")
        next_stage = {
            "cq": f"Reply to {peer}",
            "card": f"Send Exchange to {peer}",
            "exchange": f"Send RR73 with report to {peer}",
        }.get(kind)
        if kind == "73" and self._reply_stage(report) == 'final73':
            next_stage = f"Send final 73 to {peer}"
        provisional = not self._assembly_verified(report)
        if provisional and next_stage:
            next_stage += ' · provisional'
        self.reply_to_button.setText(next_stage or "Reply")
        self.reply_to_button.setToolTip(
            f"Send the next exchange stage to {peer}, including received pixels and CRC-passed blocks." if next_stage else "")
        self.reply_to_button.setVisible(bool(next_stage))
        quick_reply = self._reply_stage(report) is not None
        self.quick_draw_button.setText(
            f"Quick reply to {peer}" if quick_reply else "Quick draw")
        self.quick_draw_button.setToolTip(
            f"Draw a card for {peer}'s next exchange stage."
            if quick_reply else
            "Sketch a card while receiving or transmitting; use it for the next manual send.")
        self.quick_draw_button.setVisible(self.exchange_mode.currentIndex() == 0 and not self.auto_armed)
        self._refresh_operate_primary_action()

    def stop_responding(self):
        peer = self.auto_peer or str((self.reply_report or {}).get("card", {}).get("callsign") or "")
        if peer:
            self.stopped_reply_peers.add(peer.strip().upper())
        self.reply_report = None
        self.auto_generation += 1
        self.auto_armed = False
        if self.data2g_tx_total:
            self._stop_current_transmit()
        self.stop_auto_exchange(f"Stopped responding to {peer or 'this station'}; continuing to listen.")
        self.contact_stage = "idle"
        self.tx_snr_db = None
        self.reply_to_button.hide()
        self.exchange_mode.setCurrentIndex(0)
        self._refresh_contact_stage_indicator()
        self._refresh_operate_primary_action()

    @staticmethod
    def _reply_stage(report):
        card = (report or {}).get("card") or {}
        kind = card.get("message_type", "card")
        if kind in {"cq", "card"}:
            return "exchange"
        if kind == "exchange":
            return "report73"
        if kind == '73' and card.get('reception_report') and not card['reception_report'].get('verified'):
            return None
        if kind == "73" and (card.get("snr_db") is not None or card.get('reception_report')):
            return "final73"
        return None

    def _handle_contact_message(self, report, *, initiated=False):
        card = report.get("card") or {}
        if not card.get("avatar_burst"):
            return
        callsign = str(card.get('callsign') or '').strip().upper()
        if self.auto_armed and self.auto_peer and callsign != self.auto_peer:
            self.exchange_log.append(f"Ignoring {callsign}; current contact is with {self.auto_peer}.")
            return
        if not self._feedback_matches_contact(card):
            self.exchange_log.append(f'Ignoring reception report from {callsign} for a different station or card.')
            return
        kind = card.get("message_type", "card")
        self._show_reply_action(report)
        if not self._assembly_verified(report):
            return
        feedback = card.get('reception_report') or {}
        if (feedback and not feedback.get('verified') and
                feedback.get('callsign') == self.station_call.text().strip().upper() and
                (not self.auto_armed or feedback.get('card_id') == self.auto_own_card_id)):
            self.exchange_log.append('\n'.join(reception_report_lines(card)))
            self.rx_status.setText(f"{card.get('callsign')} reports an incomplete card · retry needed")
            return
        if kind == "cq":
            self.last_cq_snapshot = None
        if kind == "exchange" and self.last_cq_snapshot and self.active_qso_log is not None:
            has_cq = any((item.get("card") or {}).get("message_type") == "cq"
                         for item in self.active_qso_log.get("sent_cards", []))
            if not has_cq:
                self._append_qso_card("sent_cards", self.last_cq_snapshot)
        if not self.auto_armed:
            return
        callsign = str(card.get("callsign", "")).strip().upper()
        own_call = self.station_call.text().strip().upper()
        if not callsign or callsign == own_call:
            return
        if self.auto_peer and callsign != self.auto_peer:
            self.exchange_log.append(f"Ignoring {callsign}; current contact is with {self.auto_peer}.")
            return
        expected = ((kind == "cq" and self.auto_role == "responder" and initiated and self.contact_stage == "listen_cq") or
                    (kind == "exchange" and self.auto_role == "caller" and self.contact_stage == "listen_cq") or
                    (kind == "73" and self.auto_role == "responder" and self.contact_stage == "await_report73") or
                    (kind == "73" and self.auto_role == "caller" and self.contact_stage == "await_final73"))
        if not expected:
            return
        self.reply_report = report
        if not self.auto_peer:
            self.auto_peer = callsign
        if kind == "cq" and self.auto_role == "responder":
            self.tx_snr_db = card.get("measured_snr_db")
            self.contact_stage = "send_exchange"
            self._refresh_contact_stage_indicator()
            self.exchange_log.append(f"CQ received from {callsign}; sending exchange card.")
            self._schedule_contact_transmit()
        elif kind == "exchange" and self.auto_role == "caller":
            self.cq_timer.stop()
            if self.active_qso_log is None:
                self._begin_qso_log(callsign, card.get("grid", ""))
            has_cq = any((item.get("card") or {}).get("message_type") == "cq"
                         for item in self.active_qso_log.get("sent_cards", []))
            if self.last_cq_snapshot and not has_cq:
                self._append_qso_card("sent_cards", self.last_cq_snapshot)
            self.tx_snr_db = card.get("measured_snr_db")
            self.contact_stage = "send_report73"
            self._refresh_contact_stage_indicator()
            self.exchange_log.append(f"Exchange received from {callsign}; sending RR73 with measured SNR.")
            self._schedule_contact_transmit()
        elif (kind == "73" and self.auto_role == "responder" and
              self.contact_stage == "await_report73"):
            self.contact_stage = "send_final73"
            self._refresh_contact_stage_indicator()
            report = "with reported SNR " + str(card["snr_db"]) if card.get("snr_db") is not None else "without an SNR report"
            self.exchange_log.append(f"73 {report} received from {callsign}; sending final 73.")
            self._schedule_contact_transmit()
        elif kind == "73" and self.auto_role == "caller":
            self._complete_contact()

    def _schedule_contact_transmit(self):
        generation, stage = self.auto_generation, self.contact_stage
        def send():
            if self.auto_armed and self.auto_generation == generation and self.contact_stage == stage:
                self.transmit_exchange()
        QTimer.singleShot(AUTO_REPLY_GUARD_MS, send)

    def _complete_contact(self):
        self.contact_stage = "idle"
        self._refresh_contact_stage_indicator()
        if self.active_qso_log is not None:
            self.active_qso_log["status"] = "Complete"
            self.active_qso_log["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._persist_active_qso_log()
            self.refresh_qso_log()
            self.active_qso_log = None
            self._active_qso_path = None
            self.duplicate_displayed_in_qso = False
            self.duplicate_preview_key = None
        QTimer.singleShot(400, self._ensure_session_receive)
        self.auto_peer = ""
        self.last_cq_snapshot = None
        self.exchange_log.append("Contact exchange complete; all stage cards are in the QSO deck.")
        if self.auto_armed and self.auto_role == "responder":
            self.stop_auto_exchange("Automatic reply complete; listening for another CQ.")
        if self.auto_armed and self.auto_role == "caller":
            self.contact_stage = "listen_cq"
            self._refresh_contact_stage_indicator()
            if self.call_cq_active:
                self.cq_timer.start(self.cq_interval.value() * 1000)
            self.auto_should_listen = True
            QTimer.singleShot(300, self.start_receive)

    def transmit_exchange(self, *, card_override=None):
        if os.environ.get("PIXELQSO_AUDIO_ROLE") == "websdr-rx":
            self.exchange_log.append("WebSDR window is receive-only.")
            return
        if not self.auto_armed:
            self.contact_stage = "send_" + self._outgoing_message_type()
            self._refresh_contact_stage_indicator()
        backend = self.selected_backend()
        if self.selected_adapter().capabilities.kind is AdapterKind.FRAME_TRANSPORT:
            if backend is None:
                QMessageBox.warning(self, "Data2G mode unavailable",
                                    "Connect to a Data2G host and select a usable broadcast mode first.")
                return
            self._prepare_data2g_transmission(backend, card_override=card_override)
            return
        if self.audio_test_mode.isChecked():
            self._prepare_test_transmission(card_override=card_override)
            return
        if not self.rig.connected():
            QMessageBox.warning(self, "CAT unavailable", "Connect to a Hamlib rigctld service before transmitting."); return
        self.tx_btn.setEnabled(False)
        generation = self.auto_generation if self.auto_armed else None
        self.read_rig(lambda ok: self._preflight_and_confirm_tx(ok, generation, card_override))

    def _prepare_data2g_transmission(self, backend, *, message_type=None, card_override=None,
                                     beacon=False):
        if self.data2g_tx_total or self.data2g_tx_waiting is not None:
            QMessageBox.information(self, "Data2G transmission in progress",
                                    "Wait for the queued host transmission to finish before sending another card.")
            return
        if not backend.usable:
            QMessageBox.warning(self, "Data2G mode unavailable",
                                backend.disabled_reason or "Reconnect to discover usable Data2G modes.")
            return
        if not self.data2g_session or not DATA2G_ADAPTER.is_connected(self.data2g_session):
            QMessageBox.warning(self, "Data2G host unavailable", "Connect to a Data2G host in Station settings before transmitting.")
            return
        if not self._confirm_data2g_ptt_recovery():
            if self.auto_armed:
                self.stop_auto_exchange(
                    "Automatic exchange stopped after a managed-host PTT failure; "
                    "verify the radio is unkeyed before transmitting again.")
            return
        try:
            card = card_override if card_override is not None else self._chosen_card()
            card.validate()
            station_call = self.station_call.text().strip().upper()
            if card.callsign.upper() != station_call:
                raise ValueError(
                    f"This card uses {card.callsign.upper() or 'no call sign'}, but the "
                    f"Data2G broadcast group is open for {station_call or 'no call sign'}. "
                    "Update the card or Station settings so both identities match.")
            message_type = message_type or self._outgoing_message_type()
            card = self._prepare_outgoing_card(card, message_type)
            self._remember_tx_card(card)
            wire_type = {"report73": "73", "final73": "73"}.get(message_type, message_type)
            # `report73` is serialized as wire stage `73`, but its optional
            # SNR field still carries the report when a local decoder supplied one.
            snr_db = self.tx_snr_db if message_type in {"exchange", "report73"} else None
            frames = DATA2G_ADAPTER.frame_card(
                card, backend.max_frame_bytes, wire_type, snr_db)
            repeats = self.repeat_count.value()
            self.data2g_ack_timeout_ms = max(
                DATA2G_ACK_TIMEOUT_MS,
                math.ceil((float(backend.mode.seconds_at_max) + 10.0) * 1000))
            # Mode selection can fail synchronously if the session has gone
            # stale or its bounded command queue is full. Do not publish TX
            # queue state until that prerequisite has been accepted.
            DATA2G_ADAPTER.select_mode(self.data2g_session, backend.mode_name)
            self.data2g_tx_frames = frames * repeats
            self.data2g_tx_waiting = None
            self.data2g_tx_sent = 0
            self.data2g_tx_total = len(self.data2g_tx_frames)
            self.tx_card = card
            self.tx_message_type = wire_type
            self.data2g_tx_waiting_frame = None
            self._begin_transmit_preview(Data2GTransmitPreview(card, len(frames), self.data2g_tx_total),
                                         backend.mode_name, message_type)
            if wire_type == "cq":
                self.last_cq_snapshot = self._local_card_snapshot(card, wire_type, snr_db)
            elif not beacon:
                self._begin_qso_log(self.auto_peer or "Unknown", "")
                self._append_qso_card("sent_cards", self._local_card_snapshot(card, wire_type, snr_db))
            self.tx_label = "Data2G host beacon" if beacon else "Data2G host card frames"
            self.stop_tx_btn.setEnabled(True)
            self.stop_tx_btn.setVisible(True)
            self.stop_tx_btn.setText("Stop transmission")
            self._update_active_send_control(True)
            self.refresh_tx_button()
            self.listen_indicator.setText("● DATA2G HOST TX")
            self.listen_indicator.setStyleSheet(status_label_style("warning"))
            self.rx_status.setText(
                f"Sending {'beacon ' if beacon else ''}frame 0/{self.data2g_tx_total} · "
                "ACKs confirm local host transmission only")
            self.exchange_log.append(
                f"Submitting {self.data2g_tx_total} Data2G application frames using "
                f"{backend.mode_name}{' as a beacon' if beacon else ''}; "
                "no remote receipt is implied by ACKMODE.")
            if self.on_air:
                self.on_air.emit("host_tx_start", {
                    "mode": backend.mode_name, "message_type": wire_type,
                    "frames": self.data2g_tx_total, "group": "PIXELQSO",
                    "reference": card_reference(card),
                    "timing_note": "application queue start; not RF onset",
                })
            self._data2g_send_next()
        except Exception as exc:
            self.refresh_tx_button()
            if self.auto_armed:
                self.stop_auto_exchange("Automatic exchange stopped: its selected card could not be prepared.")
            QMessageBox.warning(self, "Couldn't prepare Data2G card", str(exc))

    def _data2g_send_next(self):
        if not self.data2g_tx_frames or self.data2g_tx_waiting is not None:
            return
        if not self.data2g_session or not DATA2G_ADAPTER.is_connected(self.data2g_session):
            self._finish_data2g_transmission("Data2G host disconnected; remaining frames were not sent.")
            return
        self.data2g_tag = (self.data2g_tag + 1) & 0xffff
        tag = self.data2g_tag.to_bytes(2, "big")
        frame = self.data2g_tx_frames.pop(0)
        try:
            DATA2G_ADAPTER.submit_frame(self.data2g_session, tag, frame)
            self.data2g_tx_waiting = tag
            self.data2g_tx_waiting_frame = frame
            if isinstance(getattr(self, '_active_tx_preview', None), Data2GTransmitPreview):
                self._active_tx_preview.phase = ('Channel busy · waiting for host transmission'
                                                if self.data2g_channel_busy else 'Waiting for host transmission')
                self._render_transmit_preview(self._active_tx_preview.state())
            if self.on_air:
                self.on_air.emit("host_tx_frame_queued", {
                    "port": DATA2G_ADAPTER.group_port(self.data2g_session), "tag": tag.hex(),
                    "index": self.data2g_tx_sent + 1, "total": self.data2g_tx_total,
                    "bytes": len(frame), "sha256": hashlib.sha256(frame).hexdigest(),
                })
            timeout = (DATA2G_BUSY_ACK_TIMEOUT_MS if self.data2g_channel_busy
                       else self.data2g_ack_timeout_ms)
            self.data2g_ack_timer.start(timeout)
            self.rx_status.setText(
                "Data2G host is holding the queued frame while the channel is busy"
                if self.data2g_channel_busy else
                f"Data2G frame {self.data2g_tx_sent + 1}/{self.data2g_tx_total} queued · awaiting host ACK")
        except Exception as exc:
            self._finish_data2g_transmission(f"Data2G frame queue failed: {exc}")

    def _data2g_tx_ack(self, session, port, tag):
        if (session is not self.data2g_session or not self.data2g_session or
                port != DATA2G_ADAPTER.group_port(self.data2g_session) or
                self.data2g_tx_waiting is None or tag != self.data2g_tx_waiting):
            return
        self.data2g_ack_timer.stop()
        model = getattr(self, '_active_tx_preview', None)
        frame = getattr(self, 'data2g_tx_waiting_frame', None)
        if isinstance(model, Data2GTransmitPreview) and frame is not None:
            model.acknowledge(frame)
            self._render_transmit_preview(model.state())
        self.data2g_tx_waiting_frame = None
        self.data2g_tx_waiting = None
        self.data2g_tx_sent += 1
        if self.on_air:
            self.on_air.emit("host_tx_frame_ack", {
                "port": port, "tag": tag.hex(), "sent": self.data2g_tx_sent,
                "total": self.data2g_tx_total,
                "meaning": "local KISS ACKMODE completion; remote reception unconfirmed",
            })
        if self.data2g_tx_frames:
            self._data2g_send_next()
        else:
            self._finish_data2g_transmission(
                f"Data2G host sent {self.data2g_tx_sent} frames; ACKMODE is local send confirmation, with remote card receipt unconfirmed.",
                transmission_complete=True)

    def _finish_data2g_transmission(self, message, *, transmission_complete=False):
        self._end_transmit_preview(completed=transmission_complete)
        self.data2g_tx_waiting_frame = None
        self.data2g_ack_timer.stop()
        sent_count = self.data2g_tx_sent
        total_count = self.data2g_tx_total
        self.data2g_tx_frames = []
        self.data2g_tx_waiting = None
        self.data2g_tx_total = 0
        self.stop_tx_btn.setEnabled(False)
        self.stop_tx_btn.setVisible(False)
        self.stop_tx_btn.setText("Stop transmission")
        self._update_active_send_control(False)
        self._active_send_control = None
        self.refresh_tx_button()
        self.exchange_log.append(message)
        self.rx_status.setText(message)
        self._set_transmission_result(
            message, tone="positive" if transmission_complete else "warning")
        if self.on_air:
            self.on_air.emit("host_tx_finished", {
                "sent_frames": sent_count, "total_frames": total_count,
                "complete": bool(transmission_complete), "message": message,
            })
        if self.auto_armed:
            if not transmission_complete:
                self.stop_auto_exchange("Automatic host exchange stopped: " + message)
            else:
                sent_stage = self.contact_stage
                self.auto_wait_state = "peer_response" if sent_stage == "send_cq" else "listening"
                if sent_stage == "send_cq":
                    self.contact_stage = "listen_cq"
                    if self.call_cq_active:
                        self.cq_timer.start(self.cq_interval.value() * 1000)
                elif sent_stage == "send_exchange":
                    self.contact_stage = "await_report73"
                elif sent_stage == "send_report73":
                    self.contact_stage = "await_final73"
                elif sent_stage == "send_final73":
                    self._complete_contact()
                self._refresh_contact_stage_indicator()
                self.auto_should_listen = True
        if DATA2G_ADAPTER.is_connected(self.data2g_session):
            self.listen_indicator.setText("● DATA2G HOST RX")
            self.listen_indicator.setStyleSheet(status_label_style("positive"))

    def _stop_current_transmit(self):
        if self.data2g_tx_total:
            stopped = DATA2G_ADAPTER.request_stop(
                self.data2g_tx_frames, self.data2g_tx_waiting)
            status = (f"Data2G queue stopped · {stopped.queued_cancelled} queued frames cancelled" +
                      (" · in-flight host transmission may still complete." if stopped.in_flight_may_complete
                       else " · no frame is in flight."))
            self._finish_data2g_transmission(status)
            self.data2g_tx_total = 0
            return
        self._finish_tx("Transmit stopped by operator; releasing PTT.")

    def _data2g_ack_timeout(self):
        self._finish_data2g_transmission(
            ("Data2G host remained busy beyond the ACK wait limit; whether the frame transmitted is unknown. "
             "Remaining frames were not replayed." if self.data2g_channel_busy else
             "Data2G ACKMODE timed out; whether that frame transmitted is unknown. "
             "Remaining frames were not replayed."))

    def _prepare_test_transmission(self, card_override=None):
        if self.test_link_enabled:
            output_ready = True
        else:
            output_ready = bool(self.output_device.currentData())
        if not output_ready:
            QMessageBox.warning(self, "No audio output", "Select an audio output device in Station settings, or use the two-window launcher for its built-in local link.")
            return
        self.tx_btn.setEnabled(False)
        try:
            card = card_override if card_override is not None else self._chosen_card()
            self.tx_message_type = self._outgoing_message_type()
            card = self._prepare_outgoing_card(card, self.tx_message_type)
            self._remember_tx_card(card)
            if self.tx_message_type in {"exchange", "report73"} and self.reply_report:
                self.tx_snr_db = (self.reply_report.get("card") or {}).get("measured_snr_db")
            elif self.tx_message_type not in {"exchange", "report73"}:
                self.tx_snr_db = None
            if self.auto_armed and card.callsign.upper() != self.station_call.text().strip().upper():
                raise ValueError("The automatic exchange card must use the call sign configured in Station settings.")
            if self.auto_armed and self.selected_mode() == "fast_avatar":
                raise ValueError("Raw burst pixels have no correction or integrity check; use a checked-block mode for automatic exchange.")
            if self.auto_armed: self.auto_own_card_id = card.card_id
            audio = self._append_cwid_audio(self._card_tx_audio(card))
            duration = len(audio) / modem.SAMPLE_RATE
            self.auto_last_card_duration = duration
            self.tx_bytes = pcm16_audio_bytes(audio)
            self.tx_offset = 0
            self.tx_deadline = duration + 30.0
            self.tx_label = ("card burst" if self.selected_backend() is not None else "loopback card") + (
                " + CW ID" if self.tx_cwid_appended else "")
        except Exception as exc:
            self.refresh_tx_button()
            if self.auto_armed: self.stop_auto_exchange("Automatic exchange stopped: its selected card could not be prepared.")
            QMessageBox.warning(self, "Couldn't prepare card", str(exc))
            return
        self._start_tx_audio()

    def start_auto_exchange(self, *, reply_report=None):
        if os.environ.get("PIXELQSO_AUDIO_ROLE") == "websdr-rx":
            self.exchange_log.append("WebSDR window is receive-only; transmit manually in the TX window.")
            return
        if self.exchange_mode.currentIndex() != 1:
            QMessageBox.information(self, "Select automatic exchange", "Choose Automatic card exchange before starting.")
            return
        backend = self.selected_backend()
        host_mode = (self.selected_adapter().capabilities.kind is
                     AdapterKind.FRAME_TRANSPORT)
        if backend is None or (not host_mode and not backend.checked):
            QMessageBox.warning(self, "Choose resilient burst", "Automatic CQ exchanges require resilient burst cards so the message type and integrity checks are available.")
            return
        if host_mode and not getattr(backend, "usable", False):
            QMessageBox.warning(
                self, "Data2G mode unavailable",
                getattr(backend, "disabled_reason", None) or
                "Connect to a Data2G host and select a usable broadcast mode first.")
            return
        if host_mode and (not self.data2g_session or not DATA2G_ADAPTER.is_connected(self.data2g_session)):
            QMessageBox.warning(self, "Data2G host unavailable", "Connect to the Data2G host before starting an automatic exchange.")
            return
        if host_mode and not self._confirm_data2g_ptt_recovery():
            return
        link_ready = self.test_link_enabled and self.audio_test_mode.isChecked()
        devices_ready = bool(self.output_device.currentData() and self.input_device.currentData())
        if (not host_mode and ((not self.audio_test_mode.isChecked() and not self.rig.connected()) or
                (not link_ready and not devices_ready))):
            message = ("Select audio input and output devices in Station settings first."
                       if self.audio_test_mode.isChecked() else
                       "Connect CAT and select both an audio input and output before starting the automatic exchange.")
            QMessageBox.warning(self, "Station not ready", message)
            return
        self.auto_armed = True
        self.auto_generation += 1
        self.auto_consent = False
        self.auto_should_listen = False
        self.auto_peer = ""
        self.auto_peer_card_id = None
        self.auto_own_card_id = None
        self.auto_timeout_pending = False
        self.auto_live_handled_cards.clear()
        self.last_cq_snapshot = None
        self.auto_wait_state = "peer_response" if self.exchange_role.currentIndex() == 0 else "initial_card"
        self.active_qso_log = None
        self._active_qso_path = None
        self.auto_role = "caller" if self.exchange_role.currentIndex() == 0 else "responder"
        self.contact_stage = "send_cq" if self.auto_role == "caller" else "listen_cq"
        self._refresh_contact_stage_indicator()
        self.auto_start_btn.setEnabled(False)
        self.auto_stop_btn.setEnabled(True)
        self.auto_stop_btn.setVisible(True)
        for widget in (self.exchange_mode, self.exchange_role, self.mode_combo, self.frequency,
                       self.rig_host, self.rig_port, self.rig_button, self.rig_refresh, self.output_device,
                       self.input_device, self.station_call, self.station_grid, self.audio_test_mode):
            widget.setEnabled(False)
        self._update_exchange_controls()
        self._update_transfer_controls()
        self.exchange_log.append(f"Automatic {self.auto_role} flow armed.")
        if reply_report is not None:
            self._handle_contact_message(reply_report, initiated=True)
        elif self.auto_role == "caller": self.transmit_exchange()
        else: self.start_receive()

    def stop_auto_exchange(self, log_message="Automatic exchange stopped by operator."):
        self.cq_timer.stop()
        self.call_cq_active = False
        if hasattr(self, "call_cq_button"):
            with QSignalBlocker(self.call_cq_button): self.call_cq_button.setChecked(False)
            self.call_cq_button.setText("Call CQ")
        self.cq_interval.setEnabled(True)
        self.call_cq_button.setEnabled(True)
        self.auto_generation += 1
        self.auto_armed = False
        if self.data2g_tx_total:
            status = ("Automatic exchange stopped while a host frame was in flight; its transmission status is unknown."
                      if self.data2g_tx_waiting is not None else
                      "Automatic exchange stopped before the next host frame was submitted.")
            self._finish_data2g_transmission(status)
        self.auto_consent = False
        self.auto_should_listen = False
        self.auto_peer = ""
        self.auto_peer_card_id = None
        self.auto_own_card_id = None
        self.auto_wait_state = None
        self.auto_timeout_pending = False
        self.auto_receive_timer.stop()
        self.live_decode_inflight = False
        self.auto_start_btn.setEnabled(True)
        self.auto_stop_btn.setEnabled(False)
        self._update_exchange_controls()
        for widget in (self.exchange_mode, self.exchange_role, self.mode_combo, self.frequency,
                       self.rig_host, self.rig_port, self.rig_button, self.rig_refresh, self.output_device,
                       self.input_device, self.station_call, self.station_grid, self.audio_test_mode):
            widget.setEnabled(True)
        self._update_transfer_controls()
        if self.rx_timer.isActive():
            self.rx_session_id += 1
            self.active_live_decode_token = None
            self.rx_timer.stop()
            self._stop_local_receive_capture()
            self._clear_rx_bytes()
            self.test_rx_pending = {}; self.test_rx_expected = None
        if self.tx_timer.isActive():
            self._finish_tx(log_message)
        else:
            self.exchange_log.append(log_message)
            if self.ptt_active: self._release_ptt()
            else: self.refresh_tx_button()
        if self.active_qso_log is not None:
            self.active_qso_log["status"] = "Complete" if "complete" in log_message.lower() else "Stopped"
            self.active_qso_log["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if getattr(self, "_active_qso_path", None) is None:
                self._active_qso_path = self.qso_log_dir / f"qso-{time.strftime('%Y%m%d-%H%M%S')}-{id(self.active_qso_log) & 0xffff:04x}.json"
            self._persist_active_qso_log()
            self.refresh_qso_log()
            self.active_qso_log = None
            self._active_qso_path = None
            self.duplicate_displayed_in_qso = False
            self.duplicate_preview_key = None
        QTimer.singleShot(400, self._ensure_session_receive)

    def _preflight_and_confirm_tx(self, read_ok, generation=None, card_override=None):
        if generation is not None and (not self.auto_armed or generation != self.auto_generation):
            self.refresh_tx_button(); return
        if not read_ok or not self.rig.connected():
            self.refresh_tx_button()
            if self.auto_armed: self.stop_auto_exchange("Automatic exchange stopped: fresh CAT readback failed.")
            QMessageBox.warning(self, "Rig readback failed", "Could not freshly read the rig's frequency and mode. No transmission was started.")
            return
        if not self.rig_freq_read or not self.rig_mode:
            self.refresh_tx_button()
            if self.auto_armed: self.stop_auto_exchange("Automatic exchange stopped: rig frequency or mode is unavailable.")
            QMessageBox.warning(self, "Read the rig first", "Read the connected rig's frequency and mode before transmitting."); return
        if self.rig_mode not in {"USB", "USB-D", "PKTUSB", "DATA-U"}:
            self.refresh_tx_button()
            if self.auto_armed: self.stop_auto_exchange("Automatic exchange stopped: upper-sideband data mode is required.")
            QMessageBox.warning(self, "Select upper-sideband data mode", f"The rig reports {self.rig_mode}. Set the radio to USB or an upper-sideband data mode and read it again."); return
        try:
            card = card_override if card_override is not None else self._chosen_card()
            self.tx_message_type = self._outgoing_message_type()
            card = self._prepare_outgoing_card(card, self.tx_message_type)
            self._remember_tx_card(card)
            if self.tx_message_type in {"exchange", "report73"} and self.reply_report:
                self.tx_snr_db = (self.reply_report.get("card") or {}).get("measured_snr_db")
            elif self.tx_message_type not in {"exchange", "report73"}:
                self.tx_snr_db = None
            if self.auto_armed and card.callsign.upper() != self.station_call.text().strip().upper():
                raise ValueError("The automatic exchange card must use the call sign configured in Station settings.")
            if self.auto_armed: self.auto_own_card_id = card.card_id
            if self.auto_armed and self.selected_mode() == "fast_avatar":
                raise ValueError("Raw burst pixels have no correction or integrity check; use a checked-block mode for automatic exchange.")
            audio = self._append_cwid_audio(self._card_tx_audio(card))
            duration = len(audio) / modem.SAMPLE_RATE
            self.auto_last_card_duration = duration
            self.tx_bytes = pcm16_audio_bytes(audio)
            self.tx_offset = 0
            self.tx_deadline = duration + 8.0
            self.tx_label = ("card burst" if self.selected_backend() is not None else "legacy card") + (
                " + CW ID" if self.tx_cwid_appended else "")
        except Exception as exc:
            self.tx_btn.setEnabled(self.rig.connected())
            if self.auto_armed: self.stop_auto_exchange("Automatic exchange stopped: its selected card could not be prepared.")
            QMessageBox.warning(self, "Couldn't prepare card", str(exc)); return
        self._confirmed_frequency = self.frequency.value()
        self._confirmed_mode = self.rig_mode
        self.read_rig(lambda ok: self._confirm_tx_readback(ok, generation))

    def _confirm_tx_readback(self, read_ok, generation=None):
        if generation is not None and (not self.auto_armed or generation != self.auto_generation):
            self.tx_bytes = b""
            self.refresh_tx_button()
            return
        if (not read_ok or not self.rig.connected() or
                self.frequency.value() != self._confirmed_frequency or
                self.rig_mode != self._confirmed_mode):
            self.tx_bytes = b""
            self.refresh_tx_button()
            if self.auto_armed: self.stop_auto_exchange("Automatic exchange stopped because the rig changed after confirmation.")
            QMessageBox.warning(self, "Rig changed", "The rig frequency or mode changed after confirmation, or could not be read again. Review the radio and start over.")
            return
        self.rig.request("set_ptt 1", 1, lambda response: self._ptt_started(response, generation))

    def _ptt_started(self, response, generation=None):
        if self.tx_label == "one-shot card beacon" and not self.beacon_pending:
            if response == ["RPRT 0"]:
                self.rig.request("set_ptt 0", 1, lambda _result: self.refresh_tx_button())
            return
        if generation is not None and (not self.auto_armed or generation != self.auto_generation):
            if response == ["RPRT 0"]:
                def cancelled(_result):
                    self.exchange_log.append("Cancelled pending auto-TX PTT released.")
                    self.refresh_tx_button()
                self.rig.request("set_ptt 0", 1, cancelled)
            return
        if response != ["RPRT 0"]:
            if self.tx_label == "one-shot card beacon":
                self.beacon_pending = False
            self.refresh_tx_button()
            if self.auto_armed: self.stop_auto_exchange("Automatic exchange stopped because PTT could not be asserted.")
            QMessageBox.warning(self, "PTT failed", "Rig control did not key PTT: " + " ".join(response)); return
        self._pause_receive_for_tx()
        self.ptt_active = True
        if self.tx_message_type == "cq":
            self.last_cq_snapshot = self._local_card_snapshot(self.tx_card or self._chosen_card(), "cq", None)
        if self.tx_label in {"legacy card", "card burst"} and self.tx_message_type != "cq":
            try:
                card = self.tx_card or self._chosen_card()
                if self.auto_peer or self.tx_message_type != "cq":
                    self._begin_qso_log(self.auto_peer or "Unknown", "")
                    self._append_qso_card("sent_cards", self._local_card_snapshot(card, self.tx_message_type, self.tx_snr_db))
            except Exception as exc:
                self.exchange_log.append(f"Could not add transmitted card to QSO log: {exc}")
        if self.tx_label == "one-shot card beacon":
            self.beacon_pending = False
        QTimer.singleShot(350, self._start_tx_audio)

    def _start_tx_audio(self):
        if os.environ.get("PIXELQSO_AUDIO_ROLE") == "websdr-rx":
            self._finish_tx("Receive-only WebSDR window; transmission skipped.")
            return
        if not self.audio_test_mode.isChecked() and (not self.rig.connected() or not self.ptt_active):
            self._release_ptt(); return
        if self.audio_test_mode.isChecked():
            self._pause_receive_for_tx()
        if self.test_link_enabled and self.audio_test_mode.isChecked():
            if self.tx_message_type == "cq":
                self.last_cq_snapshot = self._local_card_snapshot(self.tx_card or self._chosen_card(), "cq", None)
            if self.tx_label in {"loopback card", "card burst", "legacy card"} and self.tx_message_type != "cq":
                card = self.tx_card or self._chosen_card()
                try:
                    self._begin_qso_log(self.auto_peer or "Unknown", "")
                    self._append_qso_card("sent_cards", self._local_card_snapshot(card, self.tx_message_type, self.tx_snr_db))
                except Exception as exc:
                    self.exchange_log.append(f"Could not add transmitted card to QSO log: {exc}")
            self.tx_started = time.monotonic()
            self.stop_tx_btn.setText("Stop audio")
            self.stop_tx_btn.setEnabled(True)
            self.stop_tx_btn.setVisible(True)
            self._update_active_send_control(True)
            self._refresh_operate_primary_action()
            self.tx_timer.start()
            self._begin_transmit_preview(getattr(self, '_prepared_tx_preview', None), self.selected_mode(), self.tx_message_type)
            self._record_on_air_tx()
            self.exchange_log.append(f"TX started: {self.tx_label}, local software audio link, {len(self.tx_bytes)//2} samples. CAT/PTT bypassed.")
            return
        if not self.output_device.currentData():
            self._release_ptt()
            QMessageBox.warning(self, "No audio output", "Select an available sound output device first."); return
        if self.tx_message_type == "cq":
            self.last_cq_snapshot = self._local_card_snapshot(self.tx_card or self._chosen_card(), "cq", None)
        fmt = QAudioFormat(); fmt.setSampleRate(modem.SAMPLE_RATE); fmt.setChannelCount(1); fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = self.output_device.currentData()
        if not device.isFormatSupported(fmt):
            self._release_ptt()
            QMessageBox.warning(self, "Audio format unsupported", "The selected output does not support 48 kHz mono 16-bit audio required by this prototype."); return
        self.tx_audio = QAudioSink(device, fmt, self)
        self.tx_audio.setBufferSize(48_000 * 2)
        self.tx_device = self.tx_audio.start()
        if self.tx_device is None:
            self._release_ptt()
            QMessageBox.warning(self, "Audio failed", "Could not open the selected audio output."); return
        self.tx_started = time.monotonic()
        self.stop_tx_btn.setEnabled(True)
        self.stop_tx_btn.setVisible(True)
        self._update_active_send_control(True)
        self._refresh_operate_primary_action()
        self.tx_timer.start()
        self._begin_transmit_preview(getattr(self, '_prepared_tx_preview', None), self.selected_mode(), self.tx_message_type)
        self._record_on_air_tx()
        self.stop_tx_btn.setText(
            "Stop audio" if self.audio_test_mode.isChecked() else "Stop transmission")
        self.exchange_log.append(f"TX started: {self.tx_label}, {len(self.tx_bytes)//2} samples. " +
                                 ("Audio-only test; PTT disabled." if self.audio_test_mode.isChecked() else "PTT asserted."))

    def _pump_tx(self):
        self._update_local_transmit_preview()
        if self.test_link_enabled and self.audio_test_mode.isChecked() and self.test_tx_socket is not None:
            if time.monotonic() - self.tx_started > self.tx_deadline:
                self._finish_tx("Transmit timed out; stopping local audio."); return
            if self.tx_offset < len(self.tx_bytes):
                count = min(960, len(self.tx_bytes) - self.tx_offset)
                count -= count % 2
                payload = self.tx_bytes[self.tx_offset:self.tx_offset + count]
                datagram = self.test_tx_sequence.to_bytes(4, "big") + payload
                written = self.test_tx_socket.writeDatagram(datagram, QHostAddress(QHostAddress.SpecialAddress.LocalHost), self.test_tx_port)
                if written < 0:
                    self.exchange_log.append("Local audio link failed: " + self.test_tx_socket.errorString())
                    self._finish_tx("Local audio link failed."); return
                self.test_tx_sequence += 1
                self.tx_offset += count
                self._update_local_transmit_preview()
            elif time.monotonic() - self.tx_started >= len(self.tx_bytes) / (2 * modem.SAMPLE_RATE):
                self.test_tx_socket.writeDatagram(
                    self.test_tx_sequence.to_bytes(4, "big") + b"EOF!",
                    QHostAddress(QHostAddress.SpecialAddress.LocalHost), self.test_tx_port)
                self._finish_tx("Audio playback complete.")
            return
        if not self.tx_audio or not self.tx_device: return
        if time.monotonic() - self.tx_started > self.tx_deadline:
            self._finish_tx("Transmit timed out; releasing PTT."); return
        expected_playback_us = (len(self.tx_bytes) // 2) * 1_000_000 / modem.SAMPLE_RATE
        if self.tx_offset < len(self.tx_bytes):
            count = min(max(0, self.tx_audio.bytesFree()), 8192, len(self.tx_bytes) - self.tx_offset)
            if count:
                count -= count % 2
                written = self.tx_device.write(self.tx_bytes[self.tx_offset:self.tx_offset+count])
                if written > 0: self.tx_offset += written
        elif self.tx_audio.processedUSecs() >= expected_playback_us:
            self._finish_tx("Transmit complete; releasing PTT.")

    def _finish_tx(self, message):
        self._update_local_transmit_preview(force=True)
        self._end_transmit_preview(completed=message in {'Transmit complete; releasing PTT.', 'Audio playback complete.'})
        if self.on_air and self.on_air_tx_id:
            self.on_air.emit("tx_end", {"tx_event_id": self.on_air_tx_id, "message": message,
                                       "submitted_samples": self.tx_offset // 2})
            self.on_air_tx_id = None
        self.tx_timer.stop()
        if self.tx_audio: self.tx_audio.stop()
        self.tx_audio = None; self.tx_device = None; self.tx_bytes = b""
        if self.tx_label == "one-shot card beacon":
            self.beacon_pending = False
        self.stop_tx_btn.setEnabled(False)
        self.stop_tx_btn.setVisible(False)
        self.stop_tx_btn.setText("Stop transmission")
        self._update_active_send_control(False)
        self._active_send_control = None
        self.exchange_log.append(f"{self.tx_label}: {message}")
        completed = message in {"Transmit complete; releasing PTT.", "Audio playback complete."}
        result = ("Transmission audio completed locally · remote card receipt is unconfirmed."
                  if completed else message)
        self._set_transmission_result(result, tone="positive" if completed else "warning")
        if self.auto_armed:
            complete = message in {"Transmit complete; releasing PTT.", "Audio playback complete."}
            if complete:
                sent_stage = self.contact_stage
                self.auto_wait_state = "peer_response" if sent_stage == "send_cq" else "listening"
                if sent_stage == "send_cq":
                    self.contact_stage = "listen_cq"
                    self._refresh_contact_stage_indicator()
                    if self.call_cq_active:
                        self.cq_timer.start(self.cq_interval.value() * 1000)
                elif sent_stage == "send_exchange":
                    self.contact_stage = "await_report73"
                    self._refresh_contact_stage_indicator()
                elif sent_stage == "send_report73":
                    self.contact_stage = "await_final73"
                    self._refresh_contact_stage_indicator()
                elif sent_stage == "send_final73": self._complete_contact()
                self.auto_should_listen = True
            else:
                self.stop_auto_exchange("Automatic exchange stopped because transmission did not complete.")
        elif message in {"Transmit complete; releasing PTT.", "Audio playback complete."} and self.contact_stage == "send_final73":
            self._complete_contact()
        if message in {"Transmit complete; releasing PTT.", "Audio playback complete."}:
            if self.tx_quick_draw_card is not None:
                try:
                    self._save_quick_draw_to_sent_library(self.tx_quick_draw_card)
                except OSError as exc:
                    self.exchange_log.append(f"Couldn't save sent quick draw to My Cards: {exc}")
        self.tx_card = None
        self.tx_quick_draw_card = None
        self._refresh_quick_draw_status()
        self._release_ptt()

    def _set_transmission_result(self, message, *, tone="muted"):
        label = getattr(self, "tx_result_status", None)
        if label is None:
            return
        label.setText(str(message))
        label.setAccessibleDescription(str(message))
        label.setStyleSheet(semantic_text_style(tone, padding="4px"))
        label.setVisible(bool(message))

    def _release_ptt(self):
        if self.audio_test_mode.isChecked():
            self.ptt_active = False
            self.refresh_tx_button()
            self._continue_auto_receive()
            return
        if self.rig.connected() and self.ptt_active:
            self.ptt_active = False
            self.rig.request("set_ptt 0", 1, self._ptt_released)
        else:
            self.refresh_tx_button()
            self._continue_auto_receive()

    def _ptt_released(self, response):
        self.refresh_tx_button()
        self.exchange_log.append("PTT released." if response == ["RPRT 0"] else "PTT release response: " + " ".join(response))
        self._continue_auto_receive()

    def _continue_auto_receive(self):
        if self.auto_armed and self.auto_should_listen:
            self.auto_should_listen = False
            QTimer.singleShot(300, self.start_receive)
        elif not self.auto_armed:
            QTimer.singleShot(300, self._ensure_session_receive)

    def _auto_receive_timeout(self):
        if not self.auto_armed or self.auto_wait_state != "peer_response":
            return
        if not self.rx_timer.isActive():
            return
        self.auto_timeout_pending = True
        self.exchange_log.append("Response window ended; checking the capture for a complete card.")
        self.stop_receive()

    def _toggle_beacon(self, enabled):
        self.beacon_interval.setEnabled(not enabled)
        self.beacon_button.setText("Stop beacon" if enabled else "Start beacon")
        self._update_beacon_help()
        if enabled:
            self.beacon_timer.start(self.beacon_interval.value() * 60 * 1000)
            self.beacon_status_timer.start()
            self._refresh_beacon_status()
            self.transmit_beacon()
        else:
            self.beacon_timer.stop()
            self.beacon_status_timer.stop()
            self.beacon_pending = False
            self._refresh_beacon_status()
            self.exchange_log.append("Periodic beacon stopped.")

    def _refresh_beacon_status(self):
        if not hasattr(self, "beacon_status"):
            return
        if not self.beacon_button.isChecked():
            self.beacon_status.setText("Beacon is off.")
            return
        remaining_ms = self.beacon_timer.remainingTime()
        if remaining_ms < 0:
            self.beacon_status.setText("Beacon active · waiting to schedule next attempt.")
            return
        remaining_seconds = max(0, math.ceil(remaining_ms / 1000))
        minutes, seconds = divmod(remaining_seconds, 60)
        self.beacon_status.setText(
            f"Beacon active · next attempt in {minutes} min {seconds:02d} s")

    def _update_beacon_help(self):
        if not hasattr(self, "beacon_button"):
            return
        if self.beacon_button.isChecked():
            message = ("Stop future beacon repeats. A card transmission already in progress "
                       "may still finish.")
            self.beacon_button.setAccessibleName("Stop card beacon")
        else:
            message = (
                "Send one card immediately at the current radio or Data2G host frequency, "
                f"then repeat every {self.beacon_interval.value()} minutes until stopped. "
                "A scheduled repeat can be skipped during an automatic exchange or while "
                "another transmission is in progress.")
            self.beacon_button.setAccessibleName("Start card beacon")
        self.beacon_button.setToolTip(message)
        self.beacon_button.setAccessibleDescription(message)

    def transmit_beacon(self):
        """Send the card at the current frequency, skipping a tick while busy."""
        if (self.tx_timer.isActive() or self.ptt_active or self.data2g_tx_total or
                getattr(self, "beacon_pending", False) or self.auto_armed):
            return
        if os.environ.get("PIXELQSO_AUDIO_ROLE") == "websdr-rx":
            return
        if self.selected_adapter().capabilities.kind is AdapterKind.FRAME_TRANSPORT:
            backend = self.selected_backend()
            if backend is None:
                QMessageBox.warning(self, "Data2G mode unavailable",
                                    "Connect to a Data2G host and select a usable broadcast mode first.")
                return
            self.update_identity()
            self.tx_snr_db = None
            self._prepare_data2g_transmission(
                backend, message_type="card", card_override=self._chosen_card(), beacon=True)
            return
        if not self.rig.connected():
            QMessageBox.warning(self, "CAT unavailable", "Connect to rigctld to send a beacon at the current radio frequency.")
            return
        self.beacon_pending = True
        self.tx_message_type = "card"
        self.tx_snr_db = None
        self.tx_btn.setEnabled(False)
        self.read_rig(self._preflight_and_confirm_beacon)

    def _preflight_and_confirm_beacon(self, read_ok):
        if not self.beacon_pending:
            return
        if not read_ok or not self.rig.connected() or not self.rig_freq_read or not self.rig_mode:
            self.beacon_pending = False
            self.refresh_tx_button()
            QMessageBox.warning(self, "Rig readback failed", "Could not freshly read the rig frequency and mode. No beacon was started.")
            return
        actual_frequency = self.frequency.value()
        if self.rig_mode not in {"USB", "USB-D", "PKTUSB", "DATA-U"}:
            self.beacon_pending = False
            self.refresh_tx_button()
            QMessageBox.warning(self, "Select upper-sideband data mode", f"The rig reports {self.rig_mode}. Set USB or an upper-sideband data mode and try again.")
            return
        try:
            self.update_identity()
            card = self._chosen_card()
            self._remember_tx_card(card)
            audio = self._append_cwid_audio(self._card_tx_audio(card, beacon=True))
            self.tx_bytes = pcm16_audio_bytes(audio)
            self.tx_offset = 0
            self.tx_deadline = len(audio) / modem.SAMPLE_RATE + 8.0
            self.tx_label = "one-shot card beacon" + (
                " + CW ID" if self.tx_cwid_appended else "")
        except Exception as exc:
            self.tx_bytes = b""
            self.beacon_pending = False
            self.refresh_tx_button()
            QMessageBox.warning(self, "Couldn't prepare beacon", str(exc))
            return
        duration = len(audio) / modem.SAMPLE_RATE
        self._confirmed_frequency = actual_frequency
        self._confirmed_mode = self.rig_mode
        self.read_rig(lambda ok: self._confirm_beacon_readback(ok, actual_frequency, self.rig_mode))

    def _confirm_beacon_readback(self, read_ok, frequency, mode):
        if not self.beacon_pending:
            return
        if (not read_ok or not self.rig.connected() or abs(self.frequency.value()-frequency) > 10
                or self.rig_mode != mode or self.frequency.value() != self._confirmed_frequency
                or self.rig_mode != self._confirmed_mode):
            self.tx_bytes = b""
            self.beacon_pending = False
            self.refresh_tx_button()
            QMessageBox.warning(self, "Rig changed", "The rig frequency or mode changed after confirmation. Review the radio and start over.")
            return
        self.rig.request("set_ptt 1", 1, self._ptt_started)

    def choose_evidence_file(self):
        name, _ = QFileDialog.getSaveFileName(self, "Choose receive evidence file", self.combine.text(), "Evidence session (*.json)")
        if name:
            self.combine.setText(name)
            self.rx_status.setText(f"Passes will combine in {Path(name).name}.")

    def decode_wav(self):
        name, _ = QFileDialog.getOpenFileName(self, "Open received WAV", str(DATA), "WAV audio (*.wav)")
        if not name: return
        self.start_decode(Path(name))

    def start_decode(self, path, profile="auto"):
        self.rx_status.setText(f"Decoding {path.name} in the background…")
        self.decode_jobs += 1
        worker = DecodeWorker(path, Path(self.combine.text()), profile, self.receive_mode(),
                              self.selected_audio_placement(), self.selected_mode(),
                              self.show_experimental_modes.isChecked())
        worker.signals.finished.connect(self.decode_finished)
        self.decode_pool.start(worker)

    def decode_finished(self, result):
        self.decode_jobs = max(0, self.decode_jobs - 1)
        name, report, error = result
        capture_path = Path(name).resolve()
        auto_capture = capture_path in self.auto_decode_paths
        self.auto_decode_paths.discard(capture_path)
        if error:
            error_text = error.message if isinstance(error, DecodeFailure) else str(error)
            self.rx_status.setText("Decode failed: " + error_text)
            if auto_capture and self.auto_armed:
                if self.auto_timeout_pending:
                    self._resolve_auto_receive_timeout()
                    return
                self.exchange_log.append("Receive decode failed; no reply sent. Resuming receive.")
                QTimer.singleShot(300, self.start_receive)
            return
        try:
            report, _improved = self._merge_live_preview(report)
        except ValueError as exc:
            self.rx_status.setText("Capture conflicts with existing verified image data: " + str(exc))
            return
        result_path = DATA / "qsl" / (Path(name).stem + ".json")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(report, indent=2))
        image = self.card_image(report)
        if image is not None:
            card = report.get("card") or {}
            self._show_receive_preview(image, card=card, exact=bool(card.get("exact")),
                                       coverage=card.get("pixel_coverage"),
                                       verified_coverage=card.get("verified_pixel_coverage"),
                                       report=report)
        card = report.get("card") or {}
        is_avatar = bool(card.get("raw_avatar") or card.get("avatar_burst"))
        verified_complete = self._assembly_verified(report)
        if card.get("raw_avatar"):
            if card.get("received_blocks") is not None:
                checked = sum(bool(value) for value in card["received_blocks"])
                total_blocks = len(card.get("received_blocks") or [])
                quality = "all blocks verified" if card.get("exact") else f"{checked}/{total_blocks} verified blocks; missing blocks remain blank"
                self.rx_status.setText(f"8-FSK RS burst · {quality} · saved {result_path.name}")
            else:
                self.rx_status.setText(f"8-FSK raw burst · identity decoded, pixels unverified · saved {result_path.name}")
        card_key = received_card_key(report)
        handled_live = auto_capture and card_key in self.auto_live_handled_cards
        already_saved = False
        duplicate_saved = False
        if handled_live:
            self.auto_live_handled_cards.discard(card_key)
        else:
            already_saved = verified_complete and card_key in self.seen_received_cards
            if verified_complete and card.get("callsign") and card.get("card_id") is not None:
                self.seen_received_cards.add(card_key)
            if verified_complete and not already_saved:
                self._add_session_wall_card(report)
                self._record_received_card(report)
            elif verified_complete and already_saved:
                duplicate_saved = True
        received_profile = report.get("receive_profile")
        profile_label = (get_backend(received_profile).label if is_card_backend(received_profile) else
                         "Data2G" if str(received_profile).startswith("data2g:") else
                         "Card burst" if received_profile == modem.MINIMAL_AVATAR_PROFILE.key else
                         modem.resolve_profile(received_profile).label.split(" · ")[0]
                         if received_profile else "profile unknown")
        if not card.get("raw_avatar"):
            if card.get("avatar_burst"):
                self.rx_status.setText(f"{card.get('callsign') or 'Unknown station'} · {card.get('color_stage')} · saved {result_path.name}")
            else:
                self.rx_status.setText(f"{len(report.get('valid_packets', []))} packets · {card.get('callsign') or 'unknown station'} · {profile_label} · saved {result_path.name}")
        if duplicate_saved:
            self._show_duplicate_card(report)
        if auto_capture and not handled_live and not already_saved:
            if is_avatar and verified_complete:
                self._handle_contact_message(report)
            elif not is_avatar:
                self._handle_auto_received_card(report)

    def _handle_auto_received_card(self, report):
        if not self.auto_armed: return
        card_meta = report.get("card") or {}
        fresh = report.get("fresh_packets", [])
        valid_current_ids = {int(packet.get("card_id", -1)) for packet in fresh
                             if int(packet.get("type", -1)) == modem.TYPE_ID}
        valid_current_ids.update(int(value) for value in report.get("fresh_identity_evidence_card_ids", []))
        complete_current_ids = {int(packet.get("card_id", -1)) for packet in fresh
                                if int(packet.get("type", -1)) == modem.TYPE_END}
        callsign = str(card_meta.get("callsign", "")).strip().upper()
        own_call = self.call.text().strip().upper()
        if card_meta.get("avatar_burst"):
            fresh_identity = card_meta.get("card_id") is not None
            fresh_complete = self._assembly_verified(report)
        else:
            fresh_identity = card_meta.get("card_id") in valid_current_ids
            fresh_complete = card_meta.get("card_id") in complete_current_ids
        if (not fresh_identity or not fresh_complete or not self._assembly_verified(report)
                or not callsign or callsign == own_call):
            if self.auto_timeout_pending:
                self._resolve_auto_receive_timeout()
                return
            self.auto_should_listen = True
            QTimer.singleShot(300, self.start_receive)
            return
        if self.auto_peer and callsign != self.auto_peer:
            self.exchange_log.append(f"Ignoring {callsign}: auto QSO is already paired with {self.auto_peer}. No transmission sent.")
            self.auto_should_listen = True
            QTimer.singleShot(300, self.start_receive)
            return
        if not self.auto_peer:
            self.auto_peer = callsign
            self.auto_peer_card_id = card_meta.get("card_id")
        if self.auto_role == "initiator":
            self.exchange_log.append(f"Received and saved {callsign}'s verified card. Listening for new images; no receipt ACK sent.")
            self.auto_wait_state = "listening"
            self.auto_should_listen = True
            QTimer.singleShot(300, self.start_receive)
            return
        self.exchange_log.append(f"Received and saved {callsign}'s verified card; sending this station's repeated card burst once.")
        QTimer.singleShot(0, self.transmit_exchange)

    def _resolve_auto_receive_timeout(self):
        self.auto_timeout_pending = False
        self.auto_receive_timer.stop()
        if not self.auto_armed:
            return
        self.exchange_log.append("No complete new image was received in this interval; returning to continuous receive.")
        self.auto_wait_state = "listening"
        QTimer.singleShot(300, self.start_receive)

    def _clear_rx_bytes(self):
        origin = getattr(self, "rx_capture_start_utc", None)
        if origin is not None:
            self.rx_capture_start_utc = origin + (
                getattr(self, "rx_capture_sample_offset", 0) + len(self.rx_bytes)//2
            ) / getattr(self, "rx_rate", modem.SAMPLE_RATE)
        self.rx_bytes.clear()
        self.rx_capture_sample_offset = 0

    def start_receive(self, quiet=False):
        if self.selected_adapter().capabilities.kind is AdapterKind.FRAME_TRANSPORT:
            self._ensure_session_receive()
            return
        if self.rx_timer.isActive():
            return
        self.rx_session_id += 1
        self.active_live_decode_token = None
        self.live_decode_inflight = False
        self.active_receive_identity = None
        self.completed_receive_events.clear()
        if self.ptt_active or self.tx_audio is not None or self.tx_timer.isActive():
            if not quiet:
                QMessageBox.warning(self, "Transmission is active", "Wait for PTT to be released before starting receive.")
            return

        def receive_error(title, message):
            self.listen_indicator.setText("● NOT LISTENING")
            self.listen_indicator.setStyleSheet(status_label_style("warning"))
            self.rx_status.setText(message)
            if not quiet:
                QMessageBox.warning(self, title, message)
            if self.auto_armed:
                self.stop_auto_exchange("Automatic exchange stopped because receive could not start.")

        self.rx_rate = modem.SAMPLE_RATE
        test_mode = self.test_link_enabled and self.audio_test_mode.isChecked()
        audio_format = QAudioFormat()
        audio_format.setSampleRate(modem.SAMPLE_RATE)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        adapter = self.selected_adapter()
        try:
            handle = adapter.start_receive(
                device=None if test_mode else self.input_device.currentData(),
                audio_format=audio_format, parent=self,
                audio_source_factory=QAudioSource,
                test_port=self.test_rx_port if test_mode else None,
                test_socket_factory=lambda parent: QUdpSocket(parent),
                test_bind=lambda sock, port: sock.bind(
                    QHostAddress(QHostAddress.SpecialAddress.LocalHost), port))
        except ReceiveStartError as exc:
            receive_error(exc.title, exc.message)
            return
        # This is a startup estimate, not a hardware ADC timestamp. UDP carries
        # no capture timestamps, so its receiver deliberately has no UTC prior.
        capture_start_utc = None if test_mode else time.time()
        self.local_receive_handle = handle
        self.local_receive_adapter = adapter
        self.rx_audio = handle.audio_source
        self.rx_device = handle.audio_stream
        self.test_rx_socket = handle.test_socket
        if test_mode:
            self.test_rx_expected = None
            self.test_rx_pending = {}
        self.card_assembly.begin_receive_window()
        self.copy_capture_cache = self.copy_capture_cache.next_window()
        self.rx_capture_sample_offset = 0
        self._clear_exact_live_preview()
        self._clear_rx_bytes()
        self.rx_capture_start_utc = capture_start_utc
        self.rx_waterfall.reset(self.input_device.currentText()
                                if self.rx_device else "UDP test audio", self.rx_rate)
        self.rx_live_profile = None if self.receive_mode() == "auto" else self.selected_profile() if (self.test_link_enabled or self.selected_backend() is not None) else None
        self.live_decode_inflight = False; self.live_decode_samples = 0; self.rx_timer.start()
        if self.on_air:
            self._flush_on_air_rx()
            self.on_air_rx_samples = 0
            self.on_air_rx_stream = self.on_air.emit("rx_start", self._on_air_settings())
        self.auto_receive_timer.stop()
        self.listen_indicator.setText("● LISTENING")
        self.listen_indicator.setStyleSheet(status_label_style("positive"))
        snapshot = self.card_assembly.preview_snapshot()
        if snapshot and not snapshot.card.get("exact"):
            preview = {"card": {**snapshot.card,
                                 "pixel_coverage": snapshot.coverage},
                       "pixels": snapshot.pixels}
            image = self.card_image(preview)
            if image is not None:
                self._show_receive_preview(image, card=snapshot.card, coverage=snapshot.coverage,
                                           verified_coverage=snapshot.verified_pixels,
                                           report={"card": dict(snapshot.card), "pixels": list(snapshot.pixels),
                                                   "receive_profile": self.receive_mode()})
        elif not snapshot:
            self._hide_receive_preview()
        self.receive_stack.setCurrentWidget(self.session_wall_page)
        self.rx_status.setText(
            f"Local two-window audio link ready · RX mode: {self._receive_mode_label()}" if self.test_rx_socket else
            self._listening_status())

    def _drain_rx(self):
        previous_samples = len(self.rx_bytes)
        if self.rx_device and self.rx_device.bytesAvailable():
            self.rx_bytes.extend(bytes(self.rx_device.readAll()))
        if self.test_rx_socket is not None:
            while self.test_rx_socket.hasPendingDatagrams():
                packet, _host, _port = self.test_rx_socket.readDatagram(self.test_rx_socket.pendingDatagramSize())
                data = bytes(packet)
                if len(data) < 4: continue
                sequence = int.from_bytes(data[:4], "big")
                if data[4:] == b"EOF!":
                    self.test_rx_final_pending = True
                    continue
                self.test_rx_pending[sequence] = data[4:]
                if self.test_rx_expected is None: self.test_rx_expected = sequence
            while self.test_rx_pending:
                if self.test_rx_expected in self.test_rx_pending:
                    self.rx_bytes.extend(self.test_rx_pending.pop(self.test_rx_expected))
                    self.test_rx_expected += 1
                else:
                    next_sequence = min(self.test_rx_pending)
                    if next_sequence > self.test_rx_expected and next_sequence - self.test_rx_expected <= 20:
                        self.rx_bytes.extend(b"\0" * 960)
                        self.test_rx_expected += 1
                    else:
                        self.test_rx_expected = next_sequence
        if len(self.rx_bytes) > previous_samples:
            self.rx_waterfall.feed_pcm16(bytes(self.rx_bytes[previous_samples:]))
        if self.on_air and len(self.rx_bytes) > previous_samples:
            self.on_air_rx_buffer.extend(self.rx_bytes[previous_samples:])
            chunk_bytes = self.rx_rate * 6 * 2
            while len(self.on_air_rx_buffer) >= chunk_bytes:
                self._flush_on_air_rx(chunk_bytes)
        # Keep receive memory bounded during long quiet sessions. The window
        # fits a full 64x64/32-color burst at 25 baud (301.52 seconds),
        # with room for leading silence, and is cleared after a card.
        max_rx_bytes = self.rx_rate * 360 * 2
        if len(self.rx_bytes) > max_rx_bytes:
            trim_bytes = len(self.rx_bytes) - max_rx_bytes
            trim_bytes -= trim_bytes % 2
            del self.rx_bytes[:trim_bytes]
            self.rx_capture_sample_offset += trim_bytes//2
            self.live_decode_samples = max(0, self.live_decode_samples - trim_bytes // 2)
        if not self.rx_timer.isActive() or self.live_decode_inflight:
            return
        sample_count = len(self.rx_bytes) // 2
        if self.receive_mode() == "auto":
            minimum_samples = self.rx_rate
            # Header acquisition scans only newly arrived audio, but still
            # performs several FFT banks. Batch live receive-all snapshots so
            # the scanner cannot monopolize the audio/UI thread pool.
            interval = 5 * self.rx_rate
        elif self.selected_backend() is not None:
            backend = self.selected_backend()
            minimum_samples = int(backend.minimum_audio_seconds() * self.rx_rate)
            interval = max(1, int((1.0 if self.selected_adapter().capabilities.kind is
                                   AdapterKind.FRAME_TRANSPORT else 0.5) * self.rx_rate))
        else:
            minimum_samples = int(1.0 * self.rx_rate)
            interval = 2 * self.rx_rate
        final_snapshot = self.test_rx_final_pending
        if sample_count < minimum_samples or (not final_snapshot and sample_count - self.live_decode_samples < interval):
            return
        self.live_decode_samples = sample_count
        self.live_decode_final = final_snapshot
        self.test_rx_final_pending = False
        snapshot = bytes(self.rx_bytes)
        if self.on_air:
            self.on_air_decode_pcm = snapshot
            self.on_air_decode_stream = self.on_air_rx_stream
            self.on_air_decode_end_sample = self.on_air_rx_samples + len(self.on_air_rx_buffer) // 2
        self.live_decode_token += 1
        token = self.live_decode_token
        generation = (self.auto_generation, self.rx_session_id, token)
        self.active_live_decode_token = token
        worker = LiveDecodeWorker(snapshot, self.rx_rate, self.rx_live_profile or "auto", generation,
                                  self.receive_mode(), self.selected_audio_placement(), self.rx_preferred_mode,
                                  self.show_experimental_modes.isChecked(),
                                  copy_cache=self.copy_capture_cache,
                                  capture_start_sample=self.rx_capture_sample_offset,
                                  capture_start_utc=(None if getattr(self, "rx_capture_start_utc", None) is None else
                                      self.rx_capture_start_utc + self.rx_capture_sample_offset/self.rx_rate),
                                  receive_session_id=self.rx_session_id)
        # QThreadPool owns the C++ QRunnable while queued, but the Python
        # wrapper owns its QObject signals. Keep that wrapper alive until the
        # finished callback has been delivered, including across session pivots.
        self.live_decode_workers[token] = worker
        worker.signals.finished.connect(self._live_decode_finished)
        worker.signals.preview.connect(self._live_decode_preview)
        self.live_decode_inflight = True
        self.decode_pool.start(worker)

    def _merge_live_preview(self, report):
        snapshot, improved = self.card_assembly.merge_preview(report)
        self._update_receive_metadata(snapshot)
        return snapshot, improved

    def _report_receive_conflict(self, report, error):
        """Surface rejected evidence while keeping repeated scan callbacks bounded."""
        card = (report or {}).get("card") or {}
        identity = self._report_receive_identity(report or {})
        signature = (self.rx_session_id, repr(identity), str(error))
        if signature not in self.rx_conflict_signatures:
            self.rx_conflict_signatures.add(signature)
            self.rx_conflict_signature_order.append(signature)
            if len(self.rx_conflict_signature_order) > 64:
                expired = self.rx_conflict_signature_order.pop(0)
                self.rx_conflict_signatures.discard(expired)
            mode = self._receive_mode_label((report or {}).get("receive_profile"))
            station = card.get("callsign") or "unknown station"
            image_id = card.get("image_id", card.get("card_id"))
            image = f", image {image_id}" if image_id is not None else ""
            self.exchange_log.append(
                f"Rejected conflicting RX evidence from {station}{image} ({mode}): {error}")
        self.rx_status.setText(
            "Conflicting verified image data rejected; listening for another copy")

    def _live_decode_preview(self, result):
        generation, report = result
        if not self._live_decode_generation_current(generation):
            return
        if isinstance(report, dict):
            self._follow_detected_rx_mode(report.get("receive_profile"))
        if report and report.get("receive_activity"):
            identity = report.get("receive_identity")
            pivot = bool(identity is not None and identity != self.active_receive_identity)
            if identity is not None:
                self.active_receive_identity = identity
            self._show_receive_activity(report["receive_activity"],
                                        pivot=pivot, report=report)
            if pivot or not self.receive_activity_timeout.isActive():
                self.receive_activity_timeout.start(12000)
            return
        try:
            report, improved = self._merge_live_preview(report)
        except ValueError as exc:
            self._report_receive_conflict(report, exc)
            return
        if improved:
            card = report["card"]
            self.active_receive_identity = self._report_receive_identity(report)
            self._show_receive_preview(self.card_image(report), card=card,
                coverage=card.get("pixel_coverage"),
                verified_coverage=card.get("verified_pixel_coverage"), report=report)
            self.rx_status.setText(
                f"Receiving card from {card.get('callsign') or 'a station'} · "
                f"{card.get('color_stage') or 'identity received'} · "
                f"RX mode: {self._receive_mode_label(report.get('receive_profile'))} · checking other modes")

    def _live_decode_generation_current(self, generation):
        if isinstance(generation, tuple) and len(generation) == 3:
            auto_generation, session_id, token = generation
            return (auto_generation == self.auto_generation and
                    session_id == self.rx_session_id and
                    token == self.active_live_decode_token and
                    self.rx_timer.isActive())
        return generation == self.auto_generation and self.rx_timer.isActive()

    @staticmethod
    def _report_receive_identity(report):
        header = report.get("copy_header") or {}
        if header.get("content_id"):
            return (int(header.get("receive_session_id", 0)),
                    header.get("announced_mode"), header["content_id"])
        card = report.get("card") or {}
        return (str(card.get("callsign", "")).upper(),
                str(card.get("grid", "")).upper(),
                card.get("image_id", card.get("card_id")))

    @staticmethod
    def _assembly_verified(report):
        event = report.get("assembly_event") or {}
        return bool(event.get("verified_complete"))

    def _live_decode_finished(self, result):
        generation, report, complete, error, stats = result
        if isinstance(generation, tuple) and len(generation) == 3:
            self.live_decode_workers.pop(generation[2], None)
        if not self._live_decode_generation_current(generation):
            return
        self.live_decode_inflight = False
        self.active_live_decode_token = None
        if report and report.get("receive_profile"):
            self._follow_detected_rx_mode(report["receive_profile"])
        error_text = (error.message if isinstance(error, DecodeFailure) else
                      str(error) if error else None)
        on_air_details = None
        if self.on_air:
            pcm = self.on_air_decode_pcm or b""
            decoder_stats = dict(stats)
            if isinstance(error, DecodeFailure):
                decoder_stats["failure_kind"] = error.kind.value
            on_air_details = {"report": report, "complete": False, "error": error_text,
                              "rx_stream_id": self.on_air_decode_stream,
                              "sample_start": self.on_air_decode_end_sample - len(pcm) // 2,
                              "sample_end": self.on_air_decode_end_sample,
                              "decoder": decoder_stats,
                              "reference": decoded_reference(report) if report else None}
        if error:
            if self.on_air and on_air_details is not None:
                self.on_air.emit("rx_decode", on_air_details)
                self.on_air_decode_pcm = None
            if (isinstance(error, DecodeFailure) and
                    error.kind is DecodeFailureKind.BACKEND_ERROR):
                self.rx_status.setText("Receive decoder error: " + error_text)
                self.exchange_log.append("Receive decoder error: " + error_text)
            else:
                self.rx_status.setText(
                    f"Listening · RX mode: {self._receive_mode_label()} · no complete card decoded yet")
            QTimer.singleShot(0, self._drain_rx)
            return
        if report and report.get("receive_activity"):
            identity = report.get("receive_identity")
            pivot = bool(identity is not None and identity != self.active_receive_identity)
            if identity is not None:
                self.active_receive_identity = identity
            self._show_receive_activity(report["receive_activity"],
                                        pivot=pivot, report=report)
            if pivot or not self.receive_activity_timeout.isActive():
                self.receive_activity_timeout.start(12000)
            if self.on_air and on_air_details is not None:
                self.on_air.emit("rx_decode", on_air_details)
                self.on_air_decode_pcm = None
            QTimer.singleShot(0, self._drain_rx)
            return
        if not isinstance(report, dict) or not isinstance(report.get("card"), dict):
            self.rx_status.setText("Decoder returned no card data; continuing to listen")
            QTimer.singleShot(0, self._drain_rx)
            return
        card = report.get("card") or {}
        try:
            report, improved = self._merge_live_preview(report)
        except ValueError as exc:
            self._report_receive_conflict(report, exc)
            QTimer.singleShot(0, self._drain_rx)
            return
        card = report.get("card") or {}
        report_identity = self._report_receive_identity(report)
        self.active_receive_identity = report_identity
        event = report.get("assembly_event") or {}
        complete = bool(event.get("verified_complete"))
        if self.on_air and on_air_details is not None:
            on_air_details.update(report=report, complete=complete,
                                  reference=decoded_reference(report))
            self.on_air.emit("rx_decode", on_air_details)
            if complete:
                self.on_air.emit("rx_verified_capture", on_air_details, pcm, self.rx_rate)
            self.on_air_decode_pcm = None
        image = self.card_image(report) if improved else None
        if card.get("card_id") is not None or card.get("preview_received"):
            live_status = card.get('color_stage') or 'waiting for image data'
            if not card.get("avatar_burst"):
                live_status += f" · {report.get('valid_packet_count', 0)} packets received"
            if image is not None and (card.get("preview_received") or card.get("avatar_burst") or card.get("raw_avatar")):
                self._show_receive_preview(image, card=card, exact=bool(card.get("exact")),
                                           coverage=card.get("pixel_coverage"),
                                           verified_coverage=card.get("verified_pixel_coverage"),
                                           report=report)
            self.rx_status.setText(
                f"Receiving card · {card.get('callsign') or 'station not identified'} · {live_status} · "
                f"RX mode: {self._receive_mode_label(report.get('receive_profile'))}")
        if complete:
            header = report.get("copy_header") or (card.get("diagnostics") or {})
            acquisition_start = header.get("acquisition_start_sample")
            encounter = (report_identity, acquisition_start,
                         None if acquisition_start is not None else
                         report.get("receive_profile") or stats.get("mode"),
                         int(card.get("received_copies", 0) or 0))
            if encounter in self.completed_receive_events:
                QTimer.singleShot(0, self._drain_rx)
                return
            self.completed_receive_events[encounter] = None
            if len(self.completed_receive_events) > 256:
                self.completed_receive_events.pop(next(iter(self.completed_receive_events)))
            if self.auto_armed:
                key = received_card_key(report)
                if (card.get("message_type") != "cq" and self._assembly_verified(report) and card.get("callsign")
                        and card.get("card_id") is not None and key in self.seen_received_cards):
                    self.rx_status.setText(f"Already received {card.get('callsign')} card {card.get('card_id')}; staying on receive.")
                    preserve_duplicate = self._show_duplicate_card(report)
                    # A duplicate is a completed card encounter, not a reason
                    # to discard buffered audio that may already contain the
                    # next station's header.
                    QTimer.singleShot(0, self._drain_rx)
                    return
                complete_text = ("All image blocks verified; saving the full capture and finalizing its QSL."
                                 if card.get("avatar_burst") else
                                 "Complete-card marker received; saving the full capture and finalizing its QSL.")
                self.exchange_log.append(complete_text)
                if self._assembly_verified(report) and card.get("callsign") and card.get("card_id") is not None:
                    self._add_session_wall_card(report)
                    self.seen_received_cards.add(key)
                    self.auto_live_handled_cards.add(key)
                    self._record_received_card(report)
                    self._handle_contact_message(report)
            else:
                self.rx_status.setText("All image blocks verified; saving and finalizing the card." if card.get("avatar_burst")
                                       else "Complete-card marker received; saving and finalizing the QSL.")
                preserve_duplicate = False
                if self._assembly_verified(report):
                    key = received_card_key(report)
                    if key in self.seen_received_cards:
                        self.rx_status.setText(f"Already heard {card.get('callsign') or 'this station'} card; staying on receive.")
                        preserve_duplicate = self._show_duplicate_card(report)
                    else:
                        self._add_session_wall_card(report)
                        self.seen_received_cards.add(key)
                        self._record_received_card(report)
                        self._handle_contact_message(report)
            if self.auto_armed:
                self.stop_receive()
            else:
                # Keep the rolling capture and protected-copy cache alive. A
                # following card may already be in the audio accumulated while
                # this decode worker was running.
                QTimer.singleShot(0, self._drain_rx)
        elif self.live_decode_final and self.auto_armed:
            self.stop_receive()
        else:
            QTimer.singleShot(0, self._drain_rx)

    def stop_receive(self):
        self.rx_session_id += 1
        self.active_live_decode_token = None
        self.live_decode_inflight = False
        self._flush_on_air_rx()
        self.rx_timer.stop(); self._drain_rx()
        self.auto_receive_timer.stop()
        self._stop_local_receive_capture()
        self.test_rx_pending = {}; self.test_rx_expected = None
        self.test_rx_final_pending = False; self.live_decode_final = False
        self.listen_indicator.setText("● PROCESSING")
        self.listen_indicator.setStyleSheet(status_label_style("warning"))
        if len(self.rx_bytes) < 4096:
            self.rx_status.setText("Capture was too short; no audio saved."); self._clear_rx_bytes()
            if self.auto_armed:
                if self.auto_timeout_pending:
                    self._resolve_auto_receive_timeout()
                    return
                self.exchange_log.append("Capture too short; no reply sent. Resuming receive.")
                QTimer.singleShot(300, self.start_receive)
            return
        DATA.mkdir(parents=True, exist_ok=True)
        path = DATA / f"received-{time.strftime('%Y%m%d-%H%M%S')}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(self.rx_rate); output.writeframes(bytes(self.rx_bytes))
        self._clear_rx_bytes()
        if self.auto_armed: self.auto_decode_paths.add(path.resolve())
        self.start_decode(path, profile=self.rx_live_profile or "auto")

    def card_image(self, report):
        data = report.get("card") or report.get("image")
        if not isinstance(data, dict) or not report.get("pixels"): return None
        pixels = report["pixels"]
        width, height = int(data.get("width", 32)), int(data.get("height", 32))
        image = QImage(width, height, QImage.Format.Format_RGB32)
        for y in range(height):
            for x in range(width):
                if len(pixels) == height and isinstance(pixels[y], (list, tuple)) and len(pixels[y]) == width:
                    rgb = pixels[y][x]
                    scale = 17 if max(rgb) <= 15 else 1
                    image.setPixelColor(x, y, QColor(*(int(v) * scale for v in rgb)))
                else:
                    card = report.get("card") or report
                    palette = card.get("palette", PALETTE)
                    pixel_number = y * width + x
                    coverage = card.get("pixel_coverage")
                    if coverage is not None and (pixel_number >= len(coverage) or not coverage[pixel_number]):
                        shade = 65 if (x + y) % 2 else 105
                        image.setPixelColor(x, y, QColor(shade, shade, shade))
                    else:
                        index = int(pixels[pixel_number])
                        rgb = palette[index]
                        image.setPixelColor(x, y, QColor(*(int(v)*17 for v in rgb)))
        return image

    def _show_duplicate_card(self, report):
        """Keep one receipt for each retransmitted card during this app session."""
        card = report.get("card") or {}
        key = received_card_key(report)
        first_receipt = key not in self.session_duplicate_receipts
        if first_receipt:
            self.session_duplicate_receipts.add(key)
            self.duplicate_displayed_in_qso = True
            self.duplicate_preview_key = key
            self.exchange_log.append(
                f"Duplicate card from {card.get('callsign') or 'this station'} received; "
                "keeping one duplicate receipt this session.")
        self._add_session_wall_card(report, duplicate=True)
        if first_receipt:
            receipt = copy.deepcopy(report)
            receipt["duplicate_receipt"] = True
            self._record_received_card(receipt)
        image = self.card_image(report)
        self._show_receive_preview(image, card=card, exact=True, duplicate=True)
        self.rx_status.setText(
            f"DUPLICATE · {card.get('callsign') or 'this station'} card "
            f"{card.get('card_id', '')} · whole-card integrity verified")
        return first_receipt

    def _add_session_wall_card(self, report, *, image=None, duplicate=False):
        card = report.get("card") or {}
        if not card.get("callsign") or card.get("card_id") is None:
            return
        image = image if image is not None else self.card_image(report)
        if image is None or image.isNull():
            return
        key = session_wall_card_key(report)
        existing = self.session_wall_items.get(key)
        callsign = str(card.get("callsign") or "Unknown station")
        grid = str(card.get("grid") or "")
        kind = {"cq": "CQ", "exchange": "Exchange", "report73": "RR73",
                "final73": "73", "73": "73"}.get(card.get("message_type"), "Card")
        if card.get('message_type') == '73' and card.get('reception_report'):
            kind = 'RR73 · report'
        received_at = time.strftime("%H:%M:%S")
        tile = image.scaled(QSize(144, 144), Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.FastTransformation)
        icon = QIcon(QPixmap.fromImage(tile))
        exact = self._assembly_verified(report)
        received_percent = self._coverage_percent(card.get("pixel_coverage"))
        verified_percent = self._coverage_percent(card.get("verified_pixel_coverage"))
        integrity_text = ("Integrity: whole card verified" if exact else
                          f"Integrity: provisional · {received_percent}% received · "
                          f"{verified_percent}% of image regions verified")
        accessible_label = " · ".join(value for value in (
            "DUPLICATE" if duplicate else "",
            "VERIFIED" if exact else "PROVISIONAL",
            callsign, grid, kind, f"received {received_at}") if value)
        frequency_hz = report.get("frequency_hz")
        if frequency_hz is None:
            frequency_hz = self.frequency.value()
        try:
            frequency_text = f"{float(frequency_hz) / 1_000_000:.6f} MHz"
        except (TypeError, ValueError):
            frequency_text = "Not recorded"
        measured_snr = card.get("measured_snr_db", report.get("measured_snr_db"))
        reported_snr = card.get("snr_db", report.get("snr_db"))
        snr_text = "\n".join(snr_lines({**card, "measured_snr_db": measured_snr,
                                       "snr_db": reported_snr}))
        profile = report.get("receive_profile") or self.receive_mode()
        if is_card_backend(profile):
            mode_text = get_backend(profile).label
        elif str(profile).startswith("data2g:"):
            mode_text = "Data2G · " + str(profile).split(":", 1)[1]
        else:
            mode_text = str(profile or "Unknown mode")
        back_text = "\n".join((
            "DUPLICATE" if duplicate else "RECEIVED CARD", callsign,
            grid or "Grid unavailable", kind, snr_text,
            f"Frequency: {frequency_text}", f"Mode: {mode_text}",
            f"Received: {received_at}",
            f"Image: {card.get('width', image.width())} × {card.get('height', image.height())}",
            integrity_text,
        ))
        if existing is None:
            item = QListWidgetItem()
            item.setSizeHint(QSize(148, 148))
            self.session_wall.insertItem(0, item)
            card_face = QPushButton()
            card_face.setCheckable(True)
            card_face.setFixedSize(144, 144)
            card_face.setIconSize(QSize(132, 132))
            card_face.setStyleSheet("QPushButton { background:#101a17; border:1px solid #43564d; border-radius:7px; padding:4px; } QPushButton:checked { background:#18251f; color:#e2eee6; border-color:#70927d; font-size:10px; font-weight:600; text-align:left; }")
            def flip_card(show_back, button=card_face):
                if show_back and getattr(button, '_animation_report', None):
                    with QSignalBlocker(button):
                        button.setChecked(False)
                    AnimationDetailsDialog(button._animation_report, self.card_image, self).exec()
                    return
                button.setIcon(QIcon() if show_back else button._front_icon)
                button.setText(button._back_text if show_back else "")
                button.setAccessibleName(button._accessible_label +
                    (" · card back; receive details" if show_back else
                     " · card front; activate to read receive details"))
            card_face.toggled.connect(flip_card)
            self.session_wall.setItemWidget(item, card_face)
            self.session_wall_items[key] = (item, card_face)
            self.session_wall_count += 1
            self.session_wall_keys.add(key)
        else:
            item, card_face = existing
        card_face._front_icon = icon
        card_face._back_text = back_text
        card_face._accessible_label = accessible_label
        card_face._ordinary_icon = icon
        card_face._ordinary_back = back_text
        card_face._ordinary_label = accessible_label
        card_face._report = copy.deepcopy(report)
        card_face._report.setdefault('logged_at', time.strftime('%Y-%m-%d %H:%M:%S'))
        card_face._report.setdefault('frequency_hz', frequency_hz)
        card_face._report.setdefault('receive_profile', profile)
        item.setToolTip(accessible_label)
        item.setData(Qt.ItemDataRole.AccessibleTextRole, accessible_label)
        card_face.setToolTip(accessible_label + " · click to flip for receive details")
        if card_face.isChecked():
            card_face.setIcon(QIcon())
            card_face.setText(back_text)
            card_face.setAccessibleName(accessible_label + " · card back; receive details")
        else:
            card_face.setText("")
            card_face.setIcon(icon)
            card_face.setAccessibleName(accessible_label + " · card front; activate to read receive details")
        self.session_wall_empty.hide()
        self.session_wall.show()
        self._update_wall_animations()

    def _saved_animation_candidates(self):
        """Read saved receptions across sessions, caching unchanged archive files."""
        cache = getattr(self, '_animation_archive_cache', {})
        current, reports = {}, []
        for path in self.qso_log_dir.glob('*.json'):
            try:
                stat = path.stat(); stamp = (stat.st_mtime_ns, stat.st_size)
                old = cache.get(path)
                cards = old[1] if old and old[0] == stamp else json.loads(path.read_text()).get('received_cards', [])
                current[path] = (stamp, cards); reports.extend(cards)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        self._animation_archive_cache = current
        return reports

    def _refresh_animation_references(self):
        """Late sidecars upgrade metadata without inventing another reception."""
        links = self.card_assembly.animation_links
        feedback_caches = (self.card_assembly.feedback, self.copy_capture_cache.feedback)
        for _item, button in self.session_wall_items.values():
            if hasattr(button, '_report'):
                links.attach(button._report)
                for cache in feedback_caches:
                    if cache.attach(button._report):
                        base = '\n'.join(line for line in button._ordinary_back.splitlines()
                                         if not line.startswith('Their reception of '))
                        button._ordinary_back = base + '\n' + '\n'.join(reception_report_lines(button._report['card']))
        entries = []
        if self.active_qso_log is not None:
            entries.append((self.active_qso_log, self._active_qso_path))
        entries.extend(getattr(self, 'passive_received_logs', {}).values())
        active_paths = {path for _, path in entries}
        for path in self.qso_log_dir.glob('*.json'):
            if path not in active_paths:
                try:
                    entries.append((json.loads(path.read_text()), path))
                except (OSError, ValueError):
                    continue
        for entry, path in entries:
            changed = False
            for report in entry.get('received_cards', []):
                meta = report.get('card') or {}
                before = copy.deepcopy((meta.get('animation'), meta.get('reception_report')))
                links.attach(report)
                for cache in feedback_caches:
                    cache.attach(report)
                changed |= before != (meta.get('animation'), meta.get('reception_report'))
            if changed and path is not None:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, suffix='.tmp', delete=False) as handle:
                        temporary = Path(handle.name); handle.write(json.dumps(entry, indent=2) + '\n')
                    os.replace(temporary, path)
                except OSError as exc:
                    self.rx_status.setText(f'Card metadata received; could not save it: {exc}')
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        if self.reply_report:
            changed = False
            for cache in feedback_caches:
                changed |= cache.attach(self.reply_report)
            if changed:
                self._handle_contact_message(self.reply_report)
        self._update_wall_animations()

    def _update_wall_animations(self):
        tiles = [(item, button) for item, button in self.session_wall_items.values()
                 if hasattr(button, '_report')]
        reports = [button._report for _, button in tiles]
        candidates = self._saved_animation_candidates() + reports
        for item, button in tiles:
            item.setHidden(False)
            button._animation_report = None
            button._animation_icons = []
            button._front_icon = button._ordinary_icon
            button._back_text = button._ordinary_back
            button._accessible_label = button._ordinary_label
            button.setToolTip(button._ordinary_label + ' · click to flip for receive details')
            button.setAccessibleName(button._ordinary_label)
            waiting = animation_details(button._report)
            if waiting:
                button._back_text += '\n' + waiting
                button.setToolTip(button._ordinary_label + ' · ' + waiting)
            if not button.isChecked():
                button.setIcon(button._front_icon)
            else:
                button.setText(button._back_text)
        for _item, button in tiles:
            second = button._report
            first = animation_partner(second, candidates)
            if first is None:
                continue
            combined = {**second, 'animation_frames': [first, second]}
            button._animation_report = combined
            button._animation_ms = second['card']['animation']['frame_ms']
            button._animation_icons = [QIcon(card_art(self.card_image(frame),
                f'Frame {i+1} / 2', 'ANIMATION', QSize(132, 132))) for i, frame in enumerate((first, second))]
            button._back_text = animation_details(combined)
            button._accessible_label = button._ordinary_label + ' · two-frame animation; activate for both frames’ details'
            button.setToolTip(button._accessible_label)
            button.setAccessibleName(button._accessible_label)
            with QSignalBlocker(button):
                button.setChecked(False)
            button.setText(''); button.setIcon(button._animation_icons[0])
            for first_item, first_button in tiles:
                candidate = first_button._report
                if candidate is first:
                    first_item.setHidden(True)
                elif (candidate.get('card') or {}).get('card_id') == second['card']['animation']['first_card_id']:
                    try:
                        if artwork_digest(candidate) == second['card']['animation']['first_digest']:
                            first_item.setHidden(True)
                    except (ValueError, KeyError, TypeError, IndexError):
                        pass
        if not hasattr(self, '_animation_wall_timer'):
            self._animation_wall_timer = QTimer(self)
            self._animation_wall_timer.timeout.connect(self._advance_wall_animations)
        if any(getattr(button, '_animation_icons', ()) for _, button in tiles):
            self._animation_wall_timer.start(100)
        else:
            self._animation_wall_timer.stop()

    def _advance_wall_animations(self):
        now = time.monotonic()*1000
        for _item, button in self.session_wall_items.values():
            icons = getattr(button, '_animation_icons', ())
            if icons and button.isVisible() and not button.isChecked():
                button._front_icon = icons[int(now/button._animation_ms) % 2]
                button.setIcon(button._front_icon)

    def _reset_receive_window(self, *, preserve_preview=False):
        self._clear_rx_bytes()
        self.live_decode_samples = 0
        self.live_decode_final = False
        self.card_assembly.begin_receive_window()
        self.copy_capture_cache = self.copy_capture_cache.next_window()
        self.rx_capture_sample_offset = 0
        if not preserve_preview:
            self._clear_exact_live_preview()
        self.live_preview_timeout.stop()
        self.receive_stack.setCurrentWidget(self.session_wall_page)
        snapshot = self.card_assembly.preview_snapshot()
        if not preserve_preview and (not snapshot or snapshot.card.get("exact")):
            self._hide_receive_preview()
        self.rx_status.setText(self._listening_status())
        self.listen_indicator.setText("● LISTENING")
        self.listen_indicator.setStyleSheet(status_label_style("positive"))
        QTimer.singleShot(0, self._drain_rx)

    def _return_to_session_wall(self):
        self.receive_preview_box.hide()
        if self.rx_timer.isActive():
            self.rx_status.setText(self._listening_status())

    def _record_received_card(self, report):
        card = report.get("card") or {}
        callsign = str(card.get("callsign", "Unknown")).strip().upper() or "Unknown"
        active_call = str((self.active_qso_log or {}).get("peer_callsign", "")).strip().upper()
        if self.active_qso_log is not None and active_call and active_call != callsign:
            # Keep an unrelated station's reception out of the active QSO. A
            # passive per-station log preserves it for the callsign archive and
            # can collect later cards from that station during this app session.
            passive = self.passive_received_logs.get(callsign)
            if passive is None:
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                passive_entry = {"peer_callsign": callsign, "peer_grid": card.get("grid", ""),
                                 "started_at": now, "sent_cards": [], "received_cards": [],
                                 "status": "Received during multi-station session"}
                passive = (passive_entry, self.qso_log_dir /
                           f"qso-{time.strftime('%Y%m%d-%H%M%S')}-"
                           f"{id(passive_entry) & 0xffff:04x}-rx.json")
                self.passive_received_logs[callsign] = passive
            original_log, original_path = self.active_qso_log, self._active_qso_path
            try:
                self.active_qso_log, self._active_qso_path = passive
                self._append_qso_card("received_cards", copy.deepcopy(report))
            finally:
                self.active_qso_log, self._active_qso_path = original_log, original_path
            return
        self._begin_qso_log(card.get("callsign", "Unknown"), card.get("grid", ""))
        if card.get("message_type") == "exchange" and self.last_cq_snapshot:
            if not any((item.get("card") or {}).get("message_type") == "cq" for item in self.active_qso_log["sent_cards"]):
                self._append_qso_card("sent_cards", copy.deepcopy(self.last_cq_snapshot))
        self._append_qso_card("received_cards", report)

    def _begin_qso_log(self, callsign="Unknown", grid=""):
        if self.active_qso_log is not None: return
        self.active_qso_log = {"peer_callsign": callsign or "Unknown", "peer_grid": grid or "",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "sent_cards": [], "received_cards": [], "status": "In progress"}
        self._active_qso_path = self.qso_log_dir / f"qso-{time.strftime('%Y%m%d-%H%M%S')}-{id(self.active_qso_log) & 0xffff:04x}.json"
        self.duplicate_displayed_in_qso = False
        self.duplicate_preview_key = None

    def _append_qso_card(self, bucket, card_data):
        if self.active_qso_log is None: self._begin_qso_log()
        if bucket == 'received_cards':
            card_data = copy.deepcopy(card_data)
        new_card = card_data.get("card") or {}
        attempts_key = "transmission_attempts" if bucket == "sent_cards" else "receive_attempts"
        duplicate = next((item for item in self.active_qso_log[bucket]
                          if (item.get("card") or {}).get("card_id") == new_card.get("card_id")
                          and (item.get("card") or {}).get("callsign") == new_card.get("callsign")
                          and (item.get("card") or {}).get("message_type", "card") == new_card.get("message_type", "card")
                          and (bucket == 'received_cards' or (item.get("card") or {}).get("snr_db") == new_card.get("snr_db"))
                          and (bucket != 'received_cards' or same_received_card(item, card_data))
                          and bool(item.get("duplicate_receipt")) == bool(card_data.get("duplicate_receipt"))
                          and new_card.get("card_id") is not None), None)
        if duplicate is not None:
            attempts = int(duplicate.get(attempts_key, 1)) + 1
            if (bucket == "received_cards" and
                    _receive_report_rank(card_data) >= _receive_report_rank(duplicate)):
                order, logged_at = duplicate.get("order"), duplicate.get("logged_at")
                duplicate.update(card_data)
                if order is not None:
                    duplicate["order"] = order
                if logged_at is not None:
                    duplicate["logged_at"] = logged_at
            duplicate[attempts_key] = attempts
        else:
            card_data[attempts_key] = 1
            card_data["order"] = max((int(item.get("order", 0)) for key in ("sent_cards", "received_cards")
                                       for item in self.active_qso_log[key]), default=0) + 1
            card_data["logged_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.active_qso_log[bucket].append(card_data)
        if bucket == "received_cards":
            meta = new_card
            self.active_qso_log["peer_callsign"] = meta.get("callsign", self.active_qso_log.get("peer_callsign", "Unknown"))
            self.active_qso_log["peer_grid"] = meta.get("grid", self.active_qso_log.get("peer_grid", ""))
        self.active_qso_log["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._persist_active_qso_log()
        self.refresh_qso_log()

    def _persist_active_qso_log(self):
        if self.active_qso_log is None or self._active_qso_path is None:
            return False
        temporary = None
        try:
            self.qso_log_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.qso_log_dir,
                    prefix=".qso-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(self.active_qso_log, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._active_qso_path)
        except OSError as exc:
            message = f"Could not save QSO log: {exc}"
            if hasattr(self, "exchange_log"):
                self.exchange_log.append(message)
            if hasattr(self, "rx_status"):
                self.rx_status.setText(message)
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return True

    def _local_card_snapshot(self, card, message_type="card", snr_db=None):
        pixels = []
        for y in range(card.height):
            row = []
            for x in range(card.width):
                rgb = card.palette[card.pixels[y*card.width+x]]
                row.append([int(channel)*17 for channel in rgb])
            pixels.append(row)
        return {"card": {"callsign": card.callsign, "grid": card.grid, "card_id": card.card_id,
                          "width": card.width, "height": card.height,
                          "palette": [list(color) for color in card.palette],
                          "animation": copy.deepcopy(card.animation),
                          "reception_report": copy.deepcopy(card.reception_report),
                          "message_type": {"report73": "73", "final73": "73"}.get(message_type, message_type),
                          "snr_db": snr_db}, "pixels": pixels}

    def closeEvent(self, event):
        self.beacon_timer.stop()
        self.beacon_status_timer.stop()
        self.receive_activity_timeout.stop()
        self._closing = True
        self.data2g_handoff_pending = False
        if self.data2g_connect_worker is not None:
            self.data2g_connect_worker.cancel()
        self._flush_on_air_rx()
        self._stop_local_receive_capture()
        if self.on_air:
            self.on_air.emit("client_leave", {"dropped_jobs": self.on_air.dropped,
                                             "write_errors": self.on_air.errors,
                                             "network_errors": self.on_air.network_errors})
            if not self.on_air.close():
                print("On-air recorder did not finish flushing before shutdown.", file=sys.stderr)
            if self.on_air.dropped or self.on_air.errors or self.on_air.network_errors:
                print(f"On-air capture: {self.on_air.dropped} dropped jobs, {self.on_air.errors} write errors, "
                      f"{self.on_air.network_errors} collector failures; review local events.", file=sys.stderr)
        if self.web_server is not None:
            self.web_server.close(); self.web_server = None
        if self.data2g_session is not None:
            DATA2G_ADAPTER.disconnect(self.data2g_session, timeout=1.0)
        self._stop_local_data2g_host()
        super().closeEvent(event)

    def _on_air_settings(self):
        return {"modem": self.selected_mode(), "audio_placement": self.selected_audio_placement(),
                "receive_mode": self.receive_mode(), "frequency_hz": self.frequency.value(),
                "rig_mode": self.rig_mode, "copies": self.repeat_count.value(),
                "input_device": self.input_device.currentText(),
                "output_device": self.output_device.currentText(),
                "audio_test": self.audio_test_mode.isChecked(), "software_link": self.test_link_enabled,
                "receiver": self.on_air.config.get("receiver", {}) if self.on_air else {}}

    def _record_on_air_tx(self):
        if self.on_air:
            try:
                card = self.tx_card or self._chosen_card()
                reference = card_reference(card)
            except (ValueError, AttributeError):
                reference = None
            self.on_air_tx_id = self.on_air.emit("tx_start", {
                **self._on_air_settings(), "message_type": self.tx_message_type,
                "reference": reference, "ptt_asserted": self.ptt_active,
                "timing_note": "application start; not hardware RF onset"}, self.tx_bytes)

    def _flush_on_air_rx(self, count=None):
        if not self.on_air or not self.on_air_rx_buffer:
            return
        count = len(self.on_air_rx_buffer) if count is None else count
        pcm = bytes(self.on_air_rx_buffer[:count])
        del self.on_air_rx_buffer[:count]
        self.on_air.emit("rx_audio", {"rx_stream_id": self.on_air_rx_stream,
                                     "sample_start": self.on_air_rx_samples,
                                     "sample_end": self.on_air_rx_samples + len(pcm) // 2}, pcm, self.rx_rate)
        self.on_air_rx_samples += len(pcm) // 2


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--run-data2g-host":
        try:
            from data2g_receive_host import main as data2g_server_main
        except ImportError as exc:
            print(f"Bundled Data2G host is unavailable: {exc}", file=sys.stderr)
            return 1
        sys.argv = ["data2g-host", *sys.argv[2:]]
        data2g_server_main()
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName("PixelQSO")
    app.setOrganizationName("PixelQSO")
    app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    win = MainWindow(); win.show()
    stopping = [False]
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__(0, True))
    shutdown_timer = QTimer(win)
    shutdown_timer.timeout.connect(lambda: win.close() if stopping[0] else None)
    shutdown_timer.start(250)
    if win.on_air:
        win.on_air.emit("client_ready", {"audio_role": os.environ.get("PIXELQSO_AUDIO_ROLE", "station")})
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

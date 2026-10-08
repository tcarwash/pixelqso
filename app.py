#!/usr/bin/env python3
"""Pixel QSO desktop application for editable, self-identifying image bursts."""
from __future__ import annotations

import json
import hashlib
import signal
import copy
import math
import os
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
from PySide6.QtCore import Qt, QRect, QSize, QObject, QTimer, QRunnable, QThreadPool, Signal, QStandardPaths, QMimeData, QPoint, QUrl, QSortFilterProxyModel, QSettings, QSignalBlocker
from PySide6.QtGui import QColor, QPainter, QPen, QImage, QPixmap, QFont, QIcon, QKeySequence, QShortcut, QDrag, QStandardItem, QStandardItemModel
from PySide6.QtNetwork import QTcpSocket, QUdpSocket, QHostAddress
from PySide6.QtMultimedia import QAudioDevice, QAudioFormat, QAudioSink, QAudioSource, QMediaDevices
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QListWidget, QListWidgetItem,
    QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QInputDialog,
    QMessageBox, QPushButton, QSpinBox, QTabWidget, QTextEdit, QDialog,
    QVBoxLayout, QWidget, QButtonGroup, QScrollArea, QTableView, QAbstractItemView, QFrame, QCheckBox, QGroupBox,
    QStackedWidget, QProgressBar)

import cardmodem as modem
from backend_adapters import (AdapterKind, DATA2G_ADAPTER, DecodeFailure,
                              DecodeFailureKind, DecodeOutcome,
                              EXPERIMENTAL_ADAPTER, ReceiveStartError,
                              adapter_for_backend_selection)
from card_backends import (BACKENDS, DEFAULT_MODE_KEY, LEGACY_MODE_KEY, MODE_REGISTRY, Data2GHostBackend,
                           available_modes, get_backend,
                           data2g_mode_name, host_mode_backends, is_card_backend, is_experimental_mode,
                           is_data2g_mode_key, mode_supports_audio_placement,
                           preferred_data2g_mode_key, recommended_data2g_modes,
                           register_data2g_mode_backends, EXPERIMENTAL_MODE_DESCRIPTIONS)
from card_transfer import CardAssemblyService
from data2g_transport import Data2GMode
from data2g_runtime import (data2g_audio_device_selector,
                            data2g_host_audio_devices, int_setting,
                            local_host_arguments, local_host_command,
                            validate_data2g_ports)
from on_air import SessionRecorder, card_reference, decoded_reference
from card_views import DeckDialog, card_art, deck_events, deck_image
from webserver import CompanionServer

APP_ICON_PATH = Path(__file__).resolve().with_name("icon.png")
DATA2G_ACK_TIMEOUT_MS = 15_000
DATA2G_BUSY_ACK_TIMEOUT_MS = 5 * 60_000
DATA = Path.home() / ".local" / "share" / "PixelQSO"
AUTO_REPLY_GUARD_MS = 600
PALETTE = [(0, 0, 0), (15, 15, 15), (0, 0, 15), (0, 15, 15),
           (0, 15, 0), (15, 15, 0), (15, 0, 0), (15, 0, 15)]


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


def default_card(callsign="N0CALL", grid="AA00") -> modem.Card:
    pixels = [0] * 1024
    return modem.Card(callsign, grid, PALETTE.copy(), pixels)


def received_card_key(report):
    card = report.get("card") or {}
    pixels = json.dumps(report.get("pixels"), separators=(",", ":"), ensure_ascii=True).encode("ascii")
    fingerprint = hashlib.blake2s(pixels, digest_size=8).hexdigest()
    return (str(card.get("callsign", "")).upper(), str(card.get("grid", "")).upper(),
            card.get("card_id"), card.get("message_type", "card"), card.get("snr_db"), fingerprint)


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
        self.setMinimumHeight(156)
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

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0b1115"))
        fm = painter.fontMetrics()
        name = fm.elidedText(self.device_name, Qt.TextElideMode.ElideMiddle,
                             max(100, self.width() - 300))
        painter.setPen(QColor("#d0ddd9"))
        painter.drawText(8, 17, f"RX AUDIO  ·  {name}")
        painter.setPen(QColor("#a5b8b5"))
        painter.drawText(max(8, self.width() - fm.horizontalAdvance(self._level_text) - 8),
                         17, self._level_text)

        graph = self.rect().adjusted(42, 24, -8, -22)
        image_data = self._rows.tobytes()
        image = QImage(image_data, self.COLUMNS, self.ROWS, self.COLUMNS * 3,
                       QImage.Format.Format_RGB888)
        painter.drawImage(graph, image)
        painter.setPen(QPen(QColor(150, 180, 182, 95), 1))
        for khz in range(5):
            x = graph.left() + round(graph.width() * khz / 4)
            painter.drawLine(x, graph.top(), x, graph.bottom())
            painter.setPen(QColor("#91a4a5"))
            painter.drawText(x - 12, self.height() - 5, f"{khz}k")
            painter.setPen(QPen(QColor(150, 180, 182, 95), 1))
        for db_label in (-20, -60, -100):
            y = graph.top() + round(graph.height() * (-20 - db_label) / 80)
            painter.drawLine(graph.left(), y, graph.right(), y)
        painter.setPen(QColor("#91a4a5"))
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


def decode_all_card_backends(audio, sample_rate, audio_placement="near_carrier",
                             preferred_mode=None, include_experimental=False,
                             on_preview=None, copy_cache=None, capture_start_sample=0, announced_copies=None):
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
            if cached_copies:
                audio,sample_rate,copies=cached_audio,8000,cached_copies
        if copies:
            preview, failure = None, None
            for key in dict.fromkeys(copy.mode for copy in copies):
                if key == LEGACY_MODE_KEY:
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
                        if report.get('card',{}).get('exact'):
                            return DecodeOutcome(report=report)
                        if preview is None or (preview.get('receive_activity') and not report.get('receive_activity')):
                            preview = report
                            if on_preview is not None:
                                on_preview(report)
                    except ValueError as exc:
                        failure = DecodeFailure(EXPERIMENTAL_ADAPTER.key, DecodeFailureKind.NO_MATCH, str(exc), True)
            return DecodeOutcome(report=preview) if preview is not None else DecodeOutcome(
                failure=failure or DecodeFailure(EXPERIMENTAL_ADAPTER.key, DecodeFailureKind.NO_MATCH,
                                                 'Announced payload is incomplete', True))
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
                 include_experimental=False, copy_cache=None, capture_start_sample=0, capture_start_utc=None):
        super().__init__()
        self.copy_cache=copy_cache
        self.capture_start_sample=capture_start_sample
        self.capture_start_utc=capture_start_utc
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
                    (self.mode == "auto" or self.copy_cache.entries)):
                from modem_envelope import find_copies
                announced = find_copies(audio,self.sample_rate, capture_start_utc=self.capture_start_utc)
            if self.mode == "auto" or announced or (self.copy_cache is not None and self.copy_cache.entries):
                outcome = decode_all_card_backends(audio, self.sample_rate, self.audio_placement,
                                                   self.preferred_mode, self.include_experimental or self.mode != "auto",
                                                   on_preview=lambda report: self.signals.preview.emit(
                                                       (self.generation, report)),
                                                   copy_cache=self.copy_cache,
                                                   capture_start_sample=self.capture_start_sample, announced_copies=announced)
                if outcome.failure:
                    self._finish(None, False, outcome.failure, started)
                    return
                result = outcome.report
                result.setdefault("valid_packet_count", 0)
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
    changed = Signal()
    image_dropped = Signal(QImage)
    placed_stamp_removed = Signal()
    def __init__(self, card: modem.Card):
        super().__init__()
        self.card = card
        self.color_index = 1
        self.tool = "draw"
        self.show_grid = True
        self.last_paint_pixel = None
        self.undo_stack = []
        self.redo_stack = []
        self.base_pixels = list(card.pixels)
        self.stamp_layers = []
        self.selected_layer = None
        self.hover_layer = None
        self.hover_remove_rect = QRect()
        self.drag_layer = None
        self.drag_offset = (0, 0)
        self.setAcceptDrops(True)
        self.setMinimumSize(320, 320)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setMouseTracking(True)

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
        if self.hover_layer is None or self.hover_layer >= len(self.stamp_layers):
            self.hover_remove_rect = QRect()
            return
        side = min(self.width(), self.height())
        cell = side / max(self.card.width, self.card.height)
        drawn_width, drawn_height = cell*self.card.width, cell*self.card.height
        ox, oy = (self.width()-drawn_width)/2, (self.height()-drawn_height)/2
        layer = self.stamp_layers[self.hover_layer]
        stamp_right = int(ox + (layer["x"] + layer["width"]) * cell)
        stamp_top = int(oy + layer["y"] * cell)
        button_size = max(20, min(30, int(cell * 0.9)))
        self.hover_remove_rect = QRect(stamp_right-button_size, stamp_top,
                                       button_size, button_size)

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
        p.setPen(QPen(QColor(90, 90, 90), 0.5))
        for x in range(self.card.width+1) if self.show_grid else ():
            p.drawLine(int(ox+x*cell), int(oy), int(ox+x*cell), int(oy+cell*self.card.height))
        for y in range(self.card.height+1) if self.show_grid else ():
            p.drawLine(int(ox), int(oy+y*cell), int(ox+cell*self.card.width), int(oy+y*cell))
        if self.selected_layer is not None and self.selected_layer < len(self.stamp_layers):
            layer = self.stamp_layers[self.selected_layer]
            p.setPen(QPen(QColor(255, 220, 60), 2))
            p.drawRect(QRect(int(ox+layer['x']*cell), int(oy+layer['y']*cell),
                             int(layer['width']*cell), int(layer['height']*cell)))
        if self.hover_layer is not None and self.hover_layer < len(self.stamp_layers):
            layer = self.stamp_layers[self.hover_layer]
            self._update_hover_remove_rect()
            p.setPen(QPen(QColor(110, 0, 0), 1))
            p.setBrush(QColor(220, 35, 45, 245))
            p.drawEllipse(self.hover_remove_rect)
            inset = max(6, self.hover_remove_rect.width() // 3)
            left = self.hover_remove_rect.left() + inset
            right = self.hover_remove_rect.right() - inset
            top = self.hover_remove_rect.top() + inset
            bottom = self.hover_remove_rect.bottom() - inset
            p.setPen(QPen(QColor(255, 255, 255), max(2, self.hover_remove_rect.width() // 9),
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(left, top, right, bottom)
            p.drawLine(right, top, left, bottom)
        else:
            self.hover_remove_rect = QRect()

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
        self.drag_layer = None
        self.undo_stack.clear(); self.redo_stack.clear()
        self._render_layers()

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
        self.selected_layer = self.drag_layer = None
        self.color_index = min(self.color_index, len(palette)-1)
        self._render_layers()

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

    def add_stamp(self, image):
        if image.isNull(): return
        self.remember_edit()
        self.tool = "move"
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
        if (event.button() == Qt.MouseButton.LeftButton and self.hover_layer is not None and
                self.hover_layer < len(self.stamp_layers) and self.hover_remove_rect.contains(point)):
            self.remember_edit()
            del self.stamp_layers[self.hover_layer]
            self.selected_layer = None
            self.hover_layer = None
            self._render_layers()
            self.placed_stamp_removed.emit()
            return
        x, y = self.card_position(point)
        if x is None: return
        if self.tool == "pick":
            self.color_index = self.card.pixels[y*self.card.width+x]
            self.tool = "draw"; self.changed.emit(); return
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
        self.last_paint_pixel = None
        self.drag_layer = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if self.undo_stack and self.undo_stack[-1] == self.snapshot(): self.undo_stack.pop()
        self.changed.emit()

    def mouseMoveEvent(self, event):
        if not event.buttons() & (Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton):
            point = event.position().toPoint()
            x, y = self.card_position(point)
            # The close control may cover transparent pixels or extend beyond a small stamp.
            layer = (self.hover_layer if self.hover_layer is not None and
                     self.hover_remove_rect.contains(point) else
                     self._hit_layer(x, y) if x is not None else None)
            if layer != self.hover_layer:
                self.hover_layer = layer
                self._update_hover_remove_rect()
                self.update()
            if layer is not None and self.hover_remove_rect.contains(event.position().toPoint()):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if self.tool == "move" and self.drag_layer is not None:
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
    def __init__(self, image: QImage, color: QColor):
        super().__init__()
        self.image = image
        self.color = color
        self.erase = False
        self.setMinimumSize(384, 384)
        self.setMaximumSize(512, 512)

    def _paint_pixel(self, point):
        side = min(self.width(), self.height())
        cell = side / max(self.image.width(), self.image.height())
        ox, oy = (self.width() - self.image.width()*cell)/2, (self.height() - self.image.height()*cell)/2
        x, y = int((point.x()-ox)/cell), int((point.y()-oy)/cell)
        if 0 <= x < self.image.width() and 0 <= y < self.image.height():
            self.image.setPixelColor(x, y, QColor(0, 0, 0, 0) if self.erase else self.color)
            self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#202820"))
        side = min(self.width(), self.height())
        cell = side / max(self.image.width(), self.image.height())
        ox, oy = (self.width()-self.image.width()*cell)/2, (self.height()-self.image.height()*cell)/2
        for y in range(self.image.height()):
            for x in range(self.image.width()):
                rect = QRect(int(ox+x*cell), int(oy+y*cell), max(1, int(cell)), max(1, int(cell)))
                painter.fillRect(rect, QColor("#364136") if (x+y)%2 else QColor("#293329"))
                color = self.image.pixelColor(x, y)
                if color.alpha():
                    painter.fillRect(rect, color)
        painter.setPen(QPen(QColor(110, 130, 110), 0.5))
        for x in range(self.image.width()+1):
            px = int(ox+x*cell); painter.drawLine(px, int(oy), px, int(oy+self.image.height()*cell))
        for y in range(self.image.height()+1):
            py = int(oy+y*cell); painter.drawLine(int(ox), py, int(ox+self.image.width()*cell), py)

    def mousePressEvent(self, event):
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self.erase = event.button() == Qt.MouseButton.RightButton
            self._paint_pixel(event.position().toPoint())

    def mouseMoveEvent(self, event):
        if event.buttons() & (Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton):
            self._paint_pixel(event.position().toPoint())


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
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #11161a; color: #e8eee9; }
            QTabWidget::pane { border: 1px solid #2e3d45; border-radius: 10px; top: -1px; }
            QTabBar::tab { background: #1b252b; color: #9eada3; padding: 10px 18px; margin-right: 4px; border-radius: 8px 8px 0 0; }
            QTabBar::tab:selected { background: #304149; color: #eff7f1; }
            QGroupBox { border: 1px solid #2e3d45; border-radius: 10px; margin-top: 12px; padding: 12px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #b8d4c0; }
            QLineEdit, QComboBox, QSpinBox, QTextEdit, QTableView, QListWidget { background: #192126; border: 1px solid #35464e; border-radius: 7px; padding: 6px; selection-background-color: #52775d; }
            QPushButton { background: #25333b; border: 1px solid #3c5747; border-radius: 8px; padding: 8px 12px; }
            QPushButton:hover { background: #344a54; }
            QPushButton:checked { background: #36564e; border: 1px solid #9ed7bd; color: #eefff6; }
            QListWidget::item { border-radius: 10px; padding: 5px; }
            QListWidget::item:hover { background: #24343d; }
            QListWidget::item:selected { background: #30474f; border: 1px solid #729b96; }
            QScrollBar:vertical { background:#141b20; width:10px; }
            QScrollBar::handle:vertical { background:#40545c; border-radius:5px; min-height:30px; }
            QPushButton:disabled { color: #718077; background: #1b231e; }
            QPushButton#primaryAction { background: #a0dfc0; color: #142017; font-weight: 700; border: 0; padding: 10px 16px; }
            QPushButton#primaryAction:hover { background: #b2df91; }
            QHeaderView::section { background: #202d25; color: #c5d5ca; border: 0; padding: 7px; }
        """)
        global DATA
        configured_data = os.environ.get("PIXELQSO_DATA_DIR")
        DATA = (Path(configured_data).expanduser() if configured_data else
                Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)))
        DATA.mkdir(parents=True, exist_ok=True)
        self.settings = QSettings(str(DATA / "settings.ini"), QSettings.Format.IniFormat)
        self.data2g_session = None
        self.data2g_modes = {}
        self.data2g_connecting = False
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
        self.data2g_host_connect = QPushButton("Connect and discover modes")
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
        self.station_grid = QLineEdit(os.environ.get("PIXELQSO_GRID", str(saved_grid)))
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
        self.backend_combo.currentIndexChanged.connect(self._on_backend_selection_changed)
        self.show_experimental_modes.toggled.connect(self._on_experimental_visibility_changed)
        self.audio_test_mode = QCheckBox("Audio-only test mode (PTT disabled)")
        self.audio_test_mode.setChecked(test_mode)
        self.audio_test_mode.toggled.connect(self.audio_test_mode_changed)
        self.card = default_card(self.station_call.text(), self.station_grid.text())
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
        self.session_wall_keys = set()
        self.session_wall_count = 0
        self.contact_stage = "idle"
        self.call_cq_active = False
        self.reply_report = None
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
        self.qso_log_dir = DATA / "qso-log"
        root = QWidget(); layout = QVBoxLayout(root)
        self.tabs = QTabWidget()
        editor_content = self.make_card_tab()
        self.editor_dialog = EditorDialog(self); self.editor_dialog.setWindowTitle("Create or edit card")
        editor_layout = QVBoxLayout(self.editor_dialog); editor_layout.addWidget(editor_content)
        self.editor_done_button = QPushButton("Save and close"); self.editor_done_button.setObjectName("primaryAction")
        self.editor_done_button.clicked.connect(self.editor_primary_action); editor_layout.addWidget(self.editor_done_button)
        self.editor_baseline = self.editor_signature()
        self.editor_dialog.resize(1080, 780)
        save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self.editor_dialog)
        save_shortcut.activated.connect(self.save_card)
        self.paste_image_shortcut = QShortcut(QKeySequence("Ctrl+V"), self.canvas)
        self.paste_image_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.paste_image_shortcut.activated.connect(self.paste_card_image)
        self.tabs.addTab(self.make_exchange_tab(), "QSO")
        self.tabs.addTab(self.make_library_tab(), "My Cards")
        self.tabs.addTab(self.make_qso_log_tab(), "QSO Log")
        layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self.refresh_card_library()
        self.refresh_qso_log()
        self._update_data2g_host_controls()
        if self.backend_combo.currentData() == "data2g":
            QTimer.singleShot(0, self._connect_data2g_host)
        else:
            if self._should_auto_connect_rig():
                QTimer.singleShot(0, self.connect_rig)
            QTimer.singleShot(250, self._ensure_session_receive)

    def make_library_tab(self):
        page = QWidget(); layout = QVBoxLayout(page)
        heading = QHBoxLayout(); heading.addWidget(QLabel("My cards"), 1)
        create = QPushButton("Create card"); create.clicked.connect(self.create_library_card); heading.addWidget(create)
        open_button = QPushButton("Open selected"); open_button.clicked.connect(self.open_selected_library_card); heading.addWidget(open_button)
        self.export_card_button = QPushButton("Export selected…")
        self.export_card_button.setEnabled(False)
        self.export_card_button.clicked.connect(self.export_selected_card)
        heading.addWidget(self.export_card_button)
        self.copy_card_button = QPushButton("Copy PNG")
        self.copy_card_button.clicked.connect(self.copy_selected_card)
        self.copy_card_button.setEnabled(False)
        heading.addWidget(self.copy_card_button)
        duplicate = QPushButton("Duplicate"); duplicate.clicked.connect(self.duplicate_library_card); heading.addWidget(duplicate)
        delete = QPushButton("Delete"); delete.clicked.connect(self.delete_library_card); heading.addWidget(delete)
        layout.addLayout(heading)
        self.library_search = QLineEdit(); self.library_search.setPlaceholderText("Find a card…")
        self.library_search.textChanged.connect(self.filter_card_library); layout.addWidget(self.library_search)
        self.library_list = QListWidget(); self.library_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.library_list.setIconSize(QSize(190, 238)); self.library_list.setGridSize(QSize(218, 288))
        self.library_list.setMovement(QListWidget.Movement.Static); self.library_list.setSpacing(8)
        self.library_list.itemClicked.connect(lambda _item: self._activate_library_list(self.library_list))
        self.library_list.itemSelectionChanged.connect(self._update_export_buttons)
        self.library_list.setResizeMode(QListWidget.ResizeMode.Adjust); self.library_list.itemDoubleClicked.connect(lambda _item: self.open_selected_library_card())
        layout.addWidget(self.library_list, 2)
        divider = QFrame(); divider.setFrameShape(QFrame.Shape.HLine); divider.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(divider)
        sent_heading = QLabel("Sent cards")
        sent_heading.setStyleSheet("font-weight:700;color:#c9e8a8;padding-top:4px")
        layout.addWidget(sent_heading)
        self.sent_library_list = QListWidget(); self.sent_library_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.sent_library_list.setIconSize(QSize(190, 238)); self.sent_library_list.setGridSize(QSize(218, 288))
        self.sent_library_list.setMovement(QListWidget.Movement.Static); self.sent_library_list.setSpacing(8)
        self.sent_library_list.itemClicked.connect(lambda _item: self._activate_library_list(self.sent_library_list))
        self.sent_library_list.itemSelectionChanged.connect(self._update_export_buttons)
        self.sent_library_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.sent_library_list.itemDoubleClicked.connect(lambda _item: self.open_selected_library_card())
        self.active_library_list = self.library_list
        layout.addWidget(self.sent_library_list, 1)
        self.library_status = QLabel(""); layout.addWidget(self.library_status)
        return page

    def make_qso_log_tab(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(16)
        heading = QLabel("Contacts")
        heading.setStyleSheet("font-size:24px;font-weight:700;color:#c9e8a8")
        layout.addWidget(heading)
        controls = QHBoxLayout()
        self.log_search = QLineEdit(); self.log_search.setPlaceholderText("Find a callsign or grid…")
        controls.addWidget(self.log_search, 1)
        self.log_sort = QComboBox(); self.log_sort.addItems(["Newest first", "Oldest first", "Callsign A–Z"])
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
        self.log_gallery.setSpacing(8); self.log_gallery.itemClicked.connect(self.show_log_deck)
        self.log_gallery.itemActivated.connect(self.show_log_deck)
        self.log_gallery.itemSelectionChanged.connect(self._update_export_buttons)
        layout.addWidget(self.log_gallery, 1)
        self.log_status = QLabel(); layout.addWidget(self.log_status)
        self.log_search.textChanged.connect(self.refresh_qso_log)
        self.log_sort.currentIndexChanged.connect(self.refresh_qso_log)
        return page

    def refresh_card_library(self):
        if not hasattr(self, "library_list"): return
        folder = DATA / "cards"; folder.mkdir(parents=True, exist_ok=True)
        sent_folder = DATA / "sent-cards"; sent_folder.mkdir(parents=True, exist_ok=True)
        selected = self.card_combo.currentData() if hasattr(self, "card_combo") else None
        stage_selected = {stage: combo.currentData() or self.settings.value("transfer/card_" + stage)
                          for stage, combo in getattr(self, "stage_card_combos", {}).items()}
        for combo in getattr(self, "stage_card_combos", {}).values(): combo.clear()
        if hasattr(self, "card_combo"): self.card_combo.clear()
        self.library_list.clear()
        self.sent_library_list.clear()
        for path, destination in [
            *((path, self.library_list) for path in sorted(folder.glob("*.json"), key=lambda p: p.name.lower())),
            *((path, self.sent_library_list) for path in sorted(sent_folder.glob("*.json"), key=lambda p: p.name.lower())),
        ]:
            try:
                data = json.loads(path.read_text()); card = modem.Card(data["callsign"], data["grid"], [tuple(c) for c in data["palette"]], data["pixels"], data.get("card_id", 0xC0DE), data.get("width", 32), data.get("height", 32))
                preview = QImage(card.width, card.height, QImage.Format.Format_RGB32)
                for i, index in enumerate(card.pixels): preview.setPixelColor(i % card.width, i // card.width, QColor(*(int(v)*17 for v in card.palette[index])))
                item = QListWidgetItem(f"{path.stem}\n{card.width} × {card.height}")
                section = "Sent cards" if destination is self.sent_library_list else "My cards"
                copied_from = data.get("copied_from") or {}
                origin = (f" · copied from {copied_from.get('callsign')} {copied_from.get('grid', '')}"
                          if copied_from else "")
                item.setToolTip(f"{section} · {path.stem} · {card.callsign} · {card.grid}{origin}")
                item.setIcon(QIcon(card_art(preview, card.callsign, card.grid, QSize(190, 238))))
                item.setData(Qt.ItemDataRole.UserRole, str(path)); destination.addItem(item)
                label = f"{path.stem} · {card.width}×{card.height}"
                icon = QIcon(QPixmap.fromImage(preview))
                if hasattr(self, "card_combo"):
                    self.card_combo.addItem(icon, label, str(path))
                for combo in getattr(self, "stage_card_combos", {}).values():
                    combo.addItem(icon, label, str(path))
            except (OSError, ValueError, KeyError, IndexError): continue
        if hasattr(self, "card_combo") and selected:
            index = self.card_combo.findData(selected)
            if index >= 0: self.card_combo.setCurrentIndex(index)
        for name, path in stage_selected.items():
            combo = self.stage_card_combos.get(name)
            if combo is not None and path:
                index = combo.findData(path)
                if index >= 0: combo.setCurrentIndex(index)
        self.filter_card_library()
        self._refresh_transfer_summary()

    def filter_card_library(self, *_):
        if not hasattr(self, "library_list"): return
        query = self.library_search.text().strip().casefold()
        visible = 0
        for widget in (self.library_list, self.sent_library_list):
            for row in range(widget.count()):
                item = widget.item(row)
                item.setHidden(bool(query and query not in item.toolTip().casefold()))
                visible += not item.isHidden()
        self.library_status.setText(f"{visible} cards" if visible else "No matching cards" if query else "No cards yet")

    def editor_signature(self):
        return (self.call.text(), self.grid.text(), self.canvas.snapshot())

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
        self.card.card_id = int(time.time_ns() & 0xFFFF)
        self.canvas.set_card(self.card)
        self.card_path = None
        self.editor_baseline = self.editor_signature()
        can_reply = self._reply_stage(self.reply_report) is not None
        self.editor_done_button.setText("Send reply" if can_reply else "Use this card")
        self.editor_dialog.setWindowTitle("Quick draw reply" if can_reply else "Quick draw card")
        self.editor_dialog.exec()
        if self.quick_draw_active:
            self.quick_draw_active = False
        self.editor_done_button.setText("Save and close")
        self.editor_dialog.setWindowTitle("Create or edit card")

    def send_quick_draw_reply(self):
        try:
            self.update_identity()
            self.card.validate()
            self.quick_reply_card = modem.Card(self.card.callsign, self.card.grid,
                list(self.card.palette), list(self.card.pixels), self.card.card_id,
                self.card.width, self.card.height)
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
        self.quick_draw_status.setText("Quick-draw card ready for the next manual send" if ready else "")
        self.clear_quick_draw_button.setVisible(ready)

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
                "height": card.height, "base_pixels": pixels, "stamp_layers": [],
                "archive_reason": "quick_draw_transmission"}
        serialized = json.dumps(data, indent=2) + "\n"
        target.write_text(serialized)
        (folders[1] / target.name).write_text(serialized)
        self.refresh_card_library()
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
        self.editor_dialog.exec()
        self.refresh_card_library()

    def open_selected_library_card(self):
        item, _widget = self._selected_library_item()
        if item:
            try:
                self._load_card_path(Path(item.data(Qt.ItemDataRole.UserRole)))
                self.editor_baseline = self.editor_signature()
                self.editor_dialog.exec()
                self.refresh_card_library()
            except (OSError, ValueError, KeyError) as exc:
                QMessageBox.warning(self, "Couldn't open card", str(exc))

    def delete_library_card(self):
        item, widget = self._selected_library_item()
        if not item: return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        section = "Sent cards" if widget is self.sent_library_list else "My Cards"
        if QMessageBox.question(self, "Delete card", f"Delete {item.text().splitlines()[0]} from {section}?") == QMessageBox.StandardButton.Yes:
            path.unlink(missing_ok=True); self.refresh_card_library()

    def _selected_library_item(self):
        for widget in (getattr(self, "active_library_list", self.library_list), self.sent_library_list, self.library_list):
            item = widget.currentItem()
            if item is not None:
                return item, widget
        return None, None

    def _activate_library_list(self, widget):
        self.active_library_list = widget

    def duplicate_library_card(self):
        item, source_widget = self._selected_library_item()
        if not item:
            return
        source = Path(item.data(Qt.ItemDataRole.UserRole))
        try:
            data = json.loads(source.read_text())
            modem.Card(data["callsign"], data["grid"], [tuple(c) for c in data["palette"]],
                       data["pixels"], data.get("card_id", 0xC0DE),
                       data.get("width", 32), data.get("height", 32)).validate()
            stem = f"{source.stem}-copy"
            target = source.with_name(stem + source.suffix)
            suffix = 2
            while target.exists():
                target = source.with_name(f"{stem}-{suffix}{source.suffix}")
                suffix += 1
            data["card_id"] = (int(data.get("card_id", 0xC0DE)) + 1) & 0xFFFF
            target.write_text(json.dumps(data, indent=2) + "\n")
            self.refresh_card_library()
            target_widget = self.sent_library_list if source_widget is self.sent_library_list else self.library_list
            for row in range(target_widget.count()):
                candidate = target_widget.item(row)
                if Path(candidate.data(Qt.ItemDataRole.UserRole)) == target:
                    candidate.setSelected(True)
                    target_widget.setCurrentItem(candidate)
                    break
            self.library_status.setText(f"Created {target.stem}; the original is unchanged")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, "Couldn't duplicate card", str(exc))

    def _load_card_path(self, path):
        d = json.loads(path.read_text())
        self.card = modem.Card(d["callsign"], d["grid"], [tuple(c) for c in d["palette"]], d["pixels"], d.get("card_id", 0xC0DE), d.get("width", 32), d.get("height", 32))
        self.card.validate(); self.card_path = path
        self.canvas.set_card(self.card, d.get("base_pixels", self.card.pixels), d.get("stamp_layers", []))
        self.call.setText(self.card.callsign); self.grid.setText(self.card.grid)
        self._set_editor_format(self.card)

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
                if query and query not in (str(entry.get("peer_callsign", "")) + " " + str(entry.get("peer_grid", ""))).casefold(): continue
                entries.append((path, entry))
            except (OSError, ValueError): continue
        sort = self.log_sort.currentIndex()
        entries.sort(key=lambda pair: str(pair[1].get("peer_callsign" if sort == 2 else "started_at", "")), reverse=sort == 0)
        for path, entry in entries:
            events = deck_events(entry)
            image = next((image for _direction, card in events if (image := self.card_image(card)) is not None), None)
            call = str(entry.get("peer_callsign", "Unknown"))
            item = QListWidgetItem(f"{entry.get('started_at', '')}\n{entry.get('status', 'Saved')} · {len(events)} cards")
            item.setIcon(QIcon(card_art(image, call, str(entry.get("peer_grid", "")) + " · CONTACT DECK", QSize(220, 275), stacked=True)))
            item.setData(Qt.ItemDataRole.UserRole, str(path)); item.setToolTip(f"Open {call}'s contact deck")
            self.log_gallery.addItem(item)
        self.log_status.setText(f"{len(entries)} contact decks" if entries else
                               "No matching contacts." if query else "Your first contact deck will appear here after you exchange cards.")
        self._update_export_buttons()

    def _update_export_buttons(self, *_):
        if hasattr(self, "export_card_button"):
            self.export_card_button.setEnabled(self._selected_library_item()[0] is not None)
            self.copy_card_button.setEnabled(self.export_card_button.isEnabled())
        if hasattr(self, "export_qso_button"):
            self.export_qso_button.setEnabled(
                hasattr(self, "log_gallery") and self.log_gallery.currentItem() is not None)
            self.copy_qso_button.setEnabled(self.export_qso_button.isEnabled())

    def copy_selected_card(self):
        item, _widget = self._selected_library_item()
        if item is None:
            return
        try:
            data = json.loads(Path(item.data(Qt.ItemDataRole.UserRole)).read_text())
            card = modem.Card(data["callsign"], data.get("grid", ""),
                              [tuple(color) for color in data["palette"]], data["pixels"],
                              data.get("card_id", 0xC0DE), data.get("width", 32), data.get("height", 32))
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
            entry = json.loads(Path(item.data(Qt.ItemDataRole.UserRole)).read_text())
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
                              int(data.get("width", 32)), int(data.get("height", 32)))
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
        source = Path(item.data(Qt.ItemDataRole.UserRole))
        try:
            entry = json.loads(source.read_text())
            callsign = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                               for ch in str(entry.get("peer_callsign", "Unknown")))
            date = "".join(ch for ch in str(entry.get("started_at", ""))[:10]
                           if ch.isalnum() or ch == "-")
            target_name, file_filter = QFileDialog.getSaveFileName(
                self, "Export QSO", str(DATA / "exports" / f"qso-{callsign}-{date}.png"),
                "PNG contact sheet (*.png);;Pixel QSO contact archive (*.json)")
            if not target_name:
                return
            target = Path(target_name)
            if target.resolve() == source.resolve():
                QMessageBox.warning(self, "Couldn't export QSO", "Choose a different file from the saved contact archive.")
                return
            if "PNG" in file_filter:
                target = target.with_suffix(".png")
                target.parent.mkdir(parents=True, exist_ok=True)
                if not deck_image(entry, self.card_image).save(str(target), "PNG"):
                    raise OSError("Qt could not write the PNG contact sheet")
            else:
                target = target.with_suffix(".json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
            self.log_status.setText(f"Exported QSO to {target.name}")
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't export QSO", str(exc))

    def show_log_deck(self, item=None, *_):
        item = item or self.log_gallery.currentItem()
        if item is None: return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError): return
        if not deck_events(entry):
            self.log_status.setText("This contact has no saved image cards yet."); return
        add_received = lambda card: self.add_received_card_to_library(card, path)
        DeckDialog(entry, self.card_image, add_received, self).exec()

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
        if len(palette) not in (8, 16, 32):
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
        card = modem.Card(callsign, grid, palette, pixels, int(time.time_ns() & 0xFFFF), width, height)
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
        drawing = QVBoxLayout(); outer.addLayout(drawing, 3)
        tools = QHBoxLayout(); self.tool_buttons = {}
        self.tool_group = QButtonGroup(self); self.tool_group.setExclusive(True)
        for title, tool in (("Draw", "draw"), ("Erase", "erase"), ("Fill", "fill"), ("Pick", "pick"), ("Move stamp", "move")):
            button = QPushButton(title); button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=tool: self.set_editor_tool(value))
            self.tool_group.addButton(button); self.tool_buttons[tool] = button; tools.addWidget(button)
        self.tool_buttons["draw"].setChecked(True)
        drawing.addLayout(tools)
        self.canvas = CardCanvas(self.card); drawing.addWidget(self.canvas, 1)
        history = QHBoxLayout()
        self.undo_button = QPushButton("↶ Undo"); self.undo_button.clicked.connect(self.canvas.undo)
        self.redo_button = QPushButton("↷ Redo"); self.redo_button.clicked.connect(self.canvas.redo)
        history.addWidget(self.undo_button); history.addWidget(self.redo_button); history.addStretch()
        grid = QCheckBox("Grid"); grid.setChecked(True)
        grid.toggled.connect(lambda checked: (setattr(self.canvas, "show_grid", checked), self.canvas.update()))
        history.addWidget(grid); drawing.addLayout(history)
        for key, callback in (("Ctrl+Z", self.canvas.undo), ("Ctrl+Shift+Z", self.canvas.redo), ("Ctrl+Y", self.canvas.redo)):
            shortcut = QShortcut(QKeySequence(key), w); shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut); shortcut.activated.connect(callback)

        sidebar = QWidget(); side = QVBoxLayout(sidebar); side.setContentsMargins(4, 0, 4, 0)
        sidebar_scroll = QScrollArea(); sidebar_scroll.setWidgetResizable(True); sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_scroll.setWidget(sidebar); sidebar_scroll.setMinimumWidth(350); sidebar_scroll.setMaximumWidth(440)
        outer.addWidget(sidebar_scroll, 2)
        form = QFormLayout()
        self.call = QLineEdit(self.card.callsign); self.grid = QLineEdit(self.card.grid)
        form.addRow("Call sign", self.call); form.addRow("Grid", self.grid)
        side.addLayout(form)
        format_form = QFormLayout()
        self.canvas_size_combo = QComboBox()
        for value in (16, 32, 64): self.canvas_size_combo.addItem(f"{value} × {value}", f"{value}x{value}")
        self.color_count_combo = QComboBox()
        for value in (8, 16, 32): self.color_count_combo.addItem(f"{value} colors", value)
        self.canvas_size_combo.currentIndexChanged.connect(self.change_card_format)
        self.color_count_combo.currentIndexChanged.connect(self.change_card_format)
        format_form.addRow("Canvas", self.canvas_size_combo); format_form.addRow("Palette", self.color_count_combo)
        side.addLayout(format_form)
        self.paint_color_combo = QComboBox(); self.paint_color_combo.currentIndexChanged.connect(self.select_color)
        self.paint_color_combo.setVisible(False)
        self.palette_widget = QWidget(); self.palette_layout = QGridLayout(self.palette_widget)
        self.palette_layout.setContentsMargins(0, 0, 0, 0); self.palette_layout.setSpacing(6)
        side.addWidget(self.palette_widget)
        self._set_editor_format(self.card)
        side.addWidget(QLabel("My stamps"))
        self.stamp_list = StampShelf(); self.stamp_list.setMaximumHeight(180)
        self.stamp_list.setViewMode(QListWidget.ViewMode.IconMode); self.stamp_list.setIconSize(QSize(48, 48))
        self.stamp_list.setGridSize(QSize(82, 78)); self.stamp_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.stamp_list.itemDoubleClicked.connect(lambda _item: self.stamp_selected())
        side.addWidget(self.stamp_list)
        stamp_buttons = QHBoxLayout()
        make_stamp = QPushButton("Text / emoji"); make_stamp.clicked.connect(self.create_text_stamp); stamp_buttons.addWidget(make_stamp)
        draw_stamp = QPushButton("Draw stamp"); draw_stamp.clicked.connect(self.draw_stamp); stamp_buttons.addWidget(draw_stamp)
        import_stamp = QPushButton("Import"); import_stamp.clicked.connect(self.import_stamp); stamp_buttons.addWidget(import_stamp)
        side.addLayout(stamp_buttons)
        stamp_actions = QHBoxLayout()
        edit_stamp = QPushButton("Edit stamp…"); edit_stamp.clicked.connect(self.edit_stamp); stamp_actions.addWidget(edit_stamp)
        remove_stamp = QPushButton("Delete saved stamp"); remove_stamp.clicked.connect(self.remove_stamp); stamp_actions.addWidget(remove_stamp)
        side.addLayout(stamp_actions)
        stamp_size_row = QHBoxLayout()
        self.placed_stamp_width = QSpinBox(); self.placed_stamp_width.setRange(1, 64); self.placed_stamp_width.setSuffix(" px")
        self.resize_stamp_button = QPushButton("Resize placed stamp")
        self.resize_stamp_button.clicked.connect(self.resize_placed_stamp)
        stamp_size_row.addWidget(self.placed_stamp_width); stamp_size_row.addWidget(self.resize_stamp_button)
        side.addLayout(stamp_size_row)
        stamp = QPushButton("Place selected stamp"); stamp.clicked.connect(self.stamp_selected); side.addWidget(stamp)
        remove_placed = QPushButton("Remove placed stamp"); remove_placed.clicked.connect(self.remove_placed_stamp); side.addWidget(remove_placed)
        self.load_stamps()
        buttons = QHBoxLayout()
        for label, handler in (("New", self.new_card), ("Open…", self.open_card), ("Save", self.save_card), ("Paste image", self.paste_card_image), ("Export WAV…", self.export_wav)):
            b = QPushButton(label); b.clicked.connect(handler); buttons.addWidget(b)
        side.addLayout(buttons)
        side.addStretch()
        self.status = QLabel("Ready")
        side.addWidget(self.status)
        self.canvas.image_dropped.connect(self.place_stamp_image)
        self.canvas.changed.connect(self._sync_editor_controls)
        self.canvas.placed_stamp_removed.connect(
            lambda: self.status.setText("Placed stamp removed from the card."))
        self._sync_editor_controls()
        return w

    def set_editor_tool(self, tool):
        self.canvas.tool = tool
        self.tool_buttons[tool].setChecked(True)

    def _sync_editor_controls(self):
        self.undo_button.setEnabled(bool(self.canvas.undo_stack))
        self.redo_button.setEnabled(bool(self.canvas.redo_stack))
        self.tool_buttons[self.canvas.tool].setChecked(True)
        index = self.canvas.selected_layer
        selected = index is not None and index < len(self.canvas.stamp_layers)
        self.placed_stamp_width.setEnabled(selected); self.resize_stamp_button.setEnabled(selected)
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
        self._refresh_paint_colors()

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
            button.setToolTip(f"Color {index+1} · {color}")
            border = "#e1f3cb" if index == self.canvas.color_index else "#45574e"
            button.setStyleSheet(f"background:{color};border:2px solid {border};border-radius:7px;padding:0")
            button.clicked.connect(lambda _checked=False, value=index: (self.select_color(value), self._refresh_paint_colors()))
            self.palette_layout.addWidget(button, index//8, index%8)

    def change_card_format(self, *_):
        if not hasattr(self, "canvas_size_combo"):
            return
        size = str(self.canvas_size_combo.currentData() or "32x32").split("x", 1)
        width, height = int(size[0]), int(size[1])
        color_count = int(self.color_count_combo.currentData() or 8)
        palette_id = {8: 0, 16: 1, 32: 2}[color_count]
        self.canvas.change_format(width, height, list(modem.AVATAR_PALETTES[palette_id]))
        self._refresh_paint_colors()

    def load_stamps(self):
        folder = DATA / "stamps"; folder.mkdir(parents=True, exist_ok=True)
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
        size_button = QPushButton("New size")
        color = QComboBox()
        for index, rgb in enumerate(PALETTE):
            color.addItem(f"Color {index+1}"); color.setItemData(index, QColor(*(channel*17 for channel in rgb)), Qt.ItemDataRole.DecorationRole)
        controls.addWidget(QLabel("Width")); controls.addWidget(width)
        controls.addWidget(QLabel("Height")); controls.addWidget(height)
        controls.addWidget(size_button); controls.addWidget(QLabel("Brush")); controls.addWidget(color)
        layout.addLayout(controls)
        canvas = StampDrawingCanvas(image, QColor(*(channel*17 for channel in PALETTE[1])))
        layout.addWidget(canvas, 1)
        canvas.setToolTip("Left mouse draws; right mouse erases to transparency.")
        color.currentIndexChanged.connect(lambda index: setattr(canvas, "color", QColor(*(channel*17 for channel in PALETTE[index]))))

        def resize_canvas():
            new_image = QImage(width.value(), height.value(), QImage.Format.Format_ARGB32)
            new_image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(new_image); painter.drawImage(0, 0, canvas.image); painter.end()
            canvas.image = new_image; canvas.update()

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
        self.status.setText("Stamp placed · drag to move")

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

    def update_identity(self):
        self.card.callsign = self.call.text().strip().upper()
        self.card.grid = self.grid.text().strip().upper()

    def new_card(self):
        if hasattr(self, "editor_dialog") and self.editor_dialog.isVisible() and not self.confirm_editor_close(): return
        self.card = default_card(self.station_call.text().strip().upper() or "N0CALL",
                                 self.station_grid.text().strip().upper() or "AA00")
        self.canvas.set_card(self.card)
        self.call.setText(self.card.callsign); self.grid.setText(self.card.grid)
        self._set_editor_format(self.card)
        self.card_path = None
        self.status.setText("New card")
        self.editor_baseline = self.editor_signature()

    def save_card(self):
        self.update_identity()
        try:
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
                "width": self.card.width, "height": self.card.height,
                "base_pixels": self.canvas.base_pixels, "stamp_layers": self.canvas.layer_data()}, indent=2))
            self.card_path = target
            self.editor_baseline = self.editor_signature()
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
        w = QWidget(); root = QVBoxLayout(w); root.setContentsMargins(14, 14, 14, 14); root.setSpacing(12)
        top = QHBoxLayout()
        self.station_button = QPushButton("Station settings"); self.station_button.clicked.connect(self.open_station_settings); top.addWidget(self.station_button)
        self.rig_status = QLabel("CAT disconnected"); self.rig_status.setStyleSheet("color:#9eada3"); top.addWidget(self.rig_status, 1)
        self.test_mode_banner = QLabel("AUDIO TEST · PTT DISABLED")
        self.test_mode_banner.setStyleSheet("background:#76501b;color:#fff3d4;padding:6px 10px;border-radius:7px;font-weight:700")
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
        self.exchange_mode = QComboBox(); self.exchange_mode.addItems(["Manual exchange", "Automatic exchange", "Beacon"])
        self.exchange_role = QComboBox(); self.exchange_role.addItems(["Call-first", "Reply to CQ"])
        self.exchange_mode.currentIndexChanged.connect(self._update_exchange_controls)
        self.exchange_mode.currentIndexChanged.connect(self._refresh_transfer_summary)
        self.exchange_role.currentIndexChanged.connect(self._refresh_transfer_summary)
        self.profile_combo = QComboBox()
        for profile in modem.PROFILES: self.profile_combo.addItem(profile.label, profile.key)
        configured_profile = os.environ.get("PIXELQSO_PROFILE", modem.DEFAULT_PROFILE)
        profile_index = self.profile_combo.findData(configured_profile)
        self.profile_combo.setCurrentIndex(profile_index if profile_index >= 0 else self.profile_combo.findData(modem.DEFAULT_PROFILE))
        self.profile_form_widget = QWidget(); profile_form = QFormLayout(self.profile_form_widget)
        profile_form.setContentsMargins(0, 0, 0, 0); profile_form.addRow("Bandwidth", self.profile_combo)
        self.mode_combo = QComboBox()
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
        mode_form = QFormLayout()
        mode_form.addRow("Modem backend", self.backend_combo)
        mode_form.addRow("Transmission type", self.mode_combo)
        self.grid_tx_latency = QSpinBox()
        self.grid_tx_latency.setRange(0, 2000)
        self.grid_tx_latency.setSuffix(" ms")
        self.grid_tx_latency.setValue(self.settings.value("grid/tx_latency_ms", 0, type=int))
        self.grid_tx_latency.setToolTip("UTC grid test modem only: measured output delay from queued audio to radio audio. Calibrate with a loopback capture.")
        self.grid_tx_latency.valueChanged.connect(lambda value: self.settings.setValue("grid/tx_latency_ms", value))
        mode_form.addRow("Grid TX audio delay", self.grid_tx_latency)
        self.grid_rx_latency = QSpinBox()
        self.grid_rx_latency.setRange(0, 2000)
        self.grid_rx_latency.setSuffix(" ms")
        self.grid_rx_latency.setValue(self.settings.value("grid/rx_latency_ms", 0, type=int))
        self.grid_rx_latency.setToolTip("UTC grid acquisition: measured input startup delay. Timing remains approximate; full acquisition is retained.")
        self.grid_rx_latency.valueChanged.connect(lambda value: self.settings.setValue("grid/rx_latency_ms", value))
        mode_form.addRow("Grid RX audio delay", self.grid_rx_latency)
        self.receive_all_modes = QCheckBox("Receive all local audio modem types")
        self.receive_all_modes.setChecked(self.settings.value("transfer/receive_all", True, type=bool))
        self.receive_all_modes.setToolTip(
            "This applies to Pixel QSO's local audio decoders. Data2G receives decoded card frames from its host.")
        self.receive_all_modes.toggled.connect(lambda value: self.settings.setValue("transfer/receive_all", value))
        mode_form.addRow("Receive", self.receive_all_modes)
        self.receive_all_modes_label = mode_form.labelForField(self.receive_all_modes)
        # Retain saved repetition and mode limits without exposing another
        # main-panel choice. Single-burst modes still clamp this to one.
        self.repeat_count = QSpinBox(self); self.repeat_count.hide()
        self.repeat_count.setRange(1, 20); self.repeat_count.setValue(int(self.settings.value("transfer/repeats", 3)))
        self.repeat_count.setToolTip("Send copies of the same card; RX combines soft evidence before integrity checks.")
        self.repeat_count.valueChanged.connect(lambda value: self.settings.setValue("transfer/repeats", value))
        self.repeat_count.valueChanged.connect(self._refresh_transfer_summary)
        self.card_combo = QComboBox()
        self.card_combo.setIconSize(QSize(42, 42))
        self.card_combo.currentIndexChanged.connect(self._refresh_transfer_summary)
        self.manual_card_widget = QWidget(); manual_card_layout = QHBoxLayout(self.manual_card_widget)
        manual_card_layout.setContentsMargins(0, 0, 0, 0); manual_card_layout.addWidget(self.card_combo)
        mode_form.addRow("Card", self.manual_card_widget)
        self.manual_card_label = mode_form.labelForField(self.manual_card_widget)
        self.auto_stage_group = QGroupBox("Contact cards")
        auto_stage_form = QFormLayout(self.auto_stage_group)
        self.stage_card_combos = {}
        self.manual_stage_group = QButtonGroup(self); self.manual_stage_group.setExclusive(True)
        self.manual_stage_buttons = {}
        for stage, title in (("cq", "CQ"), ("exchange", "Exchange"),
                              ("report73", "RR73 · report"), ("final73", "73 · final")):
            combo = QComboBox(); combo.setIconSize(QSize(48, 48))
            combo.currentIndexChanged.connect(self._save_auto_stage_cards)
            combo.currentIndexChanged.connect(self._refresh_transfer_summary)
            self.stage_card_combos[stage] = combo
            button = QPushButton(title); button.setCheckable(True)
            button.setProperty("stage", stage)
            button.setToolTip(f"Choose {title} as the manual send stage")
            button.setStyleSheet("QPushButton { text-align: left; padding: 6px 9px; } QPushButton:checked { background: #176f64; border-color: #58c9ad; color: #ffffff; }")
            self.manual_stage_group.addButton(button, len(self.manual_stage_buttons))
            self.manual_stage_buttons[stage] = button
            auto_stage_form.addRow(button, combo)
        self.manual_stage_buttons["exchange"].setChecked(True)
        self.manual_stage_group.buttonToggled.connect(self._refresh_transfer_summary)
        self.role_widget = QWidget(); role_form = QFormLayout(self.role_widget); role_form.setContentsMargins(0, 0, 0, 0)
        role_form.addRow("Role", self.exchange_role)
        self.frequency = QSpinBox(); self.frequency.setRange(100_000, 1_300_000_000); self.frequency.setValue(14_074_000); self.frequency.setSuffix(" Hz")
        f = QFormLayout(); f.addRow("Rig frequency", self.frequency)
        self.frequency_controls = QWidget(); self.frequency_controls.setLayout(f)
        transfer_group = QGroupBox("Send a card")
        transfer_form = QFormLayout(transfer_group); transfer_form.addRow("Exchange", self.exchange_mode)
        transfer_form.addRow(mode_form); transfer_form.addRow(self.profile_form_widget)
        transfer_form.addRow(self.role_widget); transfer_form.addRow(self.auto_stage_group)
        transfer_form.addRow(self.frequency_controls)
        self.exchange_status = QLabel("CQ → Reply / Exchange → RR73 → 73\nCall CQ, or click Reply on a received CQ to start automatic sequencing.")
        self.exchange_status.setWordWrap(True)
        transfer_form.addRow(self.exchange_status)
        self.transfer_summary = QLabel(); self.transfer_summary.setWordWrap(True); self.transfer_summary.setStyleSheet("color:#a9b8ae; padding:4px")
        transfer_form.addRow(self.transfer_summary)
        self.tx_btn = QPushButton("Send selected stage"); self.tx_btn.setObjectName("primaryAction"); self.tx_btn.clicked.connect(self.transmit_exchange)
        self.stop_tx_btn = QPushButton("Stop TX"); self.stop_tx_btn.setEnabled(False); self.stop_tx_btn.setVisible(False); self.stop_tx_btn.clicked.connect(self._stop_current_transmit)
        tx_row = QHBoxLayout(); tx_row.addWidget(self.tx_btn); tx_row.addWidget(self.stop_tx_btn)
        self.manual_tx_controls = QWidget(); self.manual_tx_controls.setLayout(tx_row); transfer_form.addRow(self.manual_tx_controls)
        auto_row = QHBoxLayout()
        self.auto_start_btn = QPushButton("Start automatic exchange"); self.auto_start_btn.clicked.connect(self.start_auto_exchange); auto_row.addWidget(self.auto_start_btn)
        self.auto_stop_btn = QPushButton("Stop exchange"); self.auto_stop_btn.setEnabled(False); self.auto_stop_btn.setVisible(False); self.auto_stop_btn.clicked.connect(lambda _checked=False: self.stop_auto_exchange()); auto_row.addWidget(self.auto_stop_btn)
        self.auto_controls = QWidget(); self.auto_controls.setLayout(auto_row); transfer_form.addRow(self.auto_controls)
        self.call_cq_button = QPushButton("Call CQ")
        self.call_cq_button.setCheckable(True)
        self.call_cq_button.toggled.connect(self._toggle_call_cq)
        self.cq_interval = QSpinBox(); self.cq_interval.setRange(1, 3600)
        self.cq_interval.setValue(int(self.settings.value("transfer/cq_interval_seconds", 5)))
        self.cq_interval.setSuffix(" s")
        self.cq_interval.valueChanged.connect(lambda value: self.settings.setValue("transfer/cq_interval_seconds", value))
        cq_row = QHBoxLayout(); cq_row.addWidget(self.call_cq_button); cq_row.addWidget(QLabel("Repeat every")); cq_row.addWidget(self.cq_interval)
        self.cq_controls = QWidget(); self.cq_controls.setLayout(cq_row); transfer_form.addRow(self.cq_controls)
        self.beacon_timer = QTimer(self)
        self.beacon_timer.timeout.connect(self.transmit_beacon)
        self.beacon_interval = QSpinBox(); self.beacon_interval.setRange(1, 1440)
        self.beacon_interval.setValue(int(self.settings.value("transfer/beacon_interval_minutes", 5)))
        self.beacon_interval.setSuffix(" min")
        self.beacon_interval.valueChanged.connect(lambda value: self.settings.setValue("transfer/beacon_interval_minutes", value))
        self.beacon_button = QPushButton("Start beacon"); self.beacon_button.setCheckable(True)
        self.beacon_button.toggled.connect(self._toggle_beacon)
        beacon_form = QFormLayout(); beacon_form.addRow("Send card every", self.beacon_interval); beacon_form.addRow(self.beacon_button)
        self.beacon_controls = QWidget(); self.beacon_controls.setLayout(beacon_form); transfer_form.addRow(self.beacon_controls)
        self._update_transfer_controls()
        self._refresh_transfer_summary()

        wav_button = QPushButton("Decode audio file…"); wav_button.clicked.connect(self.decode_wav)
        receive_group = QGroupBox("Receive")
        receive_layout = QHBoxLayout(receive_group)
        self.receive_status_hint = QLabel()
        self.receive_status_hint.setWordWrap(True)
        receive_layout.addWidget(self.receive_status_hint, 1)
        receive_layout.addWidget(wav_button)

        left_panel = QVBoxLayout(); left_panel.setContentsMargins(0, 0, 0, 0); left_panel.addWidget(transfer_group); left_panel.addStretch()
        left = QWidget(); left.setLayout(left_panel); left.setMaximumWidth(410)

        right_panel = QVBoxLayout()
        view_heading = QHBoxLayout()
        view_heading.addWidget(QLabel("Cards heard this session"), 1)
        self.listen_indicator = QLabel("● LISTENING")
        self.listen_indicator.setStyleSheet("color:#8bd8ae;font-weight:700;letter-spacing:1px")
        view_heading.addWidget(self.listen_indicator)
        right_panel.addLayout(view_heading)
        self.rx_waterfall = RxAudioWaterfall()
        self.rx_visual_stack = QStackedWidget()
        self.rx_waterfall_unavailable = QLabel(
            "Audio waterfall unavailable · Data2G receives decoded frames from its host")
        self.rx_waterfall_unavailable.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rx_waterfall_unavailable.setStyleSheet(
            "background:#0c110e;color:#809087;border:1px solid #2e3d45;border-radius:8px;padding:10px")
        self.rx_visual_stack.addWidget(self.rx_waterfall)
        self.rx_visual_stack.addWidget(self.rx_waterfall_unavailable)
        right_panel.addWidget(self.rx_visual_stack)
        self.receive_stack = QStackedWidget()
        self.session_wall_page = QWidget()
        wall_layout = QHBoxLayout(self.session_wall_page); wall_layout.setContentsMargins(0, 0, 0, 0); wall_layout.setSpacing(10)
        wall_list_panel = QWidget()
        wall_list_layout = QVBoxLayout(wall_list_panel); wall_list_layout.setContentsMargins(0, 0, 0, 0)
        self.session_wall_empty = QLabel("Your session wall is empty.\nVerified cards received on this frequency will appear here.")
        self.session_wall_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.session_wall_empty.setStyleSheet("background:#0c110e;color:#809087;border:1px solid #2e3d45;border-radius:10px;padding:24px;font-size:16px")
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
        self.session_wall.setStyleSheet("QListWidget { background:#0c110e;border:1px solid #2e3d45;border-radius:10px;padding:4px; } QListWidget::item { padding:0; margin:0; }")
        self.session_wall.hide()
        wall_list_layout.addWidget(self.session_wall, 1)
        wall_layout.addWidget(wall_list_panel, 1)
        self.receive_preview_box = QGroupBox("Live preview")
        self.receive_preview_box.setFixedWidth(218)
        self.receive_preview_box.setToolTip(
            "Received pixels remain visible provisionally even when a block or whole-card CRC fails. "
            "Later copies can refine them; only CRC-verified pixels are accepted as final.")
        preview_layout = QVBoxLayout(self.receive_preview_box)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        self.receive_stack.addWidget(self.session_wall_page)
        self.receive_view = QLabel("Listening for a card")
        self.receive_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.receive_view.setFixedSize(194, 194)
        self.receive_view.setStyleSheet("background:#0c110e;color:#809087;border:1px solid #2e3d45;border-radius:10px")
        preview_layout.addWidget(self.receive_view, 0, Qt.AlignmentFlag.AlignCenter)
        self.receive_progress = QProgressBar()
        self.receive_progress.setRange(0, 100)
        self.receive_progress.setValue(0)
        self.receive_progress.setFormat("%p% received")
        self.receive_progress.setFixedWidth(184)
        self.receive_progress.setFixedHeight(14)
        self.receive_progress.setStyleSheet("QProgressBar { border:1px solid #35464e; border-radius:5px; background:#0c110e; color:#dce8df; text-align:center; font-size:10px; } QProgressBar::chunk { background:#36a879; border-radius:4px; }")
        preview_layout.addWidget(self.receive_progress, 0, Qt.AlignmentFlag.AlignCenter)
        self.receive_preview_box.hide()
        wall_layout.addWidget(self.receive_preview_box, 0)
        self.receive_stack.setCurrentWidget(self.session_wall_page)
        right_panel.addWidget(self.receive_stack, 1)
        self.rx_status = QLabel("Ready to receive"); self.rx_status.setStyleSheet("color:#b7c8bd; padding:2px")
        right_panel.addWidget(self.rx_status)
        right_panel.addWidget(receive_group)
        self.reply_to_button = QPushButton("Reply to CQ")
        self.reply_to_button.setVisible(False)
        self.reply_to_button.clicked.connect(self.reply_to_received)
        right_panel.addWidget(self.reply_to_button)
        self.quick_draw_button = QPushButton("Quick draw")
        self.quick_draw_button.setToolTip("Sketch a card while receiving or transmitting; use it for the next manual send.")
        self.quick_draw_button.clicked.connect(self.start_quick_draw_reply)
        self.quick_draw_status = QLabel("")
        self.quick_draw_status.setStyleSheet("color:#a9d8bb;padding:2px")
        self.clear_quick_draw_button = QPushButton("Clear quick draw")
        self.clear_quick_draw_button.clicked.connect(self.clear_quick_draw)
        quick_draw_row = QHBoxLayout()
        quick_draw_row.addWidget(self.quick_draw_button)
        quick_draw_row.addWidget(self.quick_draw_status, 1)
        quick_draw_row.addWidget(self.clear_quick_draw_button)
        right_panel.addLayout(quick_draw_row)
        self.exchange_log = QTextEdit(); self.exchange_log.setReadOnly(True); self.exchange_log.setPlaceholderText("Exchange activity"); self.exchange_log.setFixedHeight(92)
        self.exchange_log.document().setMaximumBlockCount(2000)
        right_panel.addWidget(self.exchange_log)
        right = QWidget(); right.setLayout(right_panel)
        left_scroll = QScrollArea(); left_scroll.setWidgetResizable(True); left_scroll.setWidget(left)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame); left_scroll.setMinimumWidth(410); left_scroll.setMaximumWidth(440)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_column = QWidget(); left_column.setMaximumWidth(440)
        left_controls = QVBoxLayout(left_column); left_controls.setContentsMargins(0, 0, 0, 0)
        left_controls.addWidget(left_scroll, 1)
        for controls in (self.manual_tx_controls, self.auto_controls, self.cq_controls, self.beacon_controls):
            transfer_form.removeWidget(controls); left_controls.addWidget(controls)
        content = QHBoxLayout(); content.addWidget(left_column); content.addWidget(right, 1)
        root.addLayout(content, 1)
        self.combine = QLineEdit(str(DATA / f"receive-session-v{modem.VERSION}.json")); self.combine.setVisible(False)
        self._update_exchange_controls()
        self.audio_test_mode_changed(self.audio_test_mode.isChecked())
        return w

    def open_station_settings(self):
        if getattr(self, "station_dialog", None) is not None:
            self.station_dialog.exec()
            return
        dialog = QDialog(self); self.station_dialog = dialog; dialog.setWindowTitle("Station settings")
        layout = QVBoxLayout(dialog)
        options = QGroupBox("Options")
        options_form = QFormLayout(options)
        options_form.addRow("My call sign", self.station_call)
        options_form.addRow("My grid square", self.station_grid)
        options_form.addRow("Experimental audio test", self.audio_test_mode)
        options_form.addRow("Advanced modes", self.show_experimental_modes)
        options_form.addRow("Copies per card", self.repeat_count)
        self.repeat_count.show()
        self.repeat_count_label = options_form.labelForField(self.repeat_count)
        options_form.addRow("Mobile control", self.web_enabled)
        options_form.addRow("Web port", self.web_port)
        layout.addWidget(options)

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
        layout.addWidget(self.data2g_settings_group)
        station = QGroupBox("Experimental modem audio")
        station_form = QFormLayout(station)
        station_form.addRow("Audio output", self.output_device); station_form.addRow("Audio input", self.input_device)
        layout.addWidget(station)
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
        cat_note.setStyleSheet("color:#9eada3;padding:2px")
        cat_form.addRow(cat_note)
        cat_form.addRow(self.auto_connect_cat)
        layout.addWidget(self.cat_settings_group)
        self._update_data2g_host_controls()
        self._update_backend_ui()
        done = QPushButton("Done"); done.clicked.connect(self.save_station_settings); done.clicked.connect(dialog.accept); layout.addWidget(done)
        dialog.exec()

    def _update_data2g_host_controls(self, *_):
        if not hasattr(self, "data2g_host_input"):
            return
        remote = self.data2g_host_source.currentData() != "local"
        server_running = bool(self.data2g_local_process and
                              self.data2g_local_process.poll() is None)
        busy = (self.data2g_connecting or self.data2g_local_starting or self.data2g_closing or
                self.data2g_handoff_pending or
                DATA2G_ADAPTER.is_connected(self.data2g_session) or server_running)
        self.data2g_host_source.setEnabled(not busy)
        self.data2g_host_input.setEnabled(remote and not busy)
        self.data2g_command_port.setEnabled(not busy)
        self.data2g_kiss_port.setEnabled(not busy)
        self.data2g_host_bandwidth.setEnabled(not remote and not busy)
        connected = bool(DATA2G_ADAPTER.is_connected(self.data2g_session))
        local_audio_ready = (remote or not self.data2g_audio_config_errors)
        self.data2g_host_connect.setEnabled(
            not self.data2g_connecting and not self.data2g_local_starting and
            not self.data2g_closing and
            not self.data2g_handoff_pending and
            (connected or local_audio_ready) and
            (self.backend_combo.currentData() == "data2g" or connected))
        self.data2g_host_connect.setToolTip(
            self._data2g_audio_config_error_message()
            if not local_audio_ready else "")
        if hasattr(self, "data2g_radio_group"):
            self.data2g_radio_group.setVisible(not remote)
            self.data2g_radio_group.setEnabled(not busy)
        if hasattr(self, "data2g_input_device"):
            self.data2g_input_device.setEnabled(not remote and not busy)
            self.data2g_output_device.setEnabled(not remote and not busy)
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
            args = local_host_arguments(
                command_port=self.data2g_command_port.value(),
                kiss_port=self.data2g_kiss_port.value(),
                callsign=self.station_call.text(),
                input_device=(data2g_audio_device_selector(
                    self.data2g_input_device.currentData(), self.data2g_input_device.currentText())
                    if self.data2g_input_device.count() else ""),
                output_device=(data2g_audio_device_selector(
                    self.data2g_output_device.currentData(), self.data2g_output_device.currentText())
                    if self.data2g_output_device.count() else ""),
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
            self.data2g_local_process = subprocess.Popen(
                local_host_command(args), cwd=str(DATA), stdout=self.data2g_local_log,
                stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))
        except OSError as exc:
            self.data2g_local_log.close()
            self.data2g_local_log = None
            self.data2g_host_status.setText(f"Could not start local Data2G host: {exc}")
            return False
        self.data2g_local_log_monitor_offset = self.data2g_local_log_offset
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
        if (self.backend_combo.currentData() != "data2g" or
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
        if self.rig.socket.state() != QTcpSocket.SocketState.UnconnectedState:
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
            " · host audio/PTT readiness unverified")
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
        if self.receive_activity_pending:
            self._hide_receive_preview()
        if self.backend_combo.currentData() == "data2g":
            self.listen_indicator.setText("● HOST DISCONNECTED")
        self.data2g_modes = {}
        register_data2g_mode_backends(self.data2g_modes)
        self.data2g_host_connect.setEnabled(True)
        self.data2g_host_connect.setText("Connect and discover modes")
        self.data2g_host_status.setText(
            "Host disconnected · " + self.data2g_last_host_error
            if self.data2g_last_host_error else "Not connected")
        self._refresh_mode_options()
        self._stop_local_data2g_host()
        self._update_data2g_host_controls()
        self.refresh_tx_button()
        if (not self._closing and self.backend_combo.currentData() == "experimental"
                and not self.data2g_ptt_failed and
                self._should_auto_connect_rig() and not self.rig.connected()):
            QTimer.singleShot(0, self.connect_rig)
        if (not self._closing and
                self.backend_combo.currentData() == "experimental" and
                not self.rx_timer.isActive()):
            QTimer.singleShot(0, self._ensure_session_receive)

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
        if not enabled and self.backend_combo.currentData() == "experimental":
            self.backend_combo.setCurrentIndex(0)
        self._refresh_mode_options()
        self._update_backend_ui()

    def _on_backend_selection_changed(self, *_):
        if not hasattr(self, "mode_combo"):
            return
        data2g = self.backend_combo.currentData() == "data2g"
        if not data2g and not self._confirm_data2g_ptt_recovery():
            with QSignalBlocker(self.backend_combo):
                self.backend_combo.setCurrentIndex(self.backend_combo.findData("data2g"))
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
                           (DATA2G_ADAPTER.is_connected(self.data2g_session)))
        if (not data2g and not host_active and self._should_auto_connect_rig()
                and not self.rig.connected()):
            QTimer.singleShot(0, self.connect_rig)
        self._update_transfer_controls()
        self._refresh_transfer_summary()
        self.audio_test_mode_changed(self.audio_test_mode.isChecked())
        self.refresh_tx_button()

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
            if fields[2] == "HEARD":
                snapshot = self.card_assembly.preview_snapshot()
                if not snapshot or not snapshot.card.get("exact"):
                    self._show_receive_activity(
                        f"Receiving Data2G burst from {fields[3] if len(fields) > 3 else 'a station'} · checking card frames")
                    self.receive_activity_timeout.start(15000)
            elif fields[2] in {"LOST", "DROPPED"}:
                self.rx_status.setText("Data2G lost card frames · waiting for another copy")
        if message.startswith(("BCAST ", "PTT ", "BUSY ", "BUFFER ", "MODE ")):
            self.exchange_log.append("Data2G: " + message)
            if self.on_air:
                self.on_air.emit("host_status", {"message": message})

    def _on_mode_selection_changed(self, *_):
        backend = self.selected_backend()
        if self.selected_adapter().capabilities.kind is AdapterKind.FRAME_TRANSPORT:
            if backend is None or not getattr(backend, "usable", False):
                if backend is not None and getattr(backend, "disabled_reason", None):
                    self.rx_status.setText(backend.disabled_reason)
                elif not self.data2g_connecting and not (DATA2G_ADAPTER.is_connected(self.data2g_session)):
                    self._connect_data2g_host()
                self.refresh_tx_button()
                self._update_backend_ui()
                return
            if self.rig.connected():
                self.rig.disconnect()
            if DATA2G_ADAPTER.is_connected(self.data2g_session):
                DATA2G_ADAPTER.select_mode(self.data2g_session, backend.mode_name)
            elif not self.data2g_connecting:
                self._connect_data2g_host()
            if self.rx_timer.isActive():
                self._discard_receive_for_tx()
            self.listen_indicator.setText("● DATA2G HOST RX")
            self.listen_indicator.setStyleSheet("color:#8bd8ae;font-weight:700;letter-spacing:1px")
            self.rx_status.setText("Receiving KISS card frames through the Data2G host")
        elif (self.data2g_connecting or
              DATA2G_ADAPTER.is_connected(self.data2g_session)):
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

    def _resume_experimental_backend(self):
        """Acquire local CAT/audio only after the Data2G owner has exited."""
        if (self._closing or self.backend_combo.currentData() != "experimental" or
                self.data2g_connecting or self.data2g_local_starting or self.data2g_closing or
                DATA2G_ADAPTER.is_connected(self.data2g_session)):
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
        capabilities = self.selected_adapter().capabilities
        selected_data2g = capabilities.kind is AdapterKind.FRAME_TRANSPORT
        if hasattr(self, 'repeat_count_label'):
            self.repeat_count.setVisible(not selected_data2g)
            self.repeat_count_label.setVisible(not selected_data2g)
        if hasattr(self, "audio_test_mode"):
            self.audio_test_mode.setEnabled(not selected_data2g)
            self.audio_test_mode.setToolTip(
                "Applies only to Experimental modems. Data2G transmission uses the host's radio/PTT settings.")
        host_busy = bool(self.data2g_connecting or self.data2g_local_starting or self.data2g_closing or
                         self.data2g_handoff_pending or
                         (DATA2G_ADAPTER.is_connected(self.data2g_session)))
        data2g_path = selected_data2g or host_busy
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
                           "Data2G host handles radio reception. Connect a host in Station settings to receive card frames.")
            elif host_busy:
                message = "Data2G radio handoff is in progress; local modem reception will start when the host disconnects."
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
        self.receive_all_modes.setEnabled(capabilities.receive_all_modes)
        self.receive_all_modes.setVisible(capabilities.receive_all_modes)
        if self.receive_all_modes_label is not None:
            self.receive_all_modes_label.setVisible(capabilities.receive_all_modes)
        if hasattr(self, "auto_connect_cat"):
            self.auto_connect_cat.setEnabled(not data2g_path)
            self.auto_connect_cat.setToolTip(
                "Used by local modem modes; Data2G handles radio control while its backend is active.")
        if hasattr(self, "rx_visual_stack"):
            self.rx_visual_stack.setCurrentWidget(
                self.rx_waterfall if capabilities.spectrum and not host_busy
                else self.rx_waterfall_unavailable)
        if hasattr(self, "cat_settings_group"):
            self.cat_settings_group.setVisible(not selected_data2g)
            self.cat_settings_group.setEnabled(not host_busy)
        if hasattr(self, "experimental_audio_group"):
            self.experimental_audio_group.setVisible(not selected_data2g)
        if hasattr(self, "data2g_settings_group"):
            self.data2g_settings_group.setVisible(selected_data2g or host_busy)

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
            pixels = metadata.pop("pixels")
            palette_id = int(metadata["palette_id"])
            palette = [list(color) for color in modem.AVATAR_PALETTES[palette_id]]
            card = {**metadata, "palette": palette, "avatar_burst": True,
                    "received_copies": 1}
            report = {"sample_rate": 0, "receive_profile": "Data2G host/KISS",
                      "valid_packets": [], "valid_packet_count": 1, "errors": [],
                      "card": card, "pixels": pixels}
            report, improved = self._merge_live_preview(report)
            card = report["card"]
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
                self._show_receive_preview(image, exact=bool(card.get("exact")),
                                           coverage=card.get("pixel_coverage"),
                                           verified_coverage=card.get("verified_pixel_coverage"))
            if verified_complete:
                self._add_session_wall_card(report)
                key = received_card_key(report)
                if key not in self.seen_received_cards:
                    self.seen_received_cards.add(key)
                    self.reply_report = report
                    self._record_received_card(report)
                    self._handle_contact_message(report)
                    self.rx_status.setText(f"Verified Data2G card from {card.get('callsign')} · whole-card CRC passed")
                    self.exchange_log.append(f"Verified Data2G card from {card.get('callsign')} ({card.get('grid')}); whole-card CRC passed.")
                    self.save_received_report(report, "data2g")
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
        self.settings.setValue("station/callsign", callsign)
        self.settings.setValue("station/grid", grid)
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
                    int(data.get("width", 32)), int(data.get("height", 32)))
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
        legacy = self.selected_mode() == "standard"
        self.role_widget.setVisible(False)
        self.auto_stage_group.setVisible(not beacon)
        for button in self.manual_stage_buttons.values():
            button.setEnabled(not automatic and not beacon)
        for combo in self.stage_card_combos.values():
            combo.setEnabled(not self.auto_armed)
        self.manual_card_widget.setVisible(not automatic and not beacon and legacy)
        self.manual_card_label.setVisible(not automatic and not beacon and legacy)
        self.auto_controls.setVisible(automatic)
        self.auto_start_btn.setVisible(False)
        self.auto_stop_btn.setVisible(self.auto_armed)
        self.exchange_status.setVisible(automatic)
        self.manual_tx_controls.setVisible(not automatic and not beacon)
        self.cq_controls.setVisible(not beacon)
        self.tx_btn.setText("Send selected stage" if not automatic else "Transmit stage")
        self._refresh_contact_stage_indicator()
        host_mode = (self.selected_adapter().capabilities.kind is
                     AdapterKind.FRAME_TRANSPORT)
        local_audio_test = self.audio_test_mode.isChecked() and not host_mode
        self.beacon_controls.setVisible(beacon and not local_audio_test)
        if not beacon and self.beacon_button.isChecked():
            self.beacon_button.setChecked(False)
        self.beacon_button.setToolTip("Repeatedly sends the card at the current radio or Data2G host frequency.")
        self.frequency_controls.setVisible(not beacon and not local_audio_test and not host_mode)

    def _refresh_contact_stage_indicator(self):
        buttons = getattr(self, "manual_stage_buttons", None)
        if not buttons:
            return
        if getattr(self, "auto_armed", False):
            state = getattr(self, "contact_stage", "idle")
            stage = {
                "send_cq": "cq", "send_exchange": "exchange",
                "await_exchange": "exchange", "await_report73": "report73",
                "send_report73": "report73", "await_final73": "final73",
                "send_final73": "final73",
            }.get(state, "exchange")
            if state == "listen_cq":
                stage = "cq" if getattr(self, "auto_role", "responder") == "responder" else "exchange"
        else:
            self.exchange_status.setText("CQ → Reply / Exchange → RR73 → 73\nCall CQ, or click Reply on a received CQ to start automatic sequencing.")
            return
        labels = {"send_cq": "Sending CQ", "listen_cq": "Listening for an exchange" if self.auto_role == "caller" else "Choose a CQ and click Reply",
                  "send_exchange": "Sending exchange", "await_report73": "Waiting for RR73",
                  "send_report73": "Sending RR73", "await_final73": "Waiting for final 73",
                  "send_final73": "Sending final 73", "idle": "Contact complete"}
        self.exchange_status.setText(labels.get(state, state) + (f" · {self.auto_peer}" if self.auto_peer else ""))
        button = buttons.get(stage)
        if button and not button.isChecked():
            button.setChecked(True)
        self.auto_start_btn.setVisible(False)
        self.auto_stop_btn.setVisible(self.auto_armed)
        self.call_cq_button.setEnabled(not self.auto_peer)

    def selected_profile(self):
        return self.profile_combo.currentData() or modem.DEFAULT_PROFILE

    def selected_mode(self):
        return self.mode_combo.currentData() or "standard"

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

    def _save_audio_device(self, combo, setting):
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
        self.data2g_audio_config_errors.pop(setting, None)
        if self.data2g_audio_config_errors:
            self.data2g_host_status.setText(self._data2g_audio_config_error_message())
        else:
            self.data2g_host_status.setText(
                "Data2G audio selection updated; connect to start the local host.")
        self._update_data2g_host_controls()

    def _show_receive_preview(self, image, *, exact=False, coverage=None,
                              verified_coverage=None):
        if image is None:
            return
        self.receive_activity_timeout.stop()
        self.receive_activity_pending = False
        self.receive_progress.setRange(0, 100)
        self.listen_indicator.setText("● LISTENING" if exact else "● RECEIVING CARD")
        self.receive_view.setPixmap(QPixmap.fromImage(image).scaled(
            184, 184, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation))
        if exact:
            self.receive_progress.setValue(100)
            self.receive_progress.setFormat("100% · whole card verified")
        else:
            percent = self._coverage_percent(coverage)
            verified_percent = self._coverage_percent(verified_coverage)
            self.receive_progress.setValue(percent)
            self.receive_progress.setFormat(
                f"{percent}% received · {verified_percent}% verified regions"
                if verified_percent else f"{percent}% received · provisional")
        self.receive_preview_box.show()
        if exact:
            self.live_preview_timeout.start(5000)
        else:
            self.live_preview_timeout.stop()

    def _hide_receive_preview(self):
        self.receive_activity_timeout.stop()
        self.receive_activity_pending = False
        self.live_preview_timeout.stop()
        self.receive_preview_box.hide()

    def _show_receive_activity(self, message):
        self.receive_activity_pending = True
        self.listen_indicator.setText("● RECEIVING SIGNAL")
        self.rx_status.setText(message)
        self.live_preview_timeout.stop()
        snapshot = self.card_assembly.preview_snapshot()
        if not snapshot or snapshot.card.get("exact"):
            self.receive_view.clear()
            self.receive_view.setText("Receiving signal…\nWaiting for card data")
            self.receive_progress.setRange(0, 0)
        self.receive_preview_box.show()

    def _receive_activity_expired(self):
        self.receive_activity_pending = False
        snapshot = self.card_assembly.preview_snapshot()
        if snapshot and not snapshot.card.get("exact"):
            self._show_receive_preview(self.card_image(
                {"card": dict(snapshot.card), "pixels": list(snapshot.pixels)}),
                coverage=snapshot.coverage, verified_coverage=snapshot.verified_pixels)
        else:
            self._hide_receive_preview()
        self.rx_status.setText("Listening · waiting for card data or another copy")
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

    def _save_auto_stage_cards(self, *_):
        for stage, combo in getattr(self, "stage_card_combos", {}).items():
            path = combo.currentData()
            if path:
                self.settings.setValue("transfer/card_" + stage, path)

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

    def _refresh_mode_options(self, preferred=None):
        if not hasattr(self, "mode_combo"):
            return
        self._set_mode_options(self.selected_mode() if preferred is None else preferred)
        self._update_transfer_controls()
        self._refresh_transfer_summary()

    def _set_mode_options(self, preferred):
        data2g = self.backend_combo.currentData() == "data2g"
        recommendations = dict(recommended_data2g_modes(self.data2g_modes)) if data2g else {}
        show_experimental = self.show_experimental_modes.isChecked()
        if data2g:
            entries = list(self.data2g_modes.items())
            if not entries:
                entries = [("data2g_1200_robust", Data2GHostBackend(
                    "data2g_1200_robust", "Data2G · connect to discover modes"))]
            if recommendations:
                entries = ([(key, self.data2g_modes[key]) for key in recommendations] +
                           [(key, backend) for key, backend in entries if key not in recommendations])
            fallback = "data2g_1200_robust"
        elif show_experimental:
            entries = [(key, backend) for key, backend in
                       available_modes(include_experimental=True)
                       if MODE_REGISTRY[key].adapter != DATA2G_ADAPTER.key and
                       MODE_REGISTRY[key].experimental]
            entries.append((LEGACY_MODE_KEY, BACKENDS.get(LEGACY_MODE_KEY)))
            fallback = "resilient_100"
        else:
            entries = []
            fallback = "resilient_100"
        visible_keys = {key for key, _backend in entries}
        selectable_keys = {key for key, backend in entries
                           if (MODE_REGISTRY.get(key) is None or
                               MODE_REGISTRY[key].adapter != DATA2G_ADAPTER.key or
                               getattr(backend, "usable", False))}
        if fallback not in visible_keys and data2g and entries:
            fallback = entries[0][0]
        selected = (preferred if preferred in selectable_keys else
                    next((key for key, _backend in entries if key in selectable_keys), fallback))
        with QSignalBlocker(self.mode_combo):
            self.mode_combo.clear()
            for key, backend in entries:
                if recommendations and key not in recommendations and self.mode_combo.count() == len(recommendations):
                    self.mode_combo.insertSeparator(self.mode_combo.count())
                    self.mode_combo.addItem("All other Data2G modes")
                    self.mode_combo.model().item(self.mode_combo.count() - 1).setEnabled(False)
                label = MODE_REGISTRY[key].label if key == LEGACY_MODE_KEY else backend.label
                if key in recommendations:
                    label = f"★ {recommendations[key]} · {backend.label.removeprefix('Data2G · ')}"
                self.mode_combo.addItem(label, key)
                if key in EXPERIMENTAL_MODE_DESCRIPTIONS:
                    self.mode_combo.setItemData(self.mode_combo.count() - 1,
                        EXPERIMENTAL_MODE_DESCRIPTIONS[key], Qt.ItemDataRole.ToolTipRole)
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
                    self.repeat_count.setValue(getattr(self, "_normal_repeat_count", 3))
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
        if hasattr(self, "profile_combo"):
            legacy = self.selected_mode() == "standard"
            self.profile_combo.setEnabled(not self.auto_armed and legacy)
            self.profile_form_widget.setVisible(legacy)
            if hasattr(self, "manual_card_widget"):
                show_legacy_card = legacy and self.exchange_mode.currentIndex() != 2
                self.manual_card_widget.setVisible(show_legacy_card and self.exchange_mode.currentIndex() != 1)
                self.manual_card_label.setVisible(show_legacy_card and self.exchange_mode.currentIndex() != 1)

    def _refresh_transfer_summary(self, *_):
        if not hasattr(self, "transfer_summary"):
            return
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
                        "Connect to a Data2G host to discover broadcast modes and estimate airtime.", True)
                    return
                if backend.disabled_reason:
                    set_summary(backend.disabled_reason, True)
                    return
            card = self._chosen_card()
            if backend is not None:
                total_seconds = backend.estimate_seconds(card, self.repeat_count.value())
            else:
                symbols, profile = self._card_tx_symbols(card)
                from modem_envelope import HEADER_SECONDS
                total_seconds = len(symbols) / profile.baud + 2*HEADER_SECONDS*self.repeat_count.value()
            timing = (f"~{total_seconds:.1f}s RF airtime" if data2g else
                      f"{total_seconds:.1f}s")
            set_summary(
                f"{card.width}×{card.height} · {len(card.palette)} colors · {timing}", data2g)
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

    def _card_tx_audio(self, card, *, beacon=False):
        stage = "card" if beacon else self._outgoing_message_type()
        wire_type = {"report73": "73", "final73": "73"}.get(stage, stage)
        snr_db = self.tx_snr_db if stage in {"exchange", "report73"} else None
        backend = self.selected_backend()
        adapter = self.selected_adapter()
        if adapter.capabilities.kind is AdapterKind.FRAME_TRANSPORT:
            raise ValueError(
                "Data2G creates the radio waveform inside its host and does not export "
                "a local WAV. Select Experimental modems to export local transmission audio.")
        if backend is not None:
            if adapter.capabilities.kind is not AdapterKind.LOCAL_IMAGE_MODEM:
                raise ValueError(f"Selected modem adapter {adapter.key!r} cannot encode local audio")
            return adapter.encode(backend, card, self.repeat_count.value(), wire_type, snr_db)
        encode_legacy = getattr(adapter, "encode_legacy", None)
        if not callable(encode_legacy):
            raise ValueError(f"Selected modem adapter {adapter.key!r} does not support legacy packet encoding")
        return encode_legacy(card, self.repeat_count.value(), wire_type, snr_db,
                             self.selected_mode(), self.selected_profile(), beacon=beacon)

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
        host_ready = (data2g_selected and host_backend is not None and
                      getattr(host_backend, "usable", False) and self.data2g_session is not None and
                      DATA2G_ADAPTER.is_connected(self.data2g_session))
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
        self.tx_btn.setText("Send test audio" if local_audio_test else "Send selected stage")
        if hasattr(self, "beacon_button"):
            self.beacon_button.setEnabled((ready or self.beacon_button.isChecked()) and not local_audio_test)

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
            self.stop_tx_btn.setEnabled(False)
            self.exchange_log.append("CAT disconnected during transmission. Audio stopped; verify the radio is unkeyed manually.")
        if connected: self.read_rig()

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
        if (self.quick_reply_card is not None and not self.auto_armed
                and self.exchange_mode.currentIndex() == 0):
            card = modem.Card(self.quick_reply_card.callsign, self.quick_reply_card.grid,
                              list(self.quick_reply_card.palette), list(self.quick_reply_card.pixels),
                              self.quick_reply_card.card_id, self.quick_reply_card.width,
                              self.quick_reply_card.height)
            card.validate()
            return card
        stage_combo = self._auto_stage_card_combo()
        selected_path = stage_combo.currentData() if stage_combo is not None else None
        if not selected_path and hasattr(self, "card_combo"):
            selected_path = self.card_combo.currentData()
        if selected_path:
            data = json.loads(Path(selected_path).read_text())
            card = modem.Card(data["callsign"], data["grid"], [tuple(c) for c in data["palette"]],
                              list(data["pixels"]), data.get("card_id", 0xC0DE), data.get("width", 32), data.get("height", 32))
        else:
            card = modem.Card(self.call.text().strip().upper(), self.grid.text().strip().upper(),
                              list(self.card.palette), list(self.card.pixels), self.card.card_id,
                              self.card.width, self.card.height)
        card.validate()
        return card

    def _remember_tx_card(self, card):
        self.tx_card = modem.Card(card.callsign, card.grid, list(card.palette), list(card.pixels),
                                  card.card_id, card.width, card.height)
        quick = self.quick_reply_card
        self.tx_quick_draw_card = (self.tx_card if quick is not None and not self.auto_armed
                                   and self.exchange_mode.currentIndex() == 0
                                   and quick.card_id == card.card_id
                                   and quick.callsign == card.callsign and quick.grid == card.grid
                                   and quick.pixels == card.pixels else None)

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
        self.copy_capture_cache = self.copy_capture_cache.__class__()
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
        self.listen_indicator.setStyleSheet("color:#e5b36e;font-weight:700;letter-spacing:1px")

    def _ensure_session_receive(self):
        if self.rx_timer.isActive() or self.tx_timer.isActive() or self.ptt_active:
            return
        if self.selected_adapter().capabilities.kind is AdapterKind.FRAME_TRANSPORT:
            self.listen_indicator.setText("● DATA2G HOST RX")
            self.listen_indicator.setStyleSheet("color:#8bd8ae;font-weight:700;letter-spacing:1px")
            return
        if self.auto_armed and self.auto_wait_state not in {"listening", "peer_response", "initial_card"}:
            return
        self.start_receive(quiet=True)

    def reply_to_received(self):
        report = self.reply_report
        card = (report or {}).get("card") or {}
        if not card:
            return
        stage = card.get("message_type", "card")
        if stage == "cq":
            if self.auto_armed or not self._assembly_verified(report):
                return
            self.exchange_mode.setCurrentIndex(1)
            self.exchange_role.setCurrentIndex(1)
            self.start_auto_exchange(reply_report=report)
            return
        if stage == "exchange":
            next_stage = "report73"
        elif stage == "73" and card.get("snr_db") is not None:
            next_stage = "final73"
        else:
            self.reply_to_button.setVisible(False)
            return
        if next_stage in self.manual_stage_buttons:
            self.manual_stage_buttons[next_stage].setChecked(True)
        self.tx_snr_db = card.get("measured_snr_db") if next_stage in {"exchange", "report73"} else None
        self.reply_to_button.setVisible(False)
        self.contact_stage = "send_" + next_stage
        self.exchange_mode.setCurrentIndex(0)
        self.transmit_exchange()

    def _show_reply_action(self, report):
        card = report.get("card") or {}
        if self.auto_armed:
            self.reply_to_button.setVisible(False)
            self.quick_draw_button.setVisible(False)
            return
        self.reply_report = report
        kind = card.get("message_type", "card")
        next_stage = {"cq": f"Reply to {card.get('callsign') or 'CQ'}", "exchange": "Send RR73"}.get(kind)
        if kind == "73" and card.get("snr_db") is not None:
            next_stage = "Send final 73"
        self.reply_to_button.setText(next_stage or "Reply")
        self.reply_to_button.setVisible(bool(next_stage))
        self.quick_draw_button.setVisible(self.exchange_mode.currentIndex() == 0 and not self.auto_armed)

    @staticmethod
    def _reply_stage(report):
        card = (report or {}).get("card") or {}
        kind = card.get("message_type", "card")
        if kind == "cq":
            return "exchange"
        if kind == "exchange":
            return "report73"
        if kind == "73" and card.get("snr_db") is not None:
            return "final73"
        return None

    def _handle_contact_message(self, report, *, initiated=False):
        card = report.get("card") or {}
        if not self._assembly_verified(report) or not card.get("avatar_burst"):
            return
        kind = card.get("message_type", "card")
        self._show_reply_action(report)
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

    def transmit_exchange(self):
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
            self._prepare_data2g_transmission(backend)
            return
        if self.audio_test_mode.isChecked():
            self._prepare_test_transmission()
            return
        if not self.rig.connected():
            QMessageBox.warning(self, "CAT unavailable", "Connect to a Hamlib rigctld service before transmitting."); return
        self.tx_btn.setEnabled(False)
        generation = self.auto_generation if self.auto_armed else None
        self.read_rig(lambda ok: self._preflight_and_confirm_tx(ok, generation))

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
            self._remember_tx_card(card)
            message_type = message_type or self._outgoing_message_type()
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
            if wire_type == "cq":
                self.last_cq_snapshot = self._local_card_snapshot(card, wire_type, snr_db)
            elif not beacon:
                self._begin_qso_log(self.auto_peer or "Unknown", "")
                self._append_qso_card("sent_cards", self._local_card_snapshot(card, wire_type, snr_db))
            self.tx_label = "Data2G host beacon" if beacon else "Data2G host card frames"
            self.stop_tx_btn.setEnabled(True)
            self.stop_tx_btn.setVisible(True)
            self.stop_tx_btn.setText("Stop queue")
            self.refresh_tx_button()
            self.listen_indicator.setText("● DATA2G HOST TX")
            self.listen_indicator.setStyleSheet("color:#e5b36e;font-weight:700;letter-spacing:1px")
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
        self.data2g_ack_timer.stop()
        sent_count = self.data2g_tx_sent
        total_count = self.data2g_tx_total
        self.data2g_tx_frames = []
        self.data2g_tx_waiting = None
        self.data2g_tx_total = 0
        self.stop_tx_btn.setEnabled(False)
        self.stop_tx_btn.setVisible(False)
        self.stop_tx_btn.setText("Stop TX")
        self.refresh_tx_button()
        self.exchange_log.append(message)
        self.rx_status.setText(message)
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
            self.listen_indicator.setStyleSheet("color:#8bd8ae;font-weight:700;letter-spacing:1px")

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

    def _prepare_test_transmission(self):
        if self.test_link_enabled:
            output_ready = True
        else:
            output_ready = bool(self.output_device.currentData())
        if not output_ready:
            QMessageBox.warning(self, "No audio output", "Select an audio output device in Station settings, or use the two-window launcher for its built-in local link.")
            return
        self.tx_btn.setEnabled(False)
        try:
            card = self._chosen_card()
            self._remember_tx_card(card)
            self.tx_message_type = self._outgoing_message_type()
            if self.tx_message_type in {"exchange", "report73"} and self.reply_report:
                self.tx_snr_db = (self.reply_report.get("card") or {}).get("measured_snr_db")
            elif self.tx_message_type not in {"exchange", "report73"}:
                self.tx_snr_db = None
            if self.auto_armed and card.callsign.upper() != self.station_call.text().strip().upper():
                raise ValueError("The automatic exchange card must use the call sign configured in Station settings.")
            if self.auto_armed and self.selected_mode() == "fast_avatar":
                raise ValueError("Raw burst pixels have no correction or integrity check; use a checked-block mode for automatic exchange.")
            if self.auto_armed: self.auto_own_card_id = card.card_id
            audio = self._card_tx_audio(card)
            duration = len(audio) / modem.SAMPLE_RATE
            self.auto_last_card_duration = duration
            self.tx_bytes = pcm16_audio_bytes(audio)
            self.tx_offset = 0
            self.tx_deadline = duration + 30.0
            self.tx_label = "card burst" if self.selected_backend() is not None else "loopback card"
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
        for widget in (self.exchange_mode, self.exchange_role, self.profile_combo, self.mode_combo, self.frequency,
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
        for widget in (self.exchange_mode, self.exchange_role, self.profile_combo, self.mode_combo, self.frequency,
                       self.rig_host, self.rig_port, self.rig_button, self.rig_refresh, self.output_device,
                       self.input_device, self.station_call, self.station_grid, self.audio_test_mode):
            widget.setEnabled(True)
        self._update_transfer_controls()
        if self.rx_timer.isActive():
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
        QTimer.singleShot(400, self._ensure_session_receive)

    def _preflight_and_confirm_tx(self, read_ok, generation=None):
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
            card = self._chosen_card()
            self._remember_tx_card(card)
            self.tx_message_type = self._outgoing_message_type()
            if self.tx_message_type in {"exchange", "report73"} and self.reply_report:
                self.tx_snr_db = (self.reply_report.get("card") or {}).get("measured_snr_db")
            elif self.tx_message_type not in {"exchange", "report73"}:
                self.tx_snr_db = None
            if self.auto_armed and card.callsign.upper() != self.station_call.text().strip().upper():
                raise ValueError("The automatic exchange card must use the call sign configured in Station settings.")
            if self.auto_armed: self.auto_own_card_id = card.card_id
            if self.auto_armed and self.selected_mode() == "fast_avatar":
                raise ValueError("Raw burst pixels have no correction or integrity check; use a checked-block mode for automatic exchange.")
            audio = self._card_tx_audio(card)
            duration = len(audio) / modem.SAMPLE_RATE
            self.auto_last_card_duration = duration
            self.tx_bytes = pcm16_audio_bytes(audio)
            self.tx_offset = 0
            self.tx_deadline = duration + 8.0
            self.tx_label = "card burst" if self.selected_backend() is not None else "legacy card"
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

    def _prepare_grid_audio(self):
        backend = self.selected_backend()
        if not getattr(backend, "utc_grid", False):
            return
        from grid_fsk_modem import plan_start
        # Probe before the timestamp; a slow status command cannot shift the plan.
        status = getattr(self, "grid_clock_status", "UTC clock quality unknown")
        now = time.time()
        latency = 0. if self.test_link_enabled and self.audio_test_mode.isChecked() else self.grid_tx_latency.value()/1000
        target, samples = plan_start(now, modem.SAMPLE_RATE, latency)
        self.tx_bytes = b"\0" * (samples*2) + self.tx_bytes
        self.tx_deadline += samples/modem.SAMPLE_RATE
        self.grid_tx_target_utc = target
        self.grid_tx_padding_samples = samples
        self.exchange_log.append(f"UTC grid target {target:.3f}; alignment silence {samples/modem.SAMPLE_RATE:.3f} s; {status}. Audio timing requires calibration.")

    def _start_tx_audio(self):
        if getattr(self.selected_backend(), "utc_grid", False):
            from grid_fsk_modem import clock_status
            self.grid_clock_status = clock_status()
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
            self._prepare_grid_audio()
            self.tx_started = time.monotonic()
            self.stop_tx_btn.setText("Stop audio")
            self.stop_tx_btn.setEnabled(True)
            self.stop_tx_btn.setVisible(True)
            self.tx_timer.start()
            if getattr(self.selected_backend(), "utc_grid", False):
                self._pump_tx()
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
        self._prepare_grid_audio()
        self.tx_started = time.monotonic()
        self.stop_tx_btn.setEnabled(True)
        self.stop_tx_btn.setVisible(True)
        self.tx_timer.start()
        if getattr(self.selected_backend(), "utc_grid", False):
            self._pump_tx()
        self._record_on_air_tx()
        self.stop_tx_btn.setText("Stop audio" if self.audio_test_mode.isChecked() else "Stop TX")
        self.exchange_log.append(f"TX started: {self.tx_label}, {len(self.tx_bytes)//2} samples. " +
                                 ("Audio-only test; PTT disabled." if self.audio_test_mode.isChecked() else "PTT asserted."))

    def _pump_tx(self):
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
            elif time.monotonic() - self.tx_started >= len(self.tx_bytes) / (2 * modem.SAMPLE_RATE):
                self.test_tx_socket.writeDatagram(
                    self.test_tx_sequence.to_bytes(4, "big") + b"EOF!",
                    QHostAddress(QHostAddress.SpecialAddress.LocalHost), self.test_tx_port)
                self._finish_tx("Audio playback complete.")
            return
        if not self.tx_audio or not self.tx_device: return
        if time.monotonic() - self.tx_started > self.tx_deadline:
            self._finish_tx("Transmit timed out; releasing PTT."); return
        if self.tx_offset < len(self.tx_bytes):
            count = min(max(0, self.tx_audio.bytesFree()), 8192, len(self.tx_bytes) - self.tx_offset)
            if count:
                count -= count % 2
                written = self.tx_device.write(self.tx_bytes[self.tx_offset:self.tx_offset+count])
                if written > 0: self.tx_offset += written
        elif self.tx_audio.processedUSecs() >= (len(self.tx_bytes) // 2) * 1_000_000 / modem.SAMPLE_RATE:
            self._finish_tx("Transmit complete; releasing PTT.")

    def _finish_tx(self, message):
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
        self.stop_tx_btn.setText("Stop TX")
        self.exchange_log.append(f"{self.tx_label}: {message}")
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
                    if self._save_quick_draw_to_sent_library(self.tx_quick_draw_card):
                        current = self.quick_reply_card
                        sent = self.tx_quick_draw_card
                        if (current is not None and current.card_id == sent.card_id
                                and current.callsign == sent.callsign and current.grid == sent.grid
                                and current.pixels == sent.pixels):
                            self.quick_reply_card = None
                except OSError as exc:
                    self.exchange_log.append(f"Couldn't save sent quick draw to My Cards: {exc}")
        self.tx_card = None
        self.tx_quick_draw_card = None
        self._refresh_quick_draw_status()
        self._release_ptt()

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
        if enabled:
            self.beacon_timer.start(self.beacon_interval.value() * 60 * 1000)
            self.transmit_beacon()
        else:
            self.beacon_timer.stop()
            self.beacon_pending = False
            self.exchange_log.append("Periodic beacon stopped.")

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
                backend, message_type="card", card_override=self.card, beacon=True)
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
            self.card.validate()
            audio = self._card_tx_audio(self.card, beacon=True)
            self.tx_bytes = pcm16_audio_bytes(audio)
            self.tx_offset = 0
            self.tx_deadline = len(audio) / modem.SAMPLE_RATE + 8.0
            self.tx_label = "one-shot card beacon"
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
            self._show_receive_preview(image, exact=bool(card.get("exact")),
                                       coverage=card.get("pixel_coverage"),
                                       verified_coverage=card.get("verified_pixel_coverage"))
        card = report.get("card") or {}
        is_avatar = bool(card.get("raw_avatar") or card.get("avatar_burst"))
        verified_complete = self._assembly_verified(report)
        if verified_complete:
            self._add_session_wall_card(report)
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
        if handled_live:
            self.auto_live_handled_cards.discard(card_key)
        else:
            already_saved = verified_complete and card_key in self.seen_received_cards
            if verified_complete and card.get("callsign") and card.get("card_id") is not None:
                self.seen_received_cards.add(card_key)
            if verified_complete and not already_saved:
                self._record_received_card(report)
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
        if self.ptt_active or self.tx_audio is not None or self.tx_timer.isActive():
            if not quiet:
                QMessageBox.warning(self, "Transmission is active", "Wait for PTT to be released before starting receive.")
            return

        def receive_error(title, message):
            self.listen_indicator.setText("● NOT LISTENING")
            self.listen_indicator.setStyleSheet("color:#e5b36e;font-weight:700;letter-spacing:1px")
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
        capture_start_utc = None if test_mode else time.time() - self.grid_rx_latency.value()/1000
        self.local_receive_handle = handle
        self.local_receive_adapter = adapter
        self.rx_audio = handle.audio_source
        self.rx_device = handle.audio_stream
        self.test_rx_socket = handle.test_socket
        if test_mode:
            self.test_rx_expected = None
            self.test_rx_pending = {}
        self.card_assembly.begin_receive_window()
        self.copy_capture_cache = self.copy_capture_cache.__class__()
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
        self.listen_indicator.setStyleSheet("color:#8bd8ae;font-weight:700;letter-spacing:1px")
        snapshot = self.card_assembly.preview_snapshot()
        if snapshot and not snapshot.card.get("exact"):
            preview = {"card": {**snapshot.card,
                                 "pixel_coverage": snapshot.coverage},
                       "pixels": snapshot.pixels}
            image = self.card_image(preview)
            if image is not None:
                self._show_receive_preview(image, coverage=snapshot.coverage,
                                           verified_coverage=snapshot.verified_pixels)
        elif not snapshot:
            self._hide_receive_preview()
        self.receive_stack.setCurrentWidget(self.session_wall_page)
        self.rx_status.setText("Local two-window audio link ready · listening" if self.test_rx_socket else
                               f"Listening · {self.session_wall_count} cards heard this session")

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
            interval = self.rx_rate
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
        worker = LiveDecodeWorker(snapshot, self.rx_rate, self.rx_live_profile or "auto", self.auto_generation,
                                  self.receive_mode(), self.selected_audio_placement(), self.selected_mode(),
                                  self.show_experimental_modes.isChecked(),
                                  copy_cache=self.copy_capture_cache,
                                  capture_start_sample=self.rx_capture_sample_offset,
                                  capture_start_utc=(None if getattr(self, "rx_capture_start_utc", None) is None else
                                      self.rx_capture_start_utc + self.rx_capture_sample_offset/self.rx_rate))
        worker.signals.finished.connect(self._live_decode_finished)
        worker.signals.preview.connect(self._live_decode_preview)
        self.live_decode_inflight = True
        self.decode_pool.start(worker)

    def _merge_live_preview(self, report):
        return self.card_assembly.merge_preview(report)

    def _live_decode_preview(self, result):
        generation, report = result
        if generation != self.auto_generation or not self.rx_timer.isActive():
            return
        if report.get("receive_activity"):
            self._show_receive_activity(report["receive_activity"])
            self.receive_activity_timeout.start(12000)
            return
        try:
            report, improved = self._merge_live_preview(report)
        except ValueError:
            return
        if improved:
            card = report["card"]
            self._show_receive_preview(self.card_image(report),
                coverage=card.get("pixel_coverage"),
                verified_coverage=card.get("verified_pixel_coverage"))
            self.rx_status.setText(
                f"Receiving card from {card.get('callsign') or 'a station'} · "
                f"{card.get('color_stage') or 'identity received'} · checking other modes")

    @staticmethod
    def _assembly_verified(report):
        event = report.get("assembly_event") or {}
        return bool(event.get("verified_complete"))

    def _live_decode_finished(self, result):
        generation, report, complete, error, stats = result
        self.live_decode_inflight = False
        if generation != self.auto_generation or not self.rx_timer.isActive():
            return
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
                self.rx_status.setText("Listening · no complete card decoded yet")
            QTimer.singleShot(0, self._drain_rx)
            return
        if report.get("receive_activity"):
            self._show_receive_activity(report["receive_activity"])
            self.receive_activity_timeout.start(12000)
            if self.on_air and on_air_details is not None:
                self.on_air.emit("rx_decode", on_air_details)
                self.on_air_decode_pcm = None
            QTimer.singleShot(0, self._drain_rx)
            return
        card = report.get("card") or {}
        if card.get("card_id") is not None and self.receive_mode() != "auto":
            self.rx_live_profile = report.get("receive_profile")
        try:
            report, improved = self._merge_live_preview(report)
        except ValueError as exc:
            self.exchange_log.append("Rejected conflicting receive evidence: " + str(exc))
            self.rx_status.setText("Conflicting verified image data rejected; listening for another copy")
            QTimer.singleShot(0, self._drain_rx)
            return
        card = report.get("card") or {}
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
                self._show_receive_preview(image, exact=bool(card.get("exact")),
                                           coverage=card.get("pixel_coverage"),
                                           verified_coverage=card.get("verified_pixel_coverage"))
            self.rx_status.setText(f"Receiving card · {card.get('callsign') or 'station not identified'} · {live_status}")
        if complete:
            if self.auto_armed:
                key = received_card_key(report)
                if (card.get("message_type") != "cq" and self._assembly_verified(report) and card.get("callsign")
                        and card.get("card_id") is not None and key in self.seen_received_cards):
                    self.rx_status.setText(f"Already received {card.get('callsign')} card {card.get('card_id')}; staying on receive.")
                    self._add_session_wall_card(report)
                    self._clear_rx_bytes()
                    self.copy_capture_cache = self.copy_capture_cache.__class__()
                    self.rx_capture_sample_offset = 0
                    self.live_decode_samples = 0
                    self.live_decode_final = False
                    self.card_assembly.clear_current_preview()
                    self.receive_stack.setCurrentWidget(self.session_wall_page)
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
                if self._assembly_verified(report):
                    self._add_session_wall_card(report)
                    key = received_card_key(report)
                    if key in self.seen_received_cards:
                        self.rx_status.setText(f"Already heard {card.get('callsign') or 'this station'} card; staying on receive.")
                    else:
                        self.seen_received_cards.add(key)
                        self._record_received_card(report)
                        self._handle_contact_message(report)
            if self.auto_armed:
                self.stop_receive()
            else:
                self._reset_receive_window()
        elif self.live_decode_final and self.auto_armed:
            self.stop_receive()
        else:
            QTimer.singleShot(0, self._drain_rx)

    def stop_receive(self):
        self._flush_on_air_rx()
        self.rx_timer.stop(); self._drain_rx()
        self.auto_receive_timer.stop()
        self._stop_local_receive_capture()
        self.test_rx_pending = {}; self.test_rx_expected = None
        self.test_rx_final_pending = False; self.live_decode_final = False
        self.listen_indicator.setText("● PROCESSING")
        self.listen_indicator.setStyleSheet("color:#e5b36e;font-weight:700;letter-spacing:1px")
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

    def _add_session_wall_card(self, report):
        card = report.get("card") or {}
        if not self._assembly_verified(report):
            return
        key = received_card_key(report)
        if key in self.session_wall_keys:
            return
        image = self.card_image(report)
        if image is None:
            return
        self.session_wall_keys.add(key)
        self.session_wall_count += 1
        callsign = str(card.get("callsign") or "Unknown station")
        grid = str(card.get("grid") or "")
        kind = {"cq": "CQ", "exchange": "EXCHANGE", "73": "73"}.get(card.get("message_type"), "CARD")
        received_at = time.strftime("%H:%M:%S")
        tile = image.scaled(QSize(144, 144), Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.FastTransformation)
        icon = QIcon(QPixmap.fromImage(tile))
        item = QListWidgetItem(icon, "")
        item.setToolTip(f"{callsign} · {grid} · {kind} · received {received_at}")
        self.session_wall.insertItem(0, item)
        self.session_wall_empty.hide()
        self.session_wall.show()

    def _reset_receive_window(self):
        self._clear_rx_bytes()
        self.live_decode_samples = 0
        self.live_decode_final = False
        self.card_assembly.begin_receive_window()
        self.copy_capture_cache = self.copy_capture_cache.__class__()
        self.rx_capture_sample_offset = 0
        self._clear_exact_live_preview()
        self.live_preview_timeout.stop()
        self.receive_stack.setCurrentWidget(self.session_wall_page)
        snapshot = self.card_assembly.preview_snapshot()
        if not snapshot or snapshot.card.get("exact"):
            self._hide_receive_preview()
        self.rx_status.setText(f"Listening · {self.session_wall_count} cards heard this session")
        self.listen_indicator.setText("● LISTENING")
        self.listen_indicator.setStyleSheet("color:#8bd8ae;font-weight:700;letter-spacing:1px")
        QTimer.singleShot(0, self._drain_rx)

    def _return_to_session_wall(self):
        self.receive_preview_box.hide()
        if self.rx_timer.isActive():
            self.rx_status.setText(f"Listening · {self.session_wall_count} cards heard this session")

    def _record_received_card(self, report):
        card = report.get("card") or {}
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

    def _append_qso_card(self, bucket, card_data):
        if self.active_qso_log is None: self._begin_qso_log()
        new_card = card_data.get("card") or {}
        attempts_key = "transmission_attempts" if bucket == "sent_cards" else "receive_attempts"
        duplicate = next((item for item in self.active_qso_log[bucket]
                          if (item.get("card") or {}).get("card_id") == new_card.get("card_id")
                          and (item.get("card") or {}).get("callsign") == new_card.get("callsign")
                          and (item.get("card") or {}).get("message_type", "card") == new_card.get("message_type", "card")
                          and (item.get("card") or {}).get("snr_db") == new_card.get("snr_db")
                          and new_card.get("card_id") is not None), None)
        if duplicate is not None:
            attempts = int(duplicate.get(attempts_key, 1)) + 1
            if bucket == "received_cards" and self._assembly_verified(card_data):
                duplicate.update(card_data)
            duplicate[attempts_key] = attempts
        else:
            card_data[attempts_key] = 1
            card_data["order"] = max((int(item.get("order", 0)) for key in ("sent_cards", "received_cards")
                                       for item in self.active_qso_log[key]), default=0) + 1
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
                          "message_type": {"report73": "73", "final73": "73"}.get(message_type, message_type),
                          "snr_db": snr_db}, "pixels": pixels}

    def closeEvent(self, event):
        self.beacon_timer.stop()
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
            from data2g.host import main as data2g_server_main
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

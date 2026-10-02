#!/usr/bin/env python3
"""Pixel QSO desktop application for editable, self-identifying image bursts."""
from __future__ import annotations

import json
import hashlib
import copy
import math
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QRect, QSize, QObject, QTimer, QRunnable, QThreadPool, Signal, QStandardPaths, QMimeData, QPoint, QUrl, QSortFilterProxyModel, QSettings, QSignalBlocker
from PySide6.QtGui import QColor, QPainter, QPen, QImage, QPixmap, QFont, QIcon, QKeySequence, QShortcut, QDrag, QStandardItem, QStandardItemModel
from PySide6.QtNetwork import QTcpSocket, QUdpSocket, QHostAddress
from PySide6.QtMultimedia import QAudioDevice, QAudioFormat, QAudioSink, QAudioSource, QMediaDevices
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QListWidget, QListWidgetItem,
    QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QInputDialog,
    QMessageBox, QPushButton, QSpinBox, QTabWidget, QTextEdit, QDialog,
    QVBoxLayout, QWidget, QButtonGroup, QScrollArea, QTableView, QAbstractItemView, QFrame, QCheckBox, QGroupBox,
    QStackedWidget)

import cardmodem as modem
from card_backends import BACKENDS, get_backend, is_card_backend, is_available
from card_views import DeckDialog, card_art, deck_events
from webserver import CompanionServer

DATA = Path.home() / ".local" / "share" / "PixelQSO"
AUTO_REPLY_GUARD_MS = 600
PALETTE = [(0, 0, 0), (15, 15, 15), (0, 0, 15), (0, 15, 15),
           (0, 15, 0), (15, 15, 0), (15, 0, 0), (15, 0, 15)]


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


def decode_avatar_report(audio, sample_rate):
    pixels, decoded = modem.decode_minimal_avatar_audio_auto(audio, sample_rate)
    card = {**decoded, "palette": decoded.get("palette") or
            [list(color) for color in modem.MINIMAL_AVATAR_PALETTE], "preview_received": True}
    return {"sample_rate": sample_rate, "valid_packets": [], "fresh_packets": [],
            "receive_profile": modem.MINIMAL_AVATAR_PROFILE.key, "errors": [], "card": card,
            "pixels": pixels}


class DecodeWorker(QRunnable):
    def __init__(self, path: Path, state_path: Path, profile="auto", mode="standard"):
        super().__init__()
        self.path = path
        self.state_path = state_path
        self.profile = profile
        self.mode = mode
        self.signals = DecodeSignals()

    def run(self):
        try:
            audio, sr = modem.read_wav(self.path)
            if is_card_backend(self.mode) and self.mode not in {"fast_avatar", "fast_avatar_fec"}:
                self.signals.finished.emit((str(self.path), get_backend(self.mode).decode(audio, sr), None))
                return
            try:
                result = decode_avatar_report(audio, sr)
            except Exception:
                if self.mode in {"fast_avatar", "fast_avatar_fec"}:
                    raise
            else:
                self.signals.finished.emit((str(self.path), result, None))
                return
            packets, errors, sr, state = modem.decode_wav_with_state(self.path, self.state_path, self.profile)
            meta, pixels = modem.reconstruct(packets)
            result = {"sample_rate": sr,
                "valid_packets": [{"type": p.packet_type, "card_id": p.card_id, "seq": p.seq,
                                   "corrected_bits": p.corrected_bits,
                                   "payload_hex": p.payload.hex()} for p in packets],
                "fresh_packets": state.get("fresh_valid_packets", []),
                "fresh_identity_evidence_card_ids": state.get("fresh_identity_evidence_card_ids", []),
                "receive_profile": state.get("profile"),
                "errors": errors, "card": meta, "pixels": pixels, "evidence_state": state}
            self.signals.finished.emit((str(self.path), result, None))
        except Exception as exc:
            self.signals.finished.emit((str(self.path), None, str(exc)))


class LiveDecodeWorker(QRunnable):
    """Decode a snapshot of the in-memory receive buffer while it is recording."""
    def __init__(self, samples: bytes, sample_rate: int, profile: str, generation: int, mode="standard"):
        super().__init__()
        self.samples = samples
        self.sample_rate = sample_rate
        self.profile = profile
        self.generation = generation
        self.mode = mode
        self.signals = DecodeSignals()

    def run(self):
        try:
            audio = np.frombuffer(self.samples, dtype="<i2").astype(np.float32) / 32768.0
            if is_card_backend(self.mode) and self.mode not in {"fast_avatar", "fast_avatar_fec"}:
                result = get_backend(self.mode).decode(audio, self.sample_rate)
                card = result["card"]
                self.signals.finished.emit((self.generation, {"valid_packet_count": 0,
                    "card": card, "pixels": result["pixels"], "fresh_packets": [],
                    "receive_profile": result["receive_profile"]}, bool(card.get("exact")), None))
                return
            if self.mode in {"fast_avatar", "fast_avatar_fec"}:
                result = decode_avatar_report(audio, self.sample_rate)
                card = result["card"]
                result = {"valid_packet_count": 0, "card": card, "pixels": result["pixels"],
                          "fresh_packets": [], "receive_profile": modem.MINIMAL_AVATAR_PROFILE.key}
                self.signals.finished.emit((self.generation, result, bool(card.get("exact")), None))
                return
            try:
                avatar_result = decode_avatar_report(audio, self.sample_rate)
            except Exception:
                avatar_result = None
            if avatar_result is not None:
                card = avatar_result["card"]
                result = {"valid_packet_count": 0, "card": card, "pixels": avatar_result["pixels"],
                          "fresh_packets": [], "receive_profile": modem.MINIMAL_AVATAR_PROFILE.key}
                self.signals.finished.emit((self.generation, result, bool(card.get("exact")), None))
                return
            selected = (modem.select_capture_profile(audio, self.sample_rate)
                        if self.profile == "auto" else modem.resolve_profile(self.profile))
            evidence, _start, _shift, _period = modem.receive_capture_evidence(
                audio, self.sample_rate, profile=selected)
            packets, _errors = modem.decode_packet_evidence(evidence)
            meta, pixels = modem.reconstruct(packets)
            complete = modem.is_complete_card(packets)
            result = {"valid_packet_count": len(packets), "card": meta, "pixels": pixels,
                      "fresh_packets": [{"type": p.packet_type, "card_id": p.card_id, "seq": p.seq,
                                         "payload_hex": p.payload.hex()} for p in packets],
                      "receive_profile": selected.key}
            self.signals.finished.emit((self.generation, result, complete, None))
        except Exception as exc:
            self.signals.finished.emit((self.generation, None, False, str(exc)))


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
        self.drag_layer = None
        self.drag_offset = (0, 0)
        self.setAcceptDrops(True)
        self.setMinimumSize(320, 320)
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
        if not event.buttons() & (Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton): return
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
        self.web_server = None
        self.web_bridge = WebCommandBridge(self)
        self.web_bridge.requested.connect(self._handle_web_command)
        self.web_enabled = QCheckBox("Enable mobile web app and control API (LAN)")
        self.web_enabled.setChecked(str(self.settings.value("options/web_enabled", "false")).lower() in {"1", "true", "yes", "on"})
        self.web_port = QSpinBox(); self.web_port.setRange(1024, 65535)
        self.web_port.setValue(int(self.settings.value("options/web_port", 8765)))
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
        self.show_experimental_modes = QCheckBox("Show experimental legacy packet / MFSK modes")
        self.show_experimental_modes.setChecked(str(self.settings.value("options/show_experimental_modes", "false")).lower() in {"1", "true", "yes", "on"})
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
        self.live_preview_state = None
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
                if saved_card.get("exact") and saved_card.get("callsign") and saved_card.get("card_id") is not None:
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
        self.rx_bytes = bytearray()
        self.rx_rate = modem.SAMPLE_RATE
        self.rx_live_profile = None
        self.auto_decode_paths = set()
        self.live_decode_inflight = False
        self.live_decode_samples = 0
        self.rx_timer = QTimer(self); self.rx_timer.setInterval(200); self.rx_timer.timeout.connect(self._drain_rx)
        self.live_preview_timeout = QTimer(self); self.live_preview_timeout.setSingleShot(True)
        self.live_preview_timeout.timeout.connect(self._return_to_session_wall)
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
        self.tabs.addTab(self.make_exchange_tab(), "QSO")
        self.tabs.addTab(self.make_library_tab(), "My Cards")
        self.tabs.addTab(self.make_qso_log_tab(), "QSO Log")
        layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self.refresh_card_library()
        self.refresh_qso_log()
        QTimer.singleShot(250, self._ensure_session_receive)

    def make_library_tab(self):
        page = QWidget(); layout = QVBoxLayout(page)
        heading = QHBoxLayout(); heading.addWidget(QLabel("My cards"), 1)
        create = QPushButton("Create card"); create.clicked.connect(self.create_library_card); heading.addWidget(create)
        open_button = QPushButton("Open selected"); open_button.clicked.connect(self.open_selected_library_card); heading.addWidget(open_button)
        duplicate = QPushButton("Duplicate"); duplicate.clicked.connect(self.duplicate_library_card); heading.addWidget(duplicate)
        delete = QPushButton("Delete"); delete.clicked.connect(self.delete_library_card); heading.addWidget(delete)
        layout.addLayout(heading)
        self.library_search = QLineEdit(); self.library_search.setPlaceholderText("Find a card…")
        self.library_search.textChanged.connect(self.filter_card_library); layout.addWidget(self.library_search)
        self.library_list = QListWidget(); self.library_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.library_list.setIconSize(QSize(190, 238)); self.library_list.setGridSize(QSize(218, 288))
        self.library_list.setMovement(QListWidget.Movement.Static); self.library_list.setSpacing(8)
        self.library_list.itemClicked.connect(lambda _item: self._activate_library_list(self.library_list))
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
        controls.addWidget(self.log_sort); layout.addLayout(controls)
        self.log_gallery = QListWidget(); self.log_gallery.setViewMode(QListWidget.ViewMode.IconMode)
        self.log_gallery.setMovement(QListWidget.Movement.Static)
        self.log_gallery.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.log_gallery.setIconSize(QSize(220, 275)); self.log_gallery.setGridSize(QSize(250, 325))
        self.log_gallery.setSpacing(8); self.log_gallery.itemClicked.connect(self.show_log_deck)
        self.log_gallery.itemActivated.connect(self.show_log_deck)
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
        folder = DATA / "sent-cards"
        folder.mkdir(parents=True, exist_ok=True)
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in card.callsign)
        target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{card.card_id:04x}.json"
        suffix = 2
        while target.exists():
            target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{card.card_id:04x}-{suffix}.json"
            suffix += 1
        pixels = list(card.pixels)
        data = {"callsign": card.callsign, "grid": card.grid, "palette": card.palette,
                "pixels": pixels, "card_id": card.card_id, "width": card.width,
                "height": card.height, "base_pixels": pixels, "stamp_layers": [],
                "archive_reason": "quick_draw_transmission"}
        target.write_text(json.dumps(data, indent=2) + "\n")
        self.refresh_card_library()
        self._activate_library_list(self.sent_library_list)
        for row in range(self.sent_library_list.count()):
            item = self.sent_library_list.item(row)
            if Path(item.data(Qt.ItemDataRole.UserRole)) == target:
                self.sent_library_list.setCurrentItem(item)
                item.setSelected(True)
                break
        self.library_status.setText(f"Saved sent quick draw to Sent cards · {target.stem}")
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
        self.log_gallery.clear(); self.qso_log_dir.mkdir(parents=True, exist_ok=True)
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
        remove_stamp = QPushButton("Delete stamp"); remove_stamp.clicked.connect(self.remove_stamp); stamp_actions.addWidget(remove_stamp)
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
        for label, handler in (("New", self.new_card), ("Open…", self.open_card), ("Save", self.save_card), ("Export WAV…", self.export_wav)):
            b = QPushButton(label); b.clicked.connect(handler); buttons.addWidget(b)
        side.addLayout(buttons)
        side.addStretch()
        self.status = QLabel("Ready")
        side.addWidget(self.status)
        self.canvas.image_dropped.connect(self.place_stamp_image)
        self.canvas.changed.connect(self._sync_editor_controls)
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
        self.rig_host = QLineEdit("127.0.0.1"); self.rig_host.setMaximumWidth(180)
        self.rig_port = QSpinBox(); self.rig_port.setRange(1, 65535); self.rig_port.setValue(4532)
        self.rig_button = QPushButton("Connect CAT"); self.rig_button.clicked.connect(self.connect_rig)
        self.rig_refresh = QPushButton("Read rig frequency"); self.rig_refresh.clicked.connect(self.read_rig)
        self.output_device = QComboBox()
        for dev in QMediaDevices.audioOutputs(): self.output_device.addItem(dev.description(), dev)
        self.input_device = QComboBox()
        for dev in QMediaDevices.audioInputs(): self.input_device.addItem(dev.description(), dev)
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
        for backend in BACKENDS.values():
            if is_available(backend.key):
                self.mode_combo.addItem(backend.label, backend.key)
        if self.show_experimental_modes.isChecked():
            self.mode_combo.addItem("Legacy · experimental", "standard")
        configured_mode = self.settings.value("transfer/mode", "fast_avatar_fec")
        mode_index = self.mode_combo.findData(configured_mode)
        if mode_index < 0:
            mode_index = self.mode_combo.findData("fast_avatar_fec")
        self.mode_combo.setCurrentIndex(mode_index)
        self.mode_combo.currentIndexChanged.connect(lambda: self.settings.setValue("transfer/mode", self.selected_mode()))
        self.mode_combo.currentIndexChanged.connect(self._update_transfer_controls)
        self.mode_combo.currentIndexChanged.connect(self._refresh_transfer_summary)
        mode_form = QFormLayout(); mode_form.addRow("Transmission type", self.mode_combo)
        self.repeat_count = QSpinBox(); self.repeat_count.setRange(1, 20); self.repeat_count.setValue(int(self.settings.value("transfer/repeats", 3)))
        self.repeat_count.setToolTip("Repeat the same one-way burst. The receiver combines independently checked image blocks.")
        self.repeat_count.valueChanged.connect(lambda value: self.settings.setValue("transfer/repeats", value))
        self.repeat_count.valueChanged.connect(self._refresh_transfer_summary)
        mode_form.addRow("Copies", self.repeat_count)
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
                              ("report73", "73 · with SNR"), ("final73", "73 · final")):
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
        self.transfer_summary = QLabel(); self.transfer_summary.setWordWrap(True); self.transfer_summary.setStyleSheet("color:#a9b8ae; padding:4px")
        transfer_form.addRow(self.transfer_summary)
        self.tx_btn = QPushButton("Send selected stage"); self.tx_btn.setObjectName("primaryAction"); self.tx_btn.clicked.connect(self.transmit_exchange)
        self.stop_tx_btn = QPushButton("Stop TX"); self.stop_tx_btn.setEnabled(False); self.stop_tx_btn.setVisible(False); self.stop_tx_btn.clicked.connect(lambda: self._finish_tx("Transmit stopped by operator; releasing PTT."))
        tx_row = QHBoxLayout(); tx_row.addWidget(self.tx_btn); tx_row.addWidget(self.stop_tx_btn)
        self.manual_tx_controls = QWidget(); self.manual_tx_controls.setLayout(tx_row); transfer_form.addRow(self.manual_tx_controls)
        auto_row = QHBoxLayout()
        self.auto_start_btn = QPushButton("Start automatic exchange"); self.auto_start_btn.clicked.connect(self.start_auto_exchange); auto_row.addWidget(self.auto_start_btn)
        self.auto_stop_btn = QPushButton("Stop exchange"); self.auto_stop_btn.setEnabled(False); self.auto_stop_btn.setVisible(False); self.auto_stop_btn.clicked.connect(self.stop_auto_exchange); auto_row.addWidget(self.auto_stop_btn)
        self.auto_controls = QWidget(); self.auto_controls.setLayout(auto_row); transfer_form.addRow(self.auto_controls)
        self.call_cq_button = QPushButton("Call CQ")
        self.call_cq_button.setCheckable(True)
        self.call_cq_button.toggled.connect(self._toggle_call_cq)
        self.cq_interval = QSpinBox(); self.cq_interval.setRange(1, 60)
        self.cq_interval.setValue(int(self.settings.value("transfer/cq_interval_minutes", 5)))
        self.cq_interval.setSuffix(" min")
        self.cq_interval.valueChanged.connect(lambda value: self.settings.setValue("transfer/cq_interval_minutes", value))
        cq_row = QHBoxLayout(); cq_row.addWidget(self.call_cq_button); cq_row.addWidget(QLabel("Repeat every")); cq_row.addWidget(self.cq_interval)
        self.cq_controls = QWidget(); self.cq_controls.setLayout(cq_row); transfer_form.addRow(self.cq_controls)
        self.beacon_freq = QSpinBox(); self.beacon_freq.setRange(100_000, 1_300_000_000); self.beacon_freq.setValue(28_200_000); self.beacon_freq.setSuffix(" Hz")
        self.beacon_button = QPushButton("Transmit beacon"); self.beacon_button.clicked.connect(self.transmit_beacon)
        beacon_form = QFormLayout(); beacon_form.addRow("Frequency", self.beacon_freq); beacon_form.addRow(self.beacon_button)
        self.beacon_controls = QWidget(); self.beacon_controls.setLayout(beacon_form); transfer_form.addRow(self.beacon_controls)
        self._update_transfer_controls()
        self._refresh_transfer_summary()

        wav_button = QPushButton("Decode audio file…"); wav_button.clicked.connect(self.decode_wav)
        receive_group = QGroupBox("Receive")
        receive_layout = QHBoxLayout(receive_group); receive_layout.addWidget(QLabel("Always listening when an audio input is available."), 1); receive_layout.addWidget(wav_button)

        left_panel = QVBoxLayout(); left_panel.setContentsMargins(0, 0, 0, 0); left_panel.addWidget(transfer_group); left_panel.addStretch()
        left = QWidget(); left.setLayout(left_panel); left.setMaximumWidth(410)

        right_panel = QVBoxLayout()
        view_heading = QHBoxLayout()
        view_heading.addWidget(QLabel("Cards heard this session"), 1)
        self.listen_indicator = QLabel("● LISTENING")
        self.listen_indicator.setStyleSheet("color:#8bd8ae;font-weight:700;letter-spacing:1px")
        view_heading.addWidget(self.listen_indicator)
        right_panel.addLayout(view_heading)
        self.receive_stack = QStackedWidget()
        self.session_wall_page = QWidget()
        wall_layout = QVBoxLayout(self.session_wall_page); wall_layout.setContentsMargins(0, 0, 0, 0)
        self.session_wall_empty = QLabel("Your session wall is empty.\nVerified cards received on this frequency will appear here.")
        self.session_wall_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.session_wall_empty.setStyleSheet("background:#0c110e;color:#809087;border:1px solid #2e3d45;border-radius:10px;padding:24px;font-size:16px")
        wall_layout.addWidget(self.session_wall_empty, 1)
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
        wall_layout.addWidget(self.session_wall, 1)
        self.receive_stack.addWidget(self.session_wall_page)
        self.receive_view = QLabel("Listening for a card")
        self.receive_view.setAlignment(Qt.AlignmentFlag.AlignCenter); self.receive_view.setMinimumSize(280, 280)
        self.receive_view.setStyleSheet("background:#0c110e;color:#809087;border:1px solid #2e3d45;border-radius:10px")
        self.receive_stack.addWidget(self.receive_view)
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
        options_form.addRow("Testing", self.audio_test_mode)
        options_form.addRow("Advanced modes", self.show_experimental_modes)
        options_form.addRow("Mobile control", self.web_enabled)
        options_form.addRow("Web port", self.web_port)
        layout.addWidget(options)
        station = QGroupBox("Radio and audio")
        station_form = QFormLayout(station)
        station_form.addRow("rigctld host", self.rig_host); station_form.addRow("Port", self.rig_port)
        station_form.addRow("Audio output", self.output_device); station_form.addRow("Audio input", self.input_device)
        layout.addWidget(station)
        buttons = QHBoxLayout(); buttons.addWidget(self.rig_button); buttons.addWidget(self.rig_refresh); layout.addLayout(buttons)
        done = QPushButton("Done"); done.clicked.connect(self.save_station_settings); done.clicked.connect(dialog.accept); layout.addWidget(done)
        dialog.exec()

    def save_station_settings(self):
        callsign = self.station_call.text().strip().upper()
        grid = self.station_grid.text().strip().upper()
        self.station_call.setText(callsign)
        self.station_grid.setText(grid)
        self.settings.setValue("station/callsign", callsign)
        self.settings.setValue("station/grid", grid)
        self.settings.setValue("station/audio_test_mode", self.audio_test_mode.isChecked())
        self.settings.setValue("options/show_experimental_modes", self.show_experimental_modes.isChecked())
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
                    "transmitting": bool(self.tx_audio or self.tx_timer.isActive()),
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
                if self.tx_audio or self.tx_timer.isActive() or self.ptt_active: self._finish_tx("Transmit stopped by mobile operator; releasing PTT.")
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
        if hasattr(self, "test_mode_banner"):
            self.test_mode_banner.setVisible(enabled)
        if hasattr(self, "tx_btn"):
            self.tx_btn.setText("Send test audio" if enabled else "Send selected stage")
            self.refresh_tx_button()
        if hasattr(self, "beacon_button"):
            self.beacon_button.setEnabled(not enabled and self.rig.connected())
        if hasattr(self, "beacon_controls"):
            self.beacon_controls.setEnabled(not enabled)
        if hasattr(self, "frequency_controls"):
            self.frequency_controls.setVisible(not enabled and self.exchange_mode.currentIndex() != 2)
        if hasattr(self, "exchange_mode"):
            self._update_exchange_controls()
        if hasattr(self, "rig_status") and enabled and self.test_link_enabled:
            self.rig_status.setText("Two-window local audio link · CAT/PTT disabled")
        elif hasattr(self, "rig_status") and not enabled and not self.rig.connected():
            self.rig_status.setText("CAT disconnected")

    def _update_exchange_controls(self, *_):
        automatic = self.exchange_mode.currentIndex() == 1
        beacon = self.exchange_mode.currentIndex() == 2
        self.quick_draw_button.setVisible(not automatic and not beacon and not self.auto_armed)
        self.quick_draw_status.setVisible(not automatic and not beacon and not self.auto_armed)
        self.clear_quick_draw_button.setVisible(self.quick_reply_card is not None and not automatic and not beacon and not self.auto_armed)
        legacy = self.selected_mode() == "standard"
        self.role_widget.setVisible(automatic)
        self.auto_stage_group.setVisible(not beacon)
        for button in self.manual_stage_buttons.values():
            button.setEnabled(not automatic and not beacon)
        self.manual_card_widget.setVisible(not automatic and not beacon and legacy)
        self.manual_card_label.setVisible(not automatic and not beacon and legacy)
        self.auto_controls.setVisible(automatic)
        self.manual_tx_controls.setVisible(not automatic and not beacon)
        self.cq_controls.setVisible(not beacon)
        self.tx_btn.setText("Send selected stage" if not automatic else "Transmit stage")
        self._refresh_contact_stage_indicator()
        self.beacon_controls.setVisible(beacon and not self.audio_test_mode.isChecked())
        self.frequency_controls.setVisible(not beacon and not self.audio_test_mode.isChecked())

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
            return
        button = buttons.get(stage)
        if button and not button.isChecked():
            button.setChecked(True)
        self.auto_start_btn.setVisible(not self.auto_armed)
        self.auto_stop_btn.setVisible(self.auto_armed)

    def selected_profile(self):
        return self.profile_combo.currentData() or modem.DEFAULT_PROFILE

    def selected_mode(self):
        return self.mode_combo.currentData() or "standard"

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

    def _refresh_mode_options(self):
        if not hasattr(self, "mode_combo"):
            return
        current = self.selected_mode()
        with QSignalBlocker(self.mode_combo):
            self.mode_combo.clear()
            for backend in BACKENDS.values():
                if is_available(backend.key):
                    self.mode_combo.addItem(backend.label, backend.key)
            if self.show_experimental_modes.isChecked():
                self.mode_combo.addItem("Legacy · experimental", "standard")
            index = self.mode_combo.findData(current)
            self.mode_combo.setCurrentIndex(index if index >= 0 else self.mode_combo.findData("fast_avatar_fec"))
        self._update_transfer_controls()
        self._refresh_transfer_summary()

    def _update_transfer_controls(self, *_):
        if hasattr(self, "repeat_count"):
            backend = get_backend(self.selected_mode()) if is_card_backend(self.selected_mode()) else None
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
            self.repeat_count.setToolTip("One complete checked 32×32 eight-color burst; additional copies exceed five seconds."
                                        if self.selected_mode() == "experimental_qpsk_5s" else
                                        "Repeat the same one-way burst. The receiver combines independently checked image blocks.")
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
        try:
            card = self._chosen_card()
            if is_card_backend(self.selected_mode()):
                total_seconds = get_backend(self.selected_mode()).estimate_seconds(card, self.repeat_count.value())
            else:
                symbols, profile = self._card_tx_symbols(card)
                total_seconds = len(symbols) / profile.baud
            self.transfer_summary.setText(f"{card.width}×{card.height} · {len(card.palette)} colors · {total_seconds:.1f}s")
        except Exception as exc:
            self.transfer_summary.setText(str(exc) if self.selected_mode() == "experimental_qpsk_5s" else
                                          "Choose a card and burst type to see estimated send time.")

    def _card_tx_audio(self, card, *, beacon=False):
        stage = "card" if beacon else self._outgoing_message_type()
        wire_type = {"report73": "73", "final73": "73"}.get(stage, stage)
        snr_db = self.tx_snr_db if stage in {"exchange", "report73"} else None
        mode = self.selected_mode()
        if is_card_backend(mode):
            return get_backend(mode).encode(card, self.repeat_count.value(), wire_type, snr_db)
        symbols, profile = self._card_tx_symbols(card, beacon=beacon)
        return modem.synthesize(symbols, profile=profile)

    def _card_tx_symbols(self, card, *, beacon=False):
        mode = self.selected_mode()
        stage = "card" if beacon else self._outgoing_message_type()
        wire_type = {"report73": "73", "final73": "73"}.get(stage, stage)
        snr_db = self.tx_snr_db if stage in {"exchange", "report73"} else None
        if mode == "fast_avatar":
            cycle = modem.minimal_avatar_symbols(card, message_type=wire_type, snr_db=snr_db)
        elif mode == "fast_avatar_fec":
            cycle = modem.minimal_avatar_resilient_cycle_symbols(card, message_type=wire_type, snr_db=snr_db)
        else:
            if card.width != 32 or card.height != 32 or len(card.palette) != 8:
                raise ValueError("The experimental legacy packet card supports only 32×32 with 8 colors. Use a burst transmission for other canvas formats.")
            packets = (modem.make_beacon_packets(card, mode) if beacon else modem.make_packets(card, mode))
            cycle = modem.all_symbols(packets)
        return modem.repeat_symbol_stream(cycle, self.repeat_count.value()), (
            modem.MINIMAL_AVATAR_PROFILE if mode in {"fast_avatar", "fast_avatar_fec"} else self.selected_profile())

    def connect_rig(self):
        if self.rig.connected():
            self.rig.disconnect()
            return
        self.rig_status.setText("Connecting to rig control service…")
        self.rig.connect_to(self.rig_host.text().strip(), self.rig_port.value(), self.rig_changed)

    def refresh_tx_button(self):
        test_ready = (self.audio_test_mode.isChecked() and
                      (self.test_link_enabled or self.output_device.currentData()))
        radio_ready = test_ready or self.rig.connected()
        ready = (radio_ready and not self.auto_armed and not self.ptt_active
                 and self.tx_audio is None and not self.tx_timer.isActive()
                 and self.rig.pending is None and not self.rig.queue
                 and not getattr(self, "beacon_pending", False))
        self.tx_btn.setEnabled(ready)
        self.tx_btn.setText("Send test audio" if self.audio_test_mode.isChecked() else "Send selected stage")
        if hasattr(self, "beacon_button"):
            self.beacon_button.setEnabled(ready and not self.audio_test_mode.isChecked())

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

    def _discard_receive_for_tx(self):
        self.rx_timer.stop()
        self.live_preview_timeout.stop()
        self.auto_receive_timer.stop()
        if self.rx_audio:
            self.rx_audio.stop()
        self.rx_audio = None; self.rx_device = None
        self.rx_bytes.clear()
        if self.test_rx_socket is not None:
            self.test_rx_socket.close(); self.test_rx_socket.deleteLater(); self.test_rx_socket = None
        self.test_rx_pending = {}; self.test_rx_expected = None
        self.test_rx_final_pending = False; self.live_decode_final = False
        self.live_preview_state = None
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
            next_stage = "exchange"
        elif stage == "exchange":
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
        self.reply_report = report
        card = report.get("card") or {}
        if self.auto_armed:
            self.reply_to_button.setVisible(False)
            self.quick_draw_button.setVisible(False)
            return
        kind = card.get("message_type", "card")
        next_stage = {"cq": f"Reply to {card.get('callsign') or 'CQ'}", "exchange": "Send 73 with SNR"}.get(kind)
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

    def _handle_contact_message(self, report):
        card = report.get("card") or {}
        if not card.get("exact") or not card.get("avatar_burst"):
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
        if not self.auto_peer:
            self.auto_peer = callsign
        if kind == "cq" and self.auto_role == "responder":
            self.tx_snr_db = card.get("measured_snr_db")
            self.contact_stage = "send_exchange"
            self._refresh_contact_stage_indicator()
            self.exchange_log.append(f"CQ received from {callsign}; sending exchange card.")
            QTimer.singleShot(AUTO_REPLY_GUARD_MS, self.transmit_exchange)
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
            self.exchange_log.append(f"Exchange received from {callsign}; sending 73 with measured SNR.")
            QTimer.singleShot(AUTO_REPLY_GUARD_MS, self.transmit_exchange)
        elif kind == "73" and card.get("snr_db") is not None and self.auto_role == "responder":
            self.contact_stage = "send_final73"
            self._refresh_contact_stage_indicator()
            self.exchange_log.append(f"73 with SNR received from {callsign}; sending final 73.")
            QTimer.singleShot(AUTO_REPLY_GUARD_MS, self.transmit_exchange)
        elif kind == "73" and card.get("snr_db") is None and self.auto_role == "caller":
            self._complete_contact()

    def _complete_contact(self):
        self.contact_stage = "idle"
        self._refresh_contact_stage_indicator()
        if self.active_qso_log is not None:
            self.active_qso_log["status"] = "Complete"
            self.active_qso_log["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if self._active_qso_path:
                self._active_qso_path.write_text(json.dumps(self.active_qso_log, indent=2))
            self.refresh_qso_log()
            self.active_qso_log = None
            self._active_qso_path = None
        QTimer.singleShot(400, self._ensure_session_receive)
        self.auto_peer = ""
        self.last_cq_snapshot = None
        self.exchange_log.append("Contact exchange complete; all stage cards are in the QSO deck.")
        if self.auto_armed and self.auto_role == "caller":
            self.contact_stage = "listen_cq"
            self._refresh_contact_stage_indicator()
            if self.call_cq_active:
                self.cq_timer.start(self.cq_interval.value() * 60 * 1000)
            self.auto_should_listen = True
            QTimer.singleShot(300, self.start_receive)

    def transmit_exchange(self):
        if not self.auto_armed:
            self.contact_stage = "send_" + self._outgoing_message_type()
            self._refresh_contact_stage_indicator()
        if self.audio_test_mode.isChecked():
            self._prepare_test_transmission()
            return
        if not self.rig.connected():
            QMessageBox.warning(self, "CAT unavailable", "Connect to a Hamlib rigctld service before transmitting."); return
        self.tx_btn.setEnabled(False)
        generation = self.auto_generation if self.auto_armed else None
        self.read_rig(lambda ok: self._preflight_and_confirm_tx(ok, generation))

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
            self.tx_bytes = np.clip(audio * 32767, -32768, 32767).astype("<i2").tobytes()
            self.tx_offset = 0
            self.tx_deadline = duration + 30.0
            self.tx_label = "card burst" if is_card_backend(self.selected_mode()) else "loopback card"
        except Exception as exc:
            self.refresh_tx_button()
            if self.auto_armed: self.stop_auto_exchange("Automatic exchange stopped: its selected card could not be prepared.")
            QMessageBox.warning(self, "Couldn't prepare card", str(exc))
            return
        self._start_tx_audio()

    def start_auto_exchange(self):
        if self.exchange_mode.currentIndex() != 1:
            QMessageBox.information(self, "Select automatic exchange", "Choose Automatic card exchange before starting.")
            return
        if not is_card_backend(self.selected_mode()) or not get_backend(self.selected_mode()).checked:
            QMessageBox.warning(self, "Choose resilient burst", "Automatic CQ exchanges require resilient burst cards so the message type and integrity checks are available.")
            return
        link_ready = self.test_link_enabled and self.audio_test_mode.isChecked()
        devices_ready = bool(self.output_device.currentData() and self.input_device.currentData())
        if ((not self.audio_test_mode.isChecked() and not self.rig.connected()) or
                (not link_ready and not devices_ready)):
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
        self.exchange_log.append(f"Automatic {self.auto_role} flow armed.")
        if self.auto_role == "caller": self.transmit_exchange()
        else: self.start_receive()

    def stop_auto_exchange(self, log_message="Automatic exchange stopped by operator."):
        self.cq_timer.stop()
        self.call_cq_active = False
        if hasattr(self, "call_cq_button"):
            with QSignalBlocker(self.call_cq_button): self.call_cq_button.setChecked(False)
            self.call_cq_button.setText("Call CQ")
        self.auto_generation += 1
        self.auto_armed = False
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
        if self.rx_timer.isActive():
            self.rx_timer.stop()
            if self.rx_audio: self.rx_audio.stop()
            self.rx_audio = None; self.rx_device = None; self.rx_bytes.clear()
            if self.test_rx_socket is not None:
                self.test_rx_socket.close(); self.test_rx_socket.deleteLater(); self.test_rx_socket = None
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
            self.qso_log_dir.mkdir(parents=True, exist_ok=True)
            if getattr(self, "_active_qso_path", None) is None:
                self._active_qso_path = self.qso_log_dir / f"qso-{time.strftime('%Y%m%d-%H%M%S')}-{id(self.active_qso_log) & 0xffff:04x}.json"
            self._active_qso_path.write_text(json.dumps(self.active_qso_log, indent=2))
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
            self.tx_bytes = np.clip(audio * 32767, -32768, 32767).astype("<i2").tobytes()
            self.tx_offset = 0
            self.tx_deadline = duration + 8.0
            self.tx_label = "card burst" if is_card_backend(self.selected_mode()) else "legacy card"
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
            self.tx_timer.start()
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
        self.tx_timer.start()
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
                        self.cq_timer.start(self.cq_interval.value() * 60 * 1000)
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

    def transmit_beacon(self):
        """Operator-started, single card beacon through the normal CAT/PTT audio chain."""
        if not self.rig.connected():
            QMessageBox.warning(self, "CAT unavailable", "Connect to rigctld; PixelQSO will tune the radio to the selected beacon frequency.")
            return
        self.beacon_pending = True
        self.frequency.setValue(self.beacon_freq.value())
        self.tx_message_type = "card"
        self.tx_snr_db = None
        self.tx_btn.setEnabled(False)
        self.beacon_button.setEnabled(False)
        self.rig.request(f"set_freq {self.beacon_freq.value()}", 1, self._beacon_frequency_set)

    def _beacon_frequency_set(self, response):
        if not response or response[0] != "RPRT 0":
            self.beacon_pending = False
            self.refresh_tx_button()
            self.beacon_button.setEnabled(True)
            QMessageBox.warning(self, "Could not tune beacon frequency", "The radio did not accept the requested frequency: " + " ".join(response or []))
            return
        self.exchange_log.append(f"CAT tuned to beacon frequency {self.beacon_freq.value()} Hz; verifying frequency and mode.")
        self.read_rig(self._preflight_and_confirm_beacon)

    def _preflight_and_confirm_beacon(self, read_ok):
        if not read_ok or not self.rig.connected() or not self.rig_freq_read or not self.rig_mode:
            self.beacon_pending = False
            self.refresh_tx_button()
            QMessageBox.warning(self, "Rig readback failed", "Could not freshly read the rig frequency and mode. No beacon was started.")
            return
        actual_frequency = self.frequency.value()
        selected_frequency = self.beacon_freq.value()
        if abs(actual_frequency - selected_frequency) > 10:
            self.beacon_pending = False
            self.refresh_tx_button()
            QMessageBox.warning(self, "Beacon frequency not reached", f"CAT reports {actual_frequency} Hz after the tune request; the target is {selected_frequency} Hz. No beacon was started.")
            return
        if self.rig_mode not in {"USB", "USB-D", "PKTUSB", "DATA-U"}:
            self.beacon_pending = False
            self.refresh_tx_button()
            QMessageBox.warning(self, "Select upper-sideband data mode", f"The rig reports {self.rig_mode}. Set USB or an upper-sideband data mode and try again.")
            return
        try:
            self.update_identity()
            self.card.validate()
            audio = self._card_tx_audio(self.card, beacon=True)
            self.tx_bytes = np.clip(audio * 32767, -32768, 32767).astype("<i2").tobytes()
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
        worker = DecodeWorker(path, Path(self.combine.text()), profile, self.selected_mode())
        worker.signals.finished.connect(self.decode_finished)
        self.decode_pool.start(worker)

    def decode_finished(self, result):
        self.decode_jobs = max(0, self.decode_jobs - 1)
        name, report, error = result
        capture_path = Path(name).resolve()
        auto_capture = capture_path in self.auto_decode_paths
        self.auto_decode_paths.discard(capture_path)
        if error:
            self.rx_status.setText("Decode failed: " + error)
            if auto_capture and self.auto_armed:
                if self.auto_timeout_pending:
                    self._resolve_auto_receive_timeout()
                    return
                self.exchange_log.append("Receive decode failed; no reply sent. Resuming receive.")
                QTimer.singleShot(300, self.start_receive)
            return
        result_path = DATA / "qsl" / (Path(name).stem + ".json")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(report, indent=2))
        image = self.card_image(report)
        if image is not None:
            self.receive_view.setPixmap(QPixmap.fromImage(image).scaled(440, 440, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
        card = report.get("card") or {}
        is_avatar = bool(card.get("raw_avatar") or card.get("avatar_burst"))
        if card.get("exact"):
            self._add_session_wall_card(report)
        if card.get("raw_avatar"):
            if card.get("received_blocks") is not None:
                checked = sum(bool(value) for value in card["received_blocks"])
                total_blocks = len(card.get("received_blocks") or [])
                quality = "all blocks verified" if card.get("exact") else f"{checked}/{total_blocks} verified blocks; missing blocks remain blank"
                self.rx_status.setText(f"Resilient burst · {quality} · saved {result_path.name}")
            else:
                self.rx_status.setText(f"Raw burst · identity decoded, pixels unverified · saved {result_path.name}")
        card_key = received_card_key(report)
        handled_live = auto_capture and card_key in self.auto_live_handled_cards
        if handled_live:
            self.auto_live_handled_cards.discard(card_key)
        else:
            already_saved = card.get("exact") and card_key in self.seen_received_cards
            if card.get("exact") and card.get("callsign") and card.get("card_id") is not None:
                self.seen_received_cards.add(card_key)
            if not already_saved:
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
            if is_avatar and card.get("exact"):
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
            fresh_complete = bool(card_meta.get("exact"))
        else:
            fresh_identity = card_meta.get("card_id") in valid_current_ids
            fresh_complete = card_meta.get("card_id") in complete_current_ids
        if (not fresh_identity or not fresh_complete or not card_meta.get("exact")
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

    def start_receive(self, quiet=False):
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
        if self.test_link_enabled and self.audio_test_mode.isChecked():
            self.test_rx_socket = QUdpSocket(self)
            if not self.test_rx_socket.bind(QHostAddress(QHostAddress.SpecialAddress.LocalHost), self.test_rx_port):
                message = f"Could not open the local test-audio port {self.test_rx_port}: {self.test_rx_socket.errorString()}"
                self.test_rx_socket.deleteLater(); self.test_rx_socket = None
                receive_error("Local audio link unavailable", message)
                return
            self.test_rx_expected = None
            self.test_rx_pending = {}
        else:
            device = self.input_device.currentData()
            if not device:
                receive_error("No audio input", "Not listening · select an audio input in Station settings.")
                return
            fmt = QAudioFormat(); fmt.setSampleRate(modem.SAMPLE_RATE); fmt.setChannelCount(1); fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            if not device.isFormatSupported(fmt):
                receive_error("Audio format unsupported", "Not listening · the selected input does not support 48 kHz mono 16-bit audio.")
                return
            self.rx_audio = QAudioSource(device, fmt, self)
            self.rx_device = self.rx_audio.start()
            if not self.rx_device:
                self.rx_audio = None
                receive_error("Audio failed", "Not listening · could not open the selected audio input.")
                return
        self.rx_bytes.clear()
        self.rx_live_profile = self.selected_profile() if (self.test_link_enabled or is_card_backend(self.selected_mode())) else None
        self.live_decode_inflight = False; self.live_decode_samples = 0; self.rx_timer.start()
        self.auto_receive_timer.stop()
        self.listen_indicator.setText("● LISTENING")
        self.listen_indicator.setStyleSheet("color:#8bd8ae;font-weight:700;letter-spacing:1px")
        self.receive_stack.setCurrentWidget(self.session_wall_page)
        self.rx_status.setText("Local two-window audio link ready · listening" if self.test_rx_socket else
                               f"Listening · {self.session_wall_count} cards heard this session")

    def _drain_rx(self):
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
        # Keep receive memory bounded during long quiet sessions. The window
        # is much longer than a normal card burst and is cleared after a card.
        max_rx_bytes = self.rx_rate * 180 * 2
        if len(self.rx_bytes) > max_rx_bytes:
            trim_bytes = len(self.rx_bytes) - max_rx_bytes
            trim_bytes -= trim_bytes % 2
            del self.rx_bytes[:trim_bytes]
            self.live_decode_samples = max(0, self.live_decode_samples - trim_bytes // 2)
        if not self.rx_timer.isActive() or self.live_decode_inflight:
            return
        sample_count = len(self.rx_bytes) // 2
        if is_card_backend(self.selected_mode()):
            backend = get_backend(self.selected_mode())
            minimum_samples = int(backend.minimum_audio_seconds() * self.rx_rate)
            interval = max(1, int((1.0 if self.selected_mode().startswith("data2g_") else 0.5) * self.rx_rate))
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
        worker = LiveDecodeWorker(snapshot, self.rx_rate, self.rx_live_profile or "auto", self.auto_generation,
                                  self.selected_mode())
        worker.signals.finished.connect(self._live_decode_finished)
        self.live_decode_inflight = True
        self.decode_pool.start(worker)

    def _merge_live_preview(self, report):
        """Keep the best accumulated pixels for one burst identity in the live view."""
        incoming_card = report.get("card") or {}
        if not (incoming_card.get("raw_avatar") or incoming_card.get("avatar_burst")):
            return report, True
        width, height = int(incoming_card.get("width", 32)), int(incoming_card.get("height", 32))
        count = width * height
        mode = incoming_card.get("avatar_mode", "raw")
        palette = tuple(tuple(rgb) for rgb in incoming_card.get("palette", PALETTE))
        key = (str(incoming_card.get("callsign", "")).upper(), incoming_card.get("card_id"),
               incoming_card.get("message_type", "card"), incoming_card.get("snr_db"),
               width, height, palette, mode)
        state = self.live_preview_state
        changed = state is None or state["key"] != key
        if changed:
            state = {"key": key, "pixels": [0] * count, "coverage": [False] * count,
                     "received_blocks": [False] * len(incoming_card.get("received_blocks") or []),
                     "copies": 0, "card": {}}
            self.live_preview_state = state

        pixels = report.get("pixels") or []
        coverage = incoming_card.get("pixel_coverage")
        if coverage is None:
            coverage = [True] * min(count, len(pixels))
        copies = int(incoming_card.get("received_copies", 0) or 0)
        more_raw_evidence = incoming_card.get("raw_avatar") and copies > state["copies"]
        for index in range(min(count, len(pixels), len(coverage))):
            if not coverage[index]:
                continue
            if not state["coverage"][index] or more_raw_evidence:
                state["pixels"][index] = pixels[index]
                state["coverage"][index] = True
                changed = True

        blocks = incoming_card.get("received_blocks")
        if blocks is not None:
            if len(state["received_blocks"]) != len(blocks):
                state["received_blocks"] = [False] * len(blocks)
            for index, verified in enumerate(blocks):
                if verified and not state["received_blocks"][index]:
                    state["received_blocks"][index] = True
                    changed = True
        if copies > state["copies"]:
            state["copies"] = copies
            changed = True
        state["card"].update(incoming_card)
        merged_card = {**state["card"], "pixel_coverage": state["coverage"],
                       "received_blocks": state["received_blocks"] if blocks is not None else None,
                       "received_copies": state["copies"]}
        if blocks is not None:
            checked = sum(state["received_blocks"])
            merged_card["color_stage"] = f"{checked}/{len(state['received_blocks'])} checked blocks"
            merged_card["exact"] = bool(state["received_blocks"]) and all(state["received_blocks"])
            merged_card["corrected_blocks"] = max(int(state["card"].get("corrected_blocks", 0) or 0),
                                                   int(incoming_card.get("corrected_blocks", 0) or 0))
        else:
            merged_card["color_stage"] = (f"{sum(state['coverage'])}/{count} pixels · "
                                           f"{state['copies']} copies · unverified")
        merged_report = {**report, "card": merged_card, "pixels": state["pixels"]}
        return merged_report, changed

    def _live_decode_finished(self, result):
        generation, report, complete, error = result
        self.live_decode_inflight = False
        if generation != self.auto_generation or not self.rx_timer.isActive():
            return
        if error:
            if self.selected_mode().startswith("data2g_") and "No checked Data2G card fragment received yet" in error:
                QTimer.singleShot(0, self._drain_rx)
                return
            self.rx_status.setText("Listening · no complete card decoded yet")
            QTimer.singleShot(0, self._drain_rx)
            return
        card = report.get("card") or {}
        if card.get("card_id") is not None:
            self.rx_live_profile = report.get("receive_profile")
        report, improved = self._merge_live_preview(report)
        card = report.get("card") or {}
        if card.get("avatar_burst") and card.get("exact"):
            complete = True
        image = self.card_image(report) if improved else None
        if card.get("card_id") is not None or card.get("preview_received"):
            live_status = (f"{card.get('color_stage') or 'waiting for image data'} · "
                           f"{report.get('valid_packet_count', 0)} packets received")
            if image is not None and (card.get("preview_received") or card.get("avatar_burst") or card.get("raw_avatar")):
                self.receive_view.setPixmap(QPixmap.fromImage(image).scaled(
                    440, 440, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
                self.receive_stack.setCurrentWidget(self.receive_view)
                self.live_preview_timeout.start(5000)
            self.rx_status.setText(f"Live image · {card.get('callsign') or 'station not identified'} · {live_status}")
        if complete:
            if self.auto_armed:
                key = received_card_key(report)
                if (card.get("message_type") != "cq" and card.get("exact") and card.get("callsign")
                        and card.get("card_id") is not None and key in self.seen_received_cards):
                    self.rx_status.setText(f"Already received {card.get('callsign')} card {card.get('card_id')}; staying on receive.")
                    self._add_session_wall_card(report)
                    self.rx_bytes.clear()
                    self.live_decode_samples = 0
                    self.live_decode_final = False
                    self.live_preview_state = None
                    self.receive_stack.setCurrentWidget(self.session_wall_page)
                    QTimer.singleShot(0, self._drain_rx)
                    return
                complete_text = ("All image blocks verified; saving the full capture and finalizing its QSL."
                                 if card.get("avatar_burst") else
                                 "Complete-card marker received; saving the full capture and finalizing its QSL.")
                self.exchange_log.append(complete_text)
                if card.get("exact") and card.get("callsign") and card.get("card_id") is not None:
                    self._add_session_wall_card(report)
                    self.seen_received_cards.add(key)
                    self.auto_live_handled_cards.add(key)
                    self._record_received_card(report)
                    self._handle_contact_message(report)
            else:
                self.rx_status.setText("All image blocks verified; saving and finalizing the card." if card.get("avatar_burst")
                                       else "Complete-card marker received; saving and finalizing the QSL.")
                if card.get("exact"):
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
        self.rx_timer.stop(); self._drain_rx()
        self.auto_receive_timer.stop()
        if self.rx_audio: self.rx_audio.stop()
        self.rx_audio = None; self.rx_device = None
        if self.test_rx_socket is not None:
            self.test_rx_socket.close(); self.test_rx_socket.deleteLater(); self.test_rx_socket = None
        self.test_rx_pending = {}; self.test_rx_expected = None
        self.test_rx_final_pending = False; self.live_decode_final = False
        self.listen_indicator.setText("● PROCESSING")
        self.listen_indicator.setStyleSheet("color:#e5b36e;font-weight:700;letter-spacing:1px")
        if len(self.rx_bytes) < 4096:
            self.rx_status.setText("Capture was too short; no audio saved."); self.rx_bytes.clear()
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
        self.rx_bytes.clear()
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
        if not card.get("exact"):
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
        self.rx_bytes.clear()
        self.live_decode_samples = 0
        self.live_decode_final = False
        self.live_preview_state = None
        self.live_preview_timeout.stop()
        self.receive_stack.setCurrentWidget(self.session_wall_page)
        self.rx_status.setText(f"Listening · {self.session_wall_count} cards heard this session")
        self.listen_indicator.setText("● LISTENING")
        self.listen_indicator.setStyleSheet("color:#8bd8ae;font-weight:700;letter-spacing:1px")
        QTimer.singleShot(0, self._drain_rx)

    def _return_to_session_wall(self):
        if self.rx_timer.isActive() and self.receive_stack.currentWidget() is self.receive_view:
            self.receive_stack.setCurrentWidget(self.session_wall_page)
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
            if bucket == "received_cards" and new_card.get("exact"):
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
        self.qso_log_dir.mkdir(parents=True, exist_ok=True)
        self._active_qso_path.write_text(json.dumps(self.active_qso_log, indent=2))
        self.refresh_qso_log()

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
        if self.web_server is not None:
            self.web_server.close(); self.web_server = None
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PixelQSO")
    app.setOrganizationName("PixelQSO")
    win = MainWindow(); win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

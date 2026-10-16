"""Two-card authoring controls around the existing drawing canvas."""
import copy
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QRect, QTimer, QSignalBlocker
from PySide6.QtGui import QIcon, QPixmap, QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                              QPushButton, QCheckBox, QSpinBox, QFileDialog,
                              QMessageBox, QSlider)

import cardmodem as modem
from card_animation import artwork_digest, validate_link
from card_views import card_art


def write_pair(paths, documents):
    """Stage both documents before replacing either; roll back a failed write."""
    originals = {path: path.read_bytes() if path.exists() else None for path in paths}
    staged = []
    replaced = []
    try:
        for path, document in zip(paths, documents):
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
                staged.append(Path(stream.name))
                stream.write(json.dumps(document, indent=2) + '\n')
        for path, temporary in zip(paths, staged):
            os.replace(temporary, path)
            replaced.append(path)
    except Exception:
        for path in reversed(replaced):
            if originals[path] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(originals[path])
        raise
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)


def export_gif(path, images, frame_ms):
    """Keep full card artwork in both frames and loop at the chosen timing."""
    from PIL import Image
    frames = []
    for image in images:
        image = image.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        frames.append(Image.frombytes('RGBA', (image.width(), image.height()),
                                     bytes(image.constBits()), 'raw', 'RGBA', image.bytesPerLine()).convert('RGB'))
    # One palette for the whole animation keeps identical frame chrome stable.
    # Independent GIF quantization can shift lavender antialiasing by one RGB step.
    atlas = Image.new('RGB', (frames[0].width, sum(frame.height for frame in frames)))
    y = 0
    for frame in frames:
        atlas.paste(frame, (0, y))
        y += frame.height
    palette = atlas.quantize(colors=256)
    frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    frames[0].save(str(path), format='GIF', save_all=True, append_images=frames[1:],
                   duration=frame_ms, loop=0, disposal=2, optimize=False)


class AnimationAuthor(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.frames = []
        self.active = 0
        self.busy = False
        self.preview_index = 0
        self._preview_images = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        entry = QHBoxLayout()
        heading = QLabel('Animation'); heading.setObjectName('sectionTitle')
        entry.addWidget(heading)
        self.start_button = QPushButton('Animate this card')
        self.start_button.setToolTip('Create a second frame from this drawing, keeping stamps editable.')
        self.start_button.clicked.connect(self.start)
        entry.addStretch(); entry.addWidget(self.start_button)
        layout.addLayout(entry)
        self.controls = QWidget()
        controls = QVBoxLayout(self.controls); controls.setContentsMargins(0, 0, 0, 0)
        strip = QHBoxLayout(); self.frame_buttons = []
        for index in range(2):
            button = QPushButton(f'Frame {index+1}')
            button.setCheckable(True); button.setIconSize(QSize(48, 48))
            button.setMinimumHeight(60)
            button.clicked.connect(lambda _=False, i=index: self.switch(i))
            strip.addWidget(button, 1); self.frame_buttons.append(button)
        self.flip_button = QPushButton('Flip ⇄')
        self.flip_button.setToolTip('Switch frames · Alt+Right / Alt+Left')
        self.flip_button.clicked.connect(lambda: self.switch(1-self.active))
        strip.addWidget(self.flip_button)
        controls.addLayout(strip)
        options = QHBoxLayout()
        self.onion = QCheckBox('Onion skin'); self.onion.setChecked(True)
        self.onion.setToolTip('Overlay the other frame as a faint drawing guide; excluded from saved art and exports.')
        self.onion.toggled.connect(self.update_overlay)
        options.addWidget(self.onion)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(10, 70); self.opacity.setValue(30); self.opacity.setMaximumWidth(90)
        self.opacity.setAccessibleName('Onion skin opacity')
        self.opacity.valueChanged.connect(self.update_overlay)
        options.addWidget(self.opacity)
        options.addStretch()
        self.duration = QSpinBox(); self.duration.setRange(100, 10000)
        self.duration.setSingleStep(100); self.duration.setValue(500)
        self.duration.setSuffix(' ms / frame'); self.duration.setAccessibleName('Animation frame duration')
        self.duration.valueChanged.connect(self.timing_changed)
        options.addWidget(self.duration)
        controls.addLayout(options)
        transfers = QHBoxLayout()
        self.copy_selection = QPushButton('Copy selection → other frame')
        self.copy_selection.setToolTip('Copy the box selection or selected stamp, then switch to move or resize the copy.')
        self.copy_selection.clicked.connect(self.copy_to_other)
        transfers.addWidget(self.copy_selection)
        self.copy_frame = QPushButton('Copy whole frame → other')
        self.copy_frame.setToolTip('Replace the other frame with this drawing. Undo on that frame restores it.')
        self.copy_frame.clicked.connect(self.copy_whole_frame)
        transfers.addWidget(self.copy_frame)
        controls.addLayout(transfers)
        self.hint = QLabel('Edit either frame with the drawing tools below. Each frame has its own Undo / Redo.')
        self.hint.setWordWrap(True); self.hint.setObjectName('supportingText')
        controls.addWidget(self.hint)
        layout.addWidget(self.controls)
        self.controls.hide()

        self.preview_panel = QWidget()
        preview = QVBoxLayout(self.preview_panel); preview.setContentsMargins(0, 0, 0, 0)
        label = QLabel('Live animation preview'); label.setObjectName('sectionTitle'); preview.addWidget(label)
        self.preview = QLabel(); self.preview.setFixedSize(160, 200)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setAccessibleName('Live animation preview')
        preview.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignCenter)
        self.preview_frame = QLabel('Frame 1 / 2')
        self.preview_frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.addWidget(self.preview_frame)
        buttons = QHBoxLayout()
        self.play = QPushButton('Pause'); self.play.setCheckable(True)
        self.play.toggled.connect(self.play_changed); buttons.addWidget(self.play)
        self.export = QPushButton('Export GIF…'); self.export.clicked.connect(self.export_dialog)
        buttons.addWidget(self.export); preview.addLayout(buttons)
        self.preview_panel.hide()
        self.timer = QTimer(self); self.timer.timeout.connect(self.advance)
        self.timer.setInterval(500)
        for key in ('Alt+Right', 'Alt+Left'):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            # Parent is changed by attach_shortcuts so drawing focus also works.
            shortcut.activated.connect(lambda: self.switch(1-self.active) if self.frames else None)
        self.window.canvas.changed.connect(self.changed)

    def attach_shortcuts(self, editor):
        for shortcut in self.findChildren(QShortcut):
            shortcut.setParent(editor)

    def capture(self, history=False):
        canvas = self.window.canvas
        frame = self.frames[self.active]
        frame.update(card=self.window.card, path=self.window.card_path,
                     snapshot=canvas.snapshot(), selected=canvas.selected_layer,
                     selection=QRect(canvas.selection_rect), cursor=canvas.keyboard_cursor,
                     color=canvas.color_index)
        if history:
            frame.update(undo=copy.deepcopy(canvas.undo_stack), redo=copy.deepcopy(canvas.redo_stack))

    def record(self, card, path=None, data=None):
        data = data or {}
        return dict(card=card, path=path,
                    snapshot=copy.deepcopy((card.width, card.height, card.palette,
                        data.get('base_pixels', card.pixels), data.get('stamp_layers', []))),
                    undo=[], redo=[], selected=None, selection=QRect(), cursor=(0, 0), color=1,
                    saved=json.loads(path.read_text()) if path and path.exists() else None)

    def reset(self):
        self.frames = []; self.active = 0
        self.timer.stop(); self.controls.hide(); self.preview_panel.hide()
        self.start_button.show(); self._preview_images = []
        self.window.canvas.onion_image = None; self.window.canvas.update()
        self.window.editor_format_widget.setEnabled(True)
        self.window.editor_save_button.setText('Save')
        if hasattr(self.window, 'editor_done_button'):
            self.window.editor_done_button.setText('Save and close')

    def start(self):
        if getattr(self.window, 'quick_draw_active', False):
            self.window.status.setText('Save a normal card to author an animation outside Quick draw.')
            return
        self.window.update_identity()
        if self.window.card.animation:
            QMessageBox.information(self.window, 'First frame unavailable',
                'Open this card with its matching first frame in My Cards to edit the animation.')
            return
        self.frames = [self.record(self.window.card, self.window.card_path)]
        self.active = 0; self.capture(history=True)
        second = copy.deepcopy(self.frames[0])
        second['card'] = copy.deepcopy(self.window.card)
        second['card'].card_id = self.window._new_card_id()
        second.update(path=None, undo=[], redo=[], saved=None)
        self.frames.append(second)
        self.enable(); self.switch(1)
        self.window.status.setText('Frame 2 created · move a stamp or box-select pixels to change the pose.')

    def enable(self):
        self.start_button.hide(); self.controls.show(); self.preview_panel.show()
        self.window.editor_format_widget.setEnabled(False)
        self.window.editor_save_button.setText('Save both frames')
        self.window.editor_save_button.setToolTip('Save both drawings and their animation reference together.')
        self.window.editor_done_button.setText('Save both frames and close')
        self.play.setChecked(False)
        self.timer.start(self.duration.value())
        self.changed()

    def load_pair(self, path, data, folder):
        """Only open companions whose saved artwork matches the existing reference."""
        self.reset()
        current = self.window.card
        for candidate in sorted(set(folder.glob('*.json')) | set(path.parent.glob('*.json'))):
            if candidate == path:
                continue
            try:
                other_data = json.loads(candidate.read_text())
                other = modem.Card(other_data['callsign'], other_data['grid'],
                    [tuple(c) for c in other_data['palette']], other_data['pixels'],
                    other_data['card_id'], other_data.get('width', 32), other_data.get('height', 32),
                    copy.deepcopy(other_data.get('animation')))
                other.validate()
                first, second = (other, current) if current.animation else (current, other)
                link = second.animation
                if (not link or first.animation or first.card_id != link['first_card_id'] or
                    first.callsign.upper() != second.callsign.upper() or
                    (first.width, first.height) != (second.width, second.height) or
                    artwork_digest(first) != link['first_digest']):
                    continue
                pair = [self.record(current, path, data), self.record(other, candidate, other_data)]
                self.active = 1 if current.animation else 0
                self.frames = pair[::-1] if current.animation else pair
                self.capture()
                with QSignalBlocker(self.duration):
                    self.duration.setValue(link['frame_ms'])
                self.enable()
                return
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                continue

    def switch(self, index):
        if not self.frames or index == self.active:
            if self.frames: self.refresh()
            return
        self.window.update_identity(); self.capture(history=True)
        call, grid = self.window.card.callsign, self.window.card.grid
        self.busy = True
        try:
            self.active = index
            frame = self.frames[index]; card = frame['card']
            card.callsign, card.grid = call, grid
            self.window.card, self.window.card_path = card, frame['path']
            _, _, _, base, layers = frame['snapshot']
            canvas = self.window.canvas
            canvas.set_card(card, base, layers)
            canvas.undo_stack = copy.deepcopy(frame['undo']); canvas.redo_stack = copy.deepcopy(frame['redo'])
            canvas.selected_layer = frame['selected']; canvas.selection_rect = QRect(frame['selection'])
            canvas.keyboard_cursor = frame['cursor']; canvas.color_index = frame['color']
            self.window._set_editor_format(card); self.window._sync_editor_controls()
        finally:
            self.busy = False
        self.refresh(); self.window._update_editor_saved_state()
        self.window.canvas.setFocus()

    def signature(self):
        if not self.frames:
            return None
        # Do not include active-frame/UI selection state: flipping isn't an edit.
        return (self.duration.value(), tuple(
            (frame['card'].card_id, self.window.canvas.snapshot() if i == self.active else frame['snapshot'],
             json.dumps(frame['card'].animation, sort_keys=True))
            for i, frame in enumerate(self.frames)))

    def image(self, index):
        card = self.frames[index]['card']
        return self.window.card_image({'card': asdict(card), 'pixels': card.pixels})

    def framed_images(self, size=QSize(320, 400)):
        title = self.window.call.text().strip().upper() or 'My card'
        return [card_art(self.image(i), title, 'TWO-FRAME ANIMATION', size) for i in range(2)]

    def changed(self):
        if self.busy or not self.frames:
            return
        self.capture(); self.refresh()

    def refresh(self):
        for i, button in enumerate(self.frame_buttons):
            button.setChecked(i == self.active)
            button.setText(f'Frame {i+1}' + (' · Editing' if i == self.active else ''))
            button.setIcon(QIcon(QPixmap.fromImage(self.image(i)).scaled(
                QSize(48, 48), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)))
            path = self.frames[i]['path']
            button.setToolTip(f'{path.name if path else "Unsaved frame"} · click to edit frame {i+1}')
        canvas = self.window.canvas
        self.copy_selection.setEnabled(canvas.selected_layer is not None or not canvas.selection_rect.isNull())
        self.update_overlay()
        self._preview_images = self.framed_images(QSize(160, 200))
        self.show_preview()

    def update_overlay(self, *_):
        canvas = self.window.canvas
        canvas.onion_image = self.image(1-self.active) if self.frames and self.onion.isChecked() else None
        canvas.onion_opacity = self.opacity.value()/100
        canvas.update()

    def timing_changed(self, *_):
        self.timer.setInterval(self.duration.value())
        self.window._update_editor_saved_state()

    def play_changed(self, paused):
        self.play.setText('Play' if paused else 'Pause')
        if paused: self.timer.stop()
        elif self.frames: self.timer.start(self.duration.value())

    def advance(self):
        if self.preview_panel.isVisible():
            self.preview_index = 1-self.preview_index
            self.show_preview()

    def show_preview(self):
        if self._preview_images:
            self.preview.setPixmap(self._preview_images[self.preview_index])
            self.preview_frame.setText(f'Frame {self.preview_index+1} / 2')
            self.preview.setAccessibleDescription(f'Animation preview · frame {self.preview_index+1}')

    def copy_to_other(self):
        canvas = self.window.canvas
        if not canvas.selection_rect.isNull():
            box = canvas.selection_rect.intersected(QRect(0, 0, self.window.card.width, self.window.card.height))
            layer = dict(x=box.x(), y=box.y(), width=box.width(), height=box.height(),
                         pixels=[self.window.card.pixels[y*self.window.card.width+x]
                                 for y in range(box.top(), box.bottom()+1) for x in range(box.left(), box.right()+1)])
        elif canvas.selected_layer is not None:
            layer = copy.deepcopy(canvas.stamp_layers[canvas.selected_layer])
        else:
            return
        palette = list(self.window.card.palette)
        destination = self.frames[1-self.active]['card']
        if layer['width'] > destination.width or layer['height'] > destination.height:
            self.window.status.setText('Selection is larger than the other frame. Copy the whole frame to match dimensions first.')
            return
        self.switch(1-self.active)
        # Imported pairs can use different palettes; preserve displayed colors.
        target = self.window.card.palette
        mapping = [min(range(len(target)), key=lambda j: sum((color[k]-target[j][k])**2 for k in range(3)))
                   for color in palette]
        layer['pixels'] = [None if value is None else mapping[value] for value in layer['pixels']]
        layer['x'] = min(layer['x'], self.window.card.width-layer['width'])
        layer['y'] = min(layer['y'], self.window.card.height-layer['height'])
        canvas.remember_edit()
        canvas.stamp_layers.append(layer); canvas.selected_layer = len(canvas.stamp_layers)-1
        self.window.set_editor_tool('move')
        canvas._render_layers()
        self.window.status.setText('Copied to this frame · drag or use arrow keys to move; Undo removes the copy.')

    def copy_whole_frame(self):
        canvas = self.window.canvas
        snapshot = canvas.snapshot()
        self.switch(1-self.active)
        canvas.remember_edit()
        width, height, palette, base, layers = snapshot
        self.window.card.width, self.window.card.height, self.window.card.palette = width, height, palette
        canvas.base_pixels, canvas.stamp_layers = base, layers
        canvas.selected_layer = None; canvas.selection_rect = QRect()
        canvas._render_layers(); self.window._set_editor_format(self.window.card)
        self.window.status.setText('Frame copied · Undo restores the previous drawing on this frame.')

    def save(self, folder):
        self.window.update_identity(); self.capture()
        cards = [copy.deepcopy(frame['card']) for frame in self.frames]
        for card in cards:
            card.callsign = self.window.call.text().strip().upper()
            card.grid = self.window.grid.text().strip().upper()
        if (cards[0].width, cards[0].height) != (cards[1].width, cards[1].height):
            raise ValueError('Both animation frames must have the same dimensions. Undo the format change before saving.')
        cards[0].animation = None
        for card, frame in zip(cards, self.frames):
            if frame['saved'] and artwork_digest(card) != artwork_digest(frame['saved']):
                card.card_id = self.window._new_card_id()
        cards[1].animation = validate_link(dict(first_card_id=cards[0].card_id,
            first_digest=artwork_digest(cards[0]), frame_ms=self.duration.value()), cards[1].card_id)
        if self.frames[1]['saved'] and cards[1].animation != self.frames[1]['saved'].get('animation'):
            cards[1].card_id = self.window._new_card_id()
        for card in cards: card.validate()
        paths = [frame['path'] for frame in self.frames]
        if paths[0] is None:
            name, _ = QFileDialog.getSaveFileName(self.window.editor_dialog, 'Save animation · first frame',
                str(folder / f'{cards[0].callsign or "card"}-animation.json'), 'Pixel QSO card (*.json)')
            if not name: return False
            paths[0] = Path(name).with_suffix('.json')
        if paths[1] is None:
            paths[1] = paths[0].with_name(paths[0].stem + '-frame-2.json')
            suffix = 2
            while paths[1].exists() or paths[1] == paths[0]:
                paths[1] = paths[0].with_name(f'{paths[0].stem}-frame-2-{suffix}.json'); suffix += 1
        if paths[0] == paths[1]:
            raise ValueError('Choose different files for the two frames.')
        documents = []
        for card, frame in zip(cards, self.frames):
            _, _, _, base, layers = frame['snapshot']
            # Preserve extra editor metadata in existing files.
            document = json.loads(frame['path'].read_text()) if frame['path'] and frame['path'].exists() else {}
            document.update(asdict(card), base_pixels=base, stamp_layers=layers)
            documents.append(document)
        write_pair(paths, documents)
        for card, frame, path in zip(cards, self.frames, paths):
            frame['card'].__dict__.update(copy.deepcopy(vars(card)))
            frame.update(path=path, saved=copy.deepcopy(asdict(card)))
        self.window.card_path = paths[self.active]
        self.window.editor_baseline = self.window.editor_signature()
        self.window._update_editor_saved_state(); self.window.refresh_card_library()
        self.window.status.setText(f'Saved both frames · {paths[0].name} + {paths[1].name}')
        return True

    def export_dialog(self):
        name, _ = QFileDialog.getSaveFileName(self.window.editor_dialog, 'Export framed animation',
            str((self.frames[0]['path'] or Path('animation.json')).with_suffix('.gif')), 'Animated GIF (*.gif)')
        if not name: return
        try:
            target = Path(name).with_suffix('.gif')
            export_gif(target, self.framed_images(), self.duration.value())
            self.window.status.setText(f'Exported framed animation · {target.name}')
        except Exception as exc:
            QMessageBox.warning(self.window.editor_dialog, "Couldn't export animation", str(exc))

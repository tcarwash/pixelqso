"""Exercise two-frame drawing, persistence, and framed GIFs through real Qt controls."""
import copy
import json
import os
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PIL import Image
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QImage, QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import app as pixelqso
from card_animation import artwork_digest, encode_link, decode_link
from animation_editor import write_pair


@pytest.fixture
def editor(tmp_path, monkeypatch):
    application = QApplication.instance() or QApplication([])
    monkeypatch.setenv('PIXELQSO_DATA_DIR', str(tmp_path))
    (tmp_path/'settings.ini').write_text('[radio]\nauto_connect_cat=false\n')
    original_data = pixelqso.DATA
    with patch('app.QTimer.singleShot'):
        window = pixelqso.MainWindow()
    window.call.setText('AG7SU'); window.grid.setText('CN85')
    window.editor_dialog.show(); application.processEvents()
    yield window
    window.editor_baseline = window.editor_signature()
    window.editor_dialog.close(); window.close()
    application.processEvents(); pixelqso.DATA = original_data


def draw_pixel(window, x=5, y=5, color=6):
    window.set_editor_tool('draw'); window.canvas.color_index = color
    QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton,
                     pos=window.canvas.pixel_position(x, y))


def save_pair(window, tmp_path):
    target = tmp_path/'cards'/'dance.json'
    with patch('animation_editor.QFileDialog.getSaveFileName', return_value=(str(target), '')):
        assert window.save_card()
    return target, target.with_name('dance-frame-2.json')


def test_flip_keeps_drawing_history_layers_and_dirty_state(editor):
    author = editor.animation_author
    image = QImage(4, 4, QImage.Format.Format_ARGB32); image.fill(QColor('red'))
    editor.canvas.add_stamp(image)
    original = list(editor.card.pixels)
    author.start_button.click()
    assert author.active == 1 and editor.card.pixels == original
    assert len(editor.canvas.stamp_layers) == 1
    assert editor.canvas.onion_image is not None
    draw_pixel(editor)
    changed = list(editor.card.pixels)
    signature = editor.editor_signature()
    author.frame_buttons[0].click()
    assert editor.card.pixels == original
    assert editor.editor_signature() == signature
    author.flip_button.click()
    assert editor.card.pixels == changed
    editor.canvas.undo(); assert editor.card.pixels == original
    editor.canvas.redo(); assert editor.card.pixels == changed
    # Shortcut works while the drawing canvas owns keyboard focus.
    editor.canvas.setFocus()
    QTest.keyClick(editor.canvas, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
    assert author.active == 0


def test_copy_stamp_to_other_frame_is_movable_and_undoable(editor):
    author = editor.animation_author; author.start()
    image = QImage(3, 3, QImage.Format.Format_ARGB32); image.fill(Qt.GlobalColor.transparent)
    image.setPixelColor(1, 1, QColor('red'))
    editor.canvas.add_stamp(image)
    original = copy.deepcopy(editor.canvas.stamp_layers[-1])
    author.copy_selection.click()
    assert author.active == 0
    assert editor.canvas.stamp_layers[-1] == original
    assert editor.canvas.tool == 'move'
    QTest.keyClick(editor.canvas, Qt.Key.Key_Right)
    assert editor.canvas.stamp_layers[-1]['x'] == 1
    editor.canvas.undo(); assert editor.canvas.stamp_layers[-1]['x'] == 0
    editor.canvas.undo(); assert editor.canvas.stamp_layers == []
    author.switch(1)
    assert editor.canvas.stamp_layers[-1] == original


def test_copy_box_and_whole_frame_reuse_canvas_undo(editor):
    author = editor.animation_author; author.start()
    draw_pixel(editor)
    editor.canvas.selection_rect = QRect(5, 5, 1, 1)
    editor.canvas.changed.emit()
    author.copy_selection.click()
    assert author.active == 0
    assert editor.canvas.stamp_layers[-1]['pixels'] == [6]
    assert editor.canvas.stamp_layers[-1]['x'] == 5
    editor.canvas.undo()
    author.switch(1)
    source = editor.canvas.snapshot()
    author.copy_frame.click()
    assert author.active == 0 and editor.canvas.snapshot() == source
    editor.canvas.undo()
    assert editor.canvas.snapshot() != source


def test_pair_save_reopen_either_frame_and_refresh_reference(editor, tmp_path):
    author = editor.animation_author; author.start(); draw_pixel(editor)
    author.duration.setValue(700)
    first_path, second_path = save_pair(editor, tmp_path)
    first = json.loads(first_path.read_text()); second = json.loads(second_path.read_text())
    assert first['animation'] is None
    assert second['animation'] == dict(first_card_id=first['card_id'], first_digest=artwork_digest(first), frame_ms=700)
    assert first['card_id'] != second['card_id']
    digest, link = decode_link(encode_link(author.frames[1]['card']))
    assert link == second['animation']
    baseline = editor.editor_signature()
    author.switch(0); assert editor.editor_signature() == baseline
    for path, active in ((first_path, 0), (second_path, 1)):
        editor._load_card_path(path)
        assert author.active == active and len(author.frames) == 2
        assert author.frames[0]['card'].pixels == first['pixels']
        assert author.frames[1]['card'].pixels == second['pixels']
    author.switch(0); draw_pixel(editor, 8, 8, 5)
    assert editor.save_card()
    revised_first = json.loads(first_path.read_text()); revised_second = json.loads(second_path.read_text())
    assert revised_first['card_id'] != first['card_id']
    assert revised_second['card_id'] != second['card_id']
    assert revised_second['animation']['first_digest'] == artwork_digest(revised_first)
    # Timing edits alone also produce a fresh second-frame identity.
    author.duration.setValue(800); assert editor.save_card()
    assert json.loads(second_path.read_text())['card_id'] != revised_second['card_id']


def test_cancel_save_keeps_both_drafts_and_new_card_resets_author(editor, tmp_path):
    author = editor.animation_author; author.start(); draw_pixel(editor)
    signature = editor.editor_signature()
    with patch('animation_editor.QFileDialog.getSaveFileName', return_value=('', '')):
        assert editor.save_card() is False
    assert editor.editor_signature() == signature
    assert not list((tmp_path/'cards').glob('*.json'))
    with patch.object(editor, 'confirm_editor_close', return_value=True):
        editor.new_card()
    assert author.frames == [] and editor.canvas.onion_image is None
    assert not author.timer.isActive()


def test_framed_gif_contains_both_artworks_and_selected_timing(editor, tmp_path):
    author = editor.animation_author; author.start()
    draw_pixel(editor, 10, 10, 6)
    author.duration.setValue(700)
    target = tmp_path/'dance.gif'
    original = copy.deepcopy(editor.card.pixels)
    author.onion.setChecked(True)
    with patch('animation_editor.QFileDialog.getSaveFileName', return_value=(str(target), '')):
        author.export.click()
    assert editor.card.pixels == original
    with Image.open(target) as gif:
        assert gif.n_frames == 2 and gif.size == (320, 400)
        assert gif.info['loop'] == 0
        assert gif.info['duration'] == 700
        first = gif.convert('RGB'); gif.seek(1)
        assert gif.info['duration'] == 700
        second = gif.convert('RGB')
        assert first.tobytes() != second.tobytes()
        # Both include the same styled border/title around the animated art.
        assert first.crop((0, 0, 320, 40)).tobytes() == second.crop((0, 0, 320, 40)).tobytes()
        assert len(first.crop((0, 0, 320, 40)).getcolors(128000)) > 1
    author.play.click(); assert not author.timer.isActive()
    author.play.click(); assert author.timer.isActive()


def test_failed_second_file_replace_restores_first(tmp_path):
    paths = [tmp_path/'first.json', tmp_path/'second.json']
    paths[0].write_text('original first'); paths[1].write_text('original second')
    import animation_editor
    real_replace = animation_editor.os.replace
    def fail_second(source, target):
        if target == paths[1]: raise OSError('disk failure')
        real_replace(source, target)
    with patch('animation_editor.os.replace', side_effect=fail_second):
        with pytest.raises(OSError, match='disk failure'):
            write_pair(paths, [{'pixels': [1]}, {'pixels': [2]}])
    assert [path.read_text() for path in paths] == ['original first', 'original second']
    assert sorted(tmp_path.iterdir()) == sorted(paths)


def test_external_pair_reopens_and_wrong_artwork_stays_single(editor, tmp_path):
    author = editor.animation_author; author.start(); draw_pixel(editor)
    outside = tmp_path/'exports'/'external.json'
    with patch('animation_editor.QFileDialog.getSaveFileName', return_value=(str(outside), '')):
        assert editor.save_card()
    editor._load_card_path(outside)
    assert len(author.frames) == 2
    second_path = author.frames[1]['path']
    first = json.loads(outside.read_text())
    first['pixels'][0] = 1-first['pixels'][0]
    first['base_pixels'] = first['pixels']
    outside.write_text(json.dumps(first))
    editor._load_card_path(second_path)
    assert not author.frames
    assert editor.card.animation  # Preserve its reference until the first frame is found.


def test_identity_updates_both_frames_and_save_failure_keeps_drafts(editor, tmp_path):
    author = editor.animation_author; author.start(); draw_pixel(editor)
    first_path, second_path = save_pair(editor, tmp_path)
    originals = [first_path.read_bytes(), second_path.read_bytes()]
    editor.call.setText('W1AW'); editor.grid.setText('FN31')
    author.switch(0)
    signature = editor.editor_signature()
    with patch('animation_editor.write_pair', side_effect=OSError('disk full')), \
         patch('app.QMessageBox.warning') as warning:
        assert not editor.save_card()
    warning.assert_called_once()
    assert editor.editor_signature() == signature
    assert [first_path.read_bytes(), second_path.read_bytes()] == originals
    assert editor.save_card()
    documents = [json.loads(path.read_text()) for path in (first_path, second_path)]
    assert all(document['callsign'] == 'W1AW' and document['grid'] == 'FN31' for document in documents)
    assert documents[1]['animation']['first_digest'] == artwork_digest(documents[0])


def test_saved_stamps_remain_editable_in_both_frames(editor, tmp_path):
    image = QImage(4, 4, QImage.Format.Format_ARGB32); image.fill(QColor('red'))
    editor.canvas.add_stamp(image)
    author = editor.animation_author; author.start()
    QTest.keyClick(editor.canvas, Qt.Key.Key_Right)
    paths = save_pair(editor, tmp_path)
    editor._load_card_path(paths[0])
    assert editor.canvas.stamp_layers[0]['x'] == 0
    author.switch(1)
    assert editor.canvas.stamp_layers[0]['x'] == 1
    # After reopening, a normal click and arrow nudge edit the saved layer.
    editor.set_editor_tool('move')
    QTest.mouseClick(editor.canvas, Qt.MouseButton.LeftButton, pos=editor.canvas.pixel_position(2, 2))
    QTest.keyClick(editor.canvas, Qt.Key.Key_Right)
    assert editor.canvas.stamp_layers[0]['x'] == 2
    author.switch(0); assert editor.canvas.stamp_layers[0]['x'] == 0


def test_preview_and_editor_sidebar_fit_default_window(editor):
    editor.animation_author.start()
    QApplication.processEvents()
    from PySide6.QtWidgets import QScrollArea
    sidebar = editor.editor_dialog.findChild(QScrollArea)
    assert sidebar.widget().width() <= sidebar.viewport().width()

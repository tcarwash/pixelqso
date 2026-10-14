import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QRect, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
import cardmodem
from app import CardCanvas, MainWindow, CanvasListenDialog, ListenEncodeWorker
from card_backends import get_backend

class CanvasToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_convert_stamp_preserves_pixels_and_undo(self):
        canvas = CardCanvas(cardmodem.example_card())
        image = QImage(3, 3, QImage.Format.Format_ARGB32); image.fill(QColor('red'))
        canvas.add_stamp(image)
        before = list(canvas.card.pixels)
        self.assertTrue(canvas.convert_stamp_to_pixels())
        self.assertEqual(canvas.card.pixels, before)
        self.assertFalse(canvas.stamp_layers)
        canvas.base_pixels[0] = 2; canvas._render_layers()
        self.assertEqual(canvas.card.pixels[0], 2)
        canvas.undo()
        self.assertEqual(canvas.card.pixels, before)
        self.assertEqual(len(canvas.stamp_layers), 1)

    def test_lift_box_preserves_image_and_can_move(self):
        canvas = CardCanvas(cardmodem.example_card())
        canvas.selection_rect = QRect(0, 0, 3, 2)
        before = list(canvas.card.pixels)
        selected = [before[y*32+x] for y in range(2) for x in range(3)]
        index = canvas.lift_selection()
        self.assertEqual(canvas.card.pixels, before)
        self.assertEqual(canvas.stamp_layers[index]['pixels'], selected)
        self.assertEqual(canvas.tool, 'move')
        canvas.stamp_layers[index]['x'] = 10; canvas._render_layers()
        self.assertEqual(canvas.card.pixels[10:13], selected[:3])
        canvas.undo(); self.assertEqual(canvas.card.pixels, before)

    def test_box_gesture_and_corner_resize(self):
        canvas = CardCanvas(cardmodem.example_card()); canvas.resize(320, 320); canvas.show()
        canvas.tool = 'select'; before = list(canvas.card.pixels)
        def drag(start, end):
            QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
            event = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(end), QPointF(canvas.mapToGlobal(end)),
                                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(canvas, event)
            QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
            self.qt.processEvents()
        try:
            drag(QPoint(25, 25), QPoint(55, 55))
            self.assertEqual(canvas.selection_rect, QRect(2, 2, 4, 4))
            self.assertEqual(canvas.card.pixels, before)
            drag(canvas.selection_handle.center(), QPoint(105, 105))
            self.assertEqual(len(canvas.stamp_layers), 1)
            self.assertGreater(canvas.stamp_layers[0]['width'], 4)
            self.assertEqual(canvas.tool, 'move')
            canvas.undo(); self.assertEqual(canvas.card.pixels, before)
        finally:
            canvas.close()

    def test_selected_stamp_controls_stay_visible_and_inside_canvas(self):
        canvas = CardCanvas(cardmodem.example_card()); canvas.resize(320, 320)
        image = QImage(1, 1, QImage.Format.Format_ARGB32); image.fill(QColor('red'))
        canvas.add_stamp(image); canvas.hover_layer = None; canvas._update_hover_remove_rect()
        self.assertEqual(canvas.hover_layer, canvas.selected_layer)
        self.assertTrue(canvas.rect().contains(canvas.hover_convert_rect))
        self.assertTrue(canvas.rect().contains(canvas.hover_remove_rect))

    def test_selection_saved_to_library_without_changing_canvas(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'PIXELQSO_DATA_DIR': directory}), patch('app.QTimer.singleShot'):
            window = MainWindow()
            try:
                before = list(window.card.pixels)
                window.canvas.selection_rect = QRect(2, 3, 4, 5)
                window.save_canvas_selection_stamp()
                paths = list((Path(directory)/'stamps').glob('stamp-*.png'))
                self.assertEqual(len(paths), 1)
                image = QImage(str(paths[0])); self.assertEqual((image.width(), image.height()), (4, 5))
                self.assertEqual(window.card.pixels, before)
                self.assertTrue(window.stamp_list.count())
                self.assertEqual(window.palette_layout.getItemPosition(7)[:2], (1, 3))
            finally:
                window.close()

    def test_listen_worker_encodes_current_card_and_cancel_ignores_result(self):
        worker = ListenEncodeWorker(cardmodem.example_card(), get_backend('experimental_8fsk_musical_25'), 7)
        results = []; worker.signals.finished.connect(results.append); worker.run()
        token, pcm, error = results[0]
        self.assertEqual(token, 7); self.assertIsNone(error); self.assertGreater(len(pcm), 48000*2)
        dialog = CanvasListenDialog(cardmodem.example_card)
        self.assertGreater(dialog.mode.count(), 0)
        self.assertGreaterEqual(dialog.mode.findData('experimental_8fsk_musical_25'), 0)
        dialog.show(); self.qt.processEvents()
        dialog.token = 7; dialog.stop_playback(); dialog.audio_ready(results[0])
        self.assertIsNone(dialog.sink); dialog.close()

import unittest

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import cardmodem
from app import CardCanvas


class CardCanvasStampRemovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_close_control_stays_visible_over_transparent_stamp_pixels(self):
        canvas = CardCanvas(cardmodem.example_card())
        canvas.resize(320, 320)
        image = QImage(8, 8, QImage.Format.Format_ARGB32)
        image.fill(QColor("red"))
        for x in range(5, 8):
            for y in range(2):
                image.setPixelColor(x, y, QColor(0, 0, 0, 0))
        canvas.add_stamp(image)

        def hover(point):
            event = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(point),
                                QPointF(canvas.mapToGlobal(point)),
                                Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier)
            canvas.mouseMoveEvent(event)

        hover(QPoint(20, 60))
        close_position = canvas.hover_remove_rect.center()
        x, y = canvas.card_position(close_position)
        self.assertIsNone(canvas._hit_layer(x, y))
        hover(close_position)
        self.assertEqual(canvas.hover_layer, 0)
        self.assertEqual(canvas.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=close_position)
        self.assertEqual(canvas.stamp_layers, [])
        canvas.undo()
        self.assertEqual(len(canvas.stamp_layers), 1)
        hover(QPoint(200, 200))
        self.assertIsNone(canvas.hover_layer)
        canvas.close()

    def test_hover_close_removes_only_placed_stamp_and_is_undoable(self):
        canvas = CardCanvas(cardmodem.example_card())
        canvas.resize(320, 320)
        canvas.show()
        image = QImage(8, 8, QImage.Format.Format_ARGB32)
        image.fill(QColor("red"))
        canvas.add_stamp(image)
        self.app.processEvents()
        self.assertEqual(len(canvas.stamp_layers), 1)

        # Hover and click inside the stamp but away from its corner control.
        hover = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(20, 60),
                            QPointF(canvas.mapToGlobal(QPoint(20, 60))),
                            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier)
        canvas.mouseMoveEvent(hover)
        self.app.processEvents()
        self.assertEqual(canvas.hover_layer, 0)
        self.assertFalse(canvas.hover_remove_rect.contains(QPoint(20, 60)))
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(20, 60))
        self.assertEqual(len(canvas.stamp_layers), 1)

        close_position = canvas.hover_remove_rect.center()
        hover_close = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(close_position),
                                  QPointF(canvas.mapToGlobal(close_position)),
                                  Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                  Qt.KeyboardModifier.NoModifier)
        canvas.mouseMoveEvent(hover_close)
        self.app.processEvents()
        self.assertTrue(canvas.hover_remove_rect.contains(close_position))
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=close_position)
        self.assertEqual(canvas.stamp_layers, [])
        self.assertTrue(canvas.undo_stack)
        canvas.undo()
        self.assertEqual(len(canvas.stamp_layers), 1)
        canvas.close()


if __name__ == "__main__":
    unittest.main()

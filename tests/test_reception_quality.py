from reception_quality import reception_quality


def test_raw_picture_can_be_entirely_heard_but_not_verified():
    card = dict(width=4, height=2, pixel_coverage=[True] * 8,
                verified_pixel_coverage=[False] * 8, exact=False)
    q = reception_quality(card)
    assert q.heard_percent == 100
    assert q.checked_percent == 0
    assert not q.whole_verified
    assert "PREVIEW" in q.caption
    assert "100% heard" in q.caption
    assert "0% CRC" in q.caption
    assert "may be incorrect" in q.detail


def test_partial_crc_progress_is_independent_of_raw_coverage():
    q = reception_quality(dict(width=4, height=2,
        pixel_coverage=[True] * 6 + [False] * 2,
        verified_pixel_coverage=[True] * 3 + [False] * 5))
    assert q.heard_percent == 75
    assert q.checked_percent == 38


def test_exact_flag_alone_is_not_integrity_proof():
    card = dict(width=2, height=2, exact=True)
    q = reception_quality(card)
    assert not q.whole_verified
    assert q.heard_percent is None
    assert q.checked_percent is None
    assert "NOT VERIFIED" in q.caption
    confirmed = reception_quality(card, verified_complete=True)
    assert confirmed.whole_verified
    assert confirmed.heard_percent == confirmed.checked_percent == 100


def test_masks_cannot_promote_unreported_pixels():
    q = reception_quality(dict(width=4, height=2,
        pixel_coverage=[True], verified_pixel_coverage=[True]))
    assert q.heard_percent == 12
    assert q.checked_percent == 12


def test_confidence_tint_is_a_display_copy_not_a_pixel_repair():
    from PySide6.QtGui import QColor, QImage
    from app import MainWindow

    original = QImage(4, 2, QImage.Format.Format_RGB32)
    original.fill(QColor(255, 0, 0))
    quality = reception_quality(dict(width=4, height=2,
        pixel_coverage=[True] * 6 + [False] * 2,
        verified_pixel_coverage=[True] * 3 + [False] * 5))
    assert MainWindow._image_with_reception_confidence(original, quality, False) is original
    shown = MainWindow._image_with_reception_confidence(original, quality, True)
    assert shown is not original
    assert shown.pixelColor(0, 0) == original.pixelColor(0, 0)  # CRC-checked
    assert shown.pixelColor(3, 0) != original.pixelColor(3, 0)  # heard, unchecked
    assert shown.pixelColor(2, 1) == original.pixelColor(2, 1)  # not received
    assert original.pixelColor(3, 0) == QColor(255, 0, 0)  # decoder data unchanged
    confirmed = reception_quality(dict(width=4, height=2), verified_complete=True)
    assert MainWindow._image_with_reception_confidence(original, confirmed, True) is original

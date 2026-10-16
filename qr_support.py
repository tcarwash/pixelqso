"""QR card creation and verified-image scanning helpers."""

from __future__ import annotations

import cardmodem


def make_qr_card(text: str, callsign: str, grid: str, card_id: int,
                 width: int = 32, height: int = 32) -> cardmodem.Card:
    """Create a centered QR code on a monochrome PixelQSO card."""
    value = str(text).strip()
    if not value:
        raise ValueError("Enter text or a URL for the QR code")
    import qrcode

    code = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_L,
                         box_size=1, border=4)
    code.add_data(value)
    code.make(fit=True)
    matrix = code.get_matrix()
    code_width, code_height = len(matrix[0]), len(matrix)
    if code_width > width or code_height > height:
        raise ValueError(
            f"This QR code needs at least {code_width}×{code_height} pixels; "
            f"the current card is {width}×{height}. Try a shorter value or a larger card.")
    pixels = [1] * (width * height)
    left, top = (width - code_width) // 2, (height - code_height) // 2
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                pixels[(top + y) * width + left + x] = 0
    return cardmodem.Card(callsign, grid, list(cardmodem.AVATAR_PALETTES[3]), pixels,
                          card_id, width, height)


def read_qr_image(image) -> str | None:
    """Read one QR code from a Qt image; return decoded text or ``None``."""
    import zxingcpp
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    if image is None or image.isNull():
        return None
    scaled = image.convertToFormat(QImage.Format.Format_Grayscale8).scaled(
        image.width() * 8, image.height() * 8,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation)
    result = zxingcpp.read_barcode(
        scaled, formats=zxingcpp.BarcodeFormat.QRCode,
        try_rotate=False, try_downscale=False, is_pure=True)
    return result.text if result is not None else None

"""Shared visual tokens for the Pixel QSO desktop interface.

The dark palette is the only supported theme while the UI is being migrated to
semantic tokens. Add another appearance only after the full surface is audited.
"""

COLORS = {
    "window": "#11161a",
    "surface": "#192126",
    "surface_raised": "#25333b",
    "surface_hover": "#344a54",
    "surface_item_hover": "#24343d",
    "surface_selected": "#30474f",
    "surface_tab": "#1b252b",
    "surface_tab_selected": "#304149",
    "surface_scrollbar": "#141b20",
    "surface_scrollbar_handle": "#40545c",
    "surface_header": "#202d25",
    "surface_disabled": "#1b231e",
    "surface_primary_disabled": "#26352e",
    "text": "#e8eee9",
    "text_bright": "#eff7f1",
    "text_primary": "#142017",
    "text_muted": "#9eada3",
    "text_soft": "#b8d4c0",
    "text_header": "#c5d5ca",
    "text_disabled": "#90a097",
    "text_primary_disabled": "#90a097",
    "border": "#2e3d45",
    "border_control": "#35464e",
    "border_button": "#3c5747",
    "border_selected": "#729b96",
    "border_primary_disabled": "#3b5144",
    "accent": "#a0dfc0",
    "accent_hover": "#b2df91",
    "accent_selection": "#52775d",
    "accent_checked": "#36564e",
    "accent_checked_border": "#9ed7bd",
    "accent_checked_text": "#eefff6",
    "focus_ring": "#a0dfc0",
    "focus_ring_primary": "#142017",
    "positive": "#8bd8ae",
    "warning": "#e5b36e",
    "error": "#e4867b",
    "waterfall_background": "#0b1115",
    "waterfall_text": "#d0ddd9",
    "waterfall_text_secondary": "#91a4a5",
    "waterfall_grid": "#96b4b6",
    "stamp_canvas_background": "#202820",
    "stamp_canvas_checker_light": "#364136",
    "stamp_canvas_checker_dark": "#293329",
    "stamp_canvas_grid": "#6e826e",
    "card_frame": "#1e292f",
    "card_frame_border": "#688570",
    "card_stack": "#28353c",
    "card_stack_border": "#4e6958",
    "card_art_background": "#101b16",
    "card_title": "#d9efc6",
    "card_subtitle": "#9fafaa",
    "editor_grid": "#5a5a5a",
    "editor_selection": "#ffdc3c",
    "stamp_remove_border": "#6e0000",
    "stamp_remove_background": "#dc232d",
    "stamp_control_glyph": "#ffffff",
    "stamp_resize_border": "#0c1619",
    "stamp_resize_background": "#96d2c6",
    "stamp_resize_glyph": "#112327",
}

SPACING = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}
RADIUS = {"control": 7, "button": 8, "panel": 10}
TYPE_SIZE = {"body": 14, "section": 16, "title": 24}


def status_label_style(tone, *, bold=True, letter_spaced=True):
    """Return a consistent semantic style for a text status indicator."""
    color_token = {"muted": "text_muted", "positive": "positive",
                   "warning": "warning", "error": "error"}.get(tone)
    if color_token is None:
        raise ValueError(f"Unknown status tone: {tone}")
    weight = ";font-weight:700" if bold else ""
    tracking = ";letter-spacing:1px" if letter_spaced else ""
    return f"color:{COLORS[color_token]}{weight}{tracking}"


def semantic_text_style(tone="muted", *, padding="0"):
    """Return an inline style for short explanatory or validation text."""
    color_token = {"muted": "text_muted", "soft": "text_soft",
                   "positive": "positive", "warning": "warning",
                   "error": "error"}.get(tone)
    if color_token is None:
        raise ValueError(f"Unknown text tone: {tone}")
    return f"color:{COLORS[color_token]};padding:{padding}"


def palette_swatch_style(fill, *, selected=False):
    """Return a palette-specific swatch style with visible keyboard focus."""
    border = COLORS["border_selected"] if selected else COLORS["border_control"]
    return (
        f"QPushButton {{ background:{fill}; border:2px solid {border}; "
        f"border-radius:{RADIUS['control']}px; padding:0; }} "
        f"QPushButton:focus {{ border:3px solid {COLORS['focus_ring']}; }}")


def build_app_stylesheet(text_scale=1.0):
    c = COLORS
    r = RADIUS
    scale = max(0.8, min(1.5, float(text_scale)))
    type_size = {name: max(1, round(size * scale))
                 for name, size in TYPE_SIZE.items()}
    empty_title_size = max(1, round(22 * scale))
    progress_size = max(1, round(10 * scale))
    return f"""
        QMainWindow, QWidget {{ background: {c['window']}; color: {c['text']}; }}
        QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: {r['panel']}px; top: -1px; }}
        QTabBar::tab {{ background: {c['surface_tab']}; color: {c['text_muted']}; padding: 10px 18px; margin-right: 4px; border-radius: {r['button']}px {r['button']}px 0 0; }}
        QTabBar::tab:selected {{ background: {c['surface_tab_selected']}; color: {c['text_bright']}; }}
        QGroupBox {{ border: 1px solid {c['border']}; border-radius: {r['panel']}px; margin-top: 12px; padding: 12px; font-weight: 600; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {c['text_soft']}; }}
        QLabel#pageTitle {{ font-size: {type_size['title']}px; font-weight: 700; color: {c['text_soft']}; }}
        QLabel#sectionTitle {{ font-size: {type_size['section']}px; font-weight: 700; color: {c['text']}; }}
        QLabel#gettingStartedStep {{ font-weight: 700; color: {c['text_soft']}; }}
        QLabel#emptyStateTitle {{ font-size: {empty_title_size}px; font-weight: 700; color: {c['text']}; }}
        QLabel#supportingText {{ font-size: {type_size['body']}px; color: {c['text_muted']}; }}
        QLabel#stationIdentity {{ font-weight: 700; color: {c['text']}; padding: 0 8px; }}
        QLabel#testModeBanner {{ background: {c['warning']}; color: {c['text_primary']}; padding: 6px 10px; border-radius: {r['button']}px; font-weight: 700; }}
        QLabel#receiverEmpty, QLabel#receiveView, QLabel#receiveWaterfallUnavailable {{ background: {c['surface_scrollbar']}; color: {c['text_muted']}; border: 1px solid {c['border']}; border-radius: {r['panel']}px; padding: 10px; }}
        QLabel#receiverEmpty {{ padding: 24px; font-size: {type_size['section']}px; }}
        QLabel#receiveStatus {{ color: {c['text_soft']}; padding: 2px; }}
        QLabel#quickDrawStatus {{ color: {c['positive']}; padding: 2px; }}
        QListWidget#sessionWall {{ background: {c['surface_scrollbar']}; border: 1px solid {c['border']}; border-radius: {r['panel']}px; padding: 4px; }}
        QListWidget#sessionWall::item {{ padding: 0; margin: 0; }}
        QProgressBar#receiveProgress {{ border: 1px solid {c['border_control']}; border-radius: 5px; background: {c['surface_scrollbar']}; color: {c['text']}; text-align: center; font-size: {progress_size}px; }}
        QProgressBar#receiveProgress::chunk {{ background: {c['positive']}; border-radius: 4px; }}
        QLineEdit, QComboBox, QSpinBox, QTextEdit, QTableView, QListWidget {{ background: {c['surface']}; border: 1px solid {c['border_control']}; border-radius: {r['control']}px; padding: 6px; selection-background-color: {c['accent_selection']}; }}
        QPushButton {{ background: {c['surface_raised']}; border: 1px solid {c['border_button']}; border-radius: {r['button']}px; padding: 8px 12px; }}
        QPushButton#stageSelector {{ text-align: left; padding: 6px 9px; }}
        QPushButton#stageSelector:checked {{ background: {c['accent_checked']}; border-color: {c['accent_checked_border']}; color: {c['accent_checked_text']}; }}
        QPushButton:hover {{ background: {c['surface_hover']}; }}
        QPushButton:checked {{ background: {c['accent_checked']}; border: 1px solid {c['accent_checked_border']}; color: {c['accent_checked_text']}; }}
        QListWidget::item {{ border-radius: {r['panel']}px; padding: 5px; }}
        QListWidget::item:hover {{ background: {c['surface_item_hover']}; }}
        QListWidget::item:selected {{ background: {c['surface_selected']}; border: 1px solid {c['border_selected']}; }}
        QScrollBar:vertical {{ background: {c['surface_scrollbar']}; width: 10px; }}
        QScrollBar::handle:vertical {{ background: {c['surface_scrollbar_handle']}; border-radius: 5px; min-height: 30px; }}
        QPushButton:disabled {{ color: {c['text_disabled']}; background: {c['surface_disabled']}; }}
        QPushButton#primaryAction {{ background: {c['accent']}; color: {c['text_primary']}; font-weight: 700; border: 0; padding: 10px 16px; }}
        QPushButton#primaryAction:hover {{ background: {c['accent_hover']}; }}
        QPushButton#primaryAction:disabled {{ background: {c['surface_primary_disabled']}; color: {c['text_primary_disabled']}; border: 1px solid {c['border_primary_disabled']}; }}
        QPushButton#stopTxAction {{ background: {c['error']}; color: {c['text_primary']}; border: 1px solid {c['error']}; font-weight: 700; }}
        QPushButton#stopTxAction:hover {{ background: #ee6d68; border-color: #ffaaa3; }}
        QHeaderView::section {{ background: {c['surface_header']}; color: {c['text_header']}; border: 0; padding: 7px; }}
        QPushButton:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
        QTextEdit:focus, QTableView:focus, QListWidget:focus,
        QCheckBox:focus, QRadioButton:focus, QTabBar::tab:focus {{
            border: 2px solid {c['focus_ring']};
        }}
        QPushButton#primaryAction:focus {{ border: 2px solid {c['focus_ring_primary']}; }}
    """

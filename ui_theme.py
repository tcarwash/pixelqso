"""Shared visual tokens for the Pixel QSO desktop interface.

The dark palette is the only supported theme while the UI is being migrated to
semantic tokens. Add another appearance only after the full surface is audited.
"""

COLORS = {
    "window": "#101118",
    "surface": "#242430",
    "surface_raised": "#292a37",
    "surface_hover": "#373447",
    "surface_item_hover": "#302d40",
    "surface_selected": "#353d2d",
    "surface_tab": "#191a24",
    "surface_tab_selected": "#283024",
    "surface_scrollbar": "#151522",
    "surface_scrollbar_handle": "#494458",
    "surface_header": "#252532",
    "surface_disabled": "#20212b",
    "surface_primary_disabled": "#30372a",
    "text": "#f5f1e9",
    "text_bright": "#f5f1e9",
    "text_primary": "#20251c",
    "text_muted": "#aaa8b9",
    "text_soft": "#d4c8ee",
    "text_header": "#e5e1ef",
    "text_disabled": "#92909f",
    "text_primary_disabled": "#a0aa96",
    "border": "#383744",
    "border_control": "#454252",
    "border_button": "#41414f",
    "border_selected": "#c5ff88",
    "border_primary_disabled": "#454d3d",
    "accent": "#c5ff88",
    "accent_hover": "#d5ffa9",
    "accent_selection": "#4a405f",
    "accent_checked": "#353d2d",
    "accent_checked_border": "#c5ff88",
    "accent_checked_text": "#c5ff88",
    "focus_ring": "#c6ff8a",
    "focus_ring_primary": "#252039",
    "positive": "#8bd8ae",
    "warning": "#e5b36e",
    "error": "#e4867b",
    "waterfall_background": "#101118",
    "waterfall_text": "#e5e1ef",
    "waterfall_text_secondary": "#aaa8b9",
    "waterfall_grid": "#827a99",
    "stamp_canvas_background": "#20202c",
    "stamp_canvas_checker_light": "#363344",
    "stamp_canvas_checker_dark": "#292735",
    "stamp_canvas_grid": "#736984",
    "card_frame": "#bda5f8",
    "card_frame_border": "#cbb6ff",
    "card_stack": "#62517f",
    "card_stack_border": "#947bbd",
    "card_art_background": "#151522",
    "card_title": "#252039",
    "card_subtitle": "#3e3455",
    "editor_grid": "#6a637b",
    "editor_selection": "#ffdc3c",
    "stamp_remove_border": "#6e0000",
    "stamp_remove_background": "#dc232d",
    "stamp_control_glyph": "#ffffff",
    "stamp_resize_border": "#0c1619",
    "stamp_resize_background": "#96d2c6",
    "stamp_resize_glyph": "#112327",
}

SPACING = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}
RADIUS = {"control": 10, "button": 14, "panel": 16}
TYPE_SIZE = {"body": 14, "section": 16, "title": 30}


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
        QSplitter::handle {{ background: {c['border']}; border: 3px solid {c['window']}; border-radius: 4px; }}
        QSplitter::handle:hover {{ background: {c['accent']}; }}
        QFrame#navigationRail {{ background: {c['surface_tab']}; border-right: 1px solid {c['border']}; }}
        QFrame#navigationRail QLabel {{ background: transparent; }}
        QLabel#brandName {{ color: {c['text_bright']}; font-size: 22px; font-weight: 800; }}
        QLabel#eyebrow {{ color: {c['text_muted']}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
        QLabel#railCaption {{ color: {c['text_muted']}; font-size: 11px; padding: 12px 0; }}
        QLabel#workspaceTitle {{ color: {c['text_bright']}; font-size: {type_size['title']}px; font-weight: 750; }}
        QPushButton#navigationItem {{ text-align: left; border: 0; background: transparent; padding: 12px; font-weight: 600; }}
        QPushButton#navigationItem:hover {{ background: {c['surface_hover']}; }}
        QPushButton#navigationItem:checked {{ background: {c['surface_tab_selected']}; color: {c['accent']}; }}
        QPushButton#navigationItem:focus {{ border: 2px solid {c['focus_ring']}; }}
        QPushButton#studioShortcut {{ color: {c['text_soft']}; text-align: left; padding: 12px 6px; background: transparent; border: 0; }}
        QTabWidget#workspacePages::pane {{ border: 0; }}
        QWidget#senderWorkspace {{ border: 1px solid {c['border']}; border-radius: 18px; }}
        QListWidget#collectionGallery {{ background: {c['window']}; border: 0; padding: 4px; }}
        QPushButton#stageNumber {{ padding: 8px 0; color: {c['text_soft']}; background: transparent; border: 0; }}
        QPushButton#stageNumber:focus {{ border: 2px solid {c['focus_ring']}; }}
        QPushButton#activityDisclosure {{ text-align: left; color: {c['text_muted']}; background: transparent; border: 0; }}
        QMainWindow, QWidget {{ background: {c['window']}; color: {c['text']}; }}
        QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: {r['panel']}px; top: -1px; }}
        QTabBar::tab {{ background: {c['surface_tab']}; color: {c['text_muted']}; padding: 12px 22px; margin-right: 6px; border-radius: {r['button']}px; }}
        QTabBar::tab:selected {{ background: {c['surface_tab_selected']}; color: {c['accent']}; }}
        QGroupBox {{ border: 1px solid {c['border']}; border-radius: {r['panel']}px; margin-top: 12px; padding: 12px; font-weight: 600; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {c['text_soft']}; }}
        QLabel#pageTitle {{ font-size: {type_size['title']}px; font-weight: 700; color: {c['text_bright']}; }}
        QLabel#sectionTitle {{ font-size: {type_size['section']}px; font-weight: 700; color: {c['text']}; }}
        QLabel#gettingStartedStep {{ font-weight: 700; color: {c['text_soft']}; }}
        QLabel#emptyStateTitle {{ font-size: {empty_title_size}px; font-weight: 700; color: {c['text']}; }}
        QLabel#supportingText {{ font-size: {type_size['body']}px; color: {c['text_muted']}; }}
        QLabel#stationIdentity {{ font-weight: 700; color: {c['text']}; padding: 0 8px; }}
        QLabel#testModeBanner {{ background: {c['warning']}; color: {c['text_primary']}; padding: 6px 10px; border-radius: {r['button']}px; font-weight: 700; }}
        QLabel#receiverEmpty, QLabel#receiveView, QLabel#receiveWaterfallUnavailable {{ background: {c['surface_scrollbar']}; color: {c['text_muted']}; border: 1px solid {c['border']}; border-radius: {r['panel']}px; padding: 10px; }}
        QLabel#receiveView {{ padding: 0px; }}
        QLabel#receiverEmpty {{ padding: 24px; font-size: {type_size['section']}px; }}
        QLabel#receiveStatus {{ color: {c['text_soft']}; padding: 2px; }}
        QLabel#quickDrawStatus {{ color: {c['positive']}; padding: 2px; }}
        QListWidget#sessionWall {{ background: {c['surface_scrollbar']}; border: 1px solid {c['border']}; border-radius: {r['panel']}px; padding: 4px; }}
        QListWidget#sessionWall::item {{ padding: 0; margin: 0; }}
        QProgressBar#receiveProgress {{ border: 1px solid {c['border_control']}; border-radius: 5px; background: {c['surface_scrollbar']}; color: {c['text']}; text-align: center; font-size: {progress_size}px; }}
        QProgressBar#receiveProgress::chunk {{ background: {c['positive']}; border-radius: 4px; }}
        QLineEdit, QComboBox, QSpinBox, QTextEdit, QTableView, QListWidget {{ background: {c['surface']}; border: 1px solid {c['border_control']}; border-radius: {r['control']}px; padding: 6px; selection-background-color: {c['accent_selection']}; }}
        QPushButton {{ background: {c['surface_raised']}; border: 1px solid {c['border_button']}; border-radius: {r['button']}px; padding: 9px 14px; }}
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
        QPushButton#primaryAction {{ background: {c['accent']}; color: {c['text_primary']}; font-weight: 700; border: 0; padding: 12px 20px; }}
        QPushButton#primaryAction:hover {{ background: {c['accent_hover']}; }}
        QPushButton#primaryAction:disabled {{ background: {c['surface_primary_disabled']}; color: {c['text_primary_disabled']}; border: 1px solid {c['border_primary_disabled']}; }}
        QPushButton#stopTxAction {{ background: {c['error']}; color: {c['text_primary']}; border: 1px solid {c['error']}; font-weight: 700; }}
        QPushButton#stopTxAction:hover {{ background: #ee6d68; border-color: #ffaaa3; }}
        QMenuBar, QMenu {{ background: {c['surface_tab']}; color: {c['text']}; }}
        QMenuBar::item:selected, QMenu::item:selected {{ background: {c['surface_selected']}; color: {c['accent']}; }}
        QToolTip {{ background: {c['surface_raised']}; color: {c['text']}; border: 1px solid {c['border_control']}; padding: 6px; }}
        QHeaderView::section {{ background: {c['surface_header']}; color: {c['text_header']}; border: 0; padding: 7px; }}
        QPushButton:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
        QTextEdit:focus, QTableView:focus, QListWidget:focus,
        QCheckBox:focus, QRadioButton:focus, QTabBar::tab:focus {{
            border: 2px solid {c['focus_ring']};
        }}
        QPushButton#primaryAction:focus {{ border: 2px solid {c['focus_ring_primary']}; }}
    """

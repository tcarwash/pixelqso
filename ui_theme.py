"""Shared visual tokens for the Pixel QSO desktop interface.

The dark palette is the only supported theme while the UI is being migrated to
semantic tokens. Add another appearance only after the full surface is audited.
"""

COLORS = {
    "window": "#0e0f16",
    "surface": "#20212e",
    "surface_raised": "#2a2b3a",
    "surface_hover": "#34324a",
    "surface_item_hover": "#2c2a3d",
    "surface_selected": "#2e3a28",
    "surface_tab": "#16171f",
    "surface_tab_selected": "#2c3526",
    "surface_scrollbar": "#14141d",
    "surface_scrollbar_handle": "#4c485e",
    "surface_header": "#23242f",
    "surface_disabled": "#1c1d27",
    "surface_primary_disabled": "#2b3127",
    "surface_sunken": "#12131c",
    "text": "#f5f1e9",
    "text_bright": "#faf7f0",
    "text_primary": "#1e241a",
    "text_muted": "#b0adbf",
    "text_soft": "#d8cdf2",
    "text_header": "#e8e4f2",
    "text_disabled": "#8b8998",
    "text_primary_disabled": "#9aa393",
    "border": "#2e2d3a",
    "border_subtle": "#26252f",
    "border_control": "#3f3d4e",
    "border_button": "#3a3947",
    "border_selected": "#c5ff88",
    "border_primary_disabled": "#3d4436",
    "accent": "#c5ff88",
    "accent_hover": "#d8ffab",
    "accent_pressed": "#a9e56f",
    "accent_dim": "#2c3526",
    "accent_selection": "#4a405f",
    "accent_checked": "#2e3a28",
    "accent_checked_border": "#c5ff88",
    "accent_checked_text": "#cdf59a",
    "focus_ring": "#c6ff8a",
    "focus_ring_primary": "#1e241a",
    "positive": "#8bd8ae",
    "warning": "#e5b36e",
    "error": "#e4867b",
    "waterfall_background": "#0e0f16",
    "waterfall_text": "#e8e4f2",
    "waterfall_text_secondary": "#b0adbf",
    "waterfall_grid": "#6f6884",
    "stamp_canvas_background": "#1d1e29",
    "stamp_canvas_checker_light": "#333142",
    "stamp_canvas_checker_dark": "#262534",
    "stamp_canvas_grid": "#6f6884",
    "card_frame": "#bda5f8",
    "card_frame_border": "#d3c2ff",
    "card_frame_shadow": "#0a0b11",
    "card_stack": "#574a73",
    "card_stack_border": "#8a72b8",
    "card_art_background": "#12131c",
    "card_art_border": "#3a3947",
    "card_title": "#241f38",
    "card_subtitle": "#453a63",
    "editor_grid": "#5f5875",
    "editor_selection": "#ffdc3c",
    "stamp_remove_border": "#6e0000",
    "stamp_remove_background": "#dc232d",
    "stamp_control_glyph": "#ffffff",
    "stamp_resize_border": "#0c1619",
    "stamp_resize_background": "#96d2c6",
    "stamp_resize_glyph": "#112327",
}

SPACING = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}
RADIUS = {"control": 8, "button": 10, "panel": 14, "card": 16, "pill": 999}
TYPE_SIZE = {"body": 14, "section": 16, "title": 26}


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
    width = 3 if selected else 2
    return (
        f"QPushButton {{ background:{fill}; border:{width}px solid {border}; "
        f"border-radius:{RADIUS['control']}px; padding:0; }} "
        f"QPushButton:hover {{ border:{width}px solid {COLORS['text_bright']}; }} "
        f"QPushButton:focus {{ border:3px solid {COLORS['focus_ring']}; }} "
        f"QPushButton:pressed {{ border:3px solid {COLORS['accent']}; }}")


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
        QWidget {{ selection-background-color: {c['accent_selection']};
                   selection-color: {c['text_bright']}; }}
        QSplitter::handle {{ background: transparent; border: 0; }}
        QSplitter::handle:horizontal {{ width: 12px; }}
        QSplitter::handle:vertical {{ height: 12px; }}
        QSplitter::handle:hover {{ background: {c['accent_dim']}; }}
        QSplitter::handle:pressed {{ background: {c['surface_tab_selected']}; }}
        QFrame#navigationRail {{ background: {c['surface_tab']};
                                 border-right: 1px solid {c['border_subtle']}; }}
        QFrame#navigationRail QLabel {{ background: transparent; }}
        QLabel#brandName {{ color: {c['text_bright']}; font-size: 21px; font-weight: 800;
                            letter-spacing: -0.5px; }}
        QLabel#eyebrow {{ color: {c['text_muted']}; font-size: 11px; font-weight: 700;
                          letter-spacing: 1.2px; }}
        QLabel#railCaption {{ color: {c['text_muted']}; font-size: 12px; padding: 10px 2px; }}
        QLabel#workspaceTitle {{ color: {c['text_bright']}; font-size: {type_size['title']}px;
                                 font-weight: 800; letter-spacing: -0.5px; }}
        QFrame#shellDivider {{ background: {c['border_subtle']}; border: 0;
                               min-height: 1px; max-height: 1px; }}
        QPushButton#navigationItem {{ text-align: left; border: 1px solid transparent;
                                      background: transparent; padding: 10px 12px; font-weight: 600;
                                      border-radius: {r['control']}px; }}
        QPushButton#navigationItem:hover {{ background: {c['surface_hover']}; color: {c['text_bright']}; }}
        QPushButton#navigationItem:checked {{ background: {c['surface_tab_selected']};
                                              color: {c['accent']};
                                              border-color: {c['border_subtle']}; }}
        QPushButton#navigationItem:pressed {{ background: {c['surface_item_hover']}; }}
        QPushButton#navigationItem:focus {{ border: 1px solid {c['focus_ring']}; }}
        QPushButton#studioShortcut {{ color: {c['accent']}; text-align: left; padding: 10px 12px;
                                      background: {c['accent_dim']};
                                      border: 1px solid {c['border_subtle']};
                                      border-radius: {r['control']}px; font-weight: 700; }}
        QPushButton#studioShortcut:hover {{ background: {c['surface_tab_selected']};
                                            border-color: {c['accent']}; }}
        QPushButton#studioShortcut:pressed {{ background: {c['surface_selected']}; }}
        QTabWidget#workspacePages::pane {{ border: 0; }}
        QWidget#senderWorkspace {{ background: {c['surface_tab']};
                                   border: 1px solid {c['border_subtle']};
                                   border-radius: {r['card']}px; }}
        QListWidget#collectionGallery {{ background: transparent; border: 0; padding: 4px; }}
        QPushButton#stageNumber {{ padding: 8px 0; color: {c['text_soft']}; background: transparent;
                                   border: 0; font-weight: 700; }}
        QPushButton#stageNumber:hover {{ color: {c['accent']}; }}
        QPushButton#stageNumber:focus {{ border: 2px solid {c['focus_ring']}; }}
        QPushButton#activityDisclosure {{ text-align: left; color: {c['text_muted']};
                                          background: transparent; border: 0; padding: 6px 2px; }}
        QPushButton#activityDisclosure:hover {{ color: {c['text_soft']}; }}
        QTabWidget::pane {{ border: 1px solid {c['border_subtle']};
                            border-radius: {r['panel']}px; top: -1px; }}
        QTabBar::tab {{ background: transparent; color: {c['text_muted']}; padding: 8px 16px;
                        margin-right: 4px; border-radius: {r['control']}px; font-weight: 600; }}
        QTabBar::tab:hover {{ background: {c['surface_hover']}; color: {c['text']}; }}
        QTabBar::tab:selected {{ background: {c['surface_tab_selected']}; color: {c['accent']}; }}
        QTabBar::tab:disabled {{ color: {c['text_disabled']}; }}
        QGroupBox {{ border: 1px solid {c['border_subtle']}; border-radius: {r['panel']}px;
                     margin-top: 12px; padding: 12px; font-weight: 600; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 8px;
                            color: {c['text_soft']}; }}
        QGroupBox#senderSection {{ border: 0; border-top: 1px solid {c['border_subtle']};
                                   border-radius: 0; margin-top: 8px; padding: 12px 4px 4px; }}
        QGroupBox#senderSection::title {{ subcontrol-origin: margin; left: 4px; padding: 0 4px;
                                          color: {c['text_muted']}; font-size: 12px;
                                          letter-spacing: 1px; }}
        QLabel#pageTitle {{ font-size: {type_size['title']}px; font-weight: 800; color: {c['text_bright']};
                            letter-spacing: -0.5px; }}
        QLabel#sectionTitle {{ font-size: {type_size['section']}px; font-weight: 700; color: {c['text']}; }}
        QLabel#gettingStartedStep {{ font-weight: 700; color: {c['text_soft']}; }}
        QLabel#emptyStateTitle {{ font-size: {empty_title_size}px; font-weight: 700; color: {c['text']}; }}
        QLabel#supportingText {{ font-size: {type_size['body']}px; color: {c['text_muted']}; }}
        QLabel#stationIdentity {{ font-weight: 700; color: {c['text']}; padding: 0 8px; }}
        QLabel#testModeBanner {{ background: {c['warning']}; color: {c['text_primary']}; padding: 6px 12px;
                                 border-radius: {r['button']}px; font-weight: 700; }}
        QLabel#receiverEmpty, QLabel#receiveView, QLabel#receiveWaterfallUnavailable {{
            background: {c['surface_sunken']}; color: {c['text_muted']};
            border: 1px solid {c['border_subtle']}; border-radius: {r['panel']}px; padding: 12px; }}
        QLabel#receiveView {{ padding: 0px; }}
        QLabel#receiverEmpty {{ padding: 28px; font-size: {type_size['section']}px; }}
        QLabel#receiveStatus {{ color: {c['text_soft']}; padding: 2px; }}
        QLabel#quickDrawStatus {{ color: {c['positive']}; padding: 2px; }}
        QListWidget#sessionWall {{ background: {c['surface_sunken']};
                                   border: 1px solid {c['border_subtle']};
                                   border-radius: {r['panel']}px; padding: 6px; }}
        QListWidget#sessionWall::item {{ padding: 0; margin: 0; border: 0; }}
        QProgressBar#receiveProgress {{ border: 1px solid {c['border_control']}; border-radius: 7px;
                                        background: {c['surface_sunken']}; color: {c['text']};
                                        text-align: center; font-size: {progress_size}px; }}
        QProgressBar#receiveProgress::chunk {{ background: {c['positive']}; border-radius: 6px; }}
        QProgressBar {{ border: 1px solid {c['border_control']}; border-radius: 7px;
                       background: {c['surface_sunken']}; color: {c['text']}; text-align: center; }}
        QProgressBar::chunk {{ background: {c['positive']}; border-radius: 6px; }}
        QLineEdit, QComboBox, QSpinBox, QTextEdit, QTableView, QListWidget {{
            background: {c['surface']}; border: 1px solid {c['border_control']};
            border-radius: {r['control']}px; padding: 9px 12px;
            selection-background-color: {c['accent_selection']}; }}
        QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QTextEdit:hover {{
            border-color: {c['text_muted']}; }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {{
            border-color: {c['accent']}; }}
        QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QTextEdit:disabled {{
            background: {c['surface_disabled']}; color: {c['text_disabled']};
            border-color: {c['border_subtle']}; }}
        QComboBox::drop-down {{ border: 0; width: 28px; }}
        QComboBox QAbstractItemView {{ background: {c['surface_raised']};
                                      border: 1px solid {c['border_control']};
                                      border-radius: {r['control']}px; padding: 6px;
                                      selection-background-color: {c['surface_selected']};
                                      selection-color: {c['accent']}; outline: 0; }}
        QComboBox QAbstractItemView::item {{ padding: 8px 10px;
                                             border-radius: {r['control']}px; border: 0; }}
        QPushButton {{ background: {c['surface_raised']}; border: 1px solid {c['border_button']};
                       border-radius: {r['button']}px; padding: 10px 16px; font-weight: 600; }}
        QPushButton#stageSelector {{ text-align: left; padding: 7px 10px; }}
        QPushButton#stageSelector:checked {{ background: {c['accent_checked']};
                                             border-color: {c['accent_checked_border']};
                                             color: {c['accent_checked_text']}; }}
        QPushButton:hover {{ background: {c['surface_hover']}; border-color: {c['border_control']}; }}
        QPushButton:pressed {{ background: {c['surface_item_hover']}; }}
        QPushButton:checked {{ background: {c['accent_checked']};
                               border: 1px solid {c['accent_checked_border']};
                               color: {c['accent_checked_text']}; }}
        QPushButton:disabled {{ color: {c['text_disabled']}; background: {c['surface_disabled']};
                                border-color: {c['border_subtle']}; }}
        QListWidget::item {{ border-radius: {r['panel']}px; padding: 6px; border: 1px solid transparent; }}
        QListWidget::item:hover {{ background: {c['surface_item_hover']}; }}
        QListWidget::item:selected {{ background: {c['surface_selected']};
                                      border: 1px solid {c['border_selected']}; }}
        QCheckBox, QRadioButton {{ spacing: 8px; padding: 4px 2px; }}
        QCheckBox::indicator, QRadioButton::indicator {{ width: 18px; height: 18px; }}
        QCheckBox::indicator {{ border: 1px solid {c['border_control']};
                                border-radius: 6px; background: {c['surface']}; }}
        QCheckBox::indicator:hover {{ border-color: {c['accent']}; }}
        QCheckBox::indicator:checked {{ background: {c['accent']};
                                        border-color: {c['accent']}; }}
        QCheckBox::indicator:disabled {{ background: {c['surface_disabled']};
                                         border-color: {c['border_subtle']}; }}
        QRadioButton::indicator {{ border: 1px solid {c['border_control']};
                                   border-radius: 9px; background: {c['surface']}; }}
        QRadioButton::indicator:hover {{ border-color: {c['accent']}; }}
        QRadioButton::indicator:checked {{ background: {c['accent']};
                                           border-color: {c['accent']}; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px 4px 0; }}
        QScrollBar::handle:vertical {{ background: {c['surface_scrollbar_handle']};
                                       border-radius: 4px; min-height: 36px; }}
        QScrollBar::handle:vertical:hover {{ background: {c['border_control']}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0 4px 2px 4px; }}
        QScrollBar::handle:horizontal {{ background: {c['surface_scrollbar_handle']};
                                         border-radius: 4px; min-width: 36px; }}
        QScrollBar::handle:horizontal:hover {{ background: {c['border_control']}; }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
        QPushButton#primaryAction {{ background: {c['accent']}; color: {c['text_primary']};
                                     font-weight: 800; border: 0; padding: 12px 22px;
                                     border-radius: {r['button']}px; }}
        QPushButton#primaryAction:hover {{ background: {c['accent_hover']}; }}
        QPushButton#primaryAction:pressed {{ background: {c['accent_pressed']}; }}
        QPushButton#primaryAction:disabled {{ background: {c['surface_primary_disabled']};
                                              color: {c['text_primary_disabled']};
                                              border: 1px solid {c['border_primary_disabled']}; }}
        QPushButton#stopTxAction {{ background: {c['error']}; color: {c['text_primary']};
                                    border: 1px solid {c['error']}; font-weight: 700; }}
        QPushButton#stopTxAction:hover {{ background: #ee6d68; border-color: #ffaaa3; }}
        QPushButton#stopTxAction:pressed {{ background: #d0605a; }}
        QMenuBar {{ background: {c['surface_tab']}; color: {c['text']}; padding: 4px; }}
        QMenuBar::item {{ padding: 6px 12px; border-radius: {r['control']}px; }}
        QMenuBar::item:selected {{ background: {c['surface_selected']}; color: {c['accent']}; }}
        QMenu {{ background: {c['surface_raised']}; color: {c['text']};
                 border: 1px solid {c['border_control']}; border-radius: {r['control']}px;
                 padding: 6px; }}
        QMenu::item {{ padding: 8px 24px 8px 16px; border-radius: 6px; }}
        QMenu::item:selected {{ background: {c['surface_selected']}; color: {c['accent']}; }}
        QMenu::item:disabled {{ color: {c['text_disabled']}; }}
        QMenu::separator {{ height: 1px; background: {c['border_subtle']}; margin: 6px 8px; }}
        QToolTip {{ background: {c['surface_raised']}; color: {c['text']};
                    border: 1px solid {c['border_control']}; padding: 8px;
                    border-radius: {r['control']}px; }}
        QHeaderView::section {{ background: {c['surface_header']}; color: {c['text_header']};
                                border: 0; padding: 8px; font-weight: 700; }}
        QTableView {{ gridline-color: {c['border_subtle']}; alternate-background-color: {c['surface_sunken']}; }}
        QTableView::item:selected {{ background: {c['surface_selected']}; color: {c['accent']}; }}
        QDialog {{ background: {c['window']}; }}
        QMessageBox QLabel {{ background: transparent; }}
        QPushButton:focus, QTableView:focus, QListWidget:focus,
        QCheckBox:focus, QRadioButton:focus, QTabBar::tab:focus {{
            border: 1px solid {c['focus_ring']};
        }}
        QPushButton#primaryAction:focus {{ border: 2px solid {c['focus_ring_primary']}; }}
    """

"""Protect the redesigned workspace's selection/explicit-send and navigation boundaries."""
import os
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
import app as pixelqso

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    qt = QApplication.instance() or QApplication([])
    monkeypatch.setenv('PIXELQSO_DATA_DIR', str(tmp_path))
    (tmp_path/'settings.ini').write_text('[radio]\nauto_connect_cat=false\n')
    old_data = pixelqso.DATA
    with patch('app.QTimer.singleShot'):
        window = pixelqso.MainWindow()
    window.resize(1280, 900); window.show(); qt.processEvents()
    yield window
    window.editor_baseline = window.editor_signature()
    window.close(); qt.processEvents(); pixelqso.DATA = old_data

def test_navigation_stays_in_sync_with_existing_page_actions(workspace):
    workspace.workspace_navigation.button(1).click()
    assert workspace.tabs.currentIndex() == 1
    assert workspace.workspace_title.text() == 'A world in little squares.'
    workspace.tabs.setCurrentIndex(2)
    assert workspace.workspace_navigation.checkedId() == 2
    assert not workspace.tabs.tabBar().isVisible()

def test_stage_and_card_selection_never_transmit_and_refresh_preserves_stage(workspace):
    assert workspace._outgoing_message_type() == 'exchange'
    with patch.object(workspace, 'transmit_exchange') as send:
        workspace.stage_select_buttons['cq'].click()
        assert workspace._outgoing_message_type() == 'cq'
        assert workspace.stage_rows['cq'].isVisible()
        assert workspace.stage_rows['exchange'].isVisible()
        workspace.refresh_card_library()
        assert workspace._outgoing_message_type() == 'cq'
        assert workspace.stage_picker.currentData() == 'cq'
        send.assert_not_called()
    assert workspace.tx_btn.isHidden()
    assert all(button.isVisible() for button in workspace.stage_send_buttons.values())
    assert not workspace.tx_btn.isEnabled()  # disconnected remains gated
    assert not workspace.outgoing_art.pixmap().isNull()

def test_stop_remains_visible_for_legacy_stage_send_control(workspace):
    workspace._active_send_control = workspace.stage_send_buttons['cq']
    workspace.stop_tx_btn.setEnabled(True)
    workspace._update_active_send_control(True)
    assert workspace.stop_tx_btn.isVisible()
    assert workspace.stop_tx_btn.isEnabled()
    # Docked actions are outside the scrolling card/details content.
    from PySide6.QtWidgets import QScrollArea
    parent = workspace.stop_tx_btn.parentWidget()
    while parent is not None:
        assert not isinstance(parent, QScrollArea)
        parent = parent.parentWidget()

def test_current_drawing_is_not_replaced_by_stale_manual_card(workspace, tmp_path):
    import json
    saved = tmp_path/'cards'/'saved.json'
    saved.write_text(json.dumps({'callsign':'W7TEST','grid':'CN85','card_id':123,
        'width':32,'height':32,'palette':workspace.card.palette,'pixels':[1]*1024}))
    workspace.refresh_card_library()
    workspace.card_combo.setCurrentIndex(workspace.card_combo.findData(str(saved)))
    selected = workspace.stage_card_combos['exchange']
    with patch.object(workspace, 'transmit_exchange') as send:
        selected.setCurrentIndex(selected.findData(str(saved)))
        assert workspace._chosen_card().callsign == 'W7TEST'
        workspace.refresh_card_library()
        assert selected.currentData() == str(saved)
        selected.setCurrentIndex(0)
        assert workspace._chosen_card().pixels == workspace.card.pixels
        send.assert_not_called()


def test_each_stage_send_dispatches_only_its_assigned_card(workspace, tmp_path):
    import json
    expected = {}
    for i, stage in enumerate(('cq', 'exchange', 'report73', 'final73')):
        path = tmp_path/'cards'/f'{stage}.json'
        path.write_text(json.dumps({'callsign':f'W7T{i}','grid':'CN85','card_id':200+i,
            'width':32,'height':32,'palette':workspace.card.palette,'pixels':[i]*1024}))
        expected[stage] = path
    workspace.refresh_card_library()
    with patch.object(workspace, 'transmit_exchange') as send:
        for stage, path in expected.items():
            combo = workspace.stage_card_combos[stage]
            combo.setCurrentIndex(combo.findData(str(path)))
            send.assert_not_called()
            workspace.stage_send_buttons[stage].click()
            assert workspace._outgoing_message_type() == stage
            assert send.call_args.kwargs['card_override'].callsign == json.loads(path.read_text())['callsign']
            send.reset_mock()

def test_resizable_columns_keep_stages_visible_and_remember_sizes(workspace):
    from PySide6.QtCore import QByteArray
    qt = QApplication.instance()
    splitter = workspace.operate_splitter
    assert not splitter.childrenCollapsible()
    splitter.setSizes([480, 500]); qt.processEvents()
    before = splitter.sizes()
    splitter.moveSplitter(410, 1); qt.processEvents()
    assert splitter.sizes() != before
    assert isinstance(workspace.settings.value('ui/operate/splitter'), QByteArray)
    saved_sizes = splitter.sizes()
    splitter.setSizes([500, 300])
    workspace._configure_workspace_splitter(splitter, 'operate', [500, 300])
    qt.processEvents()
    assert splitter.sizes() == saved_sizes
    for button in workspace.stage_send_buttons.values():
        assert button.isVisible()
        assert button.visibleRegion().boundingRect().contains(button.rect())
    workspace.editor_dialog.show(); qt.processEvents()
    workspace.editor_splitter.moveSplitter(480, 1); qt.processEvents()
    assert not workspace.editor_splitter.childrenCollapsible()
    assert isinstance(workspace.settings.value('ui/editor/splitter'), QByteArray)
    workspace.editor_baseline = workspace.editor_signature()
    workspace.editor_dialog.close()


def test_large_text_keeps_editor_form_inside_column(workspace):
    from PySide6.QtWidgets import QScrollArea
    workspace.editor_dialog.show()
    workspace._apply_interface_text_size(115)
    QApplication.processEvents()
    sidebar = workspace.editor_dialog.findChild(QScrollArea)
    assert sidebar.widget().width() <= sidebar.viewport().width()
    workspace._apply_interface_text_size(100)
    workspace.editor_baseline = workspace.editor_signature()
    workspace.editor_dialog.close()


@pytest.mark.parametrize('text_size', [100, 115])
@pytest.mark.parametrize('width', [1198, 1440])
def test_outgoing_card_and_details_fit_sender_viewport(workspace, text_size, width):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QScrollArea
    workspace.show_experimental_modes.setChecked(True)
    workspace.backend_combo.setCurrentIndex(workspace.backend_combo.findData('experimental'))
    workspace.advanced_controls_action.setChecked(True)
    workspace.mode_combo.setCurrentIndex(workspace.mode_combo.findData('experimental_8fsk_orthogonal'))
    workspace.card_combo.addItem('A very long saved card title ' * 8, 'layout-only-card')
    workspace._apply_interface_text_size(text_size)
    workspace.resize(width, 1064)
    workspace.operate_splitter.setSizes([370, 740])
    for _ in range(5):
        QApplication.processEvents()
    art = workspace.outgoing_art
    scroll = art.parentWidget()
    while not isinstance(scroll, QScrollArea):
        scroll = scroll.parentWidget()
    scroll.verticalScrollBar().setValue(0)
    QApplication.processEvents()
    viewport = scroll.viewport()
    assert scroll.widget().width() <= viewport.width()
    card_rect = art.rect().translated(art.mapTo(viewport, QPoint(0, 0)))
    assert viewport.rect().contains(card_rect)
    assert abs(card_rect.center().x() - viewport.rect().center().x()) <= 2
    for control in (workspace.mode_combo, workspace.mode_compatibility_filter,
                    workspace.rx_mode_preference_form_row):
        left = control.mapTo(viewport, QPoint(0, 0)).x()
        assert left >= 0
        assert left + control.width() <= viewport.width()

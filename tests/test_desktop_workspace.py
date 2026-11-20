"""Protect the redesigned workspace's selection/explicit-send and navigation boundaries."""
import os
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
import app as pixelqso

@pytest.fixture
def fresh_workspace(tmp_path, monkeypatch):
    qt = QApplication.instance() or QApplication([])
    monkeypatch.setenv('PIXELQSO_DATA_DIR', str(tmp_path))
    (tmp_path/'settings.ini').write_text('[radio]\nauto_connect_cat=false\n')
    old_data = pixelqso.DATA
    with patch('app.QTimer.singleShot'):
        window = pixelqso.MainWindow()
    window.show()
    for _ in range(5):
        qt.processEvents()
    yield window
    window.editor_baseline = window.editor_signature()
    window.close(); qt.processEvents(); pixelqso.DATA = old_data

@pytest.fixture
def workspace(fresh_workspace):
    fresh_workspace.resize(1280, 900)
    QApplication.processEvents()
    return fresh_workspace


def test_startup_shows_artwork_and_card_picker_without_resizing(fresh_workspace):
    from PySide6.QtCore import Qt
    w = fresh_workspace
    assert w.operate_splitter.width() >= 760
    assert w.operate_splitter.orientation() == Qt.Orientation.Horizontal
    for widget in (w.outgoing_art, w.browse_outgoing_cards, w.session_wall_empty):
        assert widget.visibleRegion().boundingRect().contains(widget.rect())
    assert w.outgoing_art.pixmap() and not w.outgoing_art.pixmap().isNull()
    with patch.object(w, 'transmit_exchange') as send:
        w.browse_outgoing_cards.click()
        assert w.tabs.currentIndex() == 1
        send.assert_not_called()


def test_compact_layout_keeps_artwork_and_send_actions_visible(workspace):
    from PySide6.QtCore import Qt
    w = workspace
    splitter = w.operate_splitter
    splitter.moveSplitter(455, 1)
    wide_size = splitter.sizes()[0]
    for size in [(1000, 800), (930, 800)]:
        w.resize(*size)
        for _ in range(5):
            QApplication.processEvents()
        assert splitter.width() < 820
        assert splitter.orientation() == Qt.Orientation.Vertical
        for widget in (w.outgoing_art, w.browse_outgoing_cards, w.stage_send_buttons["card"]):
            assert widget.visibleRegion().boundingRect().contains(widget.rect())
        assert not w.session_wall_empty.visibleRegion().isEmpty()
    w.resize(1280, 900)
    for _ in range(5):
        QApplication.processEvents()
    assert splitter.orientation() == Qt.Orientation.Horizontal
    assert abs(splitter.sizes()[0] - wide_size) <= 2
    assert w.outgoing_art.visibleRegion().boundingRect().contains(w.outgoing_art.rect())


def test_card_requests_disclosed_on_demand_or_when_heard(workspace):
    w = workspace
    assert w.card_requests_panel.isHidden()
    assert w.incoming_requests.isHidden()
    w.card_requests_toggle.click()
    assert w.card_requests_panel.isVisible()
    assert w.broadcast_requests_button.isVisible()
    assert w.incoming_requests.isHidden()
    w.card_requests_toggle.click()
    assert w.card_requests_panel.isHidden()
    with patch.object(w, 'transmit_exchange') as send:
        w._receive_card_requests([dict(requester='W7TEST', target=w.station_call.text(),
                                       card_id=123, reason=0)])
        assert w.card_requests_toggle.isChecked()
        assert w.incoming_requests.count() == 1
        assert w.incoming_requests.isVisible()
        assert '1 heard' in w.card_requests_toggle.text()
        w.incoming_requests.setCurrentRow(0)
        assert w.queue_resend_button.isEnabled()
        send.assert_not_called()


def test_navigation_stays_in_sync_with_existing_page_actions(workspace):
    workspace.workspace_navigation.button(1).click()
    assert workspace.tabs.currentIndex() == 1
    assert workspace.workspace_title.text() == 'A world in little squares.'
    workspace.tabs.setCurrentIndex(2)
    assert workspace.workspace_navigation.checkedId() == 2
    assert not workspace.tabs.tabBar().isVisible()

def test_stage_and_card_selection_never_transmit_and_refresh_preserves_stage(workspace):
    assert workspace._outgoing_message_type() == 'card'
    assert workspace.stage_send_buttons["card"].isVisible()
    assert not workspace.stage_send_buttons["exchange"].isVisible()
    with patch.object(workspace, 'transmit_exchange') as send:
        workspace.qso_stage_toggle.click()
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
    assert workspace.stage_send_buttons["card"].isVisible()
    workspace.qso_stage_toggle.click()
    qt.processEvents()
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
    sidebar = workspace.editor_dialog.findChild(QScrollArea, "editorSidebar")
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
    workspace.send_details_toggle.setChecked(True)
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


def test_getting_started_follows_real_setup_state(workspace, tmp_path):
    import json
    w = workspace
    assert w._getting_started_next == "identity"
    assert "call sign" in w.session_wall_empty.text()
    assert w.getting_started_button.isVisible()
    with patch.object(w, "open_station_settings") as settings:
        w.getting_started_button.click()
        settings.assert_called_once_with(0)
    w.station_call.setText("W7TEST")
    w.station_grid.setText("CN85")
    w._refresh_station_identity()
    assert w._getting_started_next == "card"
    w.getting_started_button.click()
    assert w.tabs.currentIndex() == 1
    assert w.library_filter.currentData() == "starter"
    w.tabs.setCurrentIndex(0)
    (tmp_path / "cards" / "ready.json").write_text(json.dumps({
        "callsign": "W7TEST", "grid": "CN85", "card_id": 123,
        "width": 32, "height": 32, "palette": w.card.palette,
        "pixels": [1] * 1024,
    }))
    w.refresh_card_library()
    assert w._getting_started_next == "radio"
    with patch.object(w, "open_station_settings") as settings:
        w.getting_started_button.click()
        settings.assert_called_once_with(3)
    with patch.object(pixelqso.DATA2G_ADAPTER, "is_connected", return_value=True):
        w._refresh_getting_started()
        assert w._getting_started_next is None
        assert w.getting_started_button.isHidden()
    w._refresh_getting_started()
    assert w._getting_started_next == "radio"


def test_saved_gallery_card_assigns_selected_stage_only(workspace, tmp_path):
    import json
    from PySide6.QtCore import Qt
    w = workspace
    path = tmp_path / "cards" / "ready.json"
    path.write_text(json.dumps({
        "callsign": "W7TEST", "grid": "CN85", "card_id": 123,
        "width": 32, "height": 32, "palette": w.card.palette,
        "pixels": [1] * 1024,
    }))
    w.refresh_card_library()
    w.stage_select_buttons["cq"].click()
    w.browse_outgoing_cards.click()
    item = next(w.library_list.item(i) for i in range(w.library_list.count())
                if w.library_list.item(i).data(Qt.ItemDataRole.UserRole) == str(path))
    w.library_list.setCurrentItem(item)
    assert w.use_library_card_button.isVisible()
    assert w.use_library_card_button.text() == "Use for CQ"
    with patch.object(w, "transmit_exchange") as send:
        w.use_library_card_button.click()
        assert w.tabs.currentIndex() == 0
        assert w._outgoing_message_type() == "cq"
        assert w.stage_card_combos["cq"].currentData() == str(path)
        assert all(w.stage_card_combos[s].currentData() is None for s in ("exchange", "report73", "final73"))
        assert w._chosen_card().card_id == 123
        w.refresh_card_library()
        assert w.stage_card_combos["cq"].currentData() == str(path)
        w.stage_card_combos["cq"].setCurrentIndex(0)
        w.refresh_card_library()
        assert w.stage_card_combos["cq"].currentData() is None
        send.assert_not_called()


def test_placeholder_does_not_autoconnect_data2g(tmp_path, monkeypatch):
    qt = QApplication.instance() or QApplication([])
    monkeypatch.setenv("PIXELQSO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PIXELQSO_CALLSIGN", raising=False)
    old_data = pixelqso.DATA
    for call, expected in (("N0CALL", False), ("W7TEST", True)):
        (tmp_path / "settings.ini").write_text(f"[station]\ncallsign={call}\ngrid=CN85\n")
        with patch("app.QTimer.singleShot") as schedule:
            w = pixelqso.MainWindow()
        try:
            assert any(c.args[-1] == w._connect_data2g_host for c in schedule.call_args_list) == expected
        finally:
            w.close()
            qt.processEvents()
    pixelqso.DATA = old_data


def test_editor_tool_disclosure_and_palette_are_discoverable(workspace):
    w = workspace
    w.editor_dialog.show()
    for _ in range(4):
        QApplication.processEvents()
    assert w.tool_buttons["draw"].isVisible()
    assert w.tool_buttons["select"].isVisible()
    assert w.editor_shapes_widget.isHidden()
    assert "Draw" in w.editor_tool_hint.text()
    assert w.palette_layout.getItemPosition(7)[:2] == (0, 7)
    assert w.palette_selected_label.text().startswith("2/8")
    w.editor_shapes_toggle.click()
    assert w.editor_shapes_widget.isVisible()
    w.tool_buttons["filled_rect"].click()
    assert w.canvas.tool == "filled_rect"
    assert "Filled rectangle" in w.editor_tool_hint.text()
    w.editor_shapes_toggle.click()
    assert w.editor_shapes_widget.isHidden()
    w.set_editor_tool("ellipse")
    assert w.editor_shapes_widget.isVisible()
    w.palette_layout.itemAt(3).widget().click()
    assert w.canvas.color_index == 3
    assert w.canvas.tool == "draw"
    assert w.palette_selected_label.text().startswith("4/8")
    assert w.new_stamp_button.menu() is not None
    assert [a.text() for a in w.new_stamp_button.menu().actions()] == [
        "Text / emoji…", "Draw a stamp…", "Import image…"]
    assert w.new_stamp_button.isVisible()
    assert w.stamp_list.isVisible()


def test_editor_canvas_has_usable_space_at_common_display_sizes(workspace):
    w = workspace
    w.editor_dialog.show()
    for width, height in ((1260, 860), (1080, 780)):
        w.editor_dialog.resize(width, height)
        for _ in range(6):
            QApplication.processEvents()
        # Toolbars, palettes, and status must not leave a tiny canvas.
        assert w.canvas.visibleRegion().boundingRect().width() >= 320
        assert w.canvas.visibleRegion().boundingRect().height() >= 320
        assert w.editor_done_button.isVisible()
    w._apply_interface_text_size(115)
    for _ in range(6):
        QApplication.processEvents()
    assert w.canvas.visibleRegion().boundingRect().width() >= 320
    assert w.editor_done_button.isVisible()
    w._apply_interface_text_size(100)


def test_editor_workbench_rail_docks_and_zoom_do_not_transmit(workspace):
    from PySide6.QtCore import Qt
    w = workspace
    w.editor_dialog.show()
    for _ in range(5):
        QApplication.processEvents()
    assert w.editor_canvas_view.objectName() == "editorCanvasViewport"
    assert w.editor_zoom_combo.currentData() == "fit"
    assert w.editor_palette_toggle.isChecked()
    assert w.editor_stamps_toggle.isChecked()
    assert not w.editor_properties_toggle.isChecked()
    assert w.tool_buttons["draw"].isVisible()
    assert w.editor_mirror.isVisible()
    assert not w.editor_centered.isVisible()
    assert w.canvas.width() >= 320

    baseline = w.editor_signature()
    with patch.object(w, "transmit_exchange") as send:
        w.editor_zoom_combo.setCurrentIndex(w.editor_zoom_combo.findData(200))
        assert w.canvas.width() == 1024
        assert w.editor_canvas_view.horizontalScrollBar().maximum() > 0
        w.editor_zoom_out.click()
        assert w.editor_zoom_combo.currentData() == 150
        w.editor_zoom_combo.setCurrentIndex(w.editor_zoom_combo.findData("fit"))
        assert w.canvas.width() <= max(320, w.editor_canvas_view.viewport().width())

        w.tool_buttons["erase"].click()
        assert w.canvas.tool == "erase"
        assert w.editor_mirror.isVisible()
        w.tool_buttons["select"].click()
        assert not w.editor_mirror.isVisible()
        assert not w.editor_centered.isVisible()
        w.tool_buttons["rect"].click()
        assert w.editor_shapes_widget.isVisible()
        assert w.editor_centered.isVisible()

        w.editor_properties_toggle.click()
        assert w.editor_properties_toggle.isChecked()
        assert w.call.isVisible()
        w.editor_stamps_toggle.click()
        assert not w.stamp_list.isVisible()
        w.editor_stamps_toggle.click()
        assert w.stamp_list.isVisible()
        send.assert_not_called()
    assert w.editor_signature() == baseline


def test_editor_file_menu_and_animation_timeline(workspace):
    w = workspace
    w.editor_dialog.show()
    for _ in range(5):
        QApplication.processEvents()
    menu = next(button.menu() for button in w.editor_dialog.findChildren(pixelqso.QPushButton)
                if button.text().startswith("File") and button.menu() is not None)
    labels = [action.text() for action in menu.actions()]
    for name in ("New card", "Open card…", "Import indexed PNG…",
                 "Export indexed PNG…", "Make QR card…"):
        assert name in labels
    assert w.editor_done_button.isVisible()
    w.animation_author.start()
    QApplication.processEvents()
    assert w.animation_author.controls.isVisible()
    assert w.animation_author.preview_panel.isVisible()
    assert w.animation_author.frames[1]["card"].card_id != w.animation_author.frames[0]["card"].card_id
    assert w.canvas.visibleRegion().boundingRect().height() >= 320


def test_card_collection_inspector_is_contextual(workspace, tmp_path):
    import json
    from PySide6.QtCore import Qt
    w = workspace
    w.tabs.setCurrentIndex(1)
    QApplication.processEvents()
    assert w.library_inspector_preview.pixmap().isNull()
    path = tmp_path / "cards" / "choice.json"
    path.write_text(json.dumps({
        "callsign": "W7TEST", "grid": "CN85", "card_id": 123,
        "width": 32, "height": 32, "palette": w.card.palette,
        "pixels": [1] * 1024,
    }))
    w.refresh_card_library()
    target = next(w.library_list.item(i) for i in range(w.library_list.count())
                  if w.library_list.item(i).data(Qt.ItemDataRole.UserRole) == str(path))
    w.library_list.setCurrentItem(target)
    assert "choice" in w.library_inspector_name.text()
    assert not w.library_inspector_preview.pixmap().isNull()
    assert w.open_library_button.isVisible()
    assert w.use_library_card_button.isVisible()
    w.library_list.clearSelection()
    w.library_list.setCurrentRow(-1)
    assert w.library_inspector_preview.pixmap().isNull()



def test_share_card_is_default_and_qso_is_optional(workspace):
    w = workspace
    assert w._outgoing_message_type() == "card"
    assert w.stage_card_combos["card"].currentData() is None
    assert w.stage_send_buttons["card"].text() == "Send card"
    assert w.stage_send_buttons["card"].isVisible()
    assert not w.qso_stage_toggle.isChecked()
    assert not w.auto_stage_group.isVisible()
    assert not w.send_details_group.isVisible()
    assert "optional" in w.qso_stage_toggle.text()
    with patch.object(w, "transmit_exchange") as send:
        w.send_details_toggle.click()
        assert w.send_details_group.isVisible()
        w.send_details_toggle.click()
        assert not w.send_details_group.isVisible()
        send.assert_not_called()
    assert w.cq_interval.value() == 60
    assert w.log_deck_view.currentData() == "recent"
    with patch.object(w, "transmit_exchange") as transmit:
        w.stage_card_combos["card"].setCurrentIndex(0)
        assert w._outgoing_message_type() == "card"
        transmit.assert_not_called()
        w.stage_send_buttons["card"].click()
        transmit.assert_called_once_with(card_override=None)
    w.qso_stage_toggle.click()
    assert w.auto_stage_group.isVisible()
    assert w.stage_send_buttons["exchange"].isVisible()
    assert w.call_cq_button.isVisible()
    w.qso_stage_toggle.click()
    assert not w.stage_send_buttons["exchange"].isVisible()
    assert w.stage_send_buttons["card"].isVisible()


def test_focus_heard_artwork_changes_target_without_sending(workspace):
    w = workspace
    first = {"card": {"callsign": "W7ONE", "grid": "CN85", "card_id": 10,
                      "message_type": "card"},
             "assembly_event": {"verified_complete": True}}
    second = {"card": {"callsign": "W7TWO", "grid": "CN85", "card_id": 11,
                       "message_type": "card"},
              "assembly_event": {"verified_complete": True}}
    with patch.object(w, "transmit_exchange") as transmit:
        w._show_reply_action(first)
        assert w.reply_report is first
        assert "Share card after W7ONE" in w.reply_to_button.text()
        w._focus_heard_card(first)
        assert w._pinned_heard_key is not None
        w._show_reply_action(second)
        assert w.reply_report is first
        assert w._latest_heard_report is second
        w.heard_focus_clear.click()
        assert w._pinned_heard_key is None
        assert w.reply_report is second
        transmit.assert_not_called()


def test_standalone_card_reply_is_not_formal_exchange(workspace):
    w = workspace
    received = {"card": {"callsign": "W7ART", "grid": "CN85", "card_id": 98,
                         "message_type": "card"},
                "assembly_event": {"verified_complete": True}}
    w._show_reply_action(received)
    with patch.object(w, "transmit_exchange") as transmit, \
         patch.object(w, "start_auto_exchange") as auto:
        w.reply_to_received()
        assert w._outgoing_message_type() == "card"
        assert w.contact_stage == "idle"
        transmit.assert_called_once()
        auto.assert_not_called()
    assert w._prepare_outgoing_card(w._chosen_card(), "card").reception_report is None


def test_data2g_share_uses_standalone_card_wire_type(workspace):
    """Broadcast card uses a standalone wire type, without completing a QSO."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    w = workspace
    w.data2g_session = Mock()
    backend = SimpleNamespace(
        usable=True, mode_name="offline-test-mode", max_frame_bytes=128,
        mode=SimpleNamespace(seconds_at_max=5),
    )
    try:
        with patch.object(pixelqso.DATA2G_ADAPTER, "is_connected", return_value=True), \
             patch.object(pixelqso.DATA2G_ADAPTER, "frame_card",
                          return_value=[b"offline-test-frame"]) as frame, \
             patch.object(pixelqso.DATA2G_ADAPTER, "select_mode") as select, \
             patch.object(w, "_confirm_data2g_ptt_recovery", return_value=True), \
             patch.object(w, "_data2g_send_next") as send, \
             patch.object(w, "refresh_tx_button"), \
             patch.object(pixelqso.QMessageBox, "warning",
                          side_effect=AssertionError("Unexpected warning")):
            w._prepare_data2g_transmission(backend)
            assert frame.call_count == 1
            assert frame.call_args.args[2] == "card"
            assert frame.call_args.args[3] is None
            assert select.call_count == 1
            assert send.call_count == 1
            assert w.tx_message_type == "card"
            assert w.active_qso_log["deck_type"] == "broadcast"
            assert w.active_qso_log["peer_callsign"] == "Broadcast"
            assert "unconfirmed" in w.active_qso_log["status"]
            assert w.active_qso_log["sent_cards"][0]["card"]["message_type"] == "card"
    finally:
        w.data2g_tx_total = 0
        w.data2g_tx_frames = []
        w.data2g_tx_waiting = None
        w.data2g_session = None


def test_cardcast_playlist_add_load_send_and_reorder_are_explicit(workspace, tmp_path):
    """Queueing and navigating cannot accidentally key the radio."""
    import json
    from PySide6.QtCore import Qt
    w = workspace
    cards = []
    for i in range(2):
        path = tmp_path / "cards" / f"art-{i}.json"
        path.write_text(json.dumps({
            "callsign": "W7TEST", "grid": "CN85", "card_id": 400 + i,
            "width": 32, "height": 32, "palette": w.card.palette,
            "pixels": [i] * 1024,
        }))
        cards.append(str(path))
    w.refresh_card_library()

    with patch.object(w, "transmit_exchange") as send:
        w.cardcast_browse_button.click()
        assert w.tabs.currentIndex() == 1
        assert w.cardcast_playlist.paths == []
        for path in cards:
            item = next(w.library_list.item(i) for i in range(w.library_list.count())
                        if w.library_list.item(i).data(Qt.ItemDataRole.UserRole) == path)
            w.library_list.setCurrentItem(item)
            assert w.queue_library_card_button.isVisible()
            w.queue_library_card_button.click()
        assert w.cardcast_playlist.paths == cards
        assert w.cardcast_queue_list.count() == 2
        assert len(w.settings.value("ui/cardcast/queue")) == 2
        assert w.stage_card_combos["card"].currentData() is None
        send.assert_not_called()

        w.tabs.setCurrentIndex(0)
        w.cardcast_queue_list.setCurrentRow(1)
        w.cardcast_up_button.click()
        assert w.cardcast_playlist.paths == list(reversed(cards))
        assert w.cardcast_queue_list.currentRow() == 0
        w.cardcast_load_button.click()
        assert w.stage_card_combos["card"].currentData() == cards[1]
        assert w._outgoing_message_type() == "card"
        assert "does not transmit" in w.cardcast_queue_status.text()
        send.assert_not_called()

        w.cardcast_send_button.click()
        send.assert_called_once()
        assert send.call_args.kwargs["card_override"].card_id == 401
        assert w.cardcast_playlist.paths == list(reversed(cards))
        assert "stays queued" in w.cardcast_queue_status.text()
        w.cardcast_remove_button.click()
        assert w.cardcast_playlist.paths == [cards[0]]
        assert all(path in w.settings.value("ui/cardcast/queue") for path in [cards[0]])
        assert cards[1] not in w.settings.value("ui/cardcast/queue")
        assert (tmp_path / "cards" / "art-1.json").is_file()
        # A running TX/beacon must neither be replaced nor stopped by a queue click.
        with patch.object(w, "_station_operation_active", return_value=True):
            w.cardcast_send_button.click()
            assert "Finish the current transmission" in w.cardcast_queue_status.text()
            assert w.cardcast_playlist.paths == [cards[0]]
        send.assert_called_once()


def test_cardcast_queue_prunes_deleted_library_card(workspace, tmp_path):
    import json
    from PySide6.QtCore import Qt
    w = workspace
    path = tmp_path / "cards" / "going-away.json"
    path.write_text(json.dumps({
        "callsign": "W7TEST", "grid": "CN85", "card_id": 987,
        "width": 32, "height": 32, "palette": w.card.palette,
        "pixels": [1] * 1024,
    }))
    w.refresh_card_library()
    w.library_list.setCurrentItem(next(
        w.library_list.item(i) for i in range(w.library_list.count())
        if w.library_list.item(i).data(Qt.ItemDataRole.UserRole) == str(path)))
    w._queue_selected_library_card()
    assert w.cardcast_playlist.paths == [str(path)]
    path.unlink()
    w.refresh_card_library()
    assert not w.cardcast_playlist.paths
    assert w.cardcast_queue_list.count() == 0
    assert w.cardcast_empty.isVisible() or w.tabs.currentIndex() != 0


def test_heard_gallery_tiles_show_integrity_and_safe_actions(workspace):
    w = workspace
    def report(call, card_id, verified):
        return {
            "card": {
                "callsign": call, "grid": "CN85", "card_id": card_id,
                "message_type": "card", "width": 16, "height": 16,
                "pixel_coverage": [True] * 256,
                "verified_pixel_coverage": ([True] * 256 if verified
                                             else [False] * 256),
            },
            "pixels": [[[12, 4, 2]] * 16 for _ in range(16)],
            "assembly_event": {"verified_complete": verified},
        }
    w._add_session_wall_card(report("W7ART", 44, True))
    w._add_session_wall_card(report("W7MORE", 45, False))
    assert w.session_wall_count == 2
    assert "2 heard" in w.heard_gallery_count.text()
    assert "1 verified" in w.heard_gallery_count.text()
    assert "1 provisional" in w.heard_gallery_count.text()
    tiles = [button for _item, button in w.session_wall_items.values()]
    assert any("✓ VERIFIED" in button._caption.text() for button in tiles)
    assert any("PREVIEW" in button._caption.text() for button in tiles)

    with patch.object(w, "transmit_exchange") as send:
        verified = next(button for button in tiles
                        if button._report["card"]["callsign"] == "W7ART")
        verified.click()
        assert w.heard_focus_actions.isVisible()
        assert w._pinned_heard_key is not None
        w.heard_station_archive_button.click()
        assert w.tabs.currentIndex() == 2
        assert w.log_deck_view.currentData() == "received_by_call"
        assert w.log_search.text() == "W7ART"
        w.tabs.setCurrentIndex(0)
        w.heard_focus_clear.click()
        assert not w.heard_focus_actions.isVisible()
        w.heard_archive_button.click()
        assert w.log_search.text() == ""
        assert w.log_deck_view.currentData() == "recent"
        send.assert_not_called()


def test_quick_draw_after_standalone_art_only_prepares_next_card(workspace):
    w = workspace
    incoming = {"card": {"callsign": "W7ART", "grid": "CN85",
                         "card_id": 123, "message_type": "card"},
                "assembly_event": {"verified_complete": True}}
    w._focus_heard_card(incoming)
    seen = []
    with patch.object(w.editor_dialog, "exec",
                      side_effect=lambda: seen.append(w.editor_done_button.text())), \
         patch.object(w, "transmit_exchange") as send:
        w.start_quick_draw_reply()
        assert seen == ["Use this card"]
        send.assert_not_called()
    assert w._outgoing_message_type() == "card"



def test_recent_cards_archive_is_individual_and_chronological(workspace, tmp_path):
    import json
    from PySide6.QtCore import Qt
    w = workspace

    def reception(call, card_id, when, verified):
        return {
            "card": {"callsign": call, "grid": "CN85",
                     "card_id": card_id, "message_type": "card",
                     "width": 16, "height": 16, "exact": verified},
            "pixels": [[[11, 3, 5]] * 16 for _ in range(16)],
            "logged_at": when,
            "assembly_event": {"verified_complete": verified},
        }

    reports = [
        reception("K7FIRST", 12, "2026-11-20 09:00:00", True),
        reception("K7LAST", 13, "2026-11-20 10:00:00", False),
    ]
    w.qso_log_dir.mkdir(parents=True, exist_ok=True)
    path = w.qso_log_dir / "saved-cards.json"
    path.write_text(json.dumps({
        "peer_callsign": "Some session",
        "started_at": "2026-11-20 08:00:00",
        "received_cards": reports,
        "sent_cards": [],
    }))
    w.tabs.setCurrentIndex(2)
    w.log_deck_view.setCurrentIndex(w.log_deck_view.findData("recent"))
    w.refresh_qso_log()
    assert w.log_gallery.count() == 2
    assert w.contact_summary.text() == "2 cards"
    newest = w.log_gallery.item(0)
    assert newest.data(Qt.ItemDataRole.UserRole).startswith("recent-card:")
    assert newest.data(Qt.ItemDataRole.UserRole + 1)["callsign"] == "K7LAST"
    assert "Provisional" in newest.data(Qt.ItemDataRole.UserRole + 1)["status"]
    source = newest.data(Qt.ItemDataRole.UserRole)
    loaded, editable_path = w._load_qso_deck_source(source)
    assert editable_path is None
    assert loaded["received_cards"][0]["card"]["card_id"] == 13
    assert loaded["sent_cards"] == []

    w.log_gallery.setCurrentRow(0)
    assert w.contact_selected_title.text() == "K7LAST"
    assert w.open_qso_button.text() == "Open received card"
    with patch.object(w, "transmit_exchange") as send:
        w.log_sort.setCurrentIndex(1)
        assert w.log_gallery.item(0).data(Qt.ItemDataRole.UserRole + 1)["callsign"] == "K7FIRST"
        w.log_sort.setCurrentIndex(0)
        w.log_search.setText("K7FIRST")
        assert w.log_gallery.count() == 1
        w.log_deck_view.setCurrentIndex(w.log_deck_view.findData("received_by_call"))
        assert w.log_gallery.count() == 1
        assert w.contact_summary.text() == "1 station"
        send.assert_not_called()

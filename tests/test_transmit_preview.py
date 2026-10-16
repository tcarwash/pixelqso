"""TX artwork follows actual playback/host frames and cannot become RX evidence."""
import copy
import json
import os
from dataclasses import asdict
from unittest.mock import Mock, patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
import pytest
import cardmodem as modem
from card_backends import get_backend
from transmit_preview import LocalTransmitPreview, Data2GTransmitPreview


def card():
    return modem.Card('AG7SU', 'CN85', list(modem.MINIMAL_AVATAR_PALETTE), [0, 1]*512, 123)


def model(mode='resilient_100', copies=2):
    backend = get_backend(mode); source = card()
    audio = backend.encode(source, copies, 'card', None)
    return LocalTransmitPreview(source, mode, backend, copies, 'card', None, len(audio))


@pytest.fixture
def window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    import app as pixelqso
    qt = QApplication.instance() or QApplication([])
    monkeypatch.setenv('PIXELQSO_DATA_DIR', str(tmp_path))
    (tmp_path/'settings.ini').write_text('[radio]\nauto_connect_cat=false\n')
    old = pixelqso.DATA
    with patch('app.QTimer.singleShot'):
        result = pixelqso.MainWindow()
    yield result
    result.auto_armed = False; result.data2g_session = None; result.data2g_tx_total = 0
    result.tx_audio = None; result.tx_device = None
    result.close(); qt.processEvents(); pixelqso.DATA = old


def test_serial_preview_reveals_whole_encoded_blocks_after_their_audio():
    preview = model()
    assert not any(preview.state(0)['coverage'])
    first_end, first_pixels = preview.checkpoints[0]
    at = round((preview.prefix+first_end)*48000)
    assert sum(preview.state(at-1)['coverage']) == 0
    assert sum(preview.state(at+1)['coverage']) == first_pixels
    assert all(preview.state(round(preview.seconds*48000))['coverage'])
    second_start = round(preview.cycle_seconds*48000)+1
    assert preview.state(second_start)['copy'] == 2
    assert not any(preview.state(second_start)['coverage'])


def test_raw_e_major_preview_uses_palette_symbol_order_and_frozen_artwork():
    preview = model('experimental_8fsk_musical_25', 1)
    at = preview.prefix+preview.native_header_seconds+1/preview.baud
    assert sum(preview.state(round(at*48000)+1)['coverage']) == 1
    assert preview.report['pixels'] == card().pixels
    assert preview.report['card']['palette'] == list(modem.AVATAR_PALETTES[0])


def test_whole_card_coding_uses_global_reveal_instead_of_false_row_progress():
    preview = model('experimental_qpsk_equalized', 1)
    first = preview.state(round((preview.prefix+.1)*48000))
    later = preview.state(round((preview.seconds-preview.suffix-.1)*48000))
    assert first['coverage'] is None and later['coverage'] is None
    assert first['opacity'] < later['opacity']
    assert 'whole-card' in first['phase']
    assert first['progress'] < later['progress']


def test_data2g_reveals_only_acknowledged_fragment_bytes_and_resets_each_copy():
    from backend_adapters import DATA2G_ADAPTER
    from card_transfer import HEADER
    source = card(); frames = DATA2G_ADAPTER.frame_card(source, 128, 'card')
    preview = Data2GTransmitPreview(source, len(frames), 2*len(frames))
    assert not any(preview.state()['coverage'])
    preview.acknowledge(frames[0])
    expected = ((len(frames[0])-HEADER.size)*8)//3
    assert sum(preview.state()['coverage']) == expected
    for frame in frames[1:]: preview.acknowledge(frame)
    assert all(preview.state()['coverage'])
    preview.acknowledge(frames[0])
    assert preview.copy == 2 and sum(preview.state()['coverage']) == expected
    assert 'remote receipt unconfirmed' in preview.state()['detail']


def test_outgoing_preview_has_its_own_direction_and_preserves_received_preview(window):
    source = card()
    received = {'card': {**asdict(source), 'exact': False, 'pixel_coverage': [True]*40+[False]*984},
                'pixels': source.pixels}
    window._show_receive_preview(window.card_image(received), card=received['card'],
                                 coverage=received['card']['pixel_coverage'], report=received)
    image_key = window.receive_view.pixmap().cacheKey()
    records = copy.deepcopy(window.active_qso_log)
    preview = model(); window.tx_card = source; window.tx_bytes = b'\0'*round(preview.seconds*48000)*2
    window._begin_transmit_preview(preview, 'Resilient 100', 'exchange')
    assert window.traffic_preview.currentWidget() is window.tx_preview_box
    assert 'SENDING' in window.tx_preview_box.title() and 'outgoing' in window.tx_preview_box.title()
    assert 'audio playback' in window.tx_preview_progress.format()
    window._render_transmit_preview(preview.state(round(preview.seconds*48000/4)))
    assert window.receive_view.pixmap().cacheKey() == image_key
    assert window.active_qso_log == records
    assert 'received' not in window.tx_preview_progress.format()
    window._end_transmit_preview(False)
    assert window.traffic_preview.currentWidget() is window.receive_workspace
    assert 'Stopped' in window.tx_preview_phase.text()
    assert window.tx_preview_progress.value() < 1000


def test_audio_progress_uses_processed_samples_not_queued_pcm_and_includes_cwid(window):
    preview = model(); total = round((preview.seconds+5)*48000)
    window.tx_card = card(); window.tx_bytes = b'\0'*total*2
    window._begin_transmit_preview(preview, 'Resilient 100', 'exchange')
    window.tx_offset = len(window.tx_bytes)  # Queueing is not playback.
    window.tx_audio = Mock(); window.tx_audio.processedUSecs.return_value = 0
    window._update_local_transmit_preview(force=True)
    assert window.tx_preview_progress.value() == 0
    window.tx_audio.processedUSecs.return_value = round((preview.seconds+1)*1e6)
    window._update_local_transmit_preview(force=True)
    assert 'CW identification' in window.tx_preview_phase.text()
    assert window.tx_preview_progress.value() < 1000
    window.tx_audio = None


def test_data2g_stale_ack_cannot_advance_preview(window):
    from backend_adapters import DATA2G_ADAPTER
    source = card(); frames = DATA2G_ADAPTER.frame_card(source, 128, 'card')
    preview = Data2GTransmitPreview(source, len(frames), len(frames))
    window.tx_card = source; window._begin_transmit_preview(preview, 'Data2G', 'exchange')
    session = Mock(); window.data2g_session = session
    window.data2g_tx_total = len(frames); window.data2g_tx_frames = frames[1:]
    window.data2g_tx_waiting = b'aa'; window.data2g_tx_waiting_frame = frames[0]
    with patch.object(DATA2G_ADAPTER, 'group_port', return_value=1), patch.object(window, '_data2g_send_next'):
        window._data2g_tx_ack(session, 1, b'bb')
        assert preview.completed == 0
        window._data2g_tx_ack(session, 1, b'aa')
        assert preview.completed == 1 and sum(preview.coverage) > 0


def test_provisional_cards_persist_reload_and_upgrade_in_both_decks(window):
    from card_views import DeckDialog
    source = card(); source.callsign = 'K1ABC'
    received = {'card': {**asdict(source), 'avatar_burst': True, 'exact': False,
                        'pixel_coverage': [True]*70+[False]*954, 'snr_db': -10},
                'pixels': source.pixels}
    window._show_receive_preview(window.card_image(received), card=received['card'], exact=False,
                                 coverage=received['card']['pixel_coverage'], report=received)
    assert window._active_qso_path.exists()
    entry, _ = window._load_qso_deck_source(window._active_qso_path)
    assert len(entry['received_cards']) == 1 and not entry['received_cards'][0]['card']['exact']
    deck = DeckDialog(entry, window.card_image)
    assert len(deck.events) == 1 and 'Provisional' in deck.strip.item(0).text()
    assert not deck.add_button.isVisible(); deck.close()
    archive, _ = window._load_qso_deck_source('received-callsign:K1ABC')
    assert len(archive['received_cards']) == 1 and not archive['received_cards'][0]['card']['exact']
    # Better snapshots and changed reports update this card, not another tile.
    better = copy.deepcopy(received); better['card']['pixel_coverage'] = [True]*140+[False]*884
    better['card']['snr_db'] = -9
    window._record_received_card(better)
    assert len(window.active_qso_log['received_cards']) == 1
    # Final proof promotes the same deck entry and leaves the caller snapshot untouched.
    final = copy.deepcopy(better); final['card']['exact'] = True; final['card']['pixel_coverage'] = [True]*1024
    window._record_received_card(final)
    assert len(window.active_qso_log['received_cards']) == 1
    assert window.active_qso_log['received_cards'][0]['card']['exact']
    assert 'logged_at' not in received
    archive, _ = window._load_qso_deck_source('received-callsign:K1ABC')
    assert len(archive['received_cards']) == 1 and archive['received_cards'][0]['card']['exact']


def test_callsign_deck_coalesces_partial_sessions_but_keeps_known_different_artwork(window):
    source = card(); source.callsign = 'K1ABC'
    first = {'card': {**asdict(source), 'exact': False}, 'pixels': [0]*1024}
    better = copy.deepcopy(first); better['pixels'][:200] = [1]*200
    entries = [(None, {'received_cards': [first]}), (None, {'received_cards': [better]})]
    assert len(window._aggregate_received_decks(entries)['K1ABC']['received_cards']) == 1
    first['card'].update(image_id='raster-a'); better['card'].update(image_id='raster-b')
    assert len(window._aggregate_received_decks(entries)['K1ABC']['received_cards']) == 2

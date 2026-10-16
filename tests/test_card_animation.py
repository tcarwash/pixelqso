"""Two-frame links must supplement independent, verified image decoding."""
import copy
from dataclasses import asdict
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
import pytest
import cardmodem as modem
from card_animation import (artwork_digest, encode_link, decode_link, partner,
                            group_events, LinkCache, validate_link)


def cards():
    first = modem.Card('AG7SU', 'CN85', list(modem.MINIMAL_AVATAR_PALETTE), [0, 1]*512, 100)
    second = modem.Card('AG7SU', 'CN85', list(modem.MINIMAL_AVATAR_PALETTE), [1, 0]*512, 101)
    second.animation = dict(first_card_id=100, first_digest=artwork_digest(first), frame_ms=500)
    return first, second


def report(card, exact=True):
    return dict(card={**asdict(card), 'exact': exact}, pixels=list(card.pixels),
                logged_at='2026-10-14 12:00:00', receive_profile='test')


def test_sidecar_roundtrip_bound_to_second_artwork():
    first, second = cards()
    digest, link = decode_link(encode_link(second))
    assert digest == artwork_digest(second)
    assert link == second.animation
    assert encode_link(first) is None
    damaged = bytearray(encode_link(second)); damaged[10] ^= 1
    with pytest.raises(ValueError):
        decode_link(damaged)


@pytest.mark.parametrize('bad', [dict(first_card_id=-1, first_digest='a'*16),
    dict(first_card_id=100, first_digest='z'*16),
    dict(first_card_id=100, first_digest='a'*16, frame_ms=0),
    dict(first_card_id=100, first_digest='a'*16, frame_ms=10001)])
def test_invalid_reference(bad):
    with pytest.raises(ValueError):
        validate_link(bad)


def test_digest_matches_rgb_and_indexed_and_shared_palette_mapping():
    first, _ = cards()
    indexed = report(first)
    rgb = copy.deepcopy(indexed)
    rgb['pixels'] = [[list(v*17 for v in first.palette[first.pixels[y*32+x]])
                      for x in range(32)] for y in range(32)]
    assert artwork_digest(indexed) == artwork_digest(rgb) == artwork_digest(first)
    changed = copy.deepcopy(first); changed.pixels[0] ^= 1
    assert artwork_digest(changed) != artwork_digest(first)
    changed = copy.deepcopy(first); changed.callsign = 'N0CALL'
    assert artwork_digest(changed) != artwork_digest(first)


def test_pair_order_independent_and_retains_original_records():
    first, second = map(report, cards())
    for events in ([('received_cards', first), ('received_cards', second)],
                   [('received_cards', second), ('received_cards', first)]):
        combined = group_events(events)
        assert len(combined) == 1
        assert combined[0][1]['animation_frames'] == [first, second]
        assert 'animation_frames' not in second
    assert group_events([('received_cards', second)], [first])[0][1]['animation_frames'][0] is first


def test_missing_partial_wrong_station_old_artwork_and_dimensions_stay_separate():
    first, second = map(report, cards())
    assert partner(second, []) is None
    for alteration in ({'exact': False}, {'callsign': 'N0CALL'}, {'width': 16}, {'animation': second['card']['animation']}):
        bad = copy.deepcopy(first); bad['card'].update(alteration)
        assert partner(second, [bad]) is None
    bad = copy.deepcopy(first); bad['pixels'][0] ^= 1
    assert partner(second, [bad]) is None
    incomplete_second = copy.deepcopy(second); incomplete_second['card']['exact'] = False
    assert partner(incomplete_second, [first]) is None
    # A reused ID cannot override a fingerprint-matching historical frame.
    assert partner(second, [first, bad]) is first


def test_cache_attaches_only_to_verified_matching_artwork_and_rejects_conflicts():
    first, second = cards(); cache = LinkCache(limit=2)
    cache.add(*decode_link(encode_link(second)))
    ordinary = report(second); ordinary['card'].pop('animation')
    cache.attach(ordinary)
    assert ordinary['card']['animation'] == second.animation
    partial = report(second, False); partial['card'].pop('animation')
    cache.attach(partial); assert 'animation' not in partial['card']
    cache.add(artwork_digest(second), {**second.animation, 'frame_ms': 600})
    ordinary['card'].pop('animation'); cache.attach(ordinary)
    assert 'animation' not in ordinary['card']


def test_data2g_keeps_normal_fragments_unchanged_and_accepts_sidecar():
    from backend_adapters import DATA2G_ADAPTER
    from card_transfer import CardAssemblyService, CardTransferReceiver
    _, second = cards()
    frames = DATA2G_ADAPTER.frame_card(second, 256, 'card')
    plain = copy.deepcopy(second); plain.animation = None
    assert frames[1:] == DATA2G_ADAPTER.frame_card(plain, 256, 'card')
    legacy = CardTransferReceiver()
    with pytest.raises(ValueError): legacy.feed('PIXELQSO', frames[0])
    for frame in frames[1:]:
        _, meta = legacy.feed('PIXELQSO', frame)
    assert meta['exact']
    service = CardAssemblyService()
    assert service.feed_transport_frame('PIXELQSO', frames[0])[1]['animation_metadata_only']
    for frame in frames[1:]:
        _, meta = service.feed_transport_frame('PIXELQSO', frame)
    pixels = meta.pop('pixels'); meta['palette'] = [list(c) for c in second.palette]
    assembled, _ = service.merge_preview({'card': meta, 'pixels': pixels})
    assert assembled['card']['exact']
    assert assembled['card']['animation'] == second.animation


def test_protected_audio_sidecar_waveform_and_copy_cache():
    import modem_envelope as envelope
    _, second = cards()
    audio = envelope.animation_audio('resilient_100', second)
    found = envelope.find_copies(audio, 48000)
    assert not found  # A sidecar must never announce a payload or inhibit native RX.
    assert found.animation_links == [(artwork_digest(second), second.animation)]
    cache = envelope.CopyCaptureCache()
    cache.update(audio, 48000, found)
    assert cache.animation_links.links[artwork_digest(second)] == second.animation
    native = report(second); native['card'].pop('animation')
    decoded = envelope.decode_capture('resilient_100', audio, 48000,
                                      lambda *_: copy.deepcopy(native), copies=found)
    assert decoded['card']['animation'] == second.animation
    cache.clear(); assert not cache.animation_links.links


def test_deck_groups_plays_and_click_details_contains_both_frames():
    from PySide6.QtWidgets import QApplication, QLabel
    from PySide6.QtGui import QImage, QColor
    from card_views import DeckDialog, AnimationDetailsDialog
    app = QApplication.instance() or QApplication([])
    first, second = map(report, cards())
    def image_for_card(received):
        image = QImage(32, 32, QImage.Format.Format_RGB32)
        image.fill(QColor('red' if received['card']['card_id'] == 100 else 'blue'))
        return image
    deck = DeckDialog({'received_cards': [second, first], 'peer_callsign': 'AG7SU'}, image_for_card)
    assert len(deck.events) == 1
    deck.show(); app.processEvents()
    assert len(deck.animation_frames) == 2 and deck.animation_timer.isActive()
    from unittest.mock import patch
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    with patch('card_views.time.monotonic', return_value=0):
        deck._advance_animation(); first_key = deck.art.original.cacheKey()
    with patch('card_views.time.monotonic', return_value=.5):
        deck._advance_animation(); second_key = deck.art.original.cacheKey()
    assert first_key != second_key
    deck.pause_animation_button.click(); assert not deck.animation_timer.isActive()
    deck.pause_animation_button.click(); assert deck.animation_timer.isActive()
    with patch.object(AnimationDetailsDialog, 'exec', return_value=0) as opened:
        QTest.mouseClick(deck.art, Qt.MouseButton.LeftButton)
        opened.assert_called_once()
    details = AnimationDetailsDialog(deck.events[0][1], image_for_card)
    labels = '\n'.join(label.text() for label in details.findChildren(QLabel))
    assert 'Frame 1' in labels and 'Frame 2' in labels and '0064' in labels and '0065' in labels
    assert '500 ms' in labels
    deck.close(); details.close()


def test_complete_local_waveform_survives_legacy_header_reader():
    from unittest.mock import patch
    import modem_envelope as envelope
    from card_backends import get_backend
    _, second = cards()
    backend = get_backend('resilient_100')
    waveform = backend.encode(second, 1, 'card', None)
    assert len(waveform)/48000 == pytest.approx(backend.estimate_seconds(second, 1), abs=.01)
    decoded = backend.decode(waveform, 48000)
    assert decoded['card']['exact']
    assert decoded['card']['animation'] == second.animation
    assert decoded['pixels'] == second.pixels
    new_decode = envelope.decode_header
    def legacy_header(*args, **kwargs):
        header = new_decode(*args, **kwargs)
        if header['version'] == envelope.ANIMATION_VERSION:
            raise ValueError('unsupported copy header')
        return header
    with patch.object(envelope, 'decode_header', legacy_header):
        legacy = backend.decode(waveform, 48000)
    assert legacy['card']['exact'] and legacy['pixels'] == second.pixels
    assert 'animation' not in legacy['card']


def test_wall_pairing_reverse_arrival_late_sidecar_and_saved_session(tmp_path, monkeypatch):
    import json
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    import app as pixelqso
    application = QApplication.instance() or QApplication([])
    monkeypatch.setenv('PIXELQSO_DATA_DIR', str(tmp_path))
    (tmp_path/'settings.ini').write_text('[radio]\nauto_connect_cat=false\n')
    original_data = pixelqso.DATA
    with patch('app.QTimer.singleShot'):
        window = pixelqso.MainWindow()
    try:
        first, second = map(report, cards())
        for frame in (first, second):
            frame['assembly_event'] = {'verified_complete': True}
        window._add_session_wall_card(second)
        assert not next(iter(window.session_wall_items.values()))[1]._animation_report
        window._add_session_wall_card(first)
        tiles = list(window.session_wall_items.values())
        assert sum(not item.isHidden() for item, _ in tiles) == 1
        combined = next(button._animation_report for _, button in tiles if button._animation_report)
        assert combined['animation_frames'][0]['card']['card_id'] == 100
        assert combined['animation_frames'][1]['card']['card_id'] == 101
        with patch.object(pixelqso.AnimationDetailsDialog, 'exec', return_value=0) as opened:
            next(button for _, button in tiles if button._animation_report).click()
            opened.assert_called_once()
        # Persist a first frame in a previous session; late sidecar upgrades the
        # second reception in both the current wall and its saved log.
        for item, button in tiles:
            window.session_wall.takeItem(window.session_wall.row(item)); button.deleteLater()
        window.session_wall_items.clear()
        window.qso_log_dir.mkdir(parents=True, exist_ok=True)
        (window.qso_log_dir/'historical.json').write_text(json.dumps({'received_cards': [first]}))
        ordinary = copy.deepcopy(second); ordinary['card'].pop('animation')
        (window.qso_log_dir/'new.json').write_text(json.dumps({'received_cards': [ordinary]}))
        window._add_session_wall_card(ordinary)
        button = next(iter(window.session_wall_items.values()))[1]
        assert button._animation_report is None
        window.card_assembly.animation_links.add(*decode_link(encode_link(cards()[1])))
        window._refresh_animation_references()
        assert button._animation_report['animation_frames'][0]['card']['card_id'] == 100
        saved = json.loads((window.qso_log_dir/'new.json').read_text())
        assert saved['received_cards'][0]['card']['animation'] == second['card']['animation']
        assert 'animation_frames' not in saved['received_cards'][0]
    finally:
        window.close(); application.processEvents(); pixelqso.DATA = original_data


def test_library_link_save_load_send_snapshot_and_unlink(tmp_path, monkeypatch):
    import json
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication, QListWidgetItem, QDialog
    from PySide6.QtCore import Qt
    import app as pixelqso
    application = QApplication.instance() or QApplication([])
    monkeypatch.setenv('PIXELQSO_DATA_DIR', str(tmp_path))
    (tmp_path/'settings.ini').write_text('[radio]\nauto_connect_cat=false\n')
    original_data = pixelqso.DATA
    with patch('app.QTimer.singleShot'):
        window = pixelqso.MainWindow()
    try:
        first, second = cards(); second.animation = None
        folder = tmp_path/'cards'; folder.mkdir(exist_ok=True)
        (folder/'first.json').write_text(json.dumps(asdict(first)))
        target = folder/'second.json'; target.write_text(json.dumps(asdict(second)))
        item = QListWidgetItem('second'); item.setData(Qt.ItemDataRole.UserRole, str(target))
        with patch.object(window, '_selected_library_item', return_value=(item, None)), \
             patch.object(QDialog, 'exec', return_value=QDialog.DialogCode.Accepted):
            window.link_library_animation()
        data = json.loads(target.read_text())
        assert data['animation']['first_card_id'] == 100
        window._load_card_path(target)
        assert window.card.animation == data['animation']
        window.card_combo.clear(); window.card_combo.addItem('second', str(target))
        for combo in window.stage_card_combos.values(): combo.clear()
        chosen = window._chosen_card(); assert chosen.animation == data['animation']
        window._remember_tx_card(chosen)
        assert window.tx_card.animation == data['animation']
        assert window._local_card_snapshot(chosen)['card']['animation'] == data['animation']
        window.save_card()
        assert json.loads(target.read_text())['animation'] == data['animation']
        with patch.object(window, '_selected_library_item', return_value=(item, None)):
            window.unlink_library_animation()
        assert 'animation' not in json.loads(target.read_text()) and window.card.animation is None
    finally:
        window.close(); application.processEvents(); pixelqso.DATA = original_data


@pytest.mark.parametrize('key,interlaced', [('experimental_qpsk_equalized', True),
                                         ('experimental_8fsk_grouped', False)])
def test_interlaced_and_grouped_audio_preserve_link_and_airtime(key, interlaced):
    from card_backends import get_backend
    _, second = cards(); backend = get_backend(key)
    kwargs = {'interlaced': True} if interlaced else {}
    audio = backend.encode(second, 2, 'card', None, **kwargs)
    assert len(audio)/48000 == pytest.approx(backend.estimate_seconds(second, 2), abs=.015)
    decoded = backend.decode(audio, 48000)
    assert decoded['card']['exact'] and decoded['pixels'] == second.pixels
    assert decoded['card']['animation'] == second.animation

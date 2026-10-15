"""Exchange/RR73 reports describe reception evidence without accepting partial art."""
import copy
import json
import os
from dataclasses import asdict
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
import cardmodem as modem
from card_feedback import (make_report, validate_report, report_lines, encode_report,
                           decode_report, data2g_binding, FeedbackCache)


def partial(stage='cq'):
    card = modem.Card('K1ABC', 'FN42', list(modem.MINIMAL_AVATAR_PALETTE), [0, 1]*512, 0x1234)
    return {'card': {**asdict(card), 'avatar_burst': True, 'header_version': 4,
                    'message_type': stage, 'pixel_coverage': [True]*384+[False]*640,
                    'verified_pixel_coverage': [True]*212+[False]*812,
                    'received_blocks': [True, False, True]+[False]*7,
                    'exact': False, 'whole_raster_valid': False, 'measured_snr_db': -13},
            'pixels': card.pixels, 'assembly_event': {'verified_complete': False},
            'receive_profile': 'resilient_100'}


def reply_card():
    card = modem.Card('AG7SU', 'CN85', list(modem.MINIMAL_AVATAR_PALETTE), [1, 0]*512, 0x4567)
    card.reception_report = make_report(partial())
    return card


def test_progress_counts_candidate_pixels_and_crc_blocks_separately():
    result = make_report(partial())
    assert result['pixels_received'] == 384 and result['pixels_total'] == 1024
    assert result['crc_blocks_passed'] == 2 and result['crc_blocks_total'] == 10
    assert not result['verified']
    text = '\n'.join(report_lines({'reception_report': result}))
    assert 'K1ABC card 1234' in text and '384/1024 pixels' in text
    assert '2/10 blocks passed CRC' in text and 'RETRY NEEDED' in text
    all_pixels = partial(); all_pixels['card']['pixel_coverage'] = [True]*1024
    assert not make_report(all_pixels)['verified']
    assert 'RETRY NEEDED' in '\n'.join(report_lines({'reception_report': make_report(all_pixels)}))


def test_no_blocks_is_unknown_and_missing_coverage_never_counts_placeholder_pixels():
    received = partial(); received['card'].pop('received_blocks'); received['card'].pop('pixel_coverage')
    result = make_report(received)
    assert result['pixels_received'] == 0
    assert result['crc_blocks_passed'] is None and result['crc_blocks_total'] is None
    assert 'unavailable' in '\n'.join(report_lines({'reception_report': result}))


@pytest.mark.parametrize('change', [{'pixels_received': 1025}, {'pixels_total': 0},
    {'crc_blocks_passed': 11}, {'verified': True}, {'callsign': None}, {'card_id': -1}])
def test_malformed_or_overclaimed_progress_is_rejected(change):
    with pytest.raises(ValueError): validate_report({**make_report(partial()), **change})


@pytest.mark.parametrize('stage', ['exchange', '73'])
def test_report_sidecar_crc_and_stage_binding(stage):
    expected = make_report(partial())
    frame = encode_report(expected, b'abcdefgh', stage)
    assert len(frame) == 40
    binding, decoded_stage, decoded = decode_report(frame)
    assert binding == b'abcdefgh'.hex() and decoded_stage == stage and decoded == expected
    bad = bytearray(frame); bad[25] ^= 1
    with pytest.raises(ValueError): decode_report(bad)
    with pytest.raises(ValueError): encode_report(expected, b'abcdefgh', 'cq')


@pytest.mark.parametrize('stage', ['exchange', '73'])
def test_data2g_report_attaches_before_reply_image_completes_and_counts_checked_fragments(stage):
    from backend_adapters import DATA2G_ADAPTER
    from card_transfer import CardAssemblyService
    card = reply_card(); frames = DATA2G_ADAPTER.frame_card(card, 128, stage, -7)
    ordinary = copy.deepcopy(card); ordinary.reception_report = None
    assert frames[1:] == DATA2G_ADAPTER.frame_card(ordinary, 128, stage, -7)
    service = CardAssemblyService()
    assert service.feed_transport_frame('PIXELQSO', frames[0])[1]['reception_metadata_only']
    _, metadata = service.feed_transport_frame('PIXELQSO', frames[1])
    pixels = metadata.pop('pixels'); metadata['palette'] = card.palette
    received, _ = service.merge_preview({'card': metadata, 'pixels': pixels})
    assert not received['card']['exact']
    assert received['card']['reception_report'] == card.reception_report
    assert received['card']['crc_blocks_passed'] == 1
    assert received['card']['crc_blocks_total'] is None
    assert received['card']['crc_block_kind'] == 'transport_fragment'
    service.feed_transport_frame('PIXELQSO', frames[1])
    _, duplicate = service.feed_transport_frame('PIXELQSO', frames[1])
    assert duplicate['crc_blocks_passed'] == 1
    report_back = make_report(received)
    assert report_back['block_kind'] == 'transport_fragment'
    assert report_back['crc_blocks_passed'] == 1 and report_back['crc_blocks_total'] is None
    assert not report_back['verified']
    for frame in frames[2:]:
        _, metadata = service.feed_transport_frame('PIXELQSO', frame)
    pixels = metadata.pop('pixels'); metadata['palette'] = card.palette
    complete, _ = service.merge_preview({'card': metadata, 'pixels': pixels})
    assert complete['card']['exact']
    assert complete['card']['reception_report'] == card.reception_report
    # Metadata about another image/stage/SNR cannot be applied to this card.
    cache = FeedbackCache(); cache.add(frames[0])
    for change in ({'card_id': 4}, {'message_type': 'cq'}, {'snr_db': -8}):
        wrong = copy.deepcopy(complete); wrong['card'].pop('reception_report'); wrong['card'].update(change)
        assert not cache.attach(wrong)
        assert 'reception_report' not in wrong['card']


@pytest.mark.parametrize('stage', ['exchange', '73'])
def test_local_audio_reports_roundtrip_with_legacy_static_image_fallback(stage):
    import modem_envelope as envelope
    from card_backends import get_backend
    card = reply_card(); backend = get_backend('resilient_100')
    audio = backend.encode(card, 1, stage, -7)
    assert len(audio)/48000 == pytest.approx(backend.estimate_seconds(card, 1), abs=.015)
    received = backend.decode(audio, 48000)
    assert received['card']['exact'] and received['pixels'] == card.pixels
    assert received['card']['reception_report'] == card.reception_report
    decode = envelope.decode_header
    def old_header(*args, **kwargs):
        header = decode(*args, **kwargs)
        if header['version'] in envelope.REPORT_VERSIONS: raise ValueError('unsupported copy header')
        return header
    with patch.object(envelope, 'decode_header', old_header):
        old = backend.decode(audio, 48000)
    assert old['card']['exact'] and old['pixels'] == card.pixels
    assert 'reception_report' not in old['card']


def test_split_metadata_retained_across_incremental_header_scans():
    import modem_envelope as envelope
    card = reply_card(); frame = encode_report(card.reception_report, b'abcdefgh', 'exchange')
    token = int.from_bytes(frame[-4:], 'big')
    cache = envelope.CopyCaptureCache()
    first = ('resilient_100', token, 0, frame[4:20].hex())
    second = ('resilient_100', token, 1, frame[20:36].hex())
    import numpy as np
    cache.update(np.zeros(10), 8000, envelope.AnnouncedCopies(feedback_parts=[first]))
    assert not cache.feedback.reports
    cache.update(np.zeros(10), 8000, envelope.AnnouncedCopies(feedback_parts=[second]))
    assert cache.feedback.reports[('6162636465666768', 'exchange')] == card.reception_report
    cache.clear(); assert not cache.feedback.reports and not cache.feedback.parts


@pytest.fixture
def window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    import app as pixelqso
    application = QApplication.instance() or QApplication([])
    monkeypatch.setenv('PIXELQSO_DATA_DIR', str(tmp_path))
    (tmp_path/'settings.ini').write_text('[radio]\nauto_connect_cat=false\n')
    original_data = pixelqso.DATA
    with patch('app.QTimer.singleShot'):
        result = pixelqso.MainWindow()
    result.station_call.setText('AG7SU')
    yield result
    result.auto_armed = False
    result.close(); application.processEvents(); pixelqso.DATA = original_data


@pytest.mark.parametrize('incoming,outgoing', [('cq', 'exchange'), ('card', 'exchange'), ('exchange', 'report73')])
def test_user_can_reply_to_provisional_without_automatic_acceptance(window, incoming, outgoing):
    received = partial(incoming)
    window._show_receive_preview(window.card_image(received), card=received['card'], exact=False,
        coverage=received['card']['pixel_coverage'], report=received)
    assert not window.reply_to_button.isHidden()
    assert 'provisional' in window.reply_to_button.text()
    with patch.object(window, 'transmit_exchange') as send, patch.object(window, 'start_auto_exchange') as automatic:
        window.reply_to_button.click()
        send.assert_called_once(); automatic.assert_not_called()
    assert window._outgoing_message_type() == outgoing
    assert not window.auto_armed and not received['card']['exact']
    outgoing_card = window._prepare_outgoing_card(reply_card(), outgoing)
    assert outgoing_card.reception_report == make_report(received)
    window._remember_tx_card(outgoing_card)
    assert window.tx_card.reception_report == make_report(received)
    assert window._local_card_snapshot(outgoing_card)['card']['reception_report'] == make_report(received)
    for ordinary in ('cq', 'final73', 'card'):
        assert window._prepare_outgoing_card(outgoing_card, ordinary).reception_report is None


def test_partial_progress_report_does_not_complete_automatic_contact(window):
    received = partial('73'); received['card'].update(exact=True, snr_db=-7)
    received['assembly_event']['verified_complete'] = True
    feedback = {**make_report(partial()), 'callsign': 'AG7SU', 'card_id': 0x4567}
    received['card']['reception_report'] = feedback
    window.auto_armed = True; window.auto_role = 'responder'; window.auto_peer = 'K1ABC'
    window.auto_own_card_id = 0x4567; window.contact_stage = 'await_report73'
    with patch.object(window, '_schedule_contact_transmit') as send, patch.object(window, '_complete_contact') as complete:
        window._handle_contact_message(received)
        send.assert_not_called(); complete.assert_not_called()
    assert window.contact_stage == 'await_report73'
    assert 'retry needed' in window.rx_status.text()


def test_provisional_off_peer_report_cannot_replace_automatic_reply_target(window):
    original = partial(); window.reply_report = original
    window.auto_armed = True; window.auto_peer = 'K1ABC'
    other = partial(); other['card']['callsign'] = 'K2OTHER'
    window._show_reply_action(other)
    assert window.reply_report is original
    window._show_reply_action(partial())
    assert window.reply_report is original


def test_late_data2g_feedback_updates_saved_receipt_without_new_attempt(window):
    from backend_adapters import DATA2G_ADAPTER
    card = reply_card(); frames = DATA2G_ADAPTER.frame_card(card, 128, 'exchange', -7)
    service = window.card_assembly
    for frame in frames[1:]: _, meta = service.feed_transport_frame('PIXELQSO', frame)
    pixels = meta.pop('pixels'); meta['palette'] = card.palette
    received, _ = service.merge_preview({'card': meta, 'pixels': pixels})
    received['receive_attempts'] = 2
    window._add_session_wall_card(received)
    window.qso_log_dir.mkdir(exist_ok=True, parents=True)
    target = window.qso_log_dir/'late-feedback.json'
    target.write_text(json.dumps({'received_cards': [received]}))
    service.feed_transport_frame('PIXELQSO', frames[0]); window._refresh_animation_references()
    saved = json.loads(target.read_text())['received_cards']
    assert len(saved) == 1 and saved[0]['receive_attempts'] == 2
    assert saved[0]['card']['reception_report'] == card.reception_report
    button = next(iter(window.session_wall_items.values()))[1]
    assert '384/1024 pixels' in button._back_text and 'RETRY NEEDED' in button._back_text


@pytest.mark.parametrize('stage', ['exchange', 'report73'])
def test_data2g_transmit_preparation_includes_report_for_both_reply_stages(window, stage):
    from types import SimpleNamespace
    from unittest.mock import Mock
    import app as pixelqso
    from backend_adapters import DATA2G_ADAPTER
    window.reply_report = partial('cq' if stage == 'exchange' else 'exchange')
    card = reply_card(); card.reception_report = None
    window.tx_snr_db = -13
    window.manual_stage_buttons[stage].setChecked(True)
    session = Mock(); window.data2g_session = session
    backend = SimpleNamespace(mode_name='test-mode', max_frame_bytes=128, usable=True,
                              mode=SimpleNamespace(seconds_at_max=5))
    with patch.object(DATA2G_ADAPTER, 'is_connected', return_value=True), \
         patch.object(DATA2G_ADAPTER, 'select_mode'), \
         patch.object(window, '_confirm_data2g_ptt_recovery', return_value=True), \
         patch.object(window, '_data2g_send_next'), \
         patch.object(window, 'refresh_tx_button'), \
         patch.object(pixelqso.QMessageBox, 'warning', side_effect=AssertionError('unexpected warning')):
        window._prepare_data2g_transmission(backend, message_type=stage, card_override=card)
        frame = next(frame for frame in window.data2g_tx_frames if frame.startswith(b'PQRP'))
        _, wire_stage, received = decode_report(frame)
        assert wire_stage == ('exchange' if stage == 'exchange' else '73')
        assert received == make_report(window.reply_report)
        assert window.tx_card.reception_report == received
    window.data2g_tx_total = 0; window.data2g_tx_frames = []; window.data2g_session = None


def test_checked_local_feedback_can_describe_an_incomplete_reply_image():
    import modem_envelope as envelope
    import numpy as np
    card = reply_card()
    identity = envelope.content_id('resilient_100', card, 'exchange', -7)
    frame = encode_report(card.reception_report, identity[:8], 'exchange')
    incomplete = partial('exchange')
    incomplete['card'].update(callsign=card.callsign, card_id=card.card_id, snr_db=-7)
    announcement = envelope.AnnouncedCopy('resilient_100', identity.hex(), 0, 800,
        {'payload_samples': 4800, 'carrier_offset_hz': 0, 'reception_reports': [frame.hex()]})
    decoded = envelope.decode_capture('resilient_100', np.zeros(800), 8000,
        lambda *_: copy.deepcopy(incomplete), copies=[announcement])
    assert not decoded['card']['exact']
    assert decoded['card']['reception_report'] == card.reception_report


def test_quick_draw_reply_uses_the_same_progress_report(window):
    received = partial('exchange'); window.reply_report = received
    with patch.object(window, 'reply_to_received') as reply:
        window.send_quick_draw_reply()
        reply.assert_called_once()
    frozen = window._prepare_outgoing_card(window.quick_reply_card, 'report73')
    assert frozen.reception_report == make_report(received)


def test_incomplete_rr73_report_does_not_offer_final_73(window):
    received = partial('73'); received['card']['reception_report'] = make_report(partial())
    received['card']['snr_db'] = -7
    window._show_reply_action(received)
    assert window._reply_stage(received) is None
    assert window.reply_to_button.isHidden()
    with patch.object(window, 'transmit_exchange') as send:
        window.reply_to_received(); send.assert_not_called()


@pytest.mark.parametrize('key,interlaced', [('experimental_qpsk_equalized', True),
                                         ('experimental_8fsk_grouped', False)])
def test_feedback_and_animation_coexist_with_grouped_or_interlaced_audio(key, interlaced):
    from card_backends import get_backend
    from card_animation import artwork_digest
    card = reply_card()
    first = copy.deepcopy(card); first.card_id = 0x4566; first.reception_report = None
    card.animation = dict(first_card_id=first.card_id, first_digest=artwork_digest(first), frame_ms=500)
    backend = get_backend(key)
    audio = backend.encode(card, 2, 'exchange', -7, **({'interlaced': True} if interlaced else {}))
    assert len(audio)/48000 == pytest.approx(backend.estimate_seconds(card, 2), abs=.015)
    decoded = backend.decode(audio, 48000)
    assert decoded['card']['exact'] and decoded['pixels'] == card.pixels
    assert decoded['card']['reception_report'] == card.reception_report
    assert decoded['card']['animation'] == card.animation


def test_rr73_progress_report_without_snr_is_still_a_report():
    from card_views import stage_label
    received = partial('73'); received['card']['snr_db'] = None
    received['card']['reception_report'] = make_report(partial())
    assert stage_label(received) == 'RR73 · REPORT'


def test_received_reply_verification_is_separate_from_its_partial_progress_report(window):
    from card_views import DeckDialog
    card = reply_card()
    received = {'card': {**asdict(card), 'exact': True, 'message_type': '73'}, 'pixels': card.pixels}
    deck = DeckDialog({'received_cards': [received], 'peer_callsign': card.callsign}, window.card_image)
    try:
        assert 'WHOLE CARD VERIFIED' in deck.card_back.text()
        assert 'RETRY NEEDED' in deck.card_back.text()
        assert '384/1024 pixels' in deck.card_back.text()
    finally:
        deck.close()


def test_report_for_another_card_cannot_advance_automatic_exchange(window):
    received = partial('exchange'); received['card']['exact'] = True
    received['assembly_event']['verified_complete'] = True
    feedback = make_report(partial())
    feedback.update(callsign='AG7SU', card_id=1, pixels_received=1024,
                    crc_blocks_passed=10, verified=True)
    received['card']['reception_report'] = feedback
    window.auto_armed = True; window.auto_role = 'caller'; window.auto_peer = 'K1ABC'
    window.auto_own_card_id = 0x4567; window.contact_stage = 'listen_cq'
    original = partial(); window.reply_report = original
    with patch.object(window, '_schedule_contact_transmit') as send:
        window._handle_contact_message(received)
        send.assert_not_called()
    assert window.contact_stage == 'listen_cq' and window.reply_report is original

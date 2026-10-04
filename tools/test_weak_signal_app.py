"""Headless Qt/UDP integration: UI encoder, live worker, image and saved card.

Uses isolated station data and the application's normal test-link timers.
No CAT/PTT or sound device is used. The noise padding is channel receive time.
"""
import json
import argparse
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modem_benchmark import channel
from synthetic_channel import ReceiveFilter
import cardmodem as pixel
import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--combined', action='store_true', help='Exercise four-copy reception at -9 dB')
    parser.add_argument('--narrow', choices=('100', '50', '25'), help='Exercise a narrow resilient mode')
    parser.add_argument('--placement', choices=('near_carrier', 'centered', 'custom'), default='near_carrier')
    parser.add_argument('--low-hz', type=int, default=300)
    parser.add_argument('--single-mode', action='store_true', help='Receive only the selected narrow mode and placement')
    parser.add_argument('--telemetry', action='store_true', help='Verify on-air recording hooks using synthetic UDP audio')
    args = parser.parse_args()
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    with tempfile.TemporaryDirectory(prefix='pixelqso-weak-app-') as directory:
        os.environ.update(PIXELQSO_DATA_DIR=directory, PIXELQSO_AUDIO_TEST='1', PIXELQSO_TEST_LINK='1')
        sockets = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
        for sock in sockets:
            sock.bind(('127.0.0.1',0))
        ports = [sock.getsockname()[1] for sock in sockets]
        for sock in sockets:
            sock.close()
        from PySide6.QtWidgets import QApplication
        import app
        errors = []
        sys.excepthook = lambda kind, value, trace: (errors.append(str(value)),
                                                    traceback.print_exception(kind, value, trace))
        qt = QApplication([])
        collector = None
        if args.telemetry:
            from on_air import SessionCollector
            from tools.on_air_test import token_file
            credential = Path(directory) / 'session.token'
            collector = SessionCollector(Path(directory) / 'collector', token_file(credential))
        windows = []
        for index in range(2):
            os.environ.update(PIXELQSO_TEST_RX_PORT=str(ports[index]),
                              PIXELQSO_TEST_TX_PORT=str(ports[1-index]))
            if args.telemetry:
                config_path = Path(directory) / f'client-{index}.json'
                config_path.write_text(json.dumps({'session_id': 'synthetic-integration', 'station_id': f'client-{index}',
                    'output_dir': str(Path(directory) / 'captures'), 'source': 'synthetic-software-link',
                    'collector_url': f'http://127.0.0.1:{collector.port}', 'token_file': str(credential)}))
                os.environ['PIXELQSO_ON_AIR_CONFIG'] = str(config_path)
            else:
                os.environ.pop('PIXELQSO_ON_AIR_CONFIG', None)
            window = app.MainWindow()
            window.show_experimental_modes.setChecked(True)
            window._refresh_mode_options()
            window.mode_combo.setCurrentIndex(window.mode_combo.findData('experimental_qpsk_5s'))
            assert window.repeat_count.maximum() == 1 and window.repeat_count.value() == 1
            window.mode_combo.setCurrentIndex(window.mode_combo.findData('fast_avatar_fec'))
            assert window.repeat_count.maximum() == 20 and window.repeat_count.value() == 3
            window.mode_combo.setCurrentIndex(window.mode_combo.findData('experimental_qpsk_5s'))
            windows.append(window)
        tx, rx = windows
        if args.narrow:
            tx.mode_combo.setCurrentIndex(tx.mode_combo.findData(f'resilient_{args.narrow}'))
            tx.repeat_count.setValue(1)
            tx.audio_placement_combo.setCurrentIndex(tx.audio_placement_combo.findData(args.placement))
            tx.audio_low_hz.setValue(args.low_hz)
            assert tx.audio_placement_combo.isEnabled()
            assert tx.audio_low_hz.isEnabled() == (args.placement == 'custom')
            assert tx.settings.value('transfer/audio_placement', 'near_carrier') == args.placement
            assert int(tx.settings.value('transfer/audio_low_hz', 300)) == args.low_hz
            rx.audio_placement_combo.setCurrentIndex(rx.audio_placement_combo.findData('near_carrier'))
            rx.audio_low_hz.setValue(args.low_hz)
            if args.placement == 'custom':
                rx.audio_placement_combo.setCurrentIndex(rx.audio_placement_combo.findData('custom'))
            expected_low = args.low_hz if args.placement == 'custom' else 1500-3.5*int(args.narrow) if args.placement == 'centered' else int(args.narrow)
            assert f'{expected_low:g}–' in tx.transfer_summary.text()
        if args.combined:
            tx.mode_combo.setCurrentIndex(tx.mode_combo.findData('experimental_qpsk_combined'))
            tx.repeat_count.setValue(4)
            assert tx.repeat_count.maximum() == 8
        # Receive must not depend on the station's selected TX format.
        rx.mode_combo.setCurrentIndex(rx.mode_combo.findData('fast_avatar_fec'))
        rx.receive_all_modes.setChecked(True)
        if args.single_mode:
            assert args.narrow, '--single-mode requires --narrow'
            rx.mode_combo.setCurrentIndex(rx.mode_combo.findData(f'resilient_{args.narrow}'))
            rx.receive_all_modes.setChecked(False)
            rx.audio_placement_combo.setCurrentIndex(rx.audio_placement_combo.findData(args.placement))
        card = pixel.example_card()
        tx.tx_card = card
        clean = tx._card_tx_audio(card)
        from card_backends import get_backend
        assert len(clean)/48000 == get_backend(tx.selected_mode(), tx.selected_audio_placement()).estimate_seconds(card, tx.repeat_count.value())
        rx_filter = ReceiveFilter(48000, low_hz=0) if args.narrow else None
        received, measures = channel(clean, {}, 6 if args.narrow else -9 if args.combined else -3, 801,
                                     rx_filter=rx_filter)
        tx.tx_bytes = (np.clip(received,-1,1)*32767).astype('<i2').tobytes()
        tx.tx_offset = 0
        tx.tx_label = 'weak signal integration test'
        tx.tx_message_type = 'card'
        tx.tx_deadline = len(received)/48000 + 10
        rx.start_receive()
        tx._start_tx_audio()
        deadline = time.monotonic()+len(received)/48000+40
        while time.monotonic() < deadline:
            qt.processEvents()
            if rx.seen_received_cards and not rx.live_decode_inflight and rx.decode_jobs == 0:
                break
            time.sleep(.005)
        assert rx.seen_received_cards, rx.rx_status.text()
        assert not errors, errors
        assert not rx.receive_view.pixmap().isNull(), 'Live receive image missing'
        reports = list(Path(directory).rglob('*.json'))
        found = []
        for path in reports:
            data = json.loads(path.read_text())
            if isinstance(data,dict) and 'received_cards' in data:
                found.extend(data['received_cards'])
        expected = pixel.unpack_indices(pixel.minimal_avatar_payload(card),1024)
        assert any(report.get('card',{}).get('exact') and report.get('pixels') == expected
                   and report['card'].get('callsign') == card.callsign
                   and report['card'].get('grid') == card.grid
                   and report['card'].get('card_id') == card.card_id
                   for report in found), f'No exact saved card in {reports}'
        if args.combined:
            assert any(report.get('card', {}).get('diagnostics', {}).get('combined_copies', 0) > 1
                       for report in found), 'Image was not recovered by soft combining'
        if args.narrow:
            # Exercise the independent file-decode worker with the same RX
            # placement, including auto reception of a different TX preset.
            path = Path(directory) / 'placement.wav'
            pixel.write_wav(path, clean)
            worker = app.DecodeWorker(path, Path(directory) / 'placement-state.json',
                                      mode=rx.receive_mode(), audio_placement=rx.selected_audio_placement())
            results = []
            worker.signals.finished.connect(results.append)
            worker.run()
            assert results and results[0][2] is None, results
            assert results[0][1]['card']['exact'] and results[0][1]['pixels'] == expected
        print(json.dumps(dict(result='PASS', snr_db_reference=measures['measured_snr_db_reference'],
                              waveform_seconds=len(clean)/48000, saved_exact_cards=len(found),
                              receive_status=rx.rx_status.text()), indent=2))
        for window in windows:
            window.tx_timer.stop()
            window.rx_timer.stop()
            window.decode_pool.waitForDone()
            window.close()
        qt.processEvents()
        assert not errors, errors
        if args.telemetry:
            from tools.on_air_test import session_report
            rows, matches = session_report(Path(directory) / 'captures', Path(directory) / 'review')
            kinds = {event['kind'] for event in rows}
            assert {'tx_start', 'tx_end', 'rx_audio', 'rx_decode', 'rx_verified_capture'} <= kinds, kinds
            assert matches, 'No whole-card reference match in telemetry'
            assert all(event['source'] == 'synthetic-software-link' for event in rows)
            assert list((Path(directory) / 'captures').rglob('*.wav'))
            assert all(window.on_air.errors == 0 and window.on_air.network_errors == 0 for window in windows)
            collector.close()
            print(json.dumps({'telemetry': 'PASS', 'events': len(rows), 'reference_matches': len(matches)}))


if __name__ == '__main__':
    main()

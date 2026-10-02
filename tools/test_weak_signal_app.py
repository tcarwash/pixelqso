"""Headless Qt/UDP integration: UI encoder, live worker, image and saved card.

Uses isolated station data and the application's normal test-link timers.
No CAT/PTT or sound device is used. The noise padding is channel receive time.
"""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modem_benchmark import channel
import cardmodem as pixel
import numpy as np


def main():
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
        sys.excepthook = lambda kind, value, trace: errors.append(str(value))
        qt = QApplication([])
        windows = []
        for index in range(2):
            os.environ.update(PIXELQSO_TEST_RX_PORT=str(ports[index]),
                              PIXELQSO_TEST_TX_PORT=str(ports[1-index]))
            window = app.MainWindow()
            window.mode_combo.setCurrentIndex(window.mode_combo.findData('experimental_qpsk_5s'))
            assert window.repeat_count.maximum() == 1 and window.repeat_count.value() == 1
            window.mode_combo.setCurrentIndex(window.mode_combo.findData('fast_avatar_fec'))
            assert window.repeat_count.maximum() == 20 and window.repeat_count.value() == 3
            window.mode_combo.setCurrentIndex(window.mode_combo.findData('experimental_qpsk_5s'))
            windows.append(window)
        tx, rx = windows
        card = pixel.example_card()
        clean = tx._card_tx_audio(card)
        assert len(clean)/48000 == 4.622
        received, measures = channel(clean, {}, -3, 801)
        tx.tx_bytes = (np.clip(received,-1,1)*32767).astype('<i2').tobytes()
        tx.tx_offset = 0
        tx.tx_label = 'weak signal integration test'
        tx.tx_message_type = 'card'
        tx.tx_deadline = 15
        rx.start_receive()
        tx._start_tx_audio()
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            qt.processEvents()
            if rx.seen_received_cards and not rx.live_decode_inflight and rx.decode_jobs == 0 and not rx.rx_timer.isActive():
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


if __name__ == '__main__':
    main()

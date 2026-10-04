"""Headless Qt exercise of Data2G host mode discovery, TX queue, and RX assembly."""
import os
from pathlib import Path
import tempfile
import sys
import json
import threading
import time
from urllib.request import Request, urlopen
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_directory = tempfile.TemporaryDirectory(prefix="pixelqso-data2g-app-")
os.environ["PIXELQSO_DATA_DIR"] = _directory.name
os.environ["PIXELQSO_AUDIO_TEST"] = "1"
os.environ.pop("PIXELQSO_TEST_LINK", None)

from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
import app
import cardmodem as modem
from card_transfer import fragment
from data2g_transport import Data2GMode
from webserver import CompanionServer


class FakeSession:
    connected = True
    port = 7

    def __init__(self):
        self.modes = []
        self.frames = []

    def set_mode(self, mode):
        self.modes.append(mode)

    def send_frame(self, tag, payload):
        self.frames.append((tag, payload))

    def close(self, timeout=5.0):
        self.connected = False
        return True


class FakeRecorder:
    def __init__(self):
        self.events = []

    def emit(self, kind, details=None, *_args):
        self.events.append((kind, details or {}))


class FakeProcess:
    pid = 12345

    def __init__(self):
        self.stopped = False

    def poll(self):
        return None if not self.stopped else 0

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


def finish_host_tx(window):
    sent = []
    while window.data2g_tx_waiting is not None:
        tag, payload = window.data2g_session.frames[-1]
        sent.append(payload)
        window._data2g_tx_ack(window.data2g_session.port, tag)
    return sent


def feed_remote_card(window, callsign, stage, card_id, snr=None):
    card = modem.example_card()
    card.callsign, card.grid, card.card_id = callsign, "CN85", card_id
    packed = modem.minimal_avatar_payload(card)
    chunk_size = 256 - app.CARD_TRANSFER_HEADER.size
    for offset in range(0, len(packed), chunk_size):
        window._data2g_rx_frame(window.data2g_session.port,
                                 fragment(card, packed, offset,
                                         packed[offset:offset + chunk_size], stage, snr))


def phone_post(qt, base, path, body):
    result = {}
    def post():
        try:
            request = Request(base + "/api/" + path,
                              data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                result["body"] = json.load(response)
        except Exception as exc:
            result["error"] = exc
    worker = threading.Thread(target=post, daemon=True)
    worker.start()
    deadline = time.monotonic() + 6
    while worker.is_alive() and time.monotonic() < deadline:
        qt.processEvents()
        time.sleep(.01)
    worker.join(timeout=.1)
    if worker.is_alive():
        raise TimeoutError(f"mobile /api/{path} did not complete")
    if "error" in result:
        raise result["error"]
    return result["body"]


def main():
    qt = QApplication.instance() or QApplication([])
    tx, rx = app.MainWindow(), app.MainWindow()
    try:
        assert not app.QIcon(str(app.APP_ICON_PATH)).isNull(), "app icon should load from icon.png"
        assert tx.auto_connect_cat.isChecked()
        assert not tx._should_auto_connect_rig(), "audio test mode must never auto-connect CAT"
        # Local hosting must launch Data2G's supported server CLI as a child,
        # with the station's audio, CAT, callsign, and ports; it must not call
        # Data2G modem internals in the PixelQSO process.
        tx.data2g_host_source.setCurrentIndex(tx.data2g_host_source.findData("local"))
        tx.data2g_command_port.setValue(18300)
        tx.data2g_kiss_port.setValue(18100)
        fake_process = FakeProcess()
        with patch("importlib.util.find_spec", return_value=object()), \
                patch("app.subprocess.Popen", return_value=fake_process) as popen, \
                patch.object(app.QTimer, "singleShot", lambda *_args: None):
            tx._connect_data2g_host()
        host_argv = popen.call_args.args[0]
        assert host_argv[:3] == [sys.executable, "-m", "data2g.host"]
        assert host_argv[host_argv.index("--command-port") + 1] == "18300"
        assert host_argv[host_argv.index("--kiss-port") + 1] == "18100"
        assert host_argv[host_argv.index("--mycall") + 1] == tx.station_call.text().upper()
        assert ("--input-device" in host_argv) == bool(tx.input_device.count())
        assert ("--output-device" in host_argv) == bool(tx.output_device.count())
        if tx.input_device.count():
            input_index = host_argv.index("--input-device") + 1
            assert host_argv[input_index] == app.data2g_audio_device_selector(
                tx.input_device.currentData().id(), tx.input_device.currentText())
        if tx.output_device.count():
            output_index = host_argv.index("--output-device") + 1
            assert host_argv[output_index] == app.data2g_audio_device_selector(
                tx.output_device.currentData().id(), tx.output_device.currentText())
        tx._stop_local_data2g_host()
        tx.data2g_host_source.setCurrentIndex(tx.data2g_host_source.findData("remote"))

        # Do not open a remote host session until CAT has fully left the socket.
        handoff_callbacks = []
        with patch.object(app.QTcpSocket, "state",
                          return_value=app.QTcpSocket.SocketState.ConnectedState), \
                patch.object(tx.rig, "disconnect") as disconnect_cat, \
                patch.object(app.QTimer, "singleShot",
                             side_effect=lambda _ms, callback: handoff_callbacks.append(callback)), \
                patch.object(tx.decode_pool, "start") as start_worker:
            tx._connect_data2g_host()
            assert disconnect_cat.called and tx.data2g_handoff_pending
            assert not tx.data2g_connecting and not start_worker.called
        with patch.object(app.QTcpSocket, "state",
                          return_value=app.QTcpSocket.SocketState.UnconnectedState), \
                patch.object(app.QTimer, "singleShot", lambda *_args: None), \
                patch.object(tx.decode_pool, "start") as start_worker:
            handoff_callbacks.pop(0)()
            assert tx.data2g_connecting and start_worker.called
            tx._data2g_connect_finished(None, "test cleanup")

        host_modes = [
            Data2GMode("qpsk-r1/2", 1200.0, 116, 64, 1.78, 74.36),
            Data2GMode("16qam-r1/2", 1200.0, 236, 64, 1.78, 74.36),
        ]
        os.environ["PIXELQSO_DATA2G_MODE"] = "fast"
        tx_recorder, rx_recorder = FakeRecorder(), FakeRecorder()
        tx.on_air, rx.on_air = tx_recorder, rx_recorder
        for window in (tx, rx):
            window.data2g_session = FakeSession()
            window._data2g_connect_finished(host_modes, None)
            assert window.mode_combo.findData("data2g_1200_robust") >= 0
            assert window.selected_mode() == "data2g_1200_fast"
            assert window.data2g_session.modes[-1] == "16qam-r1/2"
            assert not window.rx_timer.isActive(), "Data2G mode must not open the local PCM receiver"
            assert not window._should_auto_connect_rig(), "Data2G mode owns radio control, not PixelQSO CAT"

        tx.audio_test_mode.setChecked(False)
        tx.tx_card = modem.example_card()
        backend = app.BACKENDS["data2g_1200_fast"]
        tx._prepare_data2g_transmission(backend)
        assert tx.data2g_tx_total > 0 and len(tx.data2g_session.frames) == 1
        while tx.data2g_tx_waiting is not None:
            tag, payload = tx.data2g_session.frames[-1]
            tx._data2g_tx_ack(tx.data2g_session.port, tag)
            if tx.data2g_tx_waiting is not None:
                assert len(tx.data2g_session.frames) <= tx.data2g_tx_total
        assert "remote card receipt unconfirmed" in tx.rx_status.text()
        assert any(kind == "host_connected" for kind, _ in tx_recorder.events)
        assert any(kind == "host_tx_frame_queued" for kind, _ in tx_recorder.events)
        assert any(kind == "host_tx_frame_ack" for kind, _ in tx_recorder.events)
        manual_tx_frames = len(tx.data2g_session.frames)

        # Exercise the actual HTTP companion API through Qt dispatch and the
        # Data2G host path, rather than only invoking transmit_exchange directly.
        phone = CompanionServer(tx._web_dispatch, port=0)
        try:
            mobile_card = modem.example_card()
            mobile_card.callsign, mobile_card.grid = "AG7SU", "CN85"
            assert phone_post(qt, f"http://127.0.0.1:{phone.port}", "quickdraw", {
                "callsign": mobile_card.callsign, "grid": mobile_card.grid,
                "width": mobile_card.width, "height": mobile_card.height,
                "palette": [list(color) for color in mobile_card.palette],
                "pixels": mobile_card.pixels,
            })["loaded"]
            assert phone_post(qt, f"http://127.0.0.1:{phone.port}", "stage",
                              {"stage": "cq"})["stage"] == "cq"
            assert phone_post(qt, f"http://127.0.0.1:{phone.port}", "transmit", {})["accepted"]
            QTest.qWait(50)
            assert tx.data2g_tx_waiting is not None
            phone_tx_frames = finish_host_tx(tx)
            assert len(tx.data2g_session.frames) > manual_tx_frames
            assert phone_tx_frames
        finally:
            phone.close()

        # Feed one complete application transfer from a new remote station.
        remote = modem.example_card()
        remote.callsign, remote.grid, remote.card_id = "K1ABC", "FN31", 902
        packed = modem.minimal_avatar_payload(remote)
        chunk_size = 256 - app.CARD_TRANSFER_HEADER.size
        for offset in range(0, len(packed), chunk_size):
            frame = app.card_fragment(remote, packed, offset,
                                      packed[offset:offset + chunk_size], "cq")
            rx._data2g_rx_frame(rx.data2g_session.port, frame)
        qt.processEvents()
        assert "CRC passed" in rx.rx_status.text(), rx.rx_status.text()
        assert rx.receive_progress.value() == 100
        assert rx.reply_report and rx.reply_report["card"]["callsign"] == "K1ABC"
        assert any(kind == "host_rx_frame" for kind, _ in rx_recorder.events)
        assert any(kind == "host_rx_card" and details["exact"]
                   for kind, details in rx_recorder.events)

        # Exercise the complete automatic stage sequence through host frames.
        tx.station_call.setText("AG7SU")
        tx.station_grid.setText("CN85")
        tx.call.setText("AG7SU")
        tx.grid.setText("CN85")
        rx.station_call.setText("K1ABC")
        rx.station_grid.setText("FN31")
        rx.call.setText("K1ABC")
        rx.grid.setText("FN31")
        tx.exchange_mode.setCurrentIndex(1)
        rx.exchange_mode.setCurrentIndex(1)
        tx.exchange_role.setCurrentIndex(0)
        rx.exchange_role.setCurrentIndex(1)
        rx.start_auto_exchange()
        assert rx.auto_armed and rx.auto_wait_state == "initial_card"
        tx.start_auto_exchange()
        assert tx.auto_armed and tx.contact_stage == "send_cq"

        cq_frames = finish_host_tx(tx)
        assert tx.contact_stage == "listen_cq" and tx.auto_wait_state == "peer_response"
        for frame in cq_frames:
            rx._data2g_rx_frame(rx.data2g_session.port, frame)
        QTest.qWait(700)
        assert rx.contact_stage == "send_exchange" and rx.data2g_tx_waiting is not None
        exchange_frames = finish_host_tx(rx)
        assert rx.contact_stage == "await_report73"
        for frame in exchange_frames:
            tx._data2g_rx_frame(tx.data2g_session.port, frame)
        QTest.qWait(700)
        assert tx.contact_stage == "send_report73" and tx.data2g_tx_waiting is not None
        report_frames = finish_host_tx(tx)
        assert tx.contact_stage == "await_final73"
        for frame in report_frames:
            rx._data2g_rx_frame(rx.data2g_session.port, frame)
        QTest.qWait(700)
        assert rx.contact_stage == "send_final73" and rx.data2g_tx_waiting is not None
        final_frames = finish_host_tx(rx)
        assert rx.auto_armed and rx.contact_stage == "idle"
        for frame in final_frames:
            tx._data2g_rx_frame(tx.data2g_session.port, frame)
        assert tx.auto_armed and tx.contact_stage == "listen_cq"
        assert rx.active_qso_log is None and tx.active_qso_log is None

        # Stopping automation clears unsent fragments and leaves the in-flight
        # ACK status unknown without replaying or submitting another frame.
        tx.contact_stage = "send_cq"
        sent_before_cancel = len(tx.data2g_session.frames)
        tx._prepare_data2g_transmission(backend)
        assert tx.data2g_tx_waiting is not None and tx.data2g_tx_frames
        tx.stop_auto_exchange("Test stopped automatic exchange.")
        assert tx.data2g_tx_waiting is None and not tx.data2g_tx_frames
        assert tx.data2g_tx_total == 0
        assert len(tx.data2g_session.frames) == sent_before_cancel + 1
        assert "status is unknown" in tx.rx_status.text()
        print("DATA2G_QT_APP_PASS", {
            "manual_tx_frames": manual_tx_frames,
            "mode": backend.mode_name,
            "rx_card": rx.reply_report["card"]["callsign"],
            "preview_progress": rx.receive_progress.value(),
            "auto_exchange": "CQ/exchange/73/final73",
            "host_cancel": "in-flight unknown; remaining fragments not queued",
        })
    finally:
        for window in (tx, rx):
            window.on_air = None
            window.rx_timer.stop()
            window.tx_timer.stop()
            window.close()
        qt.processEvents()
        _directory.cleanup()


if __name__ == "__main__":
    main()

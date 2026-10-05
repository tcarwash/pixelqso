import socket
import socketserver
import threading
import time
import unittest
from unittest.mock import patch

from data2g_transport import (Data2GError, Data2GSession, Data2GTransport, KissDecoder,
                              encode_kiss, parse_modes)


class _CommandHandler(socketserver.BaseRequestHandler):
    commands = []
    modes_terminal_ok = True
    refused_modes = set()

    def handle(self):
        buffer = bytearray()
        while chunk := self.request.recv(1024):
            buffer.extend(chunk)
            while b"\r" in buffer:
                raw, _, tail = buffer.partition(b"\r")
                buffer[:] = tail
                command = raw.decode()
                self.commands.append(command)
                if command == "VERSION":
                    response = b"VERSION Data2G 0.1\r"
                elif command == "MODES":
                    response = (b"MODE qpsk-r1/5 500 20 4 1.0 4.0\rMODE qpsk-r1/2 1200 80 8 2.0 16.0\r" +
                                (b"OK\r" if self.modes_terminal_ok else b""))
                elif command in {"BCAST OPEN PIXELQSO", "BCAST OPEN PIXELQSO FROM AG7SU"}:
                    response = b"BCAST PORT 7\r"
                elif (command.startswith("BCAST MODE ") and
                      (command.endswith(" refused-mode") or
                       command.rsplit(" ", 1)[-1] in self.refused_modes)):
                    response = b"WRONG\r"
                elif command.startswith(("BCAST MODE ", "BCAST CLOSE ")):
                    response = (b"OK\rBCAST 7 HEARD AG7SU\rBCAST 7 LOST 2\r"
                                b"BCAST * MISSED n10-qpsk-r1/2 3\rBCAST 7 DROPPED 1\r"
                                if command.startswith("BCAST MODE ") else b"OK\r")
                else:
                    response = b"WRONG\r"
                self.request.sendall(response)


class _KissHandler(socketserver.BaseRequestHandler):
    delay_seconds = 0
    reply_payload = b"image-fragment"

    def handle(self):
        parser = KissDecoder()
        while chunk := self.request.recv(1024):
            for port, command, payload in parser.feed(chunk):
                if command == 0x0C:
                    if self.delay_seconds:
                        time.sleep(self.delay_seconds)
                    self.request.sendall(encode_kiss(port, 0x0C, payload[:2]))
                    self.request.sendall(encode_kiss(port, 0x00, self.reply_payload))


class Data2GTransportTests(unittest.TestCase):
    def test_kiss_escape_and_incremental_frame_parsing(self):
        wire = encode_kiss(7, 0, bytes((0, 0xC0, 0xDB, 0xFF)))
        parser = KissDecoder()
        self.assertEqual(parser.feed(wire[:3]), [])
        self.assertEqual(parser.feed(wire[3:]), [(7, 0, bytes((0, 0xC0, 0xDB, 0xFF)))])

    def test_modes_response_parsing(self):
        modes = parse_modes(["NOTICE test", "MODE qpsk-r1/2 1200 80 8 2.0 16.0"])
        self.assertEqual(modes[0].name, "qpsk-r1/2")
        self.assertEqual(modes[0].bandwidth_hz, 1200)
        with self.assertRaises(Data2GError):
            parse_modes(["MODE malformed"])

    def test_python_host_modes_catalog_without_ok_terminator(self):
        command_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CommandHandler)
        kiss_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _KissHandler)
        command_server.daemon_threads = kiss_server.daemon_threads = True
        threads = [threading.Thread(target=s.serve_forever, daemon=True)
                   for s in (command_server, kiss_server)]
        for thread in threads:
            thread.start()
        _CommandHandler.modes_terminal_ok = False
        client = Data2GTransport("127.0.0.1", command_server.server_address[1],
                                 kiss_server.server_address[1], timeout=1)
        try:
            modes = client.connect()
            self.assertEqual([mode.name for mode in modes], ["qpsk-r1/5", "qpsk-r1/2"])
        finally:
            _CommandHandler.modes_terminal_ok = True
            client.close()
            for server in (command_server, kiss_server):
                server.shutdown()
                server.server_close()

    def test_supported_host_command_and_kiss_round_trip(self):
        command_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CommandHandler)
        kiss_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _KissHandler)
        command_server.daemon_threads = kiss_server.daemon_threads = True
        threads = [threading.Thread(target=s.serve_forever, daemon=True)
                   for s in (command_server, kiss_server)]
        for thread in threads:
            thread.start()
        acks, frames, statuses = [], [], []
        client = Data2GTransport("127.0.0.1", command_server.server_address[1],
            kiss_server.server_address[1], timeout=2, on_ack=lambda p, tag: acks.append((p, tag)),
            on_frame=lambda p, frame: frames.append((p, frame)),
            on_status=statuses.append)
        try:
            modes = client.connect()
            self.assertEqual([mode.name for mode in modes], ["qpsk-r1/5", "qpsk-r1/2"])
            port = client.open_group("PIXELQSO", "AG7SU")
            self.assertEqual(port, 7)
            client.set_mode(port, "qpsk-r1/5")
            client.send_frame(port, b"\x12\x34", b"frame")
            deadline = time.monotonic() + 2
            expected_statuses = {"BCAST 7 HEARD AG7SU", "BCAST 7 LOST 2",
                                 "BCAST * MISSED n10-qpsk-r1/2 3", "BCAST 7 DROPPED 1"}
            while (not acks or not frames or not expected_statuses <= set(statuses)) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(acks, [(7, b"\x12\x34")])
            self.assertEqual(frames, [(7, b"image-fragment")])
            self.assertIn("BCAST 7 HEARD AG7SU", statuses)
            self.assertIn("BCAST 7 LOST 2", statuses)
            self.assertIn("BCAST * MISSED n10-qpsk-r1/2 3", statuses)
            self.assertIn("BCAST 7 DROPPED 1", statuses)
            client.close_group(port)
        finally:
            client.close()
            for server in (command_server, kiss_server):
                server.shutdown()
                server.server_close()

    def test_reader_sockets_remain_alive_after_connect_timeout(self):
        command_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CommandHandler)
        kiss_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _KissHandler)
        command_server.daemon_threads = kiss_server.daemon_threads = True
        threads = [threading.Thread(target=s.serve_forever, daemon=True)
                   for s in (command_server, kiss_server)]
        for thread in threads:
            thread.start()
        acks = []
        _KissHandler.delay_seconds = 0.2
        client = Data2GTransport("127.0.0.1", command_server.server_address[1],
            kiss_server.server_address[1], timeout=0.05,
            on_ack=lambda p, tag: acks.append((p, tag)))
        try:
            client.connect()
            time.sleep(0.15)  # longer than the TCP connect timeout
            self.assertTrue(client.connected)
            port = client.open_group("PIXELQSO", "AG7SU")
            client.send_frame(port, b"\x00\x07", b"frame")
            deadline = time.monotonic() + 2
            while not acks and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(acks, [(7, b"\x00\x07")])
        finally:
            _KissHandler.delay_seconds = 0
            client.close()
            for server in (command_server, kiss_server):
                server.shutdown()
                server.server_close()

    def test_managed_host_connection_retries_startup_refusal(self):
        command_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CommandHandler)
        kiss_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _KissHandler)
        command_server.daemon_threads = kiss_server.daemon_threads = True
        threads = [threading.Thread(target=s.serve_forever, daemon=True)
                   for s in (command_server, kiss_server)]
        for thread in threads:
            thread.start()
        real_connect = socket.create_connection
        attempts = []

        def late_listeners(address, timeout=None, source_address=None, *, all_errors=False):
            attempts.append(address)
            if len(attempts) <= 3:
                raise ConnectionRefusedError(111, "Connection refused")
            return real_connect(address, timeout, source_address, all_errors=all_errors)

        client = Data2GTransport(
            "127.0.0.1", command_server.server_address[1],
            kiss_server.server_address[1], timeout=1)
        try:
            with patch("data2g_transport.socket.create_connection", side_effect=late_listeners):
                modes = client.connect(retry_window=.5)
            self.assertTrue(modes)
            self.assertEqual(attempts[:4], [
                ("127.0.0.1", command_server.server_address[1]),
                ("127.0.0.1", command_server.server_address[1]),
                ("127.0.0.1", command_server.server_address[1]),
                ("127.0.0.1", command_server.server_address[1]),
            ])
        finally:
            client.close()
            for server in (command_server, kiss_server):
                server.shutdown()
                server.server_close()

    def test_background_command_queue_is_bounded(self):
        session = Data2GSession()
        session._ready.set()
        try:
            for index in range(64):
                session.send_frame(index.to_bytes(2, "big"), b"frame")
            with self.assertRaisesRegex(Data2GError, "queue is full"):
                session.send_frame(b"\xff\xff", b"overflow")
        finally:
            session._ready.clear()

    def test_kiss_socket_disconnect_is_reported_and_closes_the_session(self):
        command_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CommandHandler)
        kiss_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _KissHandler)
        command_server.daemon_threads = kiss_server.daemon_threads = True
        threads = [threading.Thread(target=s.serve_forever, daemon=True)
                   for s in (command_server, kiss_server)]
        for thread in threads:
            thread.start()
        statuses = []
        client = Data2GTransport("127.0.0.1", command_server.server_address[1],
            kiss_server.server_address[1], timeout=1, on_status=statuses.append)
        try:
            client.connect()
            client.kiss_socket.shutdown(socket.SHUT_RDWR)
            deadline = time.monotonic() + 2
            while client.connected and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(client.connected)
            self.assertIn("Data2G KISS connection closed", statuses)
        finally:
            client.close()
            for server in (command_server, kiss_server):
                server.shutdown()
                server.server_close()

    def test_background_session_owns_group_and_serializes_commands(self):
        _CommandHandler.commands.clear()
        command_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CommandHandler)
        kiss_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _KissHandler)
        command_server.daemon_threads = kiss_server.daemon_threads = True
        threads = [threading.Thread(target=s.serve_forever, daemon=True)
                   for s in (command_server, kiss_server)]
        for thread in threads:
            thread.start()
        ready, acks, frames, errors = [], [], [], []
        session = Data2GSession("127.0.0.1", command_server.server_address[1],
            kiss_server.server_address[1], callsign="ag7su",
            on_ready=lambda m, p: ready.append((m, p)),
            on_ack=lambda p, tag: acks.append((p, tag)),
            on_frame=lambda p, data: frames.append((p, data)), on_error=errors.append)
        try:
            modes = session.start(timeout=2)
            self.assertEqual(len(modes), 2)
            self.assertEqual(ready[0][1], 7)
            self.assertIn("BCAST OPEN PIXELQSO FROM AG7SU", _CommandHandler.commands)
            session.set_mode("qpsk-r1/2")
            session.send_frame(b"\x00\x05", b"fragment")
            deadline = time.monotonic() + 2
            while (not acks or not frames) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(acks, [(7, b"\x00\x05")])
            self.assertEqual(frames, [(7, b"image-fragment")])
            self.assertFalse(errors)
            self.assertTrue(session.close())
            self.assertFalse(session.connected)
            session._commands.put(("send", (b"\xff\xff", b"unknown old transmission")))
            self.assertEqual(len(session.start(timeout=2)), 2)
            self.assertTrue(session.connected)
            time.sleep(.1)
            self.assertEqual(acks, [(7, b"\x00\x05")], "stale frames must not replay after reconnect")
        finally:
            self.assertTrue(session.close())
            self.assertFalse(session.connected)
            for server in (command_server, kiss_server):
                server.shutdown()
                server.server_close()

    def test_broadcast_mode_discovery_checks_every_advertised_mode(self):
        _CommandHandler.commands.clear()
        _CommandHandler.refused_modes = {"qpsk-r1/5"}
        command_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CommandHandler)
        kiss_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _KissHandler)
        command_server.daemon_threads = kiss_server.daemon_threads = True
        threads = [threading.Thread(target=s.serve_forever, daemon=True)
                   for s in (command_server, kiss_server)]
        for thread in threads:
            thread.start()
        session = Data2GSession("127.0.0.1", command_server.server_address[1],
                                kiss_server.server_address[1], callsign="AG7SU")
        try:
            modes = session.start(timeout=2)
            self.assertEqual([mode.name for mode in modes], ["qpsk-r1/5", "qpsk-r1/2"])
            checked = session.check_broadcast_modes((mode.name for mode in modes), timeout=2)
            self.assertIsNotNone(checked["qpsk-r1/5"])
            self.assertIsNone(checked["qpsk-r1/2"])
            self.assertIn("BCAST MODE 7 qpsk-r1/5", _CommandHandler.commands)
            self.assertIn("BCAST MODE 7 qpsk-r1/2", _CommandHandler.commands)
            self.assertTrue(session.connected, "a refused catalog entry must not close the host session")
        finally:
            _CommandHandler.refused_modes = set()
            session.close()
            for server in (command_server, kiss_server):
                server.shutdown()
                server.server_close()

    def test_two_sessions_keep_host_ports_and_frames_isolated(self):
        class FirstKissHandler(_KissHandler):
            reply_payload = b"first-host-card"

        class SecondKissHandler(_KissHandler):
            reply_payload = b"second-host-card"

        servers = []
        sessions = []
        first_frames, second_frames = [], []
        first_acks, second_acks = [], []
        for kiss_handler in (FirstKissHandler, SecondKissHandler):
            command_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CommandHandler)
            kiss_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), kiss_handler)
            command_server.daemon_threads = kiss_server.daemon_threads = True
            for server in (command_server, kiss_server):
                threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.extend((command_server, kiss_server))

        try:
            for index, (command_server, kiss_server) in enumerate(zip(servers[::2], servers[1::2])):
                frames = first_frames if index == 0 else second_frames
                acks = first_acks if index == 0 else second_acks
                sessions.append(Data2GSession(
                    "127.0.0.1", command_server.server_address[1],
                    kiss_server.server_address[1], callsign="AG7SU",
                    on_frame=lambda _port, payload, target=frames: target.append(payload),
                    on_ack=lambda _port, tag, target=acks: target.append(tag)))
            first, second = sessions
            first_modes = first.start(timeout=2)
            second_modes = second.start(timeout=2)
            self.assertEqual([mode.name for mode in first_modes], [mode.name for mode in second_modes])
            self.assertNotEqual((first.command_port, first.kiss_port),
                                (second.command_port, second.kiss_port))
            first.send_frame(b"\x00\x01", b"first-test")
            second.send_frame(b"\x00\x02", b"second-test")
            deadline = time.monotonic() + 2
            while (not first_acks or not second_acks or not first_frames or not second_frames) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(first_acks, [b"\x00\x01"])
            self.assertEqual(second_acks, [b"\x00\x02"])
            self.assertEqual(first_frames, [b"first-host-card"])
            self.assertEqual(second_frames, [b"second-host-card"])
        finally:
            for session in sessions:
                session.close()
            for server in servers:
                server.shutdown()
                server.server_close()

    def test_refused_mode_ends_session_before_any_queued_frame(self):
        command_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CommandHandler)
        kiss_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _KissHandler)
        command_server.daemon_threads = kiss_server.daemon_threads = True
        threads = [threading.Thread(target=s.serve_forever, daemon=True)
                   for s in (command_server, kiss_server)]
        for thread in threads:
            thread.start()
        errors = []
        session = Data2GSession("127.0.0.1", command_server.server_address[1],
                                kiss_server.server_address[1], on_error=errors.append)
        try:
            session.start(timeout=2)
            session.set_mode("refused-mode")
            session.send_frame(b"\x00\x09", b"must-not-be-sent")
            deadline = time.monotonic() + 2
            while session.connected and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(session.connected)
            self.assertTrue(any("refused selected mode" in error for error in errors), errors)
        finally:
            session.close()
            for server in (command_server, kiss_server):
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()

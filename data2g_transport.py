"""Client for the supported Data2G command and KISS broadcast interfaces."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import queue
import socket
import threading
import time
from typing import Callable

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
KISS_DATA, KISS_ACKMODE = 0x00, 0x0C
MAX_KISS_FRAME_BYTES = 65536


class Data2GError(RuntimeError):
    """Host connection, command, or protocol error."""


@dataclass(frozen=True)
class Data2GMode:
    name: str
    bandwidth_hz: float
    bytes_per_codeword: int
    max_codewords: int
    seconds_at_one: float
    seconds_at_max: float


def parse_modes(lines: list[str]) -> list[Data2GMode]:
    """Parse MODE records returned by the host's MODES command."""
    modes = []
    for line in lines:
        fields = line.split()
        if not fields or fields[0] != "MODE":
            continue
        if len(fields) != 7:
            raise Data2GError(f"Malformed Data2G MODE response: {line!r}")
        try:
            modes.append(Data2GMode(fields[1], float(fields[2]), int(fields[3]),
                                    int(fields[4]), float(fields[5]), float(fields[6])))
        except ValueError as exc:
            raise Data2GError(f"Malformed Data2G MODE response: {line!r}") from exc
    if not modes:
        raise Data2GError("Data2G host returned no MODE records")
    return modes


def encode_kiss(port: int, command: int, payload: bytes) -> bytes:
    if not 0 <= port <= 15 or not 0 <= command <= 15:
        raise ValueError("KISS port and command must be 0..15")
    body = bytes(((port << 4) | command,)) + bytes(payload)
    escaped = bytearray((FEND,))
    for value in body:
        if value == FEND:
            escaped.extend((FESC, TFEND))
        elif value == FESC:
            escaped.extend((FESC, TFESC))
        else:
            escaped.append(value)
    escaped.append(FEND)
    return bytes(escaped)


class KissDecoder:
    """Incremental KISS parser; one feed may contain partial or many frames."""
    def __init__(self):
        self._frame = bytearray()
        self._inside = False
        self._escaped = False

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        frames = []
        for value in data:
            if value == FEND:
                if self._inside and self._frame:
                    header = self._frame[0]
                    frames.append((header >> 4, header & 0x0F, bytes(self._frame[1:])))
                self._frame.clear()
                self._inside = True
                self._escaped = False
            elif not self._inside:
                continue
            elif self._escaped:
                if value == TFEND:
                    self._frame.append(FEND)
                elif value == TFESC:
                    self._frame.append(FESC)
                else:
                    self._frame.clear()
                    self._inside = False
                self._escaped = False
            elif value == FESC:
                self._escaped = True
            else:
                self._frame.append(value)
                if len(self._frame) > MAX_KISS_FRAME_BYTES:
                    self._frame.clear()
                    self._inside = False
        return frames


class Data2GTransport:
    """Synchronous commands plus background delivery of KISS frames/statuses.

    Create and use this object from a worker thread. Callbacks run on its reader
    threads; GUI clients should marshal them onto the Qt thread.
    """
    def __init__(self, host: str = "127.0.0.1", command_port: int = 8300,
                 kiss_port: int = 8100, *, timeout: float = 3.0,
                 on_frame: Callable[[int, bytes], None] | None = None,
                 on_ack: Callable[[int, bytes], None] | None = None,
                 on_status: Callable[[str], None] | None = None):
        self.host, self.command_port, self.kiss_port = host, command_port, kiss_port
        self.timeout = timeout
        self.on_frame, self.on_ack, self.on_status = on_frame, on_ack, on_status
        self.command_socket = self.kiss_socket = None
        self._command_lines: queue.Queue[str | None] = queue.Queue()
        self._command_lock = threading.Lock()
        self._kiss_write_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.modes: list[Data2GMode] = []
        self.ports: dict[str, int] = {}

    @property
    def connected(self) -> bool:
        return self.command_socket is not None and self.kiss_socket is not None and not self._stop.is_set()

    def connect(self, *, retry_window: float = 0.0) -> list[Data2GMode]:
        if self.connected:
            return self.modes
        self._stop.clear()
        while True:
            try:
                self._command_lines.get_nowait()
            except queue.Empty:
                break
        command = kiss = None
        retry_until = time.monotonic() + max(0.0, retry_window)
        while True:
            command = kiss = None
            try:
                command = socket.create_connection((self.host, self.command_port), self.timeout)
                kiss = socket.create_connection((self.host, self.kiss_port), self.timeout)
                break
            except OSError as exc:
                for sock in (kiss, command):
                    if sock is not None:
                        sock.close()
                # The managed app launches this server immediately before the
                # client. Its audio backend can take several seconds to open
                # before either TCP listener is bound. Retry only refused
                # sockets and only when the caller explicitly allows startup
                # grace; remote hosts retain fail-fast behavior.
                if exc.errno == errno.ECONNREFUSED and time.monotonic() < retry_until:
                    time.sleep(min(0.1, max(0.0, retry_until - time.monotonic())))
                    continue
                raise Data2GError(f"Cannot connect to Data2G host {self.host}: {exc}") from exc
        # create_connection leaves its connect timeout installed. The reader
        # threads must stay blocked until data or EOF; command() applies its
        # own deadline while waiting on the line queue.
        command.settimeout(None)
        kiss.settimeout(None)
        self.command_socket, self.kiss_socket = command, kiss
        for target, name, runner in ((command, "data2g-command-reader", self._read_commands),
                                     (kiss, "data2g-kiss-reader", self._read_kiss)):
            thread = threading.Thread(target=runner, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        try:
            version = self.command("VERSION")
            if not any(line.startswith("VERSION Data2G ") for line in version):
                raise Data2GError("Connected service did not identify itself as a Data2G host")
            self.modes = parse_modes(self.command("MODES"))
            return self.modes
        except Exception:
            self.close()
            raise

    def command(self, line: str) -> list[str]:
        if not self.command_socket or self._stop.is_set():
            raise Data2GError("Data2G command connection is closed")
        if "\r" in line or "\n" in line:
            raise ValueError("Data2G commands are single CR-terminated lines")
        with self._command_lock:
            try:
                self.command_socket.sendall(line.encode("ascii") + b"\r")
                response = []
                until = time.monotonic() + self.timeout
                # The native host terminates MODES with OK. The supported
                # Python host currently emits all MODE lines without a
                # terminator, so accept a short quiet boundary only for this
                # multi-line catalog command.
                modes_quiet_period = 0.25 if line.upper() == "MODES" else None
                while True:
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        raise queue.Empty
                    wait_for = remaining
                    if modes_quiet_period is not None and response:
                        wait_for = min(wait_for, modes_quiet_period)
                    try:
                        item = self._command_lines.get(timeout=wait_for)
                    except queue.Empty:
                        if (modes_quiet_period is not None and response and
                                all(value.startswith("MODE ") for value in response)):
                            return response
                        raise
                    if item is None:
                        raise Data2GError("Data2G command connection closed")
                    terminal = item in {"OK", "WRONG"} or item.startswith("VERSION Data2G ")
                    if line.upper().startswith("BCAST OPEN ") and item.startswith("BCAST PORT "):
                        terminal = True
                    if terminal:
                        response.append(item)
                        if item == "WRONG":
                            raise Data2GError(f"Data2G host refused command: {line}")
                        return response
                    response.append(item)
            except queue.Empty as exc:
                raise Data2GError(f"Timed out waiting for Data2G response to {line!r}") from exc
            except (OSError, UnicodeEncodeError) as exc:
                raise Data2GError(f"Data2G command failed: {exc}") from exc

    def open_group(self, group: str = "PIXELQSO", callsign: str | None = None) -> int:
        if not group.strip():
            raise ValueError("Data2G group cannot be empty")
        command = f"BCAST OPEN {group.strip()}"
        if callsign:
            command += f" FROM {callsign.strip().upper()}"
        response = self.command(command)
        try:
            port = int(next(line.split()[2] for line in response if line.startswith("BCAST PORT ")))
        except (StopIteration, ValueError, IndexError) as exc:
            raise Data2GError(f"Malformed BCAST OPEN response: {response!r}") from exc
        self.ports[group] = port
        return port

    def set_mode(self, port: int, mode: str) -> None:
        self.command(f"BCAST MODE {int(port)} {mode.strip()}")

    def close_group(self, port: int) -> None:
        self.command(f"BCAST CLOSE {int(port)}")
        self.ports = {group: value for group, value in self.ports.items() if value != port}

    def send_frame(self, port: int, tag: bytes, payload: bytes) -> None:
        """Queue one frame with ACKMODE; ACK means sent, not heard remotely."""
        if len(tag) != 2:
            raise ValueError("ACKMODE tags must be exactly two bytes")
        if not self.kiss_socket or self._stop.is_set():
            raise Data2GError("Data2G KISS connection is closed")
        with self._kiss_write_lock:
            try:
                self.kiss_socket.sendall(encode_kiss(port, KISS_ACKMODE, tag + bytes(payload)))
            except OSError as exc:
                raise Data2GError(f"Data2G KISS send failed: {exc}") from exc

    def _read_commands(self):
        buffer = bytearray()
        sock = self.command_socket
        try:
            while not self._stop.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\r" in buffer:
                    raw, _, tail = buffer.partition(b"\r")
                    buffer[:] = tail
                    line = raw.decode("ascii", "replace").strip()
                    fields = line.split()
                    is_mode_notice = len(fields) == 2 and fields[0] == "MODE"
                    if (line.startswith(("PTT ", "BUSY ", "BUFFER ", "IAMALIVE")) or
                            is_mode_notice or (line.startswith("BCAST ") and
                                               not line.startswith("BCAST PORT "))):
                        self._status(line)
                    else:
                        self._command_lines.put(line)
        except OSError:
            pass
        finally:
            self._command_lines.put(None)
            if not self._stop.is_set():
                self._fail_connection("Data2G command connection closed")

    def _read_kiss(self):
        parser = KissDecoder()
        sock = self.kiss_socket
        try:
            while not self._stop.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    break
                for port, command, payload in parser.feed(chunk):
                    if command == KISS_ACKMODE and len(payload) == 2:
                        self._callback(self.on_ack, port, payload)
                    elif command == KISS_DATA:
                        self._callback(self.on_frame, port, payload)
                    else:
                        self._status(f"KISS command {command} on port {port}: {payload.hex()}")
        except OSError:
            pass
        finally:
            if not self._stop.is_set():
                self._fail_connection("Data2G KISS connection closed")

    def _fail_connection(self, message: str):
        if self._stop.is_set():
            return
        self._stop.set()
        self._command_lines.put(None)
        self._status(message)
        for sock in (self.kiss_socket, self.command_socket):
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _status(self, message: str):
        self._callback(self.on_status, message)

    @staticmethod
    def _callback(callback, *args):
        if callback is not None:
            try:
                callback(*args)
            except Exception:
                pass

    def close(self):
        self._stop.set()
        for sock in (self.kiss_socket, self.command_socket):
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
        self.kiss_socket = self.command_socket = None
        self._command_lines.put(None)
        current = threading.current_thread()
        for thread in self._threads:
            if thread is not current:
                thread.join(timeout=1)
        self._threads.clear()
        self.ports.clear()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_exc):
        self.close()


class Data2GSession:
    """Own a host connection and serialize commands away from the UI thread."""
    def __init__(self, host="127.0.0.1", command_port=8300, kiss_port=8100, *,
                 group="PIXELQSO", callsign=None, on_ready=None, on_frame=None, on_ack=None,
                 on_status=None, on_error=None, on_closed=None,
                 connect_retry_window=0.0):
        self.host, self.command_port, self.kiss_port = host, command_port, kiss_port
        self.group = group
        self.callsign = str(callsign).strip().upper() if callsign else None
        self.connect_retry_window = max(0.0, float(connect_retry_window))
        self.on_ready, self.on_frame, self.on_ack = on_ready, on_frame, on_ack
        self.on_status, self.on_error, self.on_closed = on_status, on_error, on_closed
        self._commands = queue.Queue(maxsize=64)
        self._thread = None
        self._ready = threading.Event()
        self._startup = threading.Event()
        self._startup_error = None
        self._closed = threading.Event()
        self.modes = []
        self.port = None

    @property
    def connected(self):
        return self._ready.is_set() and not self._closed.is_set()

    def start(self, timeout=None):
        if self._thread and self._thread.is_alive():
            raise Data2GError("Data2G session is already running")
        # A disconnected host can be retried with the same session object. A
        # fresh queue guarantees nothing left from the prior connection is
        # replayed when the host comes back.
        self._ready.clear()
        self._startup.clear()
        self._startup_error = None
        self._closed.clear()
        self._commands = queue.Queue(maxsize=64)
        self.modes = []
        self.port = None
        self._thread = threading.Thread(target=self._run, name="data2g-session", daemon=True)
        self._thread.start()
        if timeout is None:
            timeout = max(8.0, self.connect_retry_window + 8.0)
        if not self._startup.wait(timeout):
            self.close()
            raise Data2GError(f"Timed out connecting to Data2G host {self.host}")
        if self._startup_error:
            raise Data2GError(self._startup_error)
        return self.modes

    def set_mode(self, mode):
        self._require_ready()
        self._enqueue(("mode", str(mode)))

    def send_frame(self, tag, payload):
        self._require_ready()
        if len(tag) != 2:
            raise ValueError("ACKMODE tags must be exactly two bytes")
        if len(payload) + 4 > 65535:
            raise ValueError("Data2G KISS application frames must fit the 16-bit KISS-link length field")
        self._enqueue(("send", (bytes(tag), bytes(payload))))

    def _enqueue(self, action):
        try:
            self._commands.put_nowait(action)
        except queue.Full as exc:
            raise Data2GError("Data2G host command queue is full") from exc

    def _require_ready(self):
        if not self.connected:
            raise Data2GError("Data2G session is not connected")

    def close(self, timeout=5.0):
        thread = self._thread
        if thread and thread.is_alive():
            self._commands.put(("close", None))
            if thread is not threading.current_thread():
                thread.join(timeout)
        return not thread or not thread.is_alive()

    def _run(self):
        client = Data2GTransport(
            self.host, self.command_port, self.kiss_port,
            on_frame=self._frame, on_ack=self._ack, on_status=self._status)
        try:
            self.modes = client.connect(retry_window=self.connect_retry_window)
            self.port = client.open_group(self.group, self.callsign)
            self._ready.set()
            self._startup.set()
            Data2GTransport._callback(self.on_ready, self.modes, self.port)
            while True:
                try:
                    action, value = self._commands.get(timeout=0.25)
                except queue.Empty:
                    if not client.connected:
                        raise Data2GError("Data2G host connection was lost")
                    continue
                if action == "close":
                    break
                try:
                    if action == "mode":
                        client.set_mode(self.port, value)
                    elif action == "send":
                        tag, payload = value
                        client.send_frame(self.port, tag, payload)
                except Exception as exc:
                    Data2GTransport._callback(self.on_error, str(exc))
                    if action == "mode":
                        raise Data2GError(f"Data2G host refused selected mode {value!r}: {exc}") from exc
        except Exception as exc:
            if not self._ready.is_set():
                self._startup_error = str(exc)
            Data2GTransport._callback(self.on_error, str(exc))
        finally:
            self._startup.set()
            self._ready.clear()
            if self.port is not None:
                try:
                    client.close_group(self.port)
                except Exception:
                    pass
            client.close()
            self._closed.set()
            Data2GTransport._callback(self.on_closed)

    def _frame(self, port, payload):
        Data2GTransport._callback(self.on_frame, port, payload)

    def _ack(self, port, tag):
        Data2GTransport._callback(self.on_ack, port, tag)

    def _status(self, line):
        Data2GTransport._callback(self.on_status, line)

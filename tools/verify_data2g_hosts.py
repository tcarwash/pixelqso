"""Exercise the pinned upstream Python Data2G host through real audio devices.

Two temporary PulseAudio/PipeWire null sinks cross-connect two isolated hosts,
without using a radio or disturbing the system's default audio routes. Run from
the project environment with ``uv run python tools/verify_data2g_hosts.py``.
"""
from __future__ import annotations

import os
import argparse
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import cardmodem
from card_backends import host_mode_backends, preferred_data2g_mode_key
from card_transfer import CardTransferReceiver, HEADER, fragment
from data2g_transport import Data2GSession
from data2g_runtime import local_host_command


def free_port(used: set[int]) -> int:
    for _ in range(100):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in used and port + 1 not in used:
            with socket.socket() as check:
                try:
                    check.bind(("127.0.0.1", port + 1))
                except OSError:
                    continue
            used.update((port, port + 1))
            return port
    raise RuntimeError("could not find free Data2G command/data ports")


class PulseLoopback:
    def __init__(self):
        self.pactl = shutil.which("pactl")
        if not self.pactl:
            raise RuntimeError("pactl is required for the temporary audio-pipe test")
        self.suffix = uuid.uuid4().hex[:8]
        self.modules: list[str] = []
        self.devices = {}
        for name in ("a", "b"):
            sink = f"pixelqso_d2g_{name}_{self.suffix}"
            source = sink + "_rx"
            self._load("module-null-sink", f"sink_name={sink}", "rate=48000", "channels=2",
                       f"sink_properties=device.description={sink}")
            self._load("module-remap-source", f"master={sink}.monitor", f"source_name={source}",
                       "rate=48000", "channels=1", "channel_map=mono",
                       f"source_properties=device.description={source}")
            self.devices[name] = (sink, source)

    def _run(self, *args) -> str:
        result = subprocess.run([self.pactl, *args], capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "pactl failed")
        return result.stdout.strip()

    def _load(self, module: str, *args) -> None:
        self.modules.append(self._run("load-module", module, *args))

    def close(self):
        for module in reversed(self.modules):
            try:
                self._run("unload-module", module)
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
        self.modules.clear()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", help="Exact advertised Data2G mode, e.g. n10-qpsk-r1/2")
    args = parser.parse_args()
    if os.name != "posix":
        raise RuntimeError("this integration test currently requires Linux audio loopback")
    try:
        import data2g.host  # noqa: F401
        import pyaudio  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("install the standard Pixel QSO dependencies first") from exc

    temp = tempfile.TemporaryDirectory(prefix="pixelqso-python-hosts-")
    root = Path(temp.name)
    pulse = None
    processes: list[subprocess.Popen] = []
    log_handles = []
    sessions: list[Data2GSession] = []
    used: set[int] = set()
    cmd_a, cmd_b = free_port(used), free_port(used)
    kiss_a, kiss_b = free_port(used), free_port(used)
    rx_frames: queue.Queue = queue.Queue()
    statuses: queue.Queue = queue.Queue()
    acks: queue.Queue = queue.Queue()
    errors: queue.Queue = queue.Queue()
    try:
        pulse = PulseLoopback()
        device_list = subprocess.run([sys.executable, "-m", "data2g.host", "--list-audio-devices"],
                                     capture_output=True, text=True, timeout=15)
        if device_list.returncode:
            raise RuntimeError(f"Data2G audio discovery failed: {device_list.stderr[-2000:]}")
        available = device_list.stdout
        for sink, source in pulse.devices.values():
            if sink not in available or source not in available:
                raise RuntimeError(f"Data2G did not enumerate temporary loopback devices {sink}/{source}")

        def launch(station: str, command_port: int, kiss_port: int,
                   capture_source: str, playback_sink: str, callsign: str):
            log_path = root / f"host-{station}.log"
            log = log_path.open("w", encoding="utf-8")
            log_handles.append(log)
            processes.append(subprocess.Popen(local_host_command([
                "--host", "127.0.0.1", "--kiss-address", "127.0.0.1",
                "--command-port", str(command_port), "--kiss-port", str(kiss_port),
                "--kiss-bw", "2400", "--mycall", callsign,
                "--input-device", capture_source, "--output-device", playback_sink,
                "--rigctld-port", "0", "--record-dir", "", "--log-level", "INFO"]),
                cwd=root, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT))
            return log_path, log

        # TX audio at A plays into sink A; B captures its monitor, and vice versa.
        launch("a", cmd_a, kiss_a, pulse.devices["b"][1],
               pulse.devices["a"][0], "AG7SU")
        launch("b", cmd_b, kiss_b, pulse.devices["a"][1],
               pulse.devices["b"][0], "K1ABC")
        time.sleep(1)
        if any(proc.poll() is not None for proc in processes):
            raise RuntimeError("Data2G Python host exited at startup")

        sender = Data2GSession("127.0.0.1", cmd_a, kiss_a, callsign="AG7SU", connect_retry_window=15,
            on_ack=lambda port, tag: acks.put((port, tag)),
            on_status=lambda status: statuses.put("A " + status),
            on_error=lambda error: errors.put("A " + error))
        receiver = Data2GSession("127.0.0.1", cmd_b, kiss_b, callsign="K1ABC", connect_retry_window=15,
            on_frame=lambda port, frame: rx_frames.put((port, frame)),
            on_status=lambda status: statuses.put("B " + status),
            on_error=lambda error: errors.put("B " + error))
        sessions.extend((sender, receiver))
        modes_a, modes_b = sender.start(timeout=20), receiver.start(timeout=20)
        checks_a = sender.check_broadcast_modes(mode.name for mode in modes_a)
        checks_b = receiver.check_broadcast_modes(mode.name for mode in modes_b)
        catalog_a = host_mode_backends(
            modes_a, (name for name, refusal in checks_a.items() if refusal is None))
        catalog_b = host_mode_backends(
            modes_b, (name for name, refusal in checks_b.items() if refusal is None))
        common = {key: backend for key, backend in catalog_a.items()
                  if backend.usable and key in catalog_b and catalog_b[key].usable}
        if not common:
            raise RuntimeError("Hosts have no common usable broadcast card mode")
        if args.mode:
            robust = next((backend for backend in common.values()
                           if backend.mode_name == args.mode), None)
            if robust is None:
                raise RuntimeError(f"Requested mode {args.mode!r} is not usable on both hosts")
        else:
            robust = common[preferred_data2g_mode_key(common)]
        mode = robust.mode_name
        sender.set_mode(mode)
        receiver.set_mode(mode)

        card = cardmodem.example_card()
        card.callsign, card.grid = "AG7SU", "CN85"
        packed = cardmodem.minimal_avatar_payload(card)
        chunk_size = min(robust.max_frame_bytes, catalog_b[robust.key].max_frame_bytes) - HEADER.size
        frames = [fragment(card, packed, offset, packed[offset:offset + chunk_size], "cq")
                  for offset in range(0, len(packed), chunk_size)]
        for index, payload in enumerate(frames):
            tag = index.to_bytes(2, "big")
            sender.send_frame(tag, payload)
            try:
                port, ack_tag = acks.get(timeout=45)
            except queue.Empty as exc:
                raise RuntimeError(f"Python host send not ACKed; errors={list(errors.queue)}") from exc
            if (port, ack_tag) != (sender.port, tag):
                raise RuntimeError(f"unexpected Python host ACKMODE response: {port}, {ack_tag!r}")

        transfer = CardTransferReceiver()
        result = None
        deadline = time.monotonic() + 45
        received = 0
        while time.monotonic() < deadline:
            try:
                port, payload = rx_frames.get(timeout=.25)
            except queue.Empty:
                continue
            if port != receiver.port:
                raise RuntimeError(f"received Python host frame on unexpected port {port}")
            assembly, metadata = transfer.feed("AG7SU", payload)
            received += 1
            result = assembly, metadata
            if assembly.exact:
                break
        if not result or not result[0].exact:
            raise RuntimeError(f"Python host transfer incomplete after {received} frames; errors={list(errors.queue)}")
        if result[1]["callsign"] != "AG7SU" or result[1]["grid"] != "CN85":
            raise RuntimeError(f"wrong Python host card identity: {result[1]}")
        status_list = []
        while not statuses.empty():
            status_list.append(statuses.get_nowait())
        if not any("HEARD AG7SU" in status for status in status_list):
            raise RuntimeError(f"Python receiver did not report sender: {status_list}")
        print(f"PYTHON_TWO_HOST_CARD_PASS mode={robust.mode_name} fragments={len(frames)} "
              f"received={received} CRC=verified callsign=AG7SU grid=CN85")
        return 0
    finally:
        failed = sys.exc_info()[0] is not None
        for session in sessions:
            session.close(timeout=3)
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if pulse:
            pulse.close()
        for log in log_handles:
            log.close()
        if failed:
            for path in root.glob("host-*.log"):
                print(f"--- {path.name} ---", file=sys.stderr)
                print(path.read_text(errors="replace")[-12000:], file=sys.stderr)
        temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

"""Exercise PixelQSO's real transport against two native Data2G hosts.

The hosts are connected by paced float32 audio pipes, so this verifies native
modem/KISS interoperability without opening a sound device or radio. Run with
``uv run python tools/test_data2g_native_hosts.py --host-binary /path/to/data2g-host``.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import time

import cardmodem
from card_backends import host_mode_backends
from card_transfer import CardTransferReceiver, HEADER, fragment
from data2g_transport import Data2GSession


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
    raise RuntimeError("could not find free native-host command/data ports")


def host_command(binary: Path, command_port: int, kiss_port: int,
                 audio_in: Path, audio_out: Path, callsign: str) -> list[str]:
    return [str(binary), "--command-port", str(command_port), "--kiss-port", str(kiss_port),
            "--host", "127.0.0.1", "--kiss-address", "127.0.0.1",
            "--audio-io", f"pipe:{audio_in},{audio_out}", "--no-rig",
            "--record-dir", "", "--mycall", callsign, "--kiss-bw", "2400",
            "--threads", "2", "--log-level", "INFO"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-binary", type=Path, required=True,
                        help="built Data2G native data2g-host executable")
    args = parser.parse_args()
    binary = args.host_binary.expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error(f"not an executable Data2G host: {binary}")
    if not hasattr(os, "mkfifo"):
        parser.error("this native audio-pipe integration test requires POSIX named pipes")

    processes: list[subprocess.Popen] = []
    sessions: list[Data2GSession] = []
    temp = tempfile.TemporaryDirectory(prefix="pixelqso-native-hosts-")
    root = Path(temp.name)
    a_to_b, b_to_a = root / "a-to-b.f32", root / "b-to-a.f32"
    os.mkfifo(a_to_b)
    os.mkfifo(b_to_a)
    used: set[int] = set()
    cmd_a, cmd_b = free_port(used), free_port(used)
    kiss_a, kiss_b = free_port(used), free_port(used)
    rx_frames: queue.Queue = queue.Queue()
    rx_status: queue.Queue = queue.Queue()
    tx_acks: queue.Queue = queue.Queue()
    errors: queue.Queue = queue.Queue()

    try:
        processes.append(subprocess.Popen(
            host_command(binary, cmd_a, kiss_a, b_to_a, a_to_b, "AG7SU"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        processes.append(subprocess.Popen(
            host_command(binary, cmd_b, kiss_b, a_to_b, b_to_a, "K1ABC"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        # Let both native FIFO readers and writers open their paired endpoints.
        time.sleep(0.5)
        for proc in processes:
            if proc.poll() is not None:
                raise RuntimeError(f"Data2G host exited during startup: {proc.stdout.read()}")

        sender = Data2GSession("127.0.0.1", cmd_a, kiss_a, callsign="AG7SU",
                               on_ack=lambda port, tag: tx_acks.put((port, tag)),
                               on_status=lambda status: rx_status.put("A " + status),
                               on_error=lambda error: errors.put("A " + error))
        receiver = Data2GSession("127.0.0.1", cmd_b, kiss_b, callsign="K1ABC",
                                 on_frame=lambda port, frame: rx_frames.put((port, frame)),
                                 on_status=lambda status: rx_status.put("B " + status),
                                 on_error=lambda error: errors.put("B " + error))
        sessions.extend((sender, receiver))
        modes_a, modes_b = sender.start(timeout=15), receiver.start(timeout=15)
        presets_a, presets_b = host_mode_backends(modes_a), host_mode_backends(modes_b)
        robust = presets_a.get("data2g_1200_robust")
        if robust is None or "data2g_1200_robust" not in presets_b:
            raise RuntimeError("native host MODES did not expose a compatible robust preset")
        mode = robust.mode_name
        sender.set_mode(mode)
        receiver.set_mode(mode)

        card = cardmodem.example_card()
        card.callsign, card.grid = "AG7SU", "CN85"
        packed = cardmodem.minimal_avatar_payload(card)
        chunk_size = 256 - HEADER.size
        frames = [fragment(card, packed, offset, packed[offset:offset + chunk_size], "cq")
                  for offset in range(0, len(packed), chunk_size)]
        transfer = CardTransferReceiver()
        result = None
        for index, payload in enumerate(frames):
            tag = index.to_bytes(2, "big")
            sender.send_frame(tag, payload)
            try:
                ack_port, ack_tag = tx_acks.get(timeout=30)
            except queue.Empty as exc:
                raise RuntimeError(f"native host send was not ACKed; statuses={list(rx_status.queue)}; "
                                   f"errors={list(errors.queue)}; sender_connected={sender.connected}") from exc
            if (ack_port, ack_tag) != (sender.port, tag):
                raise RuntimeError(f"unexpected ACKMODE response: {ack_port}, {ack_tag!r}")

        deadline = time.monotonic() + 15
        received = 0
        while time.monotonic() < deadline:
            try:
                port, payload = rx_frames.get(timeout=0.25)
            except queue.Empty:
                if result and result[1]["exact"]:
                    break
                continue
            if port != receiver.port:
                raise RuntimeError(f"received frame on unexpected port {port}")
            assembly, metadata = transfer.feed("AG7SU", payload)
            received += 1
            result = assembly, metadata
            if assembly.exact:
                break
        if not result or not result[0].exact:
            raise RuntimeError(f"native host transfer incomplete: {received} RX fragments")
        metadata = result[1]
        if metadata["callsign"] != "AG7SU" or metadata["grid"] != "CN85":
            raise RuntimeError(f"wrong recovered card identity: {metadata}")
        if not any("HEARD AG7SU" in rx_status.get_nowait()
                   for _ in range(rx_status.qsize())):
            raise RuntimeError("native receiver did not report the sender callsign")
        print(f"NATIVE_TWO_HOST_CARD_PASS mode={mode} fragments={len(frames)} "
              f"received={received} CRC=verified callsign=AG7SU grid=CN85")
        return 0
    finally:
        failed = sys.exc_info()[0] is not None
        for session in sessions:
            session.close(timeout=3)
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        if failed:
            for index, proc in enumerate(processes, 1):
                if proc.stdout:
                    print(f"--- native Data2G host {index} log ---", file=sys.stderr)
                    print(proc.stdout.read(), file=sys.stderr)
        temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

"""Process-level integration for the supported Data2G host CLI."""
from __future__ import annotations

import sys


def local_host_command(arguments: list[str], *, frozen: bool | None = None,
                       executable: str | None = None) -> list[str]:
    """Build argv for Data2G's server entry point in a child process.

    Source installs use Data2G's published module CLI. Frozen builds dispatch
    the same CLI arguments through PixelQSO's bundled host entry point.
    No modem classes or waveform functions are imported by this adapter.
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    executable = executable or sys.executable
    if frozen:
        return [executable, "--run-data2g-host", *arguments]
    return [executable, "-m", "data2g.host", *arguments]


def data2g_audio_device_selector(device_id: bytes | str, description: str) -> str:
    """Prefer Qt's backend device ID, which PortAudio can match reliably.

    Qt's user-facing description is often not the name exposed by PyAudio
    (for example PipeWire exposes an ALSA/PulseAudio node ID instead).
    """
    try:
        stable_id = (device_id if isinstance(device_id, str)
                     else bytes(device_id).decode("utf-8")).strip()
    except (UnicodeDecodeError, TypeError, ValueError):
        stable_id = ""
    if stable_id and stable_id.isprintable():
        return stable_id
    return description.strip()


def local_host_arguments(*, command_port: int, kiss_port: int, callsign: str,
                         input_device: str, output_device: str, rig_host: str,
                         rig_port: int, bandwidth_hz: int, record_dir: str) -> list[str]:
    """Create explicit server CLI settings from the station's saved options."""
    if bandwidth_hz not in (500, 2400):
        raise ValueError("Data2G host bandwidth must be 500 or 2400 Hz")
    if not 1 <= command_port < 65535 or not 1 <= kiss_port <= 65535:
        raise ValueError("Data2G command/data and KISS ports must fit the TCP port range")
    if len({command_port, command_port + 1, kiss_port}) != 3:
        raise ValueError("Data2G command, data, and KISS ports must be distinct")
    args = ["--host", "127.0.0.1", "--kiss-address", "127.0.0.1",
            "--command-port", str(command_port), "--kiss-port", str(kiss_port),
            "--kiss-bw", str(bandwidth_hz), "--mycall", callsign.strip().upper(),
            "--rigctld-host", rig_host.strip(), "--rigctld-port", str(rig_port),
            "--record-dir", record_dir]
    if input_device:
        args.extend(("--input-device", input_device))
    if output_device:
        args.extend(("--output-device", output_device))
    return args

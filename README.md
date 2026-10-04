# Pixel QSO

Pixel QSO sends small QSO/QSL image cards over digital radio. It handles card
identity, fragment transfer, image assembly, previews, and QSO stages. Modems
with the Data2G label run through Data2G's supported host command and KISS
interfaces.

## Install and run

Python 3.10 or newer is required. The standard install includes the pinned
Data2G host runtime without PyTorch:

```sh
uv sync
uv run python app.py
```

With pip:

```sh
python -m pip install -r requirements-app.txt
python app.py
```

The app includes 16×16, 32×32, and 64×64 cards; 8-, 16-, and 32-color
palettes; drawing, text, and stamps; manual, automatic, and beacon exchanges;
and local card and QSO storage.

## Modem choices

Resilient 100, 50, and 25 baud are normal modes. Resilient 100 is the default
when no compatible Data2G host mode is selected. Higher-speed Resilient, Fast,
Weak signal, and legacy modes are under **Show experimental modem modes** in
Station settings. Weak signal is optional and uses SciPy without PyTorch:

```sh
uv sync --extra weak-signal
uv run --extra weak-signal python app.py
```

Data2G Robust and Fast become available after Pixel QSO discovers compatible
modes from a Data2G host. Robust is preferred for a new selection when the host
is connected. For local Resilient modes, the audio placement control sets the
lowest tone; 300 Hz is the recommended starting offset. This control does not
configure Data2G's audio placement.

## Data2G host

In Station settings, choose **Start local Data2G host** to have Pixel QSO start
the bundled host, or choose **Remote Data2G host** and enter a host address.
The defaults are command port 8300 and KISS port 8100. Pixel QSO discovers the
host's mode catalog, opens the shared `PIXELQSO` broadcast group, and chooses
compatible Robust/Fast presets from the host's supported modes.

For Data2G modes, Data2G owns radio audio and PTT. Pixel QSO releases its local
audio receiver and CAT connection while the host is selected. Local Resilient
modes continue using Pixel QSO's configured audio and CAT path. Do not connect
Pixel QSO to a Data2G instance whose single-client command port is already
owned by its GUI; use the managed host or a separate host instance instead.
ACKMODE confirms that a local frame finished transmitting; it does not confirm
that another station received the card. Group names and CRC masks are not
encryption or privacy. See [the host protocol notes](DATA2G-BROADCAST-NOTES.md)
and [on-air testing guide](ON-AIR-TESTING.md).

## Two-client software test

Run two isolated windows linked by simulated audio. This does not use radio,
CAT, PTT, or physical audio devices:

```sh
uv run python tools/open_two_clients.py
```

For WebSDR, radio, or Data2G host tests, follow [ON-AIR-TESTING.md](ON-AIR-TESTING.md).
For controlled modem comparisons and recorded captures, see
[BENCHMARKING.md](BENCHMARKING.md). Synthetic results do not establish live RF
performance.

## Build

The default frozen app includes the managed Data2G host runtime and excludes
PyTorch, GPU packages, and Pixel QSO's optional Weak signal modem:

```sh
python -m pip install . PyInstaller
python -m PyInstaller --clean --noconfirm pixelqso.spec
```

Build the optional Weak signal variant with SciPy using:

```sh
python -m pip install '.[weak-signal]' PyInstaller
PIXELQSO_FREEZE_WEAK_SIGNAL=1 python -m PyInstaller --clean --noconfirm pixelqso.spec
```

Both builds contain `icon.png`. Build on each target operating system; the
desktop CI matrix builds standard and Weak signal packages for Linux, Windows,
and macOS. Linux smoke tests and bundled Data2G host checks are tracked in
[the integration plan](docs/DATA2G-INTEGRATION-PLAN.md).

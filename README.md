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
Station settings. CPU weak-signal modes ship in the same app. They need no
PyTorch or GPU packages; SciPy is included by the Data2G runtime.

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
for details on group and frame behavior.

## Build

The frozen app includes the managed Data2G host runtime and experimental CPU
modems, while excluding PyTorch and GPU packages:

```sh
python -m pip install . PyInstaller
python -m PyInstaller --clean --noconfirm pixelqso.spec
```

The one build contains `icon.png` and all CPU modes. Build on each target
operating system; desktop CI builds one package for Linux, Windows, and macOS.
The managed Data2G host is bundled in each platform build.

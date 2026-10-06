# Pixel QSO

Pixel QSO sends small QSO/QSL image cards over digital radio. It handles card
identity, fragment transfer, image assembly, previews, and QSO stages. Modems
with the Data2G label run through Data2G's supported host command and KISS
interfaces.

## Screenshots

| QSO session | Card editor |
| --- | --- |
| <img src="screenshots/qso-session.png" alt="Pixel QSO session wall with staged cards and receive preview" width="480"> | <img src="screenshots/card-editor.png" alt="Pixel QSO card editor with drawing tools, palette, and card settings" width="480"> |

Example cards made in the editor:

![Starter cards showing sample QSO and signal artwork](screenshots/starter-cards.png)

## Install and run

Python 3.10 or newer is required. The standard install includes the pinned
Data2G host runtime without PyTorch:

The managed host uses [PyAudio, a PortAudio binding](https://people.csail.mit.edu/hubert/pyaudio/).
On Debian or Ubuntu, install the PortAudio
development package before installing Pixel QSO; on macOS, install PortAudio
with Homebrew first. These are the same system prerequisites used by the
desktop build workflow.

```sh
# Debian / Ubuntu
sudo apt-get install portaudio19-dev

# macOS
brew install portaudio
```

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

Data2G is the default backend. After connecting, Pixel QSO lists the host's
advertised modes that its broadcast API accepts for this group and that have
enough frame capacity for a Pixel QSO card fragment. Each entry shows the exact
host mode name, bandwidth, and maximum application-frame size.
Usable QPSK rate-1/2 modes near 1.2 kHz and 500 Hz, and a 16-QAM rate-1/2
mode near 1.2 kHz, appear first as **Everyday HF**, **Narrow HF**, and
**Faster · strong path** when available. A separator puts all other host modes
below this shortlist. Your selected mode is retained; unavailable modes are
never recommended.
The transfer estimate reports RF burst airtime; Data2G host channel-access
waits can make elapsed send time longer.

Pixel QSO's local 8-FSK, QPSK, and progressive 4-FSK modems are available
through **Experimental modems** after enabling **Show experimental modem modes**
in Station settings. The narrow 8-FSK modes have an audio placement control that
sets the lowest tone; 300 Hz is the recommended starting offset. It does not
configure Data2G's audio placement.

Four additional single-burst experiments are available: **8-FSK · soft LDPC**
(5.005 s), **QPSK · LDPC + adaptive equalizer** (4.622 s),
**8-FSK · strong RS + outer parity** (5.480 s), and **16-FSK · soft LDPC**
(5.640 s). They carry fixed 32×32, eight-color cards. The outer-parity mode
provides checked partial previews; the LDPC modes release pixels after whole-card
verification. See [experimental burst formats and validation](docs/EXPERIMENTAL-BURSTS.md).

The experimental names describe modulation and decoding: **8-FSK · RS + soft Chase**
uses Reed–Solomon with bounded soft recovery; **8-FSK · raw pixels** leaves image
pixels unchecked; **QPSK · soft LDPC** uses iterative soft decoding, with a
separate copy-combining choice for repeated transmissions; **4-FSK · soft Viterbi**
uses convolutional coding and progressive packets. Hover over a mode for its
error correction and checksum details. Display names do not change saved mode
IDs or on-air formats.

Data2G reception is selected and decoded by the connected host; it does not
follow Pixel QSO's transmit-mode selector. The host sends checked application
frames to Pixel QSO, where they enter the shared card assembly path. Receive-all,
waterfall, and local audio-placement controls apply only to Experimental modems.

The receive panel shows **Receiving signal** and an animated activity bar as
soon as the Data2G host reports a busy channel. This identifies modem activity;
card identity and image coverage appear when checked application frames arrive.
The host currently delivers those frames after decoding a burst, so Pixel QSO
cannot preview pixels within an unfinished Data2G burst. Lost-frame notices
show that another copy is needed. The Reed–Solomon 8-FSK modes expose checked identity
headers and provisional pixels before the first image block finishes. Receive-all
publishes its first preview while other decoders continue searching. QPSK tries
the strongest copy before demodulating later copies, combining them if needed;
it releases pixels only after the card checksum passes.

The existing 8-FSK RS modes keep the version 3 on-air format for compatibility
with existing local-modem stations. **8-FSK · RS + soft Chase · 400 baud · v4 CRC32** is a
separate experimental mode and sends a new version 4 format. Version 4 protects
the canonical packed palette raster's CRC32 and content tag in its header, adds
explicit block position/count fields plus a content-tag binding in every block,
and accepts a complete image only after all checked blocks match that raster
identity. Version 3 remains decodable with
its original per-block CRC verification scope; it does not claim whole-image
checksum verification. Version 4 transmissions require a peer with v4 support.

## Data2G host

On a new install, Pixel QSO selects Data2G and starts the managed local host
when the app opens, using the configured host audio and rigctld settings. To
change those settings after startup, disconnect the Data2G host in Station
settings, edit its settings, then connect again. To connect to a remote host,
disconnect the managed host, select **Remote Data2G host**, and enter its
address. On a fresh configuration, `PIXELQSO_DATA2G_HOST` selects a remote host
at startup; a saved connection source takes precedence. The defaults are
command port 8300 and KISS port 8100. Pixel QSO discovers the host's mode
catalog, opens the shared `PIXELQSO` broadcast group, and validates the catalog
against that group's `BCAST MODE` support before showing choices.

For Data2G modes, Data2G owns radio audio and PTT. Pixel QSO releases its local
audio receiver and CAT connection while the host is selected. Local 8-FSK
modes continue using Pixel QSO's configured audio and CAT path. Do not connect
Pixel QSO to a Data2G instance whose single-client command port is already
owned by its GUI; use the managed host or a separate host instance instead.
Configure the Data2G host's radio, audio devices, and rigctld endpoint in the
host settings. Its audio selectors list PortAudio devices and pass the selected
device index directly to the managed host; **System default** leaves selection
to PortAudio. A successful command/KISS connection confirms transport access;
it does not confirm that host audio or PTT is ready. If the managed host reports
a PTT-on or PTT-off failure, Pixel QSO stops automatic exchange and blocks
further Data2G transmission and CAT handoff. The fault remains set across app
restarts and host reconnects. After checking that the radio is unkeyed, confirm
the recovery prompt in Pixel QSO before resuming radio control.
The one-shot beacon action sends through Data2G at the frequency configured on
the host; Pixel QSO does not tune a Data2G radio. The local beacon frequency
control applies only to Experimental modems.
ACKMODE confirms that a local frame finished transmitting; it does not confirm
that another station received the card. Group names and CRC masks are not
encryption or privacy. See [the host protocol notes](DATA2G-BROADCAST-NOTES.md)
for details on group and frame behavior.

Pixel QSO's internal adapter boundary records which component owns radio and
audio, what receive features are available, and what integrity evidence a
backend supplies. Data2G enters as checked application frames; experimental
local modems enter as decoded blocks or candidate pixels. Both feed the same
bounded card assembly service. Adding an experimental backend requires a known
Pixel QSO framing and integrity contract; arbitrary modem formats are not
automatically interoperable.

## On-air capture

Session recording is opt-in. Set `PIXELQSO_ON_AIR_CONFIG` to a JSON config
containing `session_id`, `station_id`, and `output_dir` to create local capture
files. Data2G receive events include the exact application frame in a `.bin`
sidecar alongside the event record, so assembly behavior can be replayed from
the same received frames. If a collector URL is configured, it receives the
event metadata and frame hash; raw frame sidecars stay in the local output
directory. Replay a capture with:

```sh
uv run python tools/replay_data2g_capture.py \
  path/to/session/station/events.jsonl
```

The command checks sidecar hashes, compares direct fragment assembly with the
shared assembly service, and compares replayed card references with the
references recorded by the app. A clean result requires a closed session with
the final `capture-summary.json` written after the recorder drains, with no
recorder drops or write errors; partial captures are reported as inconclusive.

## Build

The frozen app includes the managed Data2G host runtime and experimental CPU
modems, while excluding PyTorch and GPU packages. It uses `icon.png` for the
app window, `icon.ico` for the Windows executable, and `icon.icns` for the macOS
app bundle:

```sh
uv sync
uv run --with PyInstaller python -m PyInstaller --clean --noconfirm pixelqso.spec
```

The one build contains `icon.png` and all CPU modes. Build on each target
operating system; desktop CI builds one package for Linux, Windows, and macOS.
The managed Data2G host is bundled in each platform build. On Linux the output
is `dist/PixelQSO`; on Windows it is `dist/PixelQSO.exe`; on macOS it is
`dist/PixelQSO.app`.

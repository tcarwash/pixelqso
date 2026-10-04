# Pixel QSO

Pixel QSO is a desktop application for sending small pixel-art QSO/QSL cards over radio. Transfers are one-way and can be repeated so the receiver can combine successfully decoded data from multiple copies.

Requires Python 3.10+.

With uv, the default environment contains the desktop app and the supported
Data2G host runtime. Torch remains an upstream training-only extra and is not
installed. SciPy for PixelQSO experimental weak-signal modes remains optional:

```sh
uv sync
uv run python app.py
```

The equivalent standard install with pip is shown below. `requirements-app.txt`
pins the same Data2G host revision and does not install Torch or SciPy for
PixelQSO's experimental weak-signal modem.

```sh
python -m pip install -r requirements-app.txt
python app.py
```

The app supports:

- 16×16, 32×32, and 64×64 cards
- 8-, 16-, and 32-color palettes
- Pixel drawing, text, and stamps
- Fast and error-protected burst modes
- Manual, automatic, and beacon exchanges
- Local card and QSO storage
- CAT/PTT through Hamlib

Choose **Resilient · ≈900 Hz**, **≈450 Hz**, or **≈225 Hz** under
Transmission type for narrower, longer 8-FSK bursts. Choose **Audio placement**
independently: **Near carrier** (the default) uses tones at 100–800, 50–400,
or 25–200 Hz, respectively; **Centered at 1500 Hz** moves the same signal
into the middle of the audio passband; **Custom lowest tone** lets you set
the bottom tone in Hz. Placement is saved separately from the modem type.
The summary shows the actual tone range. On USB those tones are above the
dial frequency; on LSB they are below it. Your radio's audio filters only
need to pass that range; stations do not need identical filter widths.
A 32×32 eight-color card takes 12.36, 24.72, or 49.44 seconds per copy,
respectively (the original Resilient mode takes 3.09 seconds). Larger canvases,
more colors, and extra copies increase the duration; the UI shows the total.
These modes retain the existing block error protection and support automatic
reception through **Receive all installed modem types**. Both stations need
a version with these modes installed. Bandwidths are nominal estimates;
live radio performance has not yet been measured. Automatic reception tries
both placement presets and the configured custom range. With automatic
reception off, RX uses the selected placement. For other custom placements,
match the receiver's setting or tune the radio to align the received tones.
This setting applies to the three narrow Resilient modes. Both stations need
the corrected one-symbol-rate tone spacing introduced with these modes.

For a local radio-TX/WebSDR-RX pair, continuous capture, replay and an optional
IP telemetry back channel, see [On-air testing](ON-AIR-TESTING.md).

## Mobile web app and control API

Enable **Mobile control** in Station settings → Options. Pixel QSO starts an
HTTP server on port 8765 (configurable there), reachable from devices on the
same network at `http://<desktop-ip>:8765/`. It serves a touch drawing page,
shows saved and exchanged cards, and can request transmission or stop an
active transmission. The API is available at `/api/status`, `/api/cards`, and
POST `/api/stage`, `/api/quickdraw`, `/api/transmit`, and `/api/stop`.

The server listens on all network interfaces while enabled. Use it only on a
trusted network; anyone who can reach it can request a transmission.

## Data2G host integration

PixelQSO uses Data2G through its supported command and KISS APIs. In Station
settings, choose a remote host or start a local host managed by PixelQSO. The
standard install includes the pinned Data2G runtime needed for local hosting;
the host runs as a separate process and owns radio audio, modem operation, and
PTT. PixelQSO talks to it only through command and KISS sockets. For local
modem modes, CAT connects to the saved rigctld endpoint on startup by default;
turn off **Connect to rigctld on startup** in Station settings to disable it.
Remote hosts remain supported. Torch is not installed because it is only needed for upstream
model training. If a local host exits during startup, PixelQSO reports the
current failure from `data2g-host.log` in its data directory. See
[on-air testing](ON-AIR-TESTING.md) and [the integration plan](docs/DATA2G-INTEGRATION-PLAN.md) for setup and remaining
validation.

The experimental weak-signal modem uses a fixed, attributed 3GPP NR LDPC table
and its own CPU decoder. It does not depend on Data2G or PyTorch. Install its
optional SciPy dependency with:

```sh
uv sync --extra weak-signal
uv run --extra weak-signal python app.py
```

Host protocol details and remaining interoperability checks are tracked in [DATA2G-BROADCAST-NOTES.md](DATA2G-BROADCAST-NOTES.md).

## Test with two stations

```sh
python tools/open_two_clients.py
```

This opens two isolated Pixel QSO instances connected by simulated audio. No radio, CAT, PTT, or audio hardware is required.

## Build

```sh
python -m pip install . PyInstaller
python -m PyInstaller --clean --noconfirm pixelqso.spec
```

Builds are created in `dist/`. The same PySide6 application supports Linux, Windows, and macOS.
To package the experimental weak-signal modem, use:

```sh
PIXELQSO_FREEZE_WEAK_SIGNAL=1 uv run --extra weak-signal --with pyinstaller pyinstaller --clean --noconfirm pixelqso.spec
```

The default frozen app includes SciPy for the bundled Data2G host and excludes
the optional PixelQSO weak-signal modem. The environment flag above includes
that modem.

The default frozen build includes the locally managed Data2G server runtime.
It starts as a child process and uses the same supported command and KISS
interfaces as a remote server; Torch and GPU training packages are excluded.

## Modem test

For comparisons across modem backends, seeded path impairments, and recorded
on-air WAVs, see [BENCHMARKING.md](BENCHMARKING.md). The benchmark generates
trial CSVs, JSON results, and an HTML comparison report:

```sh
python tools/modem_benchmark.py --backends fast_avatar fast_avatar_fec --profiles clean awgn --snr-db 12 6 0 --repeats 1 3 --trials 5
```

```sh
python tools/two_client_loopback.py --mode resilient --canvas 16 --colors 16 --repeats 2
```

Use `--mode fast` to test the simpler unprotected burst mode.

Synthetic and loopback tests verify software behavior only; they do not establish on-air performance or regulatory compliance.
# Experimental weak-signal cards

The optional **Weak signal · 32×32 · experimental** transmission type carries a
checked 32×32 eight-color card in 4.622 seconds using shaped QPSK and LDPC.
For weaker signals, **Weak signal · combined copies · experimental** sends
1–8 identical bursts and combines soft bit evidence at the receiver. Start with
four copies (18.488 seconds of audio). Both formats require the `weak-signal`
extra and retain whole-card integrity checks; neither provides partial images.
Install `uv sync --extra weak-signal` to enable it. See
[the modem format, limits and measurements](WEAK-SIGNAL-MODEM.md).

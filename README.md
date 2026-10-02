# Pixel QSO

Pixel QSO is a desktop application for sending small pixel-art QSO/QSL cards over radio. Transfers are one-way and can be repeated so the receiver can combine successfully decoded data from multiple copies.

Requires Python 3.10+.

With uv, the default environment contains only the desktop app and its standard
modem backends. Plain `uv sync` does not install optional extras; the lockfile
can list their resolutions without adding them to the base environment:

```sh
uv sync
uv run python app.py
```

The equivalent base install with pip is shown below. `requirements-app.txt`
deliberately contains no Data2G, SciPy, or PyTorch dependencies.

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

## Mobile web app and control API

Enable **Mobile control** in Station settings → Options. Pixel QSO starts an
HTTP server on port 8765 (configurable there), reachable from devices on the
same network at `http://<desktop-ip>:8765/`. It serves a touch drawing page,
shows saved and exchanged cards, and can request transmission or stop an
active transmission. The API is available at `/api/status`, `/api/cards`, and
POST `/api/stage`, `/api/quickdraw`, `/api/transmit`, and `/api/stop`.

The server listens on all network interfaces while enabled. Use it only on a
trusted network; anyone who can reach it can request a transmission.

## Experimental Data2G backend

Data2G is opt-in because its decoder needs PyTorch and can install large,
platform-specific runtime packages. Enable it only when you want the Data2G
transmission choices:

```sh
uv sync --extra data2g
uv run --extra data2g python app.py
```

This enables Data2G fast and robust choices within its 1.2 kHz band. Choose a
Data2G option on both stations; either choice can receive both included
Data2G submodes. The card, CQ/Exchange/73 stages, one-way copy count, and QSO
log work the same way. A decoded Data2G burst contributes checked image
fragments to the live card. Details are in [DATA2G-BACKEND.md](DATA2G-BACKEND.md).

The separate experimental weak-signal modem uses Data2G's LDPC code
construction but has its own CPU decoder, so it does not need PyTorch. Install
only its lighter optional set with:

```sh
uv sync --extra weak-signal
uv run --extra weak-signal python app.py
```

The proposed host-based broadcast integration, implementation notes, and open questions are tracked in [DATA2G-BROADCAST-NOTES.md](DATA2G-BROADCAST-NOTES.md).

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
This is the base build. To include the experimental Data2G backend, build from
its optional environment:

```sh
uv sync --extra data2g
uv run --extra data2g pyinstaller --clean --noconfirm pixelqso.spec
```

That bundle will be substantially larger because it includes Data2G and
PyTorch. To package the weak-signal experiment without PyTorch, use:

```sh
uv sync --extra weak-signal
uv run --extra weak-signal pyinstaller --clean --noconfirm pixelqso.spec
```

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
Install `uv sync --extra weak-signal` to enable it without installing
PyTorch. See
[the modem format, limits and measurements](WEAK-SIGNAL-MODEM.md).

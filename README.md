# Pixel QSO

Pixel QSO is a desktop application for sending small pixel-art QSO/QSL cards over radio. Transfers are one-way and can be repeated so the receiver can combine successfully decoded data from multiple copies.

Requires Python 3.10+.

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

## Experimental Data2G backend

Install the optional modem with `uv sync --extra data2g`, then run `uv run --extra data2g python app.py`. The transmission menu then offers Data2G fast and robust choices within its 1.2 kHz band. Choose a Data2G option on both stations; either choice can receive both included Data2G submodes. The card, CQ/Exchange/73 stages, one-way copy count, and QSO log work the same way. A decoded Data2G burst contributes checked image fragments to the live card. Details are in [DATA2G-BACKEND.md](DATA2G-BACKEND.md).

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

## Modem test

```sh
python tools/two_client_loopback.py --mode resilient --canvas 16 --colors 16 --repeats 2
```

Use `--mode fast` to test the simpler unprotected burst mode.

Synthetic and loopback tests verify software behavior only; they do not establish on-air performance or regulatory compliance.

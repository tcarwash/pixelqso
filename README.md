# Pixel QSO

Pixel QSO is a desktop application for sending small pixel-art QSO/QSL cards over radio. Transfers are one-way and can be repeated so the receiver can combine successfully decoded data from multiple copies.

## Run

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

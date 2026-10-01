# Pixel QSO card modem prototype

Pixel QSO is a native desktop application and modem prototype for small radio image cards. Each transfer is one-way: send a configured number of copies and combine independently checked blocks at the receiver. Cards use 32×32 by default; the editor also offers 16×16 and 64×64 and shared 8-, 16-, and 32-color palettes.

Read [GOALS.md](GOALS.md) for the complete product goals and [SPEC.md](SPEC.md) for engineering requirements and implementation gaps. These documents supersede earlier grayscale/color stages and ACK-driven retries. Optimize exact-card delivery time by measuring repeat count and block recovery.

Both burst types include a protected identity header that identifies payload type, canvas dimensions, and shared palette. The receiver detects the type from the burst, so the sender and receiver do not have to choose matching receive modes. On a clean synthetic channel, a 16×16 8-color resilient copy takes about 1.00 second; the default 32×32 8-color resilient copy takes about 3.07 seconds. Larger palettes and canvases take longer. The raw type has no pixel FEC or integrity check and is labeled unverified. These are symbol-count estimates, not measured on-air performance claims.

## Run the reference example

Requires Python 3.10+ and NumPy.

```sh
python3 cardmodem.py demo
```

This writes WAV transmissions/captures, SVG sent/received views, and JSON examples into `examples/`. The receiver uses a rate-1/2 K=7 convolutional code with soft-decision Viterbi decoding and per-frame interleaving; `--state` accumulates soft evidence across listening sessions. Examples are deterministic synthetic audio, not radio recordings. For PNG previews, install Pillow and run `python3 render_examples.py`.

## Desktop application

Install `requirements-app.txt`, then run `python3 app.py`. The main window has **QSO**, **My Cards**, and **QSO Log** sections. Create or edit a card from **My Cards**; the editor starts blank and supports 16×16, 32×32, and 64×64 canvases, 8-, 16-, and 32-color palettes, pixel drawing, pixel text, and transparent draggable stamp layers. Cards and received QSL images are stored locally.

The QSO view offers manual, automatic, and beacon exchanges. The editor includes draw, erase, fill, color-pick, stamp-move, proportional stamp resizing, and undo/redo. Changes prompt before closing, and Ctrl+S saves the current card. The normal transmission choices are **Fast burst** (unchecked pixels) and **Resilient burst** (RS-protected, CRC-checked blocks). The **Copies** control repeats the same burst one-way, with no ACK stage. The receiver auto-detects burst type, dimensions, and palette. Manual and automatic contacts use four thumbnail card selectors: CQ, Exchange, 73 with SNR, and final 73. Manual mode sends the selected stage when you click Transmit. Automatic mode offers Call-first and Reply to CQ roles; Call CQ repeats on a configurable interval, and received CQs show a Reply to CQ action. The QSO log groups CQ, Exchange, and both 73 cards in order as one contact deck. The live image keeps previously recovered pixels and verified blocks while newer audio is decoded. Legacy packet/MFSK cards are hidden behind **Show experimental legacy packet / MFSK modes** in Station settings. Direct Transmit/Beacon actions and starting the supervised exchange authorize transmission without another confirmation dialog; CAT frequency and upper-sideband mode are read back before PTT. RF behavior and occupied bandwidth have not been verified here.

To open two independent full application windows for operator testing, run `python3 tools/open_two_clients.py`. It starts separate station A and B processes with isolated libraries, logs, preset demo cards, and a software audio link between the apps. This test mode bypasses CAT/PTT and requires no audio devices. By default the profiles live in `~/PixelQSO-Two-Client-Test`; choose another location with `--data-dir`, and set identities with `--callsigns W7PXQ K6TEST --grids CN87 CM87`. In both windows choose **Automatic exchange**, set station A to **Call-first** and station B to **Reply to CQ**, start the automatic exchange on station B, then enable **Call CQ** on station A. The launcher stays open until both app windows close; Ctrl+C closes them. For normal radio operation, turn off **Audio-only test mode** in Station settings.

## Build a desktop application

Native builds are produced on their target operating system. On Linux, Windows, or macOS, install Python 3.10+ and run:

```sh
python -m pip install . PyInstaller
python -m PyInstaller --clean --noconfirm pixelqso.spec
```

The application is written with Qt/PySide6 and the same source runs on all three platforms. Linux and Windows builds place `PixelQSO`/`PixelQSO.exe` in `dist/`; macOS produces `dist/PixelQSO.app`. The workflow in `.github/workflows/build-desktop.yml` builds and uploads native artifacts on Linux x86-64, Windows x86-64, and Apple Silicon macOS. Hamlib `rigctld` remains a separate install, as it must match the user's radio and CAT setup. Unsigned builds may show the operating system's normal first-run warning.

## Two-station and audio-loopback checks

`python3 tools/two_client_loopback.py --mode resilient --canvas 16 --colors 16 --repeats 2` sends two one-way burst copies between two simulated stations. Add `--snr-db 8 --cfo-hz 2 --clock-ppm 80 --drop-start .05 --drop-end .10` to impair the sample stream. It reports verified block recovery and total airtime; `--mode fast` runs the unchecked raw-pixel type. For physical or virtual sound-card loopback, add `--audio-loopback --tx-device N --rx-device N` after installing `python3 -m pip install '.[loopback]'`. These checks do not establish on-air weak-signal performance or regulatory compliance.

The multi-window UI launcher is `python3 tools/open_two_clients.py`. It creates two isolated app windows with a software audio link and preset cards; neither CAT nor audio hardware is needed. For an actual sound device, use the loopback option in the burst test script above. UI, impairment, and RF checks measure different parts of the system; synthetic results are not radio performance evidence.

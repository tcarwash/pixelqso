# Two-window radio TX / WebSDR RX tests

The launcher creates isolated TX and RX windows, a shared session, and an
authenticated local telemetry collector. **The software audio link is off.**
No transmission starts automatically. The WebSDR window is receive-only.

```sh
.venv/bin/python tools/open_two_clients.py --list-audio-devices
.venv/bin/python tools/open_two_clients.py --on-air \
  --data-dir "$HOME/PixelQSO-On-Air" \
  --session first-air-test
```

Station A defaults to AG7SU / CN85; station B defaults to K6TEST / CM87. Pass
`--callsigns A B --grids A B` when you need different identities. On Linux with PipeWire/PulseAudio and
Chrome/Chromium, the launcher automatically creates a dedicated virtual sink
and a Qt-visible capture input, opens a separate browser profile routed to
that sink, and selects the input in B. Use the browser window it opens. The
virtual input is named `PixelQSO-WebSDR-RX-...`. System defaults and existing
browser streams are unchanged. On exit, the launcher closes its browser and
removes its virtual devices; browser profile files remain under `browser-profiles`.

For local PixelQSO modem tests, configure Hamlib/CAT and the radio output in A,
or pass `--tx-device`.
`--websdr-url URL` opens a receiver directly; otherwise the browser opens the
WebSDR directory. `--browser EXECUTABLE` chooses Chrome/Chromium explicitly.
Tune the receiver and click its audio-start button in that dedicated browser.

For an existing browser or another platform, use `--manual-loopback` and
select an input carrying **the browser's output**, such as a monitor, virtual
cable or hardware loopback. Select it in Station settings or pass its exact
Qt description with `--rx-device` (which also selects manual routing).
Unavailable names cause an error; the default microphone is not a WebSDR input.

## On-air test with Data2G

Use this when Data2G owns the radio and WebSDR receiver audio path. Supply two
already-running, separate Data2G host instances: station A transmits and
station B listens. The launcher connects both PixelQSO windows to those hosts
and selects the same Data2G preset automatically.

```sh
.venv/bin/python tools/open_two_clients.py --on-air \
  --data-dir "$HOME/PixelQSO-Data2G-Air" \
  --websdr-url 'https://your-websdr.example/' \
  --data2g-hosts RADIO_HOST SDR_HOST \
  --data2g-command-ports 8300 8300 \
  --data2g-kiss-ports 8100 8100 \
  --data2g-mode robust
```

Use `--data2g-mode fast` to test the faster 16-QAM preset. The hosts may run
on the same machine if they use separate ports and independent audio devices.
Configure station A's host with the radio, CAT/PTT, and transmit audio.
Configure station B's host to receive the WebSDR browser audio. With automatic
loopback, select the printed `PixelQSO-WebSDR-RX-...` source in the Data2G
receiver host; with `--manual-loopback`, route the browser to an existing input
and select that input in Data2G. PixelQSO does not capture a second copy of the
audio in this mode. The host must already be configured for its audio devices;
the launcher does not restart or reconfigure Data2G.

The command and KISS ports are per station and can differ. Avoid pointing both
windows at a single-client Data2G instance: its command connection owns the
broadcast session. Host ACKMODE and receive-status events are recorded in the
on-air session. PixelQSO does not duplicate the Data2G-owned raw WebSDR PCM;
the local host stores per-burst audio recordings under its station data folder.

To have each PixelQSO window start its own local Data2G server, use
`--data2g-local-hosts` instead of `--data2g-hosts`:

```sh
uv sync
uv run python tools/open_two_clients.py --on-air \
  --data2g-local-hosts --data2g-mode robust \
  --data-dir "$HOME/PixelQSO-Data2G-Air"
```

The launcher assigns separate command/data/KISS ports to the two processes.
Station A uses the selected radio output and configured rigctld host/port;
station B uses the WebSDR loopback as its Data2G input. Each server stops when
its PixelQSO window closes. Host audio recordings and logs stay under each
station's PixelQSO data directory.

Open and tune your WebSDR, select its sideband/audio filter, and enable audio.
The launcher does not automate tuning or depend
on a site's private streaming protocol. This follows
[WebSDR's documented external-decoder audio workflow](https://www.websdr.org/faq.html).
Route only browser audio into B. If you capture local TX playback instead,
you are measuring loopback. Listen to an RX WAV to confirm the source.

Choose the modem/placement/copies in A and automatic reception in B (or match
its RX mode/placement). Transmit one burst at a time and allow it and the
WebSDR's buffering to finish. `--rx-filter-hz LOW HIGH` records the filter
edges you selected; it does not configure the filter. Record AGC, squelch,
gain and noise-processing settings in a session note. Stations need not have
identical filters; each must pass the tone range.

Close both windows to flush recording. Ctrl+C requests graceful shutdown.
The launcher prints the session directory.

## Data and review

Files are under `DATA_DIR/sessions/SESSION/STATION/`:

- `events.jsonl`: UTC/monotonic timestamps, TX settings and raster hash, RX
  stream/sample positions, decode reports and errors.
- TX-reference WAVs and continuous six-second RX WAV chunks, including failed
  decodes. PCM values are preserved exactly. Exact decoder snapshots are also
  saved as WAVs for replay.
- `.npz` spectra with sample positions and frequency bins. Measurements
  include RMS, peak, DC and clipped fraction at 250 ms intervals.
- Avatar decode reports include acquisition start, carrier offset, fitted
  symbol period/clock error, confidence summaries and checked/corrected blocks.
  QPSK retains its LDPC/combining diagnostics.

Analysis/compression/network writes use a background queue bounded to 32 jobs
and 64 MiB of pending PCM. Dropped jobs and disk/network errors are reported
at shutdown; sample positions reveal missing RX chunks. Network failures
retain local evidence. Disk use grows with session length.

```sh
.venv/bin/python tools/on_air_test.py report \
  --session-dir "$HOME/PixelQSO-On-Air/sessions/first-air-test" \
  --output-dir "$HOME/PixelQSO-On-Air/review/first-air-test"
```

Open `report.html`; `session.json` includes merged events and TX/RX matches.
A match requires an independently exact decode and matching identity,
dimensions, palette and raster hash. IP metadata never helps the decoder. Repeated
rasters match the latest preceding TX event; overlap or clock skew can make
that association ambiguous. Counts are matching **decode events**, not RF
success-rate statistics. TX-reference PCM is intended output, not measured RF.

Replay a capture or a PCM16 WAV downloaded from WebSDR:

```sh
.venv/bin/python tools/on_air_test.py analyze \
  --wav received.wav --mode resilient_100 --placement centered \
  --output-dir replay
```

Output includes `analysis.json`, `spectra.npz`, and (for avatar decodes)
`symbol_metrics.npz`: raw-capture per-tone energies and hard decisions using
recovered timing. Stereo PCM16 WAVs are averaged to mono. Use `--placement
custom --low-hz VALUE` for custom placement. Failed decodes still save evidence.

Standalone recording uses the optional `uv sync --extra loopback` dependency:

```sh
.venv/bin/python tools/on_air_test.py devices
.venv/bin/python tools/on_air_test.py capture \
  --device "LOOPBACK INPUT NAME" --seconds 90 --wav received.wav
```

The `.capture.json` records input-overflow counts. These device names come
from sounddevice and may differ from Qt's. Recording does not transmit.

## Remote back channel

The same annotation-only collector can run on LAN/NetBird. It cannot key,
tune or inject audio into the radio. Localhost is the default bind. Use HTTPS
through a reverse proxy for an untrusted network; the collector has no native
TLS. HTTP over an encrypted NetBird path is supported.

```sh
.venv/bin/python tools/on_air_test.py serve \
  --bind YOUR_NETBIRD_IP --port 8766 \
  --token-file session.token --output-dir collector
```

The credential file is generated with owner-only permissions and never
printed. Transfer it privately to the other client. Initialize each client
with the same session/collector/credential and a distinct station ID:

```sh
.venv/bin/python tools/on_air_test.py init \
  --config tx-session.json --output-dir captures \
  --session air-test --station tx \
  --collector http://YOUR_NETBIRD_IP:8766 --token-file session.token
PIXELQSO_ON_AIR_CONFIG=tx-session.json .venv/bin/python app.py
```

For the second client use `--station rx`. To make it receive-only, also set
`PIXELQSO_AUDIO_ROLE=websdr-rx PIXELQSO_AUDIO_TEST=1 PIXELQSO_TEST_LINK=0`.
The two-window launcher accepts `--collector` and `--token-file` too.

```sh
.venv/bin/python tools/on_air_test.py fetch \
  --config tx-session.json --output-dir peer-events
.venv/bin/python tools/on_air_test.py report \
  --session-dir peer-events --output-dir peer-review
```

Raw audio stays at the originating client; transfer it separately for replay.
Event downloads are bounded to 8 MiB; larger sessions can be reviewed from
the collector's JSONL file on disk. One credential covers cooperating test
clients; this is not a multi-user public service.

## Measurement limits and validation

Synchronize remote clocks for UTC ordering. TX-to-decode timing includes
WebSDR/browser/audio/decoder buffering and is not RF propagation delay. Levels
and spectra are uncalibrated digital audio, not RF dBm. Modem SNR retains the
decoder's definition; AGC and gain affect it. Filter/source metadata is
operator-supplied. A browser recording alone does not prove the intended RF path.

Collector/recording/replay tests and Qt/UDP capture-hook tests pass. Live
WebSDR routing and RF have not been exercised in this environment.

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python -m unittest discover -s tests
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/test_weak_signal_app.py \
  --narrow 100 --placement centered --telemetry
```

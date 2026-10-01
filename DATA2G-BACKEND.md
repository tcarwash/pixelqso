# Experimental Data2G backend

PixelQSO can use [Data2G](https://github.com/arodland/Data2G) as an optional audio backend for the same indexed-color cards and CQ/Exchange/73 exchange. The dependency is pinned in `pyproject.toml` so the experiment uses a reproducible upstream revision. Install and run it with `uv sync --extra data2g` and `uv run --extra data2g python app.py`.

## Backend boundary

`card_backends.py` defines the common card backend contract: `encode(card, repeats, message_type, snr_db)` returns 48 kHz float audio, `estimate_seconds` supplies the UI duration, `decode(audio, sample_rate)` returns the app's common card report, and `minimum_audio_seconds` controls when live decoding begins. `BACKENDS` registers the native Fast/Resilient backends and the two Data2G choices. An additional backend can be registered there without changing the card editor or log format. The existing legacy packet mode retains its older path.

The Data2G adapter converts PixelQSO's 8/16/32-color card to a packed palette-index raster. It divides those bytes among independent Data2G codewords. Each codeword has a 36-byte PixelQSO fragment header: `PQD2` magic, format version 1, card ID, width, height, palette ID, message type, SNR report, callsign, grid, fragment index/count, and CRC-16 of the full packed image. The structure is `>4sBHBBBBb12s8sBBH`. Text fields are uppercase ASCII and NUL-padded. The remainder of each codeword carries raster bytes, padded with zeros to the chosen submode's fixed payload size. Data2G supplies coding and a checksum for each codeword. The full-image CRC prevents a mixture of fragments from being marked exact.

A receiver groups checked fragments by card identity, image CRC, and submode; fragments occupy fixed byte offsets. It can fill missing fragments from later complete bursts. A pixel is displayed only when all bytes containing its index bits have arrived in checked fragments. The UI saves the card as exact only after every fragment arrives and the full-image CRC matches. The exchange remains one-way: **Copies** repeats complete Data2G bursts and never waits for an ACK. Data2G's ARQ host and KISS service are not used.

## Modes and timing

The first two choices use Data2G's nominal 1.2 kHz OFDM band:

| Choice | Data2G submode | 32×32, 8-color card, one copy |
| --- | --- | ---: |
| Robust | `qpsk-r1/2` | 6.392 s |
| Fast | `16qam-r1/2` | 2.936 s |

Those example durations include the 36-byte fragment header in every codeword and Data2G's burst framing. They are from encoded sample counts, not RF measurements. The backend resamples Data2G's 8 kHz waveform to PixelQSO's 48 kHz sound-card path. Receive resamples in the other direction. The current UI only offers these sub-2-kHz choices.

## Receiver behavior and limitations

Data2G's receiver listens continuously but delivers checked codewords after a complete burst. PixelQSO's preview can improve after each burst as further fragments arrive. A partial codeword is not shown during that burst. Automatic CQ exchanges can use this backend because each completed fragment is integrity checked, and the card's message type and optional SNR travel in the fragment header.

The experimental adapter decodes a rolling audio snapshot in a worker through Data2G's streaming `Receiver`. It can lock to a later full copy after the capture begins in the middle of an earlier one. A future implementation should keep the receiver state between audio callbacks to avoid re-decoding earlier audio. It should also compare fixed-copy fragment combining with Data2G's soft codeword combining on impaired paths.

The currently pinned upstream wheel omits its `data2g/format/*.npz` frozen format tables. It can compute fallback format data, and a clean fast-mode source-to-wheel decode succeeded locally. Other submodes and release interoperability still need confirmation with the creator and an upstream packaging fix. `pixelqso.spec` collects Data2G's package data when the extra is installed. Data2G also uses PyTorch for decoding, so the optional build will be materially larger than the base app.

This is an experimental format distinct from PixelQSO avatar wire version 3. It needs Data2G on both ends. Occupied bandwidth, RF reliability, and compatibility between packaged installs remain to be measured.

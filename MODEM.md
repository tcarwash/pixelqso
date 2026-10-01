# Pixel QSO modem, explained

Pixel QSO sends a small indexed-color image as a short digital burst. The application turns a card into protocol symbols; the current sound-card modem turns those symbols into audio and decodes them back. The main path is designed around a fixed, known canvas and shared color palettes so it can spend airtime on image pixels instead of describing every color independently.

## What goes over the air

The current avatar burst carries a protected identity header followed by the image pixels. The header identifies the card and describes its callsign, grid, dimensions, palette, message type (card, CQ, exchange, or 73), and optional SNR report. It is repeated at the start of every copy so a receiver can identify and group copies of the same card.

The normal avatar canvas is 32×32 pixels. Each pixel is a palette index, not three separately transmitted RGB values. The protocol defines shared 8-, 16-, and 32-color RGB444 palettes, so each pixel takes 3, 4, or 5 bits. A 32×32 image therefore contains 3,072, 4,096, or 5,120 pixel bits before framing and error protection. The sender maps a card's colors to the closest colors in the selected shared palette.

## Two burst choices

### Fast

Fast sends the packed pixel indices directly after the protected header. It has no pixel-level error correction or checksum, so a damaged symbol can change a pixel. The receiver can combine repeated copies by voting on each pixel position. This is the smallest and quickest form when the channel is clean.

### Resilient

Resilient divides the raster into independently protected blocks. Each block uses shortened Reed–Solomon coding over 6-bit symbols (RS(63,61), able to correct one symbol error), followed by a CRC-16 check. A block is used only if its checksum verifies. The receiver places verified blocks at their original raster positions, so it can combine good blocks from different copies without rotating or reordering the image. The header is also protected and checksummed.

Resilient costs extra airtime for parity and checksums. In return, a copy can contribute only the blocks that survived decoding; another copy can fill in different missing or damaged blocks. This is one-way repetition: the sender repeats the complete burst the configured number of times, and the receiver combines the results. There is no ACK or request for a retransmission. The UI stops treating a card as new after it has been fully received and saved.

For an illustrative 32×32, 8-color card at the current profile, the code estimates about 2.9 seconds for one Fast copy and 3.1 seconds for one Resilient copy. Three copies take about 8.6 and 9.3 seconds respectively. These are symbol-count estimates for the current implementation, not measured radio airtime or weak-signal performance.

## The current audio modem

The avatar modem uses 8-FSK: each audio symbol selects one of eight tones and carries three bits. Its current profile is 400 symbols per second, with tone frequencies from 900 to 2300 Hz in 200 Hz steps. The profile is labelled approximately 1.8 kHz wide in the application; actual occupied bandwidth and on-air behavior require measurement.

The receiver searches for the burst's alternating acquisition pattern and sync word, estimates timing and frequency offset, measures the tone energy, and converts detected tones back into protocol symbols. Fast and Resilient avatar decoding currently make hard tone decisions before unpacking pixels or checking blocks. The older experimental packet modem has a separate 4-FSK path with convolutional coding, interleaving, and soft-decision Viterbi decoding; it is not the main avatar format.

## Protocol and transport

The card format and burst contents are conceptually separate from the user's station controls, QSO log, and card editor. The app can already send and receive modem audio through local audio devices, and it includes a software audio link for two-window testing. However, the current avatar implementation is not yet a fully transport-independent protocol library: its sync, framing, and symbol mapping are implemented alongside the 8-FSK audio modem. Other transports such as a different FSK/PSK modem or embedding in another mode's preamble/tail would need an adapter and agreed symbol/framing behavior.

## What reliability means here

- Fast reduces airtime and combines repeated pixel guesses, but cannot identify or repair corrupted pixels.
- Resilient repairs up to one 6-bit symbol error per block and rejects blocks whose CRC fails; repetition lets the receiver collect different valid blocks.
- Header protection helps recover the card identity and image format, but a burst with an undecodable header cannot be safely associated with its pixels.
- A lost copy is tolerated when other copies deliver its blocks. There is no two-way recovery when every copy of a block is lost.
- Current synthetic channel and local-loopback checks are useful for software development. They do not demonstrate real-radio performance, compatibility with other modes, or regulatory compliance.

## In the code

- `minimal_avatar_symbols` packs and frames the Fast avatar burst.
- `minimal_avatar_resilient_cycle_symbols` builds the protected Resilient burst.
- `decode_minimal_avatar_audio_auto` detects the burst type from its protected header and selects the matching decoder.
- `MINIMAL_AVATAR_PROFILE` defines the current 8-FSK tone set and symbol rate.
- `tools/two_client_loopback.py` exercises repeated bursts with simulated loss and channel impairments.

The on-air format and its measured performance are still evolving. Treat these details as a description of the current source, not a frozen interoperability standard.

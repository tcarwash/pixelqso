# Pixel QSO: artwork-first operating model

## Direction

Pixel QSO began as SSTV-like image sharing combined with FT8-like short
structured messages. Casual users seem to enjoy sending cards back and forth,
even when they do not complete formal four-stage contacts. A CQ-heavy user
experience is not proof the visual-radio idea has failed; it suggests that
the previous interface made CQ more obvious than sharing artwork.

The primary experience is now a shared over-the-air gallery of pixel art.

## Three activities

1. **Share art (default):** Send a one-shot broadcast card frame. No CQ,
   destination, acknowledgment or completed QSO is implied. The wire protocol
   already supports standalone card messages.
2. **Discover:** Listen, receive and focus artwork from stations on air.
   Periodic CQ is available but secondary, with a 60-second default repeat
   interval instead of 5 seconds on fresh configurations.
3. **Structured contact (optional):** CQ / Exchange / RR73 / 73 remains
   available through an expandable panel. The separate automatic mode remains
   unavailable in the normal UI; this change does not silently enable it.

Heard artwork is valuable without a completed exchange. Contacts defaults to
Stations heard, grouped by callsign, and offers Activity sessions for
chronological records including unconfirmed broadcasts.

## Safety and semantics

- Selecting a card, opening a deck or focusing a received card does not key PTT.
- All immediate transmissions have explicit send labels.
- One-shot art is *broadcast*: it is not addressed privately to the station
  whose art inspired the response. The UI must not claim remote delivery.
- Audio-test and Data2G host ACKs do not imply successful remote reception.
- Provisional images remain clearly distinct from CRC-verified images.
- Retain existing CAT/PTT preflights, card integrity checking, reception
  reports, retries and cancellation behavior.
- Physical radio validation is separate from the isolated noVNC UI sandbox.

## Potential future work

- Cardcasting loop: tune/listen, receive a live image, queue the next image,
  send it and archive what was heard, without entering a QSO state machine.
- Optional *directed* card metadata in a backward-compatible protocol
  extension; do not infer it from the timing of consecutive broadcasts.
- Saved art deck with distinct sent/heard/verified/confirmed statuses,
  separate from conventional contact-log completion.
- Low-duty-cycle discovery alternatives, with user-controlled repeat settings.
- Measure airtime, startup latency, receive robustness and image recovery
  across available modes and card sizes on real radios.
- Observe casual operators trying to send several different cards and
  respond after a received artwork tile; record whether the interface
  explains what was transmitted and what was actually received.

This is a product and UI change based on existing wire capabilities. It is
not a new modulation mode or a demonstrated fully automatic FT8-style link.

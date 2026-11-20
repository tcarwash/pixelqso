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

Heard artwork is valuable without a completed exchange. Contacts defaults
to Recent cards, ordered by time, and also offers Stations heard (grouped by
callsign) and Activity sessions (including unconfirmed broadcasts).

## Cardcasting workflow

- Save or personalize several cards under My Cards, then add them using
  **Add to on-air queue**. The ordered playlist is persisted locally in
  settings. Deleted/missing saved cards are pruned automatically.
- On Operate, select and load a queued card to make it the current outgoing
  artwork. This is not a transmit action.
- Use **Send card** to broadcast the current art, or **Send selected** on the
  queue to manually transmit the chosen item once. Entries are not removed
  or advanced on a failed attempt or an unconfirmed host ACK.
- Move queued entries up/down, duplicate by queueing again, or remove an entry
  without deleting the original saved card. There is no background send loop.
- The recent-session gallery shows verified/provisional state and station
  identity on each card. Selecting received art focuses it without
  transmitting. **Draw something new** prepares a card for later manual
  transmission, and **See this station** opens the saved reception archive.
- The persistent Recent cards view works even after an application restart:
  it reads recorded receptions from disk rather than relying only on the
  in-memory wall.


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

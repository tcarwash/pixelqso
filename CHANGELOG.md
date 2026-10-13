# Changelog

## 0.4.0b1 — Beta 1

- First public beta candidate for Pixel QSO's card exchange, station setup, and QSO history workflows.
- Disable compressed 8-FSK, compressed 16-FSK, and UTC-grid 16-FSK for transmission. Their wire formats remain in the receive decoder for compatibility. Saved selections move to the supported resilient 8-FSK default.
- Show rig frequency as read-only; remove unused transfer bandwidth and grid audio-delay controls.
- Move Data2G/Experimental backend selection into Audio & Radio settings, where changes apply or cancel with station setup.
- Keep identified provisional cards on the session wall and in the QSO deck, with clear integrity state and a flip view.
- Rename the “Hello Grid” starter card to “Mountain Signal”; remove the first-run guide and Configure beacon menu shortcut.
- Keep Data2G as the default backend and preserve existing card, station, and QSO data formats.

### Beta limits

- This is beta software. Confirm station configuration, audio routing, CAT/PTT behavior, and receive/transmit behavior with your station before relying on it.
- Experimental local modem modes and Data2G host configurations have different compatibility and hardware requirements. A successful connection does not establish audio, PTT, or RF operation.
- The remaining release checks are a clean environment build, packaged first-launch review, and operator/hardware validation on each supported platform.

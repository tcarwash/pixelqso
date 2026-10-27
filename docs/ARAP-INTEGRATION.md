# ARAP integration

PixelQSO currently has no ARAP user interface or runtime dependency. ARAP
signing, verification, and keyserver operations remain available through the
standalone ARAP application and CLI. This keeps identity proofs separate from
PixelQSO's card editor, library, modem payload, and radio controls while the
integration design is being simplified.

No ARAP proof or reference sidecars are read, written, or verified by PixelQSO.
The modem's receive integrity checks remain independent of station identity and
signatures. A valid signature alone does not show that a station transmitted a
card or that a QSO occurred.

Any future PixelQSO integration should have one deliberate entry point and a
clear data/API boundary before adding editor or library controls.

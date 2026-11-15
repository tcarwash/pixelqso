# Pixel QSO Card Tools for Aseprite

Install `pixelqso-card-tools.aseprite-extension` using **Edit → Preferences → Extensions → Add Extension**, then restart Aseprite if necessary. The commands appear under **File → Scripts → Pixel QSO**.

1. Choose **New Card…** and select 16, 32, or 64 pixels, 2, 8, 16, or 32 colors, and one or two frames.
2. Draw using the supplied palette. Keep the canvas opaque and use only its palette colors. Save an `.aseprite` file to retain layers and editable frames.
3. Choose **Export Card…**. Enter the station identity, the palette size, and unique decimal card IDs. For two frames, choose a duration from 100 to 10000 ms. Choose the first JSON filename and export.
4. Open the resulting JSON in Pixel QSO. A two-frame export also creates `-frame2.json` beside the first file; keep both together. The second card references the first by ID and artwork fingerprint.

The export renders visible layers as the card artwork. It requires exactly one or two frames as selected, checks every pixel against the selected shared palette, and refuses to overwrite existing files. Re-export using a new filename. Card IDs are initialized randomly; make sure they do not collide with your existing cards. Animation uses one shared duration for both frames. Two-color animation requires the Pixel QSO version containing the corresponding monochrome animation support.

Aseprite project files and Pixel QSO JSON are separate: the JSON export does not preserve Aseprite layers or the editable project. Some transmission modes support only particular sizes and palettes; choose a compatible mode in Pixel QSO.

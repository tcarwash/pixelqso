#!/usr/bin/env python3
"""Render the SVG modem examples as shareable PNG cards (requires Pillow)."""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from cardmodem import decode_wav, from_json, reconstruct

HERE = Path(__file__).parent
EXAMPLES = HERE / "examples"
FONT = ImageFont.load_default(size=16)
SMALL = ImageFont.load_default(size=12)
BOLD = ImageFont.load_default(size=20)


def render(path: Path, callsign: str, grid: str, status: str, pixels: list[list[tuple[int,int,int]]]):
    im = Image.new("RGB", (800, 560), "#101812")
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((18, 18, 782, 542), 15, fill="#17231d", outline="#52705a", width=2)
    d.text((48, 43), "PIXEL QSO / RECEIVER PREVIEW", fill="#c5f36b", font=SMALL)
    d.text((48, 87), callsign or "—", fill="#edf1e8", font=BOLD)
    d.text((48, 119), f"GRID {grid or '-'} / W7PXQ FIELD CARD", fill="#96a89a", font=SMALL)
    tile = Image.new("RGB", (32, 32))
    tile.putdata([tuple(v * 17 for v in pixels[y][x]) for y in range(32) for x in range(32)])
    im.paste(tile.resize((320, 320), Image.Resampling.NEAREST), (50, 157))
    d = ImageDraw.Draw(im)
    d.rectangle((49, 156, 370, 477), outline="#c5f36b")
    d.text((420, 178), status.upper(), fill="#c5f36b", font=SMALL)
    d.text((420, 214), "32 X 32 PIXELS", fill="#a7b4a9", font=SMALL)
    d.text((420, 240), "8-COLOR PALETTE", fill="#a7b4a9", font=SMALL)
    d.text((420, 266), "RGB444 SWATCHES", fill="#a7b4a9", font=SMALL)
    d.text((50, 507), "B/W -> GRAYSCALE -> COLOR -> EXACT PIXELS", fill="#91a095", font=SMALL)
    im.save(path)


def main():
    card = from_json(EXAMPLES / "w7pxq-card.json")
    sent = [[card.palette[card.pixels[y * 32 + x]] for x in range(32)] for y in range(32)]
    render(EXAMPLES / "sent-card.png", card.callsign, card.grid, "sent / exact 8 colors", sent)
    report = json.loads((EXAMPLES / "examples.json").read_text())
    for item in report["received_examples"]:
        packets, _, _ = decode_wav(EXAMPLES / item["capture"])
        meta, image = reconstruct(packets)
        render(EXAMPLES / f"received-{item['name']}.png", meta["callsign"], meta["grid"], meta["color_stage"], image)


if __name__ == "__main__":
    main()

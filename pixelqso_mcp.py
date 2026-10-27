"""MCP interface for reading PixelQSO and authoring PixelQSO cards."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage, ImageDraw

PROJECT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("PIXELQSO_DATA", Path.home() / ".local/share/PixelQSO")).expanduser().resolve()
STARTERS = PROJECT / "examples" / "starter-cards"
PALETTE = [(0, 0, 0), (0, 0, 8), (0, 8, 0), (0, 8, 8),
           (8, 0, 0), (8, 0, 8), (8, 8, 0), (8, 8, 8)]
mcp = FastMCP("PixelQSO")


@mcp.tool()
def list_project_files(directory: str = ".", limit: int = 500) -> list[str]:
    """List files under the project or PixelQSO data directory, up to limit entries."""
    if not 1 <= limit <= 5000: raise ValueError("limit must be in 1..5000")
    p = Path(directory).expanduser().resolve()
    if not any(p == root or root in p.parents for root in (PROJECT, DATA)) or not p.is_dir():
        raise ValueError("Directory must be inside the PixelQSO project or data directory")
    return [str(f) for f in sorted(p.rglob("*")) if f.is_file()][:limit]


def _card(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    w, h = int(data.get("width", 32)), int(data.get("height", 32))
    palette, pixels = data["palette"], data["pixels"]
    if not (1 <= w <= 255 and 1 <= h <= 255 and len(pixels) == w*h):
        raise ValueError(f"Invalid card raster in {path}")
    if len(palette) not in (2, 8, 16, 32) or any(not isinstance(p, int) or p < 0 or p >= len(palette) for p in pixels):
        raise ValueError(f"Invalid card palette or pixel indices in {path}")
    return data


def _render(data: dict, scale: int = 8) -> bytes:
    w, h = data.get("width", 32), data.get("height", 32)
    im = PILImage.new("RGB", (w, h))
    colors = [tuple(int(c)*17 for c in rgb) for rgb in data["palette"]]
    im.putdata([colors[i] for i in data["pixels"]])
    im = im.resize((w*scale, h*scale), PILImage.Resampling.NEAREST)
    buf = io.BytesIO(); im.save(buf, format="PNG"); return buf.getvalue()


@mcp.tool()
def list_cards() -> list[dict[str, Any]]:
    """List personal, sent, and bundled starter cards with identifying metadata."""
    result = []
    for category, folder in (("personal", DATA/"cards"), ("sent", DATA/"sent-cards"), ("starter", STARTERS)):
        if folder.is_dir():
            for p in sorted(folder.glob("*.json")):
                try:
                    d = _card(p); result.append({"category": category, "name": p.name, "path": str(p),
                        "callsign": d.get("callsign", ""), "grid": d.get("grid", ""),
                        "width": d.get("width", 32), "height": d.get("height", 32), "card_id": d.get("card_id")})
                except (ValueError, KeyError, OSError, json.JSONDecodeError):
                    continue
    return result


@mcp.tool()
def read_project_file(path: str) -> str:
    """Read a UTF-8 project or PixelQSO data file. Paths must stay inside those roots."""
    p = Path(path).expanduser().resolve()
    if not any(p == root or root in p.parents for root in (PROJECT, DATA)) or not p.is_file():
        raise ValueError("Path must name a file inside the PixelQSO project or data directory")
    return p.read_text(encoding="utf-8")


@mcp.tool()
def get_card(name: str) -> dict[str, Any]:
    """Read a card's full metadata, palette, and row-major palette-index pixels."""
    matches = [Path(c["path"]) for c in list_cards() if c["name"] == name or c["path"] == name]
    if len(matches) != 1: raise ValueError("Card name must match exactly one listed card")
    return _card(matches[0])


@mcp.tool()
def view_card(name: str, scale: int = 8) -> Image:
    """Render a card as a sharp nearest-neighbor PNG image for visual inspection."""
    if not 1 <= scale <= 24: raise ValueError("scale must be in 1..24")
    return Image(data=_render(get_card(name), scale), format="png")


@mcp.tool()
def create_card(callsign: str, grid: str, pixels: list[int], palette: list[list[int]] | None = None,
                width: int = 32, height: int = 32, card_id: int = 0xC0DE,
                filename: str | None = None) -> dict[str, Any]:
    """Create a card from code: supply row-major palette indices and RGB444 palette triples."""
    palette = palette or PALETTE
    d = {"callsign": callsign, "grid": grid, "palette": palette, "pixels": pixels,
         "card_id": card_id, "width": width, "height": height, "animation": None}
    # Validate against the application's authoritative card model.
    from cardmodem import Card
    Card(callsign, grid, [tuple(c) for c in palette], pixels, card_id, width, height).validate()
    target = (DATA/"cards"/(filename or f"{callsign or 'card'}-{card_id:04X}.json")).resolve()
    if DATA/"cards" not in target.parents or target.suffix.lower() != ".json":
        raise ValueError("filename must be a JSON filename inside the personal cards folder")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists(): raise ValueError(f"Card already exists: {target.name}")
    target.write_text(json.dumps(d, indent=2)+"\n", encoding="utf-8")
    return {"path": str(target), "width": width, "height": height,
            "preview_tool": "view_card", "preview_name": target.name}


@mcp.tool()
def draw_card(callsign: str, grid: str, commands: list[dict[str, Any]], width: int = 32,
              height: int = 32, filename: str | None = None) -> dict[str, Any]:
    """Create a canvas card using pixel, line, rectangle, or ellipse commands; later commands draw on top."""
    from PIL import ImageDraw
    rgb = PILImage.new("RGB", (width, height), tuple(v*17 for v in PALETTE[0]))
    dr = ImageDraw.Draw(rgb)
    for c in commands:
        color_index = int(c.get("color", 7))
        if not 0 <= color_index < len(PALETTE): raise ValueError("color index outside default palette")
        color = tuple(v*17 for v in PALETTE[color_index]); kind = c.get("tool")
        if kind == "pixel": dr.point((int(c["x"]), int(c["y"])), fill=color)
        elif kind == "line": dr.line((int(c["x1"]), int(c["y1"]), int(c["x2"]), int(c["y2"])), fill=color)
        elif kind in ("rectangle", "ellipse"):
            fn = dr.rectangle if kind == "rectangle" else dr.ellipse
            box = (int(c["x1"]), int(c["y1"]), int(c["x2"]), int(c["y2"]))
            fn(box, fill=color if c.get("filled", False) else None, outline=color)
        else: raise ValueError(f"Unsupported drawing tool: {kind}")
    # Keep the shared default palette and encode each raster pixel by its nearest swatch.
    pal = list(PALETTE); pixels = [min(range(len(pal)), key=lambda i: sum((pal[i][k]*17-rgb.getpixel((j%width,j//width))[k])**2 for k in range(3))) for j in range(width*height)]
    return create_card(callsign, grid, pixels, pal, width, height, filename=filename)


if __name__ == "__main__":
    mcp.run()


def main() -> None:
    mcp.run()

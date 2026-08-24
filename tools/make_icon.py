"""Generate assets/icon.ico with no image-library dependency.

Writes 256/128/64/48/32/16 px PNG frames into an ICO container (PNG-in-ICO has
been supported since Vista). Pure zlib + struct, so it runs anywhere.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "icon.ico"

BG = (21, 26, 33)
BG_EDGE = (38, 46, 57)
ARROW = (76, 141, 255)
SIZES = (256, 128, 64, 48, 32, 16)


def rounded(x: float, y: float, n: float, radius: float) -> bool:
    """Inside a rounded square spanning 0..n?"""
    r = radius
    cx = min(max(x, r), n - r)
    cy = min(max(y, r), n - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(n: int) -> bytes:
    """RGBA rows for one frame, drawn at 3x and box-filtered for smooth edges."""
    ss = 3
    big = n * ss
    radius = big * 0.22

    # Download glyph: a vertical bar with an arrowhead, over a baseline tray.
    shaft_w = big * 0.10
    shaft_top = big * 0.20
    shaft_bot = big * 0.50
    head_half = big * 0.20
    head_tip = big * 0.66
    tray_y = big * 0.74
    tray_h = big * 0.075
    tray_half = big * 0.26
    cx = big / 2

    rows = []
    for py in range(n):
        row = bytearray()
        for px in range(n):
            r = g = b = a = 0
            for sy in range(ss):
                for sx in range(ss):
                    X = px * ss + sx + 0.5
                    Y = py * ss + sy + 0.5
                    if not rounded(X, Y, big, radius):
                        continue
                    edge = not rounded(X, Y, big, radius - big * 0.012)
                    dx = abs(X - cx)
                    on_shaft = dx <= shaft_w and shaft_top <= Y <= shaft_bot
                    on_head = (shaft_bot <= Y <= head_tip
                               and dx <= head_half * (1 - (Y - shaft_bot) / (head_tip - shaft_bot)))
                    on_tray = tray_y <= Y <= tray_y + tray_h and dx <= tray_half
                    if on_shaft or on_head or on_tray:
                        c = ARROW
                    elif edge:
                        c = BG_EDGE
                    else:
                        c = BG
                    r += c[0]
                    g += c[1]
                    b += c[2]
                    a += 255
            total = ss * ss
            if a:
                # un-premultiply against covered samples so edges stay coloured
                covered = a // 255
                row += bytes((r // covered, g // covered, b // covered, a // total))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return b"".join(b"\0" + r for r in rows)


def png(n: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main() -> None:
    frames = [(n, png(n, render(n))) for n in SIZES]
    OUT.parent.mkdir(parents=True, exist_ok=True)

    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = len(header) + 16 * len(frames)
    entries, blobs = b"", b""
    for n, data in frames:
        entries += struct.pack("<BBBBHHII", n if n < 256 else 0, n if n < 256 else 0,
                               0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    OUT.write_bytes(header + entries + blobs)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.1f} KB, {len(frames)} sizes)")


if __name__ == "__main__":
    main()

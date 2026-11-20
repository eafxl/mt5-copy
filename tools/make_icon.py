"""Rasterize the MetaCT app icon: src/gui/icons/app_icon.svg -> PNG + multi-size ICO.

WHY A RASTERIZER AND NOT A DRAWING
    The previous version of this tool *was* the icon: the mark existed only as
    analytic shapes in Python, so changing it meant editing code. The mark is
    now an SVG, which is the form a designer can open, so this tool's only job
    is to produce the two raster artefacts Windows insists on:

      * ``app_icon.ico`` — what the EXE embeds and what the taskbar reads. It
        must be multi-size: Windows picks a bitmap per context and scaling a
        256px image down to 16px itself gives a muddier result than rendering
        the vector at 16px does.
      * ``app_icon.png`` — 256px, for anything that will not read an .ico.

    The SVG stays on disk too; Qt uses it directly at arbitrary sizes.

Rendering is Qt's own SVG renderer, so what the builder bakes in is exactly
what the running app draws. The ICO container is still assembled by hand —
PNG-in-ICO is a dozen lines of struct and it keeps this tool free of an
imaging dependency.

Usage:
    python3 tools/make_icon.py
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "gui" / "icons"
SVG = OUT / "app_icon.svg"

#: Windows reads whichever of these fits the context; below 32px the mark loses
#: its fine strokes, but a blurry downscale of 256 looks worse than a crisp
#: render at the real size, so every size is rendered from the vector.
SIZES = (16, 32, 48, 64, 128, 256)


def render_png(svg_path: Path, n: int, tmp_dir: Path) -> bytes:
    """Render the SVG at n x n and return PNG bytes (transparent background).

    Qt writes the PNG to a file rather than to a QBuffer: the buffer route
    crashed the interpreter on this build of PyQt, and a build tool has no
    reason to be clever about a few kilobytes.
    """
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer

    image = QImage(n, n, QImage.Format.Format_ARGB32)
    image.fill(0)                      # transparent, so round marks stay round

    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise SystemExit(f"not a readable SVG: {svg_path}")

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()

    out = tmp_dir / f"_icon_{n}.png"
    if not image.save(str(out), "PNG"):
        raise SystemExit(f"could not write {out}")
    data = out.read_bytes()
    out.unlink()
    return data


def ico_bytes(pngs: "list[tuple[int, bytes]]") -> bytes:
    """Wrap PNG images in an ICO container (PNG-in-ICO, Vista+)."""
    count = len(pngs)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = b""
    blob = b""
    for n, data in pngs:
        entries += struct.pack(
            "<BBBBHHII",
            0 if n >= 256 else n,   # width  (0 means 256)
            0 if n >= 256 else n,   # height
            0, 0, 1, 32,
            len(data), offset,
        )
        blob += data
        offset += len(data)
    return header + entries + blob


def main():
    if not SVG.exists():
        raise SystemExit(f"missing source icon: {SVG}")

    # Rendering needs a QGuiApplication alive; this is a build tool with no
    # display, so ask for the offscreen platform before Qt looks for one.
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    assert app is not None                      # kept alive for the renders

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        pngs = [(n, render_png(SVG, n, Path(tmp))) for n in SIZES]
    for n, data in pngs:
        print(f"  rendered {n}x{n} ({len(data)} bytes)")

    (OUT / "app_icon.png").write_bytes(dict(pngs)[256])
    (OUT / "app_icon.ico").write_bytes(ico_bytes(pngs))
    print(f"wrote {OUT / 'app_icon.png'}")
    print(f"wrote {OUT / 'app_icon.ico'}")


if __name__ == "__main__":
    main()

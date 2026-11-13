#!/usr/bin/env python3
"""Build labelled contact sheets from frontend_product_qa.cjs screenshots."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CELL_WIDTH = 460
CELL_HEIGHT = 330
COLUMNS = 4
ROWS = 4


def font(size: int) -> ImageFont.ImageFont:
    for candidate in (
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
    ):
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def build_sheet(screenshots: list[Path], destination: Path, title: str) -> None:
    sheet = Image.new("RGB", (CELL_WIDTH * COLUMNS, CELL_HEIGHT * ROWS), "#d7dbe3")
    draw = ImageDraw.Draw(sheet)
    label_font = font(15)
    for index, screenshot in enumerate(screenshots):
        column = index % COLUMNS
        row = index // COLUMNS
        left = column * CELL_WIDTH
        top = row * CELL_HEIGHT
        draw.rectangle(
            (left + 3, top + 3, left + CELL_WIDTH - 4, top + CELL_HEIGHT - 4),
            fill="#ffffff",
            outline="#7d8796",
        )
        draw.text((left + 12, top + 8), screenshot.stem, fill="#17202e", font=label_font)
        with Image.open(screenshot) as source:
            image = source.convert("RGB")
        max_width = CELL_WIDTH - 24
        max_height = CELL_HEIGHT - 52
        scale = min(max_width / image.width, max_height / image.height)
        rendered = image.resize(
            (round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS
        )
        x = left + (CELL_WIDTH - rendered.width) // 2
        y = top + 34 + (max_height - rendered.height) // 2
        sheet.paste(rendered, (x, y))
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, quality=92)
    print(f"{destination}\t{title}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    phase_root = args.phase_root.resolve()
    output = (args.output or (phase_root / "contact-sheets")).resolve()
    for theme in ("light", "dark"):
        for locale in ("en-US", "zh-CN"):
            for width in (1440, 768, 390):
                directory = phase_root / theme / locale / str(width)
                screenshots = sorted(directory.glob("*.png"))
                if not screenshots:
                    continue
                build_sheet(
                    screenshots,
                    output / f"{theme}-{locale}-{width}.jpg",
                    f"{theme} / {locale} / {width}px",
                )


if __name__ == "__main__":
    main()

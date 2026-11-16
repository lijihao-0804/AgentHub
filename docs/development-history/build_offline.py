"""Build the double-clickable reading edition from the split site."""

import base64
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"


def inline_script(source: str) -> str:
    return "<script>" + source.replace("</script", r"<\/script") + "</script>"


def build() -> str:
    page = (SITE / "index.html").read_text(encoding="utf-8")
    css = (SITE / "assets/app.css").read_text(encoding="utf-8")
    page = page.replace('<link rel="stylesheet" href="./assets/app.css">', f"<style>{css}</style>")
    blocks = []
    for path in sorted((SITE / "data").rglob("*.json")):
        key = "./" + path.relative_to(SITE).as_posix()
        payload = path.read_text(encoding="utf-8").replace("<", r"\u003c")
        blocks.append(f'<script type="application/json" id="{key}">{payload}</script>')
    media = {
        path.name: "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        for path in sorted((SITE / "assets/media").glob("*.webp"))
    }
    blocks.append(inline_script("window.archiveMedia = " + json.dumps(media) + ";"))
    blocks.append(inline_script((SITE / "assets/hljs.js").read_text(encoding="utf-8")))
    page = page.replace('<script defer src="./assets/hljs.js"></script>', "\n".join(blocks))
    return page.replace(
        '<script defer src="./assets/app.js"></script>',
        inline_script((SITE / "assets/app.js").read_text(encoding="utf-8")),
    )


if __name__ == "__main__":
    (ROOT / "index.html").write_text(build(), encoding="utf-8", newline="\n")

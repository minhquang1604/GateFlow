"""Render the 32x32 PNG favicon fallback from favicon.svg.

Safari before 16.4 ignores ``rel="icon"`` pointing at an SVG, so the shell
also links a PNG. That PNG is generated, not hand-drawn — run this after
any edit to ``favicon.svg`` so the two cannot drift apart::

    .venv/bin/python scripts/render_favicon.py

Requires Playwright with the Chromium download (``pip install playwright``
then ``python -m playwright install chromium``). It is a one-off authoring
tool, not a runtime or test dependency of the framework.
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src/mlops_framework/ui/static"
SIZE = 32


def main() -> None:
    from playwright.sync_api import sync_playwright

    svg = STATIC / "favicon.svg"
    png = STATIC / "favicon.png"

    # The markup is inlined rather than referenced with <img src>: the page
    # is built with set_content(), whose base URL is about:blank, and a
    # browser refuses to pull a file:// subresource into such a document —
    # the screenshot silently captured a broken-image glyph instead.
    page_html = (
        "<!doctype html><meta charset='utf-8'>"
        "<style>"
        "body{margin:0}"
        f"#icon,#icon svg{{width:{SIZE}px;height:{SIZE}px;display:block}}"
        "</style>"
        f"<body><div id='icon'>{svg.read_text(encoding='utf-8')}</div></body>"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": SIZE, "height": SIZE},
            device_scale_factor=1,
        )
        page = context.new_page()
        page.set_content(page_html)
        page.wait_for_timeout(200)
        page.locator("#icon").screenshot(path=str(png), omit_background=True)
        browser.close()

    print(f"wrote {png} ({png.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

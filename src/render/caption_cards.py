"""
Full-screen caption renderer.

No card, no chrome: heavy text burned straight over the background footage,
which is the other dominant short-form look and is deliberately nothing like
cards.py (iMessage bubbles) or reddit_cards.py (Reddit post cards). Because it
carries no source-specific furniture, any narrated format can render through
it - see sources/wikipedia.py and generate/monologue.py.

One PNG per beat, held for that beat's narration, following reddit_cards.py's
static model rather than cards.py's growth animation. Nothing here needs to
animate: the text changes when the voice does, and beats are kept short by the
generators so that happens every second or two.

Font size is measured and shrunk per beat rather than fixed, because beat
lengths vary and a fixed size either overflows on the long ones or wastes the
frame on the short ones.

Public entry point: render_script() -> list[Frame]
"""

import html as _html
import re
from pathlib import Path

from jinja2 import Template
from playwright.sync_api import sync_playwright

from src.render.cards import Frame, FRAME_W, FRAME_H

HERE = Path(__file__).parent

MAX_FONT_PX = 104
MIN_FONT_PX = 46
FONT_STEP_PX = 6

# Generators mark emphasis by wrapping a word in *asterisks*.
_EMPHASIS = re.compile(r"\*([^*]+)\*")


def _to_html(text: str) -> str:
    """Escape, then turn *emphasis* into a highlight span. Escaping first means
    a story containing < or & cannot inject markup."""
    escaped = _html.escape(text.strip())
    return _EMPHASIS.sub(r'<span class="hi">\1</span>', escaped)


def strip_emphasis(text: str) -> str:
    """The narration must not read the asterisks aloud. TTS uses this; only the
    renderer sees the markers."""
    return _EMPHASIS.sub(r"\1", text)


class CaptionRenderer:
    def __init__(self, outdir=Path("out")):
        self.css = (HERE / "caption.css").read_text()
        self.tpl = Template((HERE / "caption.html").read_text())
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)

    def _render_at(self, page, html_text: str, font_size: int):
        page.set_content(self.tpl.render(
            css=self.css, html_text=html_text, font_size=font_size,
        ))

    def _fit(self, page, html_text: str) -> int:
        """Largest size in the range that keeps the text inside the stage box."""
        for size in range(MAX_FONT_PX, MIN_FONT_PX - 1, -FONT_STEP_PX):
            self._render_at(page, html_text, size)
            overflows = page.evaluate(
                "() => {"
                "  const s = document.getElementById('stage');"
                "  const c = document.getElementById('caption');"
                "  return c.getBoundingClientRect().height > s.getBoundingClientRect().height + 1;"
                "}"
            )
            if not overflows:
                return size
        return MIN_FONT_PX

    def render_script(self, beats, beat_durations_ms) -> list[Frame]:
        """
        beats: list[Beat] (uses .text; *emphasis* markers are honoured)
        beat_durations_ms: audio duration per beat, same length

        Returns list[Frame], one per beat.
        """
        assert len(beats) == len(beat_durations_ms)
        frames = []

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                viewport={"width": FRAME_W, "height": FRAME_H},
                device_scale_factor=1,
            )

            for i, beat in enumerate(beats):
                html_text = _to_html(beat.text)
                size = self._fit(page, html_text)
                self._render_at(page, html_text, size)
                out = self.outdir / f"f{i:05d}.png"
                page.screenshot(path=str(out), omit_background=True)
                frames.append(Frame(out, beat_durations_ms[i]))

            browser.close()

        return frames

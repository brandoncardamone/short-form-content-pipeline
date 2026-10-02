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
import random
import re
from pathlib import Path

from jinja2 import Template
from playwright.sync_api import sync_playwright

from src.render.cards import Frame, FRAME_W, FRAME_H

HERE = Path(__file__).parent

# Measured, not guessed: video 98 was 19 beats and came out at 29.30s, i.e.
# 1.54s per beat including the inter-beat gap. The first estimate here was
# 2.2s, which made caption videos land at ~29s - below video.target_duration_min
# - because the generators derive their beat count from it. Re-measure if the
# TTS voice, tts.speed or tts.gap_ms changes, the same way WORDS_PER_SEC in
# sources/reddit.py has to be re-measured.
SECONDS_PER_BEAT = 1.55

MAX_FONT_PX = 104
MIN_FONT_PX = 46
FONT_STEP_PX = 6

# Generators mark emphasis by wrapping a word in *asterisks*.
_EMPHASIS = re.compile(r"\*([^*]+)\*")

# One highlight colour is drawn per video. All are picked to stay legible
# against the black stroke over arbitrary footage - nothing dark, nothing that
# vanishes into the white fill.
HIGHLIGHT_COLOURS = [
    "#FFE300",  # yellow
    "#4CE6A1",  # mint
    "#FF6B6B",  # coral
    "#5EC8FF",  # sky
    "#FFA53D",  # amber
    "#C77DFF",  # violet
]


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
    def __init__(self, outdir=Path("out"), highlight=None, speakers=None):
        self.css = (HERE / "caption.css").read_text()
        self.tpl = Template((HERE / "caption.html").read_text())
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        # Fixed for the whole video: changing it between beats would read as a
        # glitch rather than as variety.
        self.highlight = highlight or random.choice(HIGHLIGHT_COLOURS)
        # {"a": {"name": "DOC", "color": "#5EC8FF"}, ...} for two-speaker
        # explainer scripts; None for single-narrator formats, which render
        # exactly as before with no name shown.
        self.speakers = speakers or {}

    def _render_at(self, page, html_text: str, font_size: int, speaker=None):
        info = self.speakers.get(speaker) if speaker else None
        page.set_content(self.tpl.render(
            css=self.css, html_text=html_text, font_size=font_size,
            highlight=self.highlight,
            speaker_name=(info or {}).get("name"),
            speaker_color=(info or {}).get("color", "#ffffff"),
        ))

    def _fit(self, page, html_text: str, speaker=None) -> int:
        """Largest size in the range that keeps the text inside the stage box."""
        for size in range(MAX_FONT_PX, MIN_FONT_PX - 1, -FONT_STEP_PX):
            self._render_at(page, html_text, size, speaker)
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
                speaker = getattr(beat, "speaker", None) if self.speakers else None
                size = self._fit(page, html_text, speaker)
                self._render_at(page, html_text, size, speaker)
                out = self.outdir / f"f{i:05d}.png"
                page.screenshot(path=str(out), omit_background=True)
                frames.append(Frame(out, beat_durations_ms[i]))

            browser.close()

        return frames

def target_beats(cfg) -> int:
    """Beat count that lands inside video.target_duration_*, at the measured
    caption pacing. Shared by every caption-format generator so they cannot
    drift apart."""
    mid = (cfg.video.target_duration_min + cfg.video.target_duration_max) / 2
    return max(8, round(mid / SECONDS_PER_BEAT))

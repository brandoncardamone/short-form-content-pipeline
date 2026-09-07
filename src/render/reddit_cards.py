"""
Reddit-story card renderer. Unlike cards.py (iMessage bubbles that grow in),
this format is a static full-block card per beat: each card fully replaces
the previous one and holds for the beat's narration duration. No entry
animation — that matches what the reference videos actually do.

Public entry point: render_script() -> list[Frame]
"""

from pathlib import Path
import hashlib

from jinja2 import Template
from playwright.sync_api import sync_playwright

from src.render.cards import Frame, FRAME_W, FRAME_H

HERE = Path(__file__).parent

_PALETTE = [
    "#FF4500", "#0079D3", "#46D160", "#FFB000", "#FF8717",
    "#7193FF", "#FF585B", "#00A6A5", "#EA0027", "#24A0ED",
]


def _avatar_color(name: str) -> str:
    h = int(hashlib.sha256(name.encode()).hexdigest(), 16)
    return _PALETTE[h % len(_PALETTE)]


class RedditCardRenderer:
    def __init__(self, outdir=Path("out")):
        self.css = (HERE / "reddit_card.css").read_text()
        self.tpl = Template((HERE / "reddit_card.html").read_text())
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)

    def _html(self, text: str, meta) -> str:
        name = meta.username if meta.kind == "comment" else meta.subreddit
        # Avoid rendering the same string twice when a beat's narration text
        # is literally the post title (the qa-format hook beat).
        body = None if (meta.title and text == meta.title) else text
        return self.tpl.render(
            css=self.css, text=body, meta=meta,
            avatar_color=_avatar_color(name),
            avatar_letter=(name[:1] or "?").upper(),
        )

    def render_script(self, beats, card_meta, beat_durations_ms) -> list[Frame]:
        """
        beats: list[Beat] (uses .text)
        card_meta: list[RedditCardMeta], same length/order as beats
        beat_durations_ms: audio duration per beat, same length

        Returns list[Frame], one per beat.
        """
        assert len(beats) == len(card_meta) == len(beat_durations_ms)
        frames = []

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                viewport={"width": FRAME_W, "height": FRAME_H},
                device_scale_factor=1,
            )

            for i, (beat, meta) in enumerate(zip(beats, card_meta)):
                page.set_content(self._html(beat.text, meta))
                out = self.outdir / f"f{i:05d}.png"
                page.screenshot(path=str(out), omit_background=True)
                frames.append(Frame(out, beat_durations_ms[i]))

            browser.close()

        return frames

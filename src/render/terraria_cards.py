"""
Terraria format renderer: subject title, a framed picture panel, a caption.

The other renderers produce one still per beat. This one has to show things
moving - a weapon's demo GIF playing, a sprite popping in when the narration
names it - so it works in two layers:

  1. Chromium renders the STATIC layer once per beat (title, panel frame,
     label, caption), exactly as caption_cards.py does.
  2. Pillow composites the moving parts onto a copy of that layer for every
     output frame: the current GIF frame or bobbing sprite, and the progress
     bar. That is cheap enough to do at FPS for the whole video, which a
     Chromium screenshot per frame is not.

The picture's position comes from the #media box in terraria.css, read back
from the page, so layout stays CSS-only.

Pixel art is upscaled by whole multiples with nearest-neighbour. Any smooth
filter turns a 34px sprite into a blur at the size it is shown.

Public entry point: render_script() -> list[Frame]
"""

import hashlib
import html
import io
import logging
import math
import random
import re
from pathlib import Path
from typing import Optional

import requests
from jinja2 import Template
from PIL import Image, ImageDraw, ImageSequence
from playwright.sync_api import sync_playwright

from src.render.cards import Frame, FRAME_W, FRAME_H
from src.render.caption_cards import HIGHLIGHT_COLOURS

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent

FPS = 12                 # composited frames per second; the encode is still 30fps
POP_MS = 240             # scale-in when the picture changes
BOB_PX = 9               # idle float for still sprites, so the panel is never dead
BOB_PERIOD_MS = 2600
GIF_MAX_LOOP_MS = 12000  # longer clips are cut and looped, to bound memory
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
USER_AGENT = "short-form-content-pipeline/1.0 (personal project)"

MAX_FONT_PX = 84
MIN_FONT_PX = 40
FONT_STEP_PX = 4


WORD_PAUSE_CHARS = 2     # per-word allowance when estimating word timing


def _split_words(text: str) -> list[tuple[str, bool]]:
    """[(word, emphasised)] with the *asterisk* markers consumed. Emphasis may
    span several words ("*On Fire!*") or sit inside punctuation ("*lethal*.")."""
    out, inside = [], False
    for token in text.split():
        stars = token.count("*")
        emphasised = inside or stars > 0
        if stars % 2:
            inside = not inside
        word = token.replace("*", "")
        if word:
            out.append((word, emphasised))
    return out or [("", False)]


def _words_html(words, upto: int, mark_current: bool = True) -> str:
    """Caption markup with words 0..upto visible and the rest laid out but
    hidden. Escaped here, since the template is told this is safe markup."""
    spans = []
    for i, (word, emphasised) in enumerate(words):
        classes = ["w"]
        if emphasised:
            classes.append("hi")
        if i > upto:
            classes.append("off")
        elif i == upto and mark_current:
            classes.append("cur")
        spans.append(f'<span class="{" ".join(classes)}">{html.escape(word)}</span>')
    return " ".join(spans)


def _ease_out_back(t: float) -> float:
    c1, c3 = 1.4, 2.4
    return 1.0 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


class _Media:
    """A picture prepared for the panel: one frame for a sprite, several for a GIF."""

    def __init__(self, frames: list[Image.Image], step_ms: float):
        self.frames = frames
        self.step_ms = step_ms

    @property
    def animated(self) -> bool:
        return len(self.frames) > 1

    def frame_at(self, t_ms: float) -> Image.Image:
        if not self.animated:
            return self.frames[0]
        return self.frames[int(t_ms / self.step_ms) % len(self.frames)]


def _fit(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    scale = min(box_w / img.width, box_h / img.height)
    if scale >= 1.0:
        k = max(1, int(scale))
        return img.resize((img.width * k, img.height * k), Image.NEAREST)
    return img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                      Image.LANCZOS)


def _load_media(path: Path, box_w: int, box_h: int) -> _Media:
    im = Image.open(path)
    step_ms = 1000.0 / FPS

    if getattr(im, "n_frames", 1) > 1:
        # Resample the GIF onto the output frame grid while decoding, keeping
        # only the frames that will actually be shown. A 7s demo clip is a few
        # hundred source frames; holding them all at panel size is ~500MB.
        frames, elapsed, next_tick = [], 0.0, 0.0
        for fr in ImageSequence.Iterator(im):
            if elapsed >= next_tick:
                frames.append(_fit(fr.convert("RGBA"), box_w, box_h))
                next_tick += step_ms
            elapsed += fr.info.get("duration", 100) or 100
            if elapsed >= GIF_MAX_LOOP_MS:
                break
        return _Media(frames, step_ms)

    still = im.convert("RGBA")
    bbox = still.getbbox()          # trim transparent margins so it centres properly
    if bbox:
        still = still.crop(bbox)
    return _Media([_fit(still, box_w, box_h - 2 * BOB_PX)], step_ms)


class TerrariaRenderer:
    def __init__(self, outdir=Path("out"), title: str = "", media_dir=None, highlight=None):
        self.css = (HERE / "terraria.css").read_text()
        self.tpl = Template((HERE / "terraria.html").read_text(), autoescape=True)
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.media_dir = Path(media_dir) if media_dir else self.outdir / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.title = title
        # Fixed for the whole video, as in caption_cards.py.
        self.highlight = highlight or random.choice(HIGHLIGHT_COLOURS)
        self._cache: dict[str, Optional[_Media]] = {}

    # ── media ────────────────────────────────────────────────────────────────

    def _download(self, url: str) -> Optional[Path]:
        ext = url.rsplit(".", 1)[-1].lower()[:4]
        dest = self.media_dir / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}.{ext}"
        if dest.exists():
            return dest
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60, stream=True)
            resp.raise_for_status()
            if int(resp.headers.get("Content-Length") or 0) > MAX_DOWNLOAD_BYTES:
                logger.warning("Skipping oversized image %s", url)
                return None
            dest.write_bytes(resp.content)
        except requests.RequestException as e:
            logger.warning("Image download failed, keeping the previous picture: %s (%s)", url, e)
            return None
        return dest

    def _media_for(self, visual, box_w: int, box_h: int) -> Optional[_Media]:
        if visual.url not in self._cache:
            media = None
            path = self._download(visual.url)
            if path is not None:
                try:
                    media = _load_media(path, box_w, box_h)
                except (OSError, ValueError) as e:
                    logger.warning("Could not decode %s: %s", visual.url, e)
            self._cache[visual.url] = media
        return self._cache[visual.url]

    # ── static layer ─────────────────────────────────────────────────────────

    def _render_at(self, page, html_text, font_size: int, label: str, has_media: bool):
        from markupsafe import Markup
        page.set_content(self.tpl.render(
            css=Markup(self.css), title=self.title, label=label, has_media=has_media,
            html_text=Markup(html_text), font_size=font_size, highlight=self.highlight,
        ))

    def _base_layers(self, page, text: str, label: str, has_media: bool):
        """Static layers for one beat: one per word, with the caption revealed
        up to and including that word. Returns (layers, cumulative time shares).

        Words appear as they are spoken instead of the whole sentence landing
        at once - it gives the eye something to follow and stops the viewer
        reading ahead of the voice. Unrevealed words are hidden with
        `visibility`, not removed, so the line never reflows as it fills in.

        There are no word timestamps from the TTS, so each word's share of the
        beat is estimated from its length. Over a two-second beat that is
        within a syllable of the voice, which is what this needs.
        """
        words = _split_words(text)
        size = MAX_FONT_PX
        for size in range(MAX_FONT_PX, MIN_FONT_PX - 1, -FONT_STEP_PX):
            self._render_at(page, _words_html(words, len(words) - 1, mark_current=False),
                            size, label, has_media)
            overflows = page.evaluate(
                "() => document.getElementById('caption').getBoundingClientRect().height"
                " > document.getElementById('stage').getBoundingClientRect().height + 1"
            )
            if not overflows:
                break

        layers = []
        for k in range(len(words)):
            self._render_at(page, _words_html(words, k), size, label, has_media)
            png = page.screenshot(omit_background=True)
            layers.append(Image.open(io.BytesIO(png)).convert("RGBA"))

        weights = [len(re.sub(r"\W", "", w)) + WORD_PAUSE_CHARS for w, _ in words]
        total, acc, shares = float(sum(weights)), 0.0, []
        for w in weights:
            acc += w
            shares.append(acc / total)
        return layers, shares

    def _box(self, page, element_id: str) -> tuple[int, int, int, int]:
        r = page.evaluate(
            "(id) => { const r = document.getElementById(id).getBoundingClientRect();"
            " return [r.left, r.top, r.width, r.height]; }", element_id)
        return tuple(int(round(v)) for v in r)

    # ── public ───────────────────────────────────────────────────────────────

    def render_script(self, beats, visuals, beat_durations_ms) -> list[Frame]:
        """
        beats: list[Beat] (uses .text; *emphasis* markers are honoured)
        visuals: list[BeatVisual | None], same length; None keeps the previous picture
        beat_durations_ms: audio duration per beat, same length

        Returns list[Frame] in playback order, several per beat.
        """
        visuals = visuals or [None] * len(beats)
        assert len(beats) == len(visuals) == len(beat_durations_ms)
        total_ms = sum(beat_durations_ms) or 1.0
        step_ms = 1000.0 / FPS
        frames: list[Frame] = []
        idx = 0

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                viewport={"width": FRAME_W, "height": FRAME_H},
                device_scale_factor=1,
            )

            # Layout is fixed, so the boxes are measured once.
            self._render_at(page, "", MAX_FONT_PX, "", True)
            mx, my, mw, mh = self._box(page, "media")
            px, py, pw, ph = self._box(page, "progress")

            current: Optional[_Media] = None
            label = ""
            changed_at = 0.0     # video time at which `current` appeared
            now = 0.0

            for i, beat in enumerate(beats):
                v = visuals[i]
                if v is not None:
                    media = self._media_for(v, mw, mh)
                    if media is not None and media is not current:
                        current, label, changed_at = media, v.label, now

                layers, shares = self._base_layers(page, beat.text, label, current is not None)

                n = max(1, round(beat_durations_ms[i] / step_ms))
                frame_ms = beat_durations_ms[i] / n
                for k in range(n):
                    t = now + k * frame_ms
                    # The word being spoken at the middle of this frame.
                    progress = (k + 0.5) / n
                    word = next((w for w, s in enumerate(shares) if progress < s), len(shares) - 1)
                    canvas = layers[word].copy()
                    if current is not None:
                        self._paste_media(canvas, current, t - changed_at, (mx, my, mw, mh))
                    fill = int(pw * min(1.0, (t + frame_ms) / total_ms))
                    if fill >= ph:
                        ImageDraw.Draw(canvas).rounded_rectangle(
                            (px, py, px + fill, py + ph), radius=ph // 2, fill=self.highlight)
                    out = self.outdir / f"f{idx:05d}.png"
                    canvas.save(out, compress_level=1)
                    frames.append(Frame(out, frame_ms))
                    idx += 1
                now += beat_durations_ms[i]

            browser.close()

        return frames

    def _paste_media(self, canvas: Image.Image, media: _Media, since_ms: float, box) -> None:
        mx, my, mw, mh = box
        fr = media.frame_at(since_ms)
        dy = 0
        if since_ms < POP_MS:
            s = max(0.05, _ease_out_back(since_ms / POP_MS))
            fr = fr.resize((max(1, int(fr.width * s)), max(1, int(fr.height * s))), Image.BILINEAR)
        elif not media.animated:
            dy = round(BOB_PX * math.sin(2 * math.pi * since_ms / BOB_PERIOD_MS))
        x = mx + (mw - fr.width) // 2
        y = my + (mh - fr.height) // 2 + dy
        # The pop-in overshoots past the box for a frame or two; clip rather
        # than let alpha_composite raise on an out-of-bounds paste.
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        layer.paste(fr, (x, y))
        canvas.alpha_composite(layer)

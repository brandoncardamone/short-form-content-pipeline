"""
Chat card renderer.

Produces one PNG per animation frame. The card grows downward as bubbles are
added; the newest bubble is clipped by the card's bottom edge until the growth
animation completes. When the card would exceed --card-max-content-h, it resets
and the next card renders without a header.

Public entry point: render_script() -> list[Frame]
"""

from dataclasses import dataclass
from pathlib import Path
import json

from jinja2 import Template
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
FRAME_W, FRAME_H = 1080, 1920
IOS_BLUE = "#0A84FF"

# Animation: frames rendered per message entry, and the total entry duration.
ENTRY_FRAMES = 6
ENTRY_MS = 190
OVERSHOOT = 1.055   # peak scale before settling to 1.0


@dataclass
class Frame:
    path: Path
    duration_ms: float


def _ease_out_back(t: float, overshoot: float) -> float:
    """Spring-like easing that overshoots then settles — matches iOS."""
    c1 = (overshoot - 1.0) * 10.0
    c3 = c1 + 1.0
    return 1.0 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _prepare(messages):
    """Annotate messages with tail / new_speaker flags."""
    out = []
    for i, m in enumerate(messages):
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        prev = messages[i - 1] if i > 0 else None
        out.append({
            "text": m["text"],
            "speaker": m["speaker"].lower(),
            "tail": nxt is None or nxt["speaker"] != m["speaker"],
            "new_speaker": prev is not None and prev["speaker"] != m["speaker"],
            "incoming": False,
        })
    return out


class CardRenderer:
    def __init__(self, contact_name="Maddy ❤️", avatar=None, outdir=Path("out")):
        self.css = (HERE / "card.css").read_text()
        self.tpl = Template((HERE / "card.html").read_text())
        self.contact_name = contact_name
        self.avatar = avatar
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.max_h = self._css_var("--card-max-content-h")

    def _css_var(self, name):
        for line in self.css.splitlines():
            if line.strip().startswith(name):
                val = line.split(":", 1)[1]
                val = val.split("/*")[0]           # drop trailing comment
                return float(val.strip().rstrip(";").strip().rstrip("px"))
        raise KeyError(name)

    def _html(self, messages, show_header, content_h=None, scale=1.0, shift=0.0):
        return self.tpl.render(
            css=self.css, messages=messages, show_header=show_header,
            contact_name=self.contact_name, avatar=self.avatar,
            ios_blue=IOS_BLUE, content_h=content_h, scale=scale, shift=shift,
        )

    def _natural_h(self, page, messages, show_header):
        """Measure the messages container at its natural height."""
        page.set_content(self._html(messages, show_header))
        return page.evaluate(
            "() => document.getElementById('messages').getBoundingClientRect().height"
        )

    def render_script(self, messages, beat_durations_ms):
        """
        messages: [{"text": str, "speaker": "a"|"b"}, ...]
        beat_durations_ms: audio duration per message, same length

        Returns list[Frame] in playback order.
        """
        assert len(messages) == len(beat_durations_ms)
        prepared = _prepare(messages)
        frames = []
        idx = 0

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                viewport={"width": FRAME_W, "height": FRAME_H},
                device_scale_factor=1,
            )

            card_start = 0          # first message index on the current card
            show_header = True

            i = 0
            while i < len(prepared):
                # Candidate visible set for this card
                visible = [dict(m) for m in prepared[card_start:i + 1]]

                h_after = self._natural_h(page, visible, show_header)

                # Reset if this message would overflow the card
                if h_after > self.max_h and i > card_start:
                    card_start = i
                    show_header = False
                    visible = [dict(prepared[i])]
                    h_after = self._natural_h(page, visible, show_header)

                # Height before the incoming bubble was added
                if len(visible) > 1:
                    h_before = self._natural_h(
                        page, [dict(m) for m in prepared[card_start:i]], show_header
                    )
                else:
                    h_before = 0.0

                visible[-1]["incoming"] = True

                # --- entry frames ---
                for f in range(ENTRY_FRAMES):
                    t = (f + 1) / ENTRY_FRAMES
                    e = _ease_out_back(t, OVERSHOOT)
                    content_h = h_before + (h_after - h_before) * min(e, 1.0)
                    scale = 0.86 + 0.14 * e
                    shift = (1.0 - e) * 26.0

                    page.set_content(self._html(
                        visible, show_header, content_h=content_h,
                        scale=round(scale, 4), shift=round(shift, 2),
                    ))
                    out = self.outdir / f"f{idx:05d}.png"
                    page.locator("#card").screenshot(path=str(out), omit_background=True)
                    frames.append(Frame(out, ENTRY_MS / ENTRY_FRAMES))
                    idx += 1

                # --- settled frame, held for the remainder of the beat ---
                visible[-1]["incoming"] = False
                page.set_content(self._html(visible, show_header, content_h=h_after))
                out = self.outdir / f"f{idx:05d}.png"
                page.locator("#card").screenshot(path=str(out), omit_background=True)
                hold = max(beat_durations_ms[i] - ENTRY_MS, 60.0)
                frames.append(Frame(out, hold))
                idx += 1

                i += 1

            browser.close()

        return frames


if __name__ == "__main__":
    sample = [
        {"speaker": "a", "text": "Hey babe... I gotta talk to you."},
        {"speaker": "a", "text": "It's very important"},
        {"speaker": "b", "text": "what is it???"},
        {"speaker": "a", "text": "Are you alone right now?"},
        {"speaker": "b", "text": "yeah im at home why"},
        {"speaker": "b", "text": "youre scaring me"},
        {"speaker": "a", "text": "I saw your brother yesterday"},
        {"speaker": "b", "text": "ok?? and"},
        {"speaker": "a", "text": "he wasn't alone"},
    ]
    durations = [2400, 1500, 1300, 2000, 1900, 1500, 2300, 1200, 1600]

    r = CardRenderer(outdir=HERE.parent / "out")
    fr = r.render_script(sample, durations)
    print(f"{len(fr)} frames, total {sum(f.duration_ms for f in fr)/1000:.1f}s")
    (HERE.parent / "out" / "manifest.json").write_text(json.dumps(
        [{"path": f.path.name, "ms": f.duration_ms} for f in fr], indent=1))

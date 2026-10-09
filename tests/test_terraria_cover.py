"""
Offline tests for the terraria opening title card, the cover-frame offset that
points at it, and the weighted angle/hook choice. No network, no LLM.
"""

import hashlib
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.schema import Beat, BeatVisual
from src.sources import terraria as T
from src.render import terraria_cards as R
from src import cli

OUTDIR = Path(__file__).parent.parent / "output" / "work" / "test_terraria_cover"
URL = "https://x/sprite.png"


def _renderer(name):
    r = R.TerrariaRenderer(outdir=OUTDIR / name / "frames", title="Test Blade",
                           media_dir=OUTDIR / name / "media", highlight="#FFE300")
    dest = r.media_dir / f"{hashlib.sha1(URL.encode()).hexdigest()[:16]}.png"
    Image.new("RGBA", (34, 34), (0, 200, 0, 255)).save(dest)
    return r


def _column_profile(path):
    """Rows of the frame that contain any opaque green sprite pixel, as (top, bottom)."""
    img = Image.open(path).convert("RGBA")
    rows = [y for y in range(0, img.height, 4)
            if any(img.getpixel((x, y))[:3] == (0, 200, 0) for x in range(300, 780, 12))]
    return (rows[0], rows[-1]) if rows else None


def test_title_card_opens_the_video_then_hands_over():
    beats = [Beat(text="A hook line of *several* words here.", speaker="a", voice="voice_b"),
             Beat(text="Then a second line.", speaker="a", voice="voice_b")]
    visuals = [BeatVisual(url=URL, label="Sprite"), None]
    durations = [3000.0, 1500.0]

    with_card = _renderer("card").render_script(beats, visuals, durations,
                                                cover_line="The *secret* blade")
    without = _renderer("plain").render_script(beats, visuals, durations)

    # The card is laid over the first beat, so it must not change the timing
    # contract: same total, same frame count, with or without it.
    assert abs(sum(f.duration_ms for f in with_card) - sum(durations)) < 0.001
    assert len(with_card) == len(without)

    step = sum(durations) / len(with_card)      # frames are evenly spaced in time
    cover_frame = with_card[int(R.COVER_FRAME_MS / step)].path
    after_card = with_card[int((R.COVER_MS + 300) / step)].path
    plain_same_moment = without[int(R.COVER_FRAME_MS / step)].path

    # On the card the picture sits lower and larger than in the normal panel...
    card_rows, panel_rows = _column_profile(cover_frame), _column_profile(plain_same_moment)
    assert card_rows and panel_rows
    assert card_rows[0] > panel_rows[0] + 100
    # ...everything on it stays inside the centre a profile grid keeps...
    assert card_rows[0] >= 400 and card_rows[1] <= 1500
    # ...and once it is over, the frame is back to the normal layout.
    assert abs(_column_profile(after_card)[0] - panel_rows[0]) < 40


def test_no_card_without_a_picture_or_a_line():
    beats = [Beat(text="Just one line here.", speaker="a", voice="voice_b")]
    r = _renderer("nopic")
    frames = r.render_script(beats, [None], [2000.0], cover_line="Some words")
    assert abs(sum(f.duration_ms for f in frames) - 2000.0) < 0.001   # rendered, no crash


def test_cover_frame_offset_lands_inside_the_card():
    beat = lambda ms: [SimpleNamespace(duration_ms=ms, gap_ms=110.0)]
    # Normal first beat: the designed frame, with the pop-in finished.
    assert cli._cover_ms_for("terraria", beat(2400.0), has_title_card=True) == R.COVER_FRAME_MS
    assert R.POP_MS < R.COVER_FRAME_MS < R.COVER_MS
    # A very short first beat shortens the card; the offset must follow it in.
    short = cli._cover_ms_for("terraria", beat(700.0), has_title_card=True)
    assert short < min(R.COVER_MS, 810.0 - R.COVER_MIN_REMAINDER_MS)
    # No card: unchanged behaviour, the fully revealed caption at the beat's end.
    assert cli._cover_ms_for("terraria", beat(2400.0)) == 2410
    # Other formats never see the flag's effect.
    assert cli._cover_ms_for("reddit_story", beat(2400.0), has_title_card=True) == 500


def test_cover_line_falls_back_to_the_subject_name(monkeypatch):
    monkeypatch.setattr(T, "resolve_named", lambda names: {})
    page = T.Page(pageid=7, title="Test Blade", url="u", text="t", prose_chars=2000,
                  images=[{"id": "img1", "url": URL, "label": "Test Blade", "width": 34,
                           "height": 34, "animated": False, "section": "Intro"}])
    cfg = SimpleNamespace(tts=SimpleNamespace(voices=SimpleNamespace(a="voice_b")))
    beats = [{"text": f"Beat number {n} of the script.", "image": "img1"} for n in range(9)]

    good = T._parse({"cover": "The blade nobody *wanted*.", "beats": beats}, cfg, page)
    assert good.cover_line == "The blade nobody *wanted*"          # trailing stop removed
    for bad in ("", "This cover line is far too long to ever fit on a title card at all", None):
        assert T._parse({"cover": bad, "beats": beats}, cfg, page).cover_line == "Test Blade"


def test_story_angles_and_the_belief_hook_are_favoured_not_exclusive():
    angles = Counter(T._weighted(T.ANGLES) for _ in range(4000))
    hooks = Counter(T._weighted(T.HOOKS) for _ in range(4000))
    assert len(angles) == len(T.ANGLES) and len(hooks) == len(T.HOOKS)   # all still occur
    top_two = sum(n for _, n in angles.most_common(2)) / 4000
    assert 0.25 < top_two < 0.5
    assert hooks.most_common(1)[0][0].startswith("Beat 1 states something players believe")
    assert hooks.most_common(1)[0][1] / 4000 < 0.6

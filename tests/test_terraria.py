"""
Offline tests for the terraria format: article extraction, the fact-check
pass, and the renderer's timing contract. Nothing here touches the network or
the LLM - images are generated locally and served from file:// paths are
avoided by pre-seeding the renderer's media cache.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.schema import Beat, BeatVisual, Script
from src.sources import terraria as T
from src.render import terraria_cards as R

OUTDIR = Path(__file__).parent.parent / "output" / "work" / "test_terraria"

PROSE = "The Test Blade is a sword that does a specific and interesting thing. " * 30

ARTICLE = f"""
<div class="mw-parser-output">
  <div class="infobox">Test Blade <img src="/images/Test_Blade.png?abc" alt="Test Blade item sprite"
       data-file-width="40" data-file-height="44"> Damage 85</div>
  <div class="hat-note">Not to be confused with <img src="/images/Other.png" data-file-width="40"
       data-file-height="40"></div>
  <p>{PROSE}</p>
  <p><img src="/images/thumb/Test_Blade_%28demo%29.gif/300px-Test_Blade_%28demo%29.gif?x"
       data-file-width="900" data-file-height="300">
     <img src="/images/Stack_digit_9.png" data-file-width="8" data-file-height="12">
     <img src="/images/Desktop_only.png" data-file-width="32" data-file-height="32"></p>
  <h2>Tips</h2>
  <ul><li>Swing it near water for a bonus.</li></ul>
  <h2>History</h2>
  <ul><li>Desktop 1.4: SECRET_HISTORY_MARKER <img src="/images/Old.png" data-file-width="40"
       data-file-height="40"></li></ul>
  <div class="ranger-navbox">NAVBOX_MARKER</div>
</div>
"""


def _parse_payload(html=ARTICLE, categories=("Sword_items", "Entities_patched_in_Desktop_1.4.4")):
    return {"title": "Test Blade", "pageid": 42, "text": {"*": html},
            "categories": [{"*": c} for c in categories]}


def test_extract_keeps_content_and_drops_furniture():
    page = T._extract(_parse_payload())
    assert page is not None
    assert page.key == "terraria:42"
    assert "Swing it near water" in page.text
    assert "SECRET_HISTORY_MARKER" not in page.text      # stops at History
    assert "NAVBOX_MARKER" not in page.text
    assert "Not to be confused" not in page.text          # hat-note skipped

    urls = [i["url"] for i in page.images]
    assert urls == [
        "https://terraria.wiki.gg/images/Test_Blade.png",
        # thumbnail rewritten to the original - a GIF's thumbnail is a still
        "https://terraria.wiki.gg/images/Test_Blade_%28demo%29.gif",
    ]
    assert [i["label"] for i in page.images] == ["Test Blade", "Test Blade"]
    assert [i["animated"] for i in page.images] == [False, True]
    assert [i["id"] for i in page.images] == ["img1", "img2"]


def test_patch_categories_do_not_reject_an_article():
    """Nearly every article has an "Entities_patched_in_..." category; an
    over-broad category filter once rejected the entire wiki."""
    assert T._extract(_parse_payload()) is not None
    assert T._extract(_parse_payload(categories=("Disambiguation_pages",))) is None


def test_thin_article_is_rejected():
    thin = ARTICLE.replace(PROSE, "A short stub.")
    assert T._extract(_parse_payload(html=thin)) is None


@pytest.mark.parametrize("title", ["1.4.2.3", "Guide:Crafting a Zenith", "Terra Blade/ru",
                                   "List of things"])
def test_non_article_titles_are_skipped(title):
    assert T._SKIP_TITLE.search(title)


class _FakeClient:
    def __init__(self, reply):
        self.reply = reply

    def complete(self, prompt, temperature=0.9):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _script():
    texts = ["Hook line about the *blade*.", "A true claim.", "A distorted claim.",
             "An invented claim.", "What do you think?"]
    v = BeatVisual(url="https://x/a.png", label="A")
    w = BeatVisual(url="https://x/b.png", label="B")
    return Script(
        beats=[Beat(text=t, speaker="a", voice="voice_b") for t in texts],
        title="Test Blade", hook="Hook line about the blade.", tags=["swords"],
        caption="", premise="terraria:42", visuals=[v, None, None, w, None],
    )


def test_verify_fixes_and_drops():
    page = T._extract(_parse_payload())
    reply = json.dumps({"beats": [
        {"n": 1, "verdict": "ok"}, {"n": 2, "verdict": "ok"},
        {"n": 3, "verdict": "fix", "text": "A corrected claim."},
        {"n": 4, "verdict": "drop"}, {"n": 5, "verdict": "ok"},
    ]})
    out = T._verify(_FakeClient(reply), page, _script())
    assert [b.text for b in out.beats] == [
        "Hook line about the *blade*.", "A true claim.", "A corrected claim.", "What do you think?"]
    assert len(out.visuals) == len(out.beats)
    # the dropped beat's image moves to the next beat rather than vanishing
    assert out.visuals[-1].label == "B"


def test_verify_never_drops_hook_or_closer_and_fails_open():
    page = T._extract(_parse_payload())
    all_drop = json.dumps({"beats": [{"n": n, "verdict": "drop"} for n in range(1, 6)]})
    out = T._verify(_FakeClient(all_drop), page, _script())
    assert [b.text for b in out.beats] == ["Hook line about the *blade*.", "What do you think?"]

    for bad in ("not json", RuntimeError("quota")):
        assert T._verify(_FakeClient(bad), page, _script()).beats == _script().beats


def test_fit_upscales_pixel_art_by_whole_multiples():
    sprite = Image.new("RGBA", (34, 34), (255, 0, 0, 255))
    assert R._fit(sprite, 772, 440).size == (34 * 12, 34 * 12)
    wide = Image.new("RGBA", (1069, 337), (255, 0, 0, 255))
    w, h = R._fit(wide, 772, 440).size
    assert w == 772 and h <= 440


def _seed(renderer, url, image, **save_kw):
    """Put an image where the renderer's downloader would have cached it."""
    ext = url.rsplit(".", 1)[-1]
    dest = renderer.media_dir / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}.{ext}"
    image.save(dest, **save_kw)


def test_render_durations_match_the_audio_exactly():
    """The timing contract: frames for a beat must sum to that beat's audio
    duration, or voice and picture drift apart over a two-minute video."""
    r = R.TerrariaRenderer(outdir=OUTDIR / "frames", title="Test Blade",
                           media_dir=OUTDIR / "media", highlight="#FFE300")
    still_url, gif_url = "https://x/still.png", "https://x/clip.gif"
    _seed(r, still_url, Image.new("RGBA", (34, 34), (0, 200, 0, 255)))
    gif_frames = [Image.new("RGB", (120, 40), (c, 0, 0)) for c in (0, 120, 240)]
    _seed(r, gif_url, gif_frames[0], save_all=True, append_images=gif_frames[1:],
          duration=200, loop=0)

    beats = [Beat(text=t, speaker="a", voice="voice_b")
             for t in ("First *beat* here.", "Second beat keeps the picture.", "Third beat, a clip.")]
    visuals = [BeatVisual(url=still_url, label="Still"), None,
               BeatVisual(url=gif_url, label="Clip", animated=True)]
    durations = [1234.0, 987.0, 1500.0]

    frames = r.render_script(beats, visuals, durations)

    assert abs(sum(f.duration_ms for f in frames) - sum(durations)) < 0.001
    assert all(f.path.exists() for f in frames)
    assert Image.open(frames[0].path).size == (1080, 1920)
    # several frames per beat - this format is not one still per beat
    assert len(frames) > len(beats) * 5
    # the picture is drawn inside the panel: something opaque and green there
    px = Image.open(frames[len(frames) // 4].path).getpixel((540, 610))
    assert px[1] > 150 and px[3] == 255


# ── word reveal, named images, voice QA, bright backgrounds ──────────────────

def test_split_words_handles_emphasis_shapes():
    words = R._split_words("Getting hit by *On Fire!* is bad, but *lethal*.")
    assert [w for w, _ in words] == ["Getting", "hit", "by", "On", "Fire!", "is", "bad,",
                                     "but", "lethal."]
    assert [w for w, hi in words if hi] == ["On", "Fire!", "lethal."]


def test_words_html_hides_unspoken_words_and_escapes():
    out = R._words_html([("a<b", False), ("next", True), ("last", False)], upto=1)
    assert "a&lt;b" in out
    assert out.count("off") == 1 and "w hi cur" in out


def test_resolve_named_maps_redirects_and_drops_unknown(monkeypatch):
    def fake_get(params):
        assert params["titles"].count("|") == 1          # duplicates collapsed, 2 files asked
        return {"query": {
            "normalized": [{"from": "File:water candle.png", "to": "File:Water candle.png"}],
            "redirects": [{"from": "File:Water candle.png", "to": "File:Water Candle.png"}],
            "pages": {
                "1": {"title": "File:Water Candle.png",
                      "imageinfo": [{"url": "https://terraria.wiki.gg/images/Water_Candle.png?x1",
                                     "width": 12, "height": 20}]},
                "-1": {"title": "File:Not Real.png", "missing": ""},
            }}}
    monkeypatch.setattr(T, "_get", fake_get)
    out = T.resolve_named(["water candle", "Not Real", "water  candle"])
    assert list(out) == ["wiki:water candle"]
    assert out["wiki:water candle"]["url"] == "https://terraria.wiki.gg/images/Water_Candle.png"


def _tone(path, seconds, sr=24000, amp=0.3, gap=None):
    import numpy as np
    import soundfile as sf
    t = np.arange(int(sr * seconds)) / sr
    audio = (amp * np.sin(2 * np.pi * 220 * t)).astype("float32")
    if gap:
        audio[int(sr * gap[0]): int(sr * gap[1])] = 0.0
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), audio, sr)
    return path


def test_voice_qa_signal_checks():
    """The checks that need no speech recogniser. Ten words throughout."""
    from src.tts import qa
    text = "one two three four five six seven eight nine ten"
    d = OUTDIR / "qa"
    assert qa.check(_tone(d / "good.wav", 3.0), text, transcript=False).ok
    assert "near-silent" in str(qa.check(_tone(d / "quiet.wav", 3.0, amp=0.001), text, transcript=False))
    assert "clipped" in str(qa.check(_tone(d / "clip.wav", 3.0, amp=3.0), text, transcript=False))
    assert "implausible" in str(qa.check(_tone(d / "long.wav", 14.0), text, transcript=False))
    assert "implausible" in str(qa.check(_tone(d / "short.wav", 0.5), text, transcript=False))
    assert "dead gap" in str(qa.check(_tone(d / "gap.wav", 4.0, gap=(1.0, 2.5)), text, transcript=False))


def test_bad_takes_are_resampled_and_best_is_kept(monkeypatch):
    """A failing take is re-recorded; the last attempt falls back to the calm
    delivery; and if nothing passes, the best-scoring take is the one kept."""
    from types import SimpleNamespace
    from src.tts import qa, run

    class Engine:
        def __init__(self):
            self.deliveries, self.n = [], 0
        def set_delivery(self, d):
            self.deliveries.append(dict(d))
        def synthesize(self, text, voice, out_path):
            self.n += 1
            _tone(out_path, 1.0 + self.n)      # each take is identifiable by its length

    cfg = SimpleNamespace(tts=SimpleNamespace(qa_max_attempts=3))
    beat = Beat(text="some words here", speaker="a", voice="voice_b")
    delivery = {"speed": 1.3, "exaggeration": 0.8, "cfg_weight": 0.4}
    raw = OUTDIR / "qa" / "beat_000_raw.wav"

    # second take passes -> stop there
    verdicts = iter([qa.Verdict(False, 0.2, ["bad"]), qa.Verdict(True, 1.0)])
    monkeypatch.setattr(qa, "check", lambda p, t: next(verdicts))
    eng = Engine()
    assert run._synthesize_checked(eng, beat, raw, delivery, cfg, 0) == (2, True)
    assert eng.n == 2 and run.SAFE_DELIVERY not in eng.deliveries

    # nothing passes -> three takes, safe delivery on the last, best score kept
    verdicts = iter([qa.Verdict(False, 0.2, ["bad"]), qa.Verdict(False, 0.7, ["meh"]),
                     qa.Verdict(False, 0.4, ["bad"])])
    eng = Engine()
    assert run._synthesize_checked(eng, beat, raw, delivery, cfg, 0) == (3, False)
    assert run.SAFE_DELIVERY in eng.deliveries and eng.deliveries[-1] == delivery
    import soundfile as sf
    assert abs(sf.info(str(raw)).duration - 3.0) < 0.01          # take 2 (1.0 + 2)
    assert not list(raw.parent.glob("beat_000_raw_take*.wav"))   # no strays left behind


def test_bright_stretches_are_preferred_but_dark_ones_stay_possible(monkeypatch):
    from src.assemble import build
    monkeypatch.setattr(build.subprocess, "run",
                        lambda *a, **k: __import__("types").SimpleNamespace(stdout="1000.0\n"))
    # First half of the clip is daylight, second half is night.
    monkeypatch.setattr(build, "_window_luma", lambda bg, s, total: 60.0 if s < 400 else 4.0)
    starts = [build._background_start_offset(Path("x.mp4"), 90.0, prefer_bright=True)
              for _ in range(300)]
    bright = sum(s < 400 for s in starts)
    assert all(0 <= s <= 850 for s in starts)
    assert bright > 270          # strongly preferred...
    assert len({round(s) for s in starts}) > 100   # ...and still random within it

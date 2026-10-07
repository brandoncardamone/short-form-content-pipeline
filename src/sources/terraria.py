"""
Terraria Wiki source - the format behind the Terraria account.

Picks a random article from terraria.wiki.gg through its MediaWiki API (free,
no auth, no approval), and has the LLM turn it into a 60-120s narrated video
about that one item or mechanic. The script is not a stat readout: the prompt
gets the whole article and is asked for the handful of things a player would
actually want to know.

Two things make this different from sources/wikipedia.py:

  - The page is chosen BEFORE any LLM call, and chosen for richness. Random
    articles are mostly thin (a paint roller, a patch-notes page), so a batch
    is fetched, junk is rejected for free, and the best of what is left wins.
  - Every beat can point at an image from the article (item sprites, the
    "(demo)" GIFs of a weapon being used). Only URLs are stored in the script;
    render/terraria_cards.py downloads them, so the DB row stays self-contained
    and a render can be re-run on another machine.

Licensing: wiki text is CC BY-NC-SA 4.0. The narration is original writing
from the article's facts rather than its wording, and captions credit the wiki
(see captions.py). Sprites and GIFs are Re-Logic's game art. Revisit both
before this account is monetised.
"""

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote

import requests

from src.schema import Script, Beat, BeatVisual
from src.generate.llm import load_client, extract_json
from src.captions import build_caption
from src.db import premise_exists
from src.render.caption_cards import strip_emphasis

logger = logging.getLogger(__name__)

WIKI_BASE = "https://terraria.wiki.gg"
API = f"{WIKI_BASE}/api.php"
USER_AGENT = "short-form-content-pipeline/1.0 (personal project)"
REQUEST_DELAY_S = 1.0

# Same reasoning as sources/wikipedia.py: the Gemini free tier is 20 requests a
# day per model and is shared with the other account, so LLM attempts are
# capped hard. Rejecting a page costs only a wiki request.
MAX_LLM_CALLS = 3
MAX_BATCHES = 3          # random batches tried before giving up
RANDOM_BATCH = 20        # titles per batch
MAX_PAGES_PARSED = 12    # full articles fetched per batch; most are too thin

# An article thinner than this cannot carry a minute of narration without the
# model inventing things to fill the time.
MIN_PROSE_CHARS = 1500
MAX_SOURCE_CHARS = 9000
MAX_IMAGES = 14
MIN_IMAGE_PX = 16

# Measured on the first real build (243 words -> 54.15s including gaps, at
# speed 1.31 / gap_ms 110). One data point: re-measure after a few videos, and
# after any change to the voice, tts.speed or tts.gap_ms, the same way
# WORDS_PER_SEC in sources/reddit.py has to be.
WORDS_PER_SEC = 4.5
WORDS_PER_BEAT = 11      # midpoint of the 8-14 words the prompt asks for

# Titles that are never a video: patch notes ("1.4.2.3"), subpages, lists.
_SKIP_TITLE = re.compile(r"^\d+(\.\d+)+|/|^List of |\(disambiguation\)|^Guide:", re.I)
# Deliberately narrow: nearly every article carries categories like
# "Entities_patched_in_Desktop_1.4.4", so matching on "patch" or "version"
# rejects the whole wiki.
_SKIP_CATEGORY = re.compile(r"disambig|stubs?$|^removed|^unobtainable", re.I)
_STOP_SECTIONS = {"history", "references", "see also", "footnotes"}
_SKIP_CLASSES = {"ranger-navbox", "navbox", "hat-note", "toc", "message-box", "mbox"}
# Edition badges, stack digits and rarity swatches are furniture, not pictures.
_JUNK_IMAGE = re.compile(r"stack_digit|_only\.|rarity_color|icon|^auto_|^wiki", re.I)
_RICH_SECTIONS = {"tips", "trivia", "notes"}

BASE_TAGS = ["terraria", "terrariatips", "gaming"]

PROMPT = """You write narration for a short vertical video about ONE thing from the game Terraria.
The video shows the item's sprite or a clip of it in use, with big captions, over gameplay footage.
One voice reads the narration aloud. The audience plays Terraria or used to.

SUBJECT: {title}

WIKI ARTICLE (your only source of facts):
{text}

IMAGES AVAILABLE (use the id to show one):
{images}

What to write:
- Do NOT read out stats. Find what is genuinely interesting or useful here: what it is actually
  good for, how to get it faster or earlier, a non-obvious interaction, a mistake players make,
  how it compares to the obvious alternative, an odd bit of trivia. Mention a number only when
  the number is the point.
- Use ONLY facts present in the article. Never invent drop rates, damage values, recipes or
  version history. If the article is thin, write a shorter script rather than padding.
- Where the article gives different values per platform, use the Desktop/Console/Mobile ones and
  ignore Old-gen console and 3DS.
- Open on the most surprising or useful fact. Never open with "did you know", "in Terraria", or
  by naming the subject and saying what category it is.
- Then give it a shape: what it is in one line, how you get it, why it matters, the part most
  players miss, and a verdict - is it worth it, and when.
- Plain spoken English, second person, contractions. No "furthermore", no "in conclusion".
- LENGTH MATTERS: write {beats_lo}-{beats_hi} beats, {words_lo}-{words_hi} words in total. Count
  them. Scripts that come back short are rejected. Reach the length by covering more of the
  article in specific detail, never by restating a point or adding filler.
- Each beat is ONE sentence of 8-14 words, shown alone as a caption, so a longer beat is a wall
  of text.
- Vary sentence shape. Do not end beat after beat on the same kind of punchy last word.
- In roughly one beat in three, wrap ONE word in *asterisks* for visual emphasis: the word that
  carries the surprise, wherever it falls in the sentence. Most beats have none.
- For each beat set "image" to the id of the image that matches what is being said right then -
  the ingredient when you name the ingredient, the demo clip when you describe it in use. Use
  null to keep the previous image on screen. Change image every 2-4 beats: not every beat, but
  never leave one picture up for more than about five. Use an animated clip when one fits. The
  first beat must have an image, and it should be the subject itself.
- The last beat is a question to the viewer that invites a comment.

Output valid JSON only, no markdown fences:
{{
  "hook": "the first beat's text, without asterisks",
  "tags": ["tag1", "tag2", "tag3"],
  "beats": [
    {{"text": "...", "image": "img1"}},
    {{"text": "...", "image": null}}
  ]
}}"""


VERIFY_PROMPT = """You are fact-checking narration for a Terraria video against its ONLY source.

SOURCE ARTICLE ({title}):
{text}

NARRATION, one numbered beat per line:
{beats}

For every beat give a verdict:
- "ok": every factual claim in it is supported by the source. Opinions, questions to the viewer
  and framing ("most players miss this") are fine and count as ok.
- "fix": it distorts, exaggerates or over-generalises what the source says. Supply "text": a
  rewrite that says only what the source supports, 8-14 words, same job in the script. Keep an
  *asterisk* emphasis word if the original had one.
- "drop": it states something the source does not contain at all and cannot be repaired.

Be strict about numbers, causes, and words like "always", "doubles", "permanent", "only".
Output valid JSON only, one entry per beat, in order:
{{"beats": [{{"n": 1, "verdict": "ok"}}, {{"n": 2, "verdict": "fix", "text": "..."}}]}}"""


@dataclass
class Page:
    pageid: int
    title: str
    url: str
    text: str
    prose_chars: int
    images: list[dict] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Dedup key, used as the script's premise. Keyed on the page rather
        than the model's summary so an article already made into a video is
        skipped before any LLM call is spent on it."""
        return f"terraria:{self.pageid}"

    @property
    def score(self) -> float:
        s = min(self.prose_chars, 6000)
        s += 1500 * any(i["animated"] for i in self.images)
        s += 600 * len(_RICH_SECTIONS & {x.lower() for x in self.sections})
        return s


def _get(params: dict) -> Optional[dict]:
    time.sleep(REQUEST_DELAY_S)
    try:
        resp = requests.get(API, params={**params, "format": "json"},
                            headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Terraria wiki request failed (%s): %s", params.get("action"), e)
        return None


def _random_titles() -> list[dict]:
    data = _get({"action": "query", "list": "random", "rnnamespace": 0,
                 "rnlimit": RANDOM_BATCH, "rnfilterredir": "nonredirects"})
    if not data:
        return []
    return [r for r in data.get("query", {}).get("random", [])
            if not _SKIP_TITLE.search(r["title"])]


def _original_url(src: str) -> str:
    """Full-size file URL from an <img src>. Thumbnails are rewritten to the
    original, which matters for GIFs: a GIF's thumbnail is a still."""
    path = src.split("?")[0]
    m = re.match(r"^/images/thumb/(.+)/[^/]+$", path)
    if m:
        path = "/images/" + m.group(1)
    return WIKI_BASE + path


def _label_for(img, url: str) -> str:
    label = (img.get("alt") or img.get("title") or "").strip()
    if not label or re.search(r"\.(png|gif|jpe?g)$", label, re.I):
        label = unquote(url.rsplit("/", 1)[-1]).rsplit(".", 1)[0].replace("_", " ")
    # "(demo)", "(old)", "(screenshot banner)": file bookkeeping, not a caption.
    label = re.sub(r"\s*\([^)]*\)\s*$", "", label)
    label = re.sub(r"\s+(item|placed|inventory|map)?\s*(sprite|icon)$", "", label, flags=re.I)
    return label.strip()[:40]


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _extract(parse: dict) -> Optional[Page]:
    """Reduce a rendered article to source text plus usable images. Returns
    None for anything not worth a video."""
    from bs4 import BeautifulSoup

    cats = [c["*"] for c in parse.get("categories", [])]
    if any(_SKIP_CATEGORY.search(c) for c in cats):
        return None

    soup = BeautifulSoup(parse["text"]["*"], "html.parser")
    root = soup.find(class_="mw-parser-output") or soup

    lines: list[str] = []
    sections: list[str] = []
    images: list[dict] = []
    seen_urls: set[str] = set()
    prose = 0
    section = "Intro"

    for el in root.find_all(recursive=False):
        classes = set(el.get("class") or [])
        if classes & _SKIP_CLASSES:
            continue
        if el.name in ("h2", "h3"):
            heading = _text(el)
            if el.name == "h2" and heading.lower() in _STOP_SECTIONS:
                break
            section = heading
            sections.append(heading)
            lines.append(f"\n## {heading}")
        elif "infobox" in classes:
            lines.append("INFOBOX: " + _text(el)[:700])
        elif el.name == "p":
            t = _text(el)
            if t:
                lines.append(t)
                prose += len(t)
        elif el.name in ("ul", "ol"):
            for li in el.find_all("li", recursive=False):
                t = _text(li)
                if t:
                    lines.append(f"- {t}")
                    prose += len(t)
        elif el.name == "table":
            rows = [" | ".join(_text(c) for c in tr.find_all(["th", "td"]))
                    for tr in el.find_all("tr")]
            lines.append("TABLE: " + " ;; ".join(r for r in rows if r.strip(" |"))[:1200])
        elif el.name == "div":
            t = _text(el)
            if 0 < len(t) <= 600:
                lines.append(t)
        else:
            continue

        for img in el.find_all("img"):
            src = img.get("src") or ""
            if not src.startswith("/images/"):
                continue
            url = _original_url(src)
            name = url.rsplit("/", 1)[-1]
            w = int(img.get("data-file-width") or 0)
            h = int(img.get("data-file-height") or 0)
            if url in seen_urls or min(w, h) < MIN_IMAGE_PX or _JUNK_IMAGE.search(name):
                continue
            seen_urls.add(url)
            images.append({
                "url": url, "label": _label_for(img, url), "width": w, "height": h,
                "animated": name.lower().endswith(".gif"), "section": section,
            })

    if prose < MIN_PROSE_CHARS or not images:
        return None

    # Keep every GIF (they are the "moving parts") and fill the rest in page
    # order, so a long crafting table cannot crowd the demo clip out.
    keep = [i for i in images if i["animated"]][:4]
    keep += [i for i in images if not i["animated"]][:MAX_IMAGES - len(keep)]
    keep.sort(key=images.index)
    for n, img in enumerate(keep, 1):
        img["id"] = f"img{n}"

    title = parse["title"]
    return Page(
        pageid=parse["pageid"], title=title,
        url=f"{WIKI_BASE}/wiki/{title.replace(' ', '_')}",
        text="\n".join(lines)[:MAX_SOURCE_CHARS], prose_chars=prose,
        images=keep, sections=sections,
    )


def fetch_page(title: str) -> Optional[Page]:
    data = _get({"action": "parse", "page": title, "prop": "text|categories",
                 "disabletoc": 1, "disableeditsection": 1, "redirects": 1})
    if not data or "parse" not in data:
        return None
    return _extract(data["parse"])


def pick_page(db_conn) -> Page:
    """Best unused article out of a few random batches. No LLM involved."""
    for batch in range(1, MAX_BATCHES + 1):
        candidates: list[Page] = []
        for r in _random_titles()[:MAX_PAGES_PARSED]:
            if db_conn is not None and premise_exists(db_conn, f"terraria:{r['id']}"):
                continue
            page = fetch_page(r["title"])
            if page is None:
                continue
            if db_conn is not None and premise_exists(db_conn, page.key):
                continue
            candidates.append(page)
        if candidates:
            best = max(candidates, key=lambda p: p.score)
            logger.info("Terraria page: %r (%d prose chars, %d images, %d animated) from %d candidates",
                        best.title, best.prose_chars, len(best.images),
                        sum(i["animated"] for i in best.images), len(candidates))
            return best
        logger.warning("No usable Terraria article in batch %d/%d", batch, MAX_BATCHES)
    raise RuntimeError(f"No usable Terraria wiki article after {MAX_BATCHES} random batches")


def _word_band(cfg) -> tuple[int, int]:
    v = cfg.video
    return round(v.target_duration_min * WORDS_PER_SEC), round(v.target_duration_max * WORDS_PER_SEC * 0.9)


def generate_terraria_script(cfg, db_conn, title: Optional[str] = None) -> Script:
    """title pins a specific article (manual testing); otherwise a random one."""
    if title:
        page = fetch_page(title)
        if page is None:
            raise RuntimeError(f"Terraria wiki article {title!r} is missing or too thin to use")
    else:
        page = pick_page(db_conn)

    client = load_client(cfg)
    lo, hi = _word_band(cfg)
    images = "\n".join(
        f"- {i['id']}: {i['label']} ({'animated clip' if i['animated'] else 'sprite'}, "
        f"from section {i['section']})" for i in page.images
    )
    # The beat count is what the model actually honours; asked for a word
    # total alone, the first real script came back at 243 words against a
    # 300-540 target on all three calls.
    prompt = PROMPT.format(title=page.title, text=page.text, images=images,
                           words_lo=lo, words_hi=hi,
                           beats_lo=round(lo / WORDS_PER_BEAT), beats_hi=round(hi / WORDS_PER_BEAT))

    note = ""
    for call in range(1, MAX_LLM_CALLS + 1):
        raw = client.complete(prompt + note, temperature=0.8)
        try:
            script = _parse(extract_json(raw), cfg, page)
        except (json.JSONDecodeError, ValueError, KeyError, AttributeError) as e:
            logger.warning("Terraria script parse failed (call %d/%d): %s", call, MAX_LLM_CALLS, e)
            continue

        words = sum(len(b.text.split()) for b in script.beats)
        # A thin article legitimately yields a short script, so only the last
        # call's tolerance is loose; earlier ones ask for a correction.
        slack = 0.6 if call == MAX_LLM_CALLS else 0.85
        if words < lo * slack or words > hi * 1.25:
            logger.warning("Terraria script is %d words, wanted %d-%d (call %d/%d)",
                           words, lo, hi, call, MAX_LLM_CALLS)
            note = (f"\n\nYour previous attempt was {words} words. "
                    f"It must be between {lo} and {hi} words.")
            continue

        logger.info("Terraria script accepted: %r (%d beats, %d words, %d call(s))",
                    page.title, len(script.beats), words, call)
        return _verify(client, page, script)

    raise RuntimeError(
        f"Gave up on {page.title!r} after {MAX_LLM_CALLS} LLM calls. Not retrying further: "
        "the free tier's daily request cap is shared with the other account."
    )


def _verify(client, page: Page, script: Script) -> Script:
    """Second pass: check every beat against the article, fix or drop the rest.

    Exists because the first real script (the Debuffs article) got 3 of 26
    beats wrong in exactly the way this audience notices - "cannot be
    cancelled by the Nurse" became "strictly permanent", and a flat 25 damage
    per second became "doubles your fire damage". One extra request per video.

    Fails OPEN: if the check itself cannot be run or parsed, the unverified
    script is used and a warning logged, because losing the day's post to a
    malformed fact-check reply is the worse outcome.
    """
    numbered = "\n".join(f"{n}. {b.text}" for n, b in enumerate(script.beats, 1))
    try:
        raw = client.complete(
            VERIFY_PROMPT.format(title=page.title, text=page.text, beats=numbered),
            temperature=0.2,
        )
        verdicts = {int(v["n"]): v for v in extract_json(raw)["beats"]}
    except Exception as e:
        logger.warning("Fact-check pass failed, using the script unverified: %s", e)
        return script

    beats, visuals = [], []
    fixed = dropped = 0
    last = len(script.beats)
    carried = None    # image of a dropped beat, handed to the next one kept
    for n, (beat, visual) in enumerate(zip(script.beats, script.visuals), 1):
        v = verdicts.get(n, {})
        verdict = str(v.get("verdict", "ok")).lower()
        text = str(v.get("text") or "").strip()
        # The hook and the closing question hold the structure up; they are
        # rewritten if wrong but never removed.
        if verdict == "drop" and n not in (1, last):
            dropped += 1
            carried = visual or carried
            logger.info("Fact-check dropped beat %d: %r", n, beat.text)
            continue
        if verdict == "fix" and text:
            fixed += 1
            logger.info("Fact-check rewrote beat %d: %r -> %r", n, beat.text, text)
            beat = beat.model_copy(update={"text": text})
        beats.append(beat)
        visuals.append(visual or carried)
        carried = None

    logger.info("Fact-check: %d beats, %d rewritten, %d dropped", last, fixed, dropped)
    hook = strip_emphasis(beats[0].text)
    return script.model_copy(update={
        "beats": beats, "visuals": visuals, "hook": hook,
        "caption": build_caption("terraria", hook, script.tags),
    })


def _parse(data: dict, cfg, page: Page) -> Script:
    voice = cfg.tts.voices.a
    by_id = {i["id"]: i for i in page.images}

    beats: list[Beat] = []
    visuals: list[Optional[BeatVisual]] = []
    for b in data.get("beats") or []:
        text = str(b.get("text", "")).strip()
        if not text:
            continue
        img = by_id.get(str(b.get("image") or "").strip())
        beats.append(Beat(text=text, speaker="a", voice=voice))
        visuals.append(BeatVisual(
            url=img["url"], label=img["label"], width=img["width"],
            height=img["height"], animated=img["animated"],
        ) if img else None)

    if len(beats) < 8:
        raise ValueError(f"Only {len(beats)} usable beats")
    if visuals[0] is None:
        # The subject's own sprite is the first image on the page.
        first = page.images[0]
        visuals[0] = BeatVisual(url=first["url"], label=first["label"], width=first["width"],
                                height=first["height"], animated=first["animated"])

    hook = strip_emphasis(str(data.get("hook") or beats[0].text)).strip()
    tags = [str(t) for t in (data.get("tags") or [])]
    return Script(
        beats=beats,
        title=page.title,
        hook=hook,
        tags=tags,
        caption=build_caption("terraria", hook, tags),
        premise=page.key,
        visuals=visuals,
        source_url=page.url,
    )

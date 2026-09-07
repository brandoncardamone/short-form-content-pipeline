"""
Reddit story source. Fetches real posts instead of generating fictional
content — this content format narrates genuine Reddit posts/comments rather
than LLM-authored dialogue.

Three access modes (config: reddit.access):
  - "arctic" (default): Project Arctic Shift (https://github.com/ArthurHeitmann/
    arctic_shift), a free, no-auth, community-run Reddit archive/query API —
    not live, but this content format narrates old posts anyway (reference
    videos cite "3y"/"1y" timestamps), and archived scores are only reliable
    once a post is old enough to have settled (see archive_min_age_days).
  - "json": reads Reddit's own public .json endpoints, unauthenticated. As of
    2026 these sit behind an anti-bot wall that blocks most requests outright
    (403) — kept as a code path in case that ever changes, not recommended.
  - "praw": Reddit's official OAuth API via PRAW. As of 2026 Reddit closed
    self-service app registration (reddit.com/prefs/apps) in favor of a manual
    "Responsible Builder Policy" approval process — only usable if you have an
    approved app's REDDIT_CLIENT_ID/SECRET/USER_AGENT in .env.

Both modes feed the same downstream logic: _JsonPost/_JsonComment expose the
same attribute names as PRAW's Submission/Comment objects, so _build_*_script
and _finish_script don't care which backend produced them.

Two content shapes, auto-detected per post:
  - "narrative": post has real body text (selftext) -> chunk the body into cards.
  - "qa": post has little/no body (e.g. AskReddit-style questions) -> read the
    title as the hook, then chunk the top comments into cards, one commenter
    identity per card group.

Dedup reuses the existing premise_hash mechanism: a post's fullname (t3_xxx)
is used as the premise, so the same post is never sourced twice.
"""

import logging
import re
import time
from typing import Optional

import requests

from src.schema import Beat, RedditCardMeta, Script
from src.captions import build_caption
from src.db import premise_exists

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
MAX_CHARS_PER_CARD = 220
# Calibrated against real assembled output at tts.speed=1.35/gap_ms=90. A beat-count
# floor doesn't work here (unlike textchain's fixed-length messages) since real post/
# comment length varies wildly — estimate duration from word count instead and reject
# too-short candidates before spending minutes on TTS for something we'll throw away.
# NOTE: this rate is tied to the specific voice reference clips in use (their natural
# speaking cadence varies) — recalibrate after any chatterbox_ref_{female,male}.wav swap.
# Recalibrated 2026-09-02 after switching to the reddit-sourced reference clips
# (video 18: 304 words -> 43.4s = 7.01 w/s, notably faster than the prior 4.75 w/s).
WORDS_PER_SEC = 7.0
MAX_TITLE_WORDS = 25   # a hook this long/rambling rarely lands in the first second
JSON_REQUEST_DELAY_S = 2.0
DEFAULT_USER_AGENT = "sfcp-reddit-stories/0.1 (personal, low-volume, read-only use)"
ARCTIC_BASE = "https://arctic-shift.photon-reddit.com"

# Best-effort keyword denylist — real user-submitted content can't be filtered
# as precisely as LLM output, so subreddit choice + NSFW-flag filtering below
# are the primary controls. This is defense-in-depth, not exhaustive.
DENYLIST = [
    "suicide", "kill myself", "self harm", "self-harm", "rape", "molest",
    "child porn", "csam", " incest", "grooming",
]

_REMOVED_MARKERS = {"[removed]", "[deleted]", ""}


def _relative_time(created_utc: float) -> str:
    delta = max(time.time() - created_utc, 0)
    years = delta / 31536000
    if years >= 1:
        return f"{int(years)}y"
    months = delta / 2592000
    if months >= 1:
        return f"{int(months)}mo"
    days = delta / 86400
    if days >= 1:
        return f"{int(days)}d"
    hours = delta / 3600
    if hours >= 1:
        return f"{int(hours)}h"
    return f"{max(int(delta / 60), 1)}m"


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # [text](url) -> text
    text = re.sub(r"[*_~`]{1,3}", "", text)                # bold/italic/strike/code markers
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\n{2,}", "\n\n", text.strip())
    return text


def _is_flagged(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in DENYLIST)


def _chunk_text(text: str, max_chars: int = MAX_CHARS_PER_CARD) -> list[str]:
    """Greedily pack sentences into card-sized chunks without splitting mid-sentence
    (unless a single sentence itself exceeds max_chars, then hard-wrap on words)."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    sentences: list[str] = []
    for p in paragraphs:
        sentences.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", p) if s.strip())

    chunks: list[str] = []
    current = ""
    for s in sentences:
        if len(s) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            words = s.split()
            piece = ""
            for w in words:
                if len(piece) + len(w) + 1 > max_chars:
                    chunks.append(piece)
                    piece = w
                else:
                    piece = f"{piece} {w}".strip()
            if piece:
                chunks.append(piece)
            continue
        candidate = f"{current} {s}".strip()
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = s
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# ── arctic backend (default: free, no-auth, archived data) ──────────────────

def _candidate_posts_arctic(cfg):
    """
    A single contiguous time-slice badly undersamples high-volume subreddits
    (AskReddit gets thousands of posts/day, so 25 consecutive-by-time posts
    span minutes, not enough to contain a viral one). Instead, sample several
    random time-slices spread across the whole age window per subreddit, pool
    them, then sort by score — this reliably surfaces high-scoring posts
    regardless of a subreddit's post volume.
    """
    import random

    rc = cfg.reddit
    now = time.time()
    window_start = now - rc.archive_max_age_days * 86400
    window_end = now - rc.archive_min_age_days * 86400
    span_s = rc.archive_sample_span_days * 86400

    for sub_name in rc.subreddits:
        pool: list[_JsonPost] = []
        for _ in range(rc.archive_samples):
            anchor = random.uniform(window_start + span_s, window_end)
            data = _fetch_json(
                f"{ARCTIC_BASE}/api/posts/search",
                {"subreddit": sub_name, "limit": 100, "sort": "desc",
                 "before": int(anchor), "after": int(anchor - span_s)},
                cfg,
            )
            if data:
                pool.extend(_JsonPost(d) for d in data.get("data", []))

        pool = [p for p in pool if p.score >= rc.min_score]
        pool.sort(key=lambda p: p.score, reverse=True)
        yield from pool


def _fetch_comments_arctic(post: "_JsonPost", cfg) -> list["_JsonComment"]:
    data = _fetch_json(
        f"{ARCTIC_BASE}/api/comments/tree",
        {"link_id": post.fullname, "limit": cfg.reddit.max_comments * 3},
        cfg,
    )
    if not data:
        return []
    return [_JsonComment(c["data"]) for c in data.get("data", []) if c.get("kind") == "t1"]


# ── json backend (Reddit's own public endpoints — anti-bot-walled as of 2026) ─

class _JsonPost:
    """Exposes the same attribute names as a PRAW Submission, so downstream
    script-building code works unchanged regardless of backend."""

    def __init__(self, d: dict):
        self.title = d["title"]
        self.selftext = d.get("selftext") or ""
        self.score = d.get("score", 0)
        self.over_18 = bool(d.get("over_18", False))
        self.stickied = bool(d.get("stickied", False))
        self.subreddit = d.get("subreddit", "")
        author = d.get("author")
        self.author = author if author not in (None, "[deleted]") else None
        self.created_utc = d.get("created_utc", 0)
        self.fullname = d.get("name")
        self.id = d.get("id")
        self.total_awards_received = d.get("total_awards_received", 0) or 0


class _JsonComment:
    def __init__(self, d: dict):
        self.body = d.get("body") or ""
        self.score = d.get("score", 0)
        author = d.get("author")
        self.author = author if author not in (None, "[deleted]") else None
        self.created_utc = d.get("created_utc", 0)
        self.total_awards_received = d.get("total_awards_received", 0) or 0


def _user_agent(cfg) -> str:
    return cfg.reddit_user_agent or DEFAULT_USER_AGENT


def _fetch_json(url: str, params: dict, cfg) -> Optional[dict]:
    time.sleep(JSON_REQUEST_DELAY_S)
    try:
        resp = requests.get(
            url, params=params,
            headers={"User-Agent": _user_agent(cfg)},
            timeout=15,
        )
    except requests.RequestException as e:
        logger.warning("Reddit JSON request failed: %s (%s)", url, e)
        return None
    if resp.status_code == 429:
        logger.warning("Reddit rate-limited this request (429): %s", url)
        return None
    if resp.status_code != 200:
        logger.warning("Reddit JSON request returned %d: %s", resp.status_code, url)
        return None
    return resp.json()


def _candidate_posts_json(cfg):
    rc = cfg.reddit
    for sub_name in rc.subreddits:
        url = f"https://www.reddit.com/r/{sub_name}/{rc.listing}.json"
        params = {"limit": rc.fetch_pool_size, "raw_json": 1}
        if rc.listing == "top":
            params["t"] = rc.time_filter
        data = _fetch_json(url, params, cfg)
        if not data:
            continue
        for child in data.get("data", {}).get("children", []):
            if child.get("kind") == "t3":
                yield _JsonPost(child["data"])


def _fetch_comments_json(post: _JsonPost, cfg) -> list[_JsonComment]:
    url = f"https://www.reddit.com/comments/{post.id}.json"
    data = _fetch_json(url, {"sort": "top", "limit": cfg.reddit.max_comments * 3, "raw_json": 1}, cfg)
    if not data or len(data) < 2:
        return []
    children = data[1].get("data", {}).get("children", [])
    return [_JsonComment(c["data"]) for c in children if c.get("kind") == "t1"]


# ── praw backend (optional: requires a registered app + credentials) ────────

def _load_reddit_client(cfg):
    import praw

    if not (cfg.reddit_client_id and cfg.reddit_client_secret and cfg.reddit_user_agent):
        raise EnvironmentError(
            "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT are not set. "
            "Create a free 'script' app at https://www.reddit.com/prefs/apps and add them to .env, "
            "or set reddit.access: json in config.yaml to use unauthenticated public endpoints instead."
        )
    return praw.Reddit(
        client_id=cfg.reddit_client_id,
        client_secret=cfg.reddit_client_secret,
        user_agent=cfg.reddit_user_agent,
    )


def _candidate_posts_praw(reddit, cfg):
    rc = cfg.reddit
    for sub_name in rc.subreddits:
        subreddit = reddit.subreddit(sub_name)
        listing = subreddit.top(time_filter=rc.time_filter, limit=rc.fetch_pool_size) \
            if rc.listing == "top" else subreddit.hot(limit=rc.fetch_pool_size)
        for post in listing:
            yield post


def _fetch_comments_praw(post, cfg) -> list:
    post.comment_sort = "top"
    post.comments.replace_more(limit=0)
    return list(post.comments)


def _build_narrative_script(post, cfg) -> Optional[Script]:
    body = _strip_markdown(post.selftext)
    if _is_flagged(post.title + " " + body):
        return None

    chunks = _chunk_text(body)
    if not chunks:
        return None

    voice = cfg.tts.voices.a
    ts = _relative_time(post.created_utc)
    beats, card_meta = [], []

    for i, chunk in enumerate(chunks):
        beats.append(Beat(text=chunk, speaker="a", voice=voice))
        card_meta.append(RedditCardMeta(
            kind="post",
            is_first_of_unit=(i == 0),
            subreddit=str(post.subreddit),
            username=str(post.author) if post.author else "deleted",
            timestamp=ts,
            awards=getattr(post, "total_awards_received", 0) or 0,
            is_nsfw=post.over_18,
            title=post.title if i == 0 else None,
        ))

    return _finish_script(post, beats, card_meta, cfg)


def _build_qa_script(post, comments: list, cfg) -> Optional[Script]:
    if _is_flagged(post.title):
        return None

    rc = cfg.reddit
    voice = cfg.tts.voices.a
    beats = [Beat(text=post.title, speaker="a", voice=voice)]
    card_meta = [RedditCardMeta(
        kind="post",
        is_first_of_unit=True,
        subreddit=str(post.subreddit),
        username=str(post.author) if post.author else "deleted",
        timestamp=_relative_time(post.created_utc),
        awards=getattr(post, "total_awards_received", 0) or 0,
        is_nsfw=post.over_18,
        title=post.title,
    )]

    target_words = cfg.video.target_duration_max * 0.8 * WORDS_PER_SEC
    used = 0
    total_words = len(post.title.split())
    for comment in comments:
        if used >= rc.max_comments or total_words >= target_words:
            break
        body = getattr(comment, "body", "")
        if body.strip() in _REMOVED_MARKERS:
            continue
        if comment.score < rc.min_comment_score:
            continue
        author = str(comment.author) if comment.author else None
        if author is None or author.lower() == "automoderator":
            continue
        text = _strip_markdown(body)
        if _is_flagged(text):
            continue
        chunks = _chunk_text(text)
        if not chunks:
            continue

        ts = _relative_time(comment.created_utc)
        for i, chunk in enumerate(chunks):
            beats.append(Beat(text=chunk, speaker="a", voice=voice))
            card_meta.append(RedditCardMeta(
                kind="comment",
                is_first_of_unit=(i == 0),
                subreddit=str(post.subreddit),
                username=author,
                timestamp=ts,
                awards=getattr(comment, "total_awards_received", 0) or 0,
                is_nsfw=False,
                title=None,
            ))
        total_words += len(text.split())
        used += 1

    if used < rc.min_comments:
        # Too little discussion for a satisfying video length — reject so the
        # caller moves on to the next candidate instead of settling for a thin one.
        return None

    return _finish_script(post, beats, card_meta, cfg)


# Base tags researched against what's actually popular for this content niche
# (TikTok/Instagram "reddit story"/storytime content) as of 2026-09-02, plus a
# per-post subreddit tag appended dynamically.
BASE_TAGS = ["storytime", "redditstories", "fyp"]


def _hashtags_for(post) -> list[str]:
    sub = str(post.subreddit).lower()
    tags = [sub] + BASE_TAGS
    seen = set()
    return [t for t in tags if not (t in seen or seen.add(t))]


# Deliberately NOT attributed to the post/commenter (no fake username, no fake
# subreddit header — renders as a plain unheaded card, is_first_of_unit=False)
# since this is our own video's outro, not part of the real sourced content.
# Misrepresenting it as something the real Redditor said would be dishonest.
CTA_LINES = [
    "drop a comment if you want part 2",
    "comment 'update' if you want to know what happened next",
    "let me know in the comments if you want part 2",
    "comment below if you want the update on this one",
]


def _cta_beat(post, cfg) -> tuple[Beat, RedditCardMeta]:
    import random
    text = random.choice(CTA_LINES)
    beat = Beat(text=text, speaker="a", voice=cfg.tts.voices.a)
    meta = RedditCardMeta(
        kind="cta",
        is_first_of_unit=False,
        subreddit=str(post.subreddit),
        username="",
        timestamp="",
    )
    return beat, meta


def _finish_script(post, beats: list[Beat], card_meta: list[RedditCardMeta], cfg) -> Script:
    cta_beat, cta_meta = _cta_beat(post, cfg)
    beats = beats + [cta_beat]
    card_meta = card_meta + [cta_meta]

    tags = _hashtags_for(post)
    # Rebuilt fresh at publish time too (see src/captions.py + cli.py's
    # _publish_row) — this generation-time value isn't relied on as final.
    caption = build_caption("reddit_story", post.title, tags)
    return Script(
        beats=beats,
        title=post.title[:120],
        hook=post.title,
        tags=tags,
        caption=caption,
        premise=post.fullname,   # t3_xxx — unique per post, drives dedup
        card_meta=card_meta,
    )


def generate_reddit_script(cfg, db_conn) -> Script:
    """Fetch a real, non-duplicate Reddit post/thread and build a Script.
    Mirrors generate_script()'s interface so cli.py can call either interchangeably."""
    rc = cfg.reddit

    if rc.access == "praw":
        reddit = _load_reddit_client(cfg)
        candidates = lambda: _candidate_posts_praw(reddit, cfg)
        fetch_comments = _fetch_comments_praw
    elif rc.access == "json":
        candidates = lambda: _candidate_posts_json(cfg)
        fetch_comments = _fetch_comments_json
    else:
        candidates = lambda: _candidate_posts_arctic(cfg)
        fetch_comments = _fetch_comments_arctic

    for attempt in range(1, MAX_RETRIES + 1):
        found = False
        for post in candidates():
            found = True
            if post.stickied or post.score < rc.min_score:
                continue
            if post.over_18 and not rc.allow_nsfw:
                continue
            if db_conn is not None and premise_exists(db_conn, post.fullname):
                continue
            if len(post.title.split()) > MAX_TITLE_WORDS:
                # A long, rambling title reads weak as a hook (the title is the
                # very first thing shown/read — the single biggest retention
                # signal). Score already proxies "got attention," but doesn't
                # guarantee a punchy title, so filter separately.
                continue

            has_body = len(post.selftext.strip()) > 40
            mode = rc.mode
            if mode == "auto":
                mode = "narrative" if has_body else "qa"

            if mode == "narrative":
                script = _build_narrative_script(post, cfg)
            else:
                comments = fetch_comments(post, cfg)
                script = _build_qa_script(post, comments, cfg)
            if script is None:
                continue

            total_words = sum(len(b.text.split()) for b in script.beats)
            est_duration_s = total_words / WORDS_PER_SEC
            # The word-count estimate runs consistently LOW vs actual TTS output —
            # observed actual/estimated ratios of 1.12x and 1.28x across two real
            # videos (57.7s est -> 74.1s actual; 97.0s est -> 108.4s actual, which
            # overshot the 90s cap). Ceiling tightened accordingly; revisit with
            # more data points if overshoots keep happening even at this margin.
            too_short = est_duration_s < cfg.video.target_duration_min * 0.9
            too_long = est_duration_s > cfg.video.target_duration_max * 0.8
            if too_short or too_long:
                logger.info(
                    "Skipping %r — estimated %.0fs, outside target range (r/%s, %s)",
                    post.title[:60], est_duration_s, post.subreddit, mode,
                )
                continue

            logger.info(
                "Reddit script accepted: %r (r/%s, %s, %d beats)",
                script.title, post.subreddit, mode, len(script.beats),
            )
            return script

        if not found:
            break
        logger.warning("No usable, non-duplicate post found in this pass (attempt %d)", attempt)

    raise RuntimeError(
        "Could not find a usable, non-duplicate Reddit post after "
        f"{MAX_RETRIES} passes. Try widening reddit.subreddits, lowering "
        "reddit.min_score, or increasing reddit.fetch_pool_size in config.yaml."
    )

"""
Shared caption-building logic for every content format — hashtags included,
no emojis, no AI-disclosure line, hook-only text (not the full description).

Deliberately called at PUBLISH time (see cli.py's _publish_row), not just
baked into the script at generation time — a video generated before a
caption-convention change would otherwise carry a stale caption forever,
since nothing re-derives it later. Centralizing it here also means
generate/reddit.py and generate/textchain.py both call the same logic
instead of maintaining their own copies that can drift apart.
"""

import re

EMOJI_PATTERN = re.compile(
    "[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U00002B00-\U00002BFF️]+",
    flags=re.UNICODE,
)

TEXTCHAIN_BASE_TAGS = ["storytime", "texts", "drama", "fyp"]
TERRARIA_BASE_TAGS = ["terraria", "terrariatips", "gaming"]
TERRARIA_CREDIT = "Info from the Terraria Wiki (terraria.wiki.gg)"


def strip_emoji(text: str) -> str:
    return EMOJI_PATTERN.sub("", text).strip()


def _clean_tag(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


def _dedupe(tags: list[str]) -> list[str]:
    seen = set()
    return [t for t in tags if t and not (t in seen or seen.add(t))]


YOUTUBE_TITLE_MAX = 100
YOUTUBE_TAGS_MAX_CHARS = 450   # YouTube's limit is 500; leave room for its own counting


def _youtube_safe(text: str) -> str:
    # YouTube rejects < and > in titles and descriptions outright.
    return re.sub(r"\s+", " ", strip_emoji(text).replace("<", "").replace(">", "")).strip()


def build_youtube_metadata(content_format: str, script) -> tuple[str, str, list[str]]:
    """(title, description, tags) for a YouTube Short.

    The title is the most visible text on a Short, so it uses the title card's
    line (or the script title) rather than the Instagram-style hook. The
    description carries the hook, the hashtags and - for terraria - the wiki
    credit the CC BY-NC-SA licence requires, exactly as the Instagram caption does.
    """
    base = (script.cover_line or script.title or script.hook).replace("*", "")
    # The game name in the title is what YouTube search matches on; the
    # cover lines rarely say it ("Pylons break your travel").
    suffix = " #terraria #shorts" if content_format == "terraria" else " #shorts"
    title = _youtube_safe(base)
    room = YOUTUBE_TITLE_MAX - len(suffix)
    if len(title) > room:
        title = title[:room].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    title += suffix

    caption = build_caption(content_format, script.hook, script.tags)
    if content_format == "terraria" and script.source_url:
        caption += f"\n{script.source_url}"
    description = "\n".join(_youtube_safe(line) for line in caption.split("\n"))[:4900]

    tags, used = [], 0
    for t in re.findall(r"#(\w+)", caption) + ["shorts"]:
        if t in tags or used + len(t) + 1 > YOUTUBE_TAGS_MAX_CHARS:
            continue
        tags.append(t)
        used += len(t) + 1
    return title, description, tags


def build_caption(content_format: str, hook: str, tags: list[str]) -> str:
    """
    hook: the video's hook/title line — used as-is (partial/teaser caption,
    not the full description).
    tags: for textchain, raw LLM-suggested tags (cleaned, capped to 3, and
    combined with TEXTCHAIN_BASE_TAGS here). For reddit_story (and any future
    sourced-content format), the already-intended final hashtag set (e.g.
    [subreddit, "storytime", "redditstories", "fyp"]) — just cleaned/deduped.
    """
    hook_text = strip_emoji(hook)

    if content_format == "textchain":
        cleaned = [_clean_tag(t) for t in tags if t]
        cleaned = [t for t in cleaned if t][:3]
        final_tags = _dedupe(cleaned + TEXTCHAIN_BASE_TAGS)
    elif content_format == "terraria":
        cleaned = [_clean_tag(t) for t in tags if t]
        cleaned = [t for t in cleaned if t][:3]
        final_tags = _dedupe(TERRARIA_BASE_TAGS + cleaned)
    else:
        final_tags = _dedupe([_clean_tag(t) for t in tags if t])

    hashtags = " ".join(f"#{t}" for t in final_tags)
    caption = f"{hook_text}\n\n{hashtags}" if hashtags else hook_text
    if content_format == "terraria":
        # The wiki's text is CC BY-NC-SA, which requires attribution.
        caption += f"\n\n{TERRARIA_CREDIT}"
    return caption

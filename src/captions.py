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


def strip_emoji(text: str) -> str:
    return EMOJI_PATTERN.sub("", text).strip()


def _clean_tag(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


def _dedupe(tags: list[str]) -> list[str]:
    seen = set()
    return [t for t in tags if t and not (t in seen or seen.add(t))]


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
    else:
        final_tags = _dedupe([_clean_tag(t) for t in tags if t])

    hashtags = " ".join(f"#{t}" for t in final_tags)
    return f"{hook_text}\n\n{hashtags}" if hashtags else hook_text

"""
Tests that the card reset path works correctly.

A reset occurs when adding the next bubble would push the messages container
past --card-max-content-h (880px). Continuation cards must render without the
header. This is the most load-bearing logic in the renderer and was never
exercised by the 9-message sample.
"""

import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.render.cards import CardRenderer, ENTRY_FRAMES

OUTDIR = Path(__file__).parent.parent / "output" / "work" / "test_reset"
MAX_CONTENT_H = 880  # must match --card-max-content-h in card.css

# 30-message script, alternating with some same-speaker runs.
# Long messages push the card height up faster to ensure multiple resets.
MESSAGES = [
    {"speaker": "a", "text": "hey i need to tell you something important, please dont freak out"},
    {"speaker": "a", "text": "i've been keeping this from you for a while now"},
    {"speaker": "b", "text": "ok now you're actually scaring me what's going on"},
    {"speaker": "b", "text": "just tell me"},
    {"speaker": "a", "text": "you know how i've been working late every thursday night"},
    {"speaker": "a", "text": "the overtime hours i told you about"},
    {"speaker": "b", "text": "yeah you said it was the quarterly audit stuff"},
    {"speaker": "a", "text": "i lied. i wasn't at work"},
    {"speaker": "b", "text": "...what"},
    {"speaker": "b", "text": "where were you then"},
    {"speaker": "a", "text": "i was taking night classes. graphic design"},
    {"speaker": "a", "text": "i've been doing it for six months"},
    {"speaker": "b", "text": "wait why would you hide that from me"},
    {"speaker": "b", "text": "thats not even bad??"},
    {"speaker": "a", "text": "because you always said my art stuff was a waste of time"},
    {"speaker": "a", "text": "remember when you made fun of my sketchbook in front of your friends"},
    {"speaker": "b", "text": "i was joking that was like two years ago"},
    {"speaker": "a", "text": "it didn't feel like a joke"},
    {"speaker": "b", "text": "i'm really sorry. i didn't know that hurt you"},
    {"speaker": "b", "text": "i actually love your art. i have your first drawing framed on my desk at work"},
    {"speaker": "a", "text": "wait you do??"},
    {"speaker": "b", "text": "yeah. the one with the lighthouse. people ask about it all the time"},
    {"speaker": "a", "text": "i didn't know that"},
    {"speaker": "a", "text": "i thought you never noticed"},
    {"speaker": "b", "text": "i notice everything you make"},
    {"speaker": "b", "text": "i just don't always say it right"},
    {"speaker": "a", "text": "there's more i have to tell you though"},
    {"speaker": "b", "text": "more??"},
    {"speaker": "a", "text": "i got offered a junior design role at a studio. they saw my portfolio from class"},
    {"speaker": "b", "text": "oh my god. are you serious right now?? you have to take it!!!"},
]
DURATIONS = [2000] * len(MESSAGES)


def test_frame_count():
    r = CardRenderer(outdir=OUTDIR)
    frames = r.render_script(MESSAGES, DURATIONS)
    expected = len(MESSAGES) * (ENTRY_FRAMES + 1)
    assert len(frames) == expected, (
        f"Expected {expected} frames ({len(MESSAGES)} msgs × {ENTRY_FRAMES + 1}), "
        f"got {len(frames)}"
    )


def test_reset_occurs():
    r = CardRenderer(outdir=OUTDIR)
    r.render_script(MESSAGES, DURATIONS)
    assert len(r.card_boundaries) > 1, (
        f"No reset occurred. card_boundaries={r.card_boundaries}. "
        "Script may not be long enough or max_h may be too large."
    )
    print(f"\n  Cards produced: {len(r.card_boundaries)}")
    print(f"  Reset at message indices: {[b['msg_index'] for b in r.card_boundaries[1:]]}")


def test_first_card_has_header():
    r = CardRenderer(outdir=OUTDIR)
    r.render_script(MESSAGES, DURATIONS)
    assert r.card_boundaries[0]["show_header"] is True, "First card must have a header"


def test_continuation_cards_have_no_header():
    r = CardRenderer(outdir=OUTDIR)
    r.render_script(MESSAGES, DURATIONS)
    for boundary in r.card_boundaries[1:]:
        assert boundary["show_header"] is False, (
            f"Continuation card at msg {boundary['msg_index']} incorrectly has show_header=True"
        )


def test_no_card_exceeds_max_height():
    """
    Re-run the height measurement logic using playwright to verify each card's
    final content height stays within the budget.
    """
    r = CardRenderer(outdir=OUTDIR)
    frames = r.render_script(MESSAGES, DURATIONS)
    boundaries = r.card_boundaries

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)

        for b_idx, boundary in enumerate(boundaries):
            start = boundary["msg_index"]
            end = boundaries[b_idx + 1]["msg_index"] if b_idx + 1 < len(boundaries) else len(MESSAGES)
            show_header = boundary["show_header"]

            from src.render.cards import _prepare
            prepared = _prepare(MESSAGES)
            card_msgs = [dict(m) for m in prepared[start:end]]

            h = r._natural_h(page, card_msgs, show_header)
            assert h <= MAX_CONTENT_H, (
                f"Card {b_idx} (msgs {start}–{end - 1}) has content height {h:.1f}px "
                f"which exceeds limit of {MAX_CONTENT_H}px"
            )
            print(f"\n  Card {b_idx}: msgs {start}–{end - 1}, header={'yes' if show_header else 'no'}, h={h:.1f}px")

        browser.close()


if __name__ == "__main__":
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short", "-s"],
        cwd=Path(__file__).parent.parent,
    )
    sys.exit(result.returncode)

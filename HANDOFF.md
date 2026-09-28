# Short-form content pipeline — handoff (as of 2026-09-28)

This supersedes any earlier `HANDOFF.md`/`SPEC.md`/`PIPELINE.md`/`PUBLISHING.md`.
If anything here conflicts with older docs or with what the code actually
does, **the running code wins** — this file describes reality as verified
today, not a plan. Read it, then spot-check anything load-bearing against the
actual files before relying on it (docs drift, code doesn't lie).

## What this is

A local pipeline that generates short-form vertical videos (real Reddit posts
narrated by a cloned AI voice over gameplay/ambient background footage) and
publishes them to TikTok and Instagram. Target: 10 videos/day eventually,
60-90s each, 1080x1920, 30fps — starting at 2-3/day until quality is proven.

**Hard constraints:** $0 infrastructure (no paid APIs/cloud — the one
knowingly-accepted exception was ElevenLabs' free tier, since abandoned; see
below), no GUI automation, no MoviePy, everything local. Repo lives on the
WSL2 Linux filesystem (`~/short-form-content-pipeline`), never under `/mnt/c/`.

## Architecture — what actually runs

Stages: `generate → tts → render → assemble → publish`, each a `src/cli.py`
subcommand, orchestrated by `run-all`. SQLite at `data/state.db` tracks one
row per video through `generated → tts_done → rendered → assembled →
uploaded | failed`. Run `python -m src.cli status` and `... monitor` anytime
for a live read on queue state + platform token health.

### Content generation — real Reddit posts, not AI-written fiction

`src/sources/reddit.py`. Sources real posts via **Project Arctic Shift**
(`arctic-shift.photon-reddit.com`), a free no-auth Reddit archive — chosen
after Reddit's own API self-service registration turned out to be closed in
2026 (replaced by a manual "Responsible Builder Policy" approval process) and
Reddit's public `.json` endpoints turned out to sit behind an anti-bot wall.
Not live data, but irrelevant here — target subreddits: AskReddit,
menofreddit, tifu, AmItheAsshole, relationship_advice, confession (ordered —
earlier ones tried first). Two content shapes auto-detected per post:
narrative (chunked post body) or qa (question + top comments, one card per
commenter). Every script ends with a generic, honestly-unattributed CTA card
("comment for part 2" style — never faked as something the real poster said).

Real gotchas already fixed, don't reintroduce:
- **Duration estimate needs recalibrating whenever the TTS voice changes** —
  `WORDS_PER_SEC` in `reddit.py` is tied to the current voices' speaking
  cadence (currently 7.0 w/s; was 4.75 before the Sept 2026 voice swap).
  Ceiling margin is `target_duration_max * 0.8` (not 1.0-1.1x) because actual
  render duration consistently runs higher than the word-count estimate.
- Popularity sourcing samples random time-windows across the whole age
  range (`archive_min/max_age_days`) rather than one contiguous slice —
  a single slice badly undersamples high-volume subs like AskReddit.
- `min_score` currently 2000 (bumped from 500 per explicit request to bias
  toward clearly viral posts).

### TTS — Chatterbox, not Kokoro/Piper/ElevenLabs

`src/tts/chatterbox.py`. Open-weight local voice cloning, free, CPU-only
(~15-19s per short sentence — no GPU on this machine). Reference clips at
`assets/voices/chatterbox_ref_{female,male}.wav` are **real human/AI-voice
audio the user supplied**, not Piper-synthesized — Chatterbox reproduces the
reference's own audio quality along with its voice, so a synthetic/lossy
source measurably degrades the clone (this caused several rounds of "sounds
robotic/nasal" complaints before landing on real sources). The female
reference specifically had to be extracted from a two-speaker sample via
frame-level pitch analysis (coarse per-second averaging let 25-50% of the
wrong speaker's audio through undetected) — don't trust bucketed pitch stats
to declare a source single-speaker; use a smoothed local-purity gate instead.
Post-processing pitch-shifts on the *output* (tried both naive and
formant-preserving) made quality worse, not better — if a voice's character
needs adjusting, change the reference clip, not the generated audio.

Python 3.14 install gotchas: needs `setuptools<81` pinned (chatterbox's
`perth` dependency imports the now-removed `pkg_resources`), and
`soundfile.write()` instead of `torchaudio.save()` (needs a separate
`torchcodec` package otherwise).

### Rendering — two independent formats, don't conflate them

- `src/render/cards.py` — iMessage-style chat bubbles, growth/reset animation,
  fully tested (`tests/test_reset.py`, 5/5 passing). Content comes from
  `src/generate/textchain.py` (LLM-written fake text-chain drama). Not the
  primary active format, but a `textchain`-format video got accidentally
  auto-published to Instagram from a stale test backlog (see Scheduling
  section) and unexpectedly performed well — user chose to keep it live
  rather than delete it, so this format is back in active rotation too.
- `src/render/reddit_cards.py` — dark Reddit-UI-style static cards, **no
  growth animation at all** (deliberately different from the chat renderer —
  real reference-video analysis showed this format displays as full-block
  cards, not bubbles growing in). Content comes from `src/sources/reddit.py`
  (real sourced posts). Primary active format
  (`content.format: reddit_story` in `config.yaml`).

Both share nothing visually on purpose — don't try to unify their CSS.

**Caption convention (both formats):** hashtags included, no AI-disclosure
line, no emojis, only a short/partial hook line rather than the full
description. As of 2026-09-04 this is built by a single shared function,
`build_caption()` in `src/captions.py`, called both at generation time and
— critically — again at **publish time** inside `cli.py`'s `_publish_row`.
Publish-time recomputation exists because a real bug surfaced: a backlog
video generated before an earlier caption fix sat dormant for days and then
got published with the stale pre-fix caption, since nothing had recomputed
it. Now every publish always uses the current convention regardless of how
old the row is. Don't reintroduce a generation-time-only caption — always
route caption changes through `src/captions.py` so both call sites stay
correct automatically.

**Content format is now randomized per video (2026-09-04):** `config.yaml`'s
`content.format: random` makes `cmd_generate` pick `reddit_story` vs
`textchain` per call. Downstream stages already keyed off the per-row
`content_format` DB column, so this was a small, low-risk change.

### Assembly — `src/assemble/build.py`

ffmpeg-only, concat-demuxer approach, loudnorm on voice, background music
mixed quietly (`assets/music/soft_ambient.wav`, synthesized procedurally —
copyright-clean; the user deferred supplying real royalty-free tracks to
later). `cli.py`'s `_cover_ms_for()` computes a safe cover-frame timestamp per
content format so the platform thumbnail never lands on a broken mid-animation
frame. Output duration/resolution/streams are asserted before a video is ever
considered "assembled" — a silently-broken video that uploads is worse than a
failed build.

**Background clip selection (updated 2026-09-03):** `assets/backgrounds/
normalized/` now holds multiple clips (gameplay footage of varying aspect
ratios, normalized to 9:16 at ingest via `cli.py ingest`); one is picked at
random per video (pre-existing behavior). New: `_background_start_offset()`
also picks a random start point within the chosen clip (rather than always
0:00), constrained to leave the video's full duration + a 60s safety margin
before the clip's own end, so the `-stream_loop -1` looping never has to wrap
back to the clip's start mid-video (a visible jump). Falls back to 0 if the
clip is too short for the video + margin.

### Publishing — Instagram fully live, TikTok in progress

**Instagram (`src/publish/instagram.py`): done, tested, live.** Uses
resumable local-file upload (`upload_type=resumable` + `rupload.facebook.com`,
NOT the `video_url` path — that needs public hosting, wrong for a local
pipeline). Access token is a **non-expiring Page token** obtained via a
long-lived User token — this specific shape has no refresh endpoint at all
(the `ig_refresh_token` grant type is for a *different* token system —
confirmed live, not assumed). If it's ever rejected, the fix is redoing the
manual OAuth consent flow, not an automated retry. Setup required going
through "API setup with Facebook login" (not "Instagram login" — that's a
different, messaging-only product), adding the account as an **Instagram
Tester** under App Roles before OAuth would even respond, and generating the
token via Graph API Explorer (Facebook rejects `facebook.com` as your own
redirect URI, killing the classic manual-token-grab trick).

**TikTok (`src/publish/tiktok.py`): working today in `inbox` mode, `direct`
mode implemented and tested but blocked pending review.**
- `tiktok.post_mode: inbox` (**current setting**) — draft lands in the user's
  TikTok mobile app inbox, human finishes posting. Zero review needed, works
  today. **The API-sent caption/hashtags are structurally ignored by this
  flow** — confirmed via independent third-party TikTok integration docs, not
  a bug, not fixable in code, not a Sandbox-specific limitation. The pipeline
  compensates by auto-copying each video + a ready-to-paste `caption.txt` to
  `cfg.output.mobile_sync_dir` (a OneDrive-synced folder) at assemble time, so
  the caption is one tap away on the phone that does the actual posting.
- `tiktok.post_mode: direct` — caption/hashtags apply automatically via the
  API, but while the app is unaudited, TikTok's backend flat-out rejects the
  post (`unaudited_client_can_only_post_to_private_accounts`) unless the
  target account is manually set to Private at the moment of posting — no API
  exists to toggle that, so this path would actually require *more* manual
  work than inbox mode, not less. Not worth using until the app clears review.
- **App submitted for TikTok's production review 2026-09-03**, currently
  awaiting approval (their docs suggest 2 days to 2 weeks). Once approved:
  flip `tiktok.post_mode` to `direct` in `config.yaml` — the code path is
  already written and live-tested (`query_creator_info()` +
  the `mode == "direct"` branch in `upload_draft()`), only actually blocked by
  the unaudited-account restriction, which approval should lift.
- Sandbox and Production are **entirely separate app configs** — products/
  scopes added under one don't carry to the other. `video.publish` needed a
  "Direct Post" toggle inside Content Posting API's own settings (separate
  from just requesting the scope) enabled under *both* Sandbox and Production
  independently.
- `cli.py publish --platform {both,instagram,tiktok}` exists specifically so
  testing one platform never accidentally live-posts to the other. A
  single-platform run never marks a row fully `uploaded` (only the default
  `both` path does), so a later publish to the untouched platform isn't
  blocked by the already-uploaded idempotency guard.
- **Chunked upload rule (fixed twice, 2026-09-03 — read this before touching
  `_chunk_plan`/`_upload_chunks` again):** files ≤64MB must be sent as
  exactly ONE chunk. For files over that, TikTok's actual rule (from its
  Media Transfer Guide) is `total_chunk_count = file_size // chunk_size`
  (**floor**, not ceil) with a **fixed** `chunk_size`, and the trailing
  remainder folds into the LAST chunk (allowed to exceed `chunk_size`, up to
  128MB) rather than becoming its own chunk. An even-split (ceil-based)
  attempt still failed live with the same `invalid_params: The total chunk
  count is invalid` error — floor + remainder-in-last-chunk is what actually
  works, verified live on a 67.7MB file (6×10MB + one ~14.6MB final chunk).

### Full automation — randomized daily scheduling (built 2026-09-03)

`cli.py auto-publish`, driven by `cfg.schedule` in `config.yaml`
(`posts_per_day`, `window_start_hour`/`window_end_hour`, `min_gap_minutes`,
`platform`). On first call each day it randomizes N target times within the
window (reject-and-resample so no two are closer than `min_gap_minutes`),
stores them in the `schedule_slots` DB table; every subsequent call publishes
the oldest ready `assembled` video for any due-but-unfired slot. An empty
queue when a slot comes due doesn't burn the slot — it retries next call.
Two Windows Task Scheduler jobs (`SFCP-Pipeline` for `run-all`,
`SFCP-AutoPublish` for `auto-publish`), both `/sc minute /mo 15`, each
invoking `wsl.exe -d Ubuntu -e bash -lc "cd ~/short-form-content-pipeline &&
.venv/bin/python -m src.cli <cmd> >> logs/<name>.log 2>&1"`.

**Critical bug found + fixed later the same day: run-all was publishing
immediately, not at the scheduled time.** `cmd_run_all`'s stage map had
`"assembled": cmd_publish` — so the moment a video finished assembling,
run-all's very next tick published it instantly, racing (and always beating)
`auto-publish`'s randomized slots. Confirmed via `logs/run-all.log` showing
immediate publish calls right after "Advancing video N from status=assembled",
while `logs/auto-publish.log` kept logging "no assembled video is ready" for
the same slot. **Fixed** by removing `assembled` from run-all's stage map and
adding it to run-all's pending-query exclusion list — run-all now stops at
`assembled` and only `auto-publish` may claim and publish those rows. This
also gives the "always have a backlog ready" behavior for free: once a video
reaches `assembled`, run-all sees an empty queue and starts the next one, so
a backlog naturally accumulates over time (gated by the storage cap below,
not an artificial count).

**Backlog storage cap (built 2026-09-03):** `cfg.output.mobile_sync_cap_gb`
(default 1.0 GB) in `config.yaml`. After every sync to the OneDrive
`ready_to_post` folder, `_enforce_mobile_sync_cap()` deletes the oldest
video+caption pairs (by file mtime) until the folder is back under the cap.
Scoped only to that OneDrive courtesy copy — the actual publish stage
uploads from `output.ready_dir` (separate, WSL-local), which this cleanup
never touches, so it can't ever delete something mid-publish or not-yet-
posted from the platform's perspective. `output.ready_dir` itself is NOT
capped and grows unboundedly — not yet addressed.

**Two real incidents from the first live test, both fixed:**
1. The very first `auto-publish` run grabbed a stale test video from a
   backlog of ~12 old `assembled` rows and published it live to both
   platforms unintentionally. Fixed by archiving all pre-existing stale rows
   (`status='archived'`), adding `'archived'` to `run-all`'s pending-work
   exclusion list, and adding per-platform duplicate-post protection to
   `_publish_row` (skip a platform whose `*_id` is already set). **Before
   ever running `auto-publish` fresh in a new session, sanity-check
   `SELECT status, count(*) FROM videos GROUP BY status` — a stale backlog
   will get published on the very first tick.**
2. `_publish_row()` originally only caught missing-credentials exceptions —
   a real failure on one platform (e.g. the chunk-count bug above) crashed
   the whole function before it could record the other platform's success or
   finalize the row's status/schedule slot. Fixed with a broad
   `except Exception` around each platform's publish attempt. **If a video
   is ever stuck at `status='uploading'`:** check which of
   `instagram_id`/`tiktok_id` is NULL, manually retry only that platform,
   then call `mark_slot_fired()` if a schedule slot is involved — re-running
   `auto-publish` won't re-attempt a row already claimed into `uploading`.

### Migrating off Windows Task Scheduler — GitHub Actions, no card required (2026-09-07/09)

Windows Task Scheduler only runs while the host PC is on/awake with network
access — it went dark during a real outage and missed two scheduled posts
(caught up manually afterward). Wanted to move to an always-on host instead.
**First choice was Oracle Cloud's Always Free VM** (see git history for that
plan — scripts/provision_vm.sh and scripts/pipeline.cron still exist from
that attempt) — but every major cloud VM free tier (Oracle, AWS, GCP, Azure)
requires a card on file for fraud prevention, which the user explicitly
didn't want. **Pivoted to GitHub Actions on this public repo instead** —
genuinely free, zero billing setup, no card, ever. This needed real rework
(flagged as the tradeoff when this option was first considered) since each
scheduled run starts from a blank machine — here's what that rework turned
into, actually built and verified working 2026-09-09:

**`cli.py cloud-tick`** (new command, distinct from `run-all`/`auto-publish`
which are still used by the VM/local path and untouched): reuses
`_ensure_and_get_due_slots()` (factored out of `cmd_auto_publish`) to check
whether a randomized slot is due. If not, it exits almost immediately — most
scheduled runs cost near-zero Actions minutes. If a slot **is** due, it runs
`cmd_generate` → `cmd_tts` → `cmd_render` → `cmd_assemble` → `_publish_row`
for ONE video synchronously, all inside the same process/run. This is a
deliberate design difference from the VM path's backlog: an ephemeral runner
has no always-on disk to hold a backlog of assembled-but-unpublished videos
between separate runs, so nothing partially-finished needs to survive
between invocations — generation and publishing happen atomically together,
on demand, only when a slot actually comes due.

**State persistence — `data/state.db` is now tracked in git**, the only
thing that needs to survive between ephemeral runs (schedule slots, premise
dedup, published post IDs). `.gitignore` changed from ignoring all of `data/`
to `data/*` + `!data/state.db`. **Critical safeguard**: the `tokens` table
(where refreshed API access/refresh tokens get cached) is `DELETE FROM
tokens`-scrubbed before every commit, in the workflow, right before `git
add` — this repo is public, and a real token in that table would otherwise
get pushed in plaintext. `_current_token()` already falls back to the
env-supplied secret when the DB has none, so scrubbing costs nothing
functionally. **Before ever hand-editing or hand-committing this file,
re-verify that table is empty first.**

**Large binary assets (background clips, Chatterbox/Piper voice files,
ambient music, ~1.65GB total) live on a GitHub Release (`assets-v1`), not
Git LFS** — they exceed LFS's 1GB free storage tier. The workflow downloads
them once via `gh release download` and caches the result via
`actions/cache` (key `pipeline-assets-v1`) so subsequent runs don't
re-download. Note GitHub sanitizes release-asset filenames — spaces became
dots (`"minecraft parkour.mp4"` → `minecraft.parkour.mp4`); harmless, since
nothing in the pipeline cares about background-clip filenames, but the
workflow's download step matches the sanitized names, not the originals.

**Model weights**: Chatterbox's ~3GB HuggingFace download is cached via
`actions/cache` (key `chatterbox-model-v1`) against `~/.cache/huggingface`.

**Secrets**: pushed to the repo's GitHub Actions secrets (Settings → Secrets
and variables → Actions) — `GEMINI_API_KEY` and the `INSTAGRAM_*`/`TIKTOK_*`
vars, same names as `.env`, injected as env vars in the workflow step (not
via a checked-out `.env` file). GitHub-hosted runners are x86_64, so the
ARM-wheel-availability risk that would have applied to the Oracle VM path
never came up here.

**Workflow**: `.github/workflows/pipeline.yml`, `on: schedule` (`*/20 * * *
*`, accepting GitHub's documented occasional lateness under load — fine
since posting times were already approximate) plus `workflow_dispatch` for
manual test runs, `concurrency: group: pipeline` so overlapping runs can't
both try to publish or both try to push `data/state.db`, `permissions:
contents: write` so the job can push its own commits back.

**Verified live 2026-09-09**: a manual `workflow_dispatch` run succeeded
end-to-end after secrets were set (an earlier scheduled run had failed
before secrets existed — `GEMINI_API_KEY is not set`, expected and harmless,
fixed once secrets were pushed). Confirm current status via `gh run list
--workflow=pipeline.yml` or the Actions tab.

**Once confirmed reliable**, the Windows Task Scheduler jobs
(`SFCP-Pipeline`, `SFCP-AutoPublish`) on the original PC need to be disabled
— running both the PC scheduler and this workflow simultaneously would have
them racing against and diverging from each other's copy of the schedule/
video state.

### Reliability fixes after the first two weeks of live running

Four fixes landed 2026-09-14/15, after the Actions migration but before this
section existed — all in git, none of them previously written down here:
- Schedule slots were being marked fired even when the publish actually
  failed, so days went by with the workflow reporting success and no real post.
- Slots were permanently lost across a midnight rollover when a gap in
  GitHub's trigger cadence let the day turn over before they were checked.
  `due_unfired_slots` is no longer date-scoped — it keeps offering an overdue
  slot for `CATCHUP_MAX_AGE_HOURS` (24) regardless of date.
- `cloud-tick`'s retry tried to reuse an `assembled` row's mp4, which cannot
  exist on a later ephemeral runner. It always generates fresh now.
- Instagram upload retries got spacing instead of firing back-to-back.

Then 2026-09-27, after roughly **a third of all publishes had been failing**
for a week (21 failed rows against 58 uploaded). All four causes below were
found by reading the Actions logs — the DB's own error text said only
"Publish failed on the GitHub Actions runner", which is why this went a week
without being diagnosed. **When publishes fail, go to the run logs, not the
`videos.error` column.**

**Instagram was rejecting uploads at the transcode step** ("Video Transcoding
Error: both HD and SD progressive failed to transcode"), *after* the file had
already passed `_verify_output()`. The output was structurally valid but
outside what IG's transcoder reliably accepts. Four things, all fixed in
`_build_cmd`: mono 44.1kHz audio (Chatterbox's native rate, passed straight
through) is now 48kHz stereo; uncapped CRF averaging ~7.5Mbps with large spikes
on busy gameplay is now `-maxrate 5M -bufsize 10M`; libx264's default ~8.3s
scenecut-driven GOP is now a fixed 2s GOP; colour metadata inherited from
whatever the source clip carried is now explicitly bt709. There is a block
comment above `_build_cmd` with the full reasoning — **read it before
loosening any of those**. This also explains why the failures looked random
rather than tied to one background clip: they tracked how busy the chosen clip
happened to be at the randomly-picked start offset. Side benefit, output is
15-30% smaller, so uploads are quicker too.

**The upload retries could never have recovered any of it.** All three attempts
reused the same container, and a container that has rejected its bytes once is
permanently unusable — later attempts return "The ig container is not in the
status to upload a video" or a bare "Request processing failed", which is
exactly the identical-failure pattern the logs showed. Container creation,
upload and processing are now retried as a unit against a **fresh container**
(`_stage_container`), with `TokenExpired` still failing fast since retrying
cannot fix a rejected token.

**A Gemini 429 crashed the whole run and cost the slot.** The free tier allows
only 5 `generate_content` requests per minute, and the generators legitimately
make several calls in a row (a duplicate premise or malformed JSON each costs a
retry, and one `cloud-tick` run can catch up on more than one overdue slot), so
hitting the per-minute cap is routine rather than exceptional. `llm.py` now
waits the window out and retries, while still failing fast on the per-**day**
cap, which waiting cannot clear.

**`min_gap_minutes` was not actually being honoured between real posts.** It is
set to 30, but posts were landing 12-22 minutes apart (11:52/12:05 on 09-26,
19:42/20:01 on 09-27). The randomized slot *times* respect the gap; nothing
enforced it at publish time. GitHub fires this workflow far less often than the
20-minute cron asks — observed ~5 times a day with 4-6 hour gaps, made worse
by runs taking 20-55 minutes and serializing on the concurrency group — so
several slots are already overdue whenever a run does happen, and the catch-up
loop flushed them back-to-back as fast as videos could be built. Both publish
loops now defer a due slot when the last successful post was inside
`min_gap_minutes`, and stop processing further slots that run. Deferring is
safe rather than lossy (slots stay on offer for 24h), and more than one post
per run is still possible when a run is long enough that the gap genuinely
elapsed. **`_minutes_since_last_publish` works entirely in UTC on purpose** —
`videos.updated_at` is SQLite `CURRENT_TIMESTAMP` (always UTC) while
`_now_in_schedule_tz` is wall-clock in the pinned zone; mixing the two is a
live 4-5 hour error. It fails open (no enforcement) with no prior publish or an
unparseable timestamp, so it can never block the first post of a fresh DB.

**Failures were invisible, and state was thrown away when they happened.**
Three compounding problems, all fixed 2026-09-28. The workflow's scrub and
commit steps had no `if:`, so they defaulted to `if: success()` and a run that
raised discarded every state change it had already made — including, in the
worst case, the `instagram_id` of a post that had already gone up, which would
leave that slot unfired and let a later run post a second video for it. Now
`if: always()`. The real exception text was only logged, never stored, so
`videos.error` said nothing but "Publish failed on the GitHub Actions runner"
for 21 failed videos; `_publish_row` now records the actual reason and
`cloud-tick` preserves it. And a handled publish failure left the run **green**,
which is the single reason a third of publishes failing ran for a week
unnoticed — `cloud-tick` now exits non-zero so the run shows red and GitHub's
default notification fires. Token scrubbing also moved out of inline YAML into
`scripts/scrub_tokens.py`: inline it ran as `python -c`, and where only
`python3` exists the scrub failed while the commit carried on, which on a
public repo is a credential-leak path rather than a cosmetic bug. The script
verifies the table is empty afterwards and exits non-zero if not.

**Gemini's own 5xx now retry too.** Five of the six most recent red runs were
the per-minute 429 already handled; the sixth (2026-09-25) was
`InternalServerError: 500 Internal error encountered`, which the 429 matcher
did not catch, so it still killed the run. Transient 500/503/504 now retry on a
short escalating backoff, separate from the 65s quota wait. A bad API key or the
per-day cap still fail immediately.

**Workflow actions moved off deprecated Node 20 majors** — checkout v4 to v7,
setup-python v5 to v7, cache v4 to v6. Every run had been annotated that
GitHub was force-running them on Node 24.

### Scheduling: why the cron is six fixed times, not a poll (2026-09-28)

**This supersedes the randomized-slot polling described in the Full automation
section above, for the `cloud-tick` path only.** `cmd_auto_publish` and the
`schedule_slots` table still work the old way and are untouched, since the
always-on path still uses them — but both Task Scheduler jobs are disabled, so
nothing exercises that path today.

The old design randomized N post times per day into `schedule_slots` and ran a
`*/20` cron that asked, 72 times a day, whether a slot was due. That polling
existed purely because cron cannot express "sometime random this afternoon".
It backfired. Measured across 95 scheduled runs:

- GitHub delivered about **6 of the 72** daily triggers (~8%).
- The gap between consecutive runs was **never under 121 minutes**. Zero gaps
  below 2h, then a spread from 2-7h.
- Of those ~6 runs, only **~2.6 a day actually built a video** (28 min each);
  the rest checked and exited in ~4 min. Against a target of 3 posts/day that
  is no margin at all.

**That distribution is the important part.** Random load-shedding of a
20-minute schedule would leave plenty of short gaps — some consecutive triggers
would survive. A hard floor at 2h with nothing underneath means GitHub is
enforcing a **minimum interval** on a high-frequency schedule, not dropping at
random. So polling harder bought nothing and plausibly *caused* the throttling.
**Do not "fix" a missed post by tightening the cron — that is what caused this.**

The fix inverts it. The cron now fires **six fixed times a day, ~2.5h apart**
(above the observed floor), chosen so every one lands inside the 9am-11pm
Eastern window in **both EDT and EST** — cron is always UTC, so a naive choice
drifts out of the window for half the year. Six opportunities for three posts
means a dropped or failed trigger costs redundancy rather than the day's post.

`cloud-tick` then decides for itself whether to post:
- inside `window_start_hour`..`window_end_hour`?
- has today already had `posts_per_day`? (`_posts_today`)
- has `min_gap_minutes` elapsed since the last real post?

and waits a random slice of `jitter_max_minutes` (new, default 45) before
publishing, which is what keeps post times from being identical every day
despite fixed triggers. Jitter is measured from the start of the run and the
build counts toward it, so with builds taking ~25 min there is usually little
or no actual waiting.

`_posts_today` converts the local day boundary to UTC rather than comparing
date strings, for the same reason `_minutes_since_last_publish` does:
`videos.updated_at` is SQLite `CURRENT_TIMESTAMP` (UTC) while the window is in
the pinned zone.

### Monitoring

`cli.py monitor` checks token validity (catches a revoked/broken token
immediately) and follower/post-count trend for both platforms, storing
history in `account_metrics`. Deliberately does NOT attempt engagement/reach
metrics yet: Instagram's `/insights` needs `instagram_manage_insights` (not
granted) and Reels insights are hard-gated behind 1,000 followers by the
platform regardless of permissions; TikTok metrics need `video.list`, a scope
separate from what's currently granted. Extend both `check_account_health()`
functions once those are cleared.

## Credentials — what's configured (see `.env`, never commit it)

- `GEMINI_API_KEY` — pinned to model `gemini-3.6-flash` in `config.yaml`.
  **Do not use `gemini-flash-latest` or `gemini-2.5-flash`** — the former
  silently resolves to a model capped at 20 free-tier requests/day, the
  latter 404s as unavailable to this account.
- `ELEVENLABS_API_KEY` — present but unused (quota exhausted, replaced by
  Chatterbox). Harmless to leave.
- `REDDIT_CLIENT_ID/SECRET/USER_AGENT` — present but unused (PRAW path exists
  in code, gated behind `reddit.access: praw`, not the active access mode).
- `INSTAGRAM_ACCESS_TOKEN/USER_ID/APP_ID/APP_SECRET` — configured, live,
  working. Non-expiring token (see above) — no rotation needed unless rejected.
- `TIKTOK_ACCESS_TOKEN/REFRESH_TOKEN/CLIENT_KEY/CLIENT_SECRET` — configured,
  working in inbox mode. Access token auto-refreshes proactively (~1h before
  its 24h expiry), refresh token is valid 365 days.

## Deliberately declined / will not build

A few requests along the way were declined on purpose — don't re-propose
these without re-litigating why:
- Bulk-scraping a specific creator's TikTok account for content-style
  inspiration (ToS violation, reproduces one creator's work at scale).
- Self-hosting Redlib/Libreddit for Reddit access (its actual mechanism is
  OAuth token spoofing — impersonating Reddit's official app credentials).
- Browser automation that logs into TikTok's real app/site to drive the
  upload UI directly, to get automatic captions without either official
  TikTok constraint — this is exactly the GUI automation this project
  exists to avoid, and a real account-ban risk.
- Any workaround for TikTok's unaudited-app restrictions beyond completing
  their actual review process.

## Pending / next steps

1. **TikTok production review is still undecided** — submitted 2026-09-03, so
   24 days as of 2026-09-27, past the 2-weeks-max their docs suggest. Worth
   chasing rather than continuing to wait. `tiktok.post_mode` is still `inbox`
   and `schedule.platform` is still `instagram`, so nothing posts to TikTok at
   all right now.
2. Once approved: flip `tiktok.post_mode` to `direct` **and**
   `schedule.platform` to `both`, then retest one video. Both code paths are
   already written and live-tested.
3. Scheduling is live on GitHub Actions at `posts_per_day: 3`, now driven by
   six fixed cron times plus in-run jitter (see the scheduling section above).
   **Watch whether the delivery rate actually improves** — the reasoning that
   a gentler cron gets honoured more reliably is well-supported by the observed
   2h floor, but it has not yet been confirmed over several days. If posts are
   still being missed, add cron entries (keeping them >2h apart) rather than
   tightening the interval. If 3/day proves unreachable, drop `posts_per_day`
   to 2 so the schedule is honest rather than permanently behind.
4. **Four videos (31-34) sit at `status='assembled'` from 2026-09-07**, from
   before the cloud migration. Unlike a cloud-orphaned row these still have
   real mp4s in `output/ready/` on the WSL box, so they are a usable manual
   backlog rather than debris — but they were built with the **old** encoder
   settings, so re-run `cli.py assemble --video-id N` on each before posting
   (video 33 has already been re-encoded). Nothing will publish them
   automatically: the cloud path always generates fresh and both Task
   Scheduler jobs are disabled.
5. Longer-term, user-deferred: replace the synthesized ambient music with real
   royalty-free tracks ("later on we will do option 2").
6. Content-quality monitoring is still manual/qualitative. Real engagement
   metrics need the permission/follower-threshold work noted above.
7. Manually edit `video_1`'s ("Driveway Mystery") live Instagram caption to
   match the current convention — the API path needs an un-granted permission
   (`POST /{media-id}?caption=...` fails even with `comment_enabled=true`), so
   this has to be done by hand in the app. Suggested text already given to the
   user: "I literally have chills after reading that name on the band...
   \n\n#storytime #texts #drama #fyp".
8. `output.ready_dir` is still uncapped and grows unboundedly on the WSL box.
   Not yet addressed, and now mostly historical — the cloud runner builds into
   its own ephemeral disk, so only locally-built videos land there.
9. Deliberately not pursued: fake engagement (bot accounts liking/viewing
   posts) — user asked directly, was told no (real ban risk, against platform
   ToS), and agreed not to pursue it.

## Where things live

- Code: this repo (`src/`), tests in `tests/`.
- Reference voice clips: `assets/voices/chatterbox_ref_{female,male}.wav` —
  don't regenerate these from Piper output, see TTS section above.
- Synced output for mobile posting: `cfg.output.mobile_sync_dir` (a OneDrive
  path under the user's Windows filesystem). **Dead since the Actions
  migration** — a cloud runner cannot write to the user's OneDrive, so this
  only ever fills when the pipeline runs locally. It existed purely to hand
  over videos for manual TikTok posting, which is moot while TikTok is off.
  Emptied 2026-09-27 (34 stale files, ~823MB); the WSL-local `output.ready_dir`
  originals were untouched.
- TikTok legal pages (for the review submission): a GitHub Pages site
  (`terms.html`/`privacy.html` in a public repo, `main` branch, Pages
  enabled) — GitHub Gist was tried first and does NOT work for TikTok's
  URL-ownership verification (unpredictable path structure).

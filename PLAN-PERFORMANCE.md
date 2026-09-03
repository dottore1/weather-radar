# Plan: optimize dev harness load times

No code changed for this — measurements + a prioritized plan only.

## Measured baseline (real data, this machine)

| Stage | Cost |
|---|---|
| HDF5 download (one frame, ~295KB) | ~0.17s |
| `decode_h5` | ~0.01s |
| `reproject_to_dbz_grid` at display size (1000×700 = 700K px) | ~0.11s |
| `reproject_to_dbz_grid` at padded WORK size (1556×1346 = 2.09M px) | ~0.34s |
| `colorize` (display size) | ~0.04s |
| `png_from_grid` (colorize + PNG encode, display size) | ~0.07s (354KB output) |
| `png_from_grid` (WORK size) | ~0.14s (459KB output) |
| **`/api/frames`, cold cache** | **~1.9s** |
| **`/api/frames`, warm forecast-PNG cache** | **~1.35s** |
| 13 observed frame images, fetched sequentially | ~6.2s total (~0.48s/frame) |

The headline finding: **`/api/frames` costs ~1.35s even when nothing has
changed since the last call**, and the client polls it every 60s. That's not
a cold-start cost being amortized — it's paid on every single poll.

## Root cause of the biggest cost

`build_forecast()` (`dev/server.py`) unconditionally re-downloads and
re-reprojects the two motion-baseline frames (`items[-3]`, `items[-1]`) at
the padded WORK resolution on *every* `/api/frames` call — 2 downloads +
2×0.34s reprojections + FFT motion estimate — regardless of whether the
underlying DMI data has actually advanced since the last call. Since new
frames only appear every ~10 min but the page polls every 60s, roughly
5 out of 6 polls redo this work for an identical result. This alone accounts
for most of the ~1.35s warm-cache floor.

## Plan, ranked by impact/effort

- [ ] **1. Skip forecast recomputation when the input hasn't changed
      (highest impact, cheap fix).** Cache the motion/forecast result keyed
      on `(baseline_item.id, curr_item.id)`; short-circuit `build_forecast`
      entirely when that pair matches the last computed one (the forecast
      PNGs are already cached to disk — it's specifically the *download +
      reprojection + FFT* that's being redone needlessly). Should collapse
      the ~1.35s warm-cache floor to close to zero.

- [ ] **2. Stop double-fetching/double-decoding the same HDF5 files.**
      `baseline_item` and `curr_item` (used for motion) are *also* among the
      13 observed items independently downloaded+decoded via `get_or_render`
      for their own image endpoints — right now that's two separate
      downloads and two separate `decode_h5` calls per file per request
      cycle (once for the display-size render, once for the WORK-size
      forecast grid). Decode once per file, reproject to whichever
      output box(es) are actually needed from that single decode. Smaller
      win than #1 alone, but compounds with it.

- [ ] **3. Make forecast computation non-blocking for the frame list.**
      Right now `/api/frames` can't return until all 9 forecast PNGs are
      fully rendered — the observed frame list (cheap, fast) is stuck behind
      that. Decouple: return the observed list immediately, compute/refresh
      the forecast in a background thread whenever a new `curr_item`
      appears (a natural fit given #1's change-detection), and have forecast
      image requests serve whatever's cached (possibly briefly stale by a
      few seconds on a fresh frame) rather than blocking on it synchronously.

- [ ] **4. Add HTTP caching headers to `/api/frame/<id>.png`.** Observed
      frames are immutable once rendered (a historical timestamp's content
      never changes) — add `Cache-Control: public, max-age=<long>` (and/or
      `ETag`) so the browser skips re-fetching them entirely on repeat visits
      or across the periodic 60s refresh cycle. Forecast frames should stay
      short/no-cache since they're recomputed periodically under a
      timestamp that shifts forward each cycle anyway (new `fid` per cycle,
      so this mostly matters for observed frames).

- [ ] **5. Stop destroying/recreating all 22 `<img>` elements on every
      client-side refresh.** `dev/index.html`'s `loadFrames()` removes and
      rebuilds every frame `<img>` from scratch every 60s, forcing the
      browser to redo work (network round-trip even with #4's cache headers
      helping, plus decode/paint) for frames that haven't actually changed.
      Diff the frame list instead: only add newly-appeared frames, remove
      ones that fell out of the window, leave the rest untouched.

- [ ] **6. Prioritize the initially-visible frame.** On first load, all 22
      `<img src>` are set at once; the browser fetches them roughly
      concurrently (capped by its per-host connection limit), but only one
      frame (the latest observed) is actually visible until the user scrubs
      or presses play. Set just that one `src` immediately; lazily set the
      rest (e.g. via `requestIdleCallback` or a short stagger) so first
      paint isn't competing with 21 other requests for connection slots.

- [ ] **7. Reduce transferred bytes per frame.** Each PNG is ~350-460KB,
      mostly-transparent RGBA. Options: PIL's PNG `optimize=True` (CPU cost
      for smaller output — probably not worth it live, maybe worth it for
      cached/reused observed frames), or serving WebP instead of PNG
      (meaningfully smaller for this kind of content, universally supported
      by current browsers). Worth revisiting after 1-3 land, since those
      remove far more latency than byte-shaving does.

- [ ] **8. (Architecture note, not a dev-harness fix.)** Once this becomes a
      real HA integration (per `PLAN-DMI-MIGRATION.md`), "load time" should
      mostly stop being a per-request problem at all: the integration would
      pre-render frames on its own schedule as new DMI data arrives,
      independent of whether any dashboard is open, and the card would just
      display already-ready bytes. Most of 1-3 above are specifically about
      making the dev harness's on-demand-render model tolerable for local
      iteration — worth knowing that ceiling exists so effort isn't
      over-invested polishing a request-time architecture that the
      production version won't actually use.

## Suggested order

1, 2, 3 first (biggest, server-side, compounding) → 4, 5, 6 (client-perceived
latency) → 7 last (marginal once the above land). 8 is just context for
scoping effort, not an action item here.

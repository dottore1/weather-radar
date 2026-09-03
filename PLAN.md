> **Superseded** — this plan assumed the radar pipeline could stay purely
> client-side (hotlinking TV2's CDN in the browser). Once that moved to
> DMI's raw HDF5 Open Data, a real server-side pipeline became necessary
> and a card alone could no longer do this. See `PLAN-HA-COMPONENT.md` for
> what was actually built. Kept here for history.

# Plan: Convert to a HACS-installable custom Lovelace card

Goal: replace the standalone `radar.html` (loaded via an HA `iframe` card) with a
real custom Lovelace card (`weather-radar-card.js`) that HACS can install and
that lives inside HA's own document — enabling live theme inheritance, which an
iframe architecturally cannot do.

## Steps

- [x] **1. Rewrite the page as a custom element** — `weather-radar-card.js`
  - Port the existing logic (frame-fetching, anchor/forecast
    discovery, timeline, autoplay, live clock) from `radar.html`'s `<script>`
    into a class extending `HTMLElement`.
  - Implement the Lovelace card contract: `setConfig(config)`, `set hass(hass)`,
    `getCardSize()`.
  - Render into a shadow DOM; move the existing CSS in.
  - `customElements.define('weather-radar-card', WeatherRadarCard)`.
  - Register in `window.customCards` (name/description/preview) for the card
    picker UI.
  - **Theme inheritance (explicit requirement):** bind background/text/track
    colors to HA's live CSS custom properties (`--card-background-color`,
    `--primary-text-color`, `--secondary-text-color`, `--divider-color`, etc.)
    instead of the current hardcoded dark palette / `prefers-color-scheme`
    guess. Should update live when the user toggles HA's theme, no reload
    needed. The map's overlay boxes (stamp/legend) stay fixed-light/fixed-dark-text
    on purpose, since they sit on the always-light map image, not HA's UI chrome.

- [x] **2. Add `hacs.json` at repo root**
  ```json
  { "name": "Weather Radar Card", "filename": "weather-radar-card.js", "render_readme": true }
  ```

- [x] **3. Repo layout**
  - Built JS file (`weather-radar-card.js`) at root or in `dist/`, matching
    `filename` in `hacs.json`. No bundler needed (no external deps) — stays a
    single hand-written file.

- [x] **4. Add `README.md`**
  - Shown in the HACS UI via `render_readme`; expected for a custom repository.

- [ ] **5. Cut a GitHub release/tag** (e.g. `v1.0.0`)
  - HACS versions installs off releases; without one it tracks the default
    branch, which works but gives worse update signaling.

- [ ] **6. Install via HACS as a custom repository**
  - HACS → "⋮" menu → Custom repositories → add
    `https://github.com/dottore1/weather-radar`, category **Lovelace**.
  - HACS installs it and auto-registers the JS as a Lovelace resource.
  - Add a card with `type: custom:weather-radar-card` to a dashboard.

- [ ] **7. Ongoing updates**
  - New commits + a new tag → HACS offers the update normally.

## Notes / open questions
- Not targeting HACS's default store (requires a submission/review process) —
  installing as a custom repository is the realistic path for personal use.
- `radar.html` goes away as a deployable artifact once this lands; its logic
  moves into the new JS file. No more hand-copying into `www/`, no more
  `aspect_ratio` guessing on an `iframe` card.

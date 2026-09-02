# Weather Radar Card

A Home Assistant Lovelace card showing live Danish precipitation radar
(DMI, via TV 2's radar CDN): the last ~2 hours of observed rainfall and up to
~1 hour of forecast, on TV 2's Denmark basemap, with a scrub/play timeline.

Follows Home Assistant's own light/dark theme automatically, since it runs as
a real card (not an iframe) and reads HA's theme CSS variables directly.

## Installation

### HACS (custom repository)

This isn't in HACS's default store, so add it as a custom repository:

1. HACS → the "⋮" menu → **Custom repositories**.
2. Repository: `https://github.com/dottore1/weather-radar`, category **Lovelace**.
3. Install **Weather Radar Card**. HACS registers the Lovelace resource for you.

### Manual

1. Copy `weather-radar-card.js` into `<config>/www/`.
2. Add it as a Lovelace resource: Settings → Dashboards → Resources →
   `/local/weather-radar-card.js`, type **JavaScript module**.

## Usage

Add a card with:

```yaml
type: custom:weather-radar-card
```

No configuration options yet — the card fetches directly from TV 2/DMI's
public radar CDN in the viewer's browser, so the *viewing device* needs
internet access (not necessarily your HA server).

## Notes

- Radar data typically lags real time by 15–25 minutes; that's DMI's own
  processing delay, not a bug in the card. The card checks for new frames
  every minute.
- See `PLAN.md` for the conversion plan/progress from the original
  iframe-based `radar.html`.

// Weather Radar Card — live DMI/TV2 precipitation radar for Denmark.
// Custom Lovelace card. See PLAN.md for the conversion plan this implements.

const HIST_FRAMES = 13;      // last ~2h of observed frames, in 10-min steps (incl. the live one)
const MAX_FCST_FRAMES = 12;  // safety cap (2h); actual availability is discovered dynamically
const STEP_MS = 10 * 60 * 1000;
const MAX_ANCHOR_RETRIES = 5; // allow for publish lag when finding the latest observed frame
const REFRESH_MS = 60 * 1000;
const CLOCK_MS = 1000;

const pad = n => String(n).padStart(2, '0');

function floorTo10Utc(d) {
  const t = new Date(d.getTime());
  t.setUTCSeconds(0, 0);
  t.setUTCMinutes(Math.floor(t.getUTCMinutes() / 10) * 10);
  return t;
}
function fileStamp(d) {
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}-${pad(d.getUTCHours())}-${pad(d.getUTCMinutes())}`;
}
function obsUrlFor(d) {
  return `https://radar-cdn.weather.tv2api.dk/DmiRadarImages/${fileStamp(d)}-radar-image.png`;
}
function fcstUrlFor(d) {
  return `https://radar-cdn.weather.tv2api.dk/RadarPrediction/${fileStamp(d)}-radar-image.png`;
}
function fmtLocal(d) {
  return d.toLocaleTimeString('da-DK', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Copenhagen' });
}
function preload(url, bustCache) {
  // bustCache is for "does this frame exist yet" probes at the live/forecast frontier:
  // a plain-URL probe against a not-yet-published frame can get its 404 cached by the
  // browser, so re-probing the *same* URL once the frame is actually published can keep
  // returning the stale cached failure forever. A unique query string sidesteps that.
  const src = bustCache ? url + (url.includes('?') ? '&' : '?') + 'cb=' + Date.now() : url;
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('load failed: ' + url));
    img.src = src;
  });
}

const TEMPLATE = `
<style>
  :host{ --overlay-ink:#16212b; --overlay-muted:#5b6b78; }
  ha-card{ overflow:hidden; }
  .wrap{ padding:12px 16px 16px; display:flex; flex-direction:column; }
  header{ flex:none; display:flex; justify-content:flex-end; margin-bottom:12px; }
  .updated{ font-size:13px; color:var(--secondary-text-color); text-align:right; }
  .updated #clock{ font-size:15px; font-weight:600; color:var(--primary-text-color); font-variant-numeric:tabular-nums; }
  .stageOuter{ flex:none; display:flex; align-items:center; justify-content:center; }
  .stage{ position:relative; border-radius:8px; overflow:hidden; background:var(--secondary-background-color,#eceee9); border:1px solid var(--divider-color); aspect-ratio:1980/1580; width:100%; height:auto; }
  .stage img{ position:absolute; inset:0; width:100%; height:100%; object-fit:fill; display:block; }
  .stage .basemap{ position:static; pointer-events:none; }
  .stage .frame{ opacity:0; transition:opacity .12s linear; pointer-events:none; }
  .stage .frame.active{ opacity:.85; }
  .stamp{ position:absolute; left:12px; top:12px; background:rgba(255,255,255,.92); color:var(--overlay-ink); border-radius:6px; padding:8px 12px; font-size:14px; line-height:1.25; box-shadow:0 1px 3px rgba(20,40,60,.18); font-family: var(--paper-font-body1_-_font-family, inherit); }
  .stamp strong{ display:block; font-size:20px; font-variant-numeric:tabular-nums; }
  .stamp.now strong,.stamp.fc strong{ color:var(--primary-color,#0a5fb4); }
  .legend{ position:absolute; right:12px; bottom:12px; background:rgba(255,255,255,.92); border-radius:6px; padding:8px 10px; font-size:11px; color:var(--overlay-muted); box-shadow:0 1px 3px rgba(20,40,60,.18); }
  .legend .bar{ display:block; height:10px; border-radius:3px; overflow:hidden; margin-bottom:4px; width:150px; background:linear-gradient(90deg,#bfe6fb,#6fb8ec,#3a6fd8,#5a3ab0,#a726c0,#e91e8c); }
  .legend .lab{ display:flex; justify-content:space-between; }
  .controls{ flex:none; display:flex; align-items:center; gap:14px; margin-top:14px; }
  button.play{ width:44px; height:44px; border-radius:50%; border:0; background:var(--primary-color,#0a5fb4); color:#fff; cursor:pointer; display:grid; place-items:center; flex:none; }
  button.play:focus-visible{ outline:3px solid var(--primary-color,#0a5fb4); outline-offset:2px; }
  .timeline{ flex:1; position:relative; padding-top:10px; }
  .track{ position:relative; height:6px; border-radius:3px; background:var(--divider-color); }
  .track .obs{ position:absolute; left:0; top:0; bottom:0; background:var(--secondary-text-color); border-radius:3px 0 0 3px; }
  .now-mark{ position:absolute; top:-8px; width:2px; height:22px; background:var(--primary-text-color); transform:translateX(-1px); }
  .now-mark::after{ content:"Nu"; position:absolute; top:-16px; left:50%; transform:translateX(-50%); font-size:11px; font-weight:600; white-space:nowrap; color:var(--primary-text-color); }
  input[type=range]{ position:absolute; left:0; top:10px; width:100%; margin:0; height:6px; background:transparent; -webkit-appearance:none; appearance:none; cursor:pointer; }
  input[type=range]::-webkit-slider-thumb{ -webkit-appearance:none; width:18px; height:18px; border-radius:50%; background:#fff; border:3px solid var(--primary-color,#0a5fb4); box-shadow:0 1px 3px rgba(0,0,0,.3); }
  input[type=range]::-moz-range-thumb{ width:12px; height:12px; border-radius:50%; background:#fff; border:3px solid var(--primary-color,#0a5fb4); }
  .ticks{ display:flex; justify-content:space-between; font-size:11px; color:var(--secondary-text-color); margin-top:8px; font-variant-numeric:tabular-nums; }
  @media (prefers-reduced-motion:reduce){ .autoplay{ display:none; } }
</style>
<ha-card>
  <div class="wrap">
    <header>
      <div class="updated">
        <div id="clock">–</div>
        <div id="updated">Indlæser…</div>
      </div>
    </header>
    <div class="stageOuter">
      <div class="stage" id="stage">
        <img class="basemap" src="https://gfx.tv2a.dk/weather/radar_map_medium.png" alt="Kort over Danmark">
        <div class="stamp" id="stamp"><span id="stampLabel">Observeret</span><strong id="stampTime">–</strong></div>
        <div class="legend">
          <span class="bar"></span>
          <div class="lab"><span>Let</span><span>Moderat</span><span>Kraftig</span></div>
        </div>
      </div>
    </div>
    <div class="controls">
      <button class="play" id="play" aria-label="Afspil"><svg width="18" height="18" viewBox="0 0 18 18" id="playIcon"><path d="M4 2l12 7-12 7z" fill="#fff"/></svg></button>
      <div class="timeline">
        <div class="track"><div class="obs" id="obsBar"></div><div class="now-mark" id="nowMark"></div></div>
        <input type="range" id="slider" min="0" max="0" value="0" step="1" aria-label="Tidspunkt">
        <div class="ticks" id="ticks"></div>
      </div>
    </div>
  </div>
</ha-card>
`;

class WeatherRadarCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = config || {};
    if (!this.shadowRoot) {
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.innerHTML = TEMPLATE;
      this._cacheRefs();
      this._wireControls();
    }
  }

  set hass(hass) {
    this._hass = hass;
  }

  getCardSize() {
    return 6;
  }

  connectedCallback() {
    this._frames = [];
    this._nowIdx = HIST_FRAMES - 1;
    this._sliderValue = this._nowIdx;
    this._playing = false;
    this._playTimer = null;
    this._lastKey = null;
    this._start();
  }

  disconnectedCallback() {
    this._stop();
  }

  _cacheRefs() {
    const $ = sel => this.shadowRoot.getElementById(sel);
    this._stage = $('stage');
    this._slider = $('slider');
    this._stamp = $('stamp');
    this._stampTime = $('stampTime');
    this._stampLabel = $('stampLabel');
    this._ticksEl = $('ticks');
    this._updatedEl = $('updated');
    this._playBtn = $('play');
    this._icon = $('playIcon');
    this._obsBar = $('obsBar');
    this._nowMark = $('nowMark');
    this._clockEl = $('clock');
  }

  _wireControls() {
    this._playBtn.onclick = () => this._setPlaying(!this._playing);
    this._slider.oninput = () => { this._setPlaying(false); this._show(+this._slider.value); };
  }

  _start() {
    this._tickClock();
    this._clockTimer = setInterval(() => this._tickClock(), CLOCK_MS);
    this._loadFrames();
    this._refreshTimer = setInterval(() => this._loadFrames(), REFRESH_MS);
    if (!matchMedia('(prefers-reduced-motion: reduce)').matches) this._setPlaying(true);
  }

  _stop() {
    clearInterval(this._clockTimer);
    clearInterval(this._refreshTimer);
    clearInterval(this._playTimer);
  }

  _tickClock() {
    this._clockEl.textContent = 'Nu kl. ' + fmtLocal(new Date());
  }

  async _findAnchor() {
    let t = floorTo10Utc(new Date());
    for (let i = 0; i < MAX_ANCHOR_RETRIES; i++) {
      try { await preload(obsUrlFor(t), true); return t; }
      catch (e) { t = new Date(t.getTime() - STEP_MS); }
    }
    return t;
  }

  async _findForecast(anchor) {
    const times = [];
    for (let i = 1; i <= MAX_FCST_FRAMES; i++) {
      const t = new Date(anchor.getTime() + i * STEP_MS);
      try { await preload(fcstUrlFor(t), true); times.push(t); }
      catch (e) { break; }
    }
    return times;
  }

  async _loadFrames() {
    const anchor = await this._findAnchor();
    const fcstTimes = await this._findForecast(anchor);

    const key = anchor.getTime() + '|' + fcstTimes.length + (fcstTimes.length ? '|' + fcstTimes[fcstTimes.length - 1].getTime() : '');
    if (key === this._lastKey) return; // nothing new published since the last check
    this._lastKey = key;

    const histTimes = [];
    for (let i = HIST_FRAMES - 1; i >= 0; i--) histTimes.push(new Date(anchor.getTime() - i * STEP_MS));

    const histResults = await Promise.all(histTimes.map(async (t) => {
      try { await preload(obsUrlFor(t)); return { time: t, ok: true, forecast: false }; }
      catch (e) { return { time: t, ok: false, forecast: false }; }
    }));
    const fcstResults = fcstTimes.map(t => ({ time: t, ok: true, forecast: true }));

    const combined = histResults.concat(fcstResults);
    this._nowIdx = HIST_FRAMES - 1;

    this._stage.querySelectorAll('.frame').forEach(n => n.remove());
    this._frames = combined.map((r) => {
      const el = document.createElement('img');
      el.className = 'frame';
      el.alt = '';
      if (r.ok) el.src = r.forecast ? fcstUrlFor(r.time) : obsUrlFor(r.time);
      this._stage.appendChild(el);
      return { ...r, el };
    });

    this._slider.max = this._frames.length - 1;
    const lagMin = Math.round((Date.now() - anchor.getTime()) / 60000);
    this._updatedEl.textContent = `Nyeste radarbillede: ${fmtLocal(anchor)} (${lagMin} min. forsinket)` +
      (fcstTimes.length ? ' · prognose til ' + fmtLocal(fcstTimes[fcstTimes.length - 1]) : '');

    const lastIdx = this._frames.length - 1;
    const midIdx = Math.min(lastIdx, Math.max(0, Math.round((0 + lastIdx) / 2)));
    this._ticksEl.innerHTML = [0, midIdx, lastIdx]
      .map(i => `<span${i === this._nowIdx ? ' style="color:var(--primary-color,#0a5fb4)"' : ''}>${fmtLocal(this._frames[i].time)}</span>`).join('');

    const nowPct = lastIdx > 0 ? (this._nowIdx / lastIdx * 100) : 100;
    this._obsBar.style.width = nowPct + '%';
    this._nowMark.style.left = nowPct + '%';
    this._nowMark.style.transform = 'translateX(-1px)';

    const preserve = Math.min(this._sliderValue, lastIdx);
    this._show(this._sliderValue >= this._nowIdx - 1 ? this._nowIdx : preserve);
  }

  _show(i) {
    this._sliderValue = i;
    this._frames.forEach((f, idx) => f.el.classList.toggle('active', idx === i));
    const f = this._frames[i];
    if (!f) return;
    this._stampTime.textContent = 'kl. ' + fmtLocal(f.time);
    const isNow = i === this._nowIdx;
    const isFcst = i > this._nowIdx;
    this._stampLabel.textContent = isNow ? 'Seneste' : (isFcst ? 'Prognose' : 'Observeret');
    this._stamp.classList.toggle('now', isNow);
    this._stamp.classList.toggle('fc', isFcst);
    this._slider.value = i;
  }

  _setPlaying(p) {
    this._playing = p;
    this._playBtn.setAttribute('aria-label', p ? 'Pause' : 'Afspil');
    this._icon.innerHTML = p
      ? '<rect x="3" y="2" width="4" height="14" fill="#fff"/><rect x="11" y="2" width="4" height="14" fill="#fff"/>'
      : '<path d="M4 2l12 7-12 7z" fill="#fff"/>';
    clearInterval(this._playTimer);
    if (p) this._playTimer = setInterval(() => this._show((this._sliderValue + 1) % this._frames.length), 450);
  }
}

customElements.define('weather-radar-card', WeatherRadarCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'weather-radar-card',
  name: 'Weather Radar Card',
  description: 'Live DMI/TV2 precipitation radar for Denmark, with observed history and short-term forecast.',
  preview: false,
});

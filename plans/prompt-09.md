# Prompt 9: Web UI and PWA

## Steps

1. `spylead/sources.py`: name the codes of `SourceError` (TIMEOUT, CONNECTION_FAILED, BAD_PAYLOAD, MISSING_KEY) as constants, listed in `ERROR_CODES`, so a test can check that the UI localizes every code that reaches `last_fetch_error`. An HTTP error's code stays `HTTP_<status>`.
2. `scripts/make_icons.py`, standard library only (zlib, struct): a background in the theme colour and two rising wavy lines, a light one ahead of a white one, an indicator leading SPY. It writes, deterministically, the committed PNGs:
   - `icons/icon-192.png`: rounded corners, purpose `any`;
   - `icons/icon-512-maskable.png`: full bleed, the drawing inside the safe zone of a maskable icon (the central circle of 80 %);
   - `icons/apple-touch-icon.png`: 180 px, opaque and full bleed, since iOS rounds the corners itself.
3. `spylead/static/manifest.webmanifest`: name and short name "spylead", `id`, `start_url` and `scope` "/", `display` "standalone", theme and background colours, the 192 px icon and the maskable 512 px icon.
4. `spylead/static/locales/en.json` and `zh-TW.json`: flat key to string, with `{name}` placeholders. Backend codes are localized by family: `status.*` and `reason.*` (§7.6), `f_status.*`, `outcome.*`, `kind.*`, `phase.*`, `event.*`, `level.*`, `error.*` (`web.ERROR_CODES`) and `source_error.*` (`sources.ERROR_CODES`).
5. `spylead/static/index.html`:
   - the page skeleton, without text of its own: `data-i18n`, `data-i18n-aria` and `data-i18n-placeholder` name the locale keys;
   - the PWA meta tags and links: manifest, icon, 180 px apple-touch-icon, `apple-mobile-web-app-capable`, `mobile-web-app-capable`, theme colours for light and dark;
   - the vendored ECharts and `app.js` as deferred scripts with `src`, `style.css` as a stylesheet;
   - a `<template>` for chart panels.
6. `spylead/static/style.css`:
   - colours as custom properties, switched by `prefers-color-scheme`, the charts' line colours among them;
   - 44 px touch targets, safe-area insets, no hover-only behaviour;
   - below 900 px the charts come first; the table scrolls horizontally inside its own container, with the name column sticky;
   - banners in strong colours with a bold lead word.
7. `spylead/static/app.js`, plain JavaScript in one deferred script:
   - locales: `navigator.language` (`zh*` → zh-TW, else en) unless `localStorage` holds a choice; `t(key, values)` and `label(family, code)`; `Intl.NumberFormat` and `Intl.DateTimeFormat` for the current language;
   - `api(method, path, body)`: JSON in and out, a 10 s timeout, errors as `{status, code, message}`; a 401 shows the login form;
   - polling of `GET /api/state` (§12): every 2 s while a job runs, otherwise every 30 s; at once after an action, after login and when the page becomes visible again; answers that arrive out of order are dropped;
   - rendering from the state document: the status bar, the banners, N, the table, the F summary, the series panels and the events;
   - actions: Fetch Data, N with − and + and Apply (after a confirmation), the "in F" checkboxes, adding a FRED ID, removing a USER series (after a confirmation), language, logout;
   - charts with the vendored ECharts (§12): SPY with its log-axis toggle; F with the line at 1 and the out-of-sample split; SPYL1 against the composite; one collapsible panel per series with a chart, loaded when opened. All reload when `computed_at` changes.
8. Tests:
   - `tests/test_static.py` (AC-19):
     - both locales define the same keys, with the same placeholders in each string;
     - each family covers exactly the backend's codes: `analysis.STATUSES` (twice: label and reason), `analysis.F_STATUSES`, `jobs.OUTCOMES`, `jobs.KINDS`, `jobs.PHASES`, `store.EVENT_KINDS`, `store.LEVELS`, `web.ERROR_CODES` and `sources.ERROR_CODES`;
     - every key the page names (`t("…")`, `data-i18n*`, a key-shaped literal) is defined, every defined key outside the families is named, and `app.js` localizes exactly these families through `label()`;
     - the static files outside `vendor/` contain no `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `new Function`, script element without `src`, `<style>` element, `style` attribute or CSSOM style, inline event handler, or `javascript:` URL;
     - no static file outside `vendor/` names another host;
     - the manifest is valid JSON with the fields of §12; its icons, and the apple-touch-icon that index.html links, exist as PNG of their stated sizes; the meta tags are present;
     - the icons are byte for byte what `scripts/make_icons.py` makes;
     - every `/api/` path in `app.js` is a route of `web.create_app`;
     - the ECharts file matches its SHA-256.
   - `tests/test_web.py`: each kind of static file is served with its content type.
9. `.venv/Scripts/python -m pytest -q`.

### Decisions beyond the letter of SPEC.md

- **The stale banner** (see "For the owner") is judged when a poll fails: after a network error, an HTTP error other than 401, or no answer within 10 s, it shows once the last successful poll is more than 10 s old. It names the time of the last successful poll.
- **Messages.** Codes are localized; the English texts that come with them (`last_job.message`, event messages, the text of `last_fetch_error`, and the message of an error code the locales do not know) are shown beside them as detail.
- **The status bar** shows `last_job`, whatever its kind: the jobs keep it in memory, so after a restart it is null, and the bar shows the time of the last compute instead. After UP_TO_DATE it says, in plain words, that no series has new data and nothing was recomputed.
- **Series charts.** Each candidate with a chart has a collapsible panel, collapsed until opened: the LEADL1 series first, then the others. A row opens that series' panel and scrolls to it. PENDING, FETCH_FAILED and INVALID_VALUES series have no curve (§7.6), so their rows open nothing.
- **Main charts** (SPY, F, SPYL1 against the composite) are panels of the same kind, open by default. F and the composite share one request of `/api/charts/f`.
- **The log-axis toggle** is on the SPY chart only, as §12 lists it. It is a checkbox, so its state shows without colour.
- **Zoom.** `dataZoom` inside and a slider on every chart (§12):
  - pinch zooms and a one-finger drag pans; a vertical swipe over a chart still scrolls the page (`preventDefaultMouseMove: false`), since on a phone the charts fill most of the screen;
  - on the PC, Ctrl+wheel (and a touchpad pinch) zooms; the plain wheel scrolls the page;
  - 1Y, 5Y and 10Y count back from the chart's last day; All shows everything.
- **ECharts.** The canvas renderer, `renderMode: 'richText'` tooltips confined to the chart, `sampling: 'lttb'`, no animation. `useUTC: true`, because a day is a UTC midnight: dates then show as dates in any time zone. The built-in `dark` theme in dark mode, with a transparent background, and the line colours from `style.css`. ECharts' `ZH` locale for zh-TW, `EN` otherwise, for the axis and slider texts. A change of colour scheme or language redraws the charts.
- **Numbers.** Tooltips to 6 significant digits, as the payloads; y-axis labels in compact notation; A* as signed whole days; β and the scores to 3 significant digits.
- **Times.** The last job's time and the events' times in the browser's local time; days as the ISO dates of the state document.
- **Confirmations** use the browser's `confirm` dialog, with the localized text.
- **Errors of an action** appear in a notice at the bottom of the screen, wherever the page is scrolled, until dismissed or for 10 s.
- **A FRED ID** is checked in the page against the server's pattern after uppercasing, so a typing slip is answered at once; the server still decides.
- **While a job runs**, Fetch Data, Apply, the "in F" checkboxes, Remove and Add are disabled: the server would answer 409 JOB_RUNNING.
- **The title column** shows `title_zh` in zh-TW when the catalog gives one; the units follow as detail. Estimated points read as "52 of 400".
- **The settings** also show `fetch_start` and `analysis_start` once set.
- **Events**, the latest 50, are in a collapsible list at the bottom.
- **The HTML holds no text** except the name "spylead": the page fills in once its locale has loaded. Logout reloads the page, so nothing of the session stays in it.
- **`<template>`** holds the chart panel's markup; `app.js` clones it, so the markup stays in HTML.
- **Icons.** The 192 px icon (purpose `any`) and the 512 px icon (purpose `maskable`), as §12 lists them; Edge's install needs an `any` icon of at least 144 px, which the 192 px one is.

## For the owner

### Spec ambiguity: the stale banner

§12 asks for a banner "when the last successful poll is older than 10 s", and for polls every 30 s when no job runs. Read literally, the banner would show for 20 of every 30 seconds while nothing is wrong. The UI judges the age when a poll fails instead, so the banner means "the page cannot reach spylead", and appears within 10 s of the first failed poll while a job runs, and at the first failed poll otherwise. A wording for §12 that matches: "a banner when a poll fails and the last successful poll is older than 10 s".

### UI checklist (SPEC.md §16.3, step 4)

Run it with the app started from `run.bat`, both keys in `config.toml`, and Tailscale Serve publishing port 8443 (§19).

Devices:

- [ ] **Edge on the PC.** Open `http://127.0.0.1:8090`, then Apps, "Install this site as an app". The app opens in its own window with the spylead icon.
- [ ] **Light and dark.** Switch Windows between light and dark (Settings, Personalization, Colors). The page and the charts follow without a reload.
- [ ] **Both languages.** The language button switches every text: the status bar, banners, table headers, statuses and their reasons, outcomes, phases, event kinds, chart legends, axis months, and error notices. A reload keeps the choice.
- [ ] **iPhone, 375 px, portrait.** Open `https://<pc-name>.<tailnet>.ts.net:8443`, then Share, "Add to Home Screen", and start it from the icon:
  - it opens without Safari's bars, with the apple-touch icon;
  - nothing scrolls sideways except the table, inside its box;
  - the charts come first, every button is easy to tap, and nothing hides under the notch or the home indicator.
- [ ] **iPad, portrait and landscape**, added to the home screen the same way. The layout and the charts follow each rotation.

Flows:

- [ ] **Login.** A wrong token answers "The token does not match" after about a second. The right one shows the page. Log out shows the login form again, and a reload keeps it.
- [ ] **Fetch Data from empty** (a fresh `data/`):
  - the banner says there is no data yet and to press Fetch Data;
  - during the job: Fetch Data is disabled, and the bar shows the kind, the phase (Downloading, Smoothing, Measuring leads, Fitting F, Saving), the counts and the series;
  - at the end: the banner goes, the charts appear, and the table shows statuses, A*, β and both scores.
- [ ] **Already up to date.** Press Fetch Data again: the bar says the data is already up to date and nothing was recomputed; the charts stay as they are.
- [ ] **N.** − and + step N by 0.25, from 0.25 to 24, and Apply is enabled only for a changed value. Apply asks first, saying that everything is recomputed and takes some minutes:
  - Cancel changes nothing;
  - OK runs Recompute, and the charts reload at its end.
- [ ] **In F.** Uncheck a KEPT series: Refit F runs, and F's members, scores and chart change. After a reload the box stays unchecked. Check it again to restore F.
- [ ] **Adding a FRED ID:**
  - `indpro` is accepted (uppercased), and a USER row appears with a Remove button;
  - `IND PRO` is refused at once;
  - `M2SL` answers that the series is already listed;
  - an annual series (for example `GDPCA`) ends "Annual series are not supported";
  - an ID FRED does not know ends "FRED has no such series".
- [ ] **Removing a USER series.** Remove asks first, saying it is deleted with its data; then the row goes and F is refitted.
- [ ] **Charts:**
  - pinch zooms on the iPhone and the iPad, a one-finger drag pans, and a vertical swipe over a chart still scrolls the page;
  - on the PC, Ctrl+wheel zooms and the plain wheel scrolls the page;
  - the slider, and 1Y, 5Y, 10Y and All;
  - the log axis on SPY;
  - a tap shows the tooltip with the date and the values;
  - a table row opens and scrolls to that series' chart.
- [ ] **Stale.** With the page open, stop the app with Ctrl+C. Within 30 s a red banner says spylead has not answered since a given time. Start the app again: the banner goes at the next poll, at once when the page comes back to the foreground.
- [ ] **Missing key.** Empty `fred.api_key`, then restart: a banner names the missing FRED key, and Fetch Data marks the FRED series failed with "API key missing". The same for `tiingo.api_key`, whose fetch ends "No SPY data" on a fresh `data/`. Put the keys back.
- [ ] **Nothing else loads.** In Edge's DevTools (F12): the console shows no "Refused to …" line through all of the above, and the Network tab shows requests to the app's own address only.

## Extension points

- **A new code** in the backend (a status, outcome, kind, phase, event kind or error code) is one string per locale; the locale test names any code left out.
- **A new chart** is one panel made from the template, with a load and a build function.
- **A third language** is one more locale file and one more entry in `LANGUAGES`.
- **A native client** (§18) uses the same API; the static UI holds no logic the API lacks.

## Refactor seams

- **Presentation** stays in `app.js`: the API sends codes, days and numbers, and the page turns them into words, dates and digits.
- **Rendering** is a function of the last state document, so a poll, a language switch and a theme switch all redraw from it.
- **Charts** go through `makePanel`, `draw` and one option function per chart.

## Deferred items (appended to TODO.md)

- Browser tests of `app.js` (for example Playwright), which need a dev dependency. Today pytest checks the static files, and the UI checklist the behaviour.

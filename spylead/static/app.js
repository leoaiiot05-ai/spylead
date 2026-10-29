"use strict";
// The spylead page (SPEC.md §12). It polls the state document of §11 and draws the chart payloads.
// Server data reaches the page through textContent and DOM APIs only, never as HTML (§12, §13).

const LANGUAGES = ["en", "zh-TW"];
const LANGUAGE_STORAGE = "spylead-language";
const DAY_MS = 86400000;
const YEAR_MS = 365.25 * DAY_MS;
const POLL_RUNNING_MS = 2000;
const POLL_IDLE_MS = 30000;
const STALE_MS = 10000;
const TIMEOUT_MS = 10000;
const TOAST_MS = 10000;
const N_MIN = 0.25;
const N_MAX = 24;
const N_STEP = 0.25;
const FRED_ID = /^[A-Z0-9_]{1,40}$/;
const HTTP_CODE = /^HTTP_(\d{3})$/;
const CODED_TEXT = /^([A-Z0-9_]+): (.*)$/s;
// Without data, or with values that LOG form cannot take, a candidate has no curve (SPEC.md §7.6).
const NO_CHART = new Set(["PENDING", "FETCH_FAILED", "INVALID_VALUES"]);
const SPARSE_POINTS = 600;
const darkScheme = window.matchMedia("(prefers-color-scheme: dark)");

const view = {
  language: "en",
  strings: {},
  formats: null,
  state: null,
  ticket: 0,
  timer: 0,
  lastSuccess: null,
  stale: false,
  computedAt: undefined,
  storedN: null,
  draftN: null,
  tableKey: "",
  eventsKey: "",
  fRequest: null,
  toastTimer: 0,
  spy: null,
  f: null,
  composite: null,
  seriesPanels: new Map(),
};

function byId(id) {
  return document.getElementById(id);
}

// Languages

function preferredLanguage() {
  const stored = storedLanguage();
  if (LANGUAGES.includes(stored)) return stored;
  return navigator.language.toLowerCase().startsWith("zh") ? "zh-TW" : "en";
}

function storedLanguage() {
  try {
    return localStorage.getItem(LANGUAGE_STORAGE);
  } catch {
    return null; // storage can be off, as in a private window
  }
}

function storeLanguage(language) {
  try {
    localStorage.setItem(LANGUAGE_STORAGE, language);
  } catch {
    // Without storage, the choice lasts until the page reloads.
  }
}

async function loadLanguage(language) {
  const response = await fetch(`/locales/${language}.json`, {signal: AbortSignal.timeout(TIMEOUT_MS)});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  view.strings = await response.json();
  view.language = language;
  view.formats = formats(language);
  document.documentElement.lang = language;
  translate(document);
}

function formats(language) {
  return {
    count: new Intl.NumberFormat(language, {maximumFractionDigits: 0}),
    days: new Intl.NumberFormat(language, {maximumFractionDigits: 0, signDisplay: "exceptZero"}),
    short: new Intl.NumberFormat(language, {maximumSignificantDigits: 3}),
    value: new Intl.NumberFormat(language, {maximumSignificantDigits: 6}),
    axis: new Intl.NumberFormat(language, {notation: "compact", maximumSignificantDigits: 4}),
    n: new Intl.NumberFormat(language, {maximumFractionDigits: 2}),
    time: new Intl.DateTimeFormat(language, {dateStyle: "medium", timeStyle: "short"}),
    clock: new Intl.DateTimeFormat(language, {timeStyle: "medium"}),
  };
}

function t(key, values = {}) {
  const text = view.strings[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (match, name) => (name in values ? String(values[name]) : match));
}

// A backend code in the current language: label("status", "KEPT") reads status.KEPT (SPEC.md §12).
function label(family, code) {
  return t(`${family}.${code}`);
}

function known(family, code) {
  return `${family}.${code}` in view.strings;
}

function translate(root) {
  for (const node of root.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
  for (const node of root.querySelectorAll("[data-i18n-aria]")) node.setAttribute("aria-label", t(node.dataset.i18nAria));
  for (const node of root.querySelectorAll("[data-i18n-placeholder]")) node.placeholder = t(node.dataset.i18nPlaceholder);
}

function number(format, value) {
  return value === null || value === undefined ? "–" : view.formats[format].format(value);
}

function time(text) {
  return view.formats.time.format(new Date(text));
}

// The API (SPEC.md §11)

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status; // 0 when no answer came
    this.code = code;
  }
}

async function api(method, path, body) {
  const init = {method, headers: {}, signal: AbortSignal.timeout(TIMEOUT_MS)};
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, "", "");
  }
  if (response.status === 204) return null;
  let data = null;
  try {
    data = await response.json();
  } catch {
    // Not JSON, or cut off by the timeout: the status decides below.
  }
  if (!response.ok) throw new ApiError(response.status, data?.error ?? `HTTP_${response.status}`, data?.message ?? "");
  if (data === null) throw new ApiError(0, "", "");
  return data;
}

// The code in the current language when the locales know it, else the English message that came with it.
function errorText(error) {
  if (error.status === 0) return t("notice.network");
  if (known("error", error.code)) return label("error", error.code);
  const http = HTTP_CODE.exec(error.code);
  return http ? t("notice.http", {status: http[1], message: error.message}) : error.message;
}

// Every action ends with a poll, so the page shows what the server did, refused, or never heard.
async function act(method, path, body) {
  try {
    await api(method, path, body);
  } catch (error) {
    if (error.status === 401) {
      showLogin();
      return false;
    }
    toast(errorText(error));
    poll();
    return false;
  }
  poll();
  return true;
}

// Polling (SPEC.md §12)

async function poll() {
  clearTimeout(view.timer);
  const ticket = ++view.ticket;
  let state;
  try {
    state = await api("GET", "/api/state");
  } catch (error) {
    if (ticket !== view.ticket) return;
    if (error.status === 401) return showLogin();
    pollFailed();
    schedule();
    return;
  }
  if (ticket !== view.ticket) return; // a newer poll has started
  view.lastSuccess = Date.now();
  view.stale = false;
  render(state);
  schedule();
}

function schedule() {
  clearTimeout(view.timer);
  view.timer = setTimeout(poll, running() ? POLL_RUNNING_MS : POLL_IDLE_MS);
}

function running() {
  return Boolean(view.state?.app.job);
}

// Between two idle polls, 30 s apart, the last success is often older than 10 s while all is well, so the stale
// banner is judged when a poll fails.
function pollFailed() {
  view.stale = view.lastSuccess === null || Date.now() - view.lastSuccess > STALE_MS;
  renderBanners();
}

// Login

function showLogin() {
  clearTimeout(view.timer);
  view.ticket += 1; // drops the answers still on their way
  view.state = null;
  view.stale = false;
  for (const id of ["main", "bar-status", "fetch", "logout"]) byId(id).hidden = true;
  byId("login").hidden = false;
  renderBanners();
  byId("token").focus();
}

function showMain() {
  byId("login").hidden = true;
  for (const id of ["main", "bar-status", "fetch", "logout"]) byId(id).hidden = false;
}

async function logIn(event) {
  event.preventDefault();
  const submit = byId("login-submit");
  const failure = byId("login-error");
  submit.disabled = true;
  try {
    await api("POST", "/api/login", {token: byId("token").value});
  } catch (error) {
    failure.textContent = errorText(error);
    failure.hidden = false;
    return;
  } finally {
    submit.disabled = false;
  }
  byId("token").value = "";
  failure.hidden = true;
  poll();
}

async function logOut() {
  try {
    await api("POST", "/api/logout", {});
  } catch (error) {
    toast(errorText(error));
    return;
  }
  // A reload leaves nothing of the session's data in the page.
  location.reload();
}

// The page, drawn from the latest state document

function render(state) {
  view.state = state;
  showMain();
  const busy = Boolean(state.app.job);
  renderBar(state.app, busy);
  renderBanners();
  renderSettings(state.app, busy);
  renderTable(state.series, busy);
  byId("add").disabled = busy;
  renderCharts(state);
  renderEvents(state.events);
}

function renderBar(app, busy) {
  byId("fetch").disabled = busy;
  renderLastJob(app);
  renderJob(app.job);
}

function renderLastJob(app) {
  const last = app.last_job;
  const line = byId("last-job");
  const detail = byId("last-job-detail");
  line.dataset.outcome = last?.outcome ?? "";
  if (last) {
    const words = {outcome: label("outcome", last.outcome), kind: label("kind", last.kind), time: time(last.finished_at)};
    line.textContent = t("bar.last", words);
    detail.textContent = last.outcome === "UP_TO_DATE" ? t("bar.upToDate") : last.message;
  } else {
    // The jobs remember the last one in memory only: after a restart, the last compute stands in.
    line.textContent = app.computed_at ? t("bar.computed", {time: time(app.computed_at)}) : t("bar.never");
    detail.textContent = "";
  }
  detail.hidden = !detail.textContent;
}

function renderJob(job) {
  byId("job").hidden = !job;
  if (!job) return;
  const parts = [label("kind", job.kind)];
  if (job.phase) {
    const counts = {phase: label("phase", job.phase), done: number("count", job.done), total: number("count", job.total)};
    parts.push(t("bar.phase", counts));
  }
  if (job.series) parts.push(job.series);
  byId("job-text").textContent = parts.join(" · ");
  byId("job-progress").max = Math.max(job.total, 1);
  byId("job-progress").value = job.done;
}

function renderBanners() {
  const app = view.state?.app;
  byId("banner-fred").hidden = !app || app.keys.fred;
  byId("banner-tiingo").hidden = !app || app.keys.tiingo;
  byId("banner-empty").hidden = !app || app.computed_at !== null;
  byId("banner-empty-text").textContent = t(app?.job ? "banner.emptyRunning" : "banner.emptyIdle");
  byId("banner-stale").hidden = !view.stale;
  byId("banner-stale-text").textContent =
    view.lastSuccess === null
      ? t("banner.staleNever")
      : t("banner.staleSince", {time: view.formats.clock.format(view.lastSuccess)});
}

function renderSettings(app, busy) {
  if (app.inflections_per_year !== view.storedN) {
    view.storedN = app.inflections_per_year;
    view.draftN = view.storedN;
  }
  byId("n-value").textContent = number("n", view.draftN);
  byId("n-down").disabled = view.draftN <= N_MIN;
  byId("n-up").disabled = view.draftN >= N_MAX;
  byId("n-apply").disabled = busy || view.draftN === view.storedN;
  const windows = byId("windows");
  windows.hidden = !app.analysis_start;
  windows.textContent = app.analysis_start
    ? t("settings.windows", {fetch_start: app.fetch_start, analysis_start: app.analysis_start})
    : "";
}

function stepN(steps) {
  view.draftN = Math.min(N_MAX, Math.max(N_MIN, view.draftN + steps * N_STEP));
  renderSettings(view.state.app, running());
}

function applyN() {
  const change = {from: number("n", view.storedN), to: number("n", view.draftN)};
  if (window.confirm(t("settings.confirm", change))) act("POST", "/api/settings", {inflections_per_year: view.draftN});
}

// The candidate table (SPEC.md §12)

function renderTable(series, busy) {
  // Redrawn only when something changed, so a poll leaves the focus and the scroll position alone.
  const key = JSON.stringify([view.language, busy, series]);
  if (key === view.tableKey) return;
  view.tableKey = key;
  byId("rows").replaceChildren(...series.map((entry) => tableRow(entry, busy)));
}

function tableRow(entry, busy) {
  const row = element("tr");
  row.dataset.name = entry.name;
  row.dataset.status = entry.status;
  if (hasChart(entry)) row.dataset.chart = "";
  const estimated = {estimated: number("count", entry.estimated_points), points: number("count", entry.points)};
  row.append(
    cell(nameCell(entry), "name"),
    cell(titleCell(entry), "title"),
    cell(entry.source_id),
    cell(entry.frequency ?? "–"),
    cell(entry.form),
    cell(entry.last_obs_day ?? "–"),
    cell(entry.last_release_day ?? "–"),
    cell(t("table.estimated", estimated), "number"),
    cell(number("days", entry.a_star), "number"),
    cell(number("short", entry.beta), "number"),
    cell(number("short", entry.score), "number"),
    cell(number("short", entry.score_oos), "number"),
    cell(statusCell(entry), "status"),
    cell(entry.status === "KEPT" ? inFBox(entry, busy) : ""),
    cell(entry.origin === "USER" ? removeButton(entry, busy) : ""),
  );
  return row;
}

function hasChart(entry) {
  return !NO_CHART.has(entry.status);
}

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function fragment(...nodes) {
  const result = document.createDocumentFragment();
  result.append(...nodes.filter(Boolean));
  return result;
}

function cell(content, className = "") {
  const node = element("td", className);
  node.append(content);
  return node;
}

function nameCell(entry) {
  const name = element(hasChart(entry) ? "button" : "span", "series-name", entry.name);
  if (hasChart(entry)) {
    name.type = "button";
    name.setAttribute("aria-label", t("table.open", {name: entry.name}));
  }
  return fragment(name, entry.lead_name && element("span", "detail", entry.lead_name));
}

function titleCell(entry) {
  return fragment(element("span", "", seriesTitle(entry) ?? "–"), entry.units && element("span", "detail", entry.units));
}

function seriesTitle(entry) {
  return view.language === "zh-TW" && entry.title_zh ? entry.title_zh : entry.title;
}

function statusCell(entry) {
  const error = entry.last_fetch_error;
  return fragment(
    element("strong", "status-label", label("status", entry.status)),
    element("span", "detail", label("reason", entry.status)),
    error && element("span", "fetch-error", t("table.fetchError", {error: codedText(error)})),
  );
}

// last_fetch_error holds "CODE: English text" (SPEC.md §10): the code in the current language, the text as detail.
function codedText(text) {
  const match = CODED_TEXT.exec(text);
  return match ? t("table.errorDetail", {name: sourceErrorName(match[1]), detail: match[2]}) : text;
}

function sourceErrorName(code) {
  const http = HTTP_CODE.exec(code);
  if (http) return t("notice.httpStatus", {status: http[1]});
  return known("source_error", code) ? label("source_error", code) : code;
}

function inFBox(entry, busy) {
  const box = element("input", "in-f");
  box.type = "checkbox";
  box.checked = entry.selected;
  box.disabled = busy;
  box.setAttribute("aria-label", t("table.inF", {name: entry.name}));
  const target = element("label", "check-target");
  target.append(box);
  return target;
}

function removeButton(entry, busy) {
  const button = element("button", "remove", t("table.remove"));
  button.type = "button";
  button.disabled = busy;
  button.setAttribute("aria-label", t("table.removeName", {name: entry.name}));
  return button;
}

function rowClicked(event) {
  const row = event.target.closest("tr");
  if (!row) return;
  if (event.target.closest(".remove")) return removeSeries(row.dataset.name);
  if (event.target.closest(".check-target")) return;
  // A click that ends a text selection, such as a FRED ID being copied, opens nothing.
  if ("chart" in row.dataset && !String(window.getSelection())) openSeries(row.dataset.name);
}

function rowChanged(event) {
  if (event.target.matches(".in-f")) applySelection();
}

async function applySelection() {
  const boxes = [...byId("rows").querySelectorAll(".in-f")];
  const excluded = boxes.filter((box) => !box.checked).map((box) => box.closest("tr").dataset.name);
  if (!(await act("POST", "/api/selection", {excluded}))) {
    // A refused change must not stay on screen: draw the table from the last state again.
    view.tableKey = "";
    if (view.state) renderTable(view.state.series, running());
  }
}

function removeSeries(name) {
  if (window.confirm(t("table.removeConfirm", {name}))) act("DELETE", `/api/series/${encodeURIComponent(name)}`);
}

async function addSeries(event) {
  event.preventDefault();
  const input = byId("fred-id");
  const fredId = input.value.trim().toUpperCase();
  if (!FRED_ID.test(fredId)) return toast(t("add.invalid"));
  if (await act("POST", "/api/series", {fred_id: fredId})) input.value = "";
}

function toast(text) {
  byId("toast-text").textContent = text;
  byId("toast").hidden = false;
  clearTimeout(view.toastTimer);
  view.toastTimer = setTimeout(hideToast, TOAST_MS);
}

function hideToast() {
  byId("toast").hidden = true;
}

function renderEvents(events) {
  const key = JSON.stringify([view.language, events.map((event) => event.id)]);
  if (key === view.eventsKey) return;
  view.eventsKey = key;
  byId("event-list").replaceChildren(...events.map(eventItem));
  byId("events-empty").hidden = events.length > 0;
}

function eventItem(event) {
  const item = element("li");
  item.dataset.level = event.level;
  const head = [time(event.ts), label("level", event.level), label("event", event.kind), event.series];
  item.append(element("span", "event-head", head.filter(Boolean).join(" · ")), element("span", "detail", event.message));
  return item;
}

// Charts (SPEC.md §12)

function createPanels() {
  const loadF = () =>
    (view.fRequest ??= api("GET", "/api/charts/f").catch((error) => {
      view.fRequest = null;
      throw error;
    }));
  const loadSpy = () => api("GET", "/api/charts/spy");
  view.spy = makePanel({titleKey: "chart.spy", tagKey: "chart.spyTag", open: true, log: true, load: loadSpy, build: spyLines});
  view.f = makePanel({titleKey: "chart.f", tagKey: "chart.fTag", open: true, load: loadF, build: fLines});
  view.composite = makePanel({
    titleKey: "chart.composite",
    tagKey: "chart.compositeTag",
    open: true,
    load: loadF,
    build: compositeLines,
  });
  byId("main-charts").append(view.spy.details, view.f.details, view.composite.details);
}

function makePanel({titleKey = "", tagKey = "", open, log = false, load, build}) {
  const details = byId("chart-panel").content.firstElementChild.cloneNode(true);
  const panel = {
    details,
    load,
    build,
    title: details.querySelector(".chart-title"),
    tag: details.querySelector(".chart-tag"),
    note: details.querySelector(".chart-note"),
    members: details.querySelector(".chart-members"),
    tools: details.querySelector(".chart-tools"),
    logAxis: details.querySelector(".log-axis"),
    message: details.querySelector(".chart-message"),
    box: details.querySelector(".chart"),
    chart: null,
    data: null,
    blocked: null, // why the chart cannot be shown, as text
    error: null,
    transient: false, // the error was no answer at all, so the next successful poll retries
    loading: false,
    generation: 0, // counts the reloads, so that a late answer to an older one is dropped
    lines: [],
    lastTime: 0,
  };
  if (titleKey) panel.title.dataset.i18n = titleKey;
  if (tagKey) panel.tag.dataset.i18n = tagKey;
  details.querySelector(".log").hidden = !log;
  details.open = open;
  translate(details);
  details.addEventListener("toggle", () => {
    if (details.open) panel.error = null;
    updatePanel(panel);
  });
  details.querySelector(".ranges").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (button) zoom(panel, Number(button.dataset.years));
  });
  panel.logAxis.addEventListener("change", () => panel.chart?.setOption({yAxis: yAxis(panel)}));
  // Opening a panel, turning a phone or resizing the window changes the box the chart must fill.
  new ResizeObserver(() => {
    if (visible(panel)) panel.chart?.resize();
  }).observe(panel.box);
  return panel;
}

// Open, and not inside a closed group: a chart drawn in a hidden box would have no size.
function visible(panel) {
  return panel.details.open && !panel.details.parentElement.closest("details:not([open])");
}

function panels() {
  return [view.spy, view.f, view.composite, ...view.seriesPanels.values()];
}

function renderCharts(state) {
  const empty = state.app.computed_at === null ? t("chart.empty") : null;
  const noF = empty ?? (state.f.status === "OK" ? null : label("f_status", state.f.status));
  view.spy.blocked = empty;
  view.f.blocked = noF;
  view.composite.blocked = noF;
  renderFNote(state.f);
  renderSeriesPanels(state.series);
  if (state.app.computed_at !== view.computedAt) {
    view.computedAt = state.app.computed_at;
    reloadCharts();
  }
  for (const panel of panels()) {
    if (panel.transient) panel.error = null;
    updatePanel(panel);
  }
}

function renderFNote(f) {
  const ok = f?.status === "OK";
  view.f.note.hidden = !ok;
  view.f.members.hidden = !ok;
  if (!ok) return;
  const scores = {score: number("short", f.score), oos: number("short", f.score_oos), slope: number("short", f.slope_pct_per_year)};
  view.f.note.textContent = t("chart.fNote", scores);
  view.f.members.replaceChildren(...f.members.map(memberItem));
}

function memberItem(member) {
  const words = {lead_name: member.lead_name, b: number("days", member.b), beta: number("short", member.beta)};
  return element("li", "", t("chart.member", words));
}

function renderSeriesPanels(series) {
  const charted = series.filter(hasChart);
  const names = new Set(charted.map((entry) => entry.name));
  for (const [name, panel] of view.seriesPanels) {
    if (!names.has(name)) {
      panel.chart?.dispose();
      panel.details.remove();
      view.seriesPanels.delete(name);
    }
  }
  const leading = [];
  const other = [];
  for (const entry of charted) {
    const panel = seriesPanel(entry.name);
    panel.title.textContent = entry.lead_name ?? entry.name;
    panel.tag.textContent = seriesTitle(entry) ?? "";
    panel.note.textContent = entry.units ? t("chart.units", {units: entry.units}) : "";
    panel.note.hidden = !entry.units;
    (entry.lead_name ? leading : other).push(panel.details);
  }
  place(byId("leading-charts"), byId("leading-title"), leading);
  place(byId("other-charts"), byId("other-group"), other);
}

function seriesPanel(name) {
  if (!view.seriesPanels.has(name)) {
    const path = `/api/charts/series/${encodeURIComponent(name)}`;
    view.seriesPanels.set(name, makePanel({open: false, load: () => api("GET", path), build: seriesLines}));
  }
  return view.seriesPanels.get(name);
}

// Moving a panel keeps its chart but costs a layout, so panels move only when their order changed.
function place(container, heading, nodes) {
  heading.hidden = nodes.length === 0;
  const children = [...container.children];
  if (children.length !== nodes.length || nodes.some((node, index) => children[index] !== node)) {
    container.replaceChildren(...nodes);
  }
}

function openSeries(name) {
  const panel = view.seriesPanels.get(name);
  if (!panel) return;
  if (byId("other-group").contains(panel.details)) byId("other-group").open = true;
  panel.details.open = true;
  panel.details.scrollIntoView({behavior: "smooth", block: "start"});
}

function reloadCharts() {
  view.fRequest = null;
  for (const panel of panels()) {
    // The chart on screen stays until the new data replaces it.
    panel.generation += 1;
    panel.data = null;
    panel.error = null;
    panel.loading = false;
  }
}

function updatePanel(panel) {
  if (!view.state) return;
  if (panel.blocked) {
    panel.chart?.dispose();
    panel.chart = null;
    panel.data = null;
  }
  showMessage(panel);
  if (panel.blocked || !visible(panel)) return;
  if (!panel.data && !panel.loading && !panel.error) loadPanel(panel);
  else if (panel.data && !panel.chart) draw(panel);
}

function showMessage(panel) {
  const text = panel.blocked ?? panel.error ?? (panel.loading ? t("chart.loading") : "");
  panel.message.textContent = text;
  panel.message.hidden = !text;
  const hidden = Boolean(panel.blocked || panel.error);
  panel.tools.hidden = hidden;
  panel.box.hidden = hidden;
}

async function loadPanel(panel) {
  const generation = panel.generation;
  panel.loading = true;
  showMessage(panel);
  let data = null;
  let failure = null;
  try {
    data = await panel.load();
  } catch (error) {
    failure = error;
  }
  if (generation !== panel.generation) return;
  panel.loading = false;
  if (failure?.status === 401) return showLogin();
  if (failure) {
    panel.error = errorText(failure);
    panel.transient = failure.status === 0;
  } else {
    panel.data = data;
    panel.transient = false;
    if (visible(panel)) draw(panel);
  }
  updatePanel(panel);
}

function draw(panel) {
  const locale = view.language === "zh-TW" ? "ZH" : "EN";
  panel.chart ??= echarts.init(panel.box, darkScheme.matches ? "dark" : null, {renderer: "canvas", locale});
  panel.lines = panel.build(panel.data);
  panel.lastTime = Math.max(...panel.lines.map((line) => line.data.at(-1)?.[0] ?? -Infinity));
  panel.chart.setOption(chartOption(panel), {notMerge: true});
}

// A new colour scheme or language: ECharts takes its theme and locale when a chart starts.
function redrawCharts() {
  for (const panel of panels()) {
    panel.chart?.dispose();
    panel.chart = null;
    if (visible(panel) && panel.data) draw(panel);
  }
}

function zoom(panel, years) {
  if (!panel.chart) return;
  const range = years ? {startValue: panel.lastTime - years * YEAR_MS, endValue: panel.lastTime} : {start: 0, end: 100};
  panel.chart.dispatchAction({type: "dataZoom", ...range});
}

function spyLines(data) {
  return [
    points(t("legend.close"), data.raw),
    daily(t("legend.spyl1"), data.rt, "main"),
    daily(t("legend.reference"), data.ref, "reference"),
  ];
}

function seriesLines(data) {
  return [
    points(t("legend.points"), data.raw),
    daily(data.lead_name ?? t("legend.oneSided"), data.rt, "main"),
    daily(t("legend.reference"), data.ref, "reference"),
  ];
}

function fLines(data) {
  const f = daily(t("legend.f"), data.f, "main");
  f.markLine = {
    silent: true,
    symbol: "none",
    lineStyle: {color: color("guide"), type: "dashed", width: 1},
    label: {color: color("guide")},
    data: [{yAxis: 1}, {xAxis: data.split_day * DAY_MS, label: {formatter: t("chart.split")}}],
  };
  return [f];
}

function compositeLines(data) {
  return [daily(t("legend.spyl1"), data.spyl1, "main"), daily(t("legend.composite"), data.composite, "second")];
}

// A point's value holds until the next release; a daily series has a value on its own days only.
function points(name, raw) {
  return {name, role: "raw", hold: true, data: raw.days.map((day, index) => [day * DAY_MS, raw.values[index]])};
}

function daily(name, series, role) {
  const data = series.values.map((value, index) => [(series.first_day + index) * DAY_MS, value]);
  return {name, role, hold: false, data};
}

function seriesOf(line) {
  const raw = line.role === "raw";
  return {
    name: line.name,
    type: "line",
    data: line.data,
    color: color(line.role),
    sampling: "lttb",
    symbol: raw ? "circle" : "none",
    showSymbol: raw && line.data.length < SPARSE_POINTS,
    symbolSize: 4,
    lineStyle: {width: raw ? 1 : 2, type: line.role === "reference" ? "dashed" : "solid"},
    markLine: line.markLine,
  };
}

// The line colours live in style.css, next to the page's, for both colour schemes.
function color(role) {
  return getComputedStyle(document.documentElement).getPropertyValue(`--chart-${role}`).trim();
}

function chartOption(panel) {
  return {
    backgroundColor: "transparent",
    animation: false,
    // Days are UTC midnights: in UTC they show as their dates in every time zone.
    useUTC: true,
    textStyle: {fontFamily: getComputedStyle(document.body).fontFamily},
    grid: {left: 8, right: 16, top: 44, bottom: 64},
    // ECharts draws a circle on every line's legend: hidden, so a legend shows each line as it is drawn.
    legend: {type: "scroll", top: 0, left: 0, itemStyle: {opacity: 0}},
    tooltip: {trigger: "axis", renderMode: "richText", confine: true, formatter: (params) => tooltipText(panel, params)},
    xAxis: {type: "time"},
    yAxis: yAxis(panel),
    dataZoom: [
      // On a phone the charts fill most of the screen: a vertical swipe must still scroll the page.
      {type: "inside", zoomOnMouseWheel: "ctrl", moveOnMouseWheel: false, preventDefaultMouseMove: false},
      {type: "slider", height: 24, bottom: 12},
    ],
    series: panel.lines.map(seriesOf),
  };
}

function yAxis(panel) {
  const log = panel.logAxis.checked;
  // A log axis would otherwise stretch to whole powers of 10, and flatten the years in view.
  const extent = log ? {min: (range) => range.min, max: (range) => range.max} : {min: null, max: null};
  const axisLabel = {formatter: (value) => view.formats.axis.format(value)};
  return {type: log ? "log" : "value", scale: true, ...extent, axisLabel};
}

// ECharts' axis tooltip lists only the series with a point at the pointer's day, and sampling thins each series
// differently, so the tooltip reads every line at that day itself.
function tooltipText(panel, params) {
  const time = params[0]?.axisValue;
  if (time === undefined) return "";
  const rows = [new Date(time).toISOString().slice(0, 10)];
  for (const line of panel.lines) {
    const value = valueAt(line, time);
    if (value !== null) rows.push(`${line.name}  ${number("value", value)}`);
  }
  return rows.join("\n");
}

// The line's value at `time`: the last point at or before it.
function valueAt(line, time) {
  const data = line.data;
  if (!data.length || data[0][0] > time || (!line.hold && data.at(-1)[0] < time)) return null;
  let low = 0;
  let high = data.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (data[middle][0] <= time) low = middle;
    else high = middle - 1;
  }
  return data[low][1];
}

// Start

function bind() {
  byId("fetch").addEventListener("click", fetchData);
  byId("language").addEventListener("click", switchLanguage);
  byId("logout").addEventListener("click", logOut);
  byId("login-form").addEventListener("submit", logIn);
  byId("n-down").addEventListener("click", () => stepN(-1));
  byId("n-up").addEventListener("click", () => stepN(1));
  byId("n-apply").addEventListener("click", applyN);
  byId("rows").addEventListener("click", rowClicked);
  byId("rows").addEventListener("change", rowChanged);
  byId("add-form").addEventListener("submit", addSeries);
  byId("toast-close").addEventListener("click", hideToast);
  byId("other-group").addEventListener("toggle", () => {
    for (const panel of view.seriesPanels.values()) updatePanel(panel);
  });
  // iOS freezes a page in the background: catch up as soon as it shows again.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && view.state) poll();
  });
  darkScheme.addEventListener("change", redrawCharts);
}

function fetchData() {
  byId("fetch").disabled = true;
  act("POST", "/api/fetch", {});
}

async function switchLanguage() {
  const language = view.language === "en" ? "zh-TW" : "en";
  try {
    await loadLanguage(language);
  } catch {
    toast(t("notice.network"));
    return;
  }
  storeLanguage(language);
  renderBanners();
  if (view.state) render(view.state);
  redrawCharts();
}

async function start() {
  try {
    await loadLanguage(preferredLanguage());
  } catch {
    setTimeout(start, POLL_RUNNING_MS);
    return;
  }
  poll();
}

bind();
createPanels();
start();

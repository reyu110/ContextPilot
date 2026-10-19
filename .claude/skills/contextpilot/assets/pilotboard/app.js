"use strict";

const $ = id => document.getElementById(id);
const number = new Intl.NumberFormat(undefined, {maximumFractionDigits: 0});
const short = new Intl.NumberFormat(undefined, {notation: "compact", maximumFractionDigits: 1});
const usd = new Intl.NumberFormat(undefined, {style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4});
const key = location.hash.slice(1);
let data = null, selectedSession, offset = 0, days = 30, rate = 3, activeRequest;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function set(id, text) { $(id).textContent = text; }
function amount(tokens) { return usd.format(tokens * rate / 1e6); }
function signed(tokens) { return (tokens > 0 ? "+" : "") + number.format(tokens); }
function trim(id) { return id === "unassigned" ? "Unassigned session" : id.length > 18 ? id.slice(0, 8) + "…" + id.slice(-6) : id; }
function tone(value) { return value < 0 ? "negative" : "positive"; }
function bytes(value) {
  if (value < 1024) return number.format(value) + " B";
  if (value < 1048576) return short.format(value / 1024) + " KiB";
  return short.format(value / 1048576) + " MiB";
}
function emptyRow(columns, text) {
  const tr = node("tr"); const td = node("td", text); td.colSpan = columns; tr.append(td); return tr;
}

function sessionOptions() {
  const options = [new Option("All sessions", "")];
  for (const session of data.sessions) options.push(new Option(trim(session.session_id), session.session_id));
  if (data.selected_session && !data.sessions.some(s => s.session_id === data.selected_session)) {
    options.push(new Option(trim(data.selected_session), data.selected_session));
  }
  $("session").replaceChildren(...options);
  $("session").value = data.selected_session || "";
}

function renderMetrics() {
  const total = data.lifetime, scope = data.selected;
  set("lifetime-tokens", number.format(total.saved_tokens));
  $("lifetime-tokens").closest("article").classList.toggle("negative", total.saved_tokens < 0);
  set("lifetime-caption", total.saved_tokens < 0 ? "Net added context across recorded requests" : "Compared with one sampled-context reread");
  set("lifetime-money", amount(total.saved_tokens));
  $("lifetime-money").closest("article").classList.toggle("negative", total.saved_tokens < 0);
  set("money-caption", "Scenario at " + usd.format(rate) + " / 1M input tokens");
  set("session-tokens", number.format(scope.saved_tokens));
  $("session-tokens").closest("article").classList.toggle("negative", scope.saved_tokens < 0);
  set("session-caption", (data.selected_session ? trim(data.selected_session) : "All sessions") + " · " + number.format(scope.prompts) + " snapshots");
  set("session-money", amount(scope.saved_tokens) + " ESTIMATED NET VALUE");
  set("request-count", number.format(total.prompts));
  set("request-caption", number.format(data.session_count) + " recorded session" + (data.session_count === 1 ? "" : "s"));
  set("since", data.first_recorded ? "· since " + new Date(data.first_recorded).toLocaleDateString(undefined, {month: "short", day: "numeric", year: "numeric"}) : "· tracking begins with your next snapshot");
  $("empty").hidden = total.prompts > 0;
  set("cache-bytes", bytes(total.cache_bytes));
  set("baseline", number.format(scope.baseline_tokens) + " tokens");
  set("handoff", number.format(scope.handoff_tokens) + " tokens");
  const reduction = scope.baseline_tokens ? scope.saved_tokens / scope.baseline_tokens : null;
  set("reduction", reduction === null ? "—" : Math.round(Math.abs(reduction) * 100) + "%");
  set("reduction-label", reduction === null ? "no baseline yet" : reduction < 0 ? "larger handoff" : "net reduction");
  const arc = Math.max(0, Math.min(1, reduction || 0)) * 2 * Math.PI * 70;
  $("donut").setAttribute("stroke-dasharray", arc + " " + (2 * Math.PI * 70));
  $("donut").parentElement.setAttribute("aria-label", reduction === null ? "No baseline recorded" : Math.round(Math.abs(reduction) * 100) + "% " + (reduction < 0 ? "larger handoff" : "estimated net reduction"));
}

function svgNode(tag, attrs, text) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
  if (text !== undefined) element.textContent = text;
  return element;
}
function timeline() {
  const rows = [];
  const now = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const date = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
    const day = date.toISOString().slice(0, 10);
    rows.push({day, saved_tokens: data.daily.find(row => row.day === day)?.saved_tokens || 0});
  }
  const total = rows.reduce((sum, row) => sum + row.saved_tokens, 0);
  set("chart-total", signed(total) + " tokens · " + days + " days");
  if (!data.selected.prompts) {
    $("timeline").replaceChildren(node("p", "Your timeline will appear after the first recorded snapshot.", "chart-empty")); return;
  }
  const svg = svgNode("svg", {viewBox: "0 0 640 210", role: "img", "aria-label": "Daily estimated net token savings over the last " + days + " days. Total " + signed(total) + ". Negative bars show handoff overhead."});
  const max = Math.max(1, ...rows.map(row => row.saved_tokens));
  const min = Math.min(0, ...rows.map(row => row.saved_tokens));
  const y = value => 15 + (max - value) / (max - min) * 155;
  for (const value of [...new Set([max, (max + min) / 2, min, 0])]) {
    svg.append(svgNode("line", {x1: 43, x2: 635, y1: y(value), y2: y(value), class: value === 0 ? "chart-zero" : "chart-grid"}));
    if (value === max || value === min || value === 0) svg.append(svgNode("text", {x: 34, y: y(value) + 3, "text-anchor": "end", class: "chart-axis"}, short.format(value)));
  }
  const step = 590 / rows.length;
  for (const [index, row] of rows.entries()) {
    const x = 46 + index * step;
    if (row.saved_tokens !== 0) {
      const bar = svgNode("rect", {x, y: y(Math.max(0, row.saved_tokens)), width: Math.max(3, step - 6), height: Math.max(2, Math.abs(y(row.saved_tokens) - y(0))), rx: 3, class: "bar-" + (row.saved_tokens < 0 ? "negative" : "positive"), tabindex: "0", role: "img", "aria-label": row.day + ": " + signed(row.saved_tokens) + " estimated tokens"});
      bar.append(svgNode("title", {}, row.day + " · " + signed(row.saved_tokens) + " tokens")); svg.append(bar);
    }
    if (index === 0 || index === rows.length - 1 || index % Math.ceil(rows.length / 5) === 0) {
      svg.append(svgNode("text", {x: x + step / 2, y: 197, "text-anchor": "middle", class: "chart-axis"}, new Date(row.day + "T00:00:00Z").toLocaleDateString(undefined, {month: "short", day: "numeric", timeZone: "UTC"})));
    }
  }
  $("timeline").replaceChildren(svg);
}

function renderSessions() {
  set("session-count", number.format(data.session_count) + " sessions");
  const rows = data.sessions.map((session, index) => {
    const tr = node("tr"), label = node("td"), full = node("span", trim(session.session_id), "session-id");
    full.title = session.session_id;
    label.append(node("span", String(index + 1).padStart(2, "0"), "session-avatar"), full);
    tr.append(label, node("td", number.format(session.prompts)), node("td", signed(session.saved_tokens), tone(session.saved_tokens)), node("td", amount(session.saved_tokens)));
    const action = node("td"), button = node("button", "View ↗", "view-session");
    button.setAttribute("aria-label", "View session " + session.session_id);
    button.addEventListener("click", () => selectSession(session.session_id));
    action.append(button); tr.append(action); return tr;
  });
  $("session-rows").replaceChildren(...(rows.length ? rows : [emptyRow(5, "No recorded sessions yet.")]));
}
function renderPrompts() {
  const query = $("search").value.trim().toLowerCase();
  const rows = data.prompts.filter(p => (p.prompt_id + " " + p.session_id).toLowerCase().includes(query)).map(prompt => {
    const tr = node("tr"), label = node("td"), title = node("strong", "Snapshot · " + trim(prompt.prompt_id));
    title.title = prompt.prompt_id;
    label.append(title, node("small", new Date(prompt.created_at).toLocaleString() + " · " + trim(prompt.session_id)));
    tr.append(label, node("td", number.format(prompt.baseline_tokens) + " → " + number.format(prompt.handoff_tokens)), node("td", signed(prompt.saved_tokens), tone(prompt.saved_tokens)), node("td", amount(prompt.saved_tokens)));
    return tr;
  });
  $("prompt-rows").replaceChildren(...(rows.length ? rows : [emptyRow(4, query ? "No matching IDs on this page." : "Your snapshot requests will appear here.")]));
  set("page-caption", data.selected.prompts ? number.format(offset + 1) + "–" + number.format(Math.min(offset + 50, data.selected.prompts)) + " of " + number.format(data.selected.prompts) + " requests" : "No recorded requests");
  $("previous").disabled = offset === 0;
  $("next").disabled = offset + 50 >= data.selected.prompts;
}
function render() {
  if (!data) return;
  renderMetrics(); timeline(); renderSessions(); renderPrompts();
}
async function load() {
  if (activeRequest) activeRequest.abort();
  const currentRequest = new AbortController();
  activeRequest = currentRequest; $("refresh").disabled = true;
  try {
    if (!key) throw new Error("Open the original Pilotboard link printed in your terminal. Its access key stays in the URL fragment.");
    const args = new URLSearchParams({offset: String(offset)});
    if (selectedSession !== undefined) args.set("session", selectedSession);
    const response = await fetch("/api/usage?" + args, {headers: {"X-Pilotboard-Key": key}, cache: "no-store", signal: currentRequest.signal});
    if (!response.ok) throw new Error(await response.text());
    data = await response.json();
    for (const totals of [data.lifetime, data.selected, ...data.sessions, ...data.prompts, ...data.daily]) {
      for (const field of ["baseline_tokens", "handoff_tokens", "saved_tokens", "cache_bytes"]) {
        if (!Number.isSafeInteger(totals[field])) throw new Error("Usage history contains invalid numeric values. Preserve the database and inspect it before continuing.");
      }
    }
    set("project", data.project); document.title = "Pilotboard · " + data.project;
    sessionOptions(); render(); $("error").hidden = true;
    set("updated", "Updated " + new Date().toLocaleTimeString() + " · refreshes every 15s");
  } catch (error) {
    if (error.name === "AbortError") return;
    set("error", error.message || "The local dashboard could not connect."); $("error").hidden = false;
    set("updated", "Connection needs attention");
  } finally { if (activeRequest === currentRequest) { activeRequest = null; $("refresh").disabled = false; } }
}
function selectSession(value) { selectedSession = value; offset = 0; $("search").value = ""; load(); }
$("session").addEventListener("change", event => selectSession(event.target.value));
$("refresh").addEventListener("click", load);
$("search").addEventListener("input", () => { if (data) renderPrompts(); });
$("previous").addEventListener("click", () => { offset = Math.max(0, offset - 50); load(); });
$("next").addEventListener("click", () => { offset += 50; load(); });
$("rate").addEventListener("input", () => {
  const valid = $("rate").checkValidity() && $("rate").value !== "";
  $("rate").setAttribute("aria-invalid", String(!valid));
  if (!valid) return;
  rate = Number($("rate").value);
  try { localStorage.setItem("pilotboard-rate", $("rate").value); } catch (_) { /* Browser storage may be disabled. */ }
  render();
});
// Keep the access key in the URL fragment when moving between dashboard sections.
for (const link of document.querySelectorAll('a[href^="#"]')) link.addEventListener("click", event => {
  event.preventDefault();
  const target = document.getElementById(link.getAttribute("href").slice(1));
  target?.scrollIntoView(); if (target?.id === "main") target.focus({preventScroll: true});
});
for (const button of document.querySelectorAll("[data-days]")) button.addEventListener("click", () => {
  days = Number(button.dataset.days);
  for (const item of document.querySelectorAll("[data-days]")) { item.classList.toggle("selected", item === button); item.setAttribute("aria-pressed", String(item === button)); }
  if (data) timeline();
});
for (const link of document.querySelectorAll(".nav-link")) link.addEventListener("click", () => {
  for (const item of document.querySelectorAll(".nav-link")) item.classList.toggle("active", item === link);
});
try {
  const stored = localStorage.getItem("pilotboard-rate");
  if (stored !== null && stored.trim() !== "" && Number.isFinite(Number(stored)) && Number(stored) >= 0 && Number(stored) <= 1000) { rate = Number(stored); $("rate").value = stored; }
} catch (_) { /* A rate is optional; the illustrative default remains visible. */ }
load();
setInterval(() => { if (!document.hidden) load(); }, 15000);

"use strict";

const $ = id => document.getElementById(id);
const number = new Intl.NumberFormat(undefined, {maximumFractionDigits: 0});
const short = new Intl.NumberFormat(undefined, {notation: "compact", maximumFractionDigits: 1});
const usd = new Intl.NumberFormat(undefined, {style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4});
const key = location.hash.slice(1);
let data = null, selectedSession, offset = 0, liveOffset = 0, days = 30, rate = 3, activeRequest;

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
function recentSample(timestamp) { const age = Date.now() / 1000 - timestamp; return Number.isFinite(timestamp) && age >= 0 && age < 120; }
function bytes(value) {
  if (value < 1024) return number.format(value) + " B";
  if (value < 1048576) return short.format(value / 1024) + " KiB";
  return short.format(value / 1048576) + " MiB";
}
function emptyRow(columns, text) {
  const tr = node("tr"); const td = node("td", text); td.colSpan = columns; tr.append(td); return tr;
}

function updateInvocation(check = false) {
  const name = data?.agent === "codex" ? "$contextpilot" : data?.optimizer?.status?.mode === "plugin" ? "/contextpilot:contextpilot" : "/contextpilot";
  return name + " update" + (check ? " check" : "");
}

function renderUpdates() {
  const update = data.updates || {}, state = update.state;
  const labels = {available:"Update available",current:"Up to date",ahead:"Local version ahead",unchecked:"Not checked yet",unavailable:"Check unavailable",stale:"Saved check is stale"};
  set("update-state",labels[state] || "Unavailable");
  $("update-state").className = "status-pill " + (state === "available" ? "ready" : "neutral");
  $("update-state").closest("section").classList.toggle("update-available",state === "available");
  set("update-note", "Installed: " + (update.installed || "unavailable") + " · Latest observed: " + (update.latest || "unknown") + (update.checked_at ? " · Checked " + new Date(update.checked_at*1000).toLocaleString() : "") + (update.stale && update.latest ? " · historical observation" : "") + ". " + (update.warning || "") + (update.automatic_checks === false ? " Automatic checks are disabled." : " Active plugin hooks check at most daily; skill-only installs need an explicit check.") + " This read-only dashboard never contacts GitHub or installs updates.");
  renderUpdateCommand();
}

function renderUpdateCommand() {
  const recipe = data?.updates?.commands?.[$("update-installation").value];
  set("update-terminal",recipe?.command || "Choose an installation type to see its command.");
  set("update-terminal-note",recipe?.note || "The dashboard only displays instructions. Updates require an explicit command in your agent or terminal.");
}

async function copyUpdateCommand(check) {
  const command = updateInvocation(check);
  try {
    await navigator.clipboard.writeText(command);
    set("update-copy-note","Copied " + command + ". Paste it into your agent chat.");
  } catch { set("update-copy-note","Run in your agent chat: " + command); }
}

function sessionOptions() {
  const options = [new Option("All sessions", "")];
  const sessions = [...data.sessions, ...(data.optimizer?.report?.sessions || [])];
  const seen = new Set();
  for (const session of sessions) if (!seen.has(session.session_id)) { options.push(new Option(trim(session.session_id), session.session_id)); seen.add(session.session_id); }
  if (data.selected_session && !seen.has(data.selected_session)) {
    options.push(new Option(trim(data.selected_session), data.selected_session));
  }
  $("session").replaceChildren(...options);
  $("session").value = data.selected_session || "";
}

function renderMetrics() {
  $("snapshot-error").hidden = !data.snapshot_error;
  set("snapshot-error", data.snapshot_error || "");
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

function renderOptimizer() {
  const codex = data.agent === "codex";
  $("claude-native-panel").hidden = codex; $("claude-native-details").hidden = codex;
  set("optimizer-title", codex ? "Codex context support" : "Automatic context optimizer");
  set("agent-intro",codex ? "Bounded handoffs. Trusted lifecycle support. Clear estimates." : "Automatic optimization. Clear estimates. Every reduction accounted for.");
  set("handoff-scope",codex ? "Project-wide snapshot history shared with Claude Code. These what-if scenarios are separate from Codex hook overhead above." : "Sampled files compared with their handoff. These estimates are never added to live optimizer reductions.");
  set("live-actions-note",codex ? "Guidance, retry notices and pre-compaction checkpoints" : "Filters, guidance, checkpoints and native observations");
  set("audit-scope",codex ? "Non-destructive review of root AGENTS.md and AGENTS.override.md" : "Non-destructive review of root instructions and .claude/rules");
  set("checkpoint-help",codex ? "Trusted Codex plugin hooks save a bounded repository checkpoint before compaction. Skill-only installs can use $contextpilot snapshot. Snapshot history is shared within this project; optimizer history is separate for each agent." : "The plugin saves repository checkpoints before compaction and at session end. Standalone users can run /contextpilot snapshot. Earlier usage is not reconstructed.");
  const optimizer = data.optimizer, report = optimizer?.report;
  if (!report || optimizer.error) {
    for (const id of ["live-net", "live-value", "live-selected", "live-actions", "native-context", "native-five", "native-week", "native-input", "native-output", "native-cache-read", "native-cache-write", "native-window", "native-cost"]) set(id, "—");
    for (const id of ["feature-rows", "live-session-rows", "live-prompt-rows"]) $(id).replaceChildren(emptyRow(id === "live-session-rows" ? 5 : 4, "Optimizer history unavailable; previous figures have been cleared."));
    $("live-contributions").replaceChildren(); $("live-timeline").replaceChildren();
    set("optimizer-health", "Needs attention"); set("optimizer-health-note", optimizer?.error || "Restart Pilotboard to load the new optimizer views.");
    set("native-note", "Native history is unavailable; no previous reading is presented as current.");
    set("native-cache-caption", "Native details unavailable."); set("native-reset-note", "Reset times unavailable."); set("native-settings-note", "Native settings observations unavailable.");
    set("native-controls-note", "Native configuration hints unavailable."); $("platform-limit-rows").replaceChildren(emptyRow(2,"Capability boundaries unavailable; restart Pilotboard if needed."));
    set("audit-note", "Audit status unavailable."); $("audit-rows").replaceChildren(emptyRow(4,"Audit status unavailable."));
    set("live-page-caption", "Optimizer history unavailable");
    $("live-previous").disabled = true; $("live-next").disabled = true;
    set("optimizer-note", "Snapshot scenarios below remain separate."); return;
  }
  for (const row of [report.lifetime, report.selected, ...report.sessions, ...report.prompts, ...report.features, ...report.daily, report.guidance]) {
    for (const field of ["actions", "before_bytes", "after_bytes", "net_bytes", "net_tokens"]) {
      if (!Number.isSafeInteger(row[field])) throw new Error("Optimizer history contains invalid counts; preserve it for inspection.");
    }
  }
  const state = optimizer.status || {}, recent = recentSample(report.last_activity);
  set("optimizer-health", state.warning ? "Needs attention" : !state.enabled ? "Not active" : recent ? "Recent hook activity" : "Configured · awaiting activity");
  $("optimizer-health").className = "status-pill " + (state.enabled && recent ? "ready" : "neutral");
  set("optimizer-health-note", state.warning || (state.enabled ? (state.mode === "plugin" ? "Plugin startup is configured. " : "Project-local hooks are configured. ") + "Last activity: " + (report.last_activity ? new Date(report.last_activity * 1000).toLocaleString() : "not yet observed") + ". Configuration alone does not prove hooks are running in your current chat." : "Automatic startup requires the ContextPilot plugin. Existing standalone installations keep their explicit auto controls. Previously recorded history remains available."));
  if (codex) set("optimizer-health-note",state.warning || "Codex lifecycle support " + (state.enabled ? "is configured" : "is paused or has not started") + ". Last recorded activity: " + (report.last_activity ? new Date(report.last_activity*1000).toLocaleString() : "none") + ". Install the plugin and review/trust its hooks in Codex. No Claude settings, tool output, model or reasoning preferences are changed. Configuration does not prove current-session hook acceptance.");
  for (const [id, value] of [["live-net", report.lifetime.net_tokens], ["live-selected", report.selected.net_tokens]]) { set(id, signed(value)); $(id).closest("article").classList.toggle("negative", value < 0); }
  set("live-value", amount(report.lifetime.net_tokens)); $("live-value").closest("article").classList.toggle("negative", report.lifetime.net_tokens < 0);
  set("live-selected-note", (data.selected_session ? trim(data.selected_session) : "All sessions") + " · " + number.format(report.selected.actions) + " actions");
  set("live-selected-value", amount(report.selected.net_tokens) + " SCENARIO VALUE");
  set("live-actions", number.format(report.lifetime.actions));
  set("optimizer-note", "Net = sum of ceil(before bytes / 4) − ceil(after bytes / 4), including recorded guidance overhead. Limits have no invented full-read baseline. Negative totals mean added context. Native billing and hook acceptance are not verified. Snapshot scenarios below are separate." + (report.legacy_actions ? " " + number.format(report.legacy_actions) + " older actions have no date and remain in lifetime totals only." : ""));
  const native = report.native, fresh = native && recentSample(native.updated_at);
  for (const [id, field] of [["native-context", "context_used"], ["native-five", "five_hour"], ["native-week", "seven_day"]]) {
    const value = native?.[field]; set(id, typeof value === "number" && Number.isFinite(value) ? value.toFixed(1) + "%" : "—");
  }
  set("native-note", native ? (fresh ? "Latest native sample" : "Historical sample · not live") + " · " + new Date(native.updated_at * 1000).toLocaleString() + " · session " + trim(native.session_id) + ". Context is current window size, not lifetime consumption. Missing allowance fields are unavailable, not zero." : "No native sample yet. A supported Claude session must invoke the owned status line; existing status lines are preserved. No transcript or billing data is inferred.");
  renderNativeDetails(native, state);
  renderAudit(report);
  renderFeatures(report, state);
  $("platform-limit-rows").replaceChildren(...((report.limitations || []).length ? report.limitations.map(limit => {
    const tr = node("tr"); tr.append(node("td",limit.label),node("td",limit.reason)); return tr;
  }) : [emptyRow(2,"Restart Pilotboard to load capability boundaries.")]));
  renderLiveRows(report);
  timeline(true);
}

function renderNativeDetails(native, state) {
  for (const [id, field] of [["native-input","input_tokens"],["native-output","output_tokens"],["native-cache-read","cache_read"],["native-cache-write","cache_write"],["native-window","window_size"]]) {
    set(id, Number.isSafeInteger(native?.[field]) && native[field] >= 0 ? number.format(native[field]) : "—");
  }
  set("native-cost", typeof native?.cost_usd === "number" && Number.isFinite(native.cost_usd) && native.cost_usd >= 0 ? usd.format(native.cost_usd) : "—");
  const values = [native?.input_tokens, native?.cache_read, native?.cache_write];
  const total = values.every(value => Number.isSafeInteger(value) && value >= 0) ? values.reduce((a,b) => a+b,0) : null;
  set("native-cache-caption", total > 0 ? "Cache-read share of reported input: " + (100*native.cache_read/total).toFixed(1) + "%. This describes cache use, not tokens or money saved." : "Cache composition is unavailable until complete native input components are supplied.");
  const resets = [];
  for (const [field,label] of [["five_reset","5-hour reset"],["seven_reset","7-day reset"]]) if (typeof native?.[field] === "number" && Number.isFinite(native[field])) resets.push(label + ": " + new Date(native[field]*1000).toLocaleString());
  set("native-reset-note", resets.length ? resets.join(" · ") : "Allowance reset times were not supplied.");
  const effort = {1:"low",2:"medium",3:"high",4:"xhigh",5:"max"}[native?.effort_code];
  const observed = [];
  if (native?.concise_active === 1 || native?.concise_active === 0) observed.push("Last observed output: " + (native.concise_active ? "Concise" : "other native style"));
  if (effort) observed.push("Last observed effort: " + effort);
  const prefs = state.native_preferences || {};
  set("native-settings-note", (observed.length ? observed.join(" · ") + ". " : "No native style/effort observations. ") + "Configuration sources: concise " + (prefs.concise || "unavailable") + "; effort " + (prefs.effort || "unavailable") + "; compaction " + (prefs.compaction || "unavailable") + "; status line " + (prefs.statusline || "unavailable") + ". Registered defaults are not proof of activation.");
  const controls = state.native_controls || {};
  set("native-controls-note", "Configuration hints: MCP tool search — " + nativeControlLabel(controls.tool_search) + "; prompt caching — " + nativeControlLabel(controls.prompt_cache) + ". Claude manages both automatically on supported models/deployments. These hints inspect saved settings and this process's environment, not the active Claude process or managed/launch policy. No servers, permissions or caching preferences are changed.");
}

function nativeControlLabel(control) {
  const modes = {platform_default:"native default · deployment dependent",enabled:"enabled preference",disabled:"disabled preference",automatic:"automatic threshold preference",custom:"custom preference · not interpreted",unavailable:"unavailable"};
  const sources = {environment:"inspected environment",settings:"saved settings",platform:"platform",unavailable:"unavailable"};
  return (modes[control?.mode] || "unavailable") + " (" + (sources[control?.source] || "unavailable") + ")";
}

function renderAudit(report) {
  const audit = report.instruction_audit;
  if (!audit) {
    set("audit-note", report.audit_warning || "No saved audit yet. Session-start hooks audit eligible project instructions automatically.");
    $("audit-rows").replaceChildren(emptyRow(4,"No eligible audited files available.")); return;
  }
  set("audit-note", (audit.stale ? "Stale metadata · " : "Last audited · ") + new Date(audit.created_at*1000).toLocaleString() + " · " + audit.files.length + " files · " + bytes(audit.bytes) + " sampled" + (audit.truncated ? " · partial coverage" : "") + ". " + audit.skipped + " read rejections. Counts are advisory; preserve all requirements.");
  $("audit-rows").replaceChildren(...(audit.files.length ? audit.files.map(file => {
    const tr = node("tr"); tr.append(node("td",file.path),node("td",number.format(file.bytes)),node("td",number.format(file.repeated_bytes)),node("td",file.truncated ? "Partial sample" : "Full file sample")); return tr;
  }) : [emptyRow(4,"No eligible instruction files found in the audit scope.")]));
}

function renderFeatures(report, state) {
  const rows = report.features.map(feature => {
    let availability;
    if (feature.mode === "unavailable") availability = "Not supported by Codex adapter";
    else if (feature.mode === "recommendation") availability = "Recommendation";
    else if (feature.mode === "report") availability = "Available · read-only";
    else if (feature.id === "handoffs") availability = "Available · bounded snapshots";
    else if (feature.id === "mcp") availability = (feature.actions ? "ToolSearch call recorded · " : "") + nativeControlLabel(state.native_controls?.tool_search);
    else if (feature.id === "cache") availability = (typeof report.native?.cache_read === "number" ? "Native cache sample · " : "") + nativeControlLabel(state.native_controls?.prompt_cache);
    else if (!state.enabled) availability = "Not active";
    else if (feature.id === "budget") availability = report.native && recentSample(report.native.updated_at) ? "Native-aware sizing ready" : "Fallback sizing ready";
    else if (feature.id === "instructions") availability = report.instruction_audit ? (report.instruction_audit.stale ? "Audit stale · advisory" : "Audit available · advisory") : "Audit unavailable";
    else if (feature.id === "retry") availability = feature.actions ? "Guidance recorded" : "Ready · no notice recorded";
    else if (feature.id === "exclusions") availability = "Enforced on eligible files";
    else if (feature.mode === "guidance") availability = report.guidance.actions ? "Guidance delivered" : "Guidance · awaiting session";
    else if (feature.mode === "native") {
      const key = {concise: "outputStyle", effort: "effortLevel", native: "statusLine", usage: "statusLine"}[feature.id];
      availability = feature.id === "compaction" ? (state.compaction_percent ? "Configured threshold" : "Existing native setting") : (["native", "usage"].includes(feature.id) && report.native ? "Native sample recorded" : key && state.defaults?.includes(key) ? "Default registered" : "Existing preference / unavailable");
    } else availability = feature.actions ? "Recorded activity" : "Ready · no recorded action";
    const tr = node("tr"), label = node("td"); label.append(node("strong", feature.label), node("small", feature.detail));
    tr.append(label, node("td", availability), node("td", number.format(feature.actions)), node("td", feature.actions ? signed(feature.net_tokens) : "—", tone(feature.net_tokens))); return tr;
  });
  $("feature-rows").replaceChildren(...rows);
  const parts = [...report.features.filter(feature => feature.before_bytes || feature.after_bytes), {label: "Session guidance", ...report.guidance}];
  const maximum = Math.max(1, ...parts.map(part => Math.abs(part.net_tokens)));
  $("live-contributions").replaceChildren(...parts.filter(part => part.actions).map(part => {
    const row = node("div", undefined, "contribution-row"), label = node("div"), track = node("div", undefined, "contribution-track"), bar = node("div", undefined, "contribution-bar " + tone(part.net_tokens));
    label.append(node("span", part.label), node("strong", signed(part.net_tokens)));
    // Native progress elements work under the dashboard's restrictive content security policy.
    const meter = document.createElement("meter"); meter.min = 0; meter.max = maximum; meter.value = Math.abs(part.net_tokens); meter.setAttribute("aria-label", part.label + ": " + signed(part.net_tokens) + " estimated tokens");
    bar.append(meter); track.append(bar); row.append(label, track); return row;
  }));
  if (!$("live-contributions").children.length) $("live-contributions").append(node("p", "Recorded payload changes will appear here.", "chart-empty"));
}

function renderLiveRows(report) {
  $("live-session-rows").replaceChildren(...(report.sessions.length ? report.sessions.map(session => {
    const tr = node("tr"), label = node("td", trim(session.session_id)); label.title = session.session_id;
    tr.append(label, node("td", number.format(session.actions)), node("td", signed(session.net_tokens), tone(session.net_tokens)), node("td", amount(session.net_tokens)));
    const action = node("td"), button = node("button", "View ↗", "view-session"); button.setAttribute("aria-label", "View optimizer session " + session.session_id); button.addEventListener("click", () => selectSession(session.session_id)); action.append(button); tr.append(action); return tr;
  }) : [emptyRow(5, "No optimizer actions recorded yet. History begins with supported hook events.")]));
  $("live-prompt-rows").replaceChildren(...(report.prompts.length ? report.prompts.map(prompt => {
    const tr = node("tr"), cell = node("td"), details = node("details"), summary = node("summary", trim(prompt.prompt_id)); summary.title = prompt.prompt_id;
    const list = node("ul", undefined, "receipt-list");
    for (const event of prompt.events) list.append(node("li", event.kind + " · " + trim(event.tool_id) + " · " + number.format(event.before_bytes) + " → " + number.format(event.after_bytes) + " bytes"));
    if (prompt.events_truncated) list.append(node("li", "Latest 20 actions shown; totals include all actions."));
    details.append(summary, node("small", trim(prompt.session_id) + " · " + (prompt.created_at ? new Date(prompt.created_at).toLocaleString() : "Legacy action · date unavailable")), list); cell.append(details);
    tr.append(cell, node("td", number.format(prompt.before_bytes) + " → " + number.format(prompt.after_bytes)), node("td", signed(prompt.net_tokens), tone(prompt.net_tokens)), node("td", amount(prompt.net_tokens))); return tr;
  }) : [emptyRow(4, "No optimizer actions for this scope.")]));
  set("live-page-caption", report.prompt_count ? number.format(liveOffset + 1) + "–" + number.format(Math.min(liveOffset + 50, report.prompt_count)) + " of " + number.format(report.prompt_count) + " prompt groups" : "No recorded prompt groups");
  $("live-previous").disabled = liveOffset === 0; $("live-next").disabled = liveOffset + 50 >= report.prompt_count;
}

function svgNode(tag, attrs, text) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
  if (text !== undefined) element.textContent = text;
  return element;
}
function timeline(live = false) {
  const span = live ? 30 : days, source = live ? data.optimizer.report.daily : data.daily, field = live ? "net_tokens" : "saved_tokens", target = live ? "live-timeline" : "timeline";
  const rows = [];
  const now = new Date();
  for (let i = span - 1; i >= 0; i--) {
    const date = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
    const day = date.toISOString().slice(0, 10);
    rows.push({day, saved_tokens: source.find(row => row.day === day)?.[field] || 0});
  }
  const total = rows.reduce((sum, row) => sum + row.saved_tokens, 0);
  if (!live) set("chart-total", signed(total) + " tokens · " + span + " days");
  if (!(live ? data.optimizer.report.selected.actions : data.selected.prompts)) {
    $(target).replaceChildren(node("p", live ? "Your timeline will appear after the first recorded action." : "Your timeline will appear after the first recorded snapshot.", "chart-empty")); return;
  }
  const svg = svgNode("svg", {viewBox: "0 0 640 210", role: "img", "aria-label": "Daily estimated net token reduction over the last " + span + " days. Total " + signed(total) + ". Negative bars show added context."});
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
  $(target).replaceChildren(svg);
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
  renderMetrics(); renderOptimizer(); timeline(); renderSessions(); renderPrompts();
}
async function load() {
  if (activeRequest) activeRequest.abort();
  const currentRequest = new AbortController();
  activeRequest = currentRequest; $("refresh").disabled = true;
  try {
    if (!key) throw new Error("Open the original Pilotboard link printed in your terminal. Its access key stays in the URL fragment.");
    const args = new URLSearchParams({offset: String(offset), live_offset: String(liveOffset)});
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
    sessionOptions(); render(); renderUpdates(); $("error").hidden = true;
    set("updated", "Updated " + new Date().toLocaleTimeString() + " · refreshes every 15s");
  } catch (error) {
    if (error.name === "AbortError") return;
    set("error", error.message || "The local dashboard could not connect."); $("error").hidden = false;
    set("updated", "Connection needs attention");
  } finally { if (activeRequest === currentRequest) { activeRequest = null; $("refresh").disabled = false; } }
}
function selectSession(value) { selectedSession = value; offset = 0; liveOffset = 0; $("search").value = ""; load(); }
$("session").addEventListener("change", event => selectSession(event.target.value));
$("refresh").addEventListener("click", load);
$("copy-update").addEventListener("click",() => copyUpdateCommand(false));
$("copy-update-check").addEventListener("click",() => copyUpdateCommand(true));
$("update-installation").addEventListener("change",renderUpdateCommand);
$("search").addEventListener("input", () => { if (data) renderPrompts(); });
$("previous").addEventListener("click", () => { offset = Math.max(0, offset - 50); load(); });
$("next").addEventListener("click", () => { offset += 50; load(); });
$("live-previous").addEventListener("click", () => { liveOffset = Math.max(0, liveOffset - 50); load(); });
$("live-next").addEventListener("click", () => { liveOffset += 50; load(); });
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
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });

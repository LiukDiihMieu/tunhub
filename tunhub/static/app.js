"use strict";

const POLL_MS = 1500;
// A hidden tab still polls, slowly, so the favicon dot keeps working.
const HIDDEN_POLL_MS = 15000;

const tiles = new Map(); // service id -> { el, service, iconKey }
const container = document.getElementById("services");
const template = document.getElementById("tile-template");
const drawer = document.getElementById("drawer");
const $ = (id) => document.getElementById(id);
const eventsPanel = $("events");

let hubUrl = location.origin;
let configDir = "";
let drawerId = null; // service shown in the drawer
let drawerIconKey = null;
let drawerRoutesKey = null;

// --------------------------------------------------------------- helpers

async function api(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Tunhub": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || data.detail || response.statusText);
  return data;
}

function showBanner(text, isError = false) {
  const banner = $("banner");
  banner.textContent = text;
  banner.classList.toggle("bad", isError);
  banner.hidden = false;
  clearTimeout(showBanner.timer);
  showBanner.timer = setTimeout(() => (banner.hidden = true), isError ? 10000 : 4000);
}

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 2500);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
}

function activeRoute(service) {
  return service.active_route === null ? null : service.routes[service.active_route];
}

/** Short human status: "Direct", "SSH", "Connecting…", "Off", ... */
function statusText(service) {
  switch (service.state) {
    case "up": {
      const route = activeRoute(service);
      return route && route.kind === "direct" ? "Direct" : "SSH";
    }
    case "connecting":
      return "Connecting…";
    case "retrying":
      return "Failed · retrying";
    default:
      return "Off";
  }
}

function setStatusAttributes(el, service) {
  el.dataset.state = service.state;
  const route = activeRoute(service);
  el.dataset.routeKind = route ? route.kind : "";
}

// ----------------------------------------------------------------- icons

function initials(name) {
  const words = name.split(/[\s_-]+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  const word = words[0] || "?";
  return word[0].toUpperCase() + word.slice(1, 2);
}

function hueOf(text) {
  let hash = 0;
  for (const ch of text) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  return hash % 360;
}

function renderLetters(iconEl, service) {
  iconEl.replaceChildren(initials(service.name));
  iconEl.classList.add("letters");
  iconEl.style.setProperty("--hue", hueOf(service.id));
}

/** Show the discovered/configured icon, or generated initials. */
function renderIcon(iconEl, service) {
  if (!service.icon_version) {
    renderLetters(iconEl, service);
    return;
  }
  const img = document.createElement("img");
  img.alt = "";
  img.src = `/api/icons/${service.id}?v=${service.icon_version}`;
  img.onerror = () => renderLetters(iconEl, service);
  iconEl.classList.remove("letters");
  iconEl.replaceChildren(img);
}

const iconKeyOf = (service) => `${service.icon_version}|${service.name}`;

// ----------------------------------------------------------------- tiles

function createTile(service) {
  const el = template.content.firstElementChild.cloneNode(true);
  el.id = service.id;
  const tile = { el, service, iconKey: null };
  const launch = el.querySelector(".launch");

  launch.addEventListener("click", async (event) => {
    if (tile.service.url) return; // web: plain link to /open/<id>
    event.preventDefault();
    // TCP: connect and copy the address for the RDP/VNC/... client.
    const { address, state } = tile.service;
    await copyText(address);
    if (state === "up") {
      toast(`Copied ${address}`);
      return;
    }
    toast(`Copied ${address} · connecting…`);
    try { await api(`/api/services/${tile.service.id}/start`); } catch (err) { showBanner(err.message, true); }
    poll();
  });

  el.querySelector(".stop").addEventListener("click", async () => {
    try { await api(`/api/services/${tile.service.id}/stop`); } catch (err) { showBanner(err.message, true); }
    poll();
  });

  el.querySelector(".more").addEventListener("click", (event) => {
    if (drawerId === tile.service.id) closeDrawer();
    else openDrawer(tile.service.id, event.currentTarget);
  });

  tiles.set(service.id, tile);
  return tile;
}

function updateTile(tile, service) {
  tile.service = service;
  const el = tile.el;
  const q = (selector) => el.querySelector(selector);

  setStatusAttributes(el, service);
  el.classList.toggle("selected", drawerId === service.id);

  const launch = q(".launch");
  if (service.url) {
    launch.href = `/open/${service.id}`;
    launch.target = "_blank";
    launch.rel = "noopener";
    launch.title = `Open ${service.name}`;
  } else {
    launch.href = "#";
    launch.removeAttribute("target");
    launch.title = `Connect and copy ${service.address}`;
  }

  q(".name").textContent = service.name;
  q(".state-text").textContent = statusText(service);
  el.title = service.state === "retrying" ? service.last_error || "" : "";

  // The direct target looks like a different machine (see known_macs).
  const warn = q(".identity-warn");
  warn.hidden = !service.identity_warning;
  warn.title = service.identity_warning || "";

  const key = iconKeyOf(service);
  if (tile.iconKey !== key) {
    tile.iconKey = key;
    renderIcon(q(".icon"), service);
  }

  const tcpAddress = q(".tcp-address");
  tcpAddress.hidden = Boolean(service.url);
  tcpAddress.textContent = service.address;

  // Only an "on" connection can be stopped (back to auto); auto-direct only
  // listens locally and comes back by itself.
  q(".stop").hidden = service.mode !== "on";
}

// ---------------------------------------------------------------- drawer

// The drawer is a non-modal side panel: the tiles stay usable while it is open.
// Focus moves into it on open (not trapped) and back to its ⋯ button on close.
let drawerTrigger = null;

function openDrawer(id, trigger = null) {
  if (!eventsPanel.hidden) eventsPanel.hidden = true; // one side panel at a time
  drawerId = id;
  drawerTrigger = trigger;
  drawerIconKey = null;
  drawerRoutesKey = null;
  drawer.hidden = false;
  for (const tile of tiles.values()) tile.el.classList.toggle("selected", tile.service.id === id);
  updateDrawer();
  refreshLogs(true);
  $("d-name").focus({ preventScroll: true });
}

function closeDrawer() {
  drawerId = null;
  drawer.hidden = true;
  for (const tile of tiles.values()) tile.el.classList.remove("selected");
  const trigger = drawerTrigger;
  drawerTrigger = null;
  if (trigger && trigger.isConnected) trigger.focus({ preventScroll: true });
}

function drawerService() {
  const tile = tiles.get(drawerId);
  return tile ? tile.service : null;
}

function updateDrawer() {
  const service = drawerService();
  if (!service) { closeDrawer(); return; }

  $("d-name").textContent = service.name;
  setStatusAttributes($("d-dot"), service);
  $("d-state").textContent = statusText(service);

  const key = iconKeyOf(service);
  if (drawerIconKey !== key) {
    drawerIconKey = key;
    renderIcon($("d-icon"), service);
  }

  const error = $("d-error");
  error.hidden = !service.last_error || service.state === "up";
  error.textContent = service.last_error || "";

  const identity = $("d-identity");
  identity.hidden = !service.identity_warning;
  identity.textContent = service.identity_warning ? `⚠ ${service.identity_warning}` : "";

  $("d-address").textContent = service.url || service.address;
  $("d-bookmark").textContent = `${hubUrl}/open/${service.id}`;

  const select = $("d-route");
  const routesKey = `${service.id}|${JSON.stringify(service.routes)}`;
  if (drawerRoutesKey !== routesKey) {
    drawerRoutesKey = routesKey;
    select.replaceChildren(new Option("Auto (in order)", "auto"));
    for (const route of service.routes) {
      select.add(new Option(`#${route.index + 1} ${route.label}`, String(route.index)));
    }
  }
  if (document.activeElement !== select) {
    select.value = service.pinned_route === null ? "auto" : String(service.pinned_route);
  }

  const route = activeRoute(service);
  let active = route ? route.label : "—";
  if (route && service.connections !== null) active += ` · ${service.connections} conn`;
  $("d-route-active").textContent = active;

  const toggle = $("d-toggle");
  const on = service.mode === "on";
  toggle.hidden = !on && service.state === "up"; // auto-direct: nothing to do
  toggle.textContent = on ? "Stop" : "Connect";
  toggle.title = on ? "End this connection and go back to auto" : "Connect now, with ssh if needed";
  toggle.classList.toggle("stop", on);

  $("d-refresh-icon").hidden = !service.url;
}

async function refreshLogs(scrollToEnd = false) {
  if (!drawerId) return;
  const id = drawerId;
  const response = await fetch(`/api/services/${id}/logs`);
  if (!response.ok || id !== drawerId) return;
  const { logs } = await response.json();
  const pre = $("d-log");
  const atBottom = scrollToEnd || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
  pre.textContent =
    logs.map((e) => `${new Date(e.time * 1000).toLocaleTimeString()}  ${e.message}`).join("\n") || "(empty)";
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

$("d-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && drawerId) closeDrawer();
});

$("d-toggle").addEventListener("click", async () => {
  const service = drawerService();
  if (!service) return;
  const action = service.mode === "on" ? "stop" : "start";
  try { await api(`/api/services/${service.id}/${action}`); } catch (err) { showBanner(err.message, true); }
  poll();
});

$("d-route").addEventListener("change", async (event) => {
  const value = event.target.value === "auto" ? null : Number(event.target.value);
  try { await api(`/api/services/${drawerId}/route`, { route: value }); } catch (err) { showBanner(err.message, true); }
  poll();
});

$("d-copy-address").addEventListener("click", async () => {
  const service = drawerService();
  if (service) { await copyText(service.url || service.address); toast("Address copied"); }
});

$("d-copy-bookmark").addEventListener("click", async () => {
  if (drawerId) { await copyText(`${hubUrl}/open/${drawerId}`); toast("Bookmark URL copied"); }
});

$("d-refresh-icon").addEventListener("click", async () => {
  try {
    const result = await api(`/api/services/${drawerId}/icon/refresh`);
    toast(result.ok ? "Icon updated" : "No icon found; using initials");
  } catch (err) {
    showBanner(err.message, true);
  }
  poll();
});

// ------------------------------------------------------------------ setup

/** Where the config lives; a setup guide while no service is configured. */
function renderSetup(config, empty) {
  empty = empty && !config.error; // a broken file is not "no services yet"
  configDir = config.dir;
  const localName = config.local_path ? config.local_path.split(/[\\/]/).pop() : "local file";
  $("m-reload-files").textContent = config.local_found
    ? `services.yaml + ${localName}`
    : `services.yaml (no ${localName} for this machine)`;
  $("setup").hidden = !empty;
  if (!empty) return;
  $("setup-lead").textContent = config.found
    ? "Your configuration file has no services yet."
    : "There is no configuration file yet. Create it with the example below.";
  $("setup-path").textContent = config.path;
  $("setup-init").textContent = config.init_command;
  $("setup-create").hidden = config.found;
}

/** The files cannot be used; tunhub runs with the last working config, or without services. */
function renderConfigError(config) {
  $("config-error").hidden = !config.error;
  if (!config.error) return;
  if ($("config-error-text").textContent !== config.error) {
    $("config-error-text").textContent = config.error; // keep a text selection across polls
  }
  $("config-error-note").textContent = config.error_keeps_previous
    ? "tunhub keeps using the last working configuration. Fix the file, then reload."
    : "No services are loaded until the file is fixed. Fix it, then reload.";
}

/** Problems that do not stop tunhub, such as an unknown key; gone after a fixed Reload. */
function renderWarnings(warnings) {
  $("config-warnings").hidden = warnings.length === 0;
  const key = warnings.join("\n");
  if (key === renderWarnings.shown) return; // keep a text selection across polls
  renderWarnings.shown = key;
  $("config-warnings-list").replaceChildren(
    ...warnings.map((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      return item;
    }),
  );
}

const copyButtons = {
  "setup-copy-path": ["setup-path", "Path copied"],
  "setup-copy-init": ["setup-init", "Command copied"],
  "setup-copy-example": ["setup-example", "Example copied"],
};
for (const [button, [source, message]] of Object.entries(copyButtons)) {
  $(button).addEventListener("click", async () => {
    await copyText($(source).textContent);
    toast(message);
  });
}
for (const button of document.querySelectorAll("[data-reload]")) {
  button.addEventListener("click", reloadConfig);
}

// --------------------------------------------------------------- favicon

// The tab icon gets a red dot only when something needs attention: a service
// that is on keeps failing, or tunhub itself is not reachable. A pinned tab
// then shows trouble without being opened.
const FAVICON_DOT = "#dc2626";
let faviconSvg = null;
let faviconAlert = false;
fetch("/static/icons/favicon.svg")
  .then((response) => response.text())
  .then((svg) => { faviconSvg = svg; })
  .catch(() => {});

function updateFavicon(services) {
  setFaviconAlert(services.some((s) => s.state === "retrying"));
}

function setFaviconAlert(alert) {
  if (!faviconSvg || alert === faviconAlert) return;
  faviconAlert = alert;
  // Browsers may show the PNG fallback instead of the SVG icon (Brave does),
  // and would then ignore the change below. Drop it so the SVG is used.
  document.querySelector('link[rel="alternate icon"]')?.remove();
  const dot = alert ? `<circle cx="13" cy="3" r="2.75" fill="${FAVICON_DOT}" stroke="#0a1024" stroke-width="1"/>` : "";
  $("favicon").href = `data:image/svg+xml,${encodeURIComponent(faviconSvg.replace("</svg>", `${dot}</svg>`))}`;
}

// --------------------------------------------------------------- offline

// tunhub itself is not answering (stopped, crashed, restarting). One failed
// poll is ignored so that a quick restart does not flash the warning.
let failedPolls = 0;
let offlineSince = null;

function setOffline(offline) {
  failedPolls = offline ? failedPolls + 1 : 0;
  const show = failedPolls >= 2;
  if (show && !offlineSince) offlineSince = new Date();
  if (!show) offlineSince = null;

  const banner = $("offline");
  banner.hidden = !show;
  document.body.classList.toggle("offline", show);
  if (show) {
    const since = offlineSince.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    banner.textContent = `Lost connection to tunhub at ${since}.`;
    $("summary").textContent = "offline";
    setFaviconAlert(true);
  }
}

// ------------------------------------------------------------------ poll

async function poll() {
  let data;
  try {
    const response = await fetch("/api/state");
    data = await response.json();
  } catch {
    setOffline(true);
    return;
  }
  setOffline(false);
  hubUrl = data.hub;
  renderSetup(data.config, data.services.length === 0);
  renderConfigError(data.config);
  renderWarnings(data.config.warnings);
  $("machine").textContent = `on ${data.machine}`;
  $("machine").title = `This machine's overrides: local.${data.machine}.yaml`;
  $("version").textContent = `tunhub ${data.version}`; // the running server's version

  const seen = new Set();
  data.services.forEach((service, position) => {
    seen.add(service.id);
    const tile = tiles.get(service.id) || createTile(service);
    updateTile(tile, service);
    if (container.children[position] !== tile.el) {
      container.insertBefore(tile.el, container.children[position] || null);
    }
  });
  for (const [id, tile] of tiles) {
    if (!seen.has(id)) { tile.el.remove(); tiles.delete(id); }
  }

  const upCount = data.services.filter((s) => s.state === "up").length;
  $("summary").textContent = `${upCount}/${data.services.length} connected`;
  updateFavicon(data.services);

  const sshCount = data.services.filter((s) => s.uses_ssh).length;
  const disconnect = $("disconnect-ssh");
  disconnect.disabled = sshCount === 0;
  disconnect.textContent = sshCount ? `Disconnect SSH (${sshCount})` : "Disconnect SSH";

  if (drawerId) {
    updateDrawer();
    refreshLogs();
  }
  refreshEvents();
}

$("disconnect-ssh").addEventListener("click", async () => {
  try {
    const result = await api("/api/disconnect-ssh");
    toast(result.stopped.length ? `Disconnected: ${result.stopped.join(", ")}` : "No ssh connections");
  } catch (err) {
    showBanner(err.message, true);
  }
  poll();
});

async function reloadConfig() {
  try {
    const result = await api("/api/reload");
    showBanner(`Config reloaded: ${result.messages.join(", ")}`);
  } catch (err) {
    // A rejected config shows up in the error box (details there); anything
    // else, e.g. tunhub not running, has no box to look at.
    toast(err instanceof TypeError ? `Reload failed: ${err.message}` : "Reload failed: see the config error");
  }
  poll();
}
// ------------------------------------------------------------------- menu

const menu = $("menu");
const menuButton = $("menu-button");

function setMenuOpen(open) {
  menu.hidden = !open;
  menuButton.setAttribute("aria-expanded", String(open));
  if (open) menu.querySelector("button").focus();
}

menuButton.addEventListener("click", () => setMenuOpen(menu.hidden));
// Any choice closes the menu; clicks elsewhere close it too.
menu.addEventListener("click", () => setMenuOpen(false));
document.addEventListener("click", (event) => {
  if (!menu.hidden && !event.target.closest(".menu-wrap")) setMenuOpen(false);
});
menu.addEventListener("keydown", (event) => {
  const items = [...menu.querySelectorAll("button")];
  const index = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    items[(index + step + items.length) % items.length].focus();
  } else if (event.key === "Escape") {
    setMenuOpen(false);
    menuButton.focus();
  }
});

$("m-copy-path").addEventListener("click", async () => {
  await copyText(configDir);
  toast("Config path copied");
});
$("m-events").addEventListener("click", openEvents);

// ----------------------------------------------------------------- events

function openEvents() {
  if (drawerId) closeDrawer();
  eventsPanel.hidden = false;
  refreshEvents(true);
  $("e-title").focus({ preventScroll: true });
}

function closeEvents() {
  eventsPanel.hidden = true;
  menuButton.focus({ preventScroll: true });
}

function timeOf(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString();
}

function networkText(data) {
  if (data.network_since === null) return "Not checked yet";
  const since = `since ${timeOf(data.network_since)}`;
  if (!data.network) return `Unknown (no gateway found), ${since}`;
  return `Gateway ${data.network.gateway} on ${data.network.subnet}, ${since}`;
}

async function refreshEvents(scrollToEnd = false) {
  if (eventsPanel.hidden) return;
  let data;
  try {
    const response = await fetch("/api/events");
    data = await response.json();
  } catch {
    return; // the offline banner already says so
  }
  $("e-network").textContent = networkText(data);
  const pre = $("e-log");
  const atBottom = scrollToEnd || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
  const key = data.events.length ? `${data.events.length}:${data.events.at(-1).time}` : "";
  if (key !== refreshEvents.shown) {
    refreshEvents.shown = key; // keep a text selection while nothing changes
    pre.replaceChildren(
      ...data.events.map((e) => {
        const line = document.createElement("div");
        line.className = e.level;
        line.textContent = `${timeOf(e.time)}  ${e.message}`;
        return line;
      }),
    );
    if (!data.events.length) pre.textContent = "(empty)";
  }
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

$("e-close").addEventListener("click", closeEvents);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !eventsPanel.hidden && menu.hidden) closeEvents();
});

let pollTimer = null;

function schedulePoll() {
  clearTimeout(pollTimer);
  const delay = document.hidden ? HIDDEN_POLL_MS : POLL_MS;
  pollTimer = setTimeout(async () => {
    try {
      await poll();
    } finally {
      schedulePoll(); // keep polling even if one update failed
    }
  }, delay);
}

// Back in view: refresh at once instead of showing a stale state.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
  schedulePoll();
});

poll();
schedulePoll();

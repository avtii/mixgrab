"use strict";
// mixgrab web UI: no framework, talks to the JSON API, polls while a search is running.

const $ = (s, el = document) => el.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "text") n.textContent = v;
    else if (k === "style") n.style.cssText = v;   // CSSOM: allowed under the strict CSP
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) n.append(c.nodeType ? c : document.createTextNode(c));
  return n;
};
const icon = (name) => el("span", { class: `i i-${name}` });

async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (r.status === 401 && !opts.noAuthRedirect) { showLogin(); throw new Error("login required"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

const fmtTs = (s) => {
  if (s == null) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${x}` : `${m}:${x}`;
};
const fmtBytes = (b) => !b ? "0 MB" : b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : (b / 1e6).toFixed(b >= 1e8 ? 0 : 1) + " MB";
const ago = (t) => {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return new Date(t * 1000).toLocaleDateString();
};
const ytId = (u) => (u.match(/(?:v=|youtu\.be\/|shorts\/|live\/)([\w-]{11})/) || [])[1];
const ACTIVE = new Set(["queued", "reading", "identifying", "downloading"]);
const JOB_LABEL = { queued: "Queued", reading: "Reading tracklist", identifying: "Identifying from audio",
  downloading: "Fetching tracks", done: "Done", error: "Failed", interrupted: "Interrupted" };
const JOB_PILL = { queued: "run", reading: "run", identifying: "run", downloading: "run", done: "ok", error: "bad", interrupted: "warn" };
const TRACK_TAG = {
  pending: ["Waiting", "wait"], searching: ["Searching", "run"], waiting: ["Found", "run"], queued: ["In peer queue", "warn"],
  downloading: ["Downloading", "run"], retry: ["Retrying", "warn"], youtube: ["Trying YouTube", "run"],
  converting: ["To MP3", "run"], done: ["Done", "ok"], missing: ["Not found", "bad"], expired: ["Expired", "wait"],
};

// ---------------- state ----------------
const state = { jobId: null, job: null, pollTimer: null, histTimer: null, editing: null };

// ---------------- login ----------------
function showLogin() {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  stopPolling();
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#login-err").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") }, noAuthRedirect: true });
    e.target.reset();
    boot();
  } catch (err) { $("#login-err").textContent = err.message; }
});
$("#logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }).catch(() => {}); showLogin(); });

// ---------------- sidebar ----------------
async function refreshMe() {
  try {
    const me = await api("/api/me");
    const s = me.soulseek.state;
    $("#slsk .dot").className = "dot " + (s === "online" ? "ok" : s === "offline" ? "bad" : "warn");
    $("#slsk span:last-child").textContent = s === "online" ? `Soulseek online` : `Soulseek ${s}`;
    $("#slsk").title = me.soulseek.error || me.soulseek.username || "";
    const c = me.cache, used = c.files + c.zips;
    $("#cache i").style.width = Math.min(100, (used / c.budget) * 100).toFixed(1) + "%";
    $("#cache span").textContent = `Cache ${fmtBytes(used)} of ${fmtBytes(c.budget)}`;
    const yc = me.youtube_cookies;
    $("#ytc .dot").className = "dot " + (yc ? "ok" : "warn");
    $("#ytc span:last-child").textContent = yc
      ? `YouTube cookies · ${new Date(yc * 1000).toLocaleDateString()}` : "Add YouTube cookies";
  } catch {}
}

$("#ytc").addEventListener("click", () => $("#ytc-file").click());
$("#ytc-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  try {
    const r = await fetch("/api/youtube-cookies", { method: "POST", body: await f.text(),
      headers: { "Content-Type": "text/plain" }, credentials: "same-origin" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || r.statusText);
    toast(`YouTube cookies saved (${d.cookies}). Press Resume on failed jobs.`);
    refreshMe();
  } catch (err) { toast(err.message); }
});

async function refreshHistory() {
  let jobs;
  try { jobs = await api("/api/jobs"); } catch { return; }
  const nav = $("#history");
  nav.replaceChildren(...jobs.map((j) => el("a", {
    class: j.id === state.jobId ? "active" : null, href: `#/job/${j.id}`,
    onclick: () => $(".side").classList.remove("open"),
  },
    el("span", { class: "h-title", text: j.title, title: j.title }),
    el("span", { class: "h-sub" },
      el("span", { class: "dot " + (JOB_PILL[j.status] === "ok" ? "ok" : JOB_PILL[j.status] === "bad" ? "bad" : JOB_PILL[j.status] === "warn" ? "warn" : "") }),
      ACTIVE.has(j.status) ? JOB_LABEL[j.status] : `${j.done}/${j.tracks} tracks`, " · ", ago(j.created)))));
  if (!jobs.length) nav.append(el("p", { class: "muted small", text: "Nothing yet." }));
}

$("#new-btn").addEventListener("click", () => { location.hash = "#/"; $(".side").classList.remove("open"); });
$("#menu").addEventListener("click", () => $(".side").classList.toggle("open"));

// ---------------- new search ----------------
$("#new-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#new-err").textContent = "";
  const url = $("#url").value.trim();
  const identify = new FormData(e.target).get("identify");
  const btn = e.target.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    const job = await api("/api/jobs", { method: "POST", body: { url, identify } });
    $("#url").value = "";
    location.hash = `#/job/${job.id}`;
    refreshHistory();
  } catch (err) { $("#new-err").textContent = err.message; }
  finally { btn.disabled = false; }
});

// ---------------- job view ----------------
function stopPolling() { clearTimeout(state.pollTimer); state.pollTimer = null; }

async function loadJob(id, { quiet = false } = {}) {
  stopPolling();
  if (id !== state.jobId) state.editing = null;
  state.jobId = id;
  let job;
  try { job = await api(`/api/jobs/${id}`); }
  catch (err) { if (!quiet) toast(err.message); location.hash = "#/"; return; }
  if (state.jobId !== id) return;
  state.job = job;
  renderJob(job);
  const busy = ACTIVE.has(job.status) || job.tracks.some((t) => !["done", "missing", "expired", "pending"].includes(t.state));
  state.pollTimer = setTimeout(() => loadJob(id, { quiet: true }), busy ? 1000 : 15000);
}

function renderJob(job) {
  const vid = ytId(job.url);
  const thumb = $("#job-thumb");
  if (vid) { thumb.src = `https://i.ytimg.com/vi/${vid}/mqdefault.jpg`; thumb.classList.remove("hidden"); }
  else thumb.classList.add("hidden");
  const pill = $("#job-status");
  pill.className = "pill " + (JOB_PILL[job.status] || "");
  pill.textContent = JOB_LABEL[job.status] || job.status;
  $("#job-title").textContent = job.title;
  const a = $("#job-url");
  a.href = job.url; a.textContent = job.url.replace(/^https?:\/\/(www\.)?/, "").slice(0, 48);
  $("#job-source").textContent = job.found_in ? `tracklist: ${job.found_in}` : (job.error || "");

  const zip = $("#zip-btn");
  if (job.zip_url) { zip.href = job.zip_url; zip.classList.remove("hidden"); } else zip.classList.add("hidden");
  $("#resume-btn").classList.toggle("hidden", !(job.status === "interrupted" || job.status === "error" || (job.status === "done" && job.tracks.some((t) => t.state === "expired"))));
  $("#del-btn").disabled = ACTIVE.has(job.status);

  // phase progress
  const ph = $("#phase");
  const p = job.progress || {};
  let label = "", info = "", frac = null;
  if (job.status === "reading" || job.status === "queued") { label = JOB_LABEL[job.status]; info = "chapters, description, comments, MixesDB, MusicBrainz"; }
  else if (job.status === "identifying") {
    if (p.clips) { const [d, t, m] = p.clips; label = "Listening to the mix"; info = `${d}/${t} clips · ${m} matches`; frac = t ? d / t : 0; }
    else if (p.audio) { const [d, t] = p.audio; label = "Downloading the mix audio"; info = t ? `${fmtBytes(d)} / ${fmtBytes(t)}` : fmtBytes(d); frac = t ? d / t : null; }
    else { label = "Identifying from audio"; info = job.note || ""; }
  } else if (job.status === "downloading") {
    const n = job.tracks.length, fin = job.tracks.filter((t) => ["done", "missing", "expired"].includes(t.state)).length;
    label = "Fetching tracks"; info = job.note || `${fin}/${n} finished`; frac = n ? fin / n : 0;
  }
  ph.classList.toggle("hidden", !label);
  if (label) {
    $("#phase-label").textContent = label;
    $("#phase-info").textContent = info;
    const bar = $("#phase-bar");
    bar.parentElement.classList.toggle("indet", frac == null);
    if (frac != null) bar.style.width = (frac * 100).toFixed(1) + "%";
  }

  // summary
  const done = job.tracks.filter((t) => t.state === "done");
  const yt = done.filter((t) => t.origin === "youtube").length;
  const size = done.reduce((s, t) => s + (t.size || 0), 0);
  const sum = [];
  if (job.tracks.length) sum.push(`${done.length} of ${job.tracks.length} tracks`, fmtBytes(size));
  if (yt) sum.push(`${yt} from YouTube`);
  const miss = job.tracks.filter((t) => t.state === "missing").length;
  if (miss) sum.push(`${miss} not found`);
  if (job.status === "error" && job.error) sum.push(job.error);
  $("#summary").textContent = sum.join(" · ");

  if (state.editing == null) $("#tracks").replaceChildren(...job.tracks.map((t) => trackRow(job, t)));

  const maybe = $("#maybe");
  maybe.classList.toggle("hidden", !(job.maybe || []).length);
  $("#maybe-list").replaceChildren(...(job.maybe || []).map((h) =>
    el("li", {}, `~${fmtTs(h.at)}  ${h.artist} – ${h.title} `,
      el("button", { class: "btn link", text: "fetch", onclick: () => addTrack(h.artist, h.title) }))));
}

function trackRow(job, t) {
  const [tagText, tagCls] = TRACK_TAG[t.state] || [t.state, "wait"];
  const pct = t.state === "done" ? 100 : t.total ? Math.min(100, (t.done / t.total) * 100) : 0;
  const running = ["searching", "waiting", "queued", "youtube", "converting"].includes(t.state) || (t.state === "downloading" && !t.total);
  const fmts = el("span", { class: "fmt-row" });
  if (t.ext && t.state === "done") {
    fmts.append(el("span", { class: "fmt", text: t.ext }));
    if (t.origin === "youtube") fmts.append(el("span", { class: "fmt yt", text: "YT", title: "Not on Soulseek — taken from YouTube" }));
    if (t.mp3_url) fmts.append(el("a", { class: "fmt", href: t.mp3_url, text: "mp3", title: "Download the MP3 copy", download: true }));
  }
  const acts = el("div", { class: "acts" });
  if (t.url) {
    acts.append(el("button", { class: "btn icon", title: "Play", onclick: () => play(t) }, icon("play")));
    acts.append(el("a", { class: "btn icon", href: t.url, title: `Download ${t.ext || ""}`, download: true }, icon("down")));
  }
  if (!ACTIVE.has(t.state) && ["done", "missing", "expired", "pending"].includes(t.state)) {
    acts.append(el("button", { class: "btn icon", title: "Edit artist/title and search again", onclick: () => editRow(job, t) }, icon("edit")));
    acts.append(el("button", { class: "btn icon", title: "Search again", onclick: () => retry(t.index) }, icon("retry")));
  }
  return el("li", { class: "tr" },
    el("span", { class: "num", text: t.index }),
    el("span", { class: "ts", text: fmtTs(t.start) }),
    el("div", { class: "name" },
      el("b", {}, el("span", { class: "t", text: t.title, title: t.title }), fmts),
      el("span", { text: t.artist, title: t.source || "" })),
    el("div", { class: "st" },
      el("div", { class: "st-top" }, el("span", { class: `tag ${tagCls}`, text: tagText }),
        el("span", { class: "note", text: t.note || (t.state === "done" && t.size ? fmtBytes(t.size) : ""), title: t.note || t.source || "" })),
      el("div", { class: "bar" + (running ? " indet" : "") }, el("i", { style: `width:${pct.toFixed(1)}%` }))),
    acts);
}

function editRow(job, t) {
  state.editing = t.index;
  const row = [...$("#tracks").children][job.tracks.indexOf(t)];
  const form = el("form", { class: "edit-row" },
    el("input", { name: "artist", value: t.artist, placeholder: "Artist", required: true }),
    el("input", { name: "title", value: t.title, placeholder: "Title", required: true }),
    el("button", { class: "btn primary", type: "submit", text: "Search again" }),
    el("button", { class: "btn ghost", type: "button", text: "Cancel", onclick: () => { state.editing = null; renderJob(state.job); } }));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    state.editing = null;
    await retry(t.index, f.get("artist"), f.get("title"));
  });
  row.className = "tr editing";
  row.replaceChildren(el("span", { class: "num", text: t.index }), form);
  form.querySelector("input").focus();
}

async function retry(index, artist, title) {
  try {
    await api(`/api/jobs/${state.jobId}/tracks/${index}/retry`, { method: "POST", body: { artist: artist || null, title: title || null } });
    loadJob(state.jobId, { quiet: true });
  } catch (err) { toast(err.message); }
}

async function addTrack(artist, title) {
  try {
    await api(`/api/jobs/${state.jobId}/tracks`, { method: "POST", body: { artist, title } });
    toast("Added — searching");
    loadJob(state.jobId, { quiet: true });
  } catch (err) { toast(err.message); }
}

$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  await addTrack(f.get("artist").trim(), f.get("title").trim());
  e.target.reset();
});

$("#copy-btn").addEventListener("click", async () => {
  try {
    const txt = await api(`/api/jobs/${state.jobId}/tracklist.txt`);
    await navigator.clipboard.writeText(txt);
    toast("Tracklist copied");
  } catch (err) { toast(err.message || "Copy failed"); }
});
$("#resume-btn").addEventListener("click", async () => {
  try { await api(`/api/jobs/${state.jobId}/resume`, { method: "POST" }); loadJob(state.jobId); refreshHistory(); }
  catch (err) { toast(err.message); }
});
$("#del-btn").addEventListener("click", async () => {
  if (!confirm("Delete this search and its files?")) return;
  try { await api(`/api/jobs/${state.jobId}`, { method: "DELETE" }); location.hash = "#/"; refreshHistory(); }
  catch (err) { toast(err.message); }
});

// ---------------- player ----------------
function play(t) {
  const audio = $("#audio");
  audio.src = t.url + "&inline=1";
  $("#pl-title").textContent = t.title;
  $("#pl-artist").textContent = t.artist;
  $("#player").classList.remove("hidden");
  audio.play().catch(() => {});
}
$("#pl-close").addEventListener("click", () => { const a = $("#audio"); a.pause(); a.removeAttribute("src"); $("#player").classList.add("hidden"); });

// ---------------- routing ----------------
function route() {
  const m = location.hash.match(/^#\/job\/([\w-]+)/);
  if (m) {
    $("#home").classList.add("hidden");
    $("#job").classList.remove("hidden");
    loadJob(m[1]);
  } else {
    stopPolling();
    state.jobId = null;
    $("#job").classList.add("hidden");
    $("#home").classList.remove("hidden");
    $("#url").focus();
  }
  refreshHistory();
}
window.addEventListener("hashchange", route);

async function boot() {
  try { await api("/api/me", { noAuthRedirect: true }); }
  catch { showLogin(); return; }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  refreshMe();
  route();
  clearInterval(state.histTimer);
  state.histTimer = setInterval(() => { refreshHistory(); refreshMe(); }, 10000);
}
boot();

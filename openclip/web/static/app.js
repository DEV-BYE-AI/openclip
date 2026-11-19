"use strict";
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
const scoreColor = (s) => (s >= 80 ? "var(--lime)" : s >= 60 ? "var(--amber)" : "var(--muted)");

let META = { accounts: {}, styles: {}, presets: {} };
let pollTimer = null;
let lastJobs = [];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (t.hidden = true), 3500);
}

/* ------------------------------------------------------------------ préférences */
const PREF_KEY = "openclip.prefs";
const defaults = { preset: "shorts", num_clips: 5, crop_mode: "face", caption_style: "pop", jump_cuts: true, progress_bar: true, language: "", auto_publish: [] };
function loadPrefs() {
  try { return { ...defaults, ...JSON.parse(localStorage.getItem(PREF_KEY) || "{}") }; } catch { return { ...defaults }; }
}
function savePrefs(p) { try { localStorage.setItem(PREF_KEY, JSON.stringify(p)); } catch {} }

/* ------------------------------------------------------------------ routeur */
async function route() {
  const hash = location.hash || "#/";
  if (!hash.startsWith("#/youtube") && !hash.startsWith("#/connect/") && window.__rfb) { try { window.__rfb.disconnect(); } catch {} window.__rfb = null; }
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.remove("active"));
  clearInterval(pollTimer);
  if (hash.startsWith("#/p/")) return renderProject(decodeURIComponent(hash.slice(4)));
  if (hash === "#/projets") { $('[data-nav="projets"]').classList.add("active"); return renderProjects(); }
  if (hash === "#/comptes") { $('[data-nav="comptes"]').classList.add("active"); return renderAccounts(); }
  if (hash === "#/auto") { $('[data-nav="auto"]').classList.add("active"); return renderAuto(); }
  if (hash === "#/youtube") { $('[data-nav="comptes"]').classList.add("active"); return renderYoutubeLogin(); }
  if (hash === "#/connect/tiktok" || hash === "#/connect/youtube") { $('[data-nav="comptes"]').classList.add("active"); return renderYoutubeLogin(hash.slice(10)); }
  $('[data-nav="home"]').classList.add("active");
  return renderHome();
}
window.addEventListener("hashchange", route);

/* ------------------------------------------------------------------ accueil */
const STYLE_DEMOS = {
  pop: `<div class="demo demo-pop">ÇA A <b>TOUT</b> <i>CHANGÉ</i></div>`,
  boxed: `<div class="demo demo-boxed"><span>ÇA A <b>TOUT</b> CHANGÉ</span></div>`,
  neon: `<div class="demo demo-neon">ÇA A <b>TOUT</b> CHANGÉ</div>`,
  karaoke: `<div class="demo demo-karaoke">ÇA A <b>TOUT</b> <s>CHANGÉ</s></div>`,
  minimal: `<div class="demo demo-minimal">Ça a <b>tout</b> changé</div>`,
};
const PRESET_INFO = {
  shorts: ["30–60 s", "Shorts"],
  rewards: ["62–95 s", "TikTok Rewards"],
  long: ["90–170 s", "Histoire"],
};
const STAGES = [["download", "Téléchargement"], ["transcribe", "Transcription"], ["analyze", "Analyse"], ["render", "Montage"]];

function chips(name, options, current) {
  return `<div class="chips" data-group="${name}">${options.map(([v, label, small]) =>
    `<button type="button" class="chip" data-v="${esc(v)}" aria-pressed="${String(v) === String(current)}">${esc(label)}${small ? `<small>${esc(small)}</small>` : ""}</button>`).join("")}</div>`;
}

function renderHome() {
  const p = loadPrefs();
  const styles = Object.entries(META.styles);
  view.innerHTML = `
    <section class="hero">
      <h1>Une vidéo longue.<br><em>Des clips qui retiennent.</em></h1>
      <p>Colle un lien YouTube : OpenClip repère les passages les plus forts, les monte en vertical avec sous-titres animés, et te dit pourquoi ils vont marcher.</p>
    </section>
    <form class="launcher" id="launch">
      <div class="url-row">
        <input id="url" type="url" required placeholder="https://www.youtube.com/watch?v=…" autocomplete="off">
        <button class="btn btn-primary" id="go">Trouver les clips →</button>
      </div>
      <div class="file-row">
        <span class="muted">ou</span>
        <label class="btn btn-sm btn-ghost">📁 Envoyer un fichier vidéo<input type="file" id="file" accept="video/*" hidden></label>
        <span id="up-status" class="muted"></span>
      </div>
      <div class="options">
        <div>
          <div class="opt-label">Durée</div>
          ${chips("preset", Object.keys(PRESET_INFO).map((k) => [k, PRESET_INFO[k][0], PRESET_INFO[k][1]]), p.preset)}
          <div class="tip" id="preset-tip"></div>
        </div>
        <div>
          <div class="opt-label">Nombre de clips</div>
          ${chips("num_clips", [[3, "3"], [5, "5"], [8, "8"], [12, "12"]], p.num_clips)}
        </div>
        <div>
          <div class="opt-label">Langue de la vidéo</div>
          ${chips("language", [["", "Auto"], ["fr", "Français"], ["en", "Anglais"], ["es", "Espagnol"]], p.language || "")}
        </div>
        <div>
          <div class="opt-label">Cadrage</div>
          ${chips("crop_mode", [["face", "Intelligent"], ["center", "Centré"], ["blur", "Fond flou"]], p.crop_mode)}
        </div>
        <div class="opt-wide">
          <div class="opt-label">Style des sous-titres</div>
          <div class="styles" data-group="caption_style">
            ${styles.map(([k, label]) => `<button type="button" class="style-card" data-v="${k}" aria-pressed="${k === p.caption_style}" title="${esc(label)}">${STYLE_DEMOS[k] || ""}<span class="name">${esc(label.split(" — ")[0])}</span></button>`).join("")}
          </div>
        </div>
        <div class="opt-wide">
          <div class="opt-label">Montage</div>
          <div class="switches">
            <label class="switch"><input type="checkbox" name="jump_cuts" ${p.jump_cuts ? "checked" : ""}> Couper les blancs</label>
            <label class="switch"><input type="checkbox" name="progress_bar" ${p.progress_bar ? "checked" : ""}> Barre de progression</label>
            <label class="switch"><input type="checkbox" name="pub_tiktok" ${p.auto_publish.includes("tiktok") ? "checked" : ""} ${META.accounts.tiktok ? "" : "disabled"}> Publier sur TikTok</label>
            <label class="switch"><input type="checkbox" name="pub_youtube" ${p.auto_publish.includes("youtube") ? "checked" : ""} ${META.accounts.youtube ? "" : "disabled"}> Publier sur YouTube</label>
          </div>
          <div class="tip">${META.accounts.claude ? "Analyse : <b>Claude</b> (clé trouvée)." : "Analyse <b>locale et gratuite</b> : accroche, rythme, énergie de la voix, émotions et chiffres."}</div>
        </div>
      </div>
    </form>
    <div id="jobs-wrap"></div>
    <div class="section-title"><h2>Derniers projets</h2><a class="muted" href="#/projets">Tout voir →</a></div>
    <div id="recent" class="projects"></div>`;

  const form = $("#launch");
  const tip = () => {
    const t = { shorts: "Le format le plus regardé jusqu'au bout. Idéal pour gagner des abonnés et pour YouTube Shorts.",
                rewards: "<b>Plus d'1 minute</b> : seule durée qui compte pour le programme Creator Rewards de TikTok.",
                long: "Pour les histoires complètes. YouTube Shorts accepte jusqu'à 3 minutes." }[p.preset];
    $("#preset-tip").innerHTML = t;
  };
  tip();
  form.addEventListener("click", (e) => {
    const b = e.target.closest("[data-v]");
    if (!b) return;
    const group = b.parentElement.dataset.group;
    b.parentElement.querySelectorAll("[data-v]").forEach((x) => x.setAttribute("aria-pressed", x === b));
    p[group] = group === "num_clips" ? Number(b.dataset.v) : b.dataset.v;
    savePrefs(p);
    if (group === "preset") tip();
  });
  form.addEventListener("change", (e) => {
    const el = e.target;
    if (el.name === "jump_cuts" || el.name === "progress_bar") p[el.name] = el.checked;
    if (el.name?.startsWith("pub_")) {
      const t = el.name.slice(4);
      p.auto_publish = el.checked ? [...new Set([...p.auto_publish, t])] : p.auto_publish.filter((x) => x !== t);
    }
    savePrefs(p);
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = $("#url").value.trim();
    $("#go").disabled = true;
    try {
      const auto = p.auto_publish.filter((t) => META.accounts[t]);
      await api("/api/jobs", { method: "POST", body: { ...p, url, auto_publish: auto, language: p.language || null } });
      $("#url").value = "";
      toast("C'est parti ! Tu peux suivre l'avancement ici.");
      refreshJobs();
    } catch (err) { toast(err.message, true); }
    $("#go").disabled = false;
  });

  $("#file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const status = $("#up-status");
    try {
      const up = await uploadFile(file, (pct) => (status.textContent = `Envoi… ${pct}%`));
      status.textContent = "";
      await api("/api/jobs", { method: "POST", body: { ...p, url: "upload:" + up.id, auto_publish: p.auto_publish.filter((t) => META.accounts[t]), language: p.language || null } });
      toast("Fichier reçu, c'est parti !");
      refreshJobs();
    } catch (err) { status.textContent = ""; toast(err.message, true); }
    e.target.value = "";
  });

  refreshJobs();
  pollTimer = setInterval(refreshJobs, 1500);
  loadRecent();
}

function uploadFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch {}
      xhr.status < 300 ? resolve(data) : reject(new Error(data.detail || "Échec de l'envoi"));
    };
    xhr.onerror = () => reject(new Error("Connexion perdue pendant l'envoi"));
    xhr.send(file);
  });
}

async function loadRecent() {
  const list = await api("/api/projects");
  const el = $("#recent");
  if (!el) return;
  el.innerHTML = list.length ? list.slice(0, 6).map(projectCard).join("") : `<div class="empty" style="grid-column:1/-1">Tes projets apparaîtront ici.</div>`;
}

async function refreshJobs() {
  const wrap = $("#jobs-wrap");
  if (!wrap) return;
  const jobs = (await api("/api/jobs")).filter((j) => j.kind === "create" && (j.stage !== "done" || Date.now() / 1000 - j.created_at < 3600));
  const finishedNow = jobs.filter((j) => j.stage === "done" && lastJobs.find((o) => o.id === j.id && o.stage !== "done"));
  if (finishedNow.length) { toast("Tes clips sont prêts 🎉"); loadRecent(); }
  lastJobs = jobs;
  if (!jobs.length) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = `<div class="section-title"><h2>En cours</h2></div><div class="jobs">${jobs.map(jobCard).join("")}</div>`;
}

function jobCard(j) {
  const idx = STAGES.findIndex(([k]) => k === j.stage);
  const done = j.stage === "done" || j.stage === "publish";
  return `<div class="job ${j.stage === "error" ? "error" : ""}">
    <div class="job-head"><span>${esc(j.message)}</span>
      ${j.project && done ? `<a class="btn btn-sm" href="#/p/${encodeURIComponent(j.project)}">Voir les clips</a>` : `<span class="muted">${Math.round(j.progress * 100)}%</span>`}</div>
    <div class="muted" style="font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${j.requested ? `${j.requested} clips demandés · ` : ""}${esc(j.url)}</div>
    <div class="steps">${STAGES.map(([k, label], i) => `<div class="step ${done || i <= idx ? "on" : ""} ${i === idx && !done ? "now" : ""}">${label}</div>`).join("")}</div>
    ${j.error ? `<div class="err">${esc(j.error)}</div>` : ""}
  </div>`;
}

/* ------------------------------------------------------------------ projets */
function projectCard(p) {
  return `<a class="project" href="#/p/${encodeURIComponent(p.id)}">
    ${p.cover ? `<img src="${esc(p.cover)}" alt="" loading="lazy">` : `<div class="ph"></div>`}
    <div><h3>${esc(p.title)}</h3>
      <div class="meta"><span>${p.count} clips</span><span>meilleur score ${Math.round(p.best)}</span>${p.published ? `<span style="color:var(--lime)">${p.published} publiés</span>` : ""}</div>
      <div class="meta">${esc(p.uploader || "")}</div></div>
  </a>`;
}

async function renderProjects() {
  view.innerHTML = `<h1 style="font-size:34px">Projets</h1><div id="all" class="projects" style="margin-top:24px"></div>`;
  const list = await api("/api/projects");
  $("#all").innerHTML = list.length ? list.map(projectCard).join("") : `<div class="empty" style="grid-column:1/-1">Aucun projet pour l'instant. <a href="#/">Crée le premier →</a></div>`;
}

/* ------------------------------------------------------------------ page projet */
let currentProject = null;
let sortMode = "score";
let pendingRenders = new Set();

async function renderProject(pid) {
  let m;
  try { m = await api(`/api/projects/${encodeURIComponent(pid)}`); } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  currentProject = m;
  const clips = m.clips.map((c, i) => ({ ...c, n: i + 1 }));
  const avg = clips.reduce((s, c) => s + (c.score || 0), 0) / (clips.length || 1);
  const published = clips.filter((c) => Object.keys(c.uploads || {}).length).length;
  view.innerHTML = `
    <a href="#/projets" class="muted" style="text-decoration:none">← Projets</a>
    <div class="proj-head" style="margin-top:12px">
      <div>
        <h1>${esc(m.source.title)}</h1>
        <div class="stats">
          <div class="stat"><b>${clips.length}</b>clips</div>
          <div class="stat"><b>${Math.round(avg)}</b>score moyen</div>
          <div class="stat"><b>${published}</b>publiés</div>
          ${m.source.url ? `<div class="stat"><a href="${esc(m.source.url)}" target="_blank" rel="noopener">Vidéo source ↗</a></div>` : ""}
          <button class="stat" id="to-queue" style="cursor:pointer">⏱ Ajouter à la file auto</button>
          <button class="stat" id="more" style="cursor:pointer;color:var(--text)">＋ D'autres clips</button>
          <button class="stat" id="del-project" style="cursor:pointer;color:var(--danger)">Supprimer le projet</button>
        </div>
      </div>
    </div>
    <div class="toolbar">
      ${chips("sort", [["score", "Meilleurs d'abord"], ["time", "Ordre de la vidéo"], ["todo", "Pas encore publiés"]], sortMode)}
    </div>
    <div class="clips" id="clips"></div>`;

  $("#del-project").onclick = async () => {
    if (!confirm("Supprimer ce projet, ses clips et son analyse ? (relancer le lien refera tout à neuf)")) return;
    try { await api(`/api/projects/${encodeURIComponent(m.id)}`, { method: "DELETE" }); toast("Projet supprimé"); location.hash = "#/projets"; }
    catch (err) { toast(err.message, true); }
  };
  $("#more").onclick = async () => {
    const prefs = loadPrefs();
    const n = Number(prompt("Combien de nouveaux clips ? (d'autres moments que ceux déjà montés)", prefs.num_clips || 5));
    if (!n) return;
    try {
      await api(`/api/projects/${encodeURIComponent(m.id)}/more`, { method: "POST", body: { ...prefs, num_clips: n, auto_publish: [] } });
      toast("C'est parti ! Suis l'avancement dans « Créer »");
      location.hash = "#/";
    } catch (err) { toast(err.message, true); }
  };
  $("#to-queue").onclick = async () => {
    try { const r = await api(`/api/automation/queue?profile=${encodeURIComponent(currentProfile())}`, { method: "POST", body: { project: m.id } }); toast(r.added ? `${r.added} clip(s) ajouté(s) à la file automatique` : "Déjà dans la file"); }
    catch (err) { toast(err.message, true); }
  };
  $(".toolbar").addEventListener("click", (e) => {
    const b = e.target.closest("[data-v]");
    if (!b) return;
    sortMode = b.dataset.v;
    renderProject(pid);
  });

  let list = [...clips];
  if (sortMode === "score") list.sort((a, b) => b.score - a.score);
  if (sortMode === "time") list.sort((a, b) => a.start - b.start);
  if (sortMode === "todo") list = list.filter((c) => !Object.keys(c.uploads || {}).length);
  const ranks = [...clips].sort((a, b) => b.score - a.score).map((c) => c.n);
  $("#clips").innerHTML = list.map((c) => clipCard(m.id, c, ranks.indexOf(c.n) + 1)).join("") || `<div class="empty" style="grid-column:1/-1">Tout est publié 👏</div>`;
  $("#clips").onclick = (e) => onClipAction(e, m);
  pollTimer = setInterval(() => watchProjectJobs(pid), 2000);
}

function clipCard(pid, c, rank) {
  const stem = c.file.replace(/\.mp4$/, "");
  const v = c.version || 1;
  const bar = (label, val) => `<div class="bar">${label}<i style="--w:${Math.round(val || 0)}%;--c:${scoreColor(val)}"></i><em>${Math.round(val || 0)}</em></div>`;
  const up = c.uploads || {};
  const hasBreakdown = c.hook_score || c.retention_score || c.share_score;
  return `<article class="clip" data-n="${c.n}">
    <div class="player">
      <video src="/media/${encodeURIComponent(pid)}/${esc(c.file)}?v=${v}" poster="/media/${encodeURIComponent(pid)}/${esc(stem)}.jpg?v=${v}" controls preload="none" playsinline></video>
      <div class="badge-row">
        <div class="score" style="--p:${Math.round(c.score)};--c:${scoreColor(c.score)}"><span>${Math.round(c.score)}</span></div>
        <span class="pill">${fmtTime(c.duration || c.end - c.start)}</span>
      </div>
      ${rank <= 3 ? `<span class="rank">#${rank}</span>` : ""}
      ${pendingRenders.has(c.n) ? `<div class="rendering">Nouveau montage…</div>` : ""}
    </div>
    <div class="clip-body">
      <h3>${esc(c.title)}</h3>
      ${c.hook ? `<div class="hook">Accroche : <b>${esc(c.hook)}</b></div>` : ""}
      ${c.reason ? `<div class="why">${esc(c.reason)}</div>` : ""}
      ${hasBreakdown ? `<div class="bars">${bar("Accroche", c.hook_score)}${bar("Rétention", c.retention_score)}${bar("Partage", c.share_score)}</div>` : ""}
      <div class="tags">${(c.hashtags || []).map((t) => `<span class="tag">#${esc(t)}</span>`).join("")}</div>
      <div class="muted" style="font-size:12px">Extrait ${fmtTime(c.start)} → ${fmtTime(c.end)}</div>
    </div>
    <div class="actions">
      <button class="btn btn-sm btn-tiktok ${Object.keys(up).some((k) => k.startsWith("tiktok")) ? "done" : ""}" data-act="tiktok">${Object.keys(up).filter((k) => k.startsWith("tiktok")).length ? `✓ TikTok${Object.keys(up).filter((k) => k.startsWith("tiktok")).length > 1 ? " ×" + Object.keys(up).filter((k) => k.startsWith("tiktok")).length : ""}` : "Publier TikTok"}</button>
      <button class="btn btn-sm btn-yt ${up.youtube ? "done" : ""}" data-act="youtube">${up.youtube ? "✓ YouTube" : "Publier Shorts"}</button>
      <button class="btn btn-sm btn-ghost" data-act="edit">Modifier</button>
      <a class="btn btn-sm btn-ghost" href="/api/projects/${encodeURIComponent(pid)}/clips/${c.n}/download">Télécharger</a>
      ${canShareFiles() ? `<button class="btn btn-sm btn-primary full" data-act="share">📲 Partager vers TikTok / YouTube…</button>` : ""}
      <button class="btn btn-sm btn-ghost" data-act="copy">Copier la légende</button>
      <button class="btn btn-sm btn-ghost" data-act="delete" style="color:var(--danger)">Supprimer</button>
    </div>
  </article>`;
}

function canShareFiles() {
  try {
    return window.isSecureContext && !!navigator.canShare &&
      navigator.canShare({ files: [new File([""], "t.mp4", { type: "video/mp4" })] });
  } catch { return false; }
}
const shareCache = new Map();

function captionOf(clip) {
  return [clip.title, clip.description, (clip.hashtags || []).map((t) => "#" + t).join(" ")].filter(Boolean).join("\n");
}

async function onClipAction(e, m) {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const n = Number(btn.closest(".clip").dataset.n);
  const clip = m.clips[n - 1];
  const act = btn.dataset.act;
  if (act === "edit") return openEditor(m, n);
  if (act === "delete") {
    if (!confirm(`Supprimer le clip « ${clip.title} » ?`)) return;
    try { await api(`/api/projects/${encodeURIComponent(m.id)}/clips/${n}`, { method: "DELETE" }); toast("Clip supprimé"); renderProject(m.id); }
    catch (err) { toast(err.message, true); }
    return;
  }
  if (act === "share") {
    // Le partage doit suivre directement un toucher : 1er toucher = préparation (téléchargement),
    // 2e toucher = ouverture du menu de partage du téléphone (TikTok, YouTube, Instagram…)
    const key = `${m.id}/${n}/${clip.version || 1}`;
    const caption = captionOf(clip);
    if (!shareCache.has(key)) {
      try { await navigator.clipboard.writeText(caption); } catch {}
      btn.disabled = true; btn.textContent = "Préparation…";
      try {
        const res = await fetch(`/media/${encodeURIComponent(m.id)}/${encodeURIComponent(clip.file)}?v=${clip.version || 1}`);
        if (!res.ok) throw new Error("vidéo introuvable");
        const name = (clip.title || "clip").replace(/[^\p{L}\p{N} _-]/gu, "").slice(0, 50).trim() || "clip";
        shareCache.set(key, new File([await res.blob()], `${name}.mp4`, { type: "video/mp4" }));
        btn.textContent = "📲 Toucher pour partager";
        toast("Prêt : touche encore pour choisir TikTok (légende copiée)");
      } catch (err) { btn.textContent = "📲 Partager vers TikTok / YouTube…"; toast(err.message, true); }
      btn.disabled = false;
      return;
    }
    const file = shareCache.get(key);
    try {
      await navigator.share({ files: [file], title: clip.title, text: caption });
      toast("Dans TikTok, colle la légende (déjà copiée) 👍");
    } catch (err) {
      if (err.name === "AbortError") return;  // l'utilisateur a fermé le menu
      // Android garde parfois le partage précédent « ouvert » (InvalidStateError) : seul un
      // rechargement de la page le libère. Dans tous les cas, on télécharge le clip à la place.
      const why = {
        InvalidStateError: "un partage précédent n'est pas terminé : recharge la page",
        NotAllowedError: "le téléphone a refusé, souvent car le fichier est trop lourd",
        DataError: "fichier trop lourd pour le partage",
      }[err.name] || err.name || "erreur inconnue";
      toast(`Partage impossible (${why}). Téléchargement du clip à la place…`, true);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(file);
      a.download = file.name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
      shareCache.delete(key);
      btn.textContent = "📲 Partager vers TikTok / YouTube…";
    }
    return;
  }
  if (act === "copy") {
    const text = [clip.title, clip.description, (clip.hashtags || []).map((t) => "#" + t).join(" ")].filter(Boolean).join("\n");
    try { await navigator.clipboard.writeText(text); toast("Légende copiée"); } catch { toast("Copie impossible", true); }
    return;
  }
  if (act === "tiktok" || act === "youtube") {
    if (!META.accounts[act] && !(META.accounts.upload_post && META.accounts.upload_post_platforms.includes(act))) { toast(`Connecte d'abord ${act === "tiktok" ? "TikTok" : "YouTube"}`, true); location.hash = "#/comptes"; return; }
    if (clip.uploads?.[act] && !confirm("Ce clip est déjà publié. Le republier ?")) return;
    let account = "main";
    if (act === "tiktok") {
      const linked = (await api("/api/profiles").catch(() => [])).filter((p) => p.tiktok);
      if (linked.length > 1) {
        const pick = prompt("Sur quel compte TikTok ?\n" + linked.map((p, i) => `${i + 1}. ${p.name}`).join("\n"), "1");
        const chosen = linked[Number(pick) - 1];
        if (!chosen) return;
        account = chosen.id;
      } else if (linked.length === 1) account = linked[0].id;
    }
    btn.disabled = true;
    btn.textContent = "Envoi…";
    try {
      const job = await api(`/api/projects/${encodeURIComponent(m.id)}/clips/${n}/publish`, { method: "POST", body: { target: act, account } });
      watchJob(job.id, () => renderProject(m.id));
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  }
}

function watchJob(id, onDone) {
  const t = setInterval(async () => {
    const j = (await api("/api/jobs")).find((x) => x.id === id);
    if (!j || j.stage === "done" || j.stage === "error") {
      clearInterval(t);
      if (j?.stage === "error") toast(j.error || "Échec", true); else toast(j?.message || "Terminé");
      onDone?.();
    }
  }, 1500);
}

async function watchProjectJobs(pid) {
  if (!pendingRenders.size) return;
  const jobs = await api("/api/jobs");
  const active = jobs.some((j) => j.kind === "rerender" && j.project === pid && !["done", "error"].includes(j.stage));
  if (!active) { pendingRenders.clear(); toast("Clip mis à jour"); renderProject(pid); }
}

/* ------------------------------------------------------------------ éditeur */
function openEditor(m, n) {
  const c = m.clips[n - 1];
  const dlg = $("#editor");
  const styleOpts = Object.entries(META.styles).map(([k, l]) => `<option value="${k}" ${k === (c.style || "pop") ? "selected" : ""}>${esc(l)}</option>`).join("");
  dlg.innerHTML = `<form class="ed" method="dialog">
    <h2 style="font-size:22px">Modifier le clip</h2>
    <label>Titre de la publication<input name="title" value="${esc(c.title)}" maxlength="100"></label>
    <label>Description<textarea name="description">${esc(c.description || "")}</textarea></label>
    <label>Hashtags (séparés par des espaces)<input name="hashtags" value="${esc((c.hashtags || []).join(" "))}"></label>
    <label>Accroche affichée à l'écran<input name="hook" value="${esc(c.hook || "")}" maxlength="80"></label>
    <label>Mots mis en couleur<input name="keywords" value="${esc((c.keywords || []).join(", "))}"></label>
    <div class="row">
      <label>Style des sous-titres<select name="style">${styleOpts}</select></label>
      <div></div>
    </div>
    <div class="row">
      <label>Début (s)<div class="nudge"><button type="button" class="btn btn-sm" data-d="start:-1">−1</button><input name="start" type="number" step="0.1" value="${c.start.toFixed(1)}"><button type="button" class="btn btn-sm" data-d="start:1">+1</button></div></label>
      <label>Fin (s)<div class="nudge"><button type="button" class="btn btn-sm" data-d="end:-1">−1</button><input name="end" type="number" step="0.1" value="${c.end.toFixed(1)}"><button type="button" class="btn btn-sm" data-d="end:1">+1</button></div></label>
    </div>
    <div class="note">Changer l'accroche, le style, les mots colorés ou les bornes relance le montage du clip (quelques secondes).</div>
    <div class="ed-foot"><button class="btn btn-ghost" value="cancel">Annuler</button><button class="btn btn-primary" value="save" style="padding:12px 20px">Enregistrer</button></div>
  </form>`;
  dlg.querySelector("form").addEventListener("click", (e) => {
    const d = e.target.dataset.d;
    if (!d) return;
    const [k, delta] = d.split(":");
    const inp = dlg.querySelector(`[name=${k}]`);
    inp.value = (Number(inp.value) + Number(delta)).toFixed(1);
  });
  dlg.onclose = async () => {
    if (dlg.returnValue !== "save") return;
    const f = new FormData(dlg.querySelector("form"));
    const start = Number(f.get("start")), end = Number(f.get("end"));
    if (!(end > start)) { toast("La fin doit être après le début", true); return; }
    const body = {
      title: f.get("title"), description: f.get("description"),
      hashtags: String(f.get("hashtags")).split(/[\s,]+/).map((t) => t.replace(/^#/, "")).filter(Boolean),
      hook: f.get("hook"), style: f.get("style"),
      keywords: String(f.get("keywords")).split(",").map((t) => t.trim()).filter(Boolean),
      start, end,
    };
    try {
      const res = await api(`/api/projects/${encodeURIComponent(m.id)}/clips/${n}`, { method: "PATCH", body });
      if (res.job) { pendingRenders.add(n); toast("Nouveau montage en cours…"); } else toast("Enregistré");
      renderProject(m.id);
    } catch (err) { toast(err.message, true); }
  };
  dlg.showModal();
}

/* ------------------------------------------------------------------ mode automatique */
function currentProfile() { try { return localStorage.getItem("openclip.profile") || "main"; } catch { return "main"; } }
function setProfile(id) { try { localStorage.setItem("openclip.profile", id); } catch {} }

async function renderAuto() {
  let a, profiles;
  let pid = currentProfile();
  try {
    profiles = await api("/api/profiles");
    if (!profiles.some((p) => p.id === pid)) { pid = "main"; setProfile(pid); }
    a = await api(`/api/automation?profile=${encodeURIComponent(pid)}`);
  } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const prof = profiles.find((p) => p.id === pid);
  const q = (path) => path + (path.includes("?") ? "&" : "?") + "profile=" + encodeURIComponent(pid);
  const save = async (changes) => {
    try { await api(q("/api/automation"), { method: "PUT", body: changes }); renderAuto(); }
    catch (err) { toast(err.message, true); }
  };
  const statusLabel = { pending: "En attente", published: "Publié ✓", failed: "Échec" };
  const missing = a.targets.filter((t) => !a.connected[t]);
  const pending = a.queue.filter((q) => q.status === "pending").length;
  view.innerHTML = `
    <div class="chips" style="margin-bottom:18px" id="profiles">
      ${profiles.map((p) => `<button type="button" class="chip" data-profile="${esc(p.id)}" aria-pressed="${p.id === pid}">${p.enabled ? "🟢" : "⚪️"} ${esc(p.name)}${p.pending ? ` <small>${p.pending}</small>` : ""}</button>`).join("")}
      <button type="button" class="chip" id="profile-add">＋ Ajouter un compte</button>
    </div>
    <div class="account" style="margin-bottom:18px">
      <h3>${esc(prof.name)} <span class="status ${a.connected.tiktok ? "ok" : ""}">TikTok ${a.connected.tiktok ? "connecté" : "non connecté"}</span></h3>
      <div class="muted" style="font-size:13px">Chaque compte a ses propres sources, réglages, heures et file d'attente. Les clips de ce compte sont publiés sur <b>son</b> compte TikTok.</div>
      <div class="chips">
        <button class="btn btn-sm btn-tiktok" id="profile-tiktok">${prof.tiktok ? "Reconnecter" : "Connecter"} le TikTok de ce compte</button>
        <button class="btn btn-sm btn-ghost" id="profile-rename">Renommer</button>
        ${pid !== "main" ? `<button class="btn btn-sm btn-ghost" id="profile-del" style="color:var(--danger)">Supprimer ce compte</button>` : ""}
      </div>
      <div class="note">Avant de connecter : ouvre l'app TikTok et passe sur le bon compte (ou déconnecte-toi de tiktok.com dans le navigateur), puis touche le bouton et autorise. Avec ta propre app TikTok en Sandbox, ajoute chaque compte dans « Target users ».</div>
      <label class="ed" style="padding:0;margin-top:6px">Profil Upload-Post de ce compte (optionnel)<input id="profile-up" value="${esc(a.upload_post_user || "")}" placeholder="ex. @inspecteurnews"></label>
    </div>
    <div class="proj-head">
      <div>
        <h1 style="font-size:34px">Mode automatique</h1>
        <p class="muted" style="max-width:640px">OpenClip surveille tes chaînes YouTube, crée les clips de chaque nouvelle vidéo et publie le meilleur clip en attente à chaque créneau horaire.</p>
      </div>
      <label class="switch" style="font-size:16px;font-weight:600"><input type="checkbox" id="auto-on" ${a.enabled ? "checked" : ""}> ${a.enabled ? "Activé" : "Désactivé"}</label>
    </div>
    ${missing.length ? `<div class="tip" style="color:var(--amber);margin-top:12px">⚠️ Non connecté : ${missing.map((t) => t === "tiktok" ? "TikTok" : "YouTube").join(", ")}. Les publications vers ${missing.length > 1 ? "ces comptes" : "ce compte"} échoueront. <a href="#/comptes">Connecter</a></div>` : ""}

    <div class="section-title"><h2>Chaînes suivies</h2><button class="btn btn-sm btn-ghost" id="auto-check">Vérifier maintenant</button></div>
    <form class="url-row" id="auto-add"><input id="auto-url" placeholder="https://www.youtube.com/@nomdelachaine" autocomplete="off"><button class="btn btn-primary" style="padding:12px 18px">Suivre</button></form>
    <div class="jobs" style="margin-top:10px">${a.channels.length ? a.channels.map((c) => `
      <div class="job" style="display:flex;justify-content:space-between;align-items:center;gap:10px">
        <div><b>${esc(c.name)}</b><div class="muted" style="font-size:12px">Seules les nouvelles vidéos sont traitées</div></div>
        <button class="btn btn-sm btn-ghost" data-unfollow="${esc(c.channel_id)}">Retirer</button>
      </div>`).join("") : `<div class="empty">Aucune chaîne. Ajoute le lien d'une chaîne dont tu as l'autorisation de clipper.</div>`}</div>

    <div class="section-title"><h2>Recherches tendances</h2></div>
    <p class="muted" style="margin-top:-6px">Toutes les heures, OpenClip cherche les vidéos YouTube les plus vues de la période pour chaque recherche, et transforme les meilleures en clips.</p>
    <form class="url-row" id="search-add"><input id="search-q" placeholder="ex. GTA 6, GTA 6 gameplay, Rockstar…" autocomplete="off"><button class="btn btn-primary" style="padding:12px 18px">Ajouter</button></form>
    <div class="jobs" style="margin-top:10px">${a.searches.length ? a.searches.map((q) => `
      <div class="job">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
          <div><b>🔎 ${esc(q.query)}</b><div class="muted" style="font-size:12px">${(q.seen || []).length} vidéo(s) déjà traitée(s)</div></div>
          <div style="display:flex;gap:6px"><button class="btn btn-sm btn-ghost" data-preview="${esc(q.query)}">Aperçu</button><button class="btn btn-sm btn-ghost" data-unsearch="${esc(q.query)}">Retirer</button></div>
        </div>
        <div class="preview" data-for="${esc(q.query)}"></div>
      </div>`).join("") : `<div class="empty">Aucune recherche. Ajoute par exemple « GTA 6 ».</div>`}</div>
    <div class="launcher" style="margin:12px 0 0;max-width:none"><div class="options">
      <div><div class="opt-label">Vidéos publiées</div>${chips("search_period", [["today", "Aujourd'hui"], ["week", "Cette semaine"], ["month", "Ce mois"]], a.search_period)}</div>
      <div><div class="opt-label">Vues minimum</div>${chips("search_min_views", [[1000, "1 k"], [10000, "10 k"], [50000, "50 k"], [200000, "200 k"]], a.search_min_views)}</div>
      <div><div class="opt-label">Nouvelles vidéos par jour (max)</div>${chips("max_new_videos_per_day", [[1, "1 (la meilleure)"], [2, "2"], [4, "4"], [8, "8"]], a.max_new_videos_per_day)}</div>
    </div>
    <div class="tip" style="padding:0 12px 12px">⚠️ Clipper les vidéos d'autres créateurs sans leur accord expose à des réclamations de droits d'auteur et n'est pas monétisable (« contenu réutilisé »). Privilégie les créateurs qui autorisent les clips, et ajoute ta valeur (titre, accroche, montage).</div></div>

    <div class="section-title"><h2>Réglages</h2></div>
    <div class="launcher" style="margin:0;max-width:none"><div class="options">
      <div><div class="opt-label">Clips par vidéo</div>${chips("clips_per_video", [[1, "1"], [3, "3"], [5, "5"], [8, "8"], [12, "12"]], a.clips_per_video)}</div>
      <div><div class="opt-label">Score minimum pour publier</div>${chips("min_score", [[40, "40"], [55, "55"], [70, "70"], [80, "80"]], a.min_score)}</div>
      <div><div class="opt-label">Durée des clips</div>${chips("preset", Object.keys(PRESET_INFO).map((k) => [k, PRESET_INFO[k][0], PRESET_INFO[k][1]]), a.preset)}</div>
      <div><div class="opt-label">Cadrage</div>${chips("crop_mode", [["face", "Intelligent"], ["center", "Centré"], ["blur", "Fond flou"]], a.crop_mode)}</div>
      <div><div class="opt-label">Style des sous-titres</div>${chips("caption_style", Object.entries(META.styles).map(([k, l]) => [k, l.split(" — ")[0]]), a.caption_style)}</div>
      <div><div class="opt-label">Vidéos sources max</div>${chips("max_video_minutes", [[30, "30 min"], [60, "1 h"], [90, "1 h 30"], [180, "3 h"]], a.max_video_minutes)}</div>
      <div class="opt-wide"><div class="opt-label">Publier sur</div>
        <div class="switches">
          <label class="switch"><input type="checkbox" data-target="tiktok" ${a.targets.includes("tiktok") ? "checked" : ""}> TikTok</label>
          <label class="switch"><input type="checkbox" data-target="youtube" ${a.targets.includes("youtube") ? "checked" : ""}> YouTube Shorts</label>
        </div></div>
      <div class="opt-wide"><div class="opt-label">Heures de publication (${esc(a.timezone)})</div>
        <div class="chips">${a.slots.map((s) => `<button type="button" class="chip" aria-pressed="true" data-slot="${s}">${s} ✕</button>`).join("")}
          <input type="time" id="slot-new" class="chip" style="color:var(--text)"><button type="button" class="btn btn-sm" id="slot-add">Ajouter</button></div>
        <div class="tip">À chaque heure, le meilleur clip en attente est publié. 1 à 3 publications par jour, c'est l'idéal.</div></div>
    </div></div>

    <div class="section-title"><h2>File d'attente <span class="muted" style="font-size:15px">· ${pending} en attente</span></h2><button class="btn btn-sm btn-ghost" id="auto-now" ${pending ? "" : "disabled"}>Publier le prochain maintenant</button></div>
    <div class="jobs">${a.queue.length ? a.queue.map((q) => `
      <div class="job ${q.status === "failed" ? "error" : ""}" style="display:flex;gap:12px;align-items:center">
        <div class="score" style="--p:${Math.round(q.score)};--c:${scoreColor(q.score)};width:44px;height:44px;flex:none"><span style="font-size:14px">${Math.round(q.score)}</span></div>
        <div style="flex:1;min-width:0">
          <a href="#/p/${encodeURIComponent(q.project)}" style="font-weight:600;text-decoration:none">${esc(q.title)}</a>
          <div class="muted" style="font-size:12px">${esc(statusLabel[q.status] || q.status)} · ${esc(q.source || "")}</div>
          ${q.errors && Object.keys(q.errors).length ? `<div class="err">${esc(Object.entries(q.errors).map(([k, v]) => k + " : " + v).join(" · "))}</div>` : ""}
        </div>
        ${q.status === "pending" ? `<button class="btn btn-sm btn-ghost" data-dequeue="${esc(q.project)}/${q.clip}">Retirer</button>` : ""}
      </div>`).join("") : `<div class="empty">Vide. Les clips des nouvelles vidéos arrivent ici, ou utilise « Ajouter à la file auto » sur un projet.</div>`}</div>`;

  view.querySelectorAll("[data-profile]").forEach((b) => (b.onclick = () => { setProfile(b.dataset.profile); renderAuto(); }));
  $("#profile-add").onclick = async () => {
    const name = prompt("Nom du compte (ex. @inspecteurnews)");
    if (!name) return;
    try { const p = await api("/api/profiles", { method: "POST", body: { name } }); setProfile(p.id); toast(`Compte ${p.name} ajouté`); renderAuto(); }
    catch (err) { toast(err.message, true); }
  };
  $("#profile-rename").onclick = async () => {
    const name = prompt("Nouveau nom", prof.name);
    if (name) { await api(`/api/profiles/${encodeURIComponent(pid)}`, { method: "PATCH", body: { name } }); renderAuto(); }
  };
  if ($("#profile-del")) $("#profile-del").onclick = async () => {
    if (!confirm(`Supprimer le compte « ${prof.name} », ses réglages et sa file d'attente ? (les projets restent)`)) return;
    await api(`/api/profiles/${encodeURIComponent(pid)}`, { method: "DELETE" }); setProfile("main"); renderAuto();
  };
  $("#profile-tiktok").onclick = async () => {
    try {
      const r = await api(`/api/connect/tiktok?direct=true&profile=${encodeURIComponent(pid)}`, { method: "POST" });
      location.href = r.url;
    } catch (err) { toast(err.message.includes("https") || err.message.includes("TIKTOK") ? "Configure d'abord ton application TikTok dans Comptes" : err.message, true); }
  };
  $("#profile-up").onchange = (e) => save({ upload_post_user: e.target.value.trim() });
  $("#auto-on").onchange = (e) => save({ enabled: e.target.checked });
  view.querySelectorAll(".options [data-group]").forEach((g) => g.addEventListener("click", (e) => {
    const b = e.target.closest("[data-v]"); if (!b) return;
    const num = ["clips_per_video", "min_score", "max_video_minutes", "search_min_views", "max_new_videos_per_day"].includes(g.dataset.group);
    save({ [g.dataset.group]: num ? Number(b.dataset.v) : b.dataset.v });
  }));
  view.querySelectorAll("[data-target]").forEach((c) => (c.onchange = () => save({ targets: [...view.querySelectorAll("[data-target]:checked")].map((x) => x.dataset.target) })));
  view.querySelectorAll("[data-slot]").forEach((b) => (b.onclick = () => save({ slots: a.slots.filter((s) => s !== b.dataset.slot) })));
  $("#slot-add").onclick = () => { const v = $("#slot-new").value; if (v) save({ slots: [...a.slots, v] }); };
  $("#auto-add").onsubmit = async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button"); btn.disabled = true; btn.textContent = "Recherche…";
    try { const c = await api(q("/api/automation/channels"), { method: "POST", body: { url: $("#auto-url").value.trim() } }); toast(`Chaîne suivie : ${c.name}`); renderAuto(); }
    catch (err) { toast(err.message, true); btn.disabled = false; btn.textContent = "Suivre"; }
  };
  $("#search-add").onsubmit = async (e) => {
    e.preventDefault();
    try { await api(q("/api/automation/searches"), { method: "POST", body: { query: $("#search-q").value } }); toast("Recherche ajoutée"); renderAuto(); }
    catch (err) { toast(err.message, true); }
  };
  view.querySelectorAll("[data-unsearch]").forEach((b) => (b.onclick = async () => {
    await api(q(`/api/automation/searches?query=${encodeURIComponent(b.dataset.unsearch)}`), { method: "DELETE" }); renderAuto();
  }));
  view.querySelectorAll("[data-preview]").forEach((b) => (b.onclick = async () => {
    const box = view.querySelector(`.preview[data-for="${CSS.escape(b.dataset.preview)}"]`);
    box.innerHTML = `<div class="muted" style="font-size:13px;margin-top:8px">Recherche…</div>`;
    try {
      const vids = await api(q(`/api/automation/preview?query=${encodeURIComponent(b.dataset.preview)}`));
      const fmtViews = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + " M" : n >= 1e3 ? Math.round(n / 1e3) + " k" : n;
      box.innerHTML = vids.length ? `<ol class="guide" style="margin-top:10px">${vids.map((v) => `<li><a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.title)}</a> <span class="muted">· ${fmtViews(v.views)} vues · ${fmtTime(v.duration || 0)} · ${esc(v.channel)}</span></li>`).join("")}</ol>` : `<div class="muted" style="font-size:13px;margin-top:8px">Aucune vidéo ne passe les filtres (vues minimum, durée).</div>`;
    } catch (err) { box.innerHTML = `<div class="err">${esc(err.message)}</div>`; }
  }));
  view.querySelectorAll("[data-unfollow]").forEach((b) => (b.onclick = async () => { if (!confirm("Ne plus suivre cette chaîne ?")) return; await api(q(`/api/automation/channels/${b.dataset.unfollow}`), { method: "DELETE" }); renderAuto(); }));
  view.querySelectorAll("[data-dequeue]").forEach((b) => (b.onclick = async () => { await api(q(`/api/automation/queue/${b.dataset.dequeue}`), { method: "DELETE" }); renderAuto(); }));
  $("#auto-check").onclick = async (e) => {
    e.target.disabled = true;
    try { const r = await api(q("/api/automation/check"), { method: "POST" }); toast(r.new_videos ? `${r.new_videos} nouvelle(s) vidéo(s) : clips en préparation` : "Pas de nouvelle vidéo"); }
    catch (err) { toast(err.message, true); }
    e.target.disabled = false;
  };
  $("#auto-now").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Publication…";
    try { const r = await api(q("/api/automation/publish-next"), { method: "POST" }); toast(Object.keys(r.results).length ? `Publié : ${r.title}` : "Échec de la publication", !Object.keys(r.results).length); }
    catch (err) { toast(err.message, true); }
    renderAuto();
  };
}

/* ------------------------------------------------------------------ comptes */
async function renderAccounts() {
  META = await api("/api/meta");
  const cookies = await api("/api/cookies");
  const a = META.accounts;
  const st = (ok, label = "Connecté") => `<span class="status ${ok ? "ok" : ""}">${ok ? label : "Non connecté"}</span>`;
  view.innerHTML = `
    <h1 style="font-size:34px">Comptes</h1>
    <p class="muted">Les clés et jetons restent sur ta machine (fichier <code>.env</code> et <code>~/.openclip/tokens</code>).</p>
    <div class="accounts" style="margin-top:20px">
      <div class="account"><h3>Claude <span class="status ${a.claude ? "ok" : ""}">${a.claude ? "Clé trouvée" : "Optionnel"}</span></h3>
        <div class="muted">${a.claude ? "Claude choisit les moments et rédige les accroches." : "Sans clé, OpenClip utilise son analyse locale gratuite (accroche, rythme, énergie de la voix, émotions, chiffres)."}</div>
        ${a.claude ? "" : `<div class="note">Pour des textes encore plus naturels, tu peux ajouter une clé <code>ANTHROPIC_API_KEY</code> dans <code>.env</code> (payant à l'usage).</div>`}
      </div>
      <div class="account" style="border-color:#3a3020"><h3>Upload-Post <span class="status ${a.upload_post ? "ok" : ""}">${a.upload_post ? "Configuré" : "Le plus simple"}</span></h3>
        <div class="muted">Publie sur TikTok et YouTube sans créer d'application : tu connectes tes comptes sur upload-post.com, puis tu colles ici ta clé API. Les vidéos TikTok peuvent être publiques tout de suite.</div>
        <details ${a.upload_post ? "" : "open"}>
          <summary>${a.upload_post ? "Réglages" : "Comment faire (5 min)"}</summary>
          <ol class="guide">
            <li>Crée un compte sur <b>upload-post.com</b> (forfait gratuit : quelques envois par mois).</li>
            <li>Dans <b>Manage Users / Profiles</b>, crée un profil (ex. <code>gta6</code>) puis connecte <b>TikTok</b> et <b>YouTube</b> à ce profil.</li>
            <li>Dans <b>API Keys</b>, génère une clé et copie-la.</li>
            <li>Remplis ci-dessous, coche les plateformes à publier via Upload-Post, puis Enregistrer.</li>
          </ol>
          <form class="ed" id="up-form" style="padding:0">
            <label>Clé API<input name="api_key" type="password" placeholder="${a.upload_post ? "•••••• (inchangée)" : ""}" autocomplete="off"></label>
            <label>Nom du profil Upload-Post<input name="user" value="${esc(a.upload_post_user || "")}" placeholder="gta6" autocomplete="off"></label>
            <div class="switches">
              ${["tiktok", "youtube", "instagram"].map((p) => `<label class="switch"><input type="checkbox" name="pf" value="${p}" ${(a.upload_post_platforms || []).includes(p) ? "checked" : ""}> ${{ tiktok: "TikTok", youtube: "YouTube", instagram: "Instagram" }[p]}</label>`).join("")}
            </div>
            <button class="btn btn-primary" style="padding:12px 18px">Enregistrer</button>
          </form>
        </details>
      </div>
      <div class="account"><h3>TikTok ${st(a.tiktok)}</h3>
        <div class="muted">Publie tes clips sur ton compte TikTok.</div>
        ${a.tiktok_configured ? (a.tiktok_redirect_uri.startsWith(location.origin + "/")
          ? `<button class="btn btn-tiktok" id="tt-direct">${a.tiktok ? "Reconnecter TikTok" : "Se connecter à TikTok"}</button>`
          : `<a class="btn btn-tiktok" href="#/connect/tiktok">${a.tiktok ? "Reconnecter TikTok" : "Se connecter à TikTok"}</a>`) : ""}
        ${a.tiktok ? `<button class="btn btn-sm btn-ghost" data-disconnect="tiktok">Déconnecter</button>` : ""}
        <details ${a.tiktok_configured ? "" : "open"}>
          <summary>${a.tiktok_configured ? "Réglages de l'application TikTok" : "Étape 1 : ton application TikTok (une seule fois)"}</summary>
          <ol class="guide">
            <li>Sur <b>developers.tiktok.com</b>, connecte-toi puis <b>Manage apps → Connect an app</b>.</li>
            <li>Ajoute les produits <b>Login Kit</b> et <b>Content Posting API</b> (active « Direct Post »).</li>
            <li>Scopes : <code>user.info.basic</code>, <code>video.upload</code>, <code>video.publish</code>.</li>
            <li>Dans Login Kit, déclare la <b>Redirect URI</b> <code>${esc(location.protocol === "https:" ? location.origin : "https://ton-domaine.fr")}/callback/tiktok</code>${location.protocol === "https:" ? "" : " (remplace par le domaine d'OpenClip)"}.</li>
            <li>Site, conditions et confidentialité demandés par TikTok : <code>…/legal/terms</code> et <code>…/legal/privacy</code> sur ce même domaine.</li>
            <li>Onglet <b>Sandbox</b> : ajoute ton compte TikTok comme « Target user » pour tester tout de suite.</li>
            <li>Copie ici le <b>Client key</b> et le <b>Client secret</b>.</li>
          </ol>
          <form class="ed" id="tt-form" style="padding:0">
            <label>Client key<input name="client_key" value="${esc(a.tiktok_client_key || "")}" autocomplete="off"></label>
            <label>Client secret<input name="client_secret" type="password" placeholder="${a.tiktok_configured ? "•••••• (inchangé)" : ""}" autocomplete="off"></label>
            <label>Redirect URI (identique à celle de l'app TikTok)<input name="redirect_uri" value="${esc(a.tiktok_redirect_uri || "")}" placeholder="https://ton-domaine.fr/callback/tiktok"></label>
            <label>Mode de publication
              <select name="mode">
                <option value="draft" ${a.tiktok_mode !== "direct" ? "selected" : ""}>Brouillon : tu valides dans l'app TikTok (recommandé)</option>
                <option value="direct" ${a.tiktok_mode === "direct" ? "selected" : ""}>Publication directe (privée tant que TikTok n'a pas audité ton app)</option>
              </select></label>
            <button class="btn btn-primary" style="padding:12px 18px">Enregistrer</button>
          </form>
        </details>
      </div>
      <div class="account"><h3>YouTube Shorts ${st(a.youtube)}</h3>
        <div class="muted">Publie tes clips en Shorts sur ta chaîne.</div>
        ${a.youtube_configured ? `<a class="btn btn-yt" href="#/connect/youtube">${a.youtube ? "Reconnecter YouTube" : "Se connecter à YouTube"}</a>` : ""}
        ${a.youtube ? `<button class="btn btn-sm btn-ghost" data-disconnect="youtube">Déconnecter</button>` : ""}
        <details ${a.youtube_configured ? "" : "open"}>
          <summary>${a.youtube_configured ? "Application Google" : "Étape 1 : ton application Google (une seule fois)"}</summary>
          <ol class="guide">
            <li>Sur <b>console.cloud.google.com</b>, crée un projet.</li>
            <li><b>API et services → Bibliothèque</b> : active <b>YouTube Data API v3</b>.</li>
            <li><b>Écran de consentement OAuth</b> : type « Externe », puis ajoute ton adresse Gmail dans <b>Utilisateurs test</b>.</li>
            <li><b>Identifiants → Créer → ID client OAuth</b>, type <b>« Application de bureau »</b>, puis <b>Télécharger le JSON</b>.</li>
            <li>Envoie ce fichier ici :</li>
          </ol>
          <label class="btn btn-sm">${a.youtube_configured ? "Remplacer" : "Envoyer"} le fichier JSON<input type="file" id="yt-client" accept=".json,application/json" hidden></label>
        </details>
      </div>
    </div>
    <div class="account" style="margin-top:16px">
      <h3>Téléchargement YouTube <span class="status ${cookies.logged_in ? "ok" : ""}">${cookies.logged_in ? "Connecté" : cookies.present ? "Session incomplète" : "Non connecté"}</span></h3>
      ${cookies.present && !cookies.logged_in ? `<div class="note" style="color:var(--amber)">Les cookies actuels ne contiennent pas de session YouTube connectée : reconnecte-toi et va jusqu'à youtube.com (ta photo de profil doit apparaître en haut à droite).</div>` : ""}
      <div class="muted">YouTube bloque souvent les serveurs (« confirm you're not a bot »). Avec les cookies d'un navigateur connecté à YouTube, le téléchargement repasse.</div>
      ${cookies.present ? `<div class="note">Mis à jour le ${new Date(cookies.updated_at * 1000).toLocaleString("fr-FR")}. Ils expirent au bout de quelques semaines : renvoie-en si l'erreur revient.</div>` : ""}
      <div class="chips">
        <a class="btn btn-sm btn-yt" href="#/youtube">▶ Se connecter à YouTube</a>
        ${cookies.present ? `<button class="btn btn-sm btn-ghost" id="cookies-refresh">Actualiser</button>` : ""}
      </div>
      <div class="note">Ouvre un vrai navigateur sur le serveur, affiché ici : connecte-toi à ton compte YouTube, OpenClip garde la session. Idéalement avec un compte Google secondaire.</div>
      <div class="chips">
        <label class="btn btn-sm btn-ghost">${cookies.present ? "Remplacer" : "Envoyer"} un cookies.txt<input type="file" id="cookies" accept=".txt,text/plain" hidden></label>
        ${cookies.present ? `<button class="btn btn-sm btn-ghost" id="cookies-del">Supprimer</button>` : ""}
      </div>
      <div class="note">Autre méthode (ordinateur) : connecte-toi à YouTube (idéalement avec un compte secondaire), installe l'extension « Get cookies.txt LOCALLY » (Chrome ou Firefox), ouvre youtube.com, clique sur l'extension puis « Export ». Envoie ici le fichier obtenu. Ne le partage avec personne : il donne accès au compte.</div>
    </div>
    <section class="money">
      <h2>Transformer les vues en revenus</h2>
      <ol>
        <li><b>TikTok Creator Rewards</b> ne paie que les vidéos de <b>plus d'1 minute</b> et le contenu <b>original</b>. Utilise la durée « 62–95 s » et ajoute ta valeur (commentaire, angle, montage) plutôt que de reposter tel quel.</li>
        <li><b>YouTube Shorts</b> partage les revenus pub une fois le Programme Partenaire atteint. Les re-uploads sans transformation sont classés « contenu réutilisé » et refusés.</li>
        <li>Ne clippe que des créateurs qui t'y autorisent : beaucoup de streamers et podcasteurs ont un programme de clippeurs payé à la vue, c'est souvent la façon la plus rapide de gagner de l'argent.</li>
        <li>Publie régulièrement (1 à 3 clips par jour), garde une seule thématique par compte, et regarde quels scores d'accroche donnent les meilleures vues pour affiner tes choix.</li>
      </ol>
    </section>`;
}

document.addEventListener("change", async (e) => {
  if (e.target.id !== "cookies" || !e.target.files[0]) return;
  try {
    const res = await fetch("/api/cookies", { method: "POST", body: await e.target.files[0].text() });
    if (!res.ok) throw new Error((await res.json()).detail || "Fichier refusé");
    toast("Cookies enregistrés ✓");
    renderAccounts();
  } catch (err) { toast(err.message, true); }
});
document.addEventListener("click", async (e) => {
  if (e.target.id !== "tt-direct") return;
  try {
    const r = await api("/api/connect/tiktok?direct=true", { method: "POST" });
    location.href = r.url;  // page officielle TikTok, puis retour sur OpenClip
  } catch (err) { toast(err.message, true); }
});
document.addEventListener("submit", async (e) => {
  if (e.target.id !== "up-form") return;
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/settings/upload-post", { method: "POST", body: { api_key: f.get("api_key"), user: f.get("user"), platforms: f.getAll("pf") } });
    toast("Upload-Post enregistré ✓");
    META = await api("/api/meta");
    renderAccounts();
  } catch (err) { toast(err.message, true); }
});
document.addEventListener("submit", async (e) => {
  if (e.target.id !== "tt-form") return;
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/settings/tiktok", { method: "POST", body: Object.fromEntries(f) });
    toast("Réglages TikTok enregistrés ✓");
    renderAccounts();
  } catch (err) { toast(err.message, true); }
});
document.addEventListener("change", async (e) => {
  if (e.target.id !== "yt-client" || !e.target.files[0]) return;
  try {
    const res = await fetch("/api/settings/youtube-client", { method: "POST", body: await e.target.files[0].text() });
    if (!res.ok) throw new Error((await res.json()).detail || "Fichier refusé");
    toast("Application Google enregistrée ✓");
    renderAccounts();
  } catch (err) { toast(err.message, true); }
});
document.addEventListener("click", async (e) => {
  const p = e.target.dataset?.disconnect;
  if (!p || !confirm("Déconnecter ce compte ?")) return;
  await api(`/api/connect/${p}`, { method: "DELETE" });
  toast("Compte déconnecté");
  renderAccounts();
});
document.addEventListener("click", async (e) => {
  if (e.target.id !== "cookies-refresh") return;
  e.target.disabled = true; e.target.textContent = "Actualisation… (20 s)";
  try { await api("/api/browser/refresh", { method: "POST" }); toast("Cookies actualisés ✓"); }
  catch (err) { toast(err.message, true); }
  renderAccounts();
});
document.addEventListener("click", async (e) => {
  if (e.target.id !== "cookies-del") return;
  await api("/api/cookies", { method: "DELETE" });
  toast("Cookies supprimés");
  renderAccounts();
});

/* ------------------------------------------------------------------ connexion YouTube */
const KEYS = { Enter: 0xff0d, Backspace: 0xff08, Tab: 0xff09, Escape: 0xff1b, Ctrl: 0xffe3 };
const keysym = (ch) => { const cp = ch.codePointAt(0); return cp <= 0xff ? cp : 0x01000000 | cp; };

async function loadRFB() {
  try { return (await import("/novnc/core/rfb.js")).default; }
  catch { return (await import("https://cdn.jsdelivr.net/npm/@novnc/novnc@1.5.0/core/rfb.js")).default; }
}

const CONNECT_TEXT = {
  download: ["Connexion YouTube (téléchargement)", "Un vrai navigateur tourne sur ton serveur et s'affiche ci-dessous. Connecte-toi à ton compte Google, <b>attends d'arriver sur YouTube avec ta photo de profil en haut à droite</b>, puis touche « J'ai fini »."],
  tiktok: ["Connecter TikTok", "La page officielle de TikTok s'ouvre ci-dessous. Connecte-toi puis touche <b>« Autoriser »</b>. Ensuite une page d'erreur peut s'afficher : c'est normal, OpenClip récupère l'autorisation tout seul."],
  youtube: ["Connecter YouTube (publication)", "La page officielle de Google s'ouvre ci-dessous. Choisis ton compte, touche <b>« Continuer »</b> (Google prévient que l'application n'est pas validée : c'est ta propre application), coche les autorisations puis <b>« Continuer »</b>. Ensuite une page « inaccessible » s'affiche : c'est normal, OpenClip récupère l'autorisation tout seul."],
};

async function renderYoutubeLogin(mode = "download") {
  const st = await api("/api/browser");
  const [title, intro] = CONNECT_TEXT[mode];
  view.innerHTML = `
    <a href="#/comptes" class="muted" style="text-decoration:none">← Comptes</a>
    <h1 style="font-size:28px;margin-top:10px">${title}</h1>
    <p class="muted">${intro}</p>
    ${st.missing.length ? `<div class="empty">Il manque sur le serveur : ${esc(st.missing.join(", "))}.<br>Relance <code>sudo bash deploy/install.sh</code> pour les installer.</div>` : `
    <div class="yt-actions">
      <button class="btn btn-primary" id="b-start">${st.running ? "Reprendre" : "Ouvrir le navigateur"}</button>
      <button class="btn btn-ghost" id="b-cancel" ${st.running ? "" : "hidden"}>Annuler</button>
    </div>
    <div id="screen-wrap" hidden>
      <div id="screen"></div>
      <div class="typebar">
        <input id="typer" placeholder="Écris ici (e-mail, mot de passe…) puis ➤" autocomplete="off" autocapitalize="off" spellcheck="false">
        <button class="btn btn-sm" id="k-send">➤</button>
      </div>
      <div class="chips">
        <button class="btn btn-sm btn-ghost" data-k="Enter">⏎ Entrée</button>
        <button class="btn btn-sm btn-ghost" data-k="Backspace">⌫ Effacer</button>
        <button class="btn btn-sm btn-ghost" data-k="Tab">⇥ Tab</button>
      </div>
      <div class="note">Touche l'écran pour cliquer. Les caractères tapés dans la barre sont envoyés au champ sélectionné dans le navigateur.</div>
      ${mode === "download" ? `<button class="btn btn-primary" id="b-finish" style="width:100%;margin-top:12px">✓ J'ai fini, enregistrer la connexion</button>` : `<div class="note" id="wait-auth" style="text-align:center;margin-top:12px">En attente de ton autorisation…</div>`}
    </div>`}`;
  if (st.missing.length) return;

  const connect = async () => {
    const res = await api(mode === "download" ? "/api/browser/start" : `/api/connect/${mode}`, { method: "POST" });
    if (mode !== "download") watchAuth();
    const RFB = await loadRFB();
    $("#screen-wrap").hidden = false;
    $("#b-start").hidden = true;
    $("#b-cancel").hidden = false;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const rfb = new RFB($("#screen"), `${proto}://${location.host}/vnc?token=${encodeURIComponent(res.token)}`);
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    rfb.focusOnClick = false;
    rfb.addEventListener("disconnect", () => { if (window.__rfb === rfb) toast("Navigateur déconnecté", true); });
    window.__rfb = rfb;
  };
  $("#b-start").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Démarrage…";
    try { await connect(); } catch (err) { toast(err.message, true); e.target.disabled = false; e.target.textContent = "Ouvrir le navigateur"; }
  };
  $("#b-cancel").onclick = async () => {
    window.__rfb?.disconnect(); window.__rfb = null;
    await api("/api/browser/stop", { method: "POST" });
    location.hash = "#/comptes";
  };
  const send = () => {
    const rfb = window.__rfb, inp = $("#typer");
    if (!rfb || !inp.value) return;
    for (const ch of inp.value) rfb.sendKey(keysym(ch));
    inp.value = "";
  };
  $("#k-send").onclick = send;
  $("#typer").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(); window.__rfb?.sendKey(KEYS.Enter); } });
  document.querySelectorAll("[data-k]").forEach((b) => (b.onclick = () => window.__rfb?.sendKey(KEYS[b.dataset.k])));
  function watchAuth() {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      const r = await api(`/api/connect/${mode}`).catch(() => null);
      if (!r || r.pending) return;
      clearInterval(pollTimer);
      window.__rfb?.disconnect(); window.__rfb = null;
      if (r.done) { toast(`${mode === "tiktok" ? "TikTok" : "YouTube"} connecté ✓ ${r.account || ""}`); location.hash = "#/comptes"; }
      else { toast(r.error || "Autorisation refusée", true); renderYoutubeLogin(mode); }
    }, 2000);
  }
  if ($("#b-finish")) $("#b-finish").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Enregistrement…";
    window.__rfb?.disconnect(); window.__rfb = null;
    try {
      const r = await api("/api/browser/finish", { method: "POST" });
      toast(`Connexion YouTube enregistrée ✓ (${r.cookies} cookies)`);
      location.hash = "#/comptes";
    } catch (err) {
      toast(err.message, true);
      e.target.disabled = false; e.target.textContent = "✓ J'ai fini, enregistrer la connexion";
      renderYoutubeLogin();
    }
  };
  if (st.running && mode === "download") $("#b-start").click();
}

/* ------------------------------------------------------------------ démarrage */
(async () => {
  try { META = await api("/api/meta"); } catch {}
  route();
})();

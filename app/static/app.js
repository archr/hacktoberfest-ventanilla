// Ventanilla — frontend del chat (sin dependencias).
import { VoiceDictation } from "./voice.mjs?v=5";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const STORE_KEY = "ventanilla.conversation.v1";
let voice;
let voiceSupported = false;
let voiceUnavailableReason = "";
let voicePhase = "idle";

function syncVoiceControls() {
  const active = voice?.active || false;
  $("#input").readOnly = active;
  $("#send").disabled = active;
  $("#dictate").disabled = !voiceSupported || state.busy || voicePhase === "stopping";
  $("#dictate").classList.toggle("listening", active);
  $("#dictate").setAttribute("aria-pressed", String(active));
  const label = active ? (voicePhase === "starting" ? "Cancelar dictado" : "Detener dictado") : (voiceUnavailableReason || "Dictar mensaje por voz");
  $("#dictate").setAttribute("aria-label", label);
  $("#dictate").title = label;
  $("#voice-cancel").hidden = !active;
  $("#attach").disabled = active || state.busy; // no adjuntar oficios mientras se dicta o responde
  $$("#suggestions button").forEach((button) => { button.disabled = active || state.busy; });
}

const state = {
  conversationId: null,
  history: [], // mensajes en formato LLM (user/assistant/tool) que se reenvían al backend
  transcript: [], // lo que se dibuja: {role, text, reasoning, sources, documents, notices, search}
  busy: false,
  controller: null,
};
// Cambia con «Nueva conversación»: una petición que termina después ya no escribe en la nueva.
let epoch = 0;
let dictamenCfg = null; // GET /api/config -> dictamen (null si está deshabilitado)
// Al recargar o cerrar, el navegador aborta los fetch pendientes: no hay que guardar ese fallo
// como definitivo (la tarjeta queda «enviando» y al volver se muestra como interrumpida).
let unloading = false;
window.addEventListener("beforeunload", () => { unloading = true; });
window.addEventListener("pageshow", () => { unloading = false; });

// ---------- utilidades ----------
// crypto.randomUUID() solo existe en contextos seguros (HTTPS/localhost); por HTTP en la VPN
// no está disponible, así que se usa getRandomValues(), que funciona en cualquier contexto.
const uid = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function safeUrl(url) {
  try {
    const u = new URL(url, location.origin);
    return ["http:", "https:"].includes(u.protocol) ? u.href : null;
  } catch {
    return null;
  }
}

function inline(text, citeIds) {
  let s = escapeHtml(text);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, t, u) => {
    const href = safeUrl(u.replace(/&amp;/g, "&"));
    return href ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener">${t}</a>` : t;
  });
  s = s.replace(/(https?:\/\/[^\s<)]+[^\s<).,;:])(?![^<]*<\/a>)/g, (u) => {
    if (/href="/.test(u)) return u;
    return `<a href="${u}" target="_blank" rel="noopener">${u}</a>`;
  });
  // citas [1], [2][3] o [1, 2]
  s = s.replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, (m, nums) =>
    nums
      .split(",")
      .map((n) => n.trim())
      .map((n) => (citeIds.has(Number(n)) ? `<sup class="cite"><a href="#" data-cite="${n}">[${n}]</a></sup>` : `[${n}]`))
      .join(""),
  );
  return s;
}

// Markdown mínimo y seguro (escapa todo antes de transformar).
function renderMarkdown(md, citeIds = new Set()) {
  const lines = String(md || "").replace(/\r/g, "").split("\n");
  const out = [];
  let list = null; // "ul" | "ol"
  let para = [];
  const flushPara = () => {
    if (para.length) out.push(`<p>${para.map((l) => inline(l, citeIds)).join("<br>")}</p>`);
    para = [];
  };
  const closeList = () => {
    if (list) out.push(`</${list}>`);
    list = null;
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^```/.test(line)) {
      flushPara(); closeList();
      const code = [];
      while (++i < lines.length && !/^```/.test(lines[i])) code.push(lines[i]);
      out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flushPara(); closeList();
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i++;
      const rows = [];
      while (i + 1 < lines.length && /^\s*\|.*\|\s*$/.test(lines[i + 1])) rows.push(cells(lines[++i]));
      out.push(
        `<div class="table-scroll" role="region" aria-label="Tabla de la respuesta; desliza para ver más columnas" tabindex="0"><table><thead><tr>${head.map((h) => `<th>${inline(h, citeIds)}</th>`).join("")}</tr></thead><tbody>` +
          rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c, citeIds)}</td>`).join("")}</tr>`).join("") +
          "</tbody></table></div>",
      );
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushPara(); closeList();
      out.push(`<h${h[1].length < 3 ? 3 : 4}>${inline(h[2], citeIds)}</h${h[1].length < 3 ? 3 : 4}>`);
      continue;
    }
    const ul = line.match(/^\s*[-*•]\s+(.*)$/);
    const ol = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    if (ul || ol) {
      flushPara();
      const kind = ul ? "ul" : "ol";
      if (list !== kind) {
        closeList();
        out.push(kind === "ol" && ol[1] !== "1" ? `<ol start="${Number(ol[1])}">` : `<${kind}>`);
        list = kind;
      }
      out.push(`<li>${inline(ul ? ul[1] : ol[2], citeIds)}</li>`);
      continue;
    }
    if (!line.trim()) {
      flushPara();
      // una línea vacía entre elementos de lista no cierra la lista
      const next = lines.slice(i + 1).find((l) => l.trim());
      if (!(list && next && (/^\s*[-*•]\s+/.test(next) || /^\s*\d+[.)]\s+/.test(next)))) closeList();
      continue;
    }
    if (list && /^\s{2,}\S/.test(line)) {
      out[out.length - 1] = out[out.length - 1].replace(/<\/li>$/, `<br>${inline(line.trim(), citeIds)}</li>`);
      continue;
    }
    closeList();
    para.push(line);
  }
  flushPara(); closeList();
  return out.join("");
}

const fmtSize = (n) => (n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

// ---------- dictamen técnico: lógica pura ----------
// Sin DOM ni estado global: tests/test_dictamen_api.py la ejecuta con node entre los marcadores.
// DICTAMEN-PURE-START
const DICT_DIGITS = /^[0-9]+$/;
const dictBlank = (v) => v == null || !String(v).trim();
const dictClean = (v) => (dictBlank(v) ? null : String(v).trim());
const dictText = (v) => (v == null ? "" : String(v));
const dictSentence = (s) => { s = String(s || "").trim(); return s && !/[.!?…]$/.test(s) ? `${s}.` : s; };

// Mismas reglas y mismo orden que app/dictamen/schema.py:faltantes().
function dictFaltantes(fields, data, own) {
  const out = fields.general.filter((f) => f.required !== false && dictBlank(data[f.name])).map((f) => f.name);
  const equipos = (Array.isArray(data.equipos) ? data.equipos : []).filter((e) => e && typeof e === "object");
  if (!equipos.length) out.push("equipos");
  equipos.forEach((eq, i) => {
    for (const f of fields.equipo) if (f.required && dictBlank(eq[f.name])) out.push(`equipo_${i + 1}_${f.name}`);
  });
  for (const f of fields.own) {
    const v = own[f.name];
    let missing;
    if (f.name === "dictamen_inicial") missing = !DICT_DIGITS.test(String(v ?? "").trim());
    else if (f.name === "motivo") missing = dictBlank(v) && equipos.some((eq) => dictBlank(eq.motivo));
    else missing = f.required !== false && dictBlank(v);
    if (missing) out.push(f.name);
  }
  return out;
}

const dictMissingKey = (scope, name, row) => (scope === "eq" ? `equipo_${row + 1}_${name}` : name);
const dictEmptyEquipo = (fields) => Object.fromEntries(fields.equipo.map((f) => [f.name, ""]));

// Estado de la tarjeta a partir de la respuesta de POST /api/dictamen/oficio (200 o 502).
function dictNewCard(body, cfg, id) {
  const fields = cfg.fields;
  const max = cfg.max_equipos || 100;
  const src = body.data && typeof body.data === "object" ? body.data : {};
  const own = body.own && typeof body.own === "object" ? body.own : {};
  const equipos = (Array.isArray(src.equipos) ? src.equipos : []).filter((e) => e && typeof e === "object").slice(0, max);
  return {
    id,
    status: "pending", // pending | sending | interrupted | done | cancelled
    filename: dictText(body.filename),
    manual: !!body.manual,
    mode: body.mode || null,
    pages: body.pages ?? null,
    total_pages: body.total_pages ?? body.pages ?? null,
    elapsed_s: body.elapsed_s ?? null,
    detected: equipos.length,
    fields,
    tecnicos: Array.isArray(cfg.tecnicos) ? cfg.tecnicos : [],
    maxEquipos: max,
    data: {
      ...Object.fromEntries(fields.general.map((f) => [f.name, dictText(src[f.name])])),
      equipos: equipos.map((eq) => Object.fromEntries(fields.equipo.map((f) => [f.name, dictText(eq[f.name])]))),
    },
    own: Object.fromEntries(fields.own.map((f) => [f.name, dictText(own[f.name])])),
    error: "",
    invalid: [], // claves marcadas por el servidor (p. ej. iniciales rechazadas) hasta editarlas
    result: null,
  };
}

// Solo campos conocidos (lista blanca): nada de sobrescribir «equipos» con texto.
function dictApplyEdit(c, scope, name, row, value) {
  const known = (list) => list.some((f) => f.name === name);
  if (scope === "data" && known(c.fields.general)) c.data[name] = value;
  else if (scope === "own" && known(c.fields.own)) c.own[name] = value;
  else if (scope === "eq" && c.data.equipos[row] && known(c.fields.equipo)) c.data.equipos[row][name] = value;
  else return false;
  const key = dictMissingKey(scope, name, row);
  c.invalid = (c.invalid || []).filter((k) => k !== key);
  c.error = "";
  return true;
}

function dictAddEquipo(c) {
  if (c.data.equipos.length >= c.maxEquipos) return false;
  c.data.equipos.push(dictEmptyEquipo(c.fields));
  return true;
}

function dictRemoveEquipo(c, i) {
  if (!(i >= 0 && i < c.data.equipos.length)) return false;
  c.data.equipos.splice(i, 1);
  c.invalid = []; // los índices de las filas cambian
  return true;
}

// Cuerpo de POST /api/dictamen/generar: claves exactas, texto recortado y "" -> null.
function dictPayload(c, conversationId) {
  const pick = (src, list) => Object.fromEntries(list.map((f) => [f.name, dictClean(src[f.name])]));
  return {
    data: { ...pick(c.data, c.fields.general), equipos: c.data.equipos.map((eq) => pick(eq, c.fields.equipo)) },
    own: pick(c.own, c.fields.own),
    conversation_id: conversationId || null,
  };
}

// Como generate.py: int(dictamen_inicial) + i, conservando los ceros a la izquierda escritos.
function dictNumero(inicial, i) {
  const n = String(Number(inicial) + i);
  return inicial.length > 1 && inicial.startsWith("0") ? n.padStart(inicial.length, "0") : n;
}

function dictRango(inicial, n) {
  const s = String(inicial ?? "").trim();
  if (!DICT_DIGITS.test(s) || n < 1 || !Number.isSafeInteger(Number(s) + n)) return "";
  return n === 1 ? `Se numerará como dictamen ${dictNumero(s, 0)}.` : `Se numerarán del ${dictNumero(s, 0)} al ${dictNumero(s, n - 1)}.`;
}

function dictSummary(n, filename) {
  if (!n) return `No detecté equipos en el oficio «${filename}». Agrégalos en la tabla, revisa los demás datos y completa los marcados.`;
  return `Detecté ${n} equipo(s) en el oficio «${filename}». Revisa todos los datos y completa los marcados.`;
}

function dictResultText(r) {
  if (!r || !Array.isArray(r.numeros)) return "Dictamen generado.";
  const [a, b] = r.numeros;
  const nums = a === b ? `dictamen ${a}` : `dictámenes ${a} a ${b}`;
  return `Dictamen generado: ${nums}, iniciales MDTG/${r.iniciales}. Descárgalo abajo.`;
}

// Nota para el historial del LLM (mantiene la alternancia user/assistant).
function dictHistoryNote(c) {
  const base = c.manual
    ? `(Sistema) No se pudo leer el oficio «${c.filename}»; se mostró una tarjeta vacía para capturar a mano los datos del dictamen técnico.`
    : `(Sistema) Detecté ${c.detected} equipo(s) en el oficio «${c.filename}»; se mostró una tarjeta para revisar los datos y generar el dictamen técnico.`;
  if (c.status === "done" && c.result) {
    const [a, b] = c.result.numeros || [];
    return `${base} La persona revisó los datos y se generó el dictamen «${c.result.filename}» (${c.result.equipos} equipo(s), números ${a} a ${b}, iniciales MDTG/${c.result.iniciales}); ya puede descargarlo desde el chat.`;
  }
  if (c.status === "cancelled") return `${base} La persona descartó la tarjeta sin generar el dictamen.`;
  return base;
}

// Al recargar la página: nada queda "en curso" ni se reintenta solo.
function dictOnLoad(entry) {
  if (entry.upload === "reading") {
    entry.upload = "interrupted";
    entry.busyText = "";
    entry.text = `La lectura del oficio «${entry.filename || ""}» se interrumpió. Vuelve a adjuntarlo con 📎.`;
  }
  if (entry.dictamen && entry.dictamen.status === "sending") Object.assign(entry.dictamen, { status: "interrupted", error: "" });
}

function dictErrorText(body, status) {
  const b = body && typeof body === "object" ? body : {};
  const msg = (typeof b.message === "string" && b.message) || (typeof b.detail === "string" && b.detail) || `Error ${status}`;
  const extra = (Array.isArray(b.errors) ? b.errors : []).filter((e) => typeof e === "string" && e && e !== msg);
  return [msg, ...extra].join(" · ");
}
// DICTAMEN-PURE-END

// ---------- persistencia ----------
function save() {
  try {
    const data = { conversationId: state.conversationId, history: state.history, transcript: state.transcript };
    localStorage.setItem(STORE_KEY, JSON.stringify(data, (k, v) => (k === "_el" || k === "_node" ? undefined : v)));
  } catch { /* almacenamiento lleno o deshabilitado */ }
}
function load() {
  try {
    const data = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    if (data) Object.assign(state, data);
  } catch { /* ignorar */ }
}

// ---------- render ----------
function renderEntry(entry) {
  const el = document.createElement("div");
  el.className = `msg ${entry.role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (entry.role === "assistant") {
    // .confirms nunca se reemplaza: así las ediciones y el foco sobreviven al streaming
    bubble.innerHTML = '<div class="main"></div><div class="confirms"></div><div class="tail"></div>';
  }
  el.appendChild(bubble);
  entry._el = bubble;
  updateEntry(entry);
  return el;
}

function citedNumbers(text) {
  const nums = new Set();
  for (const m of String(text || "").matchAll(/\[(\d+(?:\s*,\s*\d+)*)\]/g)) m[1].split(",").forEach((n) => nums.add(Number(n.trim())));
  return nums;
}

function updateEntry(entry) {
  const b = entry._el;
  if (!b) return;
  if (entry.role === "user") {
    b.textContent = entry.text;
    return;
  }
  const sources = entry.sources || [];
  const available = new Set(sources.map((s) => s.n));
  const cited = citedNumbers(entry.text);

  let main = "";
  if (entry.reasoning || (entry.pending && !entry.text)) {
    const label = entry.pending && !entry.text ? (entry.search ? `Buscando en fuentes oficiales: “${escapeHtml(entry.search)}”…` : "Pensando…") : "Ver razonamiento";
    main += `<details class="thinking"><summary>${label}</summary>${entry.reasoning ? `<div class="reasoning">${escapeHtml(entry.reasoning)}</div>` : ""}</details>`;
  }
  // entry.plain: texto con nombres de archivo o datos del oficio; se escapa sin interpretar markdown
  const body = entry.plain ? (entry.text ? `<p>${escapeHtml(entry.text).replace(/\n/g, "<br>")}</p>` : "") : renderMarkdown(entry.text, available);
  main += `<div class="content${entry.pending ? " typing" : ""}">${body}</div>`;
  if (entry.busyText) main += `<div class="tool-status"><div class="spinner"></div>${escapeHtml(entry.busyText)}</div>`;
  for (const t of entry.tools || []) {
    if (t.status === "running") main += `<div class="tool-status"><div class="spinner"></div>Generando «${escapeHtml(t.title)}»…</div>`;
    if (t.status === "error") main += `<div class="tool-status err">No se pudo generar «${escapeHtml(t.title)}»: ${escapeHtml(t.message)}</div>`;
  }
  const mainEl = b.querySelector(".main");
  const wasOpen = mainEl.querySelector("details.thinking")?.open;
  mainEl.innerHTML = main;
  if (wasOpen) mainEl.querySelector("details.thinking")?.setAttribute("open", "");

  const confirmsEl = b.querySelector(".confirms");
  for (const c of entry.confirms || []) {
    if (!c._node) c._node = buildConfirmCard(entry, c);
    if (c._node.parentNode !== confirmsEl) confirmsEl.appendChild(c._node);
  }
  if (entry.dictamen) {
    if (!entry.dictamen._node) entry.dictamen._node = buildDictamenCard(entry, entry.dictamen);
    if (entry.dictamen._node.parentNode !== confirmsEl) confirmsEl.appendChild(entry.dictamen._node);
  }

  let tail = "";
  for (const d of entry.documents || []) {
    tail += `<div class="doc-card"><div class="icon">${escapeHtml((d.filename.split(".").pop() || "DOC").toUpperCase())}</div>
      <div class="info"><strong>${escapeHtml(d.title)}</strong><span>${escapeHtml(d.filename)} · ${fmtSize(d.size)}</span></div>
      <a class="primary" href="${escapeHtml(d.url)}" download="${escapeHtml(d.filename)}">Descargar</a></div>`;
  }
  for (const n of entry.notices || []) tail += `<div class="alert ${n.kind === "warning" ? "warn" : ""}">${escapeHtml(n.message)}</div>`;
  if (sources.length && !entry.pending) {
    const anyCited = [...cited].some((n) => available.has(n));
    const items = sources
      .map((s) => {
        const isCited = !anyCited || cited.has(s.n);
        const meta = [s.document, s.version, s.page ? `p. ${s.page}` : null].filter(Boolean).join(" · ");
        const href = s.url ? safeUrl(s.url) : null;
        const title = href ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener">${escapeHtml(s.heading)}</a>` : escapeHtml(s.heading);
        return `<li id="src-${entry.id}-${s.n}" class="${isCited ? "cited" : ""}" value="${s.n}">${title}<div class="meta">${escapeHtml(meta)}</div></li>`;
      })
      .join("");
    const nCited = [...cited].filter((n) => available.has(n)).length;
    const label = anyCited ? `Fuentes citadas (${nCited} de ${sources.length} consultadas)` : `Fuentes consultadas (${sources.length})`;
    tail += `<details class="sources"${anyCited ? " open" : ""}><summary>${label}</summary><ol>${items}</ol></details>`;
  }
  b.querySelector(".tail").innerHTML = tail;

  b.querySelectorAll("a[data-cite]").forEach((a) =>
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      const li = b.querySelector(`#src-${entry.id}-${a.dataset.cite}`);
      if (li) {
        li.closest("details").open = true;
        li.scrollIntoView({ behavior: "smooth", block: "center" });
        li.querySelector("a")?.focus();
      }
    }),
  );
}

// ---------- tarjeta de confirmación de documento ----------
const LONG_FIELDS = /hechos|peticion|descripcion|datos_adicionales|tramite/;

function buildConfirmCard(entry, c) {
  const form = document.createElement("form");
  form.className = "confirm-card";
  const fields = c.fields
    .map((f) => {
      const id = `f-${entry.id}-${f.name}`;
      const raw = c.data[f.name];
      const value = Array.isArray(raw) ? raw.join("\n") : raw ?? "";
      const req = f.required ? " required" : "";
      let control;
      if (f.enum) {
        control = `<select id="${id}" name="${f.name}"${req}><option value=""></option>${f.enum
          .map((o) => `<option${o === value ? " selected" : ""}>${escapeHtml(o)}</option>`)
          .join("")}</select>`;
      } else if (f.type === "array" || LONG_FIELDS.test(f.name)) {
        control = `<textarea id="${id}" name="${f.name}" rows="${f.type === "array" ? 3 : 2}"${req}>${escapeHtml(value)}</textarea>`;
      } else {
        const type = f.name === "fecha" ? "date" : f.name.includes("correo") ? "email" : "text";
        control = `<input id="${id}" name="${f.name}" type="${type}" value="${escapeHtml(value)}"${req} />`;
      }
      const hint = f.type === "array" ? " (uno por línea)" : "";
      return `<label class="field${f.required ? " req" : ""}" for="${id}"><span>${escapeHtml(f.label)}${hint}</span>${control}</label>`;
    })
    .join("");
  form.innerHTML = `
    <div class="confirm-head"><strong>Revisa los datos: ${escapeHtml(c.title)}</strong>
      <span>Corrige lo que haga falta. Los datos se envían al servicio que genera el documento solo cuando presionas «Generar documento».</span></div>
    <div class="fields">${fields}</div>
    <div class="confirm-msg" role="alert"></div>
    <div class="confirm-actions">
      <button type="button" class="ghost cancel">Cancelar</button>
      <button type="submit" class="primary">Generar documento</button>
    </div>`;

  const msg = form.querySelector(".confirm-msg");
  const readField = (f) => {
    const el = form.elements[f.name];
    if (!el) return undefined;
    if (f.type === "array") return el.value.split("\n").map((x) => x.trim()).filter(Boolean);
    return el.value.trim();
  };
  const setStatus = (status, text) => {
    c.status = status;
    form.classList.toggle("done", status === "done");
    form.classList.toggle("cancelled", status === "cancelled");
    form.querySelectorAll("input, select, textarea, button").forEach((el) => (el.disabled = status !== "pending"));
    msg.className = `confirm-msg ${status === "error" ? "err" : ""}`;
    msg.textContent = text || "";
  };
  form.addEventListener("input", () => {
    c.fields.forEach((f) => (c.data[f.name] = readField(f)));
    save();
  });
  form.querySelector(".cancel").addEventListener("click", () => {
    setStatus("cancelled", "Generación cancelada.");
    save();
  });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    c.fields.forEach((f) => (c.data[f.name] = readField(f)));
    const btn = form.querySelector('button[type="submit"]');
    btn.disabled = true;
    btn.textContent = "Generando…";
    msg.textContent = "";
    const myEpoch = epoch;
    try {
      const res = await fetch("/api/documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: c.type, data: c.data, conversation_id: state.conversationId }),
      });
      const body = await res.json().catch(() => ({}));
      if (myEpoch !== epoch) return; // la persona empezó otra conversación
      if (!res.ok) {
        c.status = "pending";
        setStatus("pending");
        msg.className = "confirm-msg err";
        msg.textContent = [body.message || `Error ${res.status}`, ...(body.errors || [])].join(" · ");
        btn.textContent = "Generar documento";
        return;
      }
      setStatus("done", "Datos confirmados y enviados.");
      entry.documents.push(body);
      state.history.push({
        role: "assistant",
        content: `(Sistema) La persona confirmó los datos y se generó el documento «${body.title}» (${body.filename}); ya puede descargarlo desde el chat.`,
      });
      updateEntry(entry);
      save();
    } catch (err) {
      setStatus("pending");
      msg.className = "confirm-msg err";
      msg.textContent = `No se pudo contactar el servidor (${err.message}).`;
      btn.textContent = "Generar documento";
    }
  });
  if (c.status && c.status !== "pending") setStatus(c.status, c.status === "done" ? "Datos confirmados y enviados." : "Generación cancelada.");
  else c.status = "pending";
  return form;
}

// ---------- tarjeta de dictamen técnico ----------
const DICT_STATUS = {
  sending: "Generando dictamen…",
  interrupted:
    "La página se recargó mientras se generaba el dictamen y no se sabe si terminó. Si no lo descargaste, desbloquea la tarjeta para intentarlo de nuevo.",
  cancelled: "Tarjeta descartada.",
};
let dictSaveTimer = null;
const dictSaveSoon = () => {
  clearTimeout(dictSaveTimer);
  dictSaveTimer = setTimeout(save, 250);
};

function dictUpdateNote(c) {
  const note = state.history.find((m) => m.ref === c.id && m.role === "assistant");
  if (note) note.content = dictHistoryNote(c);
}

function buildDictamenCard(entry, c) {
  const form = document.createElement("form");
  form.className = "confirm-card dictamen-card";
  form.noValidate = true;
  const pid = `dc-${entry.id}`;
  const f = c.fields;
  const tecnicos = c.tecnicos.filter((t) => t && t.iniciales);
  const listId = `${pid}-tecnicos`;

  const control = (scope, field, opts = {}) => {
    const id = `${pid}-${scope}-${field.name}`;
    const value = scope === "data" ? c.data[field.name] : c.own[field.name];
    const attrs = [
      `id="${escapeHtml(id)}"`,
      `data-scope="${scope}"`,
      `data-name="${escapeHtml(field.name)}"`,
      `value="${escapeHtml(value)}"`,
      field.example ? `placeholder="Ej. ${escapeHtml(field.example)}"` : "",
      opts.numeric ? 'inputmode="numeric" pattern="[0-9]*"' : "",
      opts.list ? `list="${escapeHtml(listId)}"` : "",
      'autocomplete="off"',
      'maxlength="500"',
    ].join(" ");
    const help = [field.help, opts.extraHelp].filter(Boolean).map(escapeHtml).join(" ");
    return `<label class="field${field.required ? " req" : ""}${opts.wide ? " wide" : ""}" for="${escapeHtml(id)}">
      <span>${escapeHtml(field.label)}</span><input ${attrs} />
      ${help ? `<small class="hint">${help}</small>` : ""}${opts.after || ""}</label>`;
  };

  const tecHelp = tecnicos.length
    ? `Técnicos: ${tecnicos.map((t) => (t.nombre ? `${t.nombre} (${t.iniciales})` : t.iniciales)).join(", ")}. No escribas «MDTG»: ya va en la plantilla.`
    : "No escribas «MDTG»: ya va en la plantilla.";
  const ownHtml = f.own
    .map((field) => {
      if (field.name === "dictamen_inicial") return control("own", field, { numeric: true, after: '<small class="hint rango"></small>' });
      if (field.name === "iniciales") return control("own", field, { list: true, extraHelp: tecHelp });
      return control("own", field);
    })
    .join("");
  const origin = c.manual
    ? "No se pudo leer el oficio: captura los datos a mano."
    : `Datos extraídos de «${c.filename}»${c.mode ? ` (${c.mode === "imagen" ? "como imagen" : "como texto"}${c.pages ? `, ${c.pages} pág.` : ""}${c.elapsed_s != null ? `, ${Number(c.elapsed_s).toFixed(1)} s` : ""})` : ""}. Compáralos con el oficio: el modelo puede confundir letras y números en series e inventarios.` +
      (c.total_pages > c.pages ? ` Solo se leyeron las primeras ${c.pages} de ${c.total_pages} páginas: agrega a mano los equipos que falten.` : "");

  form.innerHTML = `
    <div class="confirm-head"><strong>Dictamen técnico: revisa los datos</strong><span>${escapeHtml(origin)}</span></div>
    <fieldset><legend>Oficio recibido</legend><div class="fields">${f.general.map((field) => control("data", field)).join("")}</div></fieldset>
    <fieldset><legend>Datos del dictamen</legend><div class="fields">${ownHtml}</div>
      <datalist id="${escapeHtml(listId)}">${tecnicos
        .map((t) => `<option value="${escapeHtml(t.iniciales)}">${escapeHtml(t.nombre || t.iniciales)}</option>`)
        .join("")}</datalist></fieldset>
    <fieldset><legend>Equipos <span class="count"></span></legend>
      <div class="equipos-wrap"><table class="equipos"><thead><tr><th class="n">#</th>${f.equipo
        .map((field) => `<th${field.required ? ' class="req"' : ""} title="${escapeHtml(field.help || "")}">${escapeHtml(field.label)}</th>`)
        .join("")}<th><span class="sr-only">Quitar</span></th></tr></thead><tbody></tbody></table>
        <div class="equipos-empty">No hay equipos. Agrega al menos uno.</div></div>
      <button type="button" class="ghost add">+ Agregar equipo</button></fieldset>
    <div class="confirm-msg" role="status" aria-live="polite"></div>
    <div class="confirm-actions">
      <button type="button" class="ghost unlock">Desbloquear para reintentar</button>
      <button type="button" class="ghost fix">Corregir datos y volver a generar</button>
      <button type="button" class="ghost cancel">Descartar</button>
      <button type="submit" class="primary submit">Generar dictamen</button>
    </div>`;

  const $c = (sel) => form.querySelector(sel);
  const tbody = $c("tbody");
  const msg = $c(".confirm-msg");
  const btn = { add: $c(".add"), unlock: $c(".unlock"), fix: $c(".fix"), cancel: $c(".cancel"), submit: $c(".submit") };

  const renderRows = () => {
    tbody.innerHTML = c.data.equipos
      .map(
        (eq, i) => `<tr><td class="n">${i + 1}</td>${f.equipo
          .map((field) => {
            const ph = field.name === "numero_inventario" ? "S/N" : field.name === "motivo" ? "El general" : "";
            return `<td><input data-scope="eq" data-row="${i}" data-name="${escapeHtml(field.name)}" value="${escapeHtml(eq[field.name])}" aria-label="Equipo ${i + 1}: ${escapeHtml(field.label)}" placeholder="${ph}" autocomplete="off" maxlength="500" /></td>`;
          })
          .join("")}<td><button type="button" class="ghost remove" data-remove="${i}" aria-label="Quitar equipo ${i + 1}" title="Quitar equipo">✕</button></td></tr>`,
      )
      .join("");
  };

  const sync = () => {
    const editable = c.status === "pending";
    const missing = new Set(editable ? [...dictFaltantes(f, c.data, c.own), ...(c.invalid || [])] : []);
    form.classList.toggle("is-locked", !editable);
    form.classList.toggle("is-done", c.status === "done");
    form.classList.toggle("is-cancelled", c.status === "cancelled");
    form.querySelectorAll("input[data-scope]").forEach((el) => {
      const miss = missing.has(dictMissingKey(el.dataset.scope, el.dataset.name, Number(el.dataset.row)));
      el.classList.toggle("missing", miss);
      if (miss) el.setAttribute("aria-invalid", "true");
      else el.removeAttribute("aria-invalid");
      el.disabled = !editable;
    });
    form.querySelectorAll("button").forEach((b) => (b.disabled = !editable));
    btn.add.disabled = !editable || c.data.equipos.length >= c.maxEquipos;
    btn.unlock.hidden = c.status !== "interrupted";
    btn.fix.hidden = c.status !== "done";
    btn.unlock.disabled = btn.fix.disabled = false;
    btn.cancel.hidden = btn.submit.hidden = !["pending", "sending"].includes(c.status);
    btn.submit.textContent = c.status === "sending" ? "Generando…" : "Generar dictamen";
    $c(".count").textContent = `(${c.data.equipos.length})`;
    const empty = $c(".equipos-empty");
    empty.hidden = c.data.equipos.length > 0;
    empty.classList.toggle("missing", missing.has("equipos"));
    const rango = $c(".rango");
    if (rango) rango.textContent = dictRango(c.own.dictamen_inicial, c.data.equipos.length);
    let text = "";
    let cls = "";
    if (c.status === "done") text = dictResultText(c.result);
    else if (c.status === "interrupted") [text, cls] = [c.error || DICT_STATUS.interrupted, "warn"];
    else if (DICT_STATUS[c.status]) text = DICT_STATUS[c.status];
    else if (c.error) [text, cls] = [c.error, "err"];
    else if (missing.size) [text, cls] = [`Faltan ${missing.size} dato(s): están marcados en rojo.`, "pending"];
    msg.className = `confirm-msg ${cls}`;
    msg.textContent = text;
  };

  const focusFirstMissing = () => {
    const el = form.querySelector("input.missing") || (c.data.equipos.length ? null : btn.add);
    if (el) {
      el.focus();
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  };

  form.addEventListener("input", (ev) => {
    const el = ev.target;
    if (c.status !== "pending" || !el.dataset || !el.dataset.scope) return;
    if (dictApplyEdit(c, el.dataset.scope, el.dataset.name, Number(el.dataset.row), el.value)) {
      sync();
      dictSaveSoon();
    }
  });
  // Enter en un campo no envía la tarjeta (solo el botón «Generar dictamen»)
  form.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && ev.target.tagName === "INPUT") ev.preventDefault();
  });
  form.addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b || b.disabled || b.type === "submit") return;
    if (b.dataset.remove != null) {
      if (c.status !== "pending" || !dictRemoveEquipo(c, Number(b.dataset.remove))) return;
      renderRows();
      sync();
      save();
      (tbody.querySelector(`tr:nth-child(${Math.min(Number(b.dataset.remove) + 1, c.data.equipos.length)}) input`) || btn.add).focus();
    } else if (b === btn.add) {
      if (c.status !== "pending" || !dictAddEquipo(c)) return;
      renderRows();
      sync();
      save();
      tbody.querySelector("tr:last-child input")?.focus();
    } else if (b === btn.cancel) {
      if (c.status !== "pending") return;
      c.status = "cancelled";
      dictUpdateNote(c);
      sync();
      save();
    } else if (b === btn.unlock || b === btn.fix) {
      if (!["interrupted", "done"].includes(c.status)) return;
      c.status = "pending";
      c.error = "";
      sync();
      save();
    }
  });
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    generateDictamen(entry, c, { sync, focusFirstMissing, form });
  });

  renderRows();
  sync();
  return form;
}

async function generateDictamen(entry, c, ui) {
  if (c.status !== "pending") return; // evita el doble envío
  const missing = dictFaltantes(c.fields, c.data, c.own);
  if (missing.length) {
    c.error = `Completa los ${missing.length} dato(s) marcados en rojo antes de generar.`;
    ui.sync();
    ui.focusFirstMissing();
    return;
  }
  const myEpoch = epoch;
  clearTimeout(dictSaveTimer);
  c.status = "sending"; // se guarda: al recargar no se reintenta solo
  c.error = "";
  ui.sync();
  save();
  let res;
  let body = {};
  try {
    res = await fetch("/api/dictamen/generar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(dictPayload(c, state.conversationId)),
    });
    body = await res.json().catch(() => ({}));
  } catch (err) {
    if (myEpoch !== epoch || unloading) return;
    // La petición pudo llegar al servidor: no se sabe si el dictamen se generó.
    c.status = "interrupted";
    c.error = `No se pudo confirmar la respuesta del servidor (${err.message}). Si no aparece la descarga, desbloquea la tarjeta para intentarlo de nuevo.`;
    ui.sync();
    save();
    return;
  }
  if (myEpoch !== epoch) return; // la persona empezó otra conversación
  if (!res.ok) {
    c.status = "pending";
    c.error = dictErrorText(body, res.status);
    c.invalid = [...(Array.isArray(body.faltantes) ? body.faltantes : []), ...(body.field ? [String(body.field)] : [])];
    ui.sync();
    save();
    if (c.invalid.length) ui.focusFirstMissing();
    return;
  }
  c.status = "done";
  c.result = { numeros: body.numeros, iniciales: body.iniciales, filename: body.filename, equipos: c.data.equipos.length };
  if (body.iniciales) {
    c.own.iniciales = String(body.iniciales); // como quedó en el documento (sin MDTG, normalizado)
    const el = ui.form.querySelector('input[data-scope="own"][data-name="iniciales"]');
    if (el) el.value = c.own.iniciales;
  }
  entry.documents = entry.documents || [];
  entry.documents.push({ id: body.id, title: body.title, filename: body.filename, mime_type: body.mime_type, size: body.size, url: body.url });
  dictUpdateNote(c);
  updateEntry(entry);
  ui.sync();
  save();
  scrollDown();
}

async function uploadOficio(file) {
  if (!dictamenCfg || state.busy || voice?.active || !file) return;
  const name = file.name || "oficio";
  $("#welcome").style.display = "none";
  const userEntry = { id: uid(), role: "user", text: `📎 ${name}` };
  const bot = {
    id: uid(), role: "assistant", text: "", plain: true, sources: [], documents: [], tools: [], confirms: [], notices: [],
    filename: name, upload: "reading", busyText: "Leyendo oficio… (puede tardar hasta 1 min)",
  };
  state.transcript.push(userEntry, bot);
  $("#messages").append(renderEntry(userEntry), renderEntry(bot));
  scrollDown(true);
  const finish = (text) => {
    bot.upload = "done";
    bot.busyText = "";
    bot.text = text;
    updateEntry(bot);
    scrollDown();
    save();
  };
  if (file.size > dictamenCfg.max_upload_mb * 1024 * 1024) {
    finish(`No puedo leer «${name}»: el archivo supera el máximo de ${dictamenCfg.max_upload_mb} MB.`);
    return;
  }
  const myEpoch = epoch;
  const controller = new AbortController();
  state.controller = controller; // «Detener» cancela la lectura
  setBusy(true);
  save();
  let res = null;
  let body = {};
  let failure = null;
  try {
    const form = new FormData();
    form.append("file", file, name);
    if (state.conversationId) form.append("conversation_id", state.conversationId);
    res = await fetch("/api/dictamen/oficio", { method: "POST", body: form, signal: controller.signal });
    body = await res.json().catch(() => ({}));
  } catch (err) {
    failure = err;
  } finally {
    if (state.controller === controller) {
      state.controller = null;
      setBusy(false);
    }
  }
  if (myEpoch !== epoch) return; // «Nueva conversación» durante la lectura: no tocar la nueva
  if (failure && unloading) return; // se recarga la página: al volver se marca como interrumpida
  if (failure) {
    finish(failure.name === "AbortError" ? "Lectura del oficio cancelada." : `No se pudo contactar el servidor (${failure.message}).`);
    return;
  }
  if (res.ok || (res.status === 502 && body && body.data)) {
    const manual = !res.ok;
    const c = dictNewCard({ ...body, filename: body.filename || name, manual }, dictamenCfg, uid());
    bot.dictamen = c;
    state.history.push(
      { role: "user", content: `(Adjunto) oficio «${c.filename}»`, ref: c.id },
      { role: "assistant", content: dictHistoryNote(c), ref: c.id },
    );
    finish(
      manual
        ? `No pude leer el oficio «${c.filename}». ${dictSentence(dictErrorText(body, res.status))} Abajo tienes la tarjeta vacía para capturar los datos.`
        : dictSummary(c.detected, c.filename),
    );
    return;
  }
  finish(`No pude procesar «${name}»: ${dictSentence(dictErrorText(body, res.status))}`);
}

function renderAll() {
  const box = $("#messages");
  box.innerHTML = "";
  state.transcript.forEach((e) => box.appendChild(renderEntry(e)));
  $("#welcome").style.display = state.transcript.length ? "none" : "";
  if (state.transcript.length) scrollDown(true);
  else $("#chat").scrollTop = 0;
}

function scrollDown(force = false) {
  if (!force && document.activeElement?.closest(".confirm-card")) return;
  const c = $("#chat");
  const near = c.scrollHeight - c.scrollTop - c.clientHeight < 160;
  if (force || near) c.scrollTop = c.scrollHeight;
}

// ---------- envío ----------
function setBusy(busy) {
  state.busy = busy;
  const btn = $("#send");
  btn.textContent = busy ? "Detener" : "Enviar";
  btn.setAttribute("aria-label", busy ? "Detener respuesta" : "Enviar mensaje");
  btn.classList.toggle("stop", busy);
  syncVoiceControls();
}

async function send(text) {
  text = text.trim();
  if (!text || state.busy || voice?.active) return;
  voice?.cancel({ silent: true });
  $("#welcome").style.display = "none";
  const userEntry = { id: uid(), role: "user", text };
  state.transcript.push(userEntry);
  state.history.push({ role: "user", content: text });
  $("#messages").appendChild(renderEntry(userEntry));

  const bot = { id: uid(), role: "assistant", text: "", reasoning: "", sources: [], documents: [], tools: [], confirms: [], notices: [], pending: true };
  state.transcript.push(bot);
  $("#messages").appendChild(renderEntry(bot));
  scrollDown(true);
  setBusy(true);
  save();

  const myEpoch = epoch;
  state.controller = new AbortController();
  const controller = state.controller;
  let frame = null;
  const refresh = () => {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = null; updateEntry(bot); scrollDown(); });
  };
  let finished = false;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: state.history.slice(-20).map(({ ref, ...m }) => m), conversation_id: state.conversationId }),
      signal: state.controller.signal,
    });
    if (!res.ok || !res.body) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.message || `Error ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const ev = { event: "message", data: "" };
        for (const line of raw.split("\n")) {
          if (line.startsWith("event:")) ev.event = line.slice(6).trim();
          else if (line.startsWith("data:")) ev.data += line.slice(5).trim();
        }
        let data = {};
        try { data = JSON.parse(ev.data || "{}"); } catch { continue; }
        switch (ev.event) {
          case "meta": state.conversationId = data.conversation_id; break;
          case "search": bot.search = data.query; break;
          case "sources": bot.sources = data.sources || []; break;
          case "reasoning": bot.reasoning += data.text; break;
          case "token": bot.text += data.text; break;
          case "tool_start": bot.tools.push({ title: data.title, status: "running" }); break;
          case "tool_error": {
            const t = bot.tools.find((x) => x.status === "running");
            if (t) Object.assign(t, { status: "error", message: data.message });
            break;
          }
          case "document": {
            const t = bot.tools.find((x) => x.status === "running");
            if (t) t.status = "done";
            bot.documents.push(data);
            break;
          }
          case "confirm_document": bot.confirms.push({ ...data, status: "pending" }); break;
          case "warning": bot.notices.push({ kind: "warning", message: data.message }); break;
          case "error": bot.notices.push({ kind: "error", message: data.message }); break;
          case "done":
            finished = true;
            state.history.push(...(data.messages || []));
            break;
        }
        refresh();
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") bot.notices.push({ kind: "error", message: `No se pudo completar la respuesta (${err.message}).` });
  } finally {
    if (myEpoch !== epoch) {
      // «Nueva conversación» con la respuesta en curso: no escribir en la conversación nueva
      if (state.controller === controller) {
        state.controller = null;
        setBusy(false);
      }
      return;
    }
    if (!finished) {
      // conserva lo que alcanzó a responder para que el historial siga siendo coherente
      state.history.push({ role: "assistant", content: bot.text || "(respuesta interrumpida)" });
    }
    bot.pending = false;
    bot.tools = bot.tools.filter((t) => t.status !== "done");
    setBusy(false);
    state.controller = null;
    updateEntry(bot);
    scrollDown();
    save();
  }
}

// ---------- inicio ----------
function suggestionDetails(question) {
  if (/carta poder/i.test(question)) return {
    title: "Documentos", description: "Prepara una carta poder",
    icon: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
  };
  if (/e\.firma/i.test(question)) return {
    title: "e.firma", description: "Consulta los requisitos",
    icon: '<circle cx="8" cy="9" r="5"/><path d="m12 13 8 8m-4-4 3-3m-1 5 3-3"/>',
  };
  if (/vacaciones/i.test(question)) return {
    title: "Tus derechos", description: "Conoce tus días de vacaciones",
    icon: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18m-13 5 2 2 4-4"/>',
  };
  if (/\bRFC\b/i.test(question)) return {
    title: "Trámites", description: "Cómo inscribirte en el RFC",
    icon: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8" cy="10" r="2"/><path d="M5 16c0-3 6-3 6 0m3-7h4m-4 4h4m-4 4h3"/>',
  };
  return { title: "Otra consulta", description: question,
    icon: '<path d="M21 11a8 8 0 0 1-8 8H7l-4 3V7a4 4 0 0 1 4-4h6a8 8 0 0 1 8 8Z"/><path d="M7 8h9M7 12h6"/>',
  };
}

async function loadConfig() {
  try {
    const cfg = await (await fetch("/api/config")).json();
    if (cfg.demo) {
      const notice = $("#demo-notice");
      notice.hidden = false;
      $("#demo-details").textContent = `El chat se guarda en este navegador. Los documentos se eliminan del servidor después de ${cfg.document_retention_hours} horas; quien tenga el enlace puede descargarlos mientras estén vigentes.`;
    }
    const sug = $("#suggestions");
    sug.innerHTML = "";
    (cfg.suggestions || []).forEach((q) => {
      const b = document.createElement("button");
      b.type = "button";
      const details = suggestionDetails(q);
      b.innerHTML = `<span class="suggestion-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${details.icon}</svg></span><span class="suggestion-copy"><strong>${escapeHtml(details.title)}</strong><small>${escapeHtml(details.description)}</small></span>`;
      b.setAttribute("aria-label", `${details.title}: ${q}`);
      b.title = q;
      b.addEventListener("click", () => send(q));
      sug.appendChild(b);
    });
    syncVoiceControls();
    dictamenCfg = cfg.dictamen && cfg.dictamen.enabled ? cfg.dictamen : null;
    $("#attach").hidden = !dictamenCfg;
    $("#attach-input").accept = dictamenCfg ? dictamenCfg.accept || "" : "";
    $("#doc-types").innerHTML =
      (cfg.documents || []).map((d) => `<li title="${escapeHtml(d.description)}">${escapeHtml(d.title)}</li>`).join("") +
      (dictamenCfg ? '<li title="Adjunta el oficio con el botón 📎 (PDF, Word o foto)">Dictamen técnico de equipos de cómputo (adjunta el oficio con 📎)</li>' : "");
    $("#document-count").textContent = (cfg.documents || []).length + (dictamenCfg ? 1 : 0);
    const src = await (await fetch("/api/sources")).json();
    $("#source-count").textContent = (src.sources || []).length;
    $("#source-list").innerHTML =
      (src.sources || []).map((s) => `<li><a href="${escapeHtml(safeUrl(s.url) || "#")}" target="_blank" rel="noopener">${escapeHtml(s.title)}</a>${s.version ? ` <span class="muted">(${escapeHtml(s.version)})</span>` : ""}</li>`).join("") ||
      '<li class="muted">Todavía no hay fuentes disponibles.</li>';
  } catch {
    $("#source-list").innerHTML = '<li class="muted">No se pudo cargar la configuración.</li>';
  }
  try {
    const h = await (await fetch("/api/health")).json();
    const st = $("#status");
    st.className = `status ${h.llm.ok ? "ok" : "bad"}`;
    st.textContent = h.llm.ok ? "Disponible" : "No disponible";
    st.title = h.llm.ok ? `${h.llm.model} · ${h.index.chunks.toLocaleString("es-MX")} fragmentos` : h.llm.detail;
  } catch {
    $("#status").className = "status bad";
    $("#status").textContent = "Sin conexión";
  }
}

// El teclado de Safari puede reducir solo el viewport visual. Ajusta la altura
// del chat a ese espacio sin bloquear el zoom ni desplazar el formulario entero.
function setupViewport(autosize) {
  const viewport = window.visualViewport;
  let frame = null;
  const resize = () => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      if (viewport && Math.abs(viewport.scale - 1) > 0.01) return;
      const chat = $("#chat");
      const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 40;
      if (viewport) document.documentElement.style.setProperty("--app-height", `${viewport.height}px`);
      autosize();
      const active = document.activeElement;
      if (active?.closest(".confirm-card")) {
        const field = active.getBoundingClientRect();
        const area = chat.getBoundingClientRect();
        if (field.top < area.top + 16) chat.scrollTop -= area.top + 16 - field.top;
        else if (field.bottom > area.bottom - 16) chat.scrollTop += field.bottom - area.bottom + 16;
      } else if (atBottom) {
        scrollDown(true);
      }
    });
  };
  window.addEventListener("resize", resize);
  viewport?.addEventListener("resize", resize);
  resize();
}

function init() {
  load();
  state.transcript.forEach((e) => { e.pending = false; dictOnLoad(e); });
  renderAll();
  save();
  loadConfig();

  const input = $("#input");
  const touchInput = () => matchMedia("(pointer: coarse)").matches;
  const autosize = () => {
    input.placeholder = matchMedia("(max-width: 640px)").matches
      ? "Escribe…"
      : (voiceSupported ? "Escribe o dicta…" : "Escribe tu pregunta…");
    input.style.height = "auto";
    const maxHeight = parseFloat(getComputedStyle(input).maxHeight) || 160;
    input.style.height = `${Math.min(input.scrollHeight, maxHeight)}px`;
  };
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  voiceSupported = window.isSecureContext && typeof Recognition === "function";
  voice = new VoiceDictation({
    Recognition: voiceSupported ? Recognition : null,
    onText: (text) => { input.value = text; autosize(); },
    onState: ({ phase, message }) => {
      voicePhase = phase;
      $("#voice-status").textContent = message;
      $("#voice-feedback").hidden = !message;
      $("#voice-feedback").classList.toggle("error", phase === "error");
      syncVoiceControls();
    },
  });
  if (!voiceSupported) {
    voiceUnavailableReason = window.isSecureContext
      ? "Este navegador no ofrece dictado por voz. Puedes escribir o usar el micrófono de tu teclado, si lo tiene."
      : "El dictado necesita una conexión HTTPS. En esta página puedes seguir escribiendo.";
  }
  syncVoiceControls();
  $("#dictate").addEventListener("click", () => {
    if (state.busy || !voiceSupported) return;
    if (voice.active) voice.stop();
    else voice.start(input.value, input.selectionStart, input.selectionEnd);
  });
  $("#voice-cancel").addEventListener("click", () => voice.cancel());
  document.addEventListener("visibilitychange", () => { if (document.hidden) voice.interrupt(); });
  window.addEventListener("pagehide", () => voice.interrupt());
  setupViewport(autosize);
  input.addEventListener("input", () => {
    autosize();
    if (!voice.active) voice.cancel({ silent: true });
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      // En el teclado móvil, Enter añade una línea. El botón envía el mensaje.
      if (touchInput() && !e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      if (!state.busy) $("#composer").requestSubmit();
    }
  });
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    if (state.busy) { state.controller?.abort(); return; }
    if (voice.active) return;
    const text = input.value;
    input.value = "";
    autosize();
    send(text);
  });
  $("#attach").addEventListener("click", () => {
    if (!state.busy && !voice.active) $("#attach-input").click();
  });
  $("#attach-input").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = ""; // permite volver a elegir el mismo archivo
    if (file) uploadOficio(file);
  });
  // Arrastrar y soltar un oficio en cualquier parte de la página
  const hasFiles = (e) => Array.from((e.dataTransfer && e.dataTransfer.types) || []).includes("Files");
  let dragDepth = 0;
  const dropOff = () => { dragDepth = 0; document.body.classList.remove("dropping"); };
  window.addEventListener("dragenter", (e) => {
    if (!dictamenCfg || state.busy || voice.active || !hasFiles(e)) return;
    dragDepth += 1;
    document.body.classList.add("dropping");
  });
  window.addEventListener("dragleave", (e) => {
    if (!dictamenCfg || !hasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) dropOff();
  });
  window.addEventListener("dragover", (e) => {
    if (!dictamenCfg || !hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = state.busy || voice.active ? "none" : "copy";
    if (state.busy || voice.active) dropOff();
  });
  window.addEventListener("drop", (e) => {
    if (!dictamenCfg || !hasFiles(e)) return;
    e.preventDefault();
    dropOff();
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) uploadOficio(file);
  });
  $("#new-chat").addEventListener("click", () => {
    voice.cancel({ silent: true });
    epoch += 1;
    if (state.busy) state.controller?.abort();
    Object.assign(state, { conversationId: null, history: [], transcript: [] });
    localStorage.removeItem(STORE_KEY);
    renderAll();
    if (!touchInput()) input.focus();
  });
  if (!touchInput()) input.focus();
}

init();

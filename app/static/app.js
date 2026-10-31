// Ventanilla — frontend del chat (sin dependencias).
const $ = (sel) => document.querySelector(sel);
const STORE_KEY = "ventanilla.conversation.v1";

const state = {
  conversationId: null,
  history: [], // mensajes en formato LLM (user/assistant/tool) que se reenvían al backend
  transcript: [], // lo que se dibuja: {role, text, reasoning, sources, documents, notices, search}
  busy: false,
  controller: null,
};

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
  main += `<div class="content${entry.pending ? " typing" : ""}">${renderMarkdown(entry.text, available)}</div>`;
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
    try {
      const res = await fetch("/api/documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: c.type, data: c.data, conversation_id: state.conversationId }),
      });
      const body = await res.json().catch(() => ({}));
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

function renderAll() {
  const box = $("#messages");
  box.innerHTML = "";
  state.transcript.forEach((e) => box.appendChild(renderEntry(e)));
  $("#welcome").style.display = state.transcript.length ? "none" : "";
  scrollDown(true);
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
}

async function send(text) {
  text = text.trim();
  if (!text || state.busy) return;
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

  state.controller = new AbortController();
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
      body: JSON.stringify({ messages: state.history.slice(-20), conversation_id: state.conversationId }),
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
async function loadConfig() {
  try {
    const cfg = await (await fetch("/api/config")).json();
    if (cfg.demo) {
      const notice = $("#demo-notice");
      notice.hidden = false;
      notice.textContent = `Demo pública: usa datos ficticios. El chat se guarda en este navegador; los documentos se eliminan del servidor después de ${cfg.document_retention_hours} horas. Quien tenga el enlace puede descargarlos mientras estén vigentes.`;
    }
    const sug = $("#suggestions");
    sug.innerHTML = "";
    (cfg.suggestions || []).forEach((q) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = q;
      b.addEventListener("click", () => send(q));
      sug.appendChild(b);
    });
    $("#doc-types").innerHTML = (cfg.documents || []).map((d) => `<li title="${escapeHtml(d.description)}">${escapeHtml(d.title)}</li>`).join("");
    const src = await (await fetch("/api/sources")).json();
    $("#source-list").innerHTML =
      (src.sources || []).map((s) => `<li><a href="${escapeHtml(safeUrl(s.url) || "#")}" target="_blank" rel="noopener">${escapeHtml(s.title)}</a>${s.version ? ` <span class="muted">(${escapeHtml(s.version)})</span>` : ""}</li>`).join("") ||
      '<li class="muted">Índice vacío: ejecuta <code>python -m app.rag.ingest</code></li>';
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
  state.transcript.forEach((e) => { e.pending = false; });
  renderAll();
  loadConfig();

  const input = $("#input");
  const touchInput = () => matchMedia("(pointer: coarse)").matches;
  const autosize = () => {
    input.style.height = "auto";
    const maxHeight = parseFloat(getComputedStyle(input).maxHeight) || 160;
    input.style.height = `${Math.min(input.scrollHeight, maxHeight)}px`;
  };
  setupViewport(autosize);
  input.addEventListener("input", autosize);
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
    const text = input.value;
    input.value = "";
    autosize();
    send(text);
  });
  $("#new-chat").addEventListener("click", () => {
    if (state.busy) state.controller?.abort();
    Object.assign(state, { conversationId: null, history: [], transcript: [] });
    localStorage.removeItem(STORE_KEY);
    renderAll();
    if (!touchInput()) input.focus();
  });
  if (!touchInput()) input.focus();
}

init();

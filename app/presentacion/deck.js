// Navegación de la presentación: flechas / espacio / RePág / AvPág / Inicio / Fin, #n en la URL,
// "N" muestra las notas del presentador y "F" pantalla completa.
(() => {
  const slides = [...document.querySelectorAll(".slide")];
  const total = slides.length;
  const progress = document.querySelector(".progress");
  const counter = document.querySelector(".nav .count");
  const speaker = document.querySelector(".speaker");
  const params = new URLSearchParams(location.search);
  let current = 0;

  if (!params.has("static")) document.documentElement.classList.add("anim");

  // Números de página en el pie.
  slides.forEach((s, i) => {
    const n = s.querySelector(".foot .page");
    if (n) n.textContent = `${String(i + 1).padStart(2, "0")} / ${String(total).padStart(2, "0")}`;
  });

  // Tiras de "píxeles" decorativas, deterministas (mismo resultado en cada carga).
  document.querySelectorAll("[data-pixels]").forEach((el) => {
    const [count, seed] = el.dataset.pixels.split(",").map(Number);
    const colors = (el.dataset.colors || "#f7f7f2,#e97b77,#f5b726,#8ca59e").split(",");
    let x = seed || 7;
    const rand = () => ((x = (x * 9301 + 49297) % 233280) / 233280);
    el.innerHTML = Array.from({ length: count }, () => {
      const r = rand();
      const c = r < 0.18 ? colors[1] : r < 0.27 ? colors[2] : r < 0.62 ? colors[3] : colors[0];
      const o = r > 0.62 && r < 0.8 ? 0.35 : 1;
      return `<i style="background:${c};opacity:${o}"></i>`;
    }).join("");
  });

  function fit() {
    const scale = Math.min(innerWidth / 1920, innerHeight / 1080);
    document.documentElement.style.setProperty("--scale", scale);
  }

  // "Demo en vivo": carga el chat solo si la presentación se sirve desde Ventanilla
  // (/presentacion/); abierta de otra forma, la diapositiva deja las instrucciones.
  async function initLive(slide) {
    if (slide.dataset.liveInit) return;
    slide.dataset.liveInit = "1";
    const iframe = slide.querySelector(".live-frame iframe");
    try {
      const r = await fetch(new URL("../api/config", location.href), { cache: "no-store" });
      if (!r.ok || !(r.headers.get("content-type") || "").includes("json")) return;
    } catch {
      return;
    }
    iframe.addEventListener("load", () => {
      // Misma app, mismo origen: con el foco en el chat, RePág/AvPág (control remoto)
      // siguen moviendo la presentación y Esc devuelve el teclado a las diapositivas.
      try {
        iframe.contentWindow.addEventListener("keydown", (e) => {
          if (e.key === "PageDown") { e.preventDefault(); show(current + 1); }
          else if (e.key === "PageUp") { e.preventDefault(); show(current - 1); }
          else if (e.key === "Escape") { iframe.blur(); window.focus(); }
        });
      } catch { /* origen distinto: sin atajos dentro del chat */ }
    });
    iframe.src = iframe.dataset.src;
    slide.classList.add("live-ready");
  }

  function show(i, push = true) {
    if (document.activeElement && document.activeElement.tagName === "IFRAME") {
      document.activeElement.blur();
      window.focus();
    }
    current = Math.max(0, Math.min(total - 1, i));
    if (slides[current].hasAttribute("data-live")) initLive(slides[current]);
    slides.forEach((s, j) => s.classList.toggle("active", j === current));
    progress.style.width = `${((current + 1) / total) * 100}%`;
    counter.textContent = `${current + 1}/${total}`;
    const notes = slides[current].querySelector("aside.notes");
    speaker.innerHTML = `<h5>Notas · ${current + 1}/${total}</h5>${notes ? notes.innerHTML : "<p>(sin notas)</p>"}`;
    if (push) history.replaceState(null, "", `${location.search}#${current + 1}`);
  }

  addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    if (["ArrowRight", "ArrowDown", "PageDown", " ", "Enter"].includes(k)) { e.preventDefault(); show(current + 1); }
    else if (["ArrowLeft", "ArrowUp", "PageUp", "Backspace"].includes(k)) { e.preventDefault(); show(current - 1); }
    else if (k === "Home") show(0);
    else if (k === "End") show(total - 1);
    else if (k === "n" || k === "N") speaker.classList.toggle("on");
    else if (k === "f" || k === "F") {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen?.();
    }
  });

  // Gestos en pantallas táctiles.
  let tx = null;
  addEventListener("touchstart", (e) => { tx = e.touches[0].clientX; }, { passive: true });
  addEventListener("touchend", (e) => {
    if (tx === null) return;
    const dx = e.changedTouches[0].clientX - tx;
    if (Math.abs(dx) > 50) show(current + (dx < 0 ? 1 : -1));
    tx = null;
  });

  document.querySelector(".nav .prev").addEventListener("click", () => show(current - 1));
  document.querySelector(".nav .next").addEventListener("click", () => show(current + 1));
  addEventListener("resize", fit);
  addEventListener("hashchange", () => show((parseInt(location.hash.slice(1), 10) || 1) - 1, false));

  fit();
  show((parseInt(location.hash.slice(1), 10) || 1) - 1, false);
})();

// Dictado nativo: el proveedor del navegador procesa la voz, no el backend.
export class VoiceDictation {
  constructor({ Recognition, onText, onState, createSilenceMonitor,
    setTimer = (callback, delay) => setTimeout(callback, delay), clearTimer = (id) => clearTimeout(id) }) {
    this.Recognition = Recognition;
    this.onText = onText;
    this.onState = onState;
    this.createSilenceMonitor = createSilenceMonitor;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.session = null;
  }

  get active() { return this.session !== null; }

  update(phase, message) {
    this.onState({ phase, message, active: this.active });
  }

  timer(session, callback, delay) {
    this.clearTimer(session.timer);
    session.timer = this.setTimer(() => {
      if (this.session === session) callback();
    }, delay);
  }

  finish(session, message, phase = "idle", abort = false) {
    if (this.session !== session) return;
    this.clearTimer(session.timer);
    this.session = null; // Invalida eventos tardíos antes de abortar el micrófono.
    session.silenceMonitor?.stop();
    if (abort) {
      try { session.recognition.abort(); } catch { /* Ya estaba detenido. */ }
    }
    this.update(phase, message);
  }

  start(draft, from = draft.length, to = from) {
    if (this.active || !this.Recognition) return false;
    let recognition;
    try { recognition = new this.Recognition(); }
    catch {
      this.update("error", "No se pudo iniciar el dictado. Puedes seguir escribiendo.");
      return false;
    }
    const session = { recognition, draft, before: draft.slice(0, from), after: draft.slice(to), text: "", phase: "starting" };
    this.session = session;
    recognition.lang = "es-MX";
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => {
      if (this.session !== session) {
        try { recognition.abort(); } catch { /* Se canceló durante el permiso. */ }
        return;
      }
      if (session.phase !== "starting") return;
      session.phase = "listening";
      session.silenceMonitor?.arm();
      this.listeningStatus(session);
      this.timer(session, () => this.stop(), 60_000);
    };
    recognition.onresult = (event) => {
      if (this.session !== session) return;
      // La lista contiene también resultados anteriores y revisiones provisionales.
      // Reconstruirla evita duplicar palabras cuando el navegador las confirma.
      const parts = Array.from(event.results, (result) => result[0]?.transcript?.trim() || "");
      session.text = parts.filter(Boolean).join(" ").trim();
      if (!session.text) { this.onText(session.draft); return; }
      const before = session.before && !/\s$/.test(session.before) ? `${session.before} ` : session.before;
      const after = session.after && !/^[\s.,;:!?)]/.test(session.after) ? ` ${session.after}` : session.after;
      this.onText(before + session.text + after);
    };
    recognition.onerror = (event) => {
      if (this.session !== session) return;
      const messages = {
        "not-allowed": "No se permitió el micrófono. Revisa el permiso de este sitio y vuelve a intentarlo.",
        "service-not-allowed": "Este navegador no permite el servicio de dictado. Puedes seguir escribiendo.",
        "audio-capture": "No se encontró un micrófono disponible. Comprueba que esté conectado y libre.",
        "network": "No se pudo conectar con el servicio de dictado del navegador. Intenta de nuevo.",
        "no-speech": "No se detectó voz. Puedes volver a intentarlo o escribir.",
        "language-not-supported": "El servicio de voz no admite español en este navegador.",
        "aborted": "El dictado se interrumpió. Puedes volver a intentarlo.",
      };
      const message = messages[event.error] || "No se pudo completar el dictado. Puedes seguir escribiendo.";
      this.finish(session, message + (session.text ? " El texto reconocido sigue en tu borrador." : ""), "error", true);
    };
    recognition.onend = () => this.finish(session, session.text
      ? "Dictado listo. Revisa el texto y pulsa Enviar."
      : "No se detectó voz. Puedes volver a intentarlo o escribir.");

    this.update("starting", "Esperando permiso del micrófono…");
    this.timer(session, () => this.finish(session, "No se pudo iniciar el micrófono. Revisa el permiso e intenta de nuevo.", "error", true), 20_000);
    // El detector arranca desde el clic, pero solo cuenta silencio al estar escuchando.
    try {
      session.silenceMonitor = this.createSilenceMonitor?.({
        onSilence: () => {
          if (this.session === session && session.phase === "listening") this.stop();
        },
        onReady: () => {
          session.silenceReady = true;
          this.listeningStatus(session);
        },
        onUnavailable: () => {
          session.silenceReady = false;
          this.listeningStatus(session);
        },
      });
    } catch { /* Si falla la detección local, sigue disponible la parada manual. */ }
    try { recognition.start(); }
    catch (error) {
      this.finish(session, error.name === "NotAllowedError"
        ? "No se permitió el micrófono. Revisa el permiso de este sitio."
        : "No se pudo iniciar el dictado. Puedes volver a intentarlo.", "error", true);
      return false;
    }
    return true;
  }

  listeningStatus(session) {
    if (this.session !== session || session.phase !== "listening") return;
    this.update("listening", session.silenceReady
      ? "Escuchando… se detendrá tras 3 segundos de silencio."
      : "Escuchando… pulsa el micrófono para terminar (máximo un minuto).");
  }

  stop() {
    const session = this.session;
    if (!session || session.phase === "stopping") return;
    if (session.phase === "starting") { this.cancel(); return; }
    session.phase = "stopping";
    session.silenceMonitor?.stop();
    this.update("stopping", "Terminando dictado…");
    this.timer(session, () => this.finish(session,
      session.text ? "Dictado detenido. Revisa el texto antes de enviarlo." : "No se detectó voz. Puedes intentar de nuevo.", "idle", true), 5_000);
    try { session.recognition.stop(); }
    catch { this.finish(session, "Dictado detenido. Revisa tu borrador.", "idle", true); }
  }

  cancel({ silent = false } = {}) {
    const session = this.session;
    if (session) {
      this.onText(session.draft);
      this.finish(session, silent ? "" : "Dictado cancelado. Conservamos tu texto anterior.", "idle", true);
    } else if (silent) {
      this.update("idle", "");
    }
  }

  interrupt() {
    if (this.session) this.finish(this.session, "Dictado detenido al salir de la página. Revisa tu borrador.", "idle", true);
  }
}

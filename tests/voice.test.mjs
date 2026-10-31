import test from "node:test";
import assert from "node:assert/strict";
import { VoiceDictation } from "../app/static/voice.mjs";

function harness({ throwsOnStart = false, createSilenceMonitor } = {}) {
  const instances = [], states = [], texts = [], timers = new Map();
  let timerId = 0;
  class Recognition {
    constructor() { instances.push(this); this.aborts = 0; this.stops = 0; }
    start() { if (throwsOnStart) throw Object.assign(new Error(), { name: "NotAllowedError" }); }
    stop() { this.stops++; }
    abort() { this.aborts++; }
    result(...parts) {
      this.onresult({ results: parts.map(([transcript, isFinal]) => Object.assign([{ transcript }], { isFinal })) });
    }
  }
  const voice = new VoiceDictation({
    createSilenceMonitor,
    Recognition, onText: text => texts.push(text), onState: state => states.push(state),
    setTimer: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimer: id => timers.delete(id),
  });
  const expire = delay => {
    const entry = [...timers.entries()].find(([, timer]) => timer.delay === delay);
    assert.ok(entry, `Expected a ${delay} ms timer`);
    timers.delete(entry[0]); entry[1].callback();
  };
  return { voice, instances, states, texts, timers, expire, lastState: () => states.at(-1), lastText: () => texts.at(-1) };
}

test("recognition starts only on request, in Spanish, without overlapping sessions", () => {
  const h = harness();
  assert.equal(h.instances.length, 0);
  assert.equal(h.voice.start(""), true);
  assert.equal(h.voice.start("other draft"), false);
  const r = h.instances[0];
  assert.equal(r.lang, "es-MX");
  assert.equal(r.continuous, true);
  assert.equal(r.interimResults, true);
  assert.equal(h.lastState().phase, "starting");
  r.onstart();
  assert.equal(h.lastState().phase, "listening");
});

test("interim revisions and final results replace earlier hypotheses without duplicates", () => {
  const h = harness(); h.voice.start("Hola"); const r = h.instances[0]; r.onstart();
  r.result(["necesito", true], ["una carta", false]);
  assert.equal(h.lastText(), "Hola necesito una carta");
  r.result(["necesito", true], ["una carta poder", true]);
  assert.equal(h.lastText(), "Hola necesito una carta poder");
  r.result(["necesito", true], ["una carta poder", true]);
  assert.equal(h.lastText(), "Hola necesito una carta poder");
  r.onend();
  assert.equal(h.voice.active, false);
  assert.match(h.lastState().message, /Revisa el texto/);
});

test("dictation replaces the selection and cancellation restores the full draft", () => {
  const h = harness(); h.voice.start("Quiero cambiar esto.", 7, 14);
  const r = h.instances[0]; r.onstart(); r.result(["revisar", true]);
  assert.equal(h.lastText(), "Quiero revisar esto.");
  h.voice.cancel();
  assert.equal(h.lastText(), "Quiero cambiar esto.");
  assert.equal(r.aborts, 1);
  r.result(["late result", true]); r.onend();
  assert.equal(h.lastText(), "Quiero cambiar esto.");
  assert.match(h.lastState().message, /cancelado/);
});

test("stop waits for final words before making the draft editable", () => {
  const h = harness(); h.voice.start(""); const r = h.instances[0]; r.onstart();
  r.result(["mi carta", false]); h.voice.stop(); h.voice.stop();
  assert.equal(r.stops, 1);
  assert.equal(h.voice.active, true);
  assert.equal(h.lastState().phase, "stopping");
  r.onstart(); // A duplicate start must not reopen a stopping session.
  assert.equal(h.lastState().phase, "stopping");
  r.result(["mi carta poder", true]); r.onend();
  assert.equal(h.lastText(), "mi carta poder");
  assert.equal(h.voice.active, false);
  assert.equal(h.timers.size, 0);
});

test("network failure preserves recognized text and permits a fresh attempt", () => {
  const h = harness(); h.voice.start("Ayuda:"); const first = h.instances[0]; first.onstart();
  first.result(["con mi RFC", false]); first.onerror({ error: "network" });
  assert.equal(h.lastText(), "Ayuda: con mi RFC");
  assert.equal(h.voice.active, false);
  assert.equal(h.lastState().phase, "error");
  assert.equal(first.aborts, 1);
  assert.equal(h.voice.start(h.lastText()), true);
  const state = h.lastState();
  first.result(["stale words", true]); first.onend();
  assert.equal(h.lastState(), state);
  assert.equal(h.lastText(), "Ayuda: con mi RFC");
});

test("permission exceptions and permission error events leave no active session", () => {
  const h = harness({ throwsOnStart: true });
  assert.equal(h.voice.start("Draft"), false);
  assert.equal(h.voice.active, false);
  assert.match(h.lastState().message, /permiso/);
  assert.equal(h.timers.size, 0);
  const second = harness(); second.voice.start("Draft");
  second.instances[0].onerror({ error: "not-allowed" });
  assert.equal(second.voice.active, false);
  assert.equal(second.texts.length, 0);
});

test("silence leaves the original draft intact", () => {
  const h = harness(); h.voice.start("My draft"); const r = h.instances[0]; r.onstart();
  r.onerror({ error: "no-speech" }); r.onend();
  assert.equal(h.texts.length, 0);
  assert.match(h.lastState().message, /No se detectó voz/);
  assert.equal(h.voice.active, false);
});

test("cancellation during a pending permission aborts a late microphone start", () => {
  const h = harness(); h.voice.start("Draft"); const r = h.instances[0];
  h.voice.stop();
  assert.equal(h.lastText(), "Draft");
  assert.equal(h.voice.active, false);
  r.onstart();
  assert.equal(r.aborts, 2);
  assert.equal(h.lastState().active, false);
});

test("a stalled permission request times out and ignores late results", () => {
  const h = harness(); h.voice.start("Draft"); const r = h.instances[0];
  h.expire(20_000);
  assert.equal(h.voice.active, false);
  assert.equal(h.lastState().phase, "error");
  r.onstart(); r.result(["late words", true]);
  assert.equal(h.texts.length, 0);
  assert.equal(r.aborts, 2);
});

test("one-minute limit remains available without silence detection", () => {
  const h = harness(); h.voice.start(""); const r = h.instances[0];
  assert.deepEqual([...h.timers.values()].map(timer => timer.delay), [20_000]);
  r.onstart();
  assert.deepEqual([...h.timers.values()].map(timer => timer.delay), [60_000]);
  r.result(["mi pregunta", false]); h.expire(60_000);
  assert.equal(r.stops, 1);
  assert.equal(h.lastState().phase, "stopping");
  h.expire(5_000);
  assert.equal(r.aborts, 1);
  assert.equal(h.voice.active, false);
  assert.equal(h.lastText(), "mi pregunta");
});

test("leaving the page stops the microphone while preserving recognized text", () => {
  const h = harness(); h.voice.start(""); const r = h.instances[0]; r.onstart();
  r.result(["consulta pendiente", false]); h.voice.interrupt();
  assert.equal(r.aborts, 1);
  assert.equal(h.lastText(), "consulta pendiente");
  assert.equal(h.voice.active, false);
  r.result(["late", true]);
  assert.equal(h.lastText(), "consulta pendiente");
});

test("unsupported recognition and clearing an idle status never alter a draft", () => {
  const h = harness(); h.voice.Recognition = null;
  assert.equal(h.voice.start("Draft"), false);
  h.voice.cancel({ silent: true });
  assert.equal(h.texts.length, 0);
  assert.equal(h.lastState().message, "");
});

function monitoredHarness() {
  const monitors = [];
  const h = harness({ createSilenceMonitor: callbacks => {
    const monitor = { ...callbacks, armed: false, stopped: false,
      arm() { this.armed = true; }, stop() { this.stopped = true; } };
    monitors.push(monitor);
    return monitor;
  } });
  return { ...h, monitors };
}

test("silence stops only an active listening session and retains final recognition results", () => {
  const h = monitoredHarness(); h.voice.start("Ayuda:");
  const r = h.instances[0], m = h.monitors[0];
  assert.equal(m.armed, false);
  m.onReady(); m.onSilence();
  assert.equal(r.stops, 0);
  r.onstart();
  assert.equal(m.armed, true);
  assert.match(h.lastState().message, /3 segundos de silencio/);
  r.result(["mi consulta", false]);
  m.onSilence(); m.onSilence();
  assert.equal(r.stops, 1);
  assert.equal(m.stopped, true);
  assert.equal(h.lastState().phase, "stopping");
  m.onReady(); // A late detector callback cannot reopen the session.
  assert.equal(h.lastState().phase, "stopping");
  r.result(["mi consulta completa", true]); r.onend();
  assert.equal(h.lastText(), "Ayuda: mi consulta completa");
  assert.equal(h.voice.active, false);
});

test("detector failure retains manual dictation instead of a three-second cutoff", () => {
  const h = monitoredHarness(); h.voice.start(""); h.instances[0].onstart();
  h.monitors[0].onReady(); h.monitors[0].onUnavailable();
  assert.match(h.lastState().message, /pulsa el micrófono/);
  assert.deepEqual([...h.timers.values()].map(timer => timer.delay), [60_000]);
  assert.equal(h.instances[0].stops, 0);
  h.voice.stop();
  assert.equal(h.instances[0].stops, 1);
  const throwing = harness({ createSilenceMonitor: () => { throw new Error("unsupported"); } });
  assert.equal(throwing.voice.start(""), true);
  throwing.instances[0].onstart();
  assert.match(throwing.lastState().message, /pulsa el micrófono/);
});

test("every termination releases monitoring and stale callbacks cannot stop a new dictation", () => {
  for (const terminate of [
    h => h.voice.cancel(), h => h.voice.interrupt(), h => h.voice.stop(),
    h => h.instances[0].onend(), h => h.instances[0].onerror({ error: "network" }),
    h => h.expire(20_000),
  ]) {
    const h = monitoredHarness(); h.voice.start("Draft"); terminate(h);
    assert.equal(h.monitors[0].stopped, true);
    assert.equal(h.voice.start("Next"), true);
    h.instances[1].onstart();
    h.monitors[0].onSilence(); h.monitors[0].onReady();
    assert.equal(h.instances[1].stops, 0);
    assert.equal(h.lastState().phase, "listening");
    h.voice.cancel();
  }
});

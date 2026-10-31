import test from "node:test";
import assert from "node:assert/strict";
import { createSilenceMonitor } from "../app/static/silence.mjs";

const flush = () => new Promise(resolve => setImmediate(resolve));

function harness({ pending = false, denied = false, unsupported = false } = {}) {
  let clock = 0, level = 0, resolveMedia;
  const intervals = new Map(), contexts = [], events = [];
  const track = { readyState: "live", muted: false, stops: 0,
    stop() { this.stops++; this.readyState = "ended"; } };
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
  class AudioContext {
    constructor() { contexts.push(this); this.state = "suspended"; this.closed = 0; }
    resume() { this.state = "running"; return Promise.resolve(); }
    close() { this.closed++; this.state = "closed"; return Promise.resolve(); }
    createMediaStreamSource() {
      this.source = { connected: false, connect() { this.connected = true; }, disconnect() { this.connected = false; } };
      return this.source;
    }
    createAnalyser() { return { fftSize: 0, getFloatTimeDomainData: samples => samples.fill(level) }; }
  }
  const monitor = createSilenceMonitor({
    AudioContext: unsupported ? null : AudioContext,
    getUserMedia: () => denied ? Promise.reject(new Error("denied"))
      : pending ? new Promise(resolve => { resolveMedia = resolve; }) : Promise.resolve(stream),
    onSilence: () => events.push("silence"), onReady: () => events.push("ready"),
    onUnavailable: () => events.push("unavailable"), now: () => clock,
    setIntervalFn: callback => { intervals.set(1, callback); return 1; },
    clearIntervalFn: id => intervals.delete(id),
  });
  return { monitor, track, contexts, events, intervals,
    permit: () => resolveMedia(stream),
    tick: (time, amplitude = 0) => { clock = time; level = amplitude; [...intervals.values()].forEach(callback => callback()); },
  };
}

test("continuous audio lasts beyond three seconds and only three full seconds of silence stop it", async () => {
  const h = harness(); await flush(); h.monitor.arm();
  for (let t = 0; t <= 10_000; t += 100) h.tick(t, 0.05);
  assert.deepEqual(h.events, ["ready"]);
  h.tick(12_999);
  assert.deepEqual(h.events, ["ready"]);
  h.tick(13_000);
  assert.deepEqual(h.events, ["ready", "silence"]);
  assert.equal(h.track.stops, 1);
  assert.equal(h.contexts[0].closed, 1);
  assert.equal(h.contexts[0].source.connected, false);
  assert.equal(h.intervals.size, 0);
});

test("speaking again after a short pause resets the full silence window", async () => {
  const h = harness(); await flush(); h.monitor.arm();
  h.tick(0, 0.03); h.tick(2_900); h.tick(2_950, 0.03);
  h.tick(3_000); h.tick(5_949);
  assert.deepEqual(h.events, ["ready"]);
  h.tick(5_950);
  assert.deepEqual(h.events, ["ready", "silence"]);
});

test("permission waiting and recognition startup are not counted as silence", async () => {
  const h = harness({ pending: true }); h.tick(10_000); h.permit(); await flush();
  h.tick(15_000); h.tick(20_000);
  assert.deepEqual(h.events, ["ready"]);
  h.monitor.arm(); h.tick(20_100); h.tick(23_099);
  assert.deepEqual(h.events, ["ready"]);
  h.tick(23_100);
  assert.deepEqual(h.events, ["ready", "silence"]);
});

test("cancelling while permission is pending releases a late stream", async () => {
  const h = harness({ pending: true }); h.monitor.stop(); h.permit(); await flush();
  assert.equal(h.track.stops, 1);
  assert.equal(h.contexts[0].closed, 1);
  assert.equal(h.intervals.size, 0);
  assert.deepEqual(h.events, []);
});

test("manual stop is idempotent and prevents subsequent silence callbacks", async () => {
  const h = harness(); await flush(); h.monitor.arm(); h.tick(0, 0.05);
  h.monitor.stop(); h.monitor.stop(); h.tick(9_000);
  assert.equal(h.track.stops, 1);
  assert.equal(h.contexts[0].closed, 1);
  assert.deepEqual(h.events, ["ready"]);
});

test("unsupported or denied monitoring reports fallback without stopping recognition", async () => {
  for (const options of [{ unsupported: true }, { denied: true }]) {
    const h = harness(options); await flush();
    assert.deepEqual(h.events, ["unavailable"]);
    assert.equal(h.intervals.size, 0);
    if (h.contexts[0]) assert.equal(h.contexts[0].closed, 1);
  }
});

test("suspended audio or interrupted microphone is unavailable, not user silence", async () => {
  for (const interrupt of [
    h => { h.track.muted = true; h.track.onmute(); },
    h => { h.track.readyState = "ended"; h.track.onended(); },
    h => { h.contexts[0].state = "suspended"; h.contexts[0].onstatechange(); },
  ]) {
    const h = harness(); await flush(); h.monitor.arm(); h.tick(0);
    interrupt(h); h.tick(5_000);
    assert.deepEqual(h.events, ["ready", "unavailable"]);
    assert.equal(h.track.stops, 1);
    assert.equal(h.intervals.size, 0);
  }
});

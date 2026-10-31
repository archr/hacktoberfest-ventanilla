// Solo mide el nivel del micrófono en memoria; no graba ni envía este audio.
export function createSilenceMonitor({ onSilence, onReady, onUnavailable,
  AudioContext = globalThis.AudioContext || globalThis.webkitAudioContext,
  getUserMedia = (constraints) => navigator.mediaDevices.getUserMedia(constraints),
  now = () => performance.now(),
  setIntervalFn = (callback, delay) => setInterval(callback, delay),
  clearIntervalFn = (id) => clearInterval(id),
}) {
  let context, stream, source, interval;
  let closed = false, armed = false, lastSoundAt = null;
  const stopTracks = (media) => media?.getTracks().forEach((track) => {
    track.onended = null;
    track.onmute = null;
    track.stop();
  });
  const stop = () => {
    if (closed) return;
    closed = true;
    clearIntervalFn(interval);
    source?.disconnect();
    stopTracks(stream);
    if (context) {
      context.onstatechange = null;
      context.close().catch(() => {});
    }
  };
  const unavailable = () => {
    if (closed) return;
    stop();
    onUnavailable();
  };
  const handle = { stop, arm: () => { armed = true; lastSoundAt = null; } };

  try {
    context = new AudioContext();
    // Se invoca dentro del clic para respetar la activación requerida en móviles.
    const resumed = context.resume();
    resumed.catch(unavailable);
    Promise.resolve(getUserMedia({ audio: true })).then(async (media) => {
      if (closed) { stopTracks(media); return; }
      stream = media;
      await resumed;
      if (closed) return;
      const track = stream.getAudioTracks()[0];
      if (!track || track.readyState !== "live" || track.muted || context.state !== "running") {
        unavailable();
        return;
      }
      // Una captura interrumpida no equivale a silencio del usuario.
      track.onended = unavailable;
      track.onmute = unavailable;
      context.onstatechange = () => { if (context.state !== "running") unavailable(); };
      source = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser();
      analyser.fftSize = 2048;
      source.connect(analyser); // Sin conexión al altavoz: no hay eco ni reproducción.
      const samples = new Float32Array(analyser.fftSize);
      interval = setIntervalFn(() => {
        if (closed || !armed) return;
        try {
          if (context.state !== "running" || track.muted || track.readyState !== "live") {
            unavailable();
            return;
          }
          analyser.getFloatTimeDomainData(samples);
          const time = now();
          const rms = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length);
          // Umbral bajo (-40 dBFS) para no confundir voz suave con silencio.
          if (lastSoundAt === null || rms >= 0.01) lastSoundAt = time;
          if (time - lastSoundAt >= 3_000) {
            stop();
            onSilence();
          }
        } catch { unavailable(); }
      }, 100);
      onReady();
    }).catch(unavailable);
  } catch { unavailable(); }
  return handle;
}

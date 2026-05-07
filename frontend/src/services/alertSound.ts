// Web Audio synthesis for the route-completion alert.
// Synthesises the picked "Cascade" sound (#9 from docs/alert-sound-picker.html):
// three descending bells with overlapping decay tails, ~2 seconds total.
//
// Settings persistence lives here too so callers from anywhere in the app
// (StatusBar settings panel, useSimulation completion hook) read/write the
// same localStorage key without each duplicating the parsing.

const SETTING_KEY = 'locwarp.settings.alertSoundEnabled';

export function isAlertSoundEnabled(): boolean {
  try {
    const v = localStorage.getItem(SETTING_KEY);
    // Default: on. Any explicit 'false' disables; anything else (including
    // first-run with no key set) keeps the alert audible by default since
    // the user opted in by adding the feature.
    return v !== 'false';
  } catch {
    return true;
  }
}

export function setAlertSoundEnabled(enabled: boolean): void {
  try {
    localStorage.setItem(SETTING_KEY, String(enabled));
  } catch { /* localStorage blocked / quota; setting becomes session-only */ }
}

let audioCtx: AudioContext | null = null;

function getCtx(): AudioContext | null {
  try {
    if (!audioCtx) {
      const Ctor = (window as any).AudioContext || (window as any).webkitAudioContext;
      if (!Ctor) return null;
      audioCtx = new Ctor();
    }
    // Browsers suspend the context until a user gesture; resume() is a
    // no-op once the gesture has happened. The Settings panel "test" button
    // doubles as the unlock event so the route-completion playback later
    // (which has no associated gesture) still produces audio.
    if (audioCtx && audioCtx.state === 'suspended') {
      audioCtx.resume().catch(() => { /* ignore — context still usable */ });
    }
    return audioCtx;
  } catch {
    return null;
  }
}

/**
 * Play the route-completion alert sound (cascading bells, ~2s).
 *
 * @param force  When true, bypasses the user's enabled-or-not setting.
 *               Use this for the test button inside the Settings panel.
 */
export function playCompletionAlert(force = false): void {
  if (!force && !isAlertSoundEnabled()) return;
  const ctx = getCtx();
  if (!ctx) return;

  const master = ctx.createGain();
  master.gain.value = 0.5;
  master.connect(ctx.destination);

  // Three descending bells (C6, G5, C5) staggered 0.30s apart so their
  // 1.4-1.6s decay tails overlap into a temple-bell texture.
  const notes: Array<{ f: number; t: number; d: number }> = [
    { f: 1046.5, t: 0.00, d: 1.4 },
    { f: 783.99, t: 0.30, d: 1.5 },
    { f: 523.25, t: 0.60, d: 1.6 },
  ];
  for (const { f, t, d } of notes) {
    const osc = ctx.createOscillator();
    const env = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = f;
    const t0 = ctx.currentTime + t;
    env.gain.setValueAtTime(0, t0);
    env.gain.linearRampToValueAtTime(0.45, t0 + 0.01);
    env.gain.exponentialRampToValueAtTime(0.001, t0 + d);
    osc.connect(env).connect(master);
    osc.start(t0);
    osc.stop(t0 + d + 0.05);
  }
}

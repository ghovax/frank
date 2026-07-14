"use client";

// Synthesized audio cues — no audio assets, just a lazily-created AudioContext
// and two tiny envelope-shaped sine motifs, kept quiet enough to read as
// ambience rather than an alert:
//   • turn end — a rising fourth (D5 → G5), "your result is ready";
//   • attention — a falling third (B5 → G5), the questioning inflection for a
//     pending approval.
//
// Browsers keep an AudioContext suspended until a user gesture, so the app
// calls primeSounds() once at startup: it arms a one-shot listener that
// creates/resumes the context on the first pointer or key interaction, and
// every later cue plays immediately. A cue that fires while the context is
// still suspended is dropped silently — a missed chime is better than a
// stale one bursting out on the next click.

let context: AudioContext | null = null;

function audioContext(): AudioContext | null {
  if (typeof window === "undefined") return null;
  try {
    context ??= new AudioContext();
  } catch {
    return null;
  }
  if (context.state === "suspended") void context.resume().catch(() => {});
  return context;
}

export function primeSounds(): void {
  if (typeof window === "undefined") return;
  const arm = () => {
    audioContext();
    window.removeEventListener("pointerdown", arm);
    window.removeEventListener("keydown", arm);
  };
  window.addEventListener("pointerdown", arm, { once: true });
  window.addEventListener("keydown", arm, { once: true });
}

function tone(
  ctx: AudioContext,
  frequency: number,
  at: number,
  duration: number,
  peak: number,
): void {
  const oscillator = ctx.createOscillator();
  const envelope = ctx.createGain();
  oscillator.type = "sine";
  oscillator.frequency.value = frequency;
  const start = ctx.currentTime + at;
  // Fast attack, exponential release — a soft mallet strike, no click on either end.
  envelope.gain.setValueAtTime(0.0001, start);
  envelope.gain.exponentialRampToValueAtTime(peak, start + 0.015);
  envelope.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  oscillator.connect(envelope).connect(ctx.destination);
  oscillator.start(start);
  oscillator.stop(start + duration + 0.05);
}

// The assistant finished a turn (in this session or a background one).
export function playTurnEndSound(): void {
  const ctx = audioContext();
  if (!ctx || ctx.state !== "running") return;
  tone(ctx, 587.33, 0, 0.3, 0.045); // D5
  tone(ctx, 783.99, 0.1, 0.45, 0.04); // G5
}

// A tool call is waiting on the user (permission or question).
export function playAttentionSound(): void {
  const ctx = audioContext();
  if (!ctx || ctx.state !== "running") return;
  tone(ctx, 987.77, 0, 0.22, 0.035); // B5
  tone(ctx, 783.99, 0.12, 0.4, 0.035); // G5
}

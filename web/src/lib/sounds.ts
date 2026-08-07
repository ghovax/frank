"use client";

import { expected } from "@/lib/swallowed";

// The desktop hands cues to macOS; elsewhere two quiet Web Audio motifs stand in.

let context: AudioContext | null = null;

type SystemSoundCue = "attention" | "turnEnd";

function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

async function playNativeSystemSound(cue: SystemSoundCue): Promise<boolean> {
  if (!isTauri()) return false;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke<boolean>("play_system_sound", { cue });
  } catch {
    // No Tauri shell — the web build has no system sound to play.
    return false;
  }
}

function audioContext(): AudioContext | null {
  if (typeof window === "undefined") return null;
  try {
    context ??= new AudioContext();
  } catch {
    // Audio is blocked until the page has been interacted with.
    return null;
  }
  // Autoplay policy refuses this until the page is interacted with, and the result is silence, not a fault.
  if (context.state === "suspended") {
    void context
      .resume()
      .catch((caught) => expected("a suspended audio context may refuse to resume", caught));
  }
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
  context: AudioContext,
  frequency: number,
  at: number,
  duration: number,
  peak: number,
): void {
  const oscillator = context.createOscillator();
  const envelope = context.createGain();
  oscillator.type = "sine";
  oscillator.frequency.value = frequency;
  const start = context.currentTime + at;
  // Fast attack, exponential release — a soft mallet strike, no click on either end.
  envelope.gain.setValueAtTime(0.0001, start);
  envelope.gain.exponentialRampToValueAtTime(peak, start + 0.015);
  envelope.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  oscillator.connect(envelope).connect(context.destination);
  oscillator.start(start);
  oscillator.stop(start + duration + 0.05);
}

function playFallbackTurnEndSound(): void {
  const context = audioContext();
  if (!context || context.state !== "running") return;
  tone(context, 587.33, 0, 0.3, 0.045); // D5
  tone(context, 783.99, 0.1, 0.45, 0.04); // G5
}

function playFallbackAttentionSound(): void {
  const context = audioContext();
  if (!context || context.state !== "running") return;
  tone(context, 523.25, 0, 0.32, 0.022); // C5
  tone(context, 659.25, 0.09, 0.46, 0.018); // E5
}

function playSound(cue: SystemSoundCue, fallback: () => void): void {
  if (!isTauri()) {
    fallback();
    return;
  }
  void playNativeSystemSound(cue).then((played) => {
    if (!played) fallback();
  });
}

// The assistant finished a turn (in this session or a background one).
export function playTurnEndSound(): void {
  playSound("turnEnd", playFallbackTurnEndSound);
}

// A tool call is waiting on the user (permission or question).
export function playAttentionSound(): void {
  playSound("attention", playFallbackAttentionSound);
}

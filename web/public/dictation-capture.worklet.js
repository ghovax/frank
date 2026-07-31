// The audio-thread half of dictation: take every block of microphone samples and hand it to
// the page. Registered by `src/lib/dictation.ts`, which opens the `AudioContext` at the rate
// the model wants and does everything else.
//
// A real file, served from `public/`, rather than a template string turned into a Blob URL.
// Both load, but only one of these is code: this is linted, formatted, diffed and syntax-
// highlighted like the rest of the app, while a string is opaque to every one of those and
// fails at runtime — inside an audio worklet, where the error surfaces as silence — for a typo
// a build would otherwise have caught.
//
// It runs on the audio rendering thread. That is the whole reason it exists as a worklet: the
// thread is real-time and never yields to layout or React, so a block is never dropped because
// the page was busy painting a transcript.
class DictationCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    // One input, first channel. The microphone is opened as mono, and a channel that is
    // momentarily absent (the graph still connecting) is not an error — returning `true` keeps
    // the processor alive for the next block.
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length > 0) {
      // Copied on the way out. The buffer the audio thread hands over is reused for the next
      // block, so posting it directly would deliver a view of samples that have already been
      // overwritten by the time the page reads them.
      this.port.postMessage(new Float32Array(channel));
    }
    return true;
  }
}

registerProcessor("dictation-capture", DictationCaptureProcessor);

// Recording a person's voice into the samples the daemon transcribes.
//
// Deliberately not `MediaRecorder`. That would hand back an encoded file — `webm/opus` in
// Chromium, `mp4/aac` in WebKit — and the daemon would then need a decoder to undo an encode
// the browser had just performed for no reason, in a format that differs by which shell the
// app happens to be running in. The Web Audio graph already holds the raw samples, so this
// takes them straight out of it: mono float32 at exactly the rate the model wants, which the
// `AudioContext` resamples to for us. Nothing on either side has to know an audio format.
//
// The capture itself runs in an `AudioWorklet`, on the audio thread, where dropping a buffer
// is not a possibility. Its processor lives in `public/dictation-capture.worklet.js` — a real
// file, linted and diffed like everything else, rather than a template string this module
// turns into a Blob URL at runtime.

// What the model expects. The `AudioContext` is opened at this rate and resamples the
// microphone into it, so no resampling code exists on either side of the wire.
import { expected } from "@/lib/swallowed";
import { errorMessage } from "@/lib/errors";

export const DICTATION_SAMPLE_RATE = 16000;

// Where the processor is served from. Root-relative, which is the one form that holds
// everywhere this app runs: the dev server, the static export, and the Tauri asset server all
// serve `public/` from the origin the page itself came from — and `addModule` requires
// same-origin.
const CAPTURE_PROCESSOR_URL = "/dictation-capture.worklet.js";

export class DictationRecordingError extends Error {}

// One recording, from the moment the microphone opens to the moment the samples are handed
// back. Deliberately a small object with a `stop` rather than a callback API: the caller is a
// toggle button, and "the thing I started, which I can stop" is what a toggle actually holds.
export interface DictationRecording {
  // Everything captured so far, as one contiguous buffer, and everything released. Safe to
  // call once; calling it again answers with nothing.
  stop: () => Promise<Float32Array>;
  // Give up on the recording and release the microphone without producing samples.
  cancel: () => void;
}

export async function startDictationRecording(): Promise<DictationRecording> {
  if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
    throw new DictationRecordingError("This browser cannot reach a microphone.");
  }
  let stream: MediaStream;
  try {
    // No processing options are requested. The model was trained on ordinary speech, and
    // asking for aggressive noise suppression on top of it is a way to remove quiet consonants.
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (caught) {
    // The one failure worth naming precisely: permission, which the person can fix, versus
    // anything else, which they cannot.
    const denied = caught instanceof DOMException && (caught.name === "NotAllowedError" || caught.name === "SecurityError");
    throw new DictationRecordingError(
      denied
        ? "Frank was not allowed to use the microphone. Grant it in your system settings and try again."
        : "No microphone is available."
    );
  }

  const audioContext = new AudioContext({ sampleRate: DICTATION_SAMPLE_RATE });
  const chunks: Float32Array[] = [];
  let settled = false;

  const release = () => {
    for (const track of stream.getTracks()) track.stop();
    void audioContext.close().catch((caught) =>
      expected("a context that will not close has already stopped producing", caught)
    );
  };

  try {
    await audioContext.audioWorklet.addModule(CAPTURE_PROCESSOR_URL);
    const source = audioContext.createMediaStreamSource(stream);
    const capture = new AudioWorkletNode(audioContext, "dictation-capture");
    capture.port.onmessage = (event: MessageEvent<Float32Array>) => {
      if (!settled) chunks.push(event.data);
    };
    // Connected to the destination because some engines will not pull from a graph whose sink
    // is not the output. The worklet emits nothing downstream, so nothing is heard.
    source.connect(capture);
    capture.connect(audioContext.destination);
  } catch (caught) {
    release();
    throw new DictationRecordingError(`The microphone could not be opened: ${errorMessage(caught)}`);
  }

  return {
    async stop() {
      if (settled) return new Float32Array(0);
      settled = true;
      // One tick before tearing the graph down, so the last block the audio thread posted
      // is delivered rather than dropped with the port.
      await new Promise((resolve) => setTimeout(resolve, 0));
      release();
      const total = chunks.reduce((count, chunk) => count + chunk.length, 0);
      const samples = new Float32Array(total);
      let offset = 0;
      for (const chunk of chunks) {
        samples.set(chunk, offset);
        offset += chunk.length;
      }
      return samples;
    },
    cancel() {
      if (settled) return;
      settled = true;
      chunks.length = 0;
      release();
    },
  };
}

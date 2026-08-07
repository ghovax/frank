// Turning a person's voice into text: recording raw samples, and getting them transcribed.

// What the model expects, which the audio context is opened at so nothing resamples on either side.
import { transcribeDictation } from "@/lib/api";
import { expected } from "@/lib/swallowed";
import { errorMessage } from "@/lib/errors";

export const DICTATION_SAMPLE_RATE = 16000;

// Where the processor is served from, root-relative so it holds in every way this app is served.
const CAPTURE_PROCESSOR_URL = "/dictation-capture.worklet.js";

/** A recording that could not be made, named by the catalogue entry that says why. */
export class DictationRecordingError extends Error {
  constructor(
    message: string,
    readonly values: Record<string, string> = {},
  ) {
    super(message);
    this.name = "DictationRecordingError";
  }
}

/** One dictation, from the moment the microphone opens to the sentence it produced. */
export interface Dictation {
  // Everything said so far, transcribed, and the microphone released; safe to call once.
  stop: () => Promise<string>;
  // Give up and release the microphone without transcribing.
  cancel: () => void;
}

export async function startDictation(): Promise<Dictation> {
  const recording = await startDictationRecording();
  return {
    async stop() {
      const samples = await recording.stop();
      if (samples.length === 0) return "";
      return await transcribeDictation(samples);
    },
    cancel: () => recording.cancel(),
  };
}

// One recording, from the moment the microphone opens to the moment the samples are handed back.
export interface DictationRecording {
  // Everything captured so far as one buffer, with everything released; safe to call once.
  stop: () => Promise<Float32Array>;
  // Give up on the recording and release the microphone without producing samples.
  cancel: () => void;
}

async function startDictationRecording(): Promise<DictationRecording> {
  if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
    // Naming the cause, because an insecure origin and a missing microphone are fixed in different places.
    const insecure = typeof window !== "undefined" && !window.isSecureContext;
    throw new DictationRecordingError(
      insecure ? "dictationInsecureOrigin" : "dictationNoMicrophone",
    );
  }
  let stream: MediaStream;
  try {
    // No processing options are requested, since aggressive noise suppression removes quiet consonants.
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (caught) {
    // The one failure worth naming precisely: permission, which the person can fix.
    const denied =
      caught instanceof DOMException &&
      (caught.name === "NotAllowedError" || caught.name === "SecurityError");
    throw new DictationRecordingError(denied ? "dictationRefused" : "dictationNoDevice");
  }

  const audioContext = new AudioContext({ sampleRate: DICTATION_SAMPLE_RATE });
  const chunks: Float32Array[] = [];
  let settled = false;

  const release = () => {
    for (const track of stream.getTracks()) track.stop();
    void audioContext
      .close()
      .catch((caught) =>
        expected("a context that will not close has already stopped producing", caught),
      );
  };

  try {
    await audioContext.audioWorklet.addModule(CAPTURE_PROCESSOR_URL);
    const source = audioContext.createMediaStreamSource(stream);
    const capture = new AudioWorkletNode(audioContext, "dictation-capture");
    capture.port.onmessage = (event: MessageEvent<Float32Array>) => {
      if (!settled) chunks.push(event.data);
    };
    // Connected to the destination because some engines will not pull from a graph whose sink is not the output.
    source.connect(capture);
    capture.connect(audioContext.destination);
  } catch (caught) {
    release();
    throw new DictationRecordingError("dictationCouldNotOpen", { reason: errorMessage(caught) });
  }

  return {
    async stop() {
      if (settled) return new Float32Array(0);
      settled = true;
      // One tick before tearing the graph down, so the last block the audio thread posted is delivered.
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

/**
 * Speaking to Frank.
 *
 * The daemon's transcriber takes raw mono float32 at 16 kHz and nothing else — no container, no
 * codec, on purpose, so that neither end has to agree on one. The desktop meets that by tapping
 * the browser's audio graph, which already hands out float32 and will resample the microphone
 * for you. A phone has no audio graph, so the same contract is met from the other side: record
 * uncompressed at exactly 16 kHz, then strip the WAV header the platform wrapped it in.
 *
 * That is why the iOS recorder below is configured so specifically. `LINEARPCM` at 16 kHz mono,
 * 16-bit, little-endian is not a quality preference; it is the one setting that makes the file on
 * disk *already* be the samples the daemon wants, with 44 bytes in front of them.
 *
 * Android is the gap, and it is stated rather than hidden. Its recorder encodes — AAC, AMR, 3GP —
 * and there is no uncompressed option to ask for, so honouring the endpoint's contract there
 * would mean shipping a decoder in the app or a demuxer in the daemon. Frank's daemon is macOS
 * only, so the phone at the other end of it is overwhelmingly an iPhone, and a clear "not here"
 * is better than a button that fails after somebody has spoken a paragraph into it.
 */

import { AudioModule, RecordingPresets, useAudioRecorder, type RecordingOptions } from "expo-audio";
import { File } from "expo-file-system";
import { Platform } from "react-native";

import { DICTATION_SAMPLE_RATE } from "./api";

/**
 * Uncompressed, mono, at exactly the rate the daemon transcribes at. `bitRate` is meaningless for
 * linear PCM and is set anyway because the type requires it.
 */
export const DICTATION_RECORDING: RecordingOptions = {
  ...RecordingPresets.HIGH_QUALITY,
  extension: ".wav",
  sampleRate: DICTATION_SAMPLE_RATE,
  numberOfChannels: 1,
  bitRate: DICTATION_SAMPLE_RATE * 16,
  ios: {
    // `lpcm`, spelled the way CoreAudio spells it.
    outputFormat: "lpcm",
    audioQuality: 0x60,
    linearPCMBitDepth: 16,
    linearPCMIsBigEndian: false,
    linearPCMIsFloat: false,
  },
  android: {
    outputFormat: "default",
    audioEncoder: "default",
  },
  web: {
    mimeType: "audio/webm",
    bitsPerSecond: 128000,
  },
};

/** Whether this device can produce what the transcriber accepts. */
export const DICTATION_SUPPORTED = Platform.OS !== "android";

export function dictationUnavailableReason(): string {
  return "Dictation needs uncompressed audio, which Android's recorder does not offer. "
    + "Type instead, or dictate from the Mac itself.";
}

export async function requestMicrophone(): Promise<boolean> {
  const granted = await AudioModule.requestRecordingPermissionsAsync();
  return granted.granted;
}

/**
 * The samples inside a recording, whatever the platform wrapped them in.
 *
 * On iOS that is a WAV file whose data chunk is already the right rate and shape, so the work is
 * finding the chunk and widening 16-bit integers to floats. On the web it is Opus in a WebM
 * container, which only the browser can open — so the browser opens it, and resamples it on the
 * way out.
 */
export async function samplesFrom(uri: string): Promise<Float32Array> {
  if (Platform.OS === "web") return decodeInBrowser(uri);
  const buffer = await new File(uri).arrayBuffer();
  return fromWave(buffer);
}

/**
 * Walk a RIFF/WAVE file to its `data` chunk.
 *
 * Not `buffer.slice(44)`, which is the version of this that works until it does not: a recorder
 * is free to put `LIST`/`INFO` chunks before the audio, and the fixed offset then reads metadata
 * as sound and produces a transcript of static.
 */
function fromWave(buffer: ArrayBuffer): Float32Array {
  const view = new DataView(buffer);
  const ascii = (offset: number) =>
    String.fromCharCode(view.getUint8(offset), view.getUint8(offset + 1), view.getUint8(offset + 2), view.getUint8(offset + 3));

  if (buffer.byteLength < 12 || ascii(0) !== "RIFF" || ascii(8) !== "WAVE") {
    throw new Error("That recording is not in the format Frank expected.");
  }

  let cursor = 12;
  let bitsPerSample = 16;
  let channels = 1;
  while (cursor + 8 <= buffer.byteLength) {
    const identifier = ascii(cursor);
    const size = view.getUint32(cursor + 4, true);
    const body = cursor + 8;
    if (identifier === "fmt ") {
      channels = view.getUint16(body + 2, true);
      bitsPerSample = view.getUint16(body + 14, true);
    } else if (identifier === "data") {
      return widen(view, body, Math.min(size, buffer.byteLength - body), bitsPerSample, channels);
    }
    // Chunks are word-aligned: an odd size is followed by a pad byte that is not counted in it.
    cursor = body + size + (size % 2);
  }
  throw new Error("That recording had no audio in it.");
}

function widen(view: DataView, offset: number, length: number, bitsPerSample: number, channels: number): Float32Array {
  if (bitsPerSample !== 16) throw new Error(`Frank cannot read ${bitsPerSample}-bit audio.`);
  const total = Math.floor(length / 2);
  const frames = Math.floor(total / channels);
  const samples = new Float32Array(frames);
  for (let frame = 0; frame < frames; frame += 1) {
    // Only the first channel. The recorder is asked for mono, and a device that gives stereo
    // anyway is better downmixed by taking one side than by having its two sides summed into
    // something the transcriber has never heard.
    samples[frame] = view.getInt16(offset + frame * channels * 2, true) / 32768;
  }
  return samples;
}

/** The browser build: let the browser decode its own container, then resample to 16 kHz. */
async function decodeInBrowser(uri: string): Promise<Float32Array> {
  const encoded = await (await fetch(uri)).arrayBuffer();
  const context = new AudioContext();
  try {
    const decoded = await context.decodeAudioData(encoded);
    const frames = Math.ceil(decoded.duration * DICTATION_SAMPLE_RATE);
    const offline = new OfflineAudioContext(1, frames, DICTATION_SAMPLE_RATE);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start();
    const resampled = await offline.startRendering();
    return resampled.getChannelData(0).slice();
  } finally {
    await context.close();
  }
}

export { useAudioRecorder };

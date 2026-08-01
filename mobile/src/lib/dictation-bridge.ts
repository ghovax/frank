/**
 * Recording, on behalf of a page that is not allowed to.
 *
 * A browser will only open a microphone in a secure context, and the interface arrives on this
 * phone over plain HTTP from a private address — so inside the webview `navigator.mediaDevices`
 * is not refused, it is simply not there. The browser is right about this and a private-network
 * address is never going to become secure, so the page cannot be the one to fix it.
 *
 * The shell can. It holds a real microphone permission, granted to the app by the operating
 * system, and `expo-audio` hands back exactly what the daemon wants — mono float32 at 16 kHz,
 * the same shape the Web Audio path produces in a browser, with no container and no codec
 * anywhere in between.
 *
 * What crosses back into the page is the sentence, not the audio. The shell posts the samples to
 * the same daemon the page is already talking to, because it has the endpoint and the token
 * anyway; a minute of speech is nearly four megabytes of float32, and moving that through a
 * webview bridge as base64 would be an expensive way to carry something neither side reads.
 *
 * This is the pattern the shell exists for: not a second implementation of the interface, but
 * the few things a page genuinely cannot do — the camera, the keychain, and now this.
 */

import { AudioModule, requestRecordingPermissionsAsync, setAudioModeAsync, type AudioStream } from "expo-audio";

// What the model expects, and what `src/frank/rest/routes/dictation.py` reads off the wire.
const SAMPLE_RATE = 16000;

/** One message from the page. `id` is the page's, and comes back untouched. */
interface Request {
  channel: "dictation";
  id: number;
  kind: "start" | "stop" | "cancel";
}

export function isDictationRequest(message: unknown): message is Request {
  return typeof message === "object" && message !== null && (message as Request).channel === "dictation";
}

/**
 * The recording in progress, if there is one.
 *
 * A module-level single rather than something held in a component: there is one microphone, the
 * page's toggle is the only thing that opens it, and a second concurrent take is not a state
 * either side can express. Kept out of React deliberately — nothing on screen renders from it,
 * and putting it in state would re-render the webview host on every buffer that arrives.
 */
let active: { stream: AudioStream; chunks: Float32Array[]; rate: number } | null = null;

function release(): void {
  if (!active) return;
  active.stream.stop();
  active = null;
}

async function start(): Promise<void> {
  release();
  const permission = await requestRecordingPermissionsAsync();
  if (!permission.granted) {
    throw new Error("Frank was not allowed to use the microphone. Grant it in Settings and try again.");
  }
  // Without this the recording category is never entered on iOS and the input node hands back
  // silence — a take that succeeds, transcribes, and produces nothing, which is the worst of the
  // available failures because it looks like the model not hearing you.
  await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true });

  const chunks: Float32Array[] = [];
  // `AudioModule` is a native module resolved at runtime, so a static import checker cannot see
  // its members and reports this one as missing. TypeScript, which has the module's declared
  // shape, is satisfied — and `useAudioStream` reaches the class exactly this way.
  // eslint-disable-next-line import/namespace
  const stream = new AudioModule.AudioStream({ sampleRate: SAMPLE_RATE, channels: 1, encoding: "float32" });
  const take = { stream, chunks, rate: SAMPLE_RATE };
  stream.addListener("audioStreamBuffer", (buffer) => {
    // Guarded on identity rather than on a flag: a buffer posted before the native side saw the
    // stop belongs to the take that is ending, and appending it to a *later* take would splice
    // the tail of one recording onto the head of the next.
    if (active !== take) return;
    take.rate = buffer.sampleRate;
    chunks.push(new Float32Array(buffer.data));
  });
  await stream.start();
  active = take;
}

async function stop(transcribe: (samples: Float32Array) => Promise<string>): Promise<string> {
  const take = active;
  if (!take) return "";
  active = null;
  take.stream.stop();
  const total = take.chunks.reduce((count, chunk) => count + chunk.length, 0);
  const samples = new Float32Array(total);
  let offset = 0;
  for (const chunk of take.chunks) {
    samples.set(chunk, offset);
    offset += chunk.length;
  }
  // Below one sample the daemon answers 400, and "the recording was empty" is a worse thing to
  // show somebody who simply tapped the button twice than nothing at all.
  if (samples.length === 0) return "";
  // The model takes 16 kHz and nothing else, and there is no resampler here on purpose. The
  // native stream already ran one — `AudioStream` converts the hardware's rate to the requested
  // one with the platform's own audio framework — so this only fires if that conversion could
  // not be set up at all. Writing a second resampler for that case would mean writing it badly:
  // decimating 48 kHz without a low-pass folds every consonant above 8 kHz back down into the
  // speech band, and mangled words that transcribe into nonsense are worse to receive than a
  // sentence saying what went wrong.
  if (take.rate !== SAMPLE_RATE) {
    throw new Error(`This phone recorded at ${Math.round(take.rate)} Hz and the model needs ${SAMPLE_RATE} Hz.`);
  }
  return await transcribe(samples);
}

/**
 * Answer one message from the page.
 *
 * `transcribe` is passed in rather than imported so this file never has to know how the shell
 * addresses the daemon; the caller already holds that.
 *
 * Returns the JavaScript to inject back, which is how a webview bridge replies.
 */
export async function handleDictationRequest(
  request: Request,
  transcribe: (samples: Float32Array) => Promise<string>,
): Promise<string> {
  try {
    if (request.kind === "start") {
      await start();
      return settlement(request.id, "", "");
    }
    if (request.kind === "cancel") {
      release();
      return settlement(request.id, "", "");
    }
    return settlement(request.id, "", await stop(transcribe));
  } catch (caught) {
    release();
    return settlement(request.id, caught instanceof Error ? caught.message : String(caught), "");
  }
}

/**
 * The reply, as the one thing a webview bridge can carry back: a line of JavaScript.
 *
 * Its whole body is a call into a function the *page* defines — `frankDictationSettle`, in
 * `web/src/lib/dictation.ts`. The page-side half of this conversation is written there, in
 * TypeScript that lints and diffs like everything else, rather than in a template string this
 * file injects; the page can already tell it is inside a React Native webview, so there was
 * never anything for the shell to install. What crosses here is three arguments and no logic.
 */
function settlement(id: number, failure: string, value: string): string {
  return `window.frankDictationSettle && window.frankDictationSettle(${id}, ${JSON.stringify(failure)}, ${JSON.stringify(value)}); true;`;
}

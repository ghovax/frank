// The audio-thread half of dictation: take every block of microphone samples and hand it to the page.
class DictationCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    // One input, first channel, with a momentarily absent channel treated as the graph still connecting.
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length > 0) {
      // Copied on the way out, since the audio thread reuses the buffer for the next block.
      this.port.postMessage(new Float32Array(channel));
    }
    return true;
  }
}

registerProcessor("dictation-capture", DictationCaptureProcessor);

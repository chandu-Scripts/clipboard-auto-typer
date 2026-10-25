/* Capture mono PCM in bounded chunks without running audio work on the UI thread. */
class AnswerBoardPCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Int16Array(2048);
    this.offset = 0;
    this.finished = false;
    this.port.onmessage = (event) => {
      if (event.data.type === "finish") {
        this.finished = true;
        this.flush();
        this.port.postMessage({type:"flushed"});
      }
    };
  }
  flush() {
    if (!this.offset) return;
    const data = this.buffer.slice(0, this.offset).buffer;
    this.port.postMessage(data, [data]);
    this.offset = 0;
  }
  process(inputs) {
    if (this.finished) return false;
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      for (let i=0; i<channel.length; i++) {
        const sample = Math.max(-1, Math.min(1, channel[i]));
        this.buffer[this.offset++] = sample < 0 ? sample * 32768 : sample * 32767;
        if (this.offset === this.buffer.length) this.flush();
      }
    }
    return true;
  }
}
registerProcessor("answer-board-pcm", AnswerBoardPCM);

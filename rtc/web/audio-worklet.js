// P0 冒烟采集处理器：任意输入采样率 → 降采样到 16k PCM16，攒 2048 样本（128ms）一包
// 注意：Android Chrome 的 AudioContext 常不支持强制 16k（抛 NotSupportedError），
// 因此采集侧用 context 实际采样率（sampleRate 全局变量），在 worklet 里均值降采样。
class CaptureProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.targetRate = 16000;
        this.ratio = sampleRate / this.targetRate; // e.g. 48000/16000 = 3
        this._acc = 0.0;
        this._count = 0;
        this._buffer = new Int16Array(2048);
        this._offset = 0;
    }

    _push(sample) {
        const v = Math.max(-32768, Math.min(32767, Math.round(sample * 32767)));
        this._buffer[this._offset++] = v;
        if (this._offset === this._buffer.length) {
            this.port.postMessage(this._buffer);
            this._buffer = new Int16Array(2048);
            this._offset = 0;
        }
    }

    process(inputs) {
        const input = inputs[0];
        if (input && input[0]) {
            const channel = input[0];
            for (let i = 0; i < channel.length; i++) {
                this._acc += channel[i];
                this._count++;
                if (this._count >= this.ratio) {
                    this._push(this._acc / this._count); // 均值滤波抗混叠
                    this._acc = 0.0;
                    this._count = 0;
                }
            }
        }
        return true;
    }
}

registerProcessor("capture-processor", CaptureProcessor);

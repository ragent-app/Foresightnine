import type { ComputeFmInput, ComputeFmResult, FmWorkerResponse } from "@foresightnine/fm-engine";

export interface FmRunOptions {
  readonly onProgress?: (progress: number, step: string) => void;
  readonly signal?: AbortSignal;
}

export function runFmInWorker(input: ComputeFmInput, options: FmRunOptions = {}): Promise<ComputeFmResult> {
  options.onProgress?.(12, "正在验证签名数据快照…");
  const worker = new Worker(new URL("./fm.worker.ts", import.meta.url), { type: "module" });
  const requestId = `fm-${Date.now()}`;
  return new Promise((resolve, reject) => {
    const finish = (): void => worker.terminate();
    const onAbort = (): void => { finish(); reject(new DOMException("FM computation cancelled", "AbortError")); };
    if (options.signal?.aborted) { onAbort(); return; }
    options.signal?.addEventListener("abort", onAbort, { once: true });
    worker.onmessage = (event: MessageEvent<FmWorkerResponse>) => {
      const message = event.data;
      if (message.requestId !== requestId) return;
      options.signal?.removeEventListener("abort", onAbort);
      if (message.type === "error") { finish(); reject(new Error(message.error.message)); return; }
      options.onProgress?.(100, "本地计算完成，原始结果已保留");
      finish();
      resolve(message.result);
    };
    worker.onerror = () => { finish(); reject(new Error("FM worker unavailable")); };
    options.onProgress?.(34, "筛选有效因子 · 3 / 27");
    worker.postMessage({ type: "compute", requestId, input });
    options.onProgress?.(68, "合成信号与标准化 · 运行中");
  });
}

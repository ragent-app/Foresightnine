export type WorkspaceSaveStatus = "loading" | "saving" | "saved" | "error";
/** Latest-value, serialized, bounded debounce; no network/model operations. */
export function createWorkspaceAutosave<T>(save: (value: T) => Promise<unknown>, status: (value: WorkspaceSaveStatus) => void, delay = 600) {
  let pending: T | undefined; let timer: ReturnType<typeof setTimeout> | undefined; let running: Promise<void> | null = null; let closed = false; let invalid = false; let last = "";
  const clear = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined; };
  const flush = (): Promise<void> => {
    clear(); if (running) return running;
    running = (async () => {
      while (pending !== undefined) {
        const value = pending; pending = undefined;
        try { await save(value); }
        catch { if (pending === undefined) pending = value; if (!closed) status("error"); return; }
      }
      if (!closed && !invalid) status("saved");
    })().finally(() => { running = null; });
    return running;
  };
  return {
    schedule(value: T) { if (closed) return; const encoded = JSON.stringify(value); if (encoded === last) return; last = encoded; invalid = false; pending = JSON.parse(encoded) as T; clear(); status("saving"); timer = setTimeout(() => { void flush(); }, delay); },
    flush,
    reject() { if (closed) return; clear(); pending = undefined; last = ""; invalid = true; status("error"); },
    resume() { closed = false; },
    retry() { if (!closed && pending !== undefined) { status("saving"); return flush(); } return Promise.resolve(); },
    close() { if (closed) return; clear(); closed = true; void flush(); },
  };
}

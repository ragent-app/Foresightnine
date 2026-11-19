/** Saves a blob through a temporary object URL (no inline script or data: URL, CSP-safe). */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function saveJson(data: unknown, filename: string): void {
  saveBlob(new Blob([`${JSON.stringify(data, null, 2)}\n`], { type: "application/json" }), filename);
}

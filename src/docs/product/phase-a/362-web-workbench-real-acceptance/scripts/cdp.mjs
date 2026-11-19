// Minimal Chrome DevTools Protocol driver for local UI QA (no dependencies).
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function launch({ port = 9333, scratch }) {
  const profile = mkdtempSync(join(scratch ?? tmpdir(), "chrome-"));
  const chrome = spawn("/usr/bin/google-chrome", [
    "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check",
    "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2", "about:blank",
  ], { stdio: "ignore" });
  let targets;
  for (let i = 0; i < 50; i++) {
    try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); if (targets.length) break; } catch {}
    await sleep(200);
  }
  const page = targets.find((t) => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r, { once: true }));
  let id = 0;
  const pending = new Map();
  const listeners = [];
  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) { const { resolve, reject } = pending.get(msg.id); pending.delete(msg.id); msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result); }
    else listeners.forEach((fn) => fn(msg));
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => { const n = ++id; pending.set(n, { resolve, reject }); ws.send(JSON.stringify({ id: n, method, params })); });
  const logs = [];
  listeners.push((msg) => {
    if (msg.method === "Runtime.consoleAPICalled") logs.push(`console.${msg.params.type}: ${msg.params.args.map((a) => a.value ?? a.description ?? "").join(" ")}`);
    if (msg.method === "Log.entryAdded") logs.push(`log.${msg.params.entry.level}: ${msg.params.entry.text}`);
    if (msg.method === "Runtime.exceptionThrown") logs.push(`exception: ${msg.params.exceptionDetails.exception?.description ?? msg.params.exceptionDetails.text}`);
  });
  await send("Page.enable"); await send("Runtime.enable"); await send("Log.enable");
  await send("Page.addScriptToEvaluateOnNewDocument", { source: "window.__csp=[];document.addEventListener('securitypolicyviolation',e=>window.__csp.push(e.violatedDirective+' '+(e.blockedURI||'')+' '+(e.sample||'')));" });

  const api = {
    send, logs,
    async viewport(width, height, { mobile = false, dark = false } = {}) {
      await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 2, mobile });
      await send("Emulation.setTouchEmulationEnabled", { enabled: mobile });
      await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: dark ? "dark" : "light" }, { name: "pointer", value: mobile ? "coarse" : "fine" }] });
    },
    async goto(url, wait = 1200) { await send("Page.navigate", { url }); await sleep(wait); },
    async eval(expression) {
      const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
      return r.result.value;
    },
    async waitFor(selector, timeout = 8000) {
      const until = Date.now() + timeout;
      while (Date.now() < until) { if (await api.eval(`!!document.querySelector(${JSON.stringify(selector)})`)) return true; await sleep(150); }
      throw new Error(`timeout waiting for ${selector}`);
    },
    async waitText(text, timeout = 15000) {
      const until = Date.now() + timeout;
      while (Date.now() < until) { if (await api.eval(`document.body.innerText.includes(${JSON.stringify(text)})`)) return true; await sleep(200); }
      throw new Error(`timeout waiting for text ${text}`);
    },
    /** Click the first element matching selector whose text includes `text` (if given), with real mouse events. */
    async click(selector, text) {
      const box = await api.eval(`(() => { const els=[...document.querySelectorAll(${JSON.stringify(selector)})]; const el=els.find(e=>${text ? `e.textContent.includes(${JSON.stringify(text)})` : "true"}); if(!el) return null; el.scrollIntoView({block:'center'}); const r=el.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()`);
      if (!box) throw new Error(`no element ${selector} ${text ?? ""}`);
      for (const type of ["mousePressed", "mouseReleased"]) await send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
      await sleep(350);
    },
    async type(selector, value) {
      await api.eval(`document.querySelector(${JSON.stringify(selector)}).focus()`);
      await send("Input.insertText", { text: value });
      await sleep(150);
    },
    async key(key, modifiers = 0) {
      const code = key === "Enter" ? 13 : key === "Escape" ? 27 : 0;
      await send("Input.dispatchKeyEvent", { type: "keyDown", key, windowsVirtualKeyCode: code, modifiers, ...(key === "Enter" ? { text: "\r" } : {}) });
      await send("Input.dispatchKeyEvent", { type: "keyUp", key, windowsVirtualKeyCode: code, modifiers });
      await sleep(250);
    },
    async shot(path) { const { data } = await send("Page.captureScreenshot", { format: "png" }); writeFileSync(path, Buffer.from(data, "base64")); },
    async csp() { return api.eval("window.__csp"); },
    close() { try { ws.close(); } catch {} chrome.kill("SIGKILL"); },
  };
  return api;
}
export { sleep };

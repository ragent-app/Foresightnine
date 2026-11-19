// 363 controlled acceptance rerun: real backend (127.0.0.1:8002) + real model through the Web UI (4176).
// Each step is checkpointed in state.json so a script failure never resubmits a question.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { launch, sleep } from "./cdp.mjs";

const BASE = process.env.QA_BASE ?? "http://127.0.0.1:4176";
const OUT = process.env.QA_OUT;
const SOURCE = readFileSync(process.env.QA_SOURCE, "utf8");
mkdirSync(OUT, { recursive: true });
const STATE = `${OUT}/state.json`;
const state = existsSync(STATE) ? JSON.parse(readFileSync(STATE, "utf8")) : { done: {}, continued: {}, log: [] };
const save = () => writeFileSync(STATE, JSON.stringify(state, null, 2));
const note = (text) => { state.log.push(`${new Date().toISOString()} ${text}`); save(); console.log(text); };

const EMAIL = "acceptance-363@example.com";
const PASSWORD = "acceptance-363-password";
const Q_A1 = "仅依据所附微软 2026 年报收入事实，比较截至 2026-06-30 和 2025-06-30 的年度总收入；分别给出金额、币种、期间，使用确定性计算工具计算同比增长率、展示计算输入及公式，并引用支持来源，说明资料局限。";
const Q_A2 = "这两个年度的营业利润率有没有变化？如果资料里没有相关数据，请明确说明缺少什么，不要推测。";
const Q_B = "微软 2026 财年的总收入是多少？与上一财年相比增长了多少？请引用可核对的来源。";

const b = await launch({ scratch: process.env.QA_SCRATCH, port: 9340 });
await b.viewport(1440, 900);

const text = () => b.eval("document.body.innerText");
const setPanelField = (label, value) => b.eval(`(() => {
  const l = [...document.querySelectorAll('.panel label')].find(x => x.textContent.trim().startsWith(${JSON.stringify(label)}));
  if (!l) throw new Error('no label ' + ${JSON.stringify(label)});
  const el = document.getElementById(l.htmlFor);
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, ${JSON.stringify(value)});
  el.dispatchEvent(new Event('input', { bubbles: true }));
  return true; })()`);
const api = (path) => b.eval(`(async () => {
  const s = JSON.parse(sessionStorage.getItem('foresightnine.web.session.v2'));
  const r = await fetch(${JSON.stringify(path)}, { headers: { Authorization: 'Bearer ' + s.access_token } });
  return { status: r.status, body: await r.json() };
})()`);
const topicIdFromUrl = () => b.eval("location.pathname.split('/').pop()");

async function login() {
  await b.goto(`${BASE}/login`);
  await b.click("summary", "开发环境本地账户");
  await b.type('input[type="email"]', EMAIL);
  await b.type('input[type="password"]', PASSWORD);
  await b.click("button", state.done.registered ? "登录" : "注册");
  await b.waitText("你想研究什么？", 20000);
  state.done.registered = true; save();
}

/** Research settings panel: search/fetch switches and the fixed budget (preregistration). */
async function applySettings({ search, fetch, searchCalls }) {
  await b.click('button[aria-label="研究设置"]');
  await b.waitText("默认设置适合大多数问题");
  await b.click('[aria-label="联网搜索"] button', search);
  await b.click('[aria-label="自动获取官方资料"] button', fetch);
  const open = await b.eval("[...document.querySelectorAll('.panel details.more')].find(d => d.textContent.includes('预算'))?.open");
  if (!open) await b.click(".panel details.more > summary", "预算");
  await setPanelField("模型调用上限", "12");
  await setPanelField("Token 上限", "150000");
  await setPanelField("运行时长上限", "20");
  await setPanelField("搜索次数上限", searchCalls ?? "");
  await sleep(200);
  await b.shot(`${OUT}/settings-${Date.now()}.png`);
  await b.click(".panel .foot .btn.primary", "应用");
  await sleep(400);
}

async function waitRun(label) {
  await b.waitFor(".livebar", 60000);
  const started = Date.now();
  let shots = 0;
  note(`${label}: running`);
  while (await b.eval("!!document.querySelector('.livebar')")) {
    if (Date.now() - started > 26 * 60 * 1000) throw new Error(`${label}: still running after 26 min`);
    if (shots < 3 && Date.now() - started > (shots + 1) * 25000) {
      await b.shot(`${OUT}/${label}-running-${shots + 1}.png`);
      const bar = await b.eval("document.querySelector('.livebar')?.innerText ?? ''");
      note(`${label}: livebar "${bar.replace(/\s+/g, " ")}"`);
      shots += 1;
    }
    await sleep(2000);
  }
  note(`${label}: finished after ${Math.round((Date.now() - started) / 1000)} s`);
  await sleep(2500);
  await b.shot(`${OUT}/${label}-done.png`);
}

async function snapshot(label) {
  const id = await topicIdFromUrl();
  const topic = await api(`/v2/research/topics/${id}`);
  const runs = await api(`/v2/research/topics/${id}/runs`);
  writeFileSync(`${OUT}/raw-${label}.json`, JSON.stringify({ topic: topic.body, runs: runs.body }, null, 2));
  return { id, topic: topic.body, runs: runs.body };
}

/** Preregistered: at most one 继续研究 per topic when recovery allows it. */
async function maybeContinue(topicKey, label) {
  await sleep(3000);
  const primary = await b.eval("(() => { const x = document.querySelector('.callout .actions .btn.primary'); return x ? { text: x.textContent, disabled: x.disabled } : null; })()");
  note(`${label}: callout primary ${JSON.stringify(primary)}`);
  if (!primary || primary.text !== "继续研究" || primary.disabled || state.continued[topicKey]) return false;
  state.continued[topicKey] = label; save();
  await b.click(".callout .actions .btn.primary", "继续研究");
  await waitRun(`${label}-continue`);
  return true;
}

async function inspect(label) {
  await b.click('button[aria-label="研究记录"]');
  for (const [tab, name] of [["结论", "claims"], ["资料", "sources"], ["数字", "numbers"], ["过程", "steps"]]) {
    await b.click('[role="tab"]', tab);
    await sleep(500);
    await b.shot(`${OUT}/${label}-record-${name}.png`);
  }
  await b.key("Escape");
  await sleep(300);
  const hasCite = await b.eval("!!document.querySelector('.claims .cite-n')");
  if (hasCite) {
    await b.click(".claims .cite-n");
    await b.waitFor(".pop");
    await sleep(300);
    await b.shot(`${OUT}/${label}-cite.png`);
    await b.click(".pop button", "查看原文");
    const marked = await b.waitFor("mark.on", 8000).then(() => true).catch(() => false);
    note(`${label}: reader highlight ${marked}`);
    await sleep(500);
    await b.shot(`${OUT}/${label}-reader.png`);
    await b.key("Escape");
    await sleep(300);
  } else {
    note(`${label}: no citation number in the answer`);
  }
}

try {
  await login();

  if (!state.done.A1) {
    await b.click('button[aria-label="添加资料"]');
    await b.click('[role="tab"]', "文本");
    await setPanelField("标题", "微软 2026 财年年报收入摘录");
    await setPanelField("正文", SOURCE);
    await sleep(200);
    await b.shot(`${OUT}/A0-add-source.png`);
    await b.click(".panel .foot .btn.primary", "添加");
    await b.waitText("资料：微软 2026 财年年报收入摘录", 20000);
    await applySettings({ search: "关闭", fetch: "关闭" });
    await b.type('textarea[aria-label="研究问题"]', Q_A1);
    await b.shot(`${OUT}/A0-home-ready.png`);
    state.done.A1 = "submitting"; save();
    await b.key("Enter", 2);
    await b.waitFor(".large-title", 30000);
    state.topicA = await topicIdFromUrl();
    state.done.A1 = "submitted"; save();
    note(`A1 submitted on topic ${state.topicA}`);
    await waitRun("A1");
    await maybeContinue("A", "A1");
    await snapshot("A1");
    await inspect("A1");
    state.done.A1 = "done"; save();
  }

  if (state.done.A2 !== "done") {
    await b.goto(`${BASE}/research/${state.topicA}`, 3000);
    await b.waitFor(".large-title");
    if (!state.done.A2) {
      await applySettings({ search: "关闭", fetch: "关闭" });
      await b.type('textarea[aria-label="追问"]', Q_A2);
      state.done.A2 = "submitting"; save();
      await b.key("Enter", 2);
      state.done.A2 = "submitted"; save();
      note("A2 submitted");
      await waitRun("A2");
      await maybeContinue("A", "A2");
    }
    await snapshot("A2");
    await inspect("A2");
    state.done.A2 = "done"; save();
  }

  if (state.done.review !== "done") {
    await b.goto(`${BASE}/research/${state.topicA}`, 3000);
    await b.waitFor(".large-title");
    await b.click(".large-title .meta .meta-link");
    await b.waitText("版本历史");
    await setPanelField("你的研究总结", "受控验收 362：依据所附收入摘录，FY2026 总收入 331,839 百万美元，FY2025 为 281,724 百万美元；资料不含营业利润，无法判断利润率变化。");
    await b.shot(`${OUT}/A3-review.png`);
    await b.click(".panel .foot .btn.primary", "保存为新版本");
    await b.waitText("已保存为", 20000);
    await sleep(600);
    await b.shot(`${OUT}/A3-saved.png`);
    await b.key("Escape");
    const snap = await snapshot("A3");
    const latest = snap.topic.versions[snap.topic.versions.length - 1];
    const exported = await api(`/v2/research/topics/${snap.id}/versions/${latest.version_id}/export`);
    writeFileSync(`${OUT}/export-A.json`, JSON.stringify(exported, null, 2));
    note(`A3 review saved; export status ${exported.status}`);
    state.done.review = "done"; save();
  }

  if (state.done.B !== "done") {
    if (!state.done.B) {
      await b.goto(`${BASE}/research`, 2500);
      await b.waitText("你想研究什么？");
      await applySettings({ search: "开启", fetch: "自动", searchCalls: "3" });
      await b.type('textarea[aria-label="研究问题"]', Q_B);
      await b.shot(`${OUT}/B0-home-ready.png`);
      state.done.B = "submitting"; save();
      await b.key("Enter", 2);
      await b.waitFor(".large-title", 30000);
      state.topicB = await topicIdFromUrl();
      state.done.B = "submitted"; save();
      note(`B submitted on topic ${state.topicB}`);
      await waitRun("B1");
      await maybeContinue("B", "B1");
    } else {
      // Resumed after the harness was interrupted while B ran: apply the same
      // preregistered at-most-one 继续研究 rule the uninterrupted path applies.
      await b.goto(`${BASE}/research/${state.topicB}`, 3000);
      await b.waitFor(".large-title");
      note("B1 resumed after harness interruption");
      await sleep(2000);
      if (!(await b.eval("!!document.querySelector('.livebar')"))) await maybeContinue("B", "B1");
    }
    const snap = await snapshot("B1");
    await inspect("B1");
    const latest = snap.topic.versions[snap.topic.versions.length - 1];
    const exported = await api(`/v2/research/topics/${snap.id}/versions/${latest.version_id}/export`);
    writeFileSync(`${OUT}/export-B.json`, JSON.stringify(exported, null, 2));
    note(`B export status ${exported.status}`);
    state.done.B = "done"; save();
  }
} catch (error) {
  note(`SCRIPT ERROR: ${error.message}`);
  try { await b.shot(`${OUT}/script-error-${Date.now()}.png`); } catch {}
} finally {
  const csp = await b.csp().catch(() => ["(csp read failed)"]);
  const errors = b.logs.filter((line) => /error|exception|Refused|violat/i.test(line));
  state.cspViolations = [...(state.cspViolations ?? []), ...(csp ?? [])];
  state.consoleErrors = [...(state.consoleErrors ?? []), ...errors];
  save();
  console.log(`CSP violations (${csp?.length ?? 0}) / console errors (${errors.length})`);
  b.close();
}

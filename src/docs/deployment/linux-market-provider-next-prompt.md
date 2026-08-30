# Linux agent next-step prompt: production wiring only

Copy the following prompt to the Linux deployment agent after dispatching the
Linux goal. This is an execution brief, not authorization by itself.

```text
你负责 ForesightNine 的 Linux 部署与服务对接。开始前完整阅读：

- plan.md
- agent.md
- src/README.md
- src/docs/progress.md
- src/docs/architecture/client-market-data-runtime.md
- src/docs/data/market-snapshot-bundle.md
- src/docs/data/public-provider-adaptation-plan.md
- src/docs/deployment/twopaltformpipeline.md
- src/docs/deployment/linux-production-goal.md
- src/docs/deployment/linux-agent-handoff.md
- src/infra/deployment/production-release-runbook.md

当前 Windows 已完成并推送到 origin/master 的客户端数据运行时 commit：
4862e57（包含 e93d5f1）。不要重写 plan.md/agent.md，不要修改
D:\FM acceleration、D:\FM acceleration-deepseek、D:\WL-data-final、
D:\ACFM_WL_paperwriting 或 /home/lxx/trade-agent-crisis、
/home/lxx/trade-agent-benchmark。Linux 只在新的可写应用 checkout 中工作。

目标边界：

1. Linux 只负责 FastAPI 持久化启动、PostgreSQL、Redis abuse control、
   Cloudflare DNS/Tunnel/Pages/API ingress、OAuth/account/session、用户
   注册/删除/隐私数据生命周期和生产监控。
2. Web/Android 已经负责三锚点市场数据请求、响应校验、最新交易日价格
   映射、FM 输入转换、因子选择、rank/ties/NaN、IC/ICIR 元数据使用、
   signal、权重、3 bps 成本、cost dead-band、金额、份额、FM Raw 和
   Public Default。绝不把这些计算搬到 FastAPI。
3. 产品运行时不是历史回测：每个用户只请求 year_start、
   latest_month_start、latest_completed_session 三个锚点。不要复制完整
   WL-data 或 benchmark 历史库作为生产依赖，也不要使用 Windows 路径。
4. 生产首版仍是免费 Web + 免费 Android；订阅只保留规划入口，未获批准
   不启用收费权益或 Google Play entitlement。

Windows 只读探测发现一个真实公开价格候选：

- Yahoo chart endpoint: https://query1.finance.yahoo.com/v8/finance/chart/
- PAVE、SOXX、XLU、GLD、VT 均可返回真实日线；探测区间为
  2026-01-01 至 2026-08-30，每个约 165 条记录。
- 该 endpoint 没有 Access-Control-Allow-Origin，不能由 Web 直接调用。
- Yahoo 返回的是厂商协议，不是项目的 POST /snapshots 合同；只提供价格，
  不提供当前 FM 所需的 momentum/quality 字段。
- 因此不得直接把 Yahoo URL 写入客户端环境，也不得宣称生产数据已接通。

请按以下串行步骤执行，每一步记录命令、退出码、脱敏输出、commit 和
Sol verdict，未通过不得进入下一步：

A. 先拉取并验证 commit 4862e57；安装锁定依赖；确认 Linux 应用 checkout
   可写、研究源只读；运行 portable tests/typecheck/secret scan。Windows
   绝对路径的 reference-hash/boundary 结果只引用 Windows 证据，不能为了
   Linux 通过而改基线。

B. 配置私有 PostgreSQL，应用现有 migrations；验证注册用户、OAuth session、
   research state、删除/保留策略、重启持久化、备份恢复和 migration 幂等。
   数据库端口不可暴露公网。先不实现订阅权益。

C. 配置 authenticated TLS Redis、TTL、配额、失败时 fail-closed 和真实
   proxy topology；不得回退内存 abuse control，不得信任用户伪造的
   forwarded headers。

D. 用 mock provider 启动受监督 FastAPI staging，验证健康检查、挑战、证据、
   worldline 三分支、OAuth contract 和 account lifecycle。日志必须脱敏。

E. 在 owner 已确认的 zone foresightnine.com 下，仅配置 staging：
   staging.foresightnine.com、api-staging.foresightnine.com。使用
   cloudflared 或严格反向代理；不能先猜 production hostname，不能公开
   PostgreSQL/Redis，不能把 Down 的旧 Tunnel 当作可用路由。

F. 为 /snapshots 合同选择真实 provider 的方式：优先使用已获许可、可
   CORS 或可由受控 edge proxy 转换的 provider。若临时评估 Yahoo，只能
   做无密钥、无缓存敏感数据、严格资产 allowlist 的薄代理，把 PAVE/SOXX/
   XLU/GLD/VT 映射写入可审计 manifest，并返回项目的三锚点 bundle；代理
   不得计算 FM 或伪造 factor features。没有 licensed factor/feature 合同
   时，返回 explicit missing/limited rows，让客户端禁用 Public Default，
   不得用价格推导未经批准的 momentum/quality 因子。

G. 生产 provider 接入前必须取得书面许可，确认 symbol、价格、FX、factor
   fields、时间戳、缓存、客户端再分发、地域和导出权利。没有许可时保留
   mock/staging，不把 Yahoo 或任何 demo endpoint 标成 production。

H. 只在 secret manager/environment 注入新的 server-side AI/search/OAuth
   keys；任何 key 不得进入客户端、source map、数据库、fixtures、日志或
   handoff。完成 CORS/CSP/SSRF、限流、CAPTCHA、依赖/容器审计和备份告警。

I. 返回脱敏 handoff：commit、环境、数据库/Redis/CF 状态、provider mode、
   data manifest/cutoff、测试计数、artifact hash、缺失外部批准、Sol verdict
   和下一串行步骤。明确说明真实 provider 是否仅 price-only/limited，不能
   把薄代理上线等同于 FM 可执行或公开生产。
```

## Local conclusion before Linux dispatch

Windows has completed the safe local work: the client runtime, validation,
three-anchor request path, provider modes, freshness propagation, dynamic
cutoff export, tests and documentation. The remaining blocker is not a missing
local implementation; it is the deployment-owned combination of CORS/edge
proxy, provider protocol adaptation, factor/FX licensing and persistent
services. The Yahoo probe is evidence for Linux planning only, not an approval
to use Yahoo in production.

The latest read-only provider assessment is in
`src/docs/data/public-provider-adaptation-plan.md`. It records that no
verified source currently satisfies public redistribution, target-asset
coverage and the FM feature contract together. Linux must preserve the
canonical `MarketSnapshotBundle v1` and may only add a thin, allowlisted
protocol adapter after written data-use approval. Price-only responses must
remain explicit `limited_data`; Linux must not derive or rename vendor
indicators into FM features.

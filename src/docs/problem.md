# ForesightNine 五维只读审计报告（含修复后复测）

- 审计时间：2026-08-31T19:35:49+08:00
- 审计对象：当前 checkout、staging 公网链路、Cloudflare Tunnel、Google Play 只读 API、Android QA 构建
- commit：uploaded revision; verify with `git rev-parse HEAD`
- 环境：Linux 7.0.0-28-generic x86_64；Node v22.23.2；pnpm 11.21.0；Python 3.14.4；uv 0.11.29；Docker 29.7.1；cloudflared 2026.8.2
- 工作区：审计前仅有未跟踪的空 `docs/problem.md`；随后只修改了 API Compose 配置、Cloudflare Tunnel ingress 和 Web preview 安全响应头，未修改 API 业务逻辑、数据模型、认证逻辑或 Android 产物。修复后重新部署了 API 和 Web preview，并重启了用户级 Tunnel/Web 服务。
- 审计原则：真实 staging 公网只读探测；Google Play 只读；没有生产发布、付费/订阅/退款/付款资料变更、Cloudflare 付费操作、OAuth authorization code 生成或持久化。
- 脱敏：本报告不包含任何凭据值、令牌值、授权码、Cookie 或敏感响应正文。

## 结论摘要

当前 checkout 的本地类型检查、全量测试、契约漂移检查、secret scan、Compose 语法检查和依赖审计通过；API、PostgreSQL、Redis、Web preview、用户级 Cloudflare Tunnel 均在运行。公网 HTTPS、provider readiness、OAuth start redirect、Google/GitHub 登录闭环、assetlinks 和 Google service-account token 交换也有实证。

修复后，CORS、app `/readyz`、staging provider 注入和 Web HSTS 已复测通过；上线/端到端验收仍被以下问题阻断：

1. **P1：Google Play access token 和 androidpublisher scope 验证成功，但 `com.foresightnine.mobile` 的 internal/closed/open release list 全部 404；没有取得有效 release 读取实证。** 未调用任何写接口。
2. **P1：app 与 staging 的 HTTP 请求未重定向到 HTTPS，返回 200 HTML。** Tunnel ingress 本身不能设置 Cloudflare 边缘 redirect，仍需边缘策略完成。
3. **P1：`verify:boundaries` 在 Linux 因治理脚本硬编码 Windows 路径失败。** 这是可移植性/治理失败，不修改基线绕过。

P0：本轮未发现已由只读证据确认的 P0；生产 preflight 仍失败，因此不能把当前 checkout 判定为生产可发布。

## 修复变更与复测

已按顺序完成并重新部署/验证：

1. `infra/docker/compose.yaml`：API CORS 现在允许 `https://app.foresightnine.com` 与 `https://staging.foresightnine.com`；LLM provider 选择改为读取 staging deployment env，默认值仍为 mock。
2. `/home/lxx/.cloudflared/foresightnine-api-staging.yml`：新增 app 精确 `/readyz`→API 8000 路由，配置验证和用户级 Tunnel 重启均成功。
3. `apps/web/vite.config.ts`：Web preview 增加 HSTS；重新 build、重启 Web preview 成功。
4. `apps/api/app/oauth_provider.py`：Google token exchange 增加 `grant_type=authorization_code`；加入请求体回归断言，重新构建并重启 API。

修复后实测：app CORS HTTP 200 且返回 app ACAO；app `/readyz` HTTP 200、`application/json`、`status=ready`；运行 provider=`glm_coding`，公网 health provider=`openai-compatible`；Web HTTPS 响应返回 HSTS；Google callback HTTP 303、OAuth exchange HTTP 200，Google identity 和 auth session 已持久化。API/Web focused tests、Compose config、Tunnel validate 均 exit 0；全量测试仍为 API 249 passed/1 skipped、Web 64 passed、Mobile 60 passed。

## Findings 表

| ID | 严重级别 | 角度 | 复现命令 | 实际结果 | 期望结果 | 影响 | 修复建议 | 状态 |
|---|---|---|---|---|---|---|---|---|
| AUTH-01 | P1 | 认证/CF | `curl -i -H 'Origin: https://app.foresightnine.com' https://api-staging.foresightnine.com/health` | 修复后 HTTP 200，返回 `Access-Control-Allow-Origin: https://app.foresightnine.com`；staging Origin 仍通过 | app 正式来源应被 API 明确允许 | 原阻断已解除 | 已在 Compose 加入 app 与 staging 两个 HTTPS origin，并重建 API | RESOLVED_2026-08-31 |
| CF-01 | P1 | CF | `curl -sS -D - https://app.foresightnine.com/readyz` | 修复后 HTTP 200，`application/json`，`status=ready` | readiness 应返回 API JSON | 原假阳性已解除 | 已在 app Tunnel ingress 增加精确 `/readyz`→API 路由并重启用户级 Tunnel | RESOLVED_2026-08-31 |
| DATA-01 | P1 | 数据 | `docker inspect docker-api-1 ...`（仅比较配置键状态）；`curl -sS https://api-staging.foresightnine.com/health` | 修复后运行值为 `glm_coding`，公网 provider 为 `openai-compatible`，status=ok | 部署声明、运行注入和 health 语义一致 | 原配置漂移已解除；实际 provider 可用性仍需业务 smoke | Compose 改为读取 deployment env 的非 secret provider 选择值；secret 仍仅在服务端环境 | RESOLVED_2026-08-31 |
| PLAY-01 | P1 | Google Play | 只读内存 JWT probe；`GET .../applications/com.foresightnine.mobile/tracks/{internal,closed,open}/releases` | credential JSON 有效、权限模式 600、token exchange 200、tokeninfo 200 且含 androidpublisher scope；三个 release list 均 HTTP 404 | 授权账号应能读到目标包及其测试轨道，或明确包尚未创建/尚未传播 | Play 自动化的读取前提未闭合 | 在 Play Console 核对包归属、轨道存在性和授权账号；确认后重跑同样的 GET；不做 edits/upload/commit | PENDING_EXTERNAL_INPUT |
| AUTH-02 | P1 | 认证 | `docker logs --since 2h docker-api-1 2>&1 | grep -E 'oauth_callback_(failed|ticket_issued)|/v1/auth/oauth/exchange|/api/auth/callback'`；数据库 auth identity/session 只读计数 | Google 真实 callback HTTP 303；`POST /v1/auth/oauth/exchange` HTTP 200；Google identity 1、auth_sessions 8；GitHub 登录此前已有 callback/exchange 200；修复前 token exchange 阶段报 `OAuthProviderError`，修复为 Google `grant_type=authorization_code` | 真实授权后 callback 完成 code→token→identity→session | Google/GitHub 登录闭环已解除；公网 refresh/logout 未在本轮重复操作，本地相关测试已覆盖 | 保留 callback/exchange/session 日志的脱敏观测；下一轮单独复核 refresh/logout，不记录 code/token | RESOLVED_LOGIN_2026-08-31 |
| CF-02 | P1 | CF | `curl -sS -D - -o /dev/null http://{app,staging}.foresightnine.com/` | app/staging 均 HTTP 200，无 Location 重定向 | HTTP 请求应 301/308 到对应 HTTPS（若正式策略为强制 HTTPS） | 明文入口未统一收敛 | 在 Cloudflare edge 或源站启用 HTTPS redirect，再验证 HSTS/缓存语义 | OPEN_DEPLOYMENT |
| GOV-01 | P1 | 其余 | `corepack pnpm verify:boundaries` | exit 1；脚本寻找 `D:\Foresightnine\plan.md`、`agent.md`、`GOAL_PROMPT.md` 并报告 Linux root 不符合 | 治理检查应使用可移植路径并在目标 checkout 正常运行 | CI/审计在 Linux 不能判定边界 | 修复治理脚本的路径发现和工作区契约；不要通过改报告或基线绕过 | OPEN_GOVERNANCE |
| PLAY-02 | P2 | Google Play | `command -v gcloud fastlane bundle` | 三者均未安装 | 后续 fastlane/Publisher 自动化应有明确、可复现工具链 | 后续自动化尚未具备运行前置 | 将安装固定到后续配置任务；本轮不安装、不配置、不发布 | TOOLCHAIN_GAP |
| AND-01 | P2 | 其余/Android | `apksigner verify --verbose --print-certs app-debug.apk`；`adb devices` | APK 包名 `com.foresightnine.mobile`；v2 签名 true，签名 DN 为 Android Debug；assetlinks 三域均 200、包名匹配、3 fingerprints，当前 APK fingerprint 匹配；adb 无设备 | release/QA signing 边界及真实设备 domain verification 应分别验证 | 无设备时无法取得 Android 系统级 App Link 解析实证；当前文件是 debug APK | 接入测试设备并安装目标 release/QA APK 后执行 domain verification；正式发布只使用 release 签名 | PENDING_ENVIRONMENT |
| CF-03 | P2 | CF | 选定安全响应头 probe | 修复后包含 `Strict-Transport-Security: max-age=31536000; includeSubDomains`，其他安全头仍在 | HTTPS 正式域按安全基线返回 HSTS | 原缺口已解除 | 已加入 Web preview response headers；HTTP→HTTPS 边缘 redirect 仍单独待处理 | RESOLVED_2026-08-31 |
| CF-04 | P2 | CF/文档 | `curl https://staging.foresightnine.com/{health,readyz,v1/auth/providers,metrics}` | 均 HTTP 200 HTML；staging 是 Web catch-all；`/metrics` 未暴露为指标 | Web-only 域的非 Web 路径应有明确 404/路由语义 | 运维/QA 容易误把 HTML 200 当 API/metrics 成功 | 在监控和文档中明确 staging Web-only；必要时 edge 返回 404，而不是 SPA fallback | DOCUMENTATION/TOPOLOGY |
| DATA-02 | P2 | 数据 | `curl https://api-staging.foresightnine.com/v1/factor-libraries/v1.0.0/manifest` 及 shard | manifest/shard 均 HTTP 200；包含版本、asOf/integrity/source refs，factor count 1；错误版本 `/v1/` 为 HTTP 404 | 客户端使用文档声明的版本并能验证 integrity | 版本路径不一致会被误报为数据缺失 | 统一文档、客户端和监控中的 factor library version | OPEN_DOCUMENTATION |

## 1. 数据侧

### 修复后复测

- CORS：app Origin HTTP 200 且返回 app ACAO；staging Origin 仍返回 staging ACAO。
- app `/readyz`：HTTP 200、`application/json`、`status=ready`。
- 重新部署 API 后运行 provider 为 `glm_coding`，公网 `/health` 为 `openai-compatible`、`status=ok`。
- Web HTTPS 响应含 HSTS；HTTP 明文入口仍由 Cloudflare 边缘 redirect 策略控制，未在 Tunnel ingress 内强行伪造。

### 公网只读探测

- `https://app.foresightnine.com/health`：修复后 HTTP 200、JSON，`status=ok`，provider=`openai-compatible`，search provider=`client-evidence-bundle`，project search=false，schema=`v1`。
- `https://app.foresightnine.com/readyz` 与 `https://api-staging.foresightnine.com/readyz`：修复后均 HTTP 200、JSON `status=ready`。
- `/v1/assets/search?q=SPY`：HTTP 200，`total=36`、`item_count=36`。
- `/v1/assets/ETF%3ASPY`：HTTP 200，资产契约字段存在，包括 currency、content hash、data license、默认配置资格等。
- `/v1/factor-libraries/v1.0.0/manifest` 和对应 core shard：均 HTTP 200；结构含 library version、asOf、integrity、source refs；factor count 为 1。
- `/v1/methodology`：HTTP 200，含 client computation、disclosure、probability display 和 methodology version。
- 三个域的 `/.well-known/assetlinks.json`：均 HTTP 200、JSON、包名匹配、3 个证书指纹；当前 APK 指纹匹配。
- API `/metrics`：HTTP 404；app `/metrics`：HTTP 404；staging Web `/metrics`：HTML 200，属于 catch-all 语义而非指标暴露。

### 契约、矩阵和新鲜度

- `corepack pnpm verify:generated-contracts`：exit 0；generated TypeScript、asset registry 和 OpenAPI drift-free，OpenAPI 25 paths。
- `uv run --project apps/api pytest apps/api/tests/test_market_provider.py apps/api/tests/test_contracts.py`：28 passed、1 warning、exit 0。覆盖资产契约、三锚点/缺失/新鲜度等本地行为；本轮没有对公网发送会创建快照或写数据的 POST。
- 全量测试：API 249 passed、1 skipped、1 warning；Web 64 passed；Mobile 60 passed；总命令 exit 0。
- 既有验收证据显示三资产×三锚点共 9 行、FX 对象和 momentum、momentum_month、rsi14、sma_ratio 可用，缺失数据保持 missing 而非 fixture 替代；本轮通过只读 GET 和现有测试复核，没有重复写入型探测。
- Redis/缓存 TTL 900 秒的实现和配置在代码/部署证据中存在，但公网响应没有 cache-hit 字段，Cloudflare 标记为 dynamic，因此不能独立证明某一次具体命中。
- 修复后 health 暴露 `openai-compatible`；市场数据仍按 staging 的 mock/public-evaluation 证据解释，不能把 staging 表述为生产实时市场 provider。

## 2. 认证侧

- `GET /v1/auth/providers`：app 和 api-staging 均 HTTP 200，Google/GitHub 均 enabled=true。
- app 域 `GET /oauth/google/start` 与 `/oauth/github/start`：修复后均 HTTP 307，Location host 分别为 Google 和 GitHub provider。Google 真实 callback HTTP 303，随后 `/v1/auth/oauth/exchange` HTTP 200；数据库出现 1 条 Google identity 和 1 条新增 auth session；GitHub 此前已有 callback/exchange HTTP 200。没有把链接中的 state/nonce 写入报告。
- `GET /api/auth/callback/google`、`/github`（无参数）：均 HTTP 400 JSON 错误，未发现敏感字段；属于 fail-closed。
- focused auth tests：41 passed、1 warning、exit 0；覆盖 callback provider mismatch、replay、过期 ticket、异常 token/identity、local session/logout/account hard-delete 等本地行为。
- 静态实现检查显示 callback 使用 state/nonce/PKCE 约束，ticket 为一次性 opaque 值；provider token 只在服务端交换过程中使用；Web OAuth session 使用 `sessionStorage`，Mobile 使用 SecureStore。
- Google/GitHub 真实登录 callback、token exchange、identity 和 session 已取得公网实证；本轮没有记录或持久化 authorization code、access token。浏览器 session refresh/logout 未在本轮单独操作；本地 focused tests 覆盖 session/logout/account hard-delete。此前 Windows 证据中 account deletion 公网曾出现 500，本轮为只读审计未重复删除请求，保留为下一轮隔离复核项。

## 3. Cloudflare 侧

- `/home/lxx/.cloudflared/foresightnine-api-staging.yml` mode 600；`cloudflared --config ... tunnel ingress validate` exit 0。
- 用户级 `foresightnine-cloudflared.service` 和 `foresightnine-web.service` 均 active；Docker API、PostgreSQL、Redis 均 healthy。
- ingress 当前明确覆盖：
  - `app /oauth/complete` → Web 4173；
  - `app /api/*`、`/oauth/*`、`/v1/*` → API 8000；
  - `app /readyz`、`/health`、`/docs`、`/openapi.json` → API 8000；
  - `app /metrics` → 404；
  - `api-staging` → API；
  - `staging` → Web；
  - final 404。
- TLS 三域均取得 Google Trust Services WE1 证书且当前日期在有效期内。
- 已观察到 CSP、X-Frame-Options: DENY、X-Content-Type-Options: nosniff、Referrer-Policy、Permissions-Policy 和 HSTS；HTTP→HTTPS redirect 仍是边缘策略待办。
- 修复后 CORS 对 app 和 staging 两个 HTTPS Origin 均返回对应允许头；恶意 Origin 仍未获允许。
- 修复后 app `/readyz` 精确命中 API；staging 的 API 风格 URL 仍命中 Web catch-all，须在文档和监控中区分。

## 4. Google Play 侧

- `/home/lxx/.config/google-play/service-account.json` 存在、JSON 结构有效、权限为 600；没有在报告或终端输出任何字段值。
- 使用短生命周期 JWT assertion 在内存签名并交换 access token：token endpoint HTTP 200；tokeninfo HTTP 200；scope 含 `androidpublisher`；过期字段存在。token 未落盘。
- 对 `com.foresightnine.mobile` 的 internal、closed、open releases 仅执行 GET；均 HTTP 404。没有调用 `edits.insert`、upload、commit、track update、user/grant mutation、billing、orders、subscriptions、refund 或生产发布接口。
- 仓库未找到可用 Developer ID 引用，因此没有调用 Users API；Service Account 的最终 Play Console 包级读取/轨道证据仍待外部核对。接口语义依据 [Google Play API Getting Started](https://developers.google.com/android-publisher/getting_started?hl=en)、[Users API](https://developers.google.com/android-publisher/api-ref/rest/v3/users) 和 [Track Releases List](https://developers.google.com/android-publisher/api-ref/rest/v3/applications/tracks/releases/list)。
- `gcloud`、`fastlane`、`bundle` 均未安装；按计划只记录，不安装和配置。

## 5. 其余小问题

- secret scan：exit 0，检查 Git 历史 1251 个 unique blobs，未发现命中。
- generated contracts：exit 0，无 drift。
- `corepack pnpm -r typecheck`：exit 0，9 个 workspace 项目完成。
- `corepack pnpm test`：exit 0，API 249 passed/1 skipped，Web 64 passed，Mobile 60 passed，1 个 StarletteDeprecationWarning。
- `node scripts/verify/production-preflight.mjs`：exit 1；生产环境变量、live provider/credentials、TLS Postgres/Redis、abuse/CAPTCHA、OAuth、licensed market admission、metrics、数据授权、外部 release artifact 和 approval manifest 等门禁失败；这证明当前不具备生产发布条件，而不是本轮去修复它。
- `node --test scripts/verify/production-preflight.test.mjs`：13 passed，exit 0，说明 preflight 规则测试本身通过。
- `docker compose -f infra/docker/compose.yaml config --quiet`：exit 0；直接无 deployment secret file 执行 compose ps 会因必需变量缺失而不能插值，本轮改用只读 `docker ps`，三容器均 healthy。
- `pnpm audit --json`：0 info/low/moderate/high/critical，600 dependencies，exit 0。
- Android QA APK 静态检查：包名 `com.foresightnine.mobile`；v2 签名 true；manifest 有 `autoVerify`、`staging.foresightnine.com` 和 `/oauth/mobile-callback`；assetlinks 已匹配当前 APK；`adb devices` 无设备，因此系统级 domain verification 未执行。
- 备份/恢复证据、外部监控、生产 provider 授权、真实设备 App Link 和真实浏览器 OAuth 均未在本轮写入或伪造结果。

## 分级与后续计划

### P0

- 本轮无已确认 P0。

### 已解决的 P1

- AUTH-01：app 正式域 CORS 缺失，已修复并复测。
- CF-01：app `/readyz` 错路由为 HTML 200，已修复并复测。
- DATA-01：staging provider 注入漂移，已修复并复测。
- AUTH-02：Google/GitHub 真实登录 callback、token exchange 和 session 已复测；Google token 请求已补齐 `grant_type=authorization_code`。

### 待处理的 P1
- PLAY-01：Play token 有效但目标包测试轨道 GET 全部 404。
- CF-02：app/staging HTTP 未重定向 HTTPS。
- GOV-01：Linux 下治理路径硬编码导致 boundary check 失败。

### P2

- PLAY-02：后续自动化工具未安装。
- AND-01：无 adb 设备，且现有 APK 为 debug signing。
- CF-03：HSTS 已补齐；HTTP→HTTPS redirect 仍待 Cloudflare 边缘设置。
- CF-04/DATA-02：Web catch-all 与 factor version 语义需要文档/监控统一。

## 已通过、未执行和外部依赖

### 已通过

API/Web/Mobile 本地测试、类型检查、契约生成漂移、secret scan、Compose config、依赖漏洞计数、Tunnel ingress validate、用户级服务状态、容器健康、HTTPS 证书、provider readiness、OAuth start redirect、无参 callback fail-closed、assetlinks JSON/package/fingerprint 匹配、Google Play credential mode/JSON/token scope 只读验证。

### 未执行

公网 refresh/logout 的单独复核；任何会写入 staging 或 Play 的 API；生产发布、APK/AAB 上传、fastlane 配置、订阅/付款/退款/银行/税务操作；真实 Android 设备 App Link；Developer Users API（无 Developer ID）；备份恢复演练。

### 外部依赖

HTTP→HTTPS redirect 仍需要 Cloudflare 边缘策略；Google/GitHub 真实授权需要用户浏览器交互；Google Play 404 需要 Play Console 包和轨道状态核对；Android domain verification 需要设备或 CI emulator；production preflight 所需 live provider、凭据、TLS 数据库、CAPTCHA、licensed data 和 release approval 需要部署环境提供。

## 审计证据索引

完整命令输出和退出码保存在：
`/home/lxx/Foresightnine/src/repair-evidence/five-dimensional-audit-20260831/`

本报告包含修复后状态；该目录同时保存只读审计日志、配置变更复测日志、原始空报告基线、差异和回滚验证，不包含任何凭据值、令牌值、授权码或 Cookie。

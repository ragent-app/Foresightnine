# ForesightNine 五维只读审计报告（含修复后复测）

> **当前发布复核（2026-09-15）**：本节覆盖下方 2026-09-10 及 2026-08-31 的历史状态。Google Play canonical Service Account 已重新完成 token exchange；`foresightnine_pro/monthly` 与 `foresightnine_max/monthly` 均已激活为 `ACTIVE`，美国区价格保持 USD 20/月与 USD 200/月。新的 Applications Tracks API 对 internal 返回 HTTP 200，当前发布为 `0.1.0`、versionCode 1、`RELEASE_LIFECYCLE_STATE_PUBLISHED`。生产 `assetlinks.json` 已加入 Play App Signing 证书并在 dist、本机 Web、本机 API、`https://app.foresightnine.com/.well-known/assetlinks.json` 四处验证匹配。versionCode 2 / versionName 0.2.0 的 Android AAB 已构建，包含 Billing 权限、原生 Billing 模块、公开金融数据运行模式及 `https://app.foresightnine.com/oauth/complete` App Link；该 AAB 当前为 unsigned，须由 Windows 侧使用已登记 upload key 的 DPAPI 口令签名后才能上传 internal track。

## 当前发布复核（2026-09-15）

### 已解除

- `PLAY-01`：`GET .../tracks/internal/releases` HTTP 200；可见一个已发布 release，名称 `0.1.0`，active artifact versionCode 1。原“轨道权限不稳定”按当前权威读数标记为 `RESOLVED_CURRENT`。
- `BILL-01`：Pro/Max 的 `monthly` base plan 均为 `ACTIVE`；商品价格仍为 USD 20/月与 USD 200/月。商品目录阻断标记为 `RESOLVED_2026-09-15`。
- `APP-LINK-01`：公网 assetlinks HTTP 200、包名匹配、证书列表共 4 条，并包含 Play App Signing 证书。Play 安装包的 OAuth App Link 配置阻断标记为 `RESOLVED_2026-09-15`。
- `BILL-02-SERVER`：修正订阅 acknowledgement 为 Google 官方 `/subscriptions/{subscriptionId}/tokens/{token}:acknowledge` 路径；服务端现在校验 Play 返回的 `obfuscatedExternalAccountId` 必须匹配当前登录账户，并按“验证 → 持久化权益 → 必要时 acknowledge”的顺序执行。API 已重新部署并通过完整测试。
- Android upload keystore 与两份 DPAPI 口令文件的权限已由 `664` 收紧为 `600`，内容 hash 未变化。
- 新增 `apps/api/scripts/google_play_internal_release.py`：默认只验证 signed AAB，只有显式 `--publish` 才创建 Play edit；包名固定为 `com.foresightnine.mobile`、轨道固定为 `internal`，失败时丢弃未提交 edit。当前 unsigned v2 实测在任何网络写入前以 exit 2 停止。
- 新增 `scripts/release/sign-android-aab-windows.ps1`：由原 Windows 用户解密现有 DPAPI SecureString，通过 stdin 向 `jarsigner` 提供口令，保留 unsigned 输入并生成单独的 signed AAB；不把口令写入参数、文件、日志或 Git。

### 仍需闭合

| ID | 严重级别 | 当前状态 | 当前证据 | 下一步 |
|---|---|---|---|---|
| BILL-02 | P1 | `SERVER_READY_FOR_SIGNING_AND_DEVICE_TEST` | versionCode 2 AAB 构建成功；Billing permission=YES；Billing native module present；Pro/Max base plan=ACTIVE；服务端 purchase-token 绑定、正确 acknowledgement URL、幂等权益写入已通过回归并部署；Windows 签名 helper 与 internal-only Publisher 脚本已就绪 | 在原 Windows 用户上下文运行签名 helper；将 signed AAB 交给 Publisher 脚本上传 internal；将实际 Gmail 加入 License testing/internal tester；在 Play Store 安装并执行测试购买、确认、恢复、取消/过期矩阵 |
| AND-01 | P1 | `PARTIALLY_RESOLVED` | v2 AAB 包名、版本、Billing、production App Link 均通过静态检查；AAB SHA-256=`3d0a2741097d67d843dd37342164ecb68e532ad83c68aee1407577531799007e`；当前 signature=`UNSIGNED`；Linux 无 adb 设备 | Windows 签名/上传后，以 Play 生成 APK 在 Play Store 模拟器或设备验证 domain verification、OAuth 回跳、session、Billing UI 与 entitlement 同步 |
| PLAY-02 | P2 | `API_AUTOMATION_AVAILABLE_TOOLCHAIN_OPTIONAL` | Python `google-auth` 已完成商品激活、商品读取与 internal track 读取；未依赖 gcloud/fastlane | 首次 signed AAB internal 上传稳定后再固定 fastlane 版本；当前不再是 Publisher API 操作阻断 |

### 当前精确结果

```text
foresightnine_pro_MONTHLY_STATE=ACTIVE
foresightnine_max_MONTHLY_STATE=ACTIVE
INTERNAL_HTTP=200
INTERNAL_RELEASE_NAME=0.1.0
INTERNAL_RELEASE_LIFECYCLE=RELEASE_LIFECYCLE_STATE_PUBLISHED
INTERNAL_ACTIVE_VERSION_CODE=1
PUBLIC_ASSETLINKS_HTTP=200
PUBLIC_ASSETLINKS_PACKAGE_MATCH=True
PUBLIC_ASSETLINKS_PLAY_SIGNING_MATCH=True
ANDROID_V2_VERSION_CODE=2
ANDROID_V2_BILLING_PERMISSION=YES
ANDROID_V2_APP_LINK=https://app.foresightnine.com/oauth/complete
ANDROID_V2_SIGNATURE=UNSIGNED
```

本次没有输出 credential JSON、private key、DPAPI 明文、access token、OAuth code、Cookie 或购买资料；也没有上传 unsigned AAB 或变更 production track。

> **当前复核说明（2026-09-10）**：下方 2026-08-31 内容保留为历史基线；本节“当前复核”是现阶段权威状态。最新 5 次/轨道只读采样中，OLD 与 NEW 的 internal/closed/open 均为 HTTP 403 `PERMISSION_DENIED`；此前曾观察到 internal HTTP 200 以及 OLD 的 200/404/403 矩阵，记录为轨道读取间歇性或权限传播不稳定，PLAY-01 保持外部复核状态。两个订阅商品可读但仍为 `DRAFT`；Cline 本机池、计费账本、Android Billing 桥接、Web/Mobile logout 客户端和 Secret Scan 已完成实现与复测。

## 当前复核（2026-09-10）

### 当前通过项

- `cloudflared-foresightnine.service`、`cline-gateway.service`：用户/系统服务均 active；Cline 健康检查为 `ok`，6 个 key、6 个可用实例。
- `https://app.foresightnine.com/health`、`https://api-staging.foresightnine.com/health`、`https://api-staging.foresightnine.com/readyz`：均 HTTP 200。
- Cline 本机池通过通用 OpenAI-compatible 适配器接入 API；当前实测模型为 `deepseek/deepseek-v4-flash`，`glm-5.3-flash` 保留为无覆盖时的默认回退。
- 服务端额度：Free 200,000、Pro 30,000,000/月、Max 500,000,000/月；provider `total_tokens` 已进入账本，超额返回 `quota_exceeded`。
- Google Play token exchange 与 `androidpublisher` scope：PASS；`foresightnine_pro/monthly`、`foresightnine_max/monthly` 读取 HTTP 200，价格分别为 USD 20/USD 200，状态均为 `DRAFT`。
- 订阅目录读取 HTTP 200；最新 5 次/轨道双 credential 采样均为 HTTP 403、reason=`PERMISSION_DENIED`；此前曾取得 internal HTTP 200，PLAY-01 当前按轨道权限/传播不稳定处理。
- JSON logout HTTP 200，注销后 `/v1/account` HTTP 401；删除账户后 `/v1/account` HTTP 401、再次登录 HTTP 401。Web/Mobile 客户端已发送符合 API 边界的 JSON logout 请求。
- 根测试 `266 passed, 1 skipped`、Secret Scan PASS、generated contracts PASS、release-candidate PASS；Web 66、Mobile 63 测试通过。

### 当前待处理项

| ID | 严重级别 | 当前状态 | 证据/复现命令 | 下一步 |
|---|---|---|---|---|
| PLAY-01 | P1 | `PENDING_EXTERNAL_PERMISSION_RECHECK` | 最新 5 次/轨道双 credential `GET .../tracks/{internal,closed,open}/releases` 均 HTTP 403，JSON reason=`PERMISSION_DENIED`；订阅目录与商品详情 HTTP 200；历史采样曾出现 internal HTTP 200 | 同一 Service Account 对相同 read scope 的轨道结果应稳定；未配置轨道才应返回 404 | 当前 track read 未稳定，自动化 release/Billing 前置确认受影响 | 在 Play Console 核对 app-level track 权限、Active 状态和传播；传播后同一时段复测双 credential 三轨道 |
| BILL-01 | P1 | `PENDING_EXTERNAL` | `GET .../subscriptions/{foresightnine_pro,foresightnine_max}` → HTTP 200，但 monthly 均 `DRAFT` | 在 Play Console 激活两个 base plan，再执行 License Tester 购买 |
| BILL-02 | P1 | `PENDING_EXTERNAL` | Linux `adb devices` 无设备；checkout 当前只有 debug APK | 提供 release/internal-test AAB 与 Windows Play Store 模拟器，完成真实 Billing 矩阵 |
| PLAY-02 | P2 | `TOOLCHAIN_GAP` | `command -v gcloud fastlane bundle` 无结果 | 后续任务安装并固定工具版本 |
| AND-01 | P2 | `PENDING_ENVIRONMENT` | `adb devices` 无连接设备；当前 APK 为 debug signing | 用正式测试设备/模拟器和 release 签名复核 App Link |
| CF-02 | P1 | `RESOLVED_CURRENT` | HTTP host 返回 301，HTTPS health 返回 200 | 保持 Zone Always Use HTTPS 和 Tunnel supervisor |
| AUTH-03 | P1 | `RESOLVED_CURRENT` | API JSON logout HTTP 200，注销后 session HTTP 401 | 保持客户端 `Content-Type: application/json` + `{}` 请求契约 |

上述当前复核不包含任何 credential JSON、private key、OAuth code、access token、Cookie 或敏感响应正文。

- 审计时间：2026-08-31T23:28:07+08:00
- 审计对象：当前 checkout、staging 公网链路、Cloudflare Tunnel、Google Play 只读 API、Android QA 构建
- 基线 commit：`a30481162bfd783d9a3806db9f99cf4e8b2deddf`；本轮 CF 全局 HTTPS 设置已在 Cloudflare 生效，仓库文档/证据更新尚未提交
- 环境：Linux 7.0.0-28-generic x86_64；Node v22.23.2；pnpm 11.21.0；Python 3.14.4；uv 0.11.29；Docker 29.7.1；cloudflared 2026.8.2
- 工作区：历史审计之后已继续完成 Cline OpenAI-compatible provider、服务端额度/Play verify 接口、Android Billing 桥接、Web/Mobile 账户状态和客户端 logout 协议修复；本报告同时记录当前只读复核与未闭合外部门槛。
- 审计原则：真实 staging 公网只读探测；Google Play 只读；没有生产发布、付费/订阅/退款/付款资料变更、Cloudflare 付费操作、OAuth authorization code 生成或持久化。
- 脱敏：本报告不包含任何凭据值、令牌值、授权码、Cookie 或敏感响应正文。

## 结论摘要

截至当前复核，Cloudflare、Cline OpenAI-compatible provider、API 计费账本、Android Billing 桥接、Web/Mobile 账户状态和 Google Play 只读 API 均有现行证据。生产发布和真实 Billing 购买仍未判定通过。

当前主要未闭合项是：两个 Play monthly base plan 仍为 `DRAFT`；最新 5 次/轨道双 credential track read 均 HTTP 403 `PERMISSION_DENIED`，历史采样曾短暂返回 200/404，仍需 Play Console 权限与传播稳定性复核；真实购买需要 base plan 激活、License Tester、internal opt-in、正式签名 AAB 和连接中的 Play Store 模拟器；生产 preflight 仍按设计失败。

P0：当前未发现已由只读证据确认的 P0。

## 修复变更与复测

已按顺序完成并重新部署/验证：

1. `infra/docker/compose.yaml`：API CORS 现在允许 `https://app.foresightnine.com` 与 `https://staging.foresightnine.com`；LLM provider 选择改为读取 staging deployment env，默认值仍为 mock。
2. `/home/lxx/.cloudflared/foresightnine-api-staging.yml`：新增 app 精确 `/readyz`→API 8000 路由，配置验证和用户级 Tunnel 重启均成功。
3. `apps/web/vite.config.ts`：Web preview 增加 HSTS；重新 build、重启 Web preview 成功。
4. `apps/api/app/oauth_provider.py`：Google token exchange 增加 `grant_type=authorization_code`；加入请求体回归断言，重新构建并重启 API。
5. `/home/lxx/.cloudflared/foresightnine-api-staging.yml`：staging Web host 对 `/api/*`、`/v1/*`、`/health`、`/readyz`、`/metrics` 增加明确 404 ingress，保留 `/oauth/mobile-callback` Web handoff；Tunnel 重启后注册两个 HTTP/2 edge connections。
6. `scripts/verify/boundaries.mjs` 与 `scripts/verify/release-candidate.mjs`：移除 Linux 不可用的 Windows 根目录假设，按实际 Git root 解析 Windows 记录路径并保留原始 hash 基线。
7. `docs/api/README.md` 与 `apps/api/tests/test_api.py`：明确 `v1.0.0` factor route，并加入无版本路由 404 回归断言。
8. Cloudflare Zone `always_use_https`：通过 `PATCH /zones/<zone>/settings/always_use_https` 设置为 `on`；app、staging、api-staging 的 HTTP 请求均返回 301 并保留路径/query。

修复后实测：app CORS HTTP 200 且返回 app ACAO；app `/readyz` HTTP 200、`application/json`、`status=ready`；运行 provider=`glm_coding`，公网 health provider=`openai-compatible`；Web HTTPS 响应返回 HSTS；Google callback HTTP 303、OAuth exchange HTTP 200，Google identity 和 auth session 已持久化。API/Web focused tests、Compose config、Tunnel validate 均 exit 0；全量测试仍为 API 249 passed/1 skipped、Web 64 passed、Mobile 60 passed。

## Findings 表

| ID | 严重级别 | 角度 | 复现命令 | 实际结果 | 期望结果 | 影响 | 修复建议 | 状态 |
|---|---|---|---|---|---|---|---|---|
| AUTH-01 | P1 | 认证/CF | `curl -i -H 'Origin: https://app.foresightnine.com' https://api-staging.foresightnine.com/health` | 修复后 HTTP 200，返回 `Access-Control-Allow-Origin: https://app.foresightnine.com`；staging Origin 仍通过 | app 正式来源应被 API 明确允许 | 原阻断已解除 | 已在 Compose 加入 app 与 staging 两个 HTTPS origin，并重建 API | RESOLVED_2026-08-31 |
| CF-01 | P1 | CF | `curl -sS -D - https://app.foresightnine.com/readyz` | 修复后 HTTP 200，`application/json`，`status=ready` | readiness 应返回 API JSON | 原假阳性已解除 | 已在 app Tunnel ingress 增加精确 `/readyz`→API 路由并重启用户级 Tunnel | RESOLVED_2026-08-31 |
| DATA-01 | P1 | 数据 | `docker inspect docker-api-1 ...`（仅比较配置键状态）；`curl -sS https://api-staging.foresightnine.com/health` | 修复后运行值为 `glm_coding`，公网 provider 为 `openai-compatible`，status=ok | 部署声明、运行注入和 health 语义一致 | 原配置漂移已解除；实际 provider 可用性仍需业务 smoke | Compose 改为读取 deployment env 的非 secret provider 选择值；secret 仍仅在服务端环境 | RESOLVED_2026-08-31 |
| PLAY-01 | P1 | Google Play | 只读 JWT probe；`GET .../applications/com.foresightnine.mobile/tracks/{internal,closed,open}/releases` | 最新 5 次/轨道双 credential 均 HTTP 403、reason=`PERMISSION_DENIED`；订阅目录与商品详情 HTTP 200；历史采样曾出现 internal HTTP 200 和 OLD 200/404/403 | 同一 Service Account 对同一 scope 的轨道结果应稳定一致；未配置轨道才应返回 HTTP 404 | Track read 当前未稳定，自动化测试 release 读取与 Billing 前置确认受影响 | 在 Play Console 核对 app-level track 权限、Active 状态和传播；随后同一时段复测双 credential 三轨道 | PENDING_EXTERNAL_PERMISSION_RECHECK_2026-09-10 |
| AUTH-02 | P1 | 认证 | `docker logs --since 2h docker-api-1 2>&1 | grep -E 'oauth_callback_(failed|ticket_issued)|/v1/auth/oauth/exchange|/api/auth/callback'`；数据库 auth identity/session 只读计数 | Google 真实 callback HTTP 303；`POST /v1/auth/oauth/exchange` HTTP 200；Google identity 1、auth_sessions 8；GitHub 登录此前已有 callback/exchange 200；修复前 token exchange 阶段报 `OAuthProviderError`，修复为 Google `grant_type=authorization_code` | 真实授权后 callback 完成 code→token→identity→session | Google/GitHub 登录闭环已解除；公网 refresh/logout 未在本轮重复操作，本地相关测试已覆盖 | 保留 callback/exchange/session 日志的脱敏观测；下一轮单独复核 refresh/logout，不记录 code/token | RESOLVED_LOGIN_2026-08-31 |
| CF-02 | P1 | CF | `PATCH /zones/<zone>/settings/always_use_https`；`curl -sS -D - -o /dev/null http://{app,staging,api-staging}.foresightnine.com/` | Zone API HTTP 200，`success=true`、`value=on`；三个 HTTP host 均 HTTP 301，Location 指向同 host 的 HTTPS URL；无 DNS/计费变更 | HTTP 请求应 301/308 到对应 HTTPS | 明文入口已统一收敛 | 保持 Zone Always Use HTTPS=on；HTTPS 终端和 Tunnel ingress 继续单独复核 | RESOLVED_2026-08-31 |
| GOV-01 | P1 | 其余 | `corepack pnpm verify:boundaries`；`node --test scripts/verify/boundaries.test.mjs` | 两者均 exit 0；三份 Windows 记录路径解析到 Linux checkout，hash 和 size 全部匹配 | 治理检查应使用可移植路径并在目标 checkout 正常运行 | Linux CI/审计已恢复可判定性 | 保留平台无关路径解析和实际 Git root 检查；不修改基线 | RESOLVED_2026-08-31 |
| PLAY-02 | P2 | Google Play | `command -v gcloud fastlane bundle` | 三者均未安装 | 后续 fastlane/Publisher 自动化应有明确、可复现工具链 | 后续自动化尚未具备运行前置 | 将安装固定到后续配置任务；本轮不安装、不配置、不发布 | TOOLCHAIN_GAP |
| AND-01 | P2 | 其余/Android | `apksigner verify --verbose --print-certs app-debug.apk`；`adb devices` | APK 包名 `com.foresightnine.mobile`；v2 签名 true，签名 DN 为 Android Debug；assetlinks 三域均 200、包名匹配、3 fingerprints，当前 APK fingerprint 匹配；adb 无设备 | release/QA signing 边界及真实设备 domain verification 应分别验证 | 无设备时无法取得 Android 系统级 App Link 解析实证；当前文件是 debug APK | 接入测试设备并安装目标 release/QA APK 后执行 domain verification；正式发布只使用 release 签名 | PENDING_ENVIRONMENT |
| CF-03 | P2 | CF | 选定安全响应头 probe | 修复后包含 `Strict-Transport-Security: max-age=31536000; includeSubDomains`，HTTP 入口另由 Zone Always Use HTTPS 返回 301 | HTTPS 正式域按安全基线返回 HSTS 并统一跳转 | 原缺口已解除 | 保持 HSTS 与 Zone Always Use HTTPS 配置 | RESOLVED_2026-08-31 |
| CF-04 | P2 | CF/文档 | `curl https://staging.foresightnine.com/{health,readyz,v1/auth/providers,metrics}` | Tunnel 重启后四个路径均 HTTP 404；`/oauth/mobile-callback` 仍 HTTP 200 HTML；staging 明确为 Web-only | Web-only 域的非 Web 路径应有明确 404/路由语义 | 运维/QA 不再把 HTML 200 当 API/metrics 成功 | 保留 staging Web-only ingress 负路由，并同步监控/文档 | RESOLVED_2026-08-31 |
| DATA-02 | P2 | 数据 | `curl https://api-staging.foresightnine.com/v1/factor-libraries/v1.0.0/manifest`；`curl https://api-staging.foresightnine.com/v1/factor-libraries/manifest` | canonical manifest HTTP 200、`library_version=v1.0.0`；无版本路由 HTTP 404；API 测试新增 404 断言 | 客户端使用文档声明的版本并能验证 integrity | 版本路径不一致不会再被误报为数据缺失 | 文档、客户端和监控统一使用显式 `v1.0.0` 路径 | RESOLVED_2026-08-31 |

## 1. 数据侧

### 修复后复测

- CORS：app Origin HTTP 200 且返回 app ACAO；staging Origin 仍返回 staging ACAO。
- app `/readyz`：HTTP 200、`application/json`、`status=ready`。
- 重新部署 API 后运行 provider 为 `glm_coding`，公网 `/health` 为 `openai-compatible`、`status=ok`。
- Web HTTPS 响应含 HSTS；HTTP 明文入口由 Cloudflare Zone Always Use HTTPS 控制并返回 301，不在 Tunnel ingress 内伪造。

### 公网只读探测

- `https://app.foresightnine.com/health`：修复后 HTTP 200、JSON，`status=ok`，provider=`openai-compatible`，search provider=`client-evidence-bundle`，project search=false，schema=`v1`。
- `https://app.foresightnine.com/readyz` 与 `https://api-staging.foresightnine.com/readyz`：修复后均 HTTP 200、JSON `status=ready`。
- `/v1/assets/search?q=SPY`：HTTP 200，`total=36`、`item_count=36`。
- `/v1/assets/ETF%3ASPY`：HTTP 200，资产契约字段存在，包括 currency、content hash、data license、默认配置资格等。
- `/v1/factor-libraries/v1.0.0/manifest` 和对应 core shard：均 HTTP 200；结构含 library version、asOf、integrity、source refs；factor count 为 1。
- `/v1/methodology`：HTTP 200，含 client computation、disclosure、probability display 和 methodology version。
- 三个域的 `/.well-known/assetlinks.json`：均 HTTP 200、JSON、包名匹配、3 个证书指纹；当前 APK 指纹匹配。
- API `/metrics`：HTTP 404；app `/metrics`：HTTP 404；staging Web `/metrics`：本轮 Tunnel 负路由后 HTTP 404，指标仍未暴露。

### 契约、矩阵和新鲜度

- `corepack pnpm verify:generated-contracts`：exit 0；generated TypeScript、asset registry 和 OpenAPI drift-free，OpenAPI 25 paths。
- `corepack pnpm verify:boundaries`：本轮修复后 exit 0；`node --test scripts/verify/boundaries.test.mjs` 为 2 passed。
- `corepack pnpm verify:reference-hashes`：exit 1；其 manifest 仍引用当前 Linux checkout 不可访问的外部 Windows `D:\FM acceleration-deepseek` 文件，本轮未修改外部研究资料基线。
- `uv run --project apps/api pytest apps/api/tests/test_market_provider.py apps/api/tests/test_contracts.py`：28 passed、1 warning、exit 0。覆盖资产契约、三锚点/缺失/新鲜度等本地行为；本轮没有对公网发送会创建快照或写数据的 POST。
- 全量测试：API 249 passed、1 skipped、1 warning；Web 64 passed；Mobile 60 passed；总命令 exit 0。
- 既有验收证据显示三资产×三锚点共 9 行、FX 对象和 momentum、momentum_month、rsi14、sma_ratio 可用，缺失数据保持 missing 而非 fixture 替代；本轮通过只读 GET 和现有测试复核，没有重复写入型探测。
- Redis/缓存 TTL 900 秒的实现和配置在代码/部署证据中存在，但公网响应没有 cache-hit 字段，Cloudflare 标记为 dynamic，因此不能独立证明某一次具体命中。
- 修复后 health 暴露 `openai-compatible`；市场数据仍按 staging 的 mock/public-evaluation 证据解释，不能把 staging 表述为生产实时市场 provider。
- factor library canonical route `v1.0.0` HTTP 200；无版本 `/v1/factor-libraries/manifest` HTTP 404，API 回归断言已补齐。

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
- 已观察到 CSP、X-Frame-Options: DENY、X-Content-Type-Options: nosniff、Referrer-Policy、Permissions-Policy 和 HSTS；Zone Always Use HTTPS 已开启，三个受管 host 的 HTTP 请求均返回 301。
- 修复后 CORS 对 app 和 staging 两个 HTTPS Origin 均返回对应允许头；恶意 Origin 仍未获允许。
- 修复后 app `/readyz` 精确命中 API；staging 的 API 风格 URL 已由 Tunnel 负路由返回 404，不再命中 Web catch-all。
- Tunnel 重启后 `cloudflared tunnel info` 显示两个活动 HTTP/2 edge connections；app/staging/api-staging HTTPS 公网请求恢复正常。三个 HTTP root 均返回 301，Location 保留原 host/path/query。

## 4. Google Play 侧

- 两份本地 Play credential 已独立只读验证：OLD 与 NEW token exchange 均 PASS，订阅目录及 Pro/Max 商品详情均 HTTP 200；最新 5 次/轨道两者 Internal/closed/open 均 HTTP 403 `PERMISSION_DENIED`，历史采样曾出现 internal HTTP 200。比较结果为 `SAME_SERVICE_ACCOUNT_DIFFERENT_KEY`，轨道问题与 key 选择无关，继续核对 Play Console 授权传播或服务响应稳定性。

- `/home/lxx/.config/google-play/service-account.json` 存在、JSON 结构有效、权限为 600；没有在报告或终端输出任何字段值。
- 使用短生命周期 JWT assertion 在内存签名并交换 access token：token endpoint HTTP 200；tokeninfo HTTP 200；scope 含 `androidpublisher`；过期字段存在。token 未落盘。
- 对 `com.foresightnine.mobile` 的 internal、closed、open releases 仅执行 GET：最新 5 次/轨道双 credential 均 HTTP 403、reason=`PERMISSION_DENIED`；订阅目录和两个商品详情 HTTP 200。没有调用 `edits.insert`、upload、commit、track update、user/grant mutation、billing、orders、subscriptions、refund 或生产发布接口。
- 仓库未找到可用 Developer ID 引用，因此没有调用 Users API；Service Account 的最终 Play Console 包级读取/轨道证据仍待外部核对。接口语义依据 [Google Play API Getting Started](https://developers.google.com/android-publisher/getting_started?hl=en)、[Users API](https://developers.google.com/android-publisher/api-ref/rest/v3/users) 和 [Track Releases List](https://developers.google.com/android-publisher/api-ref/rest/v3/applications/tracks/releases/list)。
- `gcloud`、`fastlane`、`bundle` 均未安装；按计划只记录，不安装和配置。

## 5. 其余小问题

- secret scan：exit 0，检查 Git 历史 1251 个 unique blobs，未发现命中。
- generated contracts：exit 0，无 drift。
- `corepack pnpm -r typecheck`：exit 0，9 个 workspace 项目完成。
- `corepack pnpm test`：exit 0，API 249 passed/1 skipped，Web 64 passed，Mobile 60 passed，1 个 StarletteDeprecationWarning。
- `node scripts/verify/production-preflight.mjs`：exit 1；生产环境变量、live provider/credentials、TLS Postgres/Redis、abuse/CAPTCHA、OAuth、licensed market admission、metrics、数据授权、外部 release artifact 和 approval manifest 等门禁失败；这证明当前不具备生产发布条件，而不是本轮去修复它。
- `node --test scripts/verify/production-preflight.test.mjs`：13 passed，exit 0，说明 preflight 规则测试本身通过。
- `corepack pnpm verify:release-candidate`：exit 0；实际 Git root 检查和 Linux 路径兼容性已通过。
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

### 待处理的 P1（历史段；当前状态以“当前复核”节为准）
- PLAY-01：最新 5 次/轨道双 credential 均 HTTP 403 `PERMISSION_DENIED`；此前采样曾出现 HTTP 200/404，状态保持 `PENDING_EXTERNAL_PERMISSION_RECHECK`。
- BILL-01：两个 monthly base plan 当前仍为 `DRAFT`，等待 Play Console 激活后进行测试购买。
- BILL-02：真实 Billing 购买仍等待 release/internal-test AAB、License Tester 和连接中的 Play Store 模拟器。

### P2

- PLAY-02：后续自动化工具未安装，状态保持 `TOOLCHAIN_GAP`。
- AND-01：无 adb 设备，且现有 APK 为 debug signing，状态保持 `PENDING_ENVIRONMENT`。
- CF-03：HSTS 与 Cloudflare HTTP→HTTPS redirect 均已生效。
- CF-04/DATA-02：Web catch-all 与 factor version 语义已统一并复测通过。

## 已通过、未执行和外部依赖

### 已通过

API/Web/Mobile 本地测试、类型检查、契约生成漂移、secret scan、Compose config、依赖漏洞计数、Tunnel ingress validate、用户级服务状态、容器健康、HTTPS 证书、provider readiness、OAuth start redirect、无参 callback fail-closed、assetlinks JSON/package/fingerprint 匹配、Google Play credential mode/JSON/token scope、订阅目录和双 credential track read 只读验证；最新 track 采样均为 403，历史不一致结果保留为观察项。

### 未执行

公网 OAuth refresh 的单独复核；任何会写入 staging 或 Play 的 API；生产发布、APK/AAB 上传、fastlane 配置、订阅/付款/退款/银行/税务操作；真实 Android 设备 App Link；Developer Users API（无 Developer ID）；备份恢复演练。公网 JSON logout/delete 已在当前复核中验证。

### 外部依赖

Google/GitHub 真实授权需要用户浏览器交互；Google Play 404 需要 Play Console 包和轨道状态核对；Android domain verification 需要设备或 CI emulator；production preflight 所需 live provider、凭据、TLS 数据库、CAPTCHA、licensed data 和 release approval 需要部署环境提供。

## 审计证据索引

完整命令输出和退出码保存在：
`/home/lxx/Foresightnine/src/repair-evidence/five-dimensional-audit-20260831/`

本轮 CF/GOV/DATA 修复证据保存在：
`/home/lxx/Foresightnine/src/repair-evidence/repair-cf-gov-data-20260831/`

本轮 CF 全局 HTTPS 证据保存在：
`/home/lxx/Foresightnine/src/repair-evidence/repair-cf-https-20260831/`

本报告包含修复后状态；该目录同时保存只读审计日志、配置变更复测日志、原始空报告基线、差异和回滚验证，不包含任何凭据值、令牌值、授权码或 Cookie。

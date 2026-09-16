
---

# 2026-09-06 双平台流水线复核与下一步实施计划

## A. 本次复核结论

### A.1 流水线文档当前状态

`src/docs/deployment/twopaltformpipeline.md` 仍然是“规划与证据同步”文档，且明确不授权部署。现有证据可按以下状态理解：

| 模块 | 当前状态 | 证据/结论 |
|---|---|---|
| 0 冻结与基线 | 部分完成 | 计划基线存在，但 OAuth、Cloudflare、Play、Android 外部验收仍未全部闭环 |
| 1–6 Linux、PostgreSQL、Redis、API、Cloudflare API、认证契约 | 已有证据 | `linux-module-evidence.md` 有对应 checkpoint；仍需重新验收在线 Tunnel |
| 7–8 Web/Android 客户端接口与认证基础 | 基础完成 | 本地类型检查与测试通过 |
| 9–13 浏览器、真实 Provider、OAuth、客户端链接、FM parity | 部分完成 | 真实浏览器/Android 和公开 Tunnel 证据不完整 |
| 14 生产加固 | 未完成 | 不能进入生产发布门禁 |
| 15 签名非 Debug AAB | 阻塞 | 当前 Linux 可见的是 Debug APK，正式 release 签名/AAB 还需外部构建证据 |
| 16 Play internal testing | 部分可验证 | Play API 已能读 internal release；还没有 Billing 商品与购买链路 |
| 17 Pages Web/production API | 阻塞 | 当前 `cloudflared` 无 active connection，公网历史探测为 530 |
| 18–20 生产验证、提交、运维交接 | 未开始 | 依赖上述 P0/P1 完成 |

当前代码基线有 `249 passed, 1 skipped`，typecheck 通过；没有现成订阅、权益、按 Token 计费账本或 Android Billing 实现。

### A.2 Google Play credential 与 API 权限实测

核查对象：`/home/lxx/.config/google-play/foresightnine-play-publisher.json`。

实测结果（2026-09-06）：

- 文件存在，权限 `600`；JSON 结构有效；未输出任何 credential 字段。
- Service Account token exchange：`PASS`，HTTP `200`。
- 申请 scope：`https://www.googleapis.com/auth/androidpublisher`，scope 已确认。
- `GET .../applications/com.foresightnine.mobile/tracks/internal/releases`：HTTP `200`。
- 返回 1 个 internal release，生命周期为 `RELEASE_LIFECYCLE_STATE_PUBLISHED`，有 1 个 active artifact，versionCode 为 `1`。
- `GET .../applications/com.foresightnine.mobile/subscriptions`：HTTP `204`，表示当前没有可列出的订阅商品；这不是 credential 认证失败。

因此，结论从“Play 权限未开通”更正为：**当前 credential 的 Android Publisher 认证、应用读取和 internal track 读取已经开通；当前还没有已创建/可列出的订阅商品。** 之前针对通用应用路径 404 的判断不作为依据；本次使用了有效的 track release API。

### A.3 仍然存在的外部阻塞

- Cloudflare Tunnel 进程存在但没有 active connection，且没有发现 user/systemd supervisor；app、api-staging、staging 的公网链路仍需恢复后再做浏览器 OAuth 验收。
- Play 内部测试版本已通过 API 读到 versionCode 1，但 Android 客户端安装、License Tester、商品查询、测试购买、服务端验签尚未执行。
- 官方当前模型文档未确认 `glm-5.3-flash` 可用；必须先做能力探测，不能把不可用模型直接作为默认生产模型。

## B. 目标商业与额度定义

### B.1 账户与权益

1. 权益主键使用 ForesightNine `account_id`，Google/GitHub 只是登录身份；同一账户绑定多个 Provider 时额度不重复发放。
2. 首次完成 Google 或 GitHub 登录时，按账户原子、幂等地发放一次 `200,000` Token 初始额度；身份合并时保留已发放记录，不重复领取。
3. Pro：`30,000,000` Token/月，价格目标 USD `20`/月。
4. Max：`500,000,000` Token/月，价格目标 USD `200`/月。
5. 月额度按账户和计费周期记录 `period_start/period_end`，不依赖客户端时间；周期切换由服务端完成。
6. 计费单位为 provider 返回的 `prompt_tokens + completion_tokens`；若 provider 没有可信 usage，调用不计入成功消费并进入可审计失败状态。
7. 现有 IP/session abuse quota 保留，账户月额度是第二层业务配额，不能用登录切换绕过。
8. 默认模型目标为 `glm-5.3-flash`；先执行 capability probe。若目标模型在当前商业 API 账户不可用，则停止默认切换并选择已验证的可用模型，不做静默 fallback。

### B.2 Google Play 商品约定

以下为实施默认值，真正写入 Play Console 前需要用户确认，因为 product ID/base plan ID 一旦对外使用不应随意改名：

- Product ID：`foresightnine_pro`；base plan：`monthly`；目标价 USD 20/月。
- Product ID：`foresightnine_max`；base plan：`monthly`；目标价 USD 200/月。
- Google Play 会按地区显示本地化价格；美元价格作为基准，不在客户端硬编码最终收费金额。
- 本轮不做 production 发布，不创建真实收费交易；只在 internal testing + License Tester 环境验证。

## C. 按顺序的实施计划

### P0-1：恢复公开入口与验收基线

1. 保留现有 ingress 规则：`/oauth/complete` 到 Web，`/api/auth/callback/*` 与 `/v1/*` 到 API，`/metrics` 返回 404。
2. 用现有 `/home/lxx/.cloudflared/key.txt` 和配置恢复 `cloudflared` 长驻 supervisor，启动后检查 `tunnel info` 必须显示 active connection。
3. 验证三域名 HTTPS、redirect、CSP、CORS、HSTS 和 API base；确认 Web 不再请求错误的 `app.foresightnine.com/v1/*`。
4. 成功标准：app/api-staging/staging 不再 530；health/readyz、OAuth start、callback 缺参 fail-closed 均有状态码证据。

### P0-2：固化 Play API 访问与商品前置检查

1. 继续使用当前 canonical credential path，不复制 JSON、不把 key 写入 Git。
2. 先只读核验 internal release、track、package 与当前权限；本次已达到 token exchange PASS、internal release HTTP 200。
3. 在 Play Console 对 Service Account 保持 app-level 的 `View app information`、`Release apps to testing tracks`、`Manage testing tracks and edit tester lists`、`Manage store presence`。
4. 为后续 Billing API 增加并确认：`View financial data, orders and cancellation survey responses` 与 `Manage orders and subscriptions`。Google 官方将这两项列为 Billing API 所需权限；权限变更传播可能需要等待。
5. 商品创建/更新前先输出 dry-run catalog diff；只有用户明确确认 product ID、base plan、价格和地区策略后，才进行 Play Console/API 写入。

### P0-3：后端额度、权益与审计账本

新增 migration（建议 `003_billing.sql`）和服务层：

- `billing_entitlements`：account_id、plan、status、source、period、granted_at、expires_at、version。
- `billing_purchases`：provider、package_name、product_id、base_plan_id、purchase_token_hash、purchase_state、acknowledged、raw_payload_encrypted、last_verified_at；原始 token 不进入日志。
- `usage_periods`：account_id、plan、limit_tokens、used_tokens、reserved_tokens、period reset。
- `usage_ledger`：request_id、account_id、model、input_tokens、output_tokens、total_tokens、state、created_at、settled_at；request_id 唯一。
- `login_grants`：account_id、grant_type、amount、唯一约束，保证首登额度只发一次。

调用流程：

1. 服务端从 session 得到 account_id，从数据库得到 entitlement；客户端不能声明 plan、quota 或消费量。
2. 估算 Token 后用 Redis 原子操作或 PostgreSQL 事务预留额度。
3. 调用 GLM；读取 provider usage；以实际 usage 结算预留差额。
4. 超额、超时、重试、重复 request_id、provider 缺 usage 都有确定状态；重试不能重复扣费。
5. 并发测试覆盖剩余额度边界，保证 Pro 不能超过 30,000,000，Max 不能超过 500,000,000。
6. `GET /v1/me`（或现有等价接口）返回安全字段：plan、entitlement status、used、remaining、period_end、model；不返回 provider key、purchase token 或内部凭据。

### P0-4：GLM provider 适配

1. 将模型名、endpoint、价格/计量策略配置化；默认候选写为 `glm-5.3-flash`。
2. 先对当前 GLM API 做 capability probe：模型可用、认证有效、结构化输出有效、usage 字段存在且可解析。
3. 扩展 provider result，使 usage 与 request id 进入服务端计费层；不能再只返回 ChallengeDraft 而丢弃 usage。
4. 使用可授权 SaaS/API 的商业方案。Z.AI 条款对 Coding Plan 的用途有专门限制，不能默认把个人 Coding Plan 配额代理给付费用户；需要 API 商业授权或适用的企业方案。
5. 写入 golden tests：正常 usage、usage 缺失、超时、429、结构化响应异常、重试幂等。

### P1-1：Google Play Billing 实现

1. Android 加入 Google Play Billing Library 的稳定版本，并封装 RN/Expo 可调用的原生模块；当前项目尚无 Billing 依赖。
2. App 启动/登录后查询 `foresightnine_pro` 与 `foresightnine_max` 的 ProductDetails，价格仅用于展示。
3. 购买结果只把 purchase token 通过 TLS 发到后端；后端调用 `purchases.subscriptionsv2.get` 验证 package、product/base plan、状态、到期时间与账户绑定。
4. `PENDING` 不授予权益；验证成功后幂等落库、授予权益、执行 acknowledge；重复 token 不重复发额度。
5. 登录、重装、换设备和“恢复购买”都按 account_id 重新同步；取消、到期、grace、on-hold、暂停、升级/降级均映射到明确状态。
6. 默认策略：Pro→Max 立即升级；Max→Pro 在下一个续期周期生效，并在 UI 显示当前与待生效计划。

### P1-2：模拟 Billing 验收

使用 internal test release + License Tester，不使用真实付费账户：

- internal tester opt-in 后安装 versionCode 1 或后续 release。
- 商品查询：Pro/Max 两个商品均可见，价格和 base plan 正确。
- 测试购买：Google Play 测试支付完成，后端验签成功，权益变为 active，额度正确。
- 重复回调/重复恢复：不重复记账、不重复授予额度。
- pending/取消/到期/恢复：状态与 UI、API 一致。
- Web 登录同一 ForesightNine account 后显示同一 plan/remaining；刷新 session 仍有效，logout 后只失效当前 session。
- 测试结束后保存 HTTP 状态、Purchase API 结果摘要、数据库状态摘要和截图，不保存 token/Cookie。

Google 官方说明了 License Tester 与 Billing 测试流程；Billing 实现还应遵循客户端购买、服务端验签与安全处理文档。

### P1-3：Web 与运营面板

1. 研究控制台增加 plan、周期、已用/剩余 Token、模型和额度重置日。
2. 增加管理员只读审计页：账户、权益状态、最近验证时间、用量汇总、失败原因；默认不展示 purchase token 和凭据。
3. 账号删除时撤销 session、解绑 identity、停止权益继续使用，并按保留策略保留最小财务审计记录。
4. 所有额度变更写审计事件，后台授予测试权益必须有明确测试标记，不能伪装成 Play 购买。

### P1-4：测试与发布门禁

新增并执行：

- 后端：迁移、额度边界、并发预留/结算、首登 grant 幂等、identity merge、权限拒绝、购买验签、恢复、状态同步、删除账号。
- Web：未登录/Free/Pro/Max/过期/超额/刷新/logout 状态。
- Android：商品查询、购买、pending、restore、断网重试、重复回调、release signing。
- 合约：`/v1/me`、verify、restore、usage 字段和安全响应。
- 回归：现有 `249 passed, 1 skipped` 不得回退；typecheck 必须通过。

### P2：正式发布前

只有 P0/P1 全部通过后才进入：正式 AAB、production preflight、正式商店商品与价格复核、隐私/条款/退款说明、备份恢复演练、监控告警、RTDN。RTDN 作为轮询/restore 稳定后的增强项，先不引入额外付费服务；生产发布和真实收费交易不属于本阶段执行内容。

## D. 当前需要用户支持的事项

1. 在 Play Console 确认当前 Service Account 用户状态为 `Active`，并确认 app `com.foresightnine.mobile` 已勾选；本次 API 已证明读 internal release 成功，因此这一步重点是保存权限截图或确认后续写权限。
2. 为 Billing 增加并保存两项权限：`View financial data, orders and cancellation survey responses`、`Manage orders and subscriptions`。权限变更后等待传播，再通知我复测。
3. 把测试 Gmail 加入 License testers，并完成 internal testing opt-in；提供 Windows 侧可安装的 internal release 测试结果。Linux 当前没有可用 Android 设备证据。
4. 确认商品 ID/base plan：`foresightnine_pro/monthly`、`foresightnine_max/monthly`；确认 USD 20/USD 200 是否作为基准地区价格。
5. 提供已授权的 GLM API 商业方案/测试额度，并确认目标模型 `glm-5.3-flash` 对该账户可用；API key 只放服务端环境，不写入本计划。

以下事项可由本地执行者完成：数据库迁移、后端账本/验签、Android Billing bridge、Web 状态展示、测试与审计、Cloudflare supervisor 恢复及只读验证。Play Console 邀请确认、License Tester 操作、Android Windows 设备验收和 GLM 商业账户选择需要用户侧完成。

## E. Play Console 开通路径

1. 打开 [Google Play Console](https://play.google.com/console)，进入 **Users and permissions**。
2. 找到 Service Account 用户并打开用户详情；若状态为 Invitation sent，使用与邀请完全相同的 Service Account 邮箱接受邀请，直到状态为 Active。
3. 在 **App permissions** 中选择 `com.foresightnine.mobile`，保留应用信息只读、测试轨道发布、测试轨道/测试者管理、商店管理权限。
4. 在同一用户的权限列表中增加 `View financial data, orders and cancellation survey responses` 与 `Manage orders and subscriptions`，保存。
5. 进入对应应用的 **Monetize > Products > Subscriptions**，创建两个订阅商品及 monthly base plan；先核对 ID、地区和价格，再保存/激活。商品目录写入属于后续受控变更，不在本次审查中自动执行。
6. 进入 **Testing > Internal testing**，确认 versionCode 1 已发布到 internal track；在 **Testers** 添加测试 Gmail，完成 opt-in 链接。
7. Billing 测试只使用 License Tester。购买完成后由客户端把 token 交给服务端验证；不要把 token 粘贴到聊天或提交到 Git。
8. 权限确认后通知我复测：token exchange、internal release、subscriptions、purchase verification 和 entitlement sync。Google 官方说明 Service Account 要在 Cloud Project 启用 API 后，再在 Play Console 的 Users & Permissions 授权；Billing API 需要上述两项 Billing 权限。

## F. 验收出口

本计划可进入下一阶段的条件：

- Tunnel active，三个域名 HTTPS 公网检查通过；
- Play token exchange PASS，internal release read PASS，订阅目录可读；
- Pro/Max 商品查询通过，测试购买验签通过，权益/额度/恢复/取消全链路通过；
- Pro 30M 与 Max 500M 的并发边界测试通过，首次登录 200K 只发一次；
- `glm-5.3-flash` capability probe 与 usage 计量通过，或已记录并批准替代模型；
- Web 与 Android 使用同一 account_id 读取一致权益；
- 现有回归测试保持通过，新增 billing 测试全部通过；
- 没有生产发布、真实收费、退款、付款资料或金融信息变更。

## G. 参考依据

- [Google Play Developer API Getting Started](https://developers.google.com/android-publisher/getting_started?authuser=2)
- [Play Console 用户与权限](https://support.google.com/googleplay/android-developer/answer/9844686?hl=en-GB)
- [Google Play Billing 测试](https://developer.android.com/google/play/billing/test)
- [Google Play Billing 集成](https://developer.android.com/google/play/billing/integrate)
- [Google Play Billing 后端与安全](https://developer.android.com/google/play/billing/backend)
- [Z.AI 模型总览](https://docs.z.ai/guides/overview/overview)
- [Z.AI 订阅条款](https://docs.z.ai/legal-agreement/subscription-terms)

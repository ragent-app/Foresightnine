# Progress

## Web-first v1 — 2026-09-13

- 正式网页以 `https://app.foresightnine.com` 为唯一应用入口；根域与 `www` 通过 Tunnel 进入 API 后以 308 保留路径和查询参数跳转到应用域名。
- 生产网页改用 `app.foresightnine.com` 同源 API；`/snapshots` 已加入 Tunnel API ingress，旧 `staging.foresightnine.com` 不再作为正式网页运行地址。
- 研究控制台新增账户级金融数据源设置，可配置 Market Data、Alpha Vantage、Twelve Data、Finnhub，并对 Yahoo fallback 统一启停、排序和连通性测试。
- 用户 Key 使用独立 AES-GCM 密钥按账户和 provider 绑定加密；响应只返回末四位提示，浏览器存储、导出、日志及 Git 均不包含密钥原文。账户删除同步清理配置。
- 原有 provider admission、三时点、Redis 缓存、future/stale/missing 校验和价格派生因子链路保持不变；个人 Key 只覆盖对应 adapter，不接受任意 URL。
- SEO 第一版补充 canonical、description、Open Graph、robots 和 sitemap；OAuth 与 API 路径禁止索引。

### Verification

- API 全量测试、Web 69 项测试、TypeScript typecheck 和 Vite production build 通过。
- 公网 `app` 首页、robots、sitemap、同源 `/snapshots` 与配置鉴权通过；临时账户完成配置保存、掩码读取和删除闭环。

## Current implementation correction status — 2026-08-31

- The current repository HEAD is `a990ee7` and is aligned with `origin/master`.
  The Android release artifacts were revalidated from the merged HEAD after
  the build-time production App Link checks were merged; they remain QA-only
  because signing, App Links and physical-device gates are still open.
- OAuth state and one-time exchange tickets now use the existing PostgreSQL
  document repository when persistent storage is configured. They retain the
  bounded-TTL and generic fail-closed behavior when persistence is unavailable;
  development/test memory behavior is unchanged.
- Production Android configuration now throws at build time unless an exact
  HTTPS callback, matching App Link host/package, verified marker and release
  certificate fingerprint are supplied. The custom scheme is a local/QA path
  only.
- No market, factor or FX source is approved for public production in the
  provider manifest. Stooq, Frankfurter, Kenneth French and AQR remain
  staging/reference candidates until written terms, coverage and feature/FX
  gates are evidenced.

# Prior Android emulator QA evidence — 2026-08-31

- Reinstalled the disposable QA-signed Android build whose mobile sources are
  identical to prior source snapshot `2620bdd`, not current HEAD `f1c70ef`, and cold-started `com.foresightnine.mobile`
  on `emulator-5554` (API 36 Play), switched the language to English, and
  reached the server-backed Challenge state against staging.
- UI hierarchy dumps and screenshots are retained in
  `docs/qa/android/phase9-current-head-{home,en,challenge}.{xml,png}`. The
  interaction bounds were read from the dumped UI tree; they were not guessed.
- Post-flow logcat had no `FATAL EXCEPTION`, `ReactNativeJS:E`, or
  `AndroidRuntime:E` entries. This is emulator evidence only; a physical-device
  run remains an external gate.

# Prior Android release rebuild — 2026-08-31

- Rebuilt the prior `8d7dc99` source in the short-path QA clone with the
  existing Expo/RN workspace, CMake 3.30.5, x86_64 ABI, and the disposable
  signing bypass used only for local QA. JDK 25 required the process-local
  `--enable-native-access=ALL-UNNAMED` flag; no repository build configuration
  was weakened.
- The non-debug unsigned APK and AAB were produced successfully. Their hashes
  and reproducible command are recorded in
  `docs/release/android-qa-release-2026-08-31.md`. This remains an emulator
  artifact, not a Play-uploadable signed release.
- The physical-device, production-keystore, App Link, Play App Signing and
  tablet-emulator gates remain open.

# Public provider second official review — 2026-08-31

- A second official-documentation review checked Twelve Data and Finnhub in
  addition to the already probed Yahoo, Stooq, Market Data, Alpha Vantage and
  Nasdaq sources. Twelve Data has useful ETF coverage but requires an API key
  and its redistribution guidance requires a separate agreement for providing
  data to third parties. Finnhub requires a token and documents ETF and candle
  capabilities as Premium; its response shape still does not supply the
  approved FM factor/FX contract.
- No source currently satisfies public redistribution rights, target coverage,
  browser/mobile access, exact FM feature provenance and stable session
  semantics together. The report and Linux handoff now identify Twelve Data as
  a licensing candidate based on documented ETF coverage and Finnhub as a
  secondary candidate,
  without enabling either source.
- The safe implementation boundary remains: licensed or explicit BYOK
  server-side adapter -> allowlisted symbols -> three bounded anchor rows ->
  `market-snapshot-bundle/v1` -> client validation -> client-only FM. Price-only
  or three-anchor data must remain `limited_data`; it cannot be relabeled as
  momentum/quality and cannot recreate IC/ICIR.
- Added an explicit FX snapshot boundary to the market bundle and both client
  portfolio gates. Public Default now requires independently validated,
  current FX rates for CNY/USD/EUR; fixture FX remains available only for
  transparent Raw/offline calculations. Missing or stale FX keeps public
  weights, amounts, units and execution permission at zero while retaining
  Raw results.

## Android OAuth integration and public-provider recheck — 2026-08-31

### Current staging check

- `GET https://api-staging.foresightnine.com/health` returned `200` with the
  deterministic mock provider and `client-evidence-bundle` search mode.
- `GET https://api-staging.foresightnine.com/v1/auth/providers` returned
  Google and GitHub with `enabled: false`. The clients therefore correctly
  keep provider login disabled until protected staging credentials and the
  allowlisted callback are configured; no credential was guessed or embedded.

### Delivered

- Added the mobile OAuth client boundary and five focused tests, then added
  `OAuthSessionProvider` to the Expo root layout. Android now listens for the
  initial URL and runtime deep-link events, exchanges only the server-issued
  one-time ticket, and stores the validated local session through SecureStore.
- Login buttons are gated by server provider readiness, safe API base, and an
  explicit callback URI. Logout and account deletion clear in-memory state
  before the network call and attempt SecureStore cleanup in `finally`; account
  deletion requires a native confirmation dialog. Phone/tablet layouts retain
  the existing `<600px` stacked control behavior.
- Added build-time HTTPS App Link intent-filter generation in
  `apps/mobile/app.config.cjs` and documented the required domain
  `assetlinks.json`, release certificate fingerprint, API allowlist and
  provider-console checks in `docs/deployment/oauth-mobile-app-links.md`.
- Preserved the existing `market-snapshot-bundle/v1` boundary. A current
  Windows probe confirmed that Market Data's AAPL demo endpoint is CORS-enabled
  but target ETFs require authorization and its official CORS policy prohibits
  public redistribution under self-service terms. Alpha Vantage's official
  endpoint is CORS-enabled but requires a user key and exposes price/technical
  APIs rather than the approved FM feature contract. Nasdaq's chart response
  returned no CORS header. None is enabled as a public provider.

### Verification evidence

- `corepack pnpm --filter @foresightnine/mobile test`: **55 passed**.
- `corepack pnpm --filter @foresightnine/mobile typecheck`: passed.
- `corepack pnpm --filter @foresightnine/mobile export:android`: passed; Expo
  bundled 1,313 modules and emitted the Android Hermes bundle.
- Dynamic `app.config.cjs` check produced an HTTPS `autoVerify` intent filter
  with the configured host and callback path.
- The initial ADB check found no target, but a later QA session started
  `emulator-5554` (`sdk_gphone64_x86_64`, API 36 Play). The signed QA APK
  installed, cold-started, switched language, reached the staging Challenge
  screen, and produced reviewed screenshots; a physical-device run remains
  pending.
- Native Gradle assembly in the long-path repository layout reached CMake but
  failed in linked `react-native-screens`/`react-native-worklets` with
  `ninja: error: manifest 'build.ninja' still dirty after 100 tries`. A clean
  short-path QA clone with workspace dist outputs and CMake 3.30.5 later
  produced the non-debug x86_64 APK/AAB; release signing remains fail-closed
  until the owner injects all `FORESIGHTNINE_ANDROID_KEY*` values.
- A short-drive retry avoided the original long-path warning but failed earlier
  because Expo bundling resolved dependencies under `D:` while the mapped
  project used `F:` (`this and base files have different roots`). No source or
  dependency workaround was applied.
- The secret scanner now reads reachable Git objects through one batched
  `git cat-file` process instead of spawning one process per path. The current
  run inspected 1,048 unique Git-history blobs and passed with no findings.

### Android QA release follow-up

- A clean short-path QA clone with hoisted dependencies and CMake 3.30.5
  produced non-debug x86_64 release APK/AAB outputs. The exact commands,
  hashes, signing boundary, emulator evidence and remaining physical-device
  gate are recorded in `docs/release/android-qa-release-2026-08-31.md`.
- A disposable local QA key signed copies for emulator installation only; the
  project's production signing gate was not weakened and no production key was
  created or stored.
- `emulator-5554` (API 36 Play) installed the signed APK, cold-started the
  main activity, switched language, and reached the staging API Challenge
  screen. Screenshot evidence was visually reviewed. A physical device run is
  still pending.
- The configured `pov_pixel_tablet_api35` AVD could not start because its
  Android 35 `google_apis/x86_64` system image is absent; the local SDK only
  exposes the Android 36 image. Tablet evidence is therefore still pending
  and this is recorded as a test-environment gate.

### Current interpretation

- The Android OAuth flow is code-integrated and exportable, and the core app
  flow has emulator evidence. It is not production-ready until the HTTPS App
  Link is deployed and verified, production signing is supplied, the approved
  ABI set is built, and a physical-device run completes the flow.
- The closest market-data adaptation remains a provider-side or explicit
  user-BYOK adapter that emits exactly three bounded anchor rows per asset.
  Price-only data may be displayed as evidence, but must remain `limited_data`
  for FM/Public Default until exact factor features, FX semantics and public
  redistribution rights are present.

## Public provider re-check — 2026-08-31

- Windows reached the Stooq CSV endpoint with HTTP 200, but the response was
  an anti-automation verification HTML page rather than CSV. The staging
  adapter therefore correctly returned explicit `missing` rows for SPY, SOXX
  and GLD instead of treating the challenge page as market data.
- Yahoo Finance chart data remains technically retrievable from a server-side
  probe, but it is a vendor GET protocol with no browser CORS, price-only
  fields, and no approved FM feature or public-redistribution contract. It is
  not wired into production or used as a factor source.
- The nearest safe adaptation remains a thin server-side provider adapter:
  normalize only bounded point-in-time price rows into the existing
  `market-snapshot-bundle/v1`, preserve explicit `missing`/`stale` status, and
  keep factor features and FX unavailable until their exact contracts and
  rights are separately approved. The current staging endpoint is therefore
  usable for fail-closed integration testing, not as a live Public Default
  feed.

## OAuth ticket and client-boundary progress — 2026-08-31

- API OAuth callback now uses a server-only, short-lived, one-time opaque
  exchange ticket. The callback never places a bearer token or provider token
  in a URL; `POST /v1/auth/oauth/exchange` consumes the ticket before issuing a
  local session. PKCE/state replay, provider failure, cancellation, ticket
  replay/expiry, logout and account deletion are covered by API tests. State
  and ticket records use the existing versioned PostgreSQL repository when
  configured, so a process restart does not discard a live transaction and a
  concurrent replay cannot win a second consume.
- The exchange route and its strict request/response schemas remain present in
  the generated OpenAPI JSON/YAML contract; no contract or wire semantics were
  changed by the persistence correction.
- Web now has a separately reviewed strict OAuth client boundary in
  `src/apps/web/src/oauth-client.ts`: explicit HTTPS return URI only, safe API
  base validation, callback parameter rejection, opaque-ticket exchange,
  sessionStorage-only persistence, and bearer-header logout/delete helpers.
  The helper tests and Web typecheck pass; pushed in `68ce0fa`.
- Current staging OAuth readiness is intentionally disabled because no provider
  credentials were injected. Android device evidence remains QA-only; the
  production App Link still requires the externally verified domain and release
  certificate inputs documented by the mobile build gate.

## Staging client default and Public Default fail-closed coverage — 2026-08-31

- Wired Web and Android to use `https://api-staging.foresightnine.com` when no
  supported API override is supplied. Canonical configuration still wins,
  legacy configuration remains a fallback, malformed explicit configuration
  fails closed, and Vitest keeps both clients deterministic/offline.
- Strengthened Web and Android freshness coverage for explicit missing/stale
  market rows and stale/unavailable provider status. Public Default remains
  disabled while FM Raw is retained; invalid market rows are rejected rather
  than replaced with fixture/mock prices.
- No production enablement, OAuth credential, provider, factor, FX, server,
  or commit change was made.

### Verification

- `corepack pnpm --filter @foresightnine/web test`: passed, 4 test files and 56 tests.
- `corepack pnpm --filter @foresightnine/mobile test`: passed, 5 test files and 49 tests.
- `corepack pnpm --filter @foresightnine/web typecheck`: passed (`tsc --noEmit`).
- `corepack pnpm --filter @foresightnine/mobile typecheck`: passed (`tsc --noEmit`).
- `git diff --check`: passed; the diff contains only the seven owned paths and no added credential pattern matches.

### Material judgment call

- The staging default is applied only by the app-facing runtime lookup and is
  suppressed under Vitest; the pure environment helper continues to return
  `null` for absent values so tests and explicit offline callers remain safe.

## Public-provider assessment and canonical adaptation plan — 2026-08-30

- Continued read-only evaluation of public sources. Market Data's no-token
  demonstration is limited to unlocked symbols: the Windows probe received
  HTTP 401 for PAVE, SOXX, XLU, GLD and VT. Its official CORS/data policy also
  prohibits using self-service CORS data to serve public users.
- biquote health and symbol discovery were reachable, but the actual response
  had no CORS header and daily bars for PAVE, SOXX, XLU, GLD and VT were empty.
  Xoomar macro/ETF-flow and Fear & Greed Chart responses are useful only as
  optional context; neither covers the current price-and-FM contract. HF
  Market Data's claimed keyless endpoint timed out in the Windows probe and
  remains unverified.
- Added `docs/data/public-provider-adaptation-plan.md`. It concludes that no
  verified provider currently meets unauthenticated access, public
  redistribution, target-asset coverage and exact FM feature provenance at
  once. It specifies the nearest safe path: preserve `MarketSnapshotBundle v1`,
  use a thin allowlisted Linux/Cloudflare adapter only after licensing approval,
  normalize vendor bars to the three anchors, and fail closed with explicit
  limited data when factor or FX fields are absent.
- Updated `docs/deployment/linux-market-provider-next-prompt.md` to make this
  assessment the Linux handoff reference. No provider URL, credential, proxy,
  cache or unlicensed dataset was added; no Linux or Cloudflare operation was
  executed.

## Public-provider probe and Linux follow-up prompt — 2026-08-30

- Performed a read-only network probe against the Yahoo Finance chart endpoint.
  PAVE, SOXX, XLU, GLD and VT each returned HTTP 200 and 165 daily rows for
  the 2026-01-01 through 2026-08-30 probe window, with a first session on
  2026-01-02 and a latest session on 2026-08-28.
- The candidate is not configured as a production provider: the endpoint did
  not return `Access-Control-Allow-Origin`, uses a vendor protocol rather than
  the product `POST /snapshots` contract, and supplies prices but not the
  approved FM feature fields. No client URL, proxy, cache or unlicensed data
  was added.
- Added `docs/deployment/linux-market-provider-next-prompt.md`, a ready-to-send
  Linux agent prompt. It directs Linux to deploy persistent API/PostgreSQL/
  Redis/Cloudflare/OAuth/account services, optionally build only a thin audited
  provider adapter after licensing approval, and never move FM or invent factor
  features. Windows-side implementation is exhausted for this provider
  decision; the remaining work is deployment authority, provider contract,
  licensing and production service integration.

## Client market runtime and Linux boundary — 2026-08-30

- Extended the shared client market-data package from a static contract into a
  runtime controller with explicit `mock`, `public` and request-only `byok`
  modes. It emits `idle/loading/ready/limited/error`, clears BYOK credentials
  after the one request, and never silently substitutes mock data after an
  explicitly selected public-provider failure or missing URL.
- Web and Android now request the bounded three-anchor bundle when FM starts,
  map only the latest completed-session price into their own asset shape, and
  feed the validated bundle into the existing local FM path. Web keeps the
  Worker boundary; Android keeps the shared TypeScript engine. The Web top
  status and Android FM completion state identify the provider mode and the
  three-point limitation.
- This is not a historical backtest or a server-side portfolio calculation.
  The client requests year-start, latest-month-start and latest-completed-
  session rows only; missing/stale rows remain visible as limited data. A real
  provider, data license, FX contract and factor-manifest approval are still
  external gates.
- Linux scope is explicitly reduced for this slice: deploy and supervise the
  API, Cloudflare ingress, OAuth/account boundary, PostgreSQL and Redis. Linux
  must not implement market-data aggregation, FM formulas, factor selection,
  weights, amounts or shares. It may expose a bounded provider endpoint or
  signed metadata only after the deployment owner supplies the approved data
  contract.
- Focused verification after this slice: market-data `14/14`, Web `52/52`,
  Android `45/45`; typecheck, Web production build and Android Expo Android
  export are required before the slice is committed. The full-history secret
  scan remains a release gate and is not claimed by this entry.
- Commit `e93d5f1` was pushed to `origin/master`; Sol's final read-only verdict
  for this exact diff is `ship`. The full workspace run also passed with 206
  API tests, 1 skipped and the existing Starlette/httpx warning. A changed-file
  secret scan found 0 findings; this is not a substitute for the slower
  repository full-history release scan.

## Client market snapshot data plane — 2026-08-30

- Pulled the latest Linux Module 1–6 evidence at commit `bfcd6d2`. Linux
  staging, PostgreSQL/Redis supervision, API mock service, Cloudflare API
  tunnel/CORS smoke and OAuth contract-only tests are recorded in
  `infra/deployment/linux-module-evidence.md`. No Linux research source was
  modified.
- Added `packages/market-data`, a shared `MarketSnapshotBundle` contract for
  user-selected assets and exactly three bounded anchors: year start, latest
  month start and latest completed session. Weekend roll-back is implemented;
  exchange-holiday resolution remains provider-owned. The validator rejects
  unknown/duplicate assets, malformed or future-dated rows, invalid prices,
  oversized payloads and integrity mismatches, and preserves explicit stale or
  missing rows as `limited_data` without fixture substitution.
- Added provider mode A (approved HTTPS public endpoint, no authorization
  header) and mode B (user BYOK, request-only bearer header). BYOK keys are
  excluded from bundle fields, integrity hashes, serialization and logs by
  design. No server secret or historical market-data pipeline is used by this
  client path.
- Web and Android now consume the same three-anchor mock bundle and convert it
  to the existing local FM input. No forward returns are invented, so IC/ICIR
  remain offline factor metadata and FM metrics are `null` for this product
  slice. The actual apps remain visibly `mock/offline`; provider configuration
  and async refresh are intentionally a subsequent Windows client phase.
- Verification: market-data typecheck and 9 focused tests passed; Web
  typecheck and 52 tests passed; Android typecheck and 45 tests passed; full
  workspace typecheck and test passed; Web production build and Android Expo
  Android export passed after fixing Metro to import the workspace package.
  The modified/new source set secret audit scanned 13 files with 0 findings.
  The repository's full-history secret scan was attempted but stopped after
  exceeding the local runtime window; the prior clean history result remains
  unchanged and must be rerun before a release gate.
- This change does not claim live market data, licensing, current executable
  allocations, or production readiness. The next Windows phase must connect
  an approved A/B provider at runtime, expose data freshness and limited-data
  states in both clients, and run real Web Worker/Android parity cases. The
  Linux phase only needs to verify the API boundary and must not add a raw
  historical-data dependency or move final FM computation to the server.

## Repository architecture and Linux handoff documentation — 2026-08-30

- Expanded `README.md` into the project-level source of truth for the product
  scope, current local/private QA status, architecture/data boundaries, package
  map, FM ownership and parity rules, dependency/toolchain requirements,
  verification commands, Web/Android QA state, remaining production plan and
  public-launch limitations.
- Added `docs/deployment/linux-transfer-manifest.md`, separating required
  product source, reproducibility evidence, optional licensed/cutoff-safe data
  inputs and explicitly excluded secrets, caches, debug builds, unlicensed data
  and unrelated research directories.
- Added `docs/deployment/linux-agent-handoff.md`, which gives a Linux AI agent
  the read-before-change list, writable/read-only boundary, provider and FM
  rules, phase protocol, external production dependencies and redacted handoff
  report format. It explicitly recommends a new writable Linux application
  workspace and preserves the existing research directories as read-only.
- No plan, agent, benchmark, paper, server source or read-only data directory
  was modified. This documentation change does not remove the existing public
  deployment block: hosting authority, production services/secrets, licensed
  current data, legal/security approvals and a signed Android release remain
  external gates.
- Sol's read-only review initially returned `fix-first`: the original handoff
  treated Windows-absolute-path reference/boundary checks as portable Linux
  gates, omitted root governance files from the archive tier, mixed focused and
  full-suite counts, and used filename globs that could omit required scripts.
  The documents now record those two checks as Windows-checkout evidence (or a
  separately reviewed Linux compatibility harness), include `plan.md`,
  `agent.md` and `GOAL_PROMPT.md` as read-only transfer inputs, use the latest
  full-suite counts (`196` API + `1` skipped, `50` Web, `44` Android, `20` FM,
  `9` domain, `2` asset-registry and `11` API-client), and use content-based
  secret exclusion language that keeps `secret-scan.mjs` in the package.
- A second Sol review found that the transfer manifest did not list the new
  deployment directories and that trusted reverse-proxy CIDRs were not a
  machine-checked production gate. The manifest and handoff now include
  `docs/deployment/` and `infra/deployment/`; the production preflight adds a
  `PROXY_TOPOLOGY` gate requiring either direct mode with no trusted CIDRs or
  reverse-proxy mode with syntactically valid configured CIDRs. The release
  docs require staging verification that those CIDRs are the actual ingress
  networks; syntactic validation is not presented as network ownership proof.
- The final Sol review found that universal trusted networks (`0.0.0.0/0` or
  `::/0`) would pass the syntax-only proxy gate, and that the Linux manifest
  omitted `src/tsconfig.base.json`, the configured `nm/` virtual store and
  TypeScript build-info caches. The gate now rejects universal `/0` networks;
  the manifest now includes the required base config and explicitly excludes
  generated dependency/build caches.
- Re-ran the full repository suite after the preflight change:
  Web `51/51`, Android `44/44`, FM `20/20`, domain `9/9`, asset-registry
  `2/2`, API-client `12/12`, and API `205 passed, 1 skipped, 1 existing
  Starlette/httpx warning`. The run exited successfully. The README uses this
  latest full-suite snapshot; older entries below are retained as historical
  evidence for their respective commits.

## Public deployment gate audit — 2026-08-30

- Rechecked the pushed `master` state (`813a6a3`) and the repository boundary;
  the working tree is clean and the remote branch matches. No production
  `FORESIGHTNINE_*`, database, Redis, CAPTCHA, Android signing, hosting-token,
  or approval-manifest inputs are present in this workstation environment.
- The expected GitHub Pages project URL returns HTTP 404, and no approved
  hosting CLI or deployment target is configured. The product therefore remains
  a local/private QA candidate rather than a public deployment. The release
  runbook records the exact external inputs and order needed to proceed; no
  public traffic is enabled from this environment.

## GLM provider/MCP hardening and independent WL1 parity — 2026-08-30

- Added `infra/deployment/production-release-runbook.md`, a credential-free
  operator sequence covering commit pinning, production env injection, default
  GLM/MCP smokes, signed Android build, staging, rollback, and traffic approval.
- Corrected the deployment overview label from the stale “Phase 8 draft” to
  “Phase 9 release-candidate deployment notes”; the document continues to state
  that external production approval is required.

- Updated the GLM Coding Plan MCP adapter to call the exact tool name returned
  by `tools/list`. The current official response advertises `web_search_prime`;
  the older `webSearchPrime` spelling remains only as a strict fixture alias.
  The parser also accepts the official bounded JSON-encoded text payload and
  direct candidate-array shape, while retaining HTTPS, size, and candidate
  validation. Focused MCP tests pass `7/7`.
- Added a shared provider-call quota reservation before external MCP search.
  Search requests now fail closed when the abuse backend is unavailable and
  are denied after the same daily model budget is exhausted. Focused security
  and provider tests pass `45/45`.
- Added English translations for all runtime export notices and a bilingual
  regression covering JSON, CSV, path, and method exports. Web tests now pass
  `51/51`.
- Added `scripts/fm/audit_wl_warmup_oracle.py`, which independently recomputes
  WL1 factor IC/ICIR, stable selection, average-tie ranking, support-weighted
  signal, long-only weights, volatility scale, forecasts, metrics, and the
  free initial allocation without importing the TypeScript FM package. The
  companion Node audit now compares the independent result with the real
  client engine. On the complete cutoff-safe WL1 slice (20 assets, 64 dates,
  cutoff `2026-07-15`) parity passed with maximum absolute difference
  `3.33e-16`; `synthetic_online_segment_used=false` and `cutoff_safe=true`.
- Added the redacted server-only MCP smoke at
  `docs/qa/api/glm-mcp-live-smoke.md`. The credentialed default-transport run
  on this host fails closed because DNS maps the official hostname to reserved
  `198.18/15`; the SSRF guard remains intact. A one-off diagnostic transport
  delivered the real GLM/MCP responses to the existing parsers, but this does
  not replace the approved ordinary-DNS deployment smoke or production gates.
- Addressed the first Sol review's MCP hardening finding: result traversal now
  has explicit depth and node budgets and maps budget exhaustion to the typed
  non-retryable `search_response_invalid` failure. Regression tests cover both
  budgets. Synchronized the older security note to the official
  `web_search_prime` name while documenting the camelCase fixture alias.
- Focused FM audit, Web, API, MCP, and smoke tests pass. Full repository
  verification and a fresh Sol review remain before integrating this slice;
  public production remains gated by approved provider networking, licensed
  current data, infrastructure, security, legal/compliance, and Android
  release evidence.

## Browser API transport regression — 2026-08-30

- Browser QA found that the API client detached the native browser `fetch`
  function before calling it. Chromium's receiver/brand check rejected that
  call even though direct `window.fetch` and Node tests succeeded, leaving the
  API-backed Web flow in an incorrect unavailable state.
- The client now binds its default native fetcher to `globalThis`; injected
  fetchers remain untouched. Transport failures are classified internally as
  network versus response-validation failures, while the Web surface exposes
  only safe, provider-free notices. A regression test covers the browser
  receiver requirement.
- Focused checks pass: API client `12/12`, Web `50/50`, and Web typecheck.
  A clean in-app Browser tab at `http://127.0.0.1:5173/` now reaches the
  server-backed structured challenge with no error/warning logs. This remains
  loopback deterministic-mock evidence, not public production availability.

## Reproducible local API-backed demo — 2026-08-30

- Added `docs/runbooks/local-api-backed-demo.md` with the exact two-terminal
  commands for running the Web client against the loopback API using the
  deterministic mock provider. It explicitly keeps the API and Web bindings
  on `127.0.0.1`, sets the matching development CORS origin, and preserves the
  client-side Web Worker FM boundary.
- Re-ran the documented path from the current checkout: API `/health` returned
  `200` with `deterministic_mock` and `client-evidence-bundle`; the Web root
  returned `200` and contained the React root mount; the CORS preflight for
  `POST /v1/sessions` returned the configured loopback origin. Both processes
  are currently left running for local inspection. This is reproducible local
  QA, not public production availability.

## Production startup-mode preflight — 2026-08-30

- Audited the production preflight against the actual API boot contract and
  found that a deployment could previously pass URL checks while still
  selecting the API's fail-closed-in-production defaults. The verifier now
  requires `FORESIGHTNINE_STORAGE_MODE=postgres` and
  `FORESIGHTNINE_ABUSE_MODE=redis`, matching the runtime configuration
  required by PostgreSQL persistence and distributed Redis admission control.
- `corepack pnpm test:production-preflight` passes `10/10`, including absent or
  memory-mode negative cases. The real-environment preflight still exits `1`
  as intended because this checkout has no production credentials or external
  deployment inputs; its output remains redacted and lists the missing live
  provider/search, database, CAPTCHA, CORS, Android release, and approval
  gates. Sol's fresh review returned `ship`, found no P0/P1, and authorized
  submission. This improves fail-closed accuracy but does not represent public
  production readiness.

## Current checkout packaging boundary — 2026-08-30

- Performed a fresh local runtime smoke from this checkout: the mock API
  started with `uv run ... uvicorn` and returned `200` from
  `http://127.0.0.1:8000/health`, reporting the deterministic mock provider and
  client-evidence search fallback; the Web Vite server returned `200` for `/`
  with the root mount present. Both tracked local sessions were stopped after
  the check and both ports were confirmed closed. This proves local
  reachability only, not public deployment or live-provider readiness.
- Containerized PostgreSQL smoke initially exposed a real persistence bug:
  the pre-fix dumper stored optional Pydantic fields such as
  `source_refs[].url` as `null`, and strict reload then caused
  `POST /v1/challenges` to return `500`. The dumper now omits `None`; the
  loader performs a narrow legacy compatibility normalization for null-valued
  object fields while preserving null list members so malformed data still
  fails closed. Storage tests cover default repository wiring, old null rows,
  and present URLs. After rebuilding the API image, an old named-volume
  session was successfully advanced through challenge, evidence, confirmation
  and worldline generation: 3 paths (`weak`, `main`, `strong`) and 3
  probability assessments. Sol's fresh review returned `ship` with no P0/P1/P2
  and authorized submission. This is containerized development/mock evidence,
  not public production readiness.
- With the rebuilt API image and the development Compose Redis service, a
  direct runtime smoke of the Redis Lua backend passed: the first request was
  allowed, the second was denied by the IP/session rate limit; the first model
  reservation was allowed, the second was denied by the model quota. Both
  decisions remained backend-available, and the stack was stopped afterward.
  This verifies local Redis protocol/admission behavior only; TLS,
  authentication, HA, eviction, backup/restore and production operations
  remain deployment gates.
- The development Compose profile now explicitly selects `redis` abuse mode,
  supplies only the internal development Redis URL, and waits for the Redis
  healthcheck before starting the API. A fresh Compose run confirmed the API
  environment, `/health` response, and a session/challenge request; Redis
  admission keys increased from 2 to 8. This exercises the distributed path
  locally while keeping the process default and production TLS/authentication
  boundaries unchanged.
- Rebuilt the Web static bundle after the search-configuration parity change.
  The current `apps/web/dist` hashes are `fm.worker-C7HxMD7b.js`
  `9C2BD44FEA8E04FB35CD42A0CB6A551881A5BBB9C3A42C699C319634E3EC2DA6`,
  `index-CyysJoc3.js`
  `7C345429A02816C8FA04B7C3FAF1CA99135A42D7351409A5D2832D94A3599E90`,
  CSS `8ABA60E0273BE2C84305EFBD42EC045AA31D72552D3371E8F66228D3FC5E2A1C`,
  and HTML `C1CBF656CAD01B69166508CED67ED2552371B2355F21E3DD631A84FF584E55D3`.
- The Android debug APK was rebuilt from this checkout successfully with JDK
  17, x86_64, and the repository helper
  `scripts/android-short-cmake.init.gradle`; Gradle reported `BUILD SUCCESSFUL`
  in one minute 51 seconds. The current artifact is `81,070,551` bytes with
  SHA-256
  `37DB03028D3FB207664DAB15F419B50F640C66340DD9FA27E0C210E4324CB97D`, and
  contains `assets/index.android.bundle`, so the debug QA build does not
  silently require Metro.
- The current Web bundle and Android APK were installed/served from the same
  current checkout state. On Google Play API 36 emulator `emulator-5554`, the
  rebuilt APK cold-launched without fatal/ReactNativeJS/RedBox errors. The
  packaged manifest was independently inspected with `apkanalyzer`: it has
  only the declared single `ACTION_SEND` filter for `text/plain` and
  `text/uri-list`; the empty generated `SEND_MULTIPLE` filter is absent.
  A real `ACTION_SEND` text intent opened `/evidence`, normalized a safe HTTPS
  URL, created a local Grade C entry, and showed the expected
  non-independent-review warning. Current evidence is persisted at
  `docs/qa/android/phase9-single-share-only-home.{png,xml}` and
  `docs/qa/android/phase9-single-share-only-share.{png,xml}`; screenshots were
  inspected with `view_image`.
- Sol's earlier fresh review returned `fix-first` for stale Android artifact
  binding. That P1 is now addressed by the current rebuild and evidence. The
  config-plugin regression test and mobile suite pass (`44` tests). One share
  UI-tree child still has an invalid bounds rectangle; it is retained as
  semantic UI-tree evidence only, not as geometry/fidelity evidence.
- Source-level tests, Web/Android packaging, and emulator QA are now verified
  for this slice. A fresh Sol review subsequently returned `ship` with no
  P0/P1 and the documented dependency P2 corrected. The slice was committed as
  `fa22049` and pushed to `origin/master`; this is still QA acceptance, not
  production approval. No plan, governance file, or read-only source directory
  was changed.
- After the manifest hardening and storage compatibility fix, the full
  regression is green: `50` Web tests,
  `44` Android tests, `20` FM tests, `9` domain tests, `11` API-client tests,
  `2` asset-registry tests, and `196` API tests with `1` skipped and the
  existing Starlette/httpx deprecation warning. Typecheck, generated-contract
  verification, 9WL audit, production-preflight unit tests, `pnpm audit
  --prod`, `git diff --check`, and the release-candidate verifier also pass.
  Secret scan completed with `628` unique Git-history blobs inspected and no
  findings.
- Earlier Sol planning/review attempts on 2026-08-30 exited because the Codex
  sub-agent usage limit was reached. A later fresh Sol review completed after
  the fix and authorized the QA slice; the usage-limit events are retained in
  this history for audit clarity.

## Android single-share manifest hardening — 2026-08-30

- Expo SDK 57's sharing plugin emits an empty `SEND_MULTIPLE` intent filter
  when `multipleShareMimeTypes` is empty. Added the scoped
  `apps/mobile/plugins/with-single-share-intent.cjs` config plugin and its
  direct `@expo/config-plugins` build-time dependency. The plugin removes only
  an Expo-generated `SEND_MULTIPLE` filter with no data, preserving unrelated
  filters and the declared single-share MIME types.
- `corepack pnpm --filter @foresightnine/mobile test` passes `44` tests. Native
  prebuild and Gradle rebuild pass, and `apkanalyzer manifest print` confirms
  the final APK contains no `SEND_MULTIPLE` action. No multi-item share feature
  is claimed; a future multi-item requirement needs a separate tested design.
- The prebuild operation regenerated Android native files; the previously
  approved share routing, debug bundling, release-signing guard, and wrapper
  timeout were restored and are unchanged relative to the prior approved
  native configuration except for the intended manifest filter removal.

## Search configuration parity and Android share-intent evidence — 2026-08-29

- Closed the Sol-identified P1 configuration mismatch. Web and Android now
  resolve the canonical public API variables first
  (`VITE_FORESIGHTNINE_API_BASE_URL` and
  `EXPO_PUBLIC_FORESIGHTNINE_API_BASE_URL`) while preserving the documented
  legacy aliases only when the canonical value is absent. Invalid canonical
  values fail closed instead of silently selecting a different endpoint.
  Pure configuration parsing is isolated from native Expo imports so the
  mobile test runner does not load `expo-crypto` just to test environment
  selection.
- Added Web and Android regressions for canonical precedence, legacy
  compatibility, and malformed-value rejection. The complete repository test
  suite passes: `50` Web tests, `42` Android tests, `20` FM tests, `9` domain
  tests, `11` API-client tests, `2` asset-registry tests, and `193` API tests
  with `1` skipped and the existing Starlette/httpx deprecation warning.
  TypeScript typecheck also passes.
- Exercised the embedded Android debug QA APK through a real
  `android.intent.action.SEND` intent on Google Play API 36. The system
  resolver listed ForesightNine for `text/plain`; after selection the app
  cold-opened `/evidence`, normalized a safe HTTPS URL, created a local Grade C
  entry, and visibly marked it as not independently reviewable. No source URL
  was fetched. Evidence is persisted at
  `docs/qa/android/phase9-share-intent-evidence.png` and `.xml`.
- The remaining P2 is documented rather than hidden: Expo's generated
  manifest also contains an empty `SEND_MULTIPLE` filter, while the product
  acceptance path is single text/URL sharing. A future multi-item requirement
  must either add a tested URI-only flow or remove that filter explicitly.
  Production provider/search credentials, licensed current data, legal/privacy
  approval, OAuth callback/token exchange, infrastructure/CAPTCHA, Android
  release signing, and public deployment evidence remain external gates.

## Standalone Android QA and GLM adapter hardening — 2026-08-29

- The Android debug QA artifact was rebuilt from the current checkout after
  setting `react.debuggableVariants = []`. Expo Android export and the
  approved x86_64 Gradle command passed; the resulting APK is
  `apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk`,
  82,336,416 bytes, SHA-256
  `8CF403FEAD63C336E9BBDCF831762DA7272EEECDB715E2200AD8C38BA5331291`.
  `assets/index.android.bundle` is present, so this debug QA artifact does
  not silently require Metro.
- After Metro was stopped, the APK installed and cold-launched on
  `emulator-5554` (Google Play API 36). UI-tree-derived actions completed
  the standalone path: thesis/capital → challenge → response → reserved
  confirmation → three-branch worldlines → client FM → FM Raw/Public Default
  configuration. Port 8081 was not listening, the crash buffer was empty,
  and the filtered log contained no fatal/ReactNativeJS/RedBox error. Persisted
  evidence is under `docs/qa/android/phase9-standalone-{home,worldline,portfolio}.{png,xml}`;
  `view_image` inspection found no material clipping in the reviewed states.
  This remains a debug-signed QA artifact, not a release APK/AAB.
- The GLM adapter now gives the fixed Coding profile a 4,000-token budget when
  no explicit policy is supplied, while preserving explicit generic policies.
  Its prompt enumerates the exact ten rubric criteria, top-level JSON keys, and
  one-to-three-question limit. Malformed outer or message JSON raises a
  cause-free typed failure so raw model output cannot persist in an exception
  chain. The normal production transport still enforces the public-DNS/host
  SSRF boundary; no bypass was added.
- A bounded credentialed adapter test reached the official GLM endpoint with
  an ephemeral server environment and medium reasoning effort, returned HTTP
  200, and validated the strict challenge shape, citations, and all ten rubric
  items. This is adapter-integration evidence only; no key was persisted and
  no public live-provider deployment is claimed. The stock smoke remains
  fail-closed without a deployment-injected secret, and this host's reserved
  DNS resolution is rejected by the production SSRF guard.
- Verification passed: focused GLM tests `34 passed`; full API suite
  `193 passed, 1 skipped, 1 warning`; root `corepack pnpm test`; TypeScript
  typecheck; generated contracts; governance boundaries; 9WL fixture audit;
  secret scan over `619` unique Git-history blobs; `corepack pnpm audit --prod`;
  and the release-candidate verifier. The production-preflight test suite
  passed `9/9`; the real production preflight correctly failed closed because
  production provider/search secrets, Postgres, Redis, external CAPTCHA,
  HTTPS CORS, release Android artifact, and external approval manifest remain
  absent.
- Fresh Sol review returned `ship` with no P0/P1/P2 findings. No plan,
  agent, goal, FM reference, or external read-only source directory was
  modified. Public deployment, legal/privacy/terms/risk approval, licensed
  current market data, production operations, OAuth callback/token exchange,
  Google Play billing, release signing, and store/public-domain evidence
  remain external gates.

## Rendered Web/Android flow and live-provider fail-closed gate — 2026-08-29

- Web was verified through the in-app Browser at the Vite network binding
  `http://192.168.196.222:4174/` after the client blocked loopback URLs. The
  viewpoint → capital → challenge → reply → reservation confirmation →
  worldline → Console path completed with meaningful DOM, no horizontal
  overflow at 390×844 and 834×1112, and no browser warning/error logs.
- Android API-36 emulator evidence was refreshed from the current Expo
  development-client bundle (not claimed as a freshly embedded standalone
  APK): home → challenge → reply → reserved confirmation → worldline →
  client FM portfolio completed. The tree showed FM Raw and Public Default,
  stale-data amount disabling, and the safety-constraint explanation. A
  reversible 1440×2560 tablet-layout pass completed and the emulator was
  restored to its physical 1080×2400/420-density configuration; crash-log
  capture was empty.
- The GLM coding live smoke test failed closed as expected without a new
  server-injected secret: `status=blocked`, `secret_missing`,
  `no_network_attempted=true`, exit code 2. No historical key was read,
  copied, or used, and no live-provider result is claimed.
- Remaining gates are explicit: fresh standalone Android packaging, OAuth
  callback/deep-link completion, Google Play Billing, production provider and
  current licensed-data integration, HTTPS/deployment, privacy/terms/legal
  review, and release signing. The product is locally usable in mock/offline
  mode and is not yet a public production service or trading system.

## Client FM allocation accuracy correction — 2026-08-29

- Closed a client-side allocation edge case found by Sol: `allocateCapital`
  now rejects materially over-invested target weights, preserves valid
  under-invested targets as non-negative cash, and fails closed when tolerated
  floating-point overage or allocation arithmetic would produce a negative or
  non-finite result. It does not silently normalize weights or move any
  calculation to the server.
- Added regressions for materially over-invested totals, large-capital
  tolerated overage (`1e12`), non-finite allocation arithmetic, and valid
  under-invested residual cash. FM golden behavior remains unchanged.
- Primary verification: FM TypeScript typecheck passed; native Node test
  runner passed `20/20`; the real read-only WL1 warm-up audit still selected
  factors `101, 102`, cutoff `2026-07-15`, used no synthetic online segment,
  and preserved client result hash
  `8ec7cccc74bea8c8e79869f5ddbeb4d37bf6a61893fab179b5323dceca22e6b1`.
  Fresh Sol review returned `ship` with no findings.
- This remains a historical development audit, not a return/backtest claim or
  current investable recommendation. Extreme market-data arithmetic and
  licensed/current-data validation remain product-release concerns.

## OAuth readiness/start slice — 2026-08-29

- Added strict server-side readiness and PKCE-start contracts for Google and
  GitHub at `/v1/auth/providers` and `/v1/auth/{provider}/start`. Readiness
  exposes only provider name and enabled state; start returns only the safe
  authorization fields and enforces the existing HTTPS/deep-link allowlist,
  PKCE S256, one-use state and TTL boundary. No provider network call, token
  exchange, nonce/verifier/client secret, or user credential is exposed.
- Web and Android Console/account surfaces now consult server readiness only
  when an API base URL is configured. Offline and unavailable states remain
  disabled; even server-ready providers remain disabled with explicit
  callback-not-wired copy because callback/deep-link handling is not yet
  implemented. The visible entry is Console and the methodology and
  subscription-not-configured content remain available there.
- Focused OAuth/OpenAPI checks passed (`28 passed, 1 warning`); full API suite
  passed (`191 passed, 1 skipped, 1 warning`); Web/Mobile/API-client
  typechecks, 90 Vitest tests, generated-contract checks, governance
  boundaries, immutable FM reference hashes, 9WL fixture audit and diff check
  passed. A fresh Sol review returned `ship` with no findings.
- This is an integration boundary, not production authentication or billing.
  OAuth provider registration/secrets, callback/token exchange, shared
  transaction storage, Google Play Billing, and release/legal gates remain
  external or future work. No plan or read-only source directory was modified.

## Shared API client and dual-runtime API wiring — 2026-08-29

- Added `packages/api-client` as the typed Web/Expo transport boundary. It
  validates generated response shapes at runtime, rejects unknown fields per
  SSE union variant, sends POST idempotency keys, propagates `AbortSignal`,
  URL-encodes path identifiers, preserves safe status/retry-after diagnostics,
  redacts provider details, and validates `operation_id:sequence` SSE replay
  IDs with `Last-Event-ID`.
- The client now uses an injected secure idempotency-key source: Web uses the
  platform Web Crypto source, while Expo injects `expo-crypto` random bytes.
  There is no timestamp/counter fallback. Server-side provider credentials
  remain outside both bundles.
- Web reads `VITE_FORESIGHTNINE_API_BASE_URL`; Android reads
  `EXPO_PUBLIC_FORESIGHTNINE_API_BASE_URL` (the legacy
  `EXPO_PUBLIC_API_BASE_URL` name remains a compatibility alias). Without a
  valid URL both clients visibly use the deterministic offline/mock provider.
  With a URL, challenge, evidence, confirmation, and worldline retrieval use
  the API client; a failed API worldline request does not fall back to mock
  branches. FM remains a client-local computation path.
- A direct typed-client run against the local FastAPI deterministic mock
  completed session → challenge → reply → reservation confirmation →
  worldlines, returning three worldlines and three matching probability
  assessments. Worldline-to-assessment mismatches now fail closed rather than
  selecting the first assessment. Web evidence timestamps are generated at
  capture time rather than hardcoded.
- Terra's P1 hardening was verified in the primary session: API-client
  TypeScript build and 11 tests, Web typecheck and 49 tests, Mobile typecheck
  and 41 tests, root Vitest (90 tests), Web production build, and
  `git diff --check` all pass.
- Android API-36 emulator evidence was refreshed with UI-tree-derived actions.
  The local API-backed flow now demonstrates server challenge output, a
  completed response, reservation confirmation, and a three-branch worldline
  screen in `docs/qa/android/phase-api-mode-worldline-success.{png,xml}`.
  This uses the deterministic mock provider, not a live GLM/DeepSeek request.
  The emulator crash buffer is empty. The debug APK remains a QA artifact and
  is not a signed release artifact.
- Governance, immutable FM reference hashes, 9-worldline fixture audit, and
  release-candidate artifact verification pass. Production preflight remains
  intentionally fail-closed: live provider/search secrets, production
  Postgres/Redis/CAPTCHA/HTTPS configuration, Android release signing, and an
  external approval manifest are still missing. No plan or read-only source
  directory was modified.

## Android API-36 native regression evidence — 2026-08-29

- Used the Android emulator QA workflow against `emulator-5554` (Google Play
  image, API 36) with a fresh current-checkout x86_64 debug APK installed via
  `adb`; the installed activity resolved to
  `com.foresightnine.mobile/.MainActivity`. The release build was correctly
  refused because no signing secrets are configured; the debug APK remains a
  QA artifact, not a signed or store-ready release.
- Re-ran the current clean mobile flow from the thesis screen into the mock
  challenge. UI-tree-derived bounds were used for the start action, response
  field, submit action, and language toggle; the completed challenge state
  rendered one clean user response and the expected `完成` status. English
  home and challenge content also rendered without clipping or overflow.
- `view_image` inspection found the warm paper canvas, restrained type scale,
  stage rail, warning card, rubric, and CTA hierarchy visually coherent at
  1080×2400. Current-checkout evidence is stored under
  `docs/qa/android/phase-current-debug-{home,challenge,challenge-complete}.{png,xml}`;
  the interaction dumps used to derive the response and submit bounds are
  kept separately as QA diagnostics, not release evidence. The debug APK was
  run with the existing Metro bundle through `adb reverse tcp:8081`.
- Runtime checks: Android SDK 36, package launch succeeded, crash buffer had
  zero lines, and the filtered app log had no fatal exception, AndroidRuntime,
  or RedBox entries (only the normal `ReactNativeJS: Running "main"` line).
  Current QA debug APK SHA-256 is
  `9E2B2ADAA4132121B613DF342EB1E9FB2D489B5E3A1B6C0B8738DB4FE06DD26E`.
- Mobile verification also passed: TypeScript typecheck and 41 Vitest tests.
  The restricted x86_64 `assembleDebug` completed successfully with
  `422 actionable tasks: 41 executed, 381 up-to-date`; the default arm64
  attempt is recorded as an environment-specific CMake failure.
  This is native emulator regression evidence only; it does not establish
  Android signing, Play Store packaging, self-contained offline launch, or
  public deployment readiness.

## GLM-5.3 provider contract and live-smoke boundary — 2026-08-29

- The server-only GLM Coding profile is fixed to the official
  `https://open.bigmodel.cn/api/coding/paas/v4` endpoint, host allowlist, and
  `glm-5.3` model. A non-empty legacy model override fails closed before any
  transport call; the generic OpenAI-compatible profile remains separate.
- The GLM wire contract sends structured JSON controls for enabled thinking,
  the Coding Plan reasoning mapping, and strict `do_sample`; it intentionally
  does not read or send `temperature` or `top_p`, matching the latest product
  decision. Provider failures, citation mismatches, invalid structured output,
  and `reasoning_content` leakage have regression coverage.
- Added the server-only `apps/api/scripts/glm_coding_live_smoke.py` command and
  its tests. Without `FORESIGHTNINE_GLM_CODING_API_KEY`, it exits 2, emits only
  a redacted `secret_missing` result with `no_network_attempted=true`, and does
  not fall back to mock. With a key injected by a deployment secret manager,
  it sends one synthetic challenge and emits only redacted validation metadata.
- Verification: provider tests 28 passed; live-smoke tests 4 passed; Python
  compileall passed; Git-history secret scan passed for 569 unique blobs; Sol
  returned `ship` for both the wire-contract and live-smoke slices. Commits
  `138b7a5` and `6b4e744` were pushed to `origin/master`.
- No credentialed live request has been made. A real `glm-5.3` smoke, provider
  account authorization, Secret Manager injection, and public deployment
  remain external release gates. Official parameter references are recorded in
  `docs/security/glm-openai-compatible-configuration.md` and
  `docs/qa/api/glm-coding-live-smoke.md`.

## Redis distributed abuse-control slice — 2026-08-29

- Added `FORESIGHTNINE_ABUSE_MODE=memory|redis`, preserving memory as the
  development/test default and failing closed at startup for missing, unknown,
  memory, or missing-URL hardened configurations.
- Added Redis Lua request windows and model quota reservations using Redis
  server `TIME`, atomic multi-subject admission, hashed subjects/markers, the
  fixed `{abuse}` cluster hash tag, bounded sorted sets, and bounded TTLs.
  Redis runtime failures are typed retryable 503s with no memory fallback;
  exhausted limits remain 429s. Redis reset is non-destructive.
- Added deterministic recording-Redis tests for script calls, atomicity,
  exact retry boundaries, secrecy, idempotency, TTL/cardinality bounds,
  configuration, unavailable mapping, and memory regression, plus an opt-in
  live test that is skipped without explicit deployment settings.
- Local implementation and fake-backend evidence is code-complete after the
  focused tests. A Docker Desktop runtime probe then brought up PostgreSQL,
  Redis 7, and the API from `infra/docker/compose.yaml`: PostgreSQL and Redis
  health checks passed, `/health` and `/openapi.json` returned 200, and the
  migration created the five expected persistence tables. A real API session
  write produced one durable session document, one completed operation, and
  four operation events; after an API restart, the same idempotency key returned
  `201` with `idempotency-replayed=true` and the same persisted result.
- A live Redis Lua probe against the Compose Redis service passed: the request
  window admitted two requests then denied the third at the configured IP
  limit, and the model quota admitted the first reservation, replayed the same
  operation marker, then denied a different marker. One-off configuration
  loading selected `RedisAbuseControl|redis`. The repository's opt-in pytest
  live test remains source-only because the production image intentionally does
  not package test files; the direct live probe is the runtime evidence for
  this slice.
- No HA/TLS/eviction/failover/alerting/capacity or production deployment
  evidence is claimed. Compose remains a secret-free development setup and its
  API service intentionally defaults to memory abuse control; public traffic
  still requires explicit Redis, provider/search, CAPTCHA, licensed data,
  compliance, and deployment gates.

## Durable operation state slice — 2026-08-29

- Sol narrowed the next step to PostgreSQL operation state and explicitly deferred Redis. Added `apps/api/app/operation_store.py` and `infra/migrations/002_operation_state.sql`; `OperationManager` now routes idempotency claims, typed terminal receipts/errors, operation records, worker lease metadata, session associations, and append-only SSE events through the selected store.
- The PostgreSQL path uses a unique endpoint plus SHA-256 idempotency-key hash gate, row locking for terminal transitions, parameterized SQL, and no raw idempotency keys or request bodies. Expired leases are converted to retryable terminal failures; the current provider is never automatically resubmitted without a provider receipt. SSE replay validates the actual retained sequence range.
- The development/test adapter remains deterministic and thread-safe. Primary regressions cover same-key replay/conflict, typed concurrent replay, control-command replay flags, cancellation/completion serialization, SSE trimming, retention, lease recovery, and two-manager store behavior.
- Primary verification: focused operation/storage/security tests 49 passed; full API suite 159 passed; Python compileall, root TypeScript typecheck, generated contracts, governance boundaries, Compose config, and `git diff --check` passed. The API retains the documented Starlette/httpx deprecation warning.
- A fresh Sol gate returned `ship` with no P0/P1/P2 findings for this bounded slice. No live PostgreSQL/psql, migration execution, Docker build/startup, or cross-process runtime evidence is claimed. Redis is provisioned only; distributed rate limits/model quotas remain the next implementation slice. Public provider/search credentials, licensed data, legal approval, Android signing, and deployment gates remain open.

## PostgreSQL entity storage integration — 2026-08-29

- Implemented the Sol-approved entity-persistence slice under `apps/api`: production-configured repositories now support PostgreSQL for sessions, theses, challenges, challenge turns, evidence, worldlines, market manifests, and worldline-to-challenge relationships. Development and tests retain the explicit in-memory mode; production/public hardening fails closed when PostgreSQL is not configured.
- Added the idempotent `infra/migrations/001_research_state.sql` migration for versioned JSONB documents, expiry metadata, collection/key validation, and lookup indexes. Added development-only PostgreSQL and Redis services to `infra/docker/compose.yaml`; Redis is provisioned for the next coordination slice but is not yet wired to abuse controls or operation state.
- Retention now uses an explicit unfiltered retention enumeration, deletes through repository APIs, preserves relationship-based worldline cascade cleanup, and re-raises storage runtime failures. Ordinary reads hide expired documents consistently in memory and PostgreSQL; deterministic clock injection and query-path regressions cover this boundary.
- Primary verification for the current worktree: focused API storage/security tests 29 passed; full API suite 149 passed; root TypeScript typecheck passed; generated-contract and governance-boundary checks passed; Compose config parsing passed; `git diff --check` passed with only existing line-ending warnings. The API retains the documented Starlette/httpx deprecation warning.
- This is not public production readiness. Durable operation state is a separate follow-on slice and is still pending its Sol gate; Redis atomic quotas/rate limits, live PostgreSQL/Redis runtime evidence, Docker build/startup evidence, provider/search secrets, licensed data, legal approval, Android signing, and deployment gates remain open.

## Dependency audit remediation — 2026-08-29

- A fresh Sol commitment-boundary review approved a narrowly scoped fix for `GHSA-w5hq-g745-h8pq`: the only vulnerable production path was Expo's `xcode@3.0.1 -> uuid@7.0.3` edge, and `xcode` only requires the CommonJS `uuid.v4()` interface. No plan, agent, governance, FM reference, or read-only source was changed.
- Terra first confirmed that `package.json.pnpm.overrides` is ignored by pnpm 11.21.0, reverted that attempt, and left no tracked-file change. The corrected implementation uses the supported root `overrides` entry `xcode@3.0.1>uuid: 11.1.1` in `pnpm-workspace.yaml`; the lockfile changes only the override metadata and the `uuid` package/snapshot edge. The accidental untracked `src/uuid` install log was removed.
- Primary verification passed: `corepack pnpm audit --prod` (`No known vulnerabilities found`); CommonJS installed-tree smoke (`xcode` 3.0.1 loaded, `uuid` 11.1.1, `uuid.v4()` and xcode project export available); `corepack pnpm test` (FM 16, domain 9, asset registry 2, Web 49, Android 41, API 128); typecheck; generated contracts; governance boundaries; six immutable FM references; 9WL audit; production preflight (9/9); release-candidate verifier; `git diff --check`; and `corepack pnpm scan:secrets` (518 unique Git-history blobs).
- `corepack pnpm --filter @foresightnine/mobile export:android` passed. Android Gradle debug build remains unverified on this host because neither `java` nor `JAVA_HOME` is available; the existing APK is still QA-only and no signed release artifact is claimed. Overall public-release goal remains active behind live provider/search runtime, database/Redis/CAPTCHA operations, licensed data, approvals, Docker build/startup, signing, and public deployment evidence.

## Post-Phase 9 production preparation — 2026-08-29

- A fresh Sol production-preparation audit found locally fixable release-safety gaps: unknown or hardened mock LLM selectors could fall back to simulated output, the API Dockerfile omitted `packages/asset-registry`, Compose used an unread provider variable, and Android accepted non-contract search-result hashes. It also separated external blockers that cannot be completed without new credentials, runtime infrastructure, licensed data, legal approval, or release ownership.
- Added the fail-closed `scripts/verify/production-preflight.mjs` and 9-test suite. It checks explicit live provider/search configuration, server-side secret presence without printing values, Postgres/Redis/CAPTCHA/CORS shape, an external approval manifest, Dockerfile/Compose active configuration, and an external non-debug Android APK/AAB. The current development environment correctly exits non-zero.
- Fixed the selector and client-contract gaps: production/public-hardening mock and unknown LLM selectors now return a safe unavailable provider; Android search candidates require a 64-character lowercase hexadecimal result hash. The Dockerfile now copies `packages/asset-registry`, and Compose uses `FORESIGHTNINE_LLM_PROVIDER`. No plan/agent/goal governance file or read-only source was changed.
- Current regression passed: `corepack pnpm test` (FM 16, domain 9, asset registry 2, Web 49, Android 41, API 128); `corepack pnpm test:production-preflight` (9/9); root typecheck; generated contracts; governance boundaries; immutable FM references; 9WL audit; release-candidate verifier; Compose config; and `git diff --check`. A fresh `corepack pnpm scan:secrets` passed after the implementation changes, inspecting 503 Git-history blobs. The API retains one documented Starlette/httpx deprecation warning.
- Container evidence boundary: `docker compose -f infra/docker/compose.yaml config --quiet` passed. An attempted `docker compose ... build api` could not start because this host has no Docker Desktop Linux engine/daemon (`npipe:////./pipe/dockerDesktopLinuxEngine` was unavailable); therefore Docker image build and container startup remain **not verified**, and no production image claim is made.
- The fresh final Sol review returned `ship` with no P0/P1/P2 after the Docker evidence correction. This production-preparation change set was committed as `271cd683f947c90a22df19dc97bfdd5fae2807ca` and pushed to `origin/master`. Overall goal remains active and cannot complete while live GLM/MCP runtime, PostgreSQL/Redis/CAPTCHA operations, licensed data, legal/compliance approvals, dependency advisory resolution, Android signing/store release, and public deployment evidence are absent.

## Phase 9 release-candidate preparation — 2026-08-29

- Phase 9 started only after rereading the supplied goal objective, `plan.md`, and `agent.md`; neither governance file was changed. A fresh read-only Sol planning review returned `fix-first` for evidence freshness rather than a code defect: regenerate Web/Android artifacts from the current checkout, add a unified release-candidate verifier, refresh the fidelity boundary, and record an API smoke run.
- Remediated that evidence gap in `docs/release/phase9-release-candidate.md`, `docs/qa/api/phase9-mock-smoke.md`, `scripts/verify/release-candidate.mjs`, and `docs/design/fidelity-qa.md`. The verifier checks required QA evidence, Web `dist`, Android debug APK, and prints SHA-256 without reading or printing secrets. Added `corepack pnpm verify:release-candidate` to the root scripts.
- Rebuilt the current Web static bundle with `corepack pnpm --filter @foresightnine/web build` and the Android debug QA artifact with Expo export plus the approved x86_64 Gradle command. The current APK was installed on API-36 emulator `emulator-5554`, cold-launched with Metro and `adb reverse tcp:8081` active, and its home screen/UI tree were captured as `docs/qa/android/phase9-current-home.png` and `phase9-current-home.xml`; `view_image` inspection passed. The filtered log tail contained no fatal/ReactNativeJS/RedBox error. This is explicitly Metro/debug QA evidence, not a no-Metro self-contained APK claim.
- API request-level mock smoke passed: `/health` and `/openapi.json` returned 200; strict invalid session input returned typed 422; valid session and challenge creation returned 201. The run used the deterministic mock provider and did not call GLM/OpenCode or licensed market data. No live GLM credential is present in the server environment, and no plaintext credential from task history was used or copied.
- Phase 9 candidate regression at that handoff passed: `corepack pnpm test` (FM 16, domain 9, asset registry 2, Web 49, Android 41, API 124); `corepack pnpm typecheck`; generated-contract, boundary, immutable-reference, 9WL fixture, release-candidate, secret-scan (494 Git-history blobs), and `git diff --check` verifications. The later post-Phase 9 production-preparation regression is current and records API 128. The API retains one documented Starlette/httpx deprecation warning; `corepack pnpm audit --prod` remains open for the documented moderate transitive `uuid` advisory.
- Release artifacts currently hash to Web `fm.worker-C7HxMD7b.js` `9C2BD44FEA8E04FB35CD42A0CB6A551881A5BBB9C3A42C699C319634E3EC2DA6`, Web `index-BX1xOnI2.js` `FA09335B45AACDF40C7BA2129DA8917D2E6AB606726C6BB5F70925C838AE9FB1`, Web CSS `8ABA60E0273BE2C84305EFBD42EC045AA31D72552D3371E8F66228D3FC5E2A1C`, Web HTML `3EED8CC8EC66DA61E4A315028D6F2F009FA31F5BC82A3573F925C05C3B31B916`, and Android APK `1460203312862C137207794F6659AF31F04E5062F3132956CFEAEA7890FDF1AB` (90,533,667 bytes). These are QA artifacts only, not production-signed/released artifacts.
- Final fresh read-only Sol release-candidate review returned `ship` with no new P0/P1/P2. It approved the Phase 9 QA commit/push and confirmed the Android evidence is accurately scoped to Metro + `adb reverse` + debug/dev-client execution. QA evidence was committed as `dd1a5fe93945f199b2dc55918c678d1311f3429c` and pushed to `origin/master`. This does not approve production release or complete the overall goal. External production gates remain separate: legal/privacy/terms/risk approval, data/FX redistribution licenses, new server-side provider secret, production Redis/CAPTCHA/quotas/observability/rollback, dependency remediation or approved exception, and Android owner-controlled signing/store release evidence.

## Phase 8 implementation and safety verification — 2026-08-29

- Implemented the Phase 8 public-safety boundary without changing `plan.md` or `agent.md`: bounded IP/session abuse controls, model-call quota reservation, trusted-proxy handling, production fail-closed CAPTCHA integration point, raw-body/content-type/size checks, CORS/security headers, idempotency-safe terminal operations, bounded retention cleanup, cascade deletion, and redacted telemetry. Multi-replica Redis/coordination remains an explicit deployment prerequisite; the local in-memory adapter is not claimed as production infrastructure.
- The first fresh Sol Phase 8 review returned `fix-first` with no P0/P2: it identified two current-code P1s. The request boundary now caps chunked bodies during receive (before body concatenation), and model-call subjects now include a shared rolling-24-hour `global:daily` bucket. A second fresh Sol review found two additional P1s; async model quota is now admitted before a 202 receipt, preserving typed 429/`Retry-After` semantics without creating an operation, and lifecycle retention uses a progressing runtime clock while deterministic provider timestamps remain fixed. The canonical ErrorResponse enum and regression coverage cover these paths.
- Added review-ready drafts for privacy, risk disclosure, public abuse controls, monitoring/incident response, and Phase 8 deployment operations. They explicitly separate local FM processing from server challenge processing, prohibit broker/order/custody behavior, and list market-data licensing, legal review, CAPTCHA operations, secret injection, Redis, signing, and the transitive `uuid` advisory as launch gates.
- Added shared freshness evaluation and integrated it into Web and Android defaults. Offline fixtures now have provider freshness `unavailable`; only an explicitly fresh provider snapshot may enable current Public Default amounts. Stale/invalid data renders `—` and retains the full capital as residual cash, while FM Raw remains visibly historical/simulated.
- Final verification passed: `corepack pnpm test` (FM 16, domain 9, Web 49, Android 41, API 124); focused Phase 8 security tests (14); `corepack pnpm typecheck`; `corepack pnpm verify:generated-contracts`; `corepack pnpm verify:boundaries`; `corepack pnpm audit:worldline-fixtures`; `corepack pnpm scan:secrets` (444 Git-history blobs); and `git diff --check`. A direct ASGI chunked-body probe returned 413 without `Content-Length`, and cross-session plus pre-receipt async quota tests returned typed 429. The API suite retains one Starlette/httpx deprecation warning. `corepack pnpm audit --prod` still reports the documented moderate `uuid` advisory (`GHSA-w5hq-g745-h8pq`), so it remains open.
- Browser/IAB verification completed the Web path from thesis/capital through challenge response, reservation confirmation, worldlines, local FM, Raw/Public Default, and allocation. The current offline snapshot shows Public Default amounts disabled (`—`, residual cash ¥100,000); no browser console error/warning was observed. Visual evidence: `docs/qa/web/phase8-public-default.png`, inspected with `view_image`.
- Android API-36 Google Play emulator `emulator-5554` installed and launched the fresh x86_64 debug APK. Expo Android export and `:app:assembleDebug -PreactNativeArchitectures=x86_64` passed under the bundled Temurin 17 JDK with the repository short-CMake helper. The installed app rendered the thesis screen, completed the mock challenge with a reservation, ran client FM, and showed the latest freshness/Public Default allocation page with disabled amounts (`—`) and residual-cash/risk disclosure; the unavailable/invalid mock response also displayed explicit recovery without fabricating a worldline or crashing. No fatal/ReactNativeJS/RedBox error appeared in the inspected log tail. Visual evidence: `docs/qa/android/phase8-home.png`, `docs/qa/android/phase8-provider-fail-closed.png`, and `docs/qa/android/phase8-public-default.png`, all inspected with `view_image`. APK: `apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk` (90,533,667 bytes; SHA-256 `1460203312862C137207794F6659AF31F04E5062F3132956CFEAEA7890FDF1AB`), QA-only and not a public release artifact.
- The latest fresh read-only Sol review returned `ship` with no current code-level P0/P1/P2 findings. This is a Phase 8 code gate only. No public deployment, live model/search request, licensed market-data claim, OAuth, billing, broker, execution, custody, or legal approval is claimed. The approved Phase 8 change set was committed as `73e9ee7` and pushed to `origin/master`; the requested pause follows that handoff, and Phase 9 remains pending.

## Phase 7 complete — Sol ship and repository handoff — 2026-08-29

- Final fresh read-only Sol review returned `ship` with no P0/P1/P2 findings. The review fingerprint matched before and after inspection: 340 files, manifest `0269c17a7d7f15e24532f004ad66a0cd32fd07f0e883536ad219f23d1798a319`, status `fe8b48b23f28ab4c614bc10b5d73bdfcf2aaabdd967341937ef0dce0f22e11b6`.
- Phase 7 now covers the full local research loop: bilingual Web and Android flows, challenge/evidence/worldline/portfolio gates, Web Worker and shared client-only FM calculation, raw versus Public Default outputs, local archive handling, search fallback, exports, and Async/reset invalidation. Web exports and Android restores fail closed for forged lifecycle, evidence, reviewability, vector, or future-date state.
- Final verification passed: `corepack pnpm test` (FM 16, domain 5, registry 2, Web 45, Android 38, API 110); `corepack pnpm typecheck`; `corepack pnpm verify:boundaries`; `corepack pnpm audit:worldline-fixtures`; `corepack pnpm scan:secrets` (301 Git-history blobs); and `git diff --check`. The API retains one third-party Starlette/httpx deprecation warning.
- Browser/IAB evidence: English Challenge rendered and completed; unconfirmed Worldlines/Allocation remained disabled; no console warning/error. Android API 36 Google Play emulator `emulator-5554` cold deep-link `foresightnine://challenge` showed the protected challenge screen with guard and start action, no retry/submit bypass, and no fatal/ReactNativeJS/RedBox error. QA artifacts are under `docs/qa/android/`.
- The current debug Android package is available at `apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk`; it is a local QA build, not a signed public release. No live GLM key was used or copied; provider remains mock/unconfigured unless a server environment injects a secret.
- `corepack pnpm audit --prod` still reports the documented moderate transitive `uuid` advisory (`GHSA-w5hq-g745-h8pq`) in the Expo/Xcode build toolchain. It remains a Phase 8 production gate. Phase 8 and Phase 9 have not started; no public deployment, OAuth, billing, broker, order, custody, licensed market-data release, or legal approval is claimed.

## Phase 7 gate — second fresh Sol fix-first findings — 2026-08-28

- A second fresh read-only Sol review was observably routed as `sol_advisor_sol_reviewer` / Luna high and completed with an unchanged before/after fingerprint (`333` files; manifest `BE41F461764D78252670F8E0CDDD315540028163CC6D47199F40FD158716A265`; status `9B5805FE66C368C26F5DD82FA0D62DEF2DB7C83E34FE4AC3183C9F5D5F9FCC88`). It returned `fix-first`.
- P1: Android restoration of a valid legacy privacy document without `researchState` spreads over the warm current state, allowing old challenge/evidence/FM/portfolio fields to remain. A warm-state reset regression is required.
- P1: Android `startChallenge` resets only challenge text/status and can retain evidence, branch, FM, portfolio mode and export state. New research must reset the complete downstream workflow.
- Sol re-verified Web 34/34, Android 22/22, both typechecks, 9WL audit, 301-blob secret scan and diff check; no P0 was found. No commit/push or Phase 8 work is authorized until these P1s are fixed and re-reviewed.

## Phase 7 P1 remediation — Web viewpoint reset and Android archive boundary — 2026-08-28

- Web `set_viewpoint` now preserves only locale, the new viewpoint, capital and currency; it resets stage, challenge transcript/status/reply/rubric/evidence/confirmation, branch selection, FM progress/status/result, portfolio mode, stress state and export notice. Regression coverage exercises both completed/confirmed and pending/partial prior workflows, then verifies a new challenge starts with only the new premise.
- Closed the same lifecycle boundary for an explicit `start_challenge` retry: even when the text is unchanged, the reducer now rebuilds a clean workflow before creating the two fresh challenge turns. A regression proves prior evidence, branch, FM and reply state cannot leak into a restarted challenge.
- Android archive restore now uses a dependency-free strict normalizer for the envelope, privacy document, nested research snapshot, evidence, rubric, branches/timelines, method data and JSON-safe FM result. It rejects malformed versions/shapes, arrays, enums, dates and numeric fields; maps serialized FM `null` values back to `NaN` only for fields that legitimately serialize FM `NaN`; and safely downgrades incomplete confirmation/lifecycle/downstream state to pending/empty rather than hydrating it.
- Closed the second Sol finding by centralising `mobileInitialState` and `resetMobileWorkflowState` in `apps/mobile/src/lifecycle.ts`. Android `startChallenge`, viewpoint changes and legacy input-only archive restore now reset the complete downstream workflow and rebuild the rubric in the active locale. Warm-state legacy restore and fresh-challenge tests cover the reset boundary.
- Reserved restore requires a completed challenge, explicit acknowledgement and a nonblank note; ordinary confirmed restore requires a completed challenge and evidence. Legacy v1 snapshots with missing lifecycle fields remain pending/false/empty. `restoreAppState` re-normalizes unknown input and returns the current state unchanged when rejected, so malformed archive input cannot throw during hydration.
- Focused verification passed: Web 34 Vitest tests and typecheck; Android 24 Vitest tests and typecheck; `git diff --check` passed with existing LF/CRLF normalization warnings. No FM implementation/formula, governance file, external read-only source, commit or push was changed.

## Phase 7 gate — fresh Sol fix-first findings — 2026-08-28

- A fresh, observably routed `sol_advisor_sol_reviewer` completed read-only review with an unchanged before/after fingerprint (`331` files; manifest `F88B86CFC2992DD02978303889CCA3F240DB3D21D3A19F8EB52939094D7EE187`; status `C4A266F7F93E6839FE3F94986AEA0F84EC68CFAE4A1DFA4E16E7B4608FDEFBAA`). It returned `fix-first`, not a Phase 7 pass.
- New P1: Web viewpoint reset still leaves stale challenge/evidence/FM/stage state in some paths; a changed thesis must never reuse downstream research state.
- New P1: Android archive restore accepts malformed snapshot fields/`fmResult` shapes and can throw; lifecycle consistency must be validated at the archive boundary and restore must fail closed without throwing.
- Existing gate evidence was explicitly re-verified by Sol: Android evidence/worldline route guards and empty-ledger behavior, client-only FM/no-secret boundary, bilingual challenge controls, and Web probability/date parity remain valid. No P0 was found. Terra remediation is required before another Sol gate; no commit/push or Phase 8 work is authorized.

## Phase 7 gate remediation and cross-platform confirmation QA — 2026-08-27

- Installing `jq` restored the prescribed native Sol runtime inspector. A fresh `sol_advisor_sol_reviewer` was then observably routed as Luna/high; exact before/after repository fingerprints matched, and its read-only `fix-first` verdict was accepted. The review identified six product-lifecycle and parity findings: branch-specific Web probabilities, occurrence/reveal separation, reserved-confirmation bypasses, Android mock-evidence injection, Web/Android horizon drift, and Chinese challenger copy in English mode.
- Closed all six findings. Web now renders the active branch assessment and first post-baseline reveal, uses the shared 2028-08-31 horizon, and requires a completed challenge plus explicit acknowledgement and nonempty reservation note. Android no longer injects evidence during archive/export, safely downgrades legacy archives missing lifecycle fields, applies the same reservation gate, blocks evidence/worldline deep links and `runFm` while confirmation is pending, and resets downstream state when the viewpoint changes.
- Browser/IAB QA exercised an English viewpoint through a completed challenge and reserved confirmation, confirmed no untranslated challenge controls, switched Main → Weak, and observed the active conditional range change to 20–35%, joint range to 7.00–17.15%, and first reveal to 2027-02-28. A final visual pass also removed the remaining English-date Chinese punctuation.
- Native API-36 emulator QA cold-started the current debug x86_64 build with an empty local state. Direct `foresightnine://evidence` access kept the worldline button disabled and the ledger empty; direct `foresightnine://worldline` displayed only the confirmation warning and route back to Challenge. The AndroidRuntime/ReactNativeJS error-filtered log was empty. Evidence: `docs/qa/android/phase7-evidence-gate.{png,xml}` and `docs/qa/android/phase7-worldline-gate.{png,xml}`; both screenshots were inspected with `view_image`.
- Full verification passed: FM 16, domain 5, asset registry 2, Web 31, Android 18 and API 110 tests; all workspace typechecks; governance boundaries; 9WL audit; and the current-tree/Git-history secret scan over 301 unique blobs. The API retains one third-party Starlette/httpx deprecation warning. A fresh independent Sol `ship` verdict is still required before Phase 7 commit/push or Phase 8.

## Phase 7 gate — fresh review route remains unobservable — 2026-08-27

- The Codex project registry now exposes a Git project whose authoritative path is `D:\Foresightnine`; this supersedes the earlier note that no matching project path was registered. The project label is stale (`ACFM_WL_paperwriting`), so the path—not the label—was used when checking scope.
- The Sol/Terra companion-profile check passed. A fresh read-only `sol_advisor_sol_reviewer` was assigned the complete Phase 7 gate packet, but the mandatory runtime inspector again returned `ERROR: rollout is missing, ambiguous, invalid, or inconsistent required routing metadata.` The reviewer was closed while still running, and no output or verdict was consumed.
- This remains a routing-observability failure rather than a code-quality finding. It cannot authorize a Phase 7 commit/push or Phase 8 work. A compliant independent verdict is still required.

## Phase 7 full regression after cross-platform parity coverage — 2026-08-27

- Root workspace typecheck passed. The full suite passed FM 16, domain 5, asset registry 2, Web 28, Android 14 and API 110 tests; the API retains one third-party Starlette/httpx deprecation warning.
- Governance-boundary verification and the 9WL cutoff/timeline audit passed. The current-tree plus Git-history secret scan passed after inspecting 301 unique history blobs.
- These checks verify the accumulated current worktree, including the new Web/Android FM parity regression. They do not resolve the already-documented Expo/Xcode transitive `uuid` production-audit gate, provide a live GLM call, or substitute for independent Sol acceptance.

## Phase 7 gate-support — explicit Web/Android FM parity regression — 2026-08-27

- Added an application-level parity regression in `apps/mobile/tests/mobile-domain.test.ts`. It imports the actual Web and Android FM input fixtures, asserts that the complete cutoff-safe input structures are equal, runs both through the shared client-only FM engine, and then compares selected factor IDs, combined signals, forecasts and raw target weights exactly.
- This closes the previously indirect Phase 7 acceptance evidence for “Android calculation results match Web”: future fixture or application-boundary drift now fails the Android suite instead of relying only on both apps importing the same package.
- Verification: `corepack pnpm --filter @foresightnine/mobile typecheck` passed; the mobile suite passed 14 tests; `git diff --check` passed with only existing LF/CRLF normalization warnings.
- No FM formula, fixture value, Public Default rule, plan gate or release status changed. Formal independent Sol acceptance remains required before Phase 7 commit/push or Phase 8 work.

## Repository state correction — 2026-08-27

- Read-only Git inspection confirms `origin` is configured as `https://github.com/seethelightluo/Foresightnine.git`; `master` and `origin/master` currently point to `7577176` (`feat: add Android research workflow pending native QA`). The older Phase 6 historical note saying that no remote was configured is superseded for the current checkout.
- The Phase 7 and later changes shown as modified/untracked remain local and uncommitted. No Phase 7 push is claimed because the formal Sol gate is still pending.

## Phase 7 gate-support — Android duplicate-evidence notice fix — 2026-08-27

- Corrected `apps/mobile/app/evidence.tsx` and `apps/mobile/src/app-store.tsx`: the UI no longer reads a result from inside React's asynchronous state updater. Duplicate evidence is determined from the current rendered ledger before dispatch, while the store keeps the duplicate write guard. A duplicate project-search selection now preserves the duplicate notice instead of being overwritten as a successful selection.
- Added repository-local ignore rules for generated `.build-cache`, `.playwright-cli`, Gradle/Kotlin and Android build output. Native Android source remains visible to Git; generated QA/build output no longer pollutes the Phase 7 change set.
- Verification: `corepack pnpm --filter @foresightnine/mobile typecheck` passed; the focused mobile suite passed 13 tests; `git diff --check` passed with only existing LF/CRLF normalization warnings.
- This is a Phase 7 technical correction only. It does not change the evidence hash/duplicate rule, client-only search boundary, formal Sol gate, phase ordering, or release status.

## Phase 7 gate — native Sol routing reattempt still fail-closed — 2026-08-27

- The exact companion-profile check passed again, and the host exposed both required native role names. A fresh `sol_advisor_sol_reviewer` was given a complete read-only Phase 7 packet and was closed without consuming its response.
- The prescribed runtime inspector again returned `ERROR: rollout is missing, ambiguous, invalid, or inconsistent required routing metadata.` Because selected role/model/effort and reviewer isolation are not observable, this attempt supplies no valid Sol verdict and cannot authorize a phase transition.
- The Codex app project registry still does not contain `D:\Foresightnine`, so the contract-compliant user-visible Luna task lane cannot be opened for this repository either. No fallback model or unverified review was substituted.
- Current primary verification remains green and the implementation evidence is unchanged. Phase 7 remains technically evidence-ready but formally pending; Phase 8/9, commit/push, live provider testing and public deployment remain unauthorized.

## Phase 7 primary verification pause — 2026-08-27

- Primary Sol reran the Phase 7 verification set on the current checkout: `corepack pnpm typecheck`, `corepack pnpm test` (FM 16, domain 5, registry 2, Web 28, Android 13, API 110), `corepack pnpm verify:boundaries`, `corepack pnpm audit:worldline-fixtures`, `git diff --check`, `corepack pnpm scan:secrets` (301 Git-history blobs), and Android production Hermes export/debug build evidence; all required checks passed. The API suite retains one third-party Starlette/httpx deprecation warning.
- `corepack pnpm audit --prod` still reports only the documented moderate Expo/Xcode transitive `uuid` advisory (`GHSA-w5hq-g745-h8pq`); it remains a release gate and is not silently treated as resolved. No live GLM request was made because no server-injected secret was available.
- The final Android project-search fallback screenshot and UI-tree evidence were inspected again. It shows a bounded query, explicit unconfigured-API fallback, no-auto-fetch disclosure and an unobscured local `SearchEvidenceBundle` action; the preceding Metro refactor removed the warning overlay and preserved the FM public surface.
- Formal Phase 7 acceptance is **not granted**. The native Sol reviewer routing remains invalid/unobservable, and the Codex app project registry currently has no `D:\Foresightnine` Git project, so the contract-compliant Luna review lane cannot be created for this repository. This is an environment/routing blocker, not a code-quality verdict. No Phase 8/9 work, commit/push, public deployment, production OAuth/billing, live provider call or release claim is authorized by this entry.

## Phase 7 gate-support — Android project search and Metro warning remediation — 2026-08-27

- Replaced the former Android-only `API search unavailable` placeholder with a bounded project-search client. It reads only the deployer-supplied `EXPO_PUBLIC_API_BASE_URL`, requires a public HTTPS API base, calls only `GET /v1/search/candidates`, sends no credentials, and never opens or fetches a returned source URL. Malformed responses, unconfigured API, non-2xx responses and transport failures fail closed into the existing browser-share/paste `SearchEvidenceBundle` fallback.
- A person must select a displayed candidate before the client creates a local C-grade contextual bundle. The bundle retains `search_provider` and `search_result_hash`, is labelled unverified, and cannot silently become a public fact. Android deliberately does not connect to GLM MCP or carry a model/search secret; project API search is the feasible mobile path, while Share Intent/paste remains the offline fallback.
- Removed the two Android Metro `Require cycle` warnings without changing FM formulas or the public FM package surface: the public export barrel is now separate from a dependency-free core module, and AST/orchestration import that core directly. This removes the debug warning overlay that previously obscured lower-screen controls.
- A new Metro process cold-started the Android app after that refactor and emitted only its normal bundle record (no `Require cycle` warning); the final UI tree had no `Open debugger to view warnings` overlay. This separates the passing evidence from the old Metro session's buffered pre-refactor warning messages.
- Native API-36 Google Play emulator QA: built and installed the current debug x86_64 APK, opened `foresightnine://evidence`, used UI-tree-derived field/action coordinates, entered `grid`, dismissed the IME, invoked project search with no configured public API, and confirmed the explicit fallback plus no-auto-fetch disclosure. The native process had no `FATAL EXCEPTION`, `Unable to load script`, TypeError or ReferenceError. Evidence: `docs/qa/android/phase7-project-search-query.xml`, `docs/qa/android/phase7-project-search-fallback.xml`, and `docs/qa/android/phase7-project-search-fallback.png`; the final screenshot was inspected with `view_image` after the warning-removal fix.
- Verification: root workspace typecheck; full test suite (FM 16, domain 5, registry 2, Web 28, Android 13, API 110); Android production Hermes export; debug APK build; governance-boundary/9WL audits; `git diff --check`; and the current-tree/Git-history secret scan all passed. The production dependency audit still reports the documented Expo/Xcode transitive moderate `uuid` advisory (`GHSA-w5hq-g745-h8pq`), so it remains a release gate. No GLM key was supplied through a server environment, so no live provider/search request is claimed.
- Formal Phase 7 acceptance is still blocked by the independent reviewer routing-metadata failure recorded on 2026-08-26. This entry does not authorize Phase 8/9, release/Play distribution, a phase commit/push, production OAuth/billing or public deployment.

## Phase 7 gate — independent reviewer routing rechecked, still fail-closed — 2026-08-26

- Re-read the approved `D:\Foresightnine\plan.md`, `D:\Foresightnine\agent.md`, and the Sol orchestration contracts before a new gate attempt. The non-mutating companion-profile check passed and the host exposed the exact `sol_advisor_sol_reviewer` role.
- Spawned a fresh reviewer with a read-only final-review packet covering the accumulated Phase 7 Android/shared-client diff, current test evidence, no-secret boundary, lifecycle/share behavior, and explicit non-production limitations. Before accepting any verdict, the prescribed runtime inspector returned: `ERROR: rollout is missing, ambiguous, invalid, or inconsistent required routing metadata.` The reviewer was immediately shut down and no verdict was consumed.
- This is a routing-observability failure, not a code-quality verdict. Per the fail-closed role contract, it cannot be represented as a Sol pass, cannot authorize Phase 8/9, and cannot authorize the requested phase commit/push. The Phase 7 technical evidence remains intact; the formal acceptance gate remains pending a host that exposes consistent role/model/effort metadata.

## Phase 7 gate-support — Console, optional account boundary and responsive native QA — 2026-08-24

- Replaced the visible Web and Android **Methods** navigation entry with **Console**. The original methodology, evidence-grade, probability, FM Raw/Public Default and risk material remains accessible beneath the optional account status rather than being removed.
- Console exposes only truthful states: Google/GitHub OAuth is disabled until a future server readiness/PKCE start action exists; GLM Coding is documented as server-environment-only; the OpenAI-compatible personal-provider surface explicitly reserves a future encrypted server credential-vault design and has no BYOK/API-key field; Google Play billing is only a disabled planning surface.
- Web verification: TypeScript typecheck; 28 Vitest tests; local Playwright desktop and 390×844 mobile interaction/screenshots; `view_image` inspection. The IAB browser-control runtime failed to load its own module, so the prescribed local Playwright fallback was used. The final local browser console contained **0 errors, 0 warnings** (one React DevTools informational message only).
- Android verification: TypeScript typecheck; 11 focused Vitest tests; native emulator deep-link route `foresightnine://console`; `view_image` inspection of phone (`phase7-console-phone.png`) and a temporary 1200dp-wide tablet profile (`phase7-console-tablet.png`). Phone controls stack; tablet control bounds prove Google `[37,373]–[596,421]` and GitHub `[604,373]–[1163,421]` are side-by-side. Both are disabled and contain no secret.
- Final regression: `expo export --platform android` produced the Android Hermes bundle; `:app:assembleDebug` succeeded for x86_64 under Temurin 17/API 36; workspace typecheck and tests passed (FM 16, domain 5, registry 2, Web 28, Android 11, API 110); governance-boundary and 9WL fixture audits passed; `git diff --check` passed; secret scan passed for the current tree and 301 unique Git-history blobs. The only production dependency audit result is the pre-existing moderate `uuid` advisory through Expo/Xcode tooling (`GHSA-w5hq-g745-h8pq`); it remains a release gate.
- Corrected the release-evidence record in `docs/plan-deviations.md`: current release signing is fail-closed and no release/Play artifact is claimed from this pass. The current debug native build remains emulator QA only.
- Formal Phase 7 acceptance remains **blocked**: this runtime cannot provide valid, observable routing metadata for the required independent Sol reviewer. No Phase 8/9 start, commit, push, production OAuth, live GLM request, user key storage, billing operation or public deployment is claimed.

## Phase 8 gate-support — refreshed production dependency audit — 2026-08-24

- `corepack pnpm audit --prod` now reports one moderate advisory, `GHSA-w5hq-g745-h8pq`, via `xcode@3.0.1 > uuid@7.0.3` in the Expo configuration/build toolchain. The former high-severity `image-size` findings are no longer present in the current resolved graph.
- The issue is not silently overridden: local dependency tracing confirms it is a build-time Xcode-project manipulation path, but the public-release audit is still not clear. The evidence, containment boundary and supported remediation order are in `docs/security/dependency-audit.md`.
- This is Phase 8 support evidence only. Phase 8 has **not** started or passed because the required Phase 7 Sol gate remains pending.

## Phase 7 gate-support — fresh Android back and lifecycle regression — 2026-08-24

- On the already-installed QA release running on Google Play API 36 `emulator-5554`, opened the English Challenge route from a persisted local Thesis, then issued Android system Back (`KEYCODE_BACK`). The native UI tree returned to the Thesis screen with the exact persisted viewpoint and `1,000,000` CNY capital still present.
- Forced `com.foresightnine.mobile` to stop, relaunched it with `adb shell monkey -p`, and re-inspected the native tree. The application again restored the local Thesis and capital, and the inspected logcat tail contained no application `FATAL EXCEPTION` or `AndroidRuntime` crash record. This supplements, rather than replaces, the existing full-loop Phase 7 device evidence.
- Rebuilt the current Android release from this worktree with the per-process Android Studio JBR/SDK variables and `scripts/android-short-cmake.init.gradle`; `assembleRelease` completed successfully in 33 seconds. The rebuilt APK is SHA-256 `13EA89383B4F2706F3B8B735A3933BEECA9D308E8272C4A9E3AA1F020FDB7D1E`, installed successfully with `adb install -r`, launched on `emulator-5554`, and exposed the persisted Chinese Thesis screen in the native UI tree. A second 15-second build with explicit `NODE_ENV=production` also passed and removed the earlier missing-NODE_ENV notice. The remaining SDK-XML compatibility and Gradle-10 deprecation notices are release-hardening items, not hidden build failures.
- This is emulator-only technical QA. The Phase remains **not formally accepted** because the mandated native Sol reviewer routing is still invalid/unobservable; it does not authorize Phase 8, a release claim, commit, or push.

## Phase 7 gate-support — truthful optional account affordances and refreshed device QA — 2026-08-24

- Added matching Google/GitHub account-status components to the approved Web and Android thesis flows. Both intentionally render an **anonymous research** state and disabled provider controls until a future server-readiness endpoint has verified OAuth configuration and supplied a PKCE start action. No OAuth client ID, redirect, secret, token exchange, session or simulated successful login is bundled or claimed.
- The Web component is visible in the desktop research rail and, at narrow breakpoints, relocates to the end of the thesis flow instead of disappearing with the rail. Browser/IAB QA covered desktop and 390×844 views, Chinese ↔ English language switching, page identity/non-blank DOM/no framework overlay, disabled Google control and empty `error`/`warn` console collections. A first visual pass found excessive English rail wrapping; the label was shortened and rechecked before acceptance as implementation QA.
- Rebuilt the Android release after the UI change. The first direct Gradle invocation demonstrated missing per-process JBR/SDK paths; a retry with the already-installed Android Studio JBR/SDK and the repository's Windows-only short-CMake init helper completed successfully. The helper only relocates generated CMake staging output and does not modify Android product/release configuration. The QA-only APK SHA-256 is `13EA89383B4F2706F3B8B735A3933BEECA9D308E8272C4A9E3AA1F020FDB7D1E`.
- Installed the rebuilt release on `emulator-5554` (Google Play API 36), resolved and launched `com.foresightnine.mobile/.MainActivity`, inspected its native UI tree and a release screenshot, then switched the UI to English. The tree confirmed `Anonymous research`, `Sign in with Google — Not configured` and `Sign in with GitHub — Not configured`, with both provider buttons `enabled=false`; the Android crash buffer was empty. This is local emulator evidence only, not Play/distribution evidence.
- Regression evidence: full `pnpm test` passes shared FM 16, Web 25 and mobile 9 tests; API pytest passes 108 tests with one existing Starlette deprecation warning. Workspace typecheck, Git-history/current-tree secret scan (301 blobs), boundary verification, 9WL fixture audit, real cutoff-safe WL panel FM audit and `git diff --check` pass.
- Phase 7 remains **not formally accepted**. Native Sol reviewer routing is still invalid/unobservable in this session, so this evidence does not constitute a Sol pass, does not authorize Phase 8 and does not authorize a commit/push.

## Phase 7 gate-support — GLM Coding MCP project-search boundary — 2026-08-24

- Added a server-only, fixed-host GLM Coding Plan MCP search candidate adapter. It is opt-in via `FORESIGHTNINE_SEARCH_PROVIDER=glm_coding_mcp`, retains client `SearchEvidenceBundle` fallback by default, carries Streamable HTTP MCP sessions, validates the advertised `webSearchPrime` schema and fails closed on unsafe URLs, protocol errors, missing server secrets and unparseable results.
- Search output is explicitly unverified `SearchCandidate` data. It is neither fetched nor passed into an LLM prompt; the existing canonical client-evidence submission path must retain, hash and grade a user-selected source before it can affect a Challenge.
- No live call was made: the server environment has no injected GLM secret. This change does not advance Phase 8 or replace the fresh Sol Phase 7 verdict.
- Added deterministic protocol/security tests for initialization, notification, session propagation, fixed endpoint, bounded query, absent secret, unexpected tool schema and private-result URL rejection. Configuration and operating limits are documented in `docs/security/glm-coding-mcp-search.md`.

## Phase 7 gate-support evidence — bilingual Web, GLM boundary, FM/9WL audit; pause requested — 2026-08-24

- Kept the approved plan sequence intact. The formal Phase 7 Sol acceptance remains unavailable because this session's native reviewer-routing metadata is invalid; no Phase 8/9 claim or GitHub push is made.
- Added the server-only `glm_coding` OpenAI-compatible provider preset. It pins the GLM Coding endpoint and host allowlist, accepts only the server environment secret, defaults to the requested model name, prevents redirect through generic provider variables, and fails closed when no secret exists. Its configuration guide explicitly forbids client-side/BYOK secret capture and requires a separate one-request, redacted live acceptance when a valid injected secret is available.
- Added a cutoff-safe real-panel FM audit against `D:\WL-data-final\panels\WL1_full.parquet` and a separate 9WL fixture audit. The FM audit consumed 20 historical assets through the actual shared client engine with no server allocation and no future rows. The 9WL audit verifies source SHA-256s, trigger text, stage-news reveal dates, baseline/horizon ordering, explicit leak/observed semantics, branch partition and joint-probability arithmetic for WL1–WL9. Neither audit is a forecast, return claim or public-release market-data assertion.
- Completed Web Chinese/English product-copy coverage, including persisted locale choice, translated worldline labels/nodes, form validation/a11y labels and a fixture-wrapper translator that keeps user-entered thesis text verbatim. A hydration guard prevents the initial local-draft write from overwriting a restored language preference.
- Browser/IAB QA found and fixed a real 208px side-rail defect: the language button visually overflowed into the main sheet and was not clickable. It now occupies its own in-rail row. Desktop and 390×844 mobile checks prove Chinese ⇄ English switching, restored locale persistence, non-blank page identity, translated worldline view, responsive navigation and empty browser `error`/`warn` collections. The current visual result remains aligned with the approved warm-paper/editorial concept; user input remains in its authored language by design.
- Focused verification passed: Web `typecheck` and 24 Vitest tests; GLM-provider tests (11); Phase 3 provider tests (25); shared FM tests (16); WL1 warm-up FM audit; 9WL timeline/probability audit; secret scan. Full workspace verification and a formal Sol pass remain the next gate before any commit/push.

## Phase 7 Android — technical device QA complete; formal Sol gate pending — 2026-08-24

- This entry supersedes the earlier **native-build-blocked** statements below. A reproducible Expo SDK 57 native build is now available on Windows after pinning Expo's compatible `react-native-reanimated@4.5.1` / `react-native-worklets@0.10.1`, adding `react-native-safe-area-context@~5.7.0`, and moving the pnpm virtual store to repository-relative `nm` to stay below the Windows CMake path limit.
- The Windows-only Gradle init helper places application and linked-module CMake staging output under a short external path. It changes no product/release configuration. The generated Android directory remains inside `apps/mobile/android`.
- Fixed two device-discovered P1 defects: Android gesture navigation could obscure the Challenge submit button (the shared screen now uses `react-native-safe-area-context`); and a JSON base64 archive envelope could not be restored through the Android AES bridge. Restore now decodes the envelope's base64 ciphertext to bytes before `AESSealedData.fromCombined`. Dev-only failure diagnostics disclose only a rejection stage, never keys, ciphertext, documents or caught errors.
- Emulator evidence on `emulator-5554` (Google Play API 36): thesis → mock challenge → local evidence → main/weak/strong worldline → client FM → FM Raw/Public Default switch → encrypted local archive → cold-start restore → public Android share sheet. The release APK additionally accepted `ACTION_SEND text/plain` and routed the text into the local evidence workspace without a network request.
- Visual/device artifacts are under `docs/qa/android/`: `phase7-release-home.png`, `phase7-challenge.png`, `phase7-worldline.png`, `phase7-portfolio.png`, `phase7-archive-restored-fixed.png`, their UI-tree XML companions, and `phase7-release-share-import.xml`. `view_image` inspected the release thesis, challenge, portfolio and restored archive states against the approved quiet/warm-paper concept direction.
- Builds: `:app:assembleDebug` and `:app:assembleRelease` both pass with Temurin 17, Android SDK 36, NDK 27.1.12297006 and CMake 3.22.1. The self-contained release APK is `apps/mobile/android/app/build/outputs/apk/release/app-release.apk` (97,484,137 bytes; SHA-256 `C36B9699BD2FDAA861BDC2CB343C87F676DEAA6A728A668F4E91ECD8A40F6EE1` at QA time). It was installed after Metro was stopped and `adb reverse --remove tcp:8081`; it launched and restored the encrypted archive with no `Unable to load script` or fatal Android log in the inspected tail.
- Verification: mobile `typecheck` and 9 focused Vitest tests pass; Android native archive save/restore and public share dispatch pass in the emulator; release launch and inbound share intent pass; root `pnpm test` passes 99 tests (one existing Starlette deprecation warning), `verify:boundaries`, Git-history/current-tree secret scan and `git diff --check` pass. The release artifact is **QA-only** because the generated project currently uses the Android debug keystore; it is not a Play/public-distribution artifact.
- Phase 7 is technically evidence-ready but **not formally accepted**: native Sol routing metadata is invalid in this session, so the mandated independent Sol reviewer cannot issue the required gate verdict. Phase 8 does not begin.

## Phase 7 Android — Sol fix-first remediation and blocked native gate — 2026-08-24

- Replaced the mobile-only branch fixture's incompatible probability values with one shared P(premise) range, a documented constrained weak/main/strong conditional simplex (20% / 55% / 25%) and independently recomputed joint ranges. Regression coverage now validates the three branches in both Chinese and English rather than accepting visually plausible but inconsistent numbers.
- Extended the mobile worldline fixture and UI with a data-as-of date, baseline/horizon, explicit `historical_reference_and_simulated` label, occurrence/reveal times, observable indicators, causal parents, asset anchors and source references. The timeline validator rejects future data, unordered/reveal-invalid nodes, and incomplete provenance.
- Replaced the portfolio's static NAV decoration with selected-path typed historical/simulated NAV points, start-boundary scenario return, running-peak drawdown and numeric maximum drawdown. The portfolio now carries exact raw/public market value, lot/FX cash remainder and units; the rounding remainder is transferred to the cash-equivalent row rather than discarded.
- Public Default now retains Raw independently while applying a 25% security cap, 35% aggregate high-volatility cap, stale/low-liquidity exclusion, explicit proxy mapping and per-row explanations. Cash is intentionally not treated as a security cap; a mode is flagged if it cannot be executed rather than being presented as an order.
- Closed the bilingual evidence/archive accessibility leakage found by Sol: Back/language labels, share metadata and privacy-safe export disclaimers now use the active locale. A raw Android shared URL is classified as user-supplied/unverified, is not independently reviewable, is never fetched, and cannot support a public fact.
- Verification after remediation: mobile TypeScript typecheck; 8 focused Vitest tests; Android Metro production export; root secret scan; `git diff --check`. No live LLM, broker, order, execution, custody, secret, or market-data call was made.
- Android Studio, API 36 SDK/platform tools/emulator and a Google Play API 36 AVD are installed and the AVD boots. The actual release Gradle build is still blocked before configuration because the official Gradle 9.3.1 distribution cannot be connected from this host, even after the generated wrapper timeout was raised from 10 s to 60 s. Therefore APK install, Share Intent, back/lifecycle, SecureStore/FileSystem native archive round-trip and the six required screenshot/view-image comparisons remain **unproven**; Phase 7 remains **not accepted**.

## Phase 7 Android — host implementation complete; native acceptance blocked — 2026-08-23

- Added `apps/mobile`, an Expo SDK 57 / Expo Router Android client that consumes the shared FM engine and design tokens. The implemented local-only loop covers bilingual draft input, visible counterargument/rubric/reservation states, client SearchEvidenceBundle import with an explicit API-search-unavailable state, weak/main/strong probability timelines, local `computeFm`, distinct FM Raw/Public Default allocations, risk/NAV summaries and privacy-aware export.
- Android text/URL receiving uses Expo's official `expo-sharing` config plugin and `app/+native-intent.ts`. Shared text is normalized locally and never fetched. The app remains mock-only: no GLM/OpenCode request, API key, market fetch, broker, order, execution or custody integration was added.
- Implemented a native archive adapter: AES-256-GCM from `expo-crypto`, its device-local key stored in `expo-secure-store`, and the sealed envelope in `expo-file-system`. Private evidence is excluded by default; opt-in is device-local only and public-safe export rejects it.
- Host verification passed: mobile typecheck; 6 focused mobile Vitest checks (validation, URL normalization/duplicates, i18n, worldline intervals, shared FM/Public Default, privacy envelope); Android Metro production export; root workspace typecheck/tests; secret scan; `git diff --check`. `expo config --type public` confirms SDK 57 and the text/plain/text/uri-list share configuration.
- `pnpm audit --prod` was also run. It reports two high-severity `image-size@1.2.1` parser DoS advisories via Expo SDK 57's Metro/React Native toolchain and one moderate `uuid@7.0.3` bounds-check advisory via Expo config tooling. Both are transitive build-tool dependencies, but they are release gates: no unreviewed major-version override was applied, and a compatible Expo/React-Native toolchain update or a reviewed vendor remediation is required before public release.
- Fresh Sol review returned **fix-first** with no P0: actual APK/native build, Android emulator/device flow, Share Intent dispatch, back/lifecycle behavior, AES/SecureStore/FileSystem round-trip, and six `view_image` screenshot comparisons are not proven. The host has no Java, Android SDK, ADB, emulator or Android Studio. The Android rows in `docs/design/fidelity-qa.md` therefore remain explicitly pending; Phase 7 is **not accepted** and Phase 8 must not start.

## Phase 6 Web visual gate — Sol accepted — 2026-08-23

- Closed the remaining visual-state gap from fresh Sol review: `fm_complete` now keeps the user on the completed FM audit page until “查看配置方案” is selected. This makes the factor table, FM-v1 formula note, Web Worker boundary and JSON-intermediate notice reachable in the actual flow.
- Added and inspected `apps/web/qa/screenshots/web-11-fm-complete-fixed.png` and `web-12-methods-fixed.png`; refreshed the portfolio desktop/mobile evidence as `web-09-portfolio-desktop-fixed.png` and `web-07-portfolio-mobile-p1.png`.
- Completed the single canonical Web comparison log in `docs/design/fidelity-qa.md`, including concept path, implementation evidence, viewport, mismatch/correction and verification result. Android rows remain pending for Phase 7.
- Verification after the FM visibility correction: Web 23 tests passed, typecheck passed, Vite build passed. No new provider, key, server-side FM, broker, order or custody path was introduced.

### Fresh Sol acceptance

- Verdict: **ship**; no P0/P1. Phase 6 Web is accepted. The reviewer verified the FM audit page is reachable, the refreshed 390×844 Public Default screenshot is semantically aligned, the canonical Web comparison log is complete, and the Browser/IAB console collections are empty.
- Accepted commits: `ac598a0` (Web workflow) and `84a02f2` (visual evidence gate correction). The repository has no configured remote, so no GitHub push is claimed.
- Remaining scope is explicitly deferred: Android implementation/QA, live OpenCode/GLM provider verification, production API/PostgreSQL wiring, dependency/security/licensing gates, and final Phase 9 release acceptance.

## Phase 6 Web P1 remediation and browser QA — 2026-08-23

- Closed the three functional findings from the Sol freeze review: branch conditional probabilities are exhaustive (`0.27 + 0.54 + 0.19 = 1.00`) and each reveal schedule is derived from the generated node IDs/dates; `allocationRows(null, ...)` is empty and JSON export reports `not_computed` without fabricated weights; D/E SearchEvidenceBundle entries may omit URL/publication date while A–C still require a safe HTTP(S) URL.
- Kept the full Web audit `evidenceHash` separate from the API-compatible `sourceMaterialHash`. The latter was checked against the Python API canonical hash: `cd3d44755bda8101435b8d21841f2297c1a14bc9ce1427a71b3a60269869f317`.
- Corrected the Raw/Public mode selector so its selected glyph, `aria-pressed`, panel copy and table caption stay aligned.
- Independent verification: Web 22 tests passed, typecheck passed, Vite build passed; API 71 tests passed with one unchanged Starlette TestClient deprecation warning; `git diff --check` and secret scan passed.
- Browser/IAB evidence now covers viewpoint → challenge → no-URL D evidence → reserved confirmation → three worldlines/probability ranges → client FM Worker → Raw/Public Default → stress selection → NAV/drawdown → methods/risk. Browser console `error`/`warn` collections were empty. New inspected screenshots: `apps/web/qa/screenshots/web-07-portfolio-mobile-p1.png` and `web-09-portfolio-desktop-fixed.png`.
- `view_image` inspection found the implementation visually consistent with the approved warm-paper/editorial concept for the inspected portfolio state at desktop and 390×844 mobile; the complete Web/Android concept matrix and fresh Sol verdict remain pending. Web uses deterministic mock/fixture mode; no live GLM/OpenCode provider claim is made.

## Phase 6 Web visual/interaction QA follow-up — 2026-08-23

- Repaired the reviewer-found medium-width collision: at approximately 1265px the research inspector is now hidden before the three-column probability cards can be clipped. The 1536px desktop inspector layout remains available.
- Browser/IAB smoke now covers the full client path with current viewpoint propagation, fail-closed 25/40 challenge state, 31/40 plus independently reviewable evidence normal confirmation, worldline premise/three branches, client FM completion, USD formatting, Raw/Public Default, stress selection, NAV interval and drawdown toggle. Fresh console `error`/`warn` collections were empty.
- Added and visually inspected current Web screenshots: `apps/web/qa/screenshots/web-01-thesis-current.jpg`, `web-02-challenge-current.jpg`, `web-03-worldline-current.jpg`, `web-05-portfolio-current.jpg`, and `web-06-nav-current.jpg`. The current IAB viewport is approximately 1265×709 rather than the 1536×1024 concept artboard; screenshots are therefore evidence for implementation QA, not a final pixel-perfect Sol pass.
- Web verification after this follow-up: 18 Vitest tests passed, typecheck passed, Vite production build passed, and `git diff --check` passed. No GLM/OpenCode key was read, stored, or used; the Web path remains deterministic mock/fixture mode.
- Phase remains **in progress**: Android, bilingual shared i18n, live OpenAI-compatible GLM provider boundary, API/PostgreSQL production wiring, security/dependency/licensing gates, and final Sol acceptance are not complete.

### Sol freeze review — 2026-08-23

- Verdict: **fix-first / frozen pause**, with no P0. This is not a Web pass or release-candidate claim.
- P1 blockers retained for the next continuation: `makeBranches()` conditional probabilities currently sum to `0.99` and its reveal schedule contains node IDs not present in the generated branch graph; `allocationRows(null, ...)` shows deterministic fallback allocations while the no-FM JSON state says no weights exist; and the Web evidence form currently requires a URL, so it cannot represent the approved no-URL D/unreviewable path. Web canonical-hash parity with the API is also not independently demonstrated.
- Formal concept comparison remains pending: current Web screenshot evidence is implementation QA at approximately 1265×709, with FM and Methods states not yet captured; Android has not entered implementation QA.
- This is the requested pause point. Do not advance to Android/API production work until the next continuation explicitly resolves these P1 items and obtains a fresh Sol review.

## Phase 6 Web P1 remediation — 2026-08-23

- Dynamic viewpoint input is consumed by challenge start, user reply remains editable and is retained in the transcript/export, and the worldline premise follows the current viewpoint. SearchEvidenceBundle fields remain explicit (`url`, `title`, `publisher`, `published_at`, `excerpt`, `user_note`, direction and grade); content hashes are retained. Evidence classification records D/E and `user_private`/`model_inference` as `independently_reviewable: false`; A/B/C can be independently reviewable only when a URL is present.
- Public Default preserves FM Raw and applies the 25% single-security cap, 35% high-volatility-class cap, long-only/fully-invested eligibility, historical/stale and low-liquidity exclusion, proxy mapping with reasons, deterministic FX snapshot, and lot-aware Raw/Public amounts, units and residual cash. These are deterministic fixtures; no live provider or API key is configured.
- Structured JSON/CSV export retains the thesis, challenge transcript, evidence audit fields, three worldline premises/anchors/reveals/invalidation data, local FM arrays/results when available, Raw/Public allocation fields and reasons, method/data labels, risk disclosures, and complete NAV historical/weak/main/strong/interval/drawdown arrays. The NAV drawdown control has a real toggle handler.
- Verification evidence: Web has 16 passing Vitest tests; direct typecheck, Vite build and `git diff --check` passed. A fresh Browser/IAB session DOM check recorded empty `error`/`warn` evidence. The implementation remains deterministic fixture mode, with no live provider/API key.
- Fidelity limitation: the screenshot command is unsupported in the current IAB session, so no screenshot artifact was generated or inspected. Fidelity comparison rows remain **pending**; this entry is not a Sol pass.

## Phase 6 Web implementation — 2026-08-23

- Added the strict React + Vite Web MVP under `apps/web`, consuming the shared contracts, domain/design tokens, and FM engine. The local workflow covers draft restore and validation, challenge retry/cancel/recovery, SearchEvidenceBundle fixture import, worldline probability ranges and selection, client-side FM computation through a real Web Worker, FM Raw/Public Default deltas, lot-aware allocation/residual states, historical/simulation NAV and drawdown SVG charts, methods/risk disclosure, and JSON/CSV export.
- Added deterministic Web tests for thesis validation, challenge state recovery, worker/FM output retention, Raw/Public capping, small-capital residuals, and exports. Focused Web typecheck/build/test and diff checks pass; screenshot-based fidelity evidence remains pending because no screenshot artifact was committed.
- Material judgment: the default mode is deterministic offline fixture data with explicit historical/simulated/model-estimate labels; no broker, order, custody, live-network, or secret-bearing affordance was added.

## Phase 4 Sol gate remediation — 2026-08-15

- Closed synchronous unknown-provider failures in `OperationManager.execute`: ordinary exceptions now append the existing safe `operation.error`, transition to `failed`, retain the idempotent terminal error, release single-flight waiters, and remove pending execution state without exposing exception text.
- Bound accepted typed worldlines to the retained thesis and request: every path must retain the thesis premise, fixed API baseline date, requested horizon, bundle/challenge identity, and the existing probability partition validation before any worldline is persisted.
- Tightened worldline graph semantics in both API and domain validation: causal parents must exactly match incoming edges; schedules are non-empty, cover every node exactly once, use each node's event date, and retain the documented early-reveal `leak` exception only.
- Replaced the 9WL index with an auditable deterministic regression fixture. Each record carries source paths/SHA-256 values, copied title/trigger and stage-news reveal date, normalized event-date rationale, and complete synthetic weak/main/strong test branches. The fixture explicitly disclaims forecasts and does not present its probabilities as source estimates.

## Phase 4 provider-boundary gate reinforcement — 2026-08-15

- Revalidated every typed `WorldlineBundle` at `POST /v1/worldlines` before persistence: canonical schema, request challenge identity, bundle/worldline identity, worldline time/graph/reveal constraints, and weak/main/strong conditional partition plus joint-probability semantics.
- Revalidated typed `ProbabilityAssessment` results at `POST /v1/probabilities/assess` with the canonical schema, requested-worldline identity, and joint-probability semantics. Boundary failures now use the existing safe failed-operation terminal path without returning provider diagnostics.
- Added fake typed-output regressions for challenge/bundle identity, nested asset-impact schema, reveal-horizon, conditional partition, wrong-worldline and invalid-joint probability assessments; rejected bundles persist no worldlines.

## Phase 4 Worldlines and Probability — 2026-08-15

### Delivered

- Added provider-neutral `WorldlineGenerator` and `ProbabilityAssessor` interfaces, with deterministic no-key mock defaults. Worldline generation now derives its premise from the stored thesis belonging to a `CONFIRMED` challenge; the API rejects caller-supplied premise/path fields.
- Canonical worldline contracts now include node event/data-as-of dates, embedded asset impacts, and three attached probability assessments for weak/main/strong paths. Contracts, generated TypeScript, and OpenAPI are regenerated together.
- Enforced schema-plus-semantic gates for acyclic graph shape, edge-time monotonicity, baseline/horizon containment, no future data references, event/reveal rules, exact interval/scalar joint recomputation, shared premise probabilities, and conditional branch partition consistency.
- Added one 9WL regression fixture containing nine reference-derived scenario records, each with weak/main/strong expected structure, probability decomposition and event/reveal schedule expectations; added API/domain coverage including unknown-provider fail-closed behavior.

### Material judgment and limit

- The fixture copies only minimal scenario titles and trigger timing from the read-only 9WL source. Its probability values are deliberately deterministic test inputs, not historical estimates or forecasts.
- Live OpenCode smoke remains **blocked**: no newly authorized server-side provider secret is available. No live-provider claim is made; deterministic mock completion is the verified path.

## Phase 3 formal FAIL remediation — 2026-08-15

### Delivered

- Reworked the OpenCode 1.3.0 adapter for raw JSONL events while retaining only a compatibility path for single-envelope mocks. It now extracts final structured text/payload and usage, and establishes native-search provenance only from actual `tool_use.part → part.tool` `search`/`websearch` telemetry with `part.state.status` `completed`/`success`; generic `capabilities`, `tools`, `search`, and `results` fields never establish provenance.
- Added an opt-in asynchronous Phase 3 message endpoint. It returns the existing typed operation receipt before provider completion; `StreamingResponse` supplies replay, typed terminal events and keepalive heartbeats. Cancel signals the actual provider runner event before state commit. Existing synchronous endpoints remain synchronous and unchanged.
- Made `OperationManager.execute` endpoint-plus-idempotency-key single-flight under concurrency. Same-body callers wait and replay the one submission; mismatch remains a conflict. A Barrier regression test proves one provider call.
- Made evidence-backed follow-up messages return `CHALLENGING` only when both draft evidence gaps and unresolved issues are clear. Normal confirmation now has an audit regression proving preserved citations/provenance after a valid public-evidence turn.
- Promoted `citation_ids`, `public_fact_citation_ids`, `native_search_used` and server-derived `provenance` into required canonical `ChallengeRecord` fields, regenerated TypeScript and OpenAPI, and retained attached-evidence grade validation.
- Tightened usage so `total_tokens` is an exact non-negative Python integer and `cost_usd` is a finite, non-negative non-bool integer/float; rejected bool, NaN, infinity and negative values. Expanded prompt-control detection to forget-prior, approval and compliance forms across claims and untrusted evidence strings.

### Verification and limit

- `uv run --project apps/api pytest apps/api/tests`: **51 passed** (one unchanged third-party TestClient deprecation warning), including JSONL, single-flight Barrier, provider-blocked SSE/cancel and terminal async-replay tests.
- Live smoke remains **blocked**: no new authorized server-side provider key is present. No historical key was used and no DeepSeek-live claim is made.

## Phase 3 Sol FAIL remediation — 2026-08-15

### Corrections and regression evidence

- Replaced static OpenCode search capability with fail-closed runtime telemetry verification: only a real successful `tool_use.part → part.tool → part.state.status` search event establishes provenance; missing qualifying telemetry forces client fallback.
- Added canonical-hashed full client evidence bundles, C/not-yet-verified URL evidence, D/no-URL evidence, publication-time enforcement and legacy IPv4/IPv6 SSRF rejection.
- Made draft fields, rubric objects and citation subset relations strict; D/E cannot support public facts. Normal confirmation additionally requires independently reviewable evidence, while reservation confirmation is unchanged.
- Added pre-call budget reservation, finite non-negative usage validation, retry only for 429/5xx/timeout, and immediate non-transient CLI failure.
- Registered running operations and cancellation events before provider execution; cancel reaches the live callback, prevents commit/re-submit, and explicit resume uses that callback rather than synthetic completion.
- `uv run --project apps/api pytest apps/api/tests`: **35 passed** (one unchanged third-party warning). Live smoke remains **blocked by missing new server-side secret** and is not represented as DeepSeek-live verification.

## Phase 3 independent review remediation — 2026-08-15

- Removed unsafe provider re-execution from resume. Cancelled/failed operations now retain their terminal audit checkpoint, replay without a second submission/charge, and do not emit synthetic completion events; a genuinely new user command is required for another provider attempt because OpenCode CLI exposes no auditable charge receipt.
- Rebuilt `evidence_ids` and `evidence_grades` from all attached challenge evidence on every later message turn.
- Tightened native-search provenance to actual completed/successful OpenCode `tool_use.part → part.tool → part.state.status` telemetry. Generic `capabilities`, `tools`, `search`, and `results` fields are all rejected as non-provenance, including result-shaped payloads.
- Caught `ApiProblem` from `build_result`, records typed `operation.error`, transitions to `failed`, and idempotently replays that error rather than leaving a running operation.
- Regression suite: `38 passed`, including provider submission-count assertions across failure/cancel/resume, message evidence propagation, native-search false positives and `ApiProblem` lifecycle closure.

## Phase 3 Challenge and Evidence — 2026-08-15

### Delivered

- Added a provider-neutral `LlmProvider` and `SearchEvidenceProvider`, with deterministic mock as the default and an opt-in OpenCode DeepSeek Flash adapter using the required CLI model form. The adapter never reads, prints, stores or forwards credentials; it applies a concurrency-one gate, rounds/token/cost budgets, timeout/cancel termination and bounded exponential backoff for 429/5xx.
- Added fail-closed structured-output handling: malformed JSON, prompt-control text, incomplete/invalid 10-dimension rubric output, unattached citations and D/E evidence promoted as public facts all produce no challenge record. Accepted output is rebuilt as and validated against the existing canonical `ChallengeRecord`.
- Added client evidence fallback URL safety: HTTP(S), size, user-info, port and literal private/reserved-IP guards, plus a redirect-target DNS validation hook for any future fetcher. The API never server-fetches client evidence. Missing URL stays grade D/unreviewable and leaves challenge evidence requirements open.
- Preserved the Phase 1 normal-confirmation gate and extended typed SSE/Last-Event-ID/cancel/resume/idempotency behavior for provider failures: a failed original request replays its failure without re-submitting; explicit resume retains the operation ID and makes the original request replay the validated result. No canonical schema expansion or generated-contract change was necessary.
- Added reviewed JSON fixture and tests for mock end-to-end fallback, OpenCode success/no-search capability, 429, 502, timeout, cancel, invalid JSON, prompt injection, citation mismatch, D/E public-fact rejection and SSRF/redirect checks.

### Verification and limitation

- `uv run --project apps/api pytest apps/api/tests`: 28 passed; one unchanged third-party Starlette TestClient deprecation warning.
- Live smoke is **blocked by missing server secret** in this environment. The OpenCode path is implemented but is not represented as DeepSeek-live verified; deterministic mock acceptance is the verified path.

## Phase 2 strict-decoder and manifest-closure fix — 2026-08-15

Sol's second review found two false-negative paths in `af9b473`: `pngjs` alone accepted a valid-CRC PNG whose zlib header was invalid, and the registry did not reject duplicate paths or all required/variant/archive role drift.

### Fix

- Added `scripts/verify/design-png-guards.mjs`: verifies every chunk CRC and order, restricts the approved 8-bit non-interlaced encoding, inflates the concatenated IDAT stream with `inflateSync(..., { info: true })`, requires all compressed bytes to be consumed, and checks the exact filtered scanline length before `pngjs` decodes RGBA pixels.
- Added `scripts/verify/design-assets-regression.mjs` to construct in memory, with valid PNG CRCs, an invalid zlib header and trailing compressed bytes; both are required to fail the guard.
- The manifest gate now consumes `required_web`/`required_android`, rejects duplicate registry paths and duplicate variants, validates roles against the exact required/variant/archive path sets, and rejects role overlap, missing files and unregistered files.
- Added an explicit `archive_paths` manifest list so archive membership is independently checked in both directions against the registry role set.
- Added PNG `PLTE` ordering, uniqueness, length and grayscale-compatibility guards; the regression suite now rejects a legal-CRC `PLTE-after-IDAT` stream.

### Verification

- `corepack pnpm verify:phase2`: passed all 35 pixel decodes, exact dimensions, required/variant/archive closure, and both corruption regressions.
- Manual proof: a legal-CRC invalid zlib header is rejected with `incorrect header check`; no file is written by the regression test.

## Phase 2 verifier hardening — 2026-08-13

### Blocker

Sol's independent Phase 2 re-review of commit 2d5cf57 returned FAIL: the PNG verifier checked only the signature, IHDR, dimensions and the presence of an IEND marker anywhere in the byte stream, without decompressing IDAT. A corrupted pixel stream still produced PASS. Two archive PNGs with non-standard Android dimensions (916x1717, 864x1821) were also accepted without being registered as historical exceptions.

### Fix

- Added an authoritative assets registry to phase2-manifest.json: all 35 PNGs (13 required, 4 variant, 18 archive) each with exact expected width, height and role. The verifier now refuses any on-disk PNG that is not registered and any registered PNG that is missing from disk.
- Rewrote design-assets.mjs to decode every pixel via pngjs PNG.sync.read. A corrupt or truncated IDAT stream, bad CRC or incomplete RGBA buffer now throws and fails the gate. Verified by a synthetic corruption test: PNG.sync.read throws on bad compressed data, where the old verifier returned PASS.
- Strict dimension comparison: every decoded PNG width and height must exactly match its manifest entry. The two non-standard archives are registered at their true historical dimensions and reported as archive historical exception; they are not subject to the 9:16 artboard rule but are still pixel-decoded and dimension-checked.
- Added pngjs and @types/pngjs as workspace dev dependencies.

### Verification evidence

- corepack pnpm install --frozen-lockfile and uv sync --project apps/api --frozen: passed.
- corepack pnpm typecheck: passed all five typed workspace projects.
- corepack pnpm verify:phase0: passed six immutable FM references, all governance boundaries, 119 unique Git-history blobs and the current-tree secret scan.
- corepack pnpm verify:phase1: passed generated contract/registry/OpenAPI drift checks, 4 domain tests, 2 registry tests and 20 API tests.
- corepack pnpm verify:phase2: passed 35 pixel-decoded PNG assets (exact dimension match), 13 required concepts, 4 state variants, 2 registered historical-exception archives, all specifications, contrast checks and design-system typecheck.
- corepack pnpm test: passed contracts, design-system, 4 FM parity tests, 4 domain tests, 2 registry tests and 20 API tests.
# Progress

# Phase 5 FM engine — implementation handoff — 2026-08-15

### Sol P1 remediation complete — follow-up commit

- Corrected forecast scaling to average `|IC|` only over factor IDs that actually
  contributed to the support-weighted composite; added a regression proving an
  unused selected factor cannot change forecast values or dead-band decisions.
- Changed `forwardReturns` to typed `{asOf, values}` rows. Every row must align to
  its observation timestamp and be no later than `cutoff`; future and mismatched
  rows fail closed before metrics evaluation. Plain `number[][]` is no longer part
  of the TypeScript contract and is rejected at runtime as malformed input.
- Selection now skips non-finite `icMean`/`icir` metadata before `maxFactors`
  truncation; NaN/Infinity-only inputs retain explicit equal-weight fallback while
  stable finite-quality ties remain input ordered.
- `computeFm` now preserves the known asset width when no factor contributes: it
  returns width-shaped NaN composite/forecast arrays, explicit equal weights,
  fallback reasons, and a normal initial allocation/rebalance path.
- `allocateCapital` now rejects non-finite or non-positive price, FX and lot
  values, as well as non-finite/negative capital and weights, before any arithmetic.

### Delivered

- Added the pure client `computeFm` orchestration API with intermediate factor
  signals, selected IDs, combination/support, weights, forecast scale, forecasts,
  metrics, rebalance/dead-band result, and explicit reasons. Every observation is
  cutoff-checked before sorting/evaluation; future rows are rejected.
- Corrected factor-quality ties to Python stable input ordering and retained NaN
  support semantics, equal-weight fallback, free initial allocation, 3 bps default
  subsequent transfer, strict dead-band comparison, FX conversion, lot rounding,
  and canonical hashing.
- Added the finite AST DSL and browser Worker adapter. Core source contains no
  Node/DOM imports; the Android-sized test imports and executes the same package
  entry with 15 assets.
- Reworked the Python fixture generator to hash all six immutable references and to
  emit null for non-finite values, input orientation, cutoff data, formula cases for
  combination/portfolio/metrics/backtest, and the client-bound walk-forward/ASSETS
  subsets. Import failure is visible and `--require-reference-import` is fail-closed;
  normal `--check` is deterministic because transient exception text is not stored.
- Added [ADR-006](architecture/ADR-006-fm-engine.md) with the formula mapping,
  client-only boundary, Worker/Hermes compatibility, oracle mode, and limitations.

### Verification evidence

- `Set-Location D:\Foresightnine\src; uv run --project apps/api python scripts/fm/generate_oracle.py --check`: passed byte-identical fixture and all six hashes. The local environment reports `ModuleNotFoundError: No module named 'matplotlib'` and uses the documented copied-formula oracle mode.
- `Set-Location D:\Foresightnine\src; corepack pnpm --filter @foresightnine/fm-engine typecheck`: passed.
- The preceding Phase 5 implementation suite passed 14 tests, 0 failures; this
  follow-up adds two deterministic regressions, so the current suite passes 16
  tests, 0 failures, covering Python parity, reference cases, AST rejection,
  cutoff, Worker, Android-sized smoke, orchestration fallback, finite numeric
  guards, 3 bps, dead-band, FX, and lot checks.

The workspace and Phase 0/1/2 gates were run for the preceding Phase 5 commit and
remain green in the current checkout; the full reference-package import remains
unavailable in the minimal `uv` environment and is the only known oracle limitation.

## Phase 2 reviewer corrections complete — 2026-08-10

### Delivered

- Corrected the Web FM concept to the executable pipeline: one-based percentile rank minus 0.5, IC-sign direction, global factor quality `|IC|×|ICIR|`, per-asset valid-support combination, population-standard-deviation z-score, clipping and final long-only re-normalisation.
- Corrected evidence grade C to secondary commentary/expert interviews; it is no longer described as a company filing.
- Corrected Android Raw and Public Default NAV horizons to a coherent `2024-08 → 2026-08-10 today → 2028-08-10 (+24m)` boundary.
- Retained every replaced concept in an `archive` directory. The active suite contains 13 required full-state concepts and four state variants: Web/Android SSE recovery and Web/Android Public Default delta.
- Strengthened Phase 2 verification to inspect every PNG recursively, including variants and archives, and to enforce the required Web and Android artboard dimensions for both primary and variant states.
- The standing user instruction to accept all proposed solutions and continue through completion covers this reviewer-directed correction pass; its scope and source are recorded in `phase2-manifest.json`.

### Verification status

- `corepack pnpm install --frozen-lockfile` and `uv sync --project apps/api --frozen`: passed with the checked-in lockfiles.
- `corepack pnpm typecheck`: passed all five typed workspace projects.
- `corepack pnpm verify:phase0`: passed six immutable FM references, all governance boundaries, 119 unique Git-history blobs and the current-tree secret scan.
- `corepack pnpm verify:phase1`: passed generated contract/registry/OpenAPI drift checks, 4 domain tests, 2 registry tests and 20 API tests.
- `corepack pnpm verify:phase2`: passed 35 recursively inspected PNGs, 13 required concepts, 4 state variants, all specifications, contrast checks and design-system typecheck.
- `corepack pnpm test`: passed contracts, design-system, 4 FM parity tests, 4 domain tests, 2 registry tests and 20 API tests.
- Original-resolution visual inspection passed the corrected FM equation, evidence-grade C definition and both Android NAV horizons. Formal Sol re-review remains the phase-gate authority.

## Phase 1 Sol confirmation blocker remediated — 2026-08-10

### Delivered

- Reproduced the blocker at commit `1af8069`: an initial `CHALLENGING` record with a 20/40 rubric accepted ordinary `CONFIRM` with `unresolved_issues_acknowledged=false`, then confirmation-derived provider logic rewrote the rubric to 30/40.
- Ordinary `CONFIRM` now requires a rubric total of at least 30/40; `premise_clarity`, `time_boundary`, `causal_chain`, `asset_transmission` and `falsifiability` must each be at least 3/4; `missing_evidence` and `unresolved_issues` must both be empty. Failure returns structured 409 code `confirmation_requirements_not_met` with per-gate details and leaves the challenge and turn counter unchanged.
- `CONFIRM_WITH_RESERVATIONS` is an explicit pre-confirmation command from `CHALLENGING` or `EVIDENCE_REQUIRED`. Canonical validation requires `unresolved_issues_acknowledged=true` and a non-whitespace reservation note.
- Confirmation no longer invokes the research provider. It copies the current claims, scoring, missing evidence, unresolved issues, AI objections, questions and private-evidence declaration unchanged; only the new turn, confirmed status, user confirmation, optional reservation note, provenance and content hash are written.
- The deterministic mock review becomes rubric-passing only after independently reviewable evidence is accepted. Provider scoring no longer depends on `CONFIRMED` status.
- Added direct early-confirmation rejection, false acknowledgment/missing/whitespace note, reservation preservation, no-rewrite and passing ordinary-confirmation regression coverage. Updated the shared domain reservation command for both pre-confirmation review states.
- Added the canonical error code and non-whitespace note constraint, then regenerated TypeScript contracts and both 20-path OpenAPI snapshots. Canonical schema SHA-256 is `deb077db03cd740cf9d7e2d618ad5fc9c5610bdd4ac9e6fe0f1f8c26fa48d01d`.

### Verification evidence

- `corepack pnpm install --frozen-lockfile`: passed; all 6 workspace projects already up to date.
- `uv sync --project apps/api --frozen`: passed; 32 packages checked.
- `corepack pnpm typecheck`: passed all 5 typed workspace projects.
- `corepack pnpm verify:phase1`: passed generated-contract/OpenAPI drift gates, 4 domain tests, 2 registry tests and 20 API tests.
- `corepack pnpm test`: passed contracts, design-system, 4 FM parity tests, 4 domain tests, 2 registry tests and 20 API tests.
- The existing third-party Starlette `httpx2` migration warning remains non-failing and unchanged.

### Scope preservation

- This remediation did not edit design concepts, FM implementation/fixtures, governance files, root plans or any declared read-only source.
- No Git commit was created; integration remains with the main thread.

## Phase 1 gate complete — 2026-08-10

### Delivered

- Established `packages/contracts/schemas/v1/contracts.schema.json` as the sole Draft 2020-12 authority. The previous entity/research files are compatibility references only.
- Generated strict TypeScript contracts (schema SHA-256 `8763a1c15fde062aba0978226c41c3dfd899980eae0bd90b8f51750f8eab4b49`), compiled Pydantic models directly from the schema, and exported a 20-path OpenAPI 3.1 snapshot with fully resolvable local references.
- Added strict unknown-property rejection to core entities, nested records, requests, responses and errors. Every core entity composes `schema_version`, `created_at`, `source_refs` and `content_hash`.
- Added premise, conditional and joint probability scalars plus intervals, ordering/containment/product invariants, and explicit `model_estimate_unvalidated` mock status.
- Added a generated 36-entry asset registry covering all 15 benchmark economic exposures, explicit tradable proxies, global equity/bond/commodity products, BTC/ETH, REIT and cash. Registry source, snapshot and nested hashes are drift-gated.
- Added versioned market manifest/shard, factor manifest/shard with whitelist postfix tokens and admission thresholds, and client-computation attestation contracts/endpoints. Market mock data is explicitly `historical_only`; final portfolio computation remains client-side.
- Completed all planned API endpoints plus canonical request/success/error payloads. Every POST requires `Idempotency-Key`; same-body retry returns the exact operation/result after state advancement, while body conflict returns 409.
- Added deterministic fixed clock and content-derived session/thesis/challenge/worldline/operation IDs, operation/cancel/resume tokens, typed SSE started/progress/heartbeat/error/complete events, and suffix replay via `Last-Event-ID`.
- Enforced the approved lifecycle graph. Early confirmation requires the explicit `CONFIRM_WITH_RESERVATIONS` command, acknowledged unresolved issues and a non-empty reservation note.
- Added content-hashed versioned A–G acceptance fixtures and TS compile positive/negative gates, domain state/probability/worldline tests, registry tests, Python schema/model/API/operation tests, OpenAPI resolver tests and generation drift tests.
- Added `verify:generated-contracts` and a real `verify:phase1`; root `test` executes all workspace tests plus the Python API suite.

### Verification evidence

- `corepack pnpm install --frozen-lockfile`: passed; 6 workspace projects, lockfile current.
- `uv sync --project apps/api --frozen`: passed; 32 packages checked.
- `corepack pnpm typecheck`: passed for contracts, domain, asset-registry, design-system and the existing fm-engine.
- `uv run --project apps/api pytest apps/api/tests`: **18 passed**; one third-party Starlette warning recommends migrating its TestClient from `httpx` to `httpx2`.
- `corepack pnpm verify:phase0`: passed 6 immutable reference hashes, all 3 governance hashes/boundaries and secret scan.
- `corepack pnpm verify:phase1`: passed generated TS/registry/OpenAPI drift checks, 4 domain tests, 2 registry tests and 18 Python tests.
- `corepack pnpm verify:phase2`: passed 13 approved concepts, 4 variants, specification files, contrast checks and design-system typecheck.
- `corepack pnpm test`: passed contracts generation/type tests, 4 existing fm-engine parity tests, 4 domain tests, 2 registry tests, design-system typecheck and 18 Python tests.

### Preserved concurrent work

- No source or test file under `packages/fm-engine` was edited; its existing parity suite was only executed by required root verification.
- Existing Phase 0 governance/reference/boundary/secret scripts and Phase 2 concepts/design-system work were retained and passed unchanged gates.
- `plan.md`, `agent.md`, `GOAL_PROMPT.md` and all declared external reference/source roots remained read-only; Phase 0 boundary hashes confirm this.

### Known limits / later-phase obligations

- The Phase 1 stores and SSE replay log are process-local. PostgreSQL/Redis persistence must preserve the tested wire semantics before public operation.
- Integrity records use a clearly labelled deterministic mock key ID, not production signing. Live provider/search behavior, rate limits, abuse controls, licensed current market data and production key management remain later phases.
- The Starlette TestClient deprecation warning is dependency-owned and non-failing; dependency migration should be handled with a lockfile-reviewed toolchain update.

The older baseline below is retained as historical evidence and is superseded where it described interval-only probability, incomplete endpoints, or unavailable streaming.

## Phase 0/1 baseline — 2026-08-10

### Delivered

- pnpm workspace and TypeScript strict baseline, with contracts and domain packages.
- `v1` JSON Schema/OpenAPI/TypeScript contracts for core research entities and API outputs.
- Explicit research lifecycle state machine plus probability interval and worldline DAG invariants.
- `uv`/FastAPI skeleton with schema-validated mock Challenge, Worldline, Probability, session, evidence, asset, health, and methodology endpoints.
- Contract tests that validate live FastAPI mock responses against the shared JSON Schema.
- Docker API skeleton and two architecture ADRs.

### Material judgment calls

- No key or real provider was configured; the approved mock fallback is the only active provider.
- Probability remains interval-only and declares `model_estimate_unvalidated` to avoid pseudo precision.
- Failure states are not persisted as a new lifecycle enum: an operation failure retains its durable prior state and can retry, preserving the approved state-machine vocabulary.

### Evidence and verification

- `corepack pnpm typecheck`: passed for `@foresightnine/contracts` and `@foresightnine/domain`.
- `uv run pytest` from `apps/api`: **5 passed**. A third-party FastAPI/Starlette TestClient deprecation warning remains; it does not affect contract results.
- Credential-like assignment scan of authored source/config/docs (excluding dependencies, virtual environments, generated output, and lock files): no matches.

### Known limits / next phase

- No persistence, streaming, rate limit, search adapter, real market data, FM engine, Web, or Android implementation yet.
- OpenAPI is the public endpoint description; generated client artifacts will be added with the consuming clients.

### 2026-11-18 Messages 研究接口离线集成

Messages 适配、协议、研究运行和恢复、额度、账户配置及回执的 70 项禁网 API 测试通过；网页模型配置解析 11 项通过。平台与个人配置均通过两轮工具调用和引用落库，平台模拟响应的 60 个明确报告 token 完成结算。先前扩大批次中六项既有计费测试失败，未计入通过结果；未调用真实模型或发布，研究质量、真实服务能力和投资效果均未验证。上方 2026-11-16 段落是当时的状态记录，M1 真实金融研究验收仍未完成。

研究运行按显式协议选择 Chat Completions 或 Messages；Messages 平台配置使用独立的研究端点、模型、凭据及主机允许列表。个人 Messages 端点须获得部署侧精确允许；更换凭据来源或协议时须重新提供密钥。账户余额只用于判断和预留有界调用，不作为生成上限；模型回执、未知外部状态和结算状态由持久日志记录。

部署未设置 `FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL` 时沿用现有 Chat 研究路由；选择 `anthropic_messages` 时必须另行提供研究专用端点、模型、密钥、凭据版本、主机允许列表、协议版本、认证方式、正的单次输出上限，以及匹配端点、模型和协议版本的原生工具与续接能力声明。个人端点还须与部署允许的 Messages 端点精确匹配。账户计费的 Chat 调用同样需要可证明的单次输出上限或能力配置，以便在调用前预留；余额不能推导模型输出上限。能力声明是部署配置，不代表已完成真实服务验证。

真实验收预登记（尚未执行）：新建主题，通过普通 `/v2/research/topics/{id}/runs` 提问：“依据所附公司年度披露，比较连续两个财务年度的收入，计算同比增长率，报告期间、币种、计算输入及引用，并说明限制。”使用验收前确认允许模型使用的公司原始披露快照，记录公开时点并固定内容哈希；模型应调用确定性计算工具，并在报告中引用可核验的原文。关闭搜索和自动获取，仅开放读取所附材料、计算和提交研究所需工具，不复用历史运行或结果。取得用途获授权的独立凭据及部署配置后，在调用前冻结实际端点、模型、资料快照、协议版本、认证方式、能力声明、超时、单次输出上限和支持的参数。用户未设置的任务调用数、总 token、时间和费用预算保持未设置；账户额度只准入已配置上限的调用。记录脱敏配置、实际调用次数、引用与计算核验、用量或未知用量、延迟、结算及限制；不自动重试未知外呼。当前没有确认可用于直接 API 研究的凭据，因此尚未发起外呼或确定模型和快照。

最终离线 API 命令（在 `src/apps/api` 运行）：

```sh
FORESIGHTNINE_OFFLINE_TESTS=1 .venv/bin/python -m pytest -q tests/test_messages_research_integration.py tests/test_anthropic_messages_provider.py tests/test_model_protocol_native.py tests/test_research_runtime.py tests/test_research_runtime_api.py tests/test_research_capacity_unset.py tests/test_model_credentials.py tests/test_research_model_receipts.py
```

网页命令（在 `src/apps/web` 运行）：`npm test -- --run src/model-config-client.test.ts`。最终 API 批次包含两项个人 Challenge 账户配置测试；其测试替身已补上既有调用契约中的可选关键字参数，生产 Challenge 行为未因此修改。

2026-11-18 真实 Messages 验收准备：新建 [360 验收目录](product/phase-a/360-messages-runtime-validation/preregistration.md)，固定微软官方 2026 年报两期收入的最小事实摘录和独立 Decimal 对照，并准备隔离的普通研究运行与一次性提交标记。只检查了专用凭据配置是否存在：`FORESIGHTNINE_RESEARCH_LLM_API_KEY` 缺失；隔离运行的非秘密凭据版本由验收配置固定。预检在调用前停止，未冻结实际运行、未提交研究、未联系模型服务；官方网页事实与原文使用边界记录在预登记中。

2026-11-18 已按用户确认使用国际区现有凭据完成一次 360 真实尝试：[结果与证据](product/phase-a/360-messages-runtime-validation/evaluation.md)。五次模型响应后，第六次在 DNS 阶段未发送即失败；研究部分完成、未生成最终报告。确定性计算得 17.79%，五次报告总用量 21,159 tokens，费用未知；结算的六条 settled 中包含一次未发送的零用量结算，不能解释为六次提供方调用。未重放。

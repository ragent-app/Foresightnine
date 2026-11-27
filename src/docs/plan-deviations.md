# Plan deviation log

## 2026-11-27 · 用户调整产品定位

由通用金融研究工作台调整为面向自主投资者的个人投资决策 Agent。多公司研究与雷达支持机会比较，画像及持仓支持个性化配置，事前预期与实际投资记录支持收益复盘。此次修订涉及产品说明、规划入口和展示文案；阶段验收、FM 的实现选择及发布授权继续按各自记录执行。详见[宏观方案](product/fusion-finance-ai-macro-plan-2026-11-11.md)。

## 2026-08-24 — user-directed parallel hardening while Phase 7 native build is externally blocked

### Approved baseline

`D:\Foresightnine\plan.md` requires Phase 0–9 to be accepted in order. Phase 7 Android native acceptance requires a release build and emulator evidence before Phase 8 begins.

### Evidence

- Android Studio, Android SDK API 36, platform-tools, Emulator and the Google Play x86_64 image are installed and the `ForesightNine_API36_Play` AVD boots successfully.
- `expo run:android --variant release --no-bundler` completed Metro bundling but failed before Gradle configuration because the generated Wrapper could not connect to the official Gradle 9.3.1 distribution endpoint. Increasing the generated Wrapper network timeout from 10 s to 60 s reproduced a connection timeout.
- The user explicitly directed that, while permissions/install dependencies are unavailable, work may continue on Google/GitHub login components, a safe OpenAI-compatible GLM provider boundary, actual FM case tests, and Sol review.

### Minimal scope override

Until the native Gradle distribution is available, permit only work that does not claim Phase 8 completion and does not weaken Phase 7 gates:

1. secret-free configuration/contracts/tests for optional Google/GitHub authentication;
2. a server-only OpenAI-compatible provider configuration boundary and mock/contract tests, with live calls requiring a newly rotated environment-injected secret;
3. deterministic FM and scenario-data audit tests; and
4. independent Sol review.

No production OAuth login, live provider request, market-data claim, public deployment, or Phase 8 acceptance may be claimed under this override. Android release build/emulator evidence remains required before Phase 7 can pass.

### Review status

The first Sol review on 2026-08-24 returned **fix-first**, no P0: it required probability/timeline semantics, computed NAV/drawdown, full Public Default constraints/cash accounting, unverified shared-evidence labelling, bilingual a11y/export copy, real Android evidence, provider safety and OAuth foundations. The fixture/domain and provider issues are remediated with regression tests; Android native evidence remains externally blocked. A fresh Sol acceptance review is required after the OAuth foundation and final change review; this deviation does not authorize advancing to Phase 8.

## 2026-08-24 — native blocker resolved; release QA remains non-production

- The Gradle distribution and Android build dependencies became available. Debug and self-contained release APKs now build, install and run on the API 36 emulator; the detailed command/output and visual evidence are recorded in `docs/progress.md` and `docs/qa/android/`.
- Windows path-length failures were resolved without changing application behavior: pnpm's project-level virtual store is repository-relative `nm`, and `scripts/android-short-cmake.init.gradle` redirects generated CMake staging output to a short external build root. Android's documented `buildStagingDirectory` exists specifically to relocate persistent native build output; the helper is a local build invocation input, not app/release configuration.
- The release APK is signed by the generated debug key and is explicitly QA-only. Play signing/upload keystore, production dependency audit remediation, market-data licensing and jurisdictional review remain release gates.
- This resolves only the temporary Phase 7 native-build blocker. The plan sequence and requirement for a fresh independent Sol verdict remain unchanged; Phase 8 is still prohibited until that verdict.

## 2026-08-24 — correction: release-signing evidence superseded by fail-closed configuration

### Evidence

- The current `apps/mobile/android/app/build.gradle` requires all four release-signing environment values before `assembleRelease` can proceed and explicitly rejects debug signing for release variants.
- The current worktree has no approved upload keystore or release-signing environment injected. The only native artifact validated in this pass is the debug `x86_64` emulator build, executed with Temurin 17 and the repository's generated-CMake staging helper.

### Correction

The earlier statements above and in historical progress entries that a self-contained release APK was signed with a debug key are superseded. They must not be read as current release verification, Play readiness or an authorization to distribute. This correction changes no plan requirement: release signing, dependency audit remediation, market-data licensing, jurisdictional review and an independent Sol verdict remain gates.

# Production dependency audit

## 2026-08-30 — current result

Command:

```powershell
corepack pnpm audit --prod
```

Result: **no known vulnerabilities found**.

| Advisory | Resolved version | Current path | Scope | Decision |
| --- | --- | --- | --- | --- |
| None reported by the current production audit | N/A | The resolved tree contains `uuid@11.1.1` through the `xcode@3.0.1` toolchain edge | Expo configuration/build tooling, not application request handling | The previously open advisory is locally remediated; retain normal release-owner review of the override and rerun the audit in the deployment environment. |

## Evidence and containment

- `corepack pnpm why uuid` resolves exactly one copy, `uuid@11.1.1`, through
  `xcode@3.0.1`.
- Local inspection shows that `xcode@3.0.1` uses CommonJS `require('uuid')` and `uuid.v4()` only while manipulating Xcode project files. It is not imported by the Android or Web application runtime.
- The published `xcode@3.0.1` metadata requests `uuid@^7.0.3`; the repository
  lockfile uses the narrow override `xcode@3.0.1>uuid: 11.1.1`. The installed
  CommonJS smoke confirms `xcode` can load the overridden package and call
  `uuid.v4()` while exporting a project. This is a compatibility exception,
  not an upstream Expo upgrade.
- The override is intentionally scoped to this one transitive edge. A
  deployment must rerun the audit, CommonJS smoke, and supported Android/Web
  builds before treating the result as release-clear.

## Required resolution before public release

1. Prefer an Expo SDK / `@expo/config-plugins` update whose supported dependency graph keeps the advisory resolved without an override.
2. Until then, retain the tested override with a release-owner/security review and expiry/upgrade owner.
3. Re-run `corepack pnpm audit --prod`, Android release build, and all workspace tests in the deployment environment. Record the zero-advisory result or an explicitly accepted release exception approved by security/legal owners.

This record does not assert that the issue is exploitable in ForesightNine. It
records the prior advisory, its tested local resolution, and the need for a
deployment-environment rerun; it does not by itself authorize a public-release
security-clear claim.

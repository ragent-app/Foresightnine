# Production dependency audit

## 2026-08-24 — current result

Command:

```powershell
corepack pnpm audit --prod
```

Result: **one moderate advisory; not release-clear**.

| Advisory | Resolved version | Current path | Scope | Decision |
| --- | --- | --- | --- | --- |
| `GHSA-w5hq-g745-h8pq` (`uuid`: missing buffer bounds check when `buf` is provided) | `uuid >= 11.1.1` | `apps/mobile > expo@57.0.15 > @expo/cli > @expo/config > @expo/config-plugins > xcode@3.0.1 > uuid@7.0.3` | Expo configuration/build tooling, not application request handling | Keep as a public-release gate until the supported Expo dependency graph removes it. |

## Evidence and containment

- `corepack pnpm why uuid` resolves exactly one vulnerable copy, `uuid@7.0.3`, through `xcode@3.0.1`.
- Local inspection shows that `xcode@3.0.1` uses CommonJS `require('uuid')` and `uuid.v4()` only while manipulating Xcode project files. It is not imported by the Android or Web application runtime.
- The currently published `xcode@3.0.1` remains pinned to `uuid@^7.0.3`. Its upstream development branch contains a later replacement with `crypto.randomUUID()`, but this has not been accepted as a compatible published dependency upgrade for this repository.
- A blind `pnpm.overrides` major-version change was deliberately not applied: it could break Expo's supported configuration toolchain and would hide rather than remediate the unsupported dependency combination.

## Required resolution before public release

1. Prefer an Expo SDK / `@expo/config-plugins` update whose supported dependency graph resolves the advisory.
2. If upstream support is unavailable, obtain a reviewed vendor patch or a documented, tested patch-package exception with an owner and expiry.
3. Re-run `corepack pnpm audit --prod`, Android release build, and all workspace tests. Record a zero-advisory result or an explicitly accepted release exception approved by security/legal owners.

This record does not assert that the issue is exploitable in ForesightNine. It records an unresolved audit finding and prevents a public-release security-clear claim.

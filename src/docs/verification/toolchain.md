# Toolchain baseline

Observed on the Phase 0 Windows workstation on 2026-08-10:

| Tool | Version | Reproducibility mechanism |
| --- | --- | --- |
| Git | 2.51.0.windows.1 | repository rooted at `D:\Foresightnine` |
| Node.js | v22.18.0 | root `engines.node` |
| pnpm | 11.21.0 | root `packageManager` + `pnpm-lock.yaml` |
| uv | 0.11.7 | `apps/api/uv.lock` and frozen sync |
| Python | 3.13 selected by uv project | `apps/api/.python-version` |

Required cold-start checks:

```powershell
Set-Location D:\Foresightnine\src
corepack pnpm install --frozen-lockfile
uv sync --project apps/api --frozen
corepack pnpm verify:phase0
corepack pnpm typecheck
corepack pnpm test
```

Changing a pinned tool or dependency requires an ADR, regenerated lock, cold install and full parity/security rerun.

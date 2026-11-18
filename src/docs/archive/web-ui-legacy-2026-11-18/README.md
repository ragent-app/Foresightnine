# 旧版 Web UI 归档（2026-11-18）

后端默认研究路径已改为原生工具循环（`runtime: native_tools`），旧 Web UI 仍按分阶段流水线设计，因此整体退役，新界面另行重建。

## 内容

- `web-ui-legacy-2026-11-18.tar.gz`：移除前 `src/apps/web` 的工作区完整快照，包含 `src/`、`qa/stage-b-acceptance/`、`public/`、配置和 QA 截图，以及当时未提交和未跟踪的文件。快照不包含 `node_modules/`、`dist/` 和 `.env.production.local`。
  - SHA-256：`7c669775427b760e04d2e87bb96c9c2e76eb818a54b82ae8a0a3445a7ad71460`
  - 已解包并与移除前的工作区逐文件比对，结果一致。
- `uncommitted-web.diff`：移除时 `src/apps/web` 相对 `HEAD`（`199d2e0`）的未提交改动。
- 已提交的版本也可以从 git 历史 `199d2e0` 中取回。

## 恢复

```bash
cd src/apps
tar xzf ../docs/archive/web-ui-legacy-2026-11-18/web-ui-legacy-2026-11-18.tar.gz
```

## 同时调整

- 移动端测试原先引用 `web/src/fixtures`，相关改动如下：
  - `confirmation-flow` 改用从旧 Web fixtures 固化的合约世界线数据 `apps/mobile/tests/fixtures/legacy-web-worldlines.ts`；
  - `market-data` 和 `mobile-domain` 删除了与 Web fixtures 的一致性断言，因为 Web 端已不存在对应数据。
- `src/apps/web/src/main.tsx` 目前只是占位入口。

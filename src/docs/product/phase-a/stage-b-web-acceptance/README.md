# 阶段 B 工作区集中验收

此目录保存 2026-11-13 的本地浏览器证据。Browser 插件技能不可用，按前端验收流程使用本机 Playwright 和 Chrome。`browser.cjs` 拦截研究 API 并返回合成数据，用于核查页面分支；`real-api-browser.cjs` 连接隔离的本地 FastAPI 和内存存储，`mock_api.py` 注入确定性合成模型，不调用外部模型。浏览器本地测试账户及模拟会话只存在进程/浏览器中，不写入本目录。两组都不能证明真实金融研究质量、生产配置或权限隔离。

- 模拟交互结果：`browser-result.json`；桌面和移动截图 `workspace.png`、`workspace-mobile.png`。
- 本地真实 API 链结果：`real-api-result.json`、`real-api-workspace.png`。覆盖资料与问题创建、运行、成果、编辑版本及授权导出；模拟 provider 结果明确标为合成。
- 模拟交互检查了问题及资料入口、运行期间保存阶段、取消、记录的证据定位、未知语义、旧版本、保存编辑、多种恢复资格、继续和交付按钮、限制导出和记录摘要、账户切换。服务端恢复与权限本身需独立 API/数据库证据。
- 浏览器控制台的一次 403 对应有意拒绝的全文导出，其他页面错误为零。首次脚本适配时曾因测试定位器和拦截范围失败，修正测试夹具后结果如 JSON；不将夹具失败算作产品故障。
- 验收结束后，本次启动的 Vite 与内存 FastAPI 进程已停止；检查本机 5173 和 8769 端口无监听。runtime 的临时 PostgreSQL 容器由其负责人停止并删除。

复现（使用项目现有运行环境，路径可通过 `PLAYWRIGHT_MODULE` / `CHROME_PATH` 覆盖）：从 `src` 启动 Web Vite；以 `FORESIGHTNINE_ENV=development`、`FORESIGHTNINE_STORAGE_MODE=memory`、`FORESIGHTNINE_LOCAL_AUTH_MODE=mock`、`FORESIGHTNINE_LLM_PROVIDER=mock`、`FORESIGHTNINE_CORS_ORIGINS=http://127.0.0.1:5173`，并设置 API 目录为 `PYTHONPATH`，运行 `mock_api.py`；分别执行两个 `.cjs` 脚本。端口仅限本机，禁止指向生产。脚本只生成本地测试数据。

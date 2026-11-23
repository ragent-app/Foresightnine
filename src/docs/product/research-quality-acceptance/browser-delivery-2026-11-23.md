# Web 交付路径浏览器验收

- 环境：本地 Vite `http://127.0.0.1:15077`；Google Chrome headless、1440 × 900；Browser 插件不可用，使用 Chrome DevTools Protocol。脚本 `browser_delivery_cdp.py` 拦截 API 请求并返回固定脱敏 fixture，不读取或修改开发库，不调用模型或外部服务。
- 实测路径：打开完成主题，正文 `[[claim:C1]]` 显示“查看依据”；点击到判断，点击“引用 1”及“查看原文”，来源面板高亮所引原文；刷新后主题、正文链接和判断重新出现；再打开部分完成主题，收入有据、EPS 显示未完成及缺失理由。
- 结果：通过。页面标题为“ForesightNine · 研究工作台”；页面内容非空，无框架错误覆盖层；CDP 未捕获控制台或运行时错误。实际请求仅由脚本返回本地 fixture。截图位于 `/tmp/foresightnine-browser-acceptance/01-completed.png`、`02-claim.png`、`03-source.png`、`04-partial.png`。
- 边界：此宿主 Chrome 缺少中文字形，截图中中文显示为缺字方框；DOM 文本及控件交互可验证，但中文字体和排版视觉效果未验证。未测移动视口、真实 API、真实模型或用户数据。未修改应用代码。
- 命令：`src/apps/api/.venv/bin/python src/docs/product/research-quality-acceptance/browser_delivery_cdp.py`。浏览器进程和临时 profile 均在脚本退出时关闭。

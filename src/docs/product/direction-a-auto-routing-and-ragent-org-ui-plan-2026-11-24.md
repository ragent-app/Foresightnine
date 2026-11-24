# 方向 A 体验整合方案：默认入口自动路由 + 对标 `Ragent_product` 的多 Agent 组织图 UI 重做

- **日期**：2026-11-24
- **状态**：待审阅（尚未修改代码）
- **背景**：首版方向 A 上线后在真实移动端实测中暴露出两个核心体验问题：
  1. **入口割裂**：用户输入“分析NVIDIA距今最新的财报”时，主流程仍跑旧单循环研究，下方却同时常驻一个“美股财报买方投研小组（待启动）+ 手动填 Ticker”的空卡片，形成上下两套界面打架；
  2. **多 Agent 组织图视觉偏离参考**：当前 `OrgGraphPanel.tsx` 呈现为白底黑边矩形流程图，未还原公开仓库 `ragent-app/Ragent_product`（`android/feature/organization/ui/OrganizationNode.kt`、`OrganizationCanvas.kt`、`OrganizationGraphArt.kt`、`FocusTreeLayout.kt`）的核心视觉语言。

---

## 一、主入口自动路由与页面净化（消除上下两套流程打架）

### 1. 后端自动意图识别与路由（`src/apps/api/app/research_earnings_routing.py` + `main.py`）
遵循 `AGENTS.md` “默认用户输入研究问题即可开始”原则，在发起研究（`start_research_run` / 新话题提交入口）前增加确定性意图路由器：
1. **单公司美股财报意图判定**：
   - 命中财报强触发词（财报、季报、年报、业绩、10-Q、10-K、8-K、earnings、quarterly/annual results 等）；
   - 排除明确的多公司横向对比（`vs`、`对比`、`比较`、`同行`）与宏观/非财报类问题；
   - 解析问题中的公司实体（复用 `resolve_entity`），确认唯一匹配到一个**美股上市实体（有效 10 位 SEC CIK）**。
2. **自动分流**：
   - **命中单公司美股财报**：直接自动启动 **方向 A 买方投研小组（Scheme 2）**，返回 `run_mode="earnings_squad"`、`run_id`、`ticker`、`cik`，**不再同时触发旧单循环**；
   - **未命中**（宏观、A/港股、多公司横评、一般问答）：无感走原有单协调者研究流程，零回归。
3. **显式覆盖边界**：若用户通过高级设置显式关闭自动来源/工具或选择仅分析上传私有文档且未指向美股财报，尊重用户设置不强行切入。

### 2. 前端 `TopicPage.tsx` 页面净化
- **彻底删除**底部常驻的“美股财报买方投研小组（待启动）+ 手动输入美股代码”大卡片以及空的 `投研小组实时组织执行树 · 待启动` 占位框。
- **单一主视图融合**：
  - 当话题为 `earnings_squad` 运行（或已有锁定 `EarningsDossier`）时，页面直接展示 **Ragent 风格并行组织图**（运行中）与 **买方财报底稿（Memo / Deep Report）**；
  - 页面底部保留**唯一输入框**：运行中自动切换为向 Lead Coordinator 发送中场调度指令（`steerEarningsSquad`），完成后无缝切回“继续追问”，彻底消除双输入框。
- 非美股财报话题保持原样干净呈现，不出现任何“待启动”占位。

---

## 二、真正对标 `ragent-app/Ragent_product` 的多 Agent 组织图 UI 重做

逐项对照 `ragent-app/Ragent_product` 的真实绘制实现（`OrganizationNode.kt`、`OrganizationCanvas.kt`、`OrganizationGraphArt.kt`、`FocusTreeLayout.kt`、`OrganizationBottomSheet.kt`）在 Web 端（React + SVG/CSS，严守 `style-src 'self'` CSP）重做 `OrgGraphPanel.tsx`：

| 视觉/交互要素 | 当前线上版本（矩形流程图） | 重做目标（严格对标 `Ragent_product`） |
| --- | --- | --- |
| **节点形态（`OrganizationNode.kt`）** | 白底黑边矩形卡片 + 状态胶囊 + 多行截断小字 | **纯圆形三段线性渐变球体（Circular Gradient Orb）**：对角线 sRGB 渐变（`0% → 55% → 100%`），按角色分配柔和专属渐变色系（Lead 深海蓝、分部 KPI 靛紫、财务法证翡翠绿、指引预期差天青、独立空头琥珀橙、撰稿人石墨青）；球体正中仅放单个高对比度白色图标，球体下方仅留极简角色短名 |
| **动作徽章（`OrganizationNode.kt`）** | 卡片内文字标签 | **45° 角悬浮磨砂玻璃圆徽章（Frosted Glass Action Badge）**：定位在圆球边缘外侧（`radius - badgeRadius / 3`），通过半透明毛玻璃圆片 + 单图标实时表达 `READING / SEARCHING / INSPECTING / DELEGATING / WAITING / BLOCKED / FAILED` |
| **拓扑方向（`FocusTreeLayout.kt`）** | 桌面端 4 列左→右硬排 | **自上而下垂直 FocusTree 层次布局**：顶部居中为 **Lead Coordinator** 大圆球 → 中层居中对称展开 3 个并列 **Worker** 圆球 → 下层汇入 **独立空头审查（Bear Reviewer）** → **底稿撰稿（Writer）**，天然同时适配手机竖屏（390px）与桌面端（1440px） |
| **连线与通信粒子（`OrganizationCanvas.kt`）** | 较粗连线 + 线上文字标签 + 图例栏 | **极细低饱和三次贝塞尔曲线（`1.25px`, `alpha 0.22–0.28`）**：普通派发为极细实线，`isReview` 独立审查为极细虚线（`4px 5px`）；移除笨重图例栏；叠加三类语义柔光粒子——**蓝色柔光圆点**（`TASK_ASSIGNED` 派发）、**翠绿柔光菱形**（`RESULT_AVAILABLE` 回传证据）、**琥珀柔光菱形**（`REVIEW_REQUESTED` 空头审查） |
| **出生/分离动效（`OrganizationGraphArt.kt`）** | 瞬间静态画出全部节点 | **平滑生长与分离动效**：Stage 1 派发时子圆球从 Lead 圆球位置平滑分离到位，边按 `growthProgress` 伸展；严格遵循 `UI_RULES.md` `MotionPolicy`（支持 `prefers-reduced-motion`，无无限 shimmer，无 token 抖动） |
| **点击节点详情与完成收拢（`OrganizationBottomSheet.kt`）** | 桌面端侧栏，移动端偏拥挤 | 点击任意圆球弹出**毛玻璃底部抽屉（移动端）/ 侧滑抽屉（桌面端）**，仅展示该角色的简洁任务摘要、当前动作与过程日志；整轮完成后画布平滑收拢为顶部极简**彩色圆点状态摘要条**（可随时点击展开回看），把主屏完全让给财报底稿 |

---

## 三、分步实施与验收标准

1. **Step 1（后端自动路由）**：实现 `research_earnings_routing.py` 并接入主研究启动入口；补齐单测覆盖“美股单公司财报自动命中 / 多公司对比不误触发 / 宏观与 A 港股不误触发 / 显式禁用尊重”。
2. **Step 2（`OrgGraphPanel.tsx` 视觉重构 + `TopicPage.tsx` 净化）**：按上述 6 项规范重写圆形渐变球体组织图与毛玻璃徽章/抽屉，移除底部“待启动”占位卡片，统一底部输入栏为中场调度/追问二合一。
3. **Step 3（双端真机比例浏览器截图验收）**：
   - 运行全量 API + Web 回归测试、`tsc --noEmit`、`vite build`；
   - 使用 Chrome 无头浏览器同时截取 **桌面端（`1440×900`）** 与 **手机竖屏（`390×844`）** 的实时并行组织图、点击圆球后的毛玻璃日志抽屉、完成收拢态 + Memo 底稿视图，确认 **0 CSP 违规、0 布局重叠、0 双入口割裂**。
4. **Step 4（提交、推送并部署上线）**：验收通过后提交至 `master`，执行 `git push origin master` 并按 `aws-runtime` 发布上线与线上冒烟验证。

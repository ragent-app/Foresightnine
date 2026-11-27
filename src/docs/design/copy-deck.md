# Approved interface copy

Product positioning is defined in the [product plan](../product/fusion-finance-ai-macro-plan-2026-11-11.md). The following approved labels describe the research and simulation interface.

## Research and simulation disclosures

- `公开研究模式 · 不连接券商 · 不托管资金`
- `本产品用于情景研究与教育，不构成投资建议；不连接券商、不下单、不托管资金。`
- `情景模拟，不是预测或收益承诺。`
- `概率是可修订判断，不是事实。`

## Probability labels

- `前提成立概率 P(H)`
- `世界线条件概率 P(W | H)`
- `联合概率 P(H ∩ W)`
- `区间优先 · 当前未校准`
- `model_estimate_unvalidated`

## Evidence labels

- `A 原始或权威公开来源`
- `B 高质量二手来源`
- `C 二手评论或专家访谈`
- `D 私有声明`
- `E 模型推断`
- `A–E 表示来源与可核验性，不代表结论正确。`
- `用户身份或业内经验不是公开证据；可作为私有前提单独标记。`

## Portfolio labels

- `FM Raw（实验层）`
- `Public Default（公开层）`
- `本地计算`
- `数据截止`
- `查看计算解释`
- `世界线压力测试`
- `组合 NAV（归一化）`
- `历史观测 + 世界线情景模拟`

## Operation and recovery

- `正在连接反方审查…`
- `正在接收回复，可随时取消。`
- `已取消生成；已接收内容和证据仍保留。`
- `连接中断，正在从上次事件恢复…`
- `恢复失败。你可以重试，不会重复提交已确认内容。`
- `请求过于频繁，请在 {retry_after} 后重试。`
- `模型暂时不可用。已保存当前研究，可稍后继续。`
- `响应未通过结构校验，未写入研究档案。`
- `搜索能力不可用。请导入客户端 SearchEvidenceBundle。`
- `当前离线；可查看已完成快照，联网后继续。`

## FM and data states

- `正在验证签名数据快照…`
- `数据快照已过期；不会继续计算。`
- `没有因子通过录取规则，将使用全资产等权回退。`
- `本地计算已取消；中间结果未上传。`
- `设备内存不足，已停止计算并保留研究状态。`
- `金额过小，部分资产无法满足最小交易单位。`
- `汇率时点早于市场数据时点，结果已降级为不可实施。`
- `没有可用历史 NAV；仅显示世界线模拟段。`

## Confirmation below threshold

- Primary: `继续补充证据`
- Caution: `带保留意见确认`
- Explanation: `将保存未解决问题、AI 异议与私有证据声明。`

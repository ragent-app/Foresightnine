# 341 阶段 B 受控验证

冻结任务、来源、标准、预算和模型参数见 `preregistration.json`、`criteria.json` 与 `code-freeze.json`。`code-snapshot/` 保存 72 项实际后端源码（含提示）、迁移和 runner，受哈希清单约束；不包含 Web、环境配置、账户凭据或研究数据，不等于整个项目快照。模型版本由提供方维护，无法保证固定。

首题运行完成但分析阶段留有未决外呼标记，runner 按规则停批；后四题未启动，没有重投。首题保存的授权 checkpoint 为 partial、分析失败、0 事实/结论/计算，**执行未完成，没有可评分研究成果**；不能据此计算质量通过率。只读 PG 对账显示分析返回 provider_configuration_invalid，实际 provider policy 为 3,000/1,500 tokens、20 秒且 model=null，并非预登记配置。冻结代码在此配置错误时于 HTTP 传输前退出，未发模型 HTTP；具体配置错误未持久化。账本 1 次、4,500 tokens 是回退预留的保守记账，不是实际模型用量；费用未知。记录为 `MACRO_CALC-journal.json` 和 `MACRO_CALC-checkpoint-export.json`，后四题无运行记录。

环境预检及前一次列表读取失误见 `environment-preflight.json`，均未提交研究运行或调用模型。来源许可、调用次数、时间和预算以预登记为准；此批无重试、无搜索和自动获取。隔离服务暂保留只读对账。340 为不同冻结版本，不能用其质量结果给本次代码打分。

后续核查：隔离启动误设置 `FORESIGHTNINE_GLM_CODING_MODEL`，固定 GLM Coding 适配器禁止该覆盖，触发配置拒绝。该结论来自启动配置和冻结代码；341 原 marker 与账本未修改。342 作为新的经授权 attempt 独立冻结，详见其评价。341 隔离 API、数据库及临时凭据已清理。

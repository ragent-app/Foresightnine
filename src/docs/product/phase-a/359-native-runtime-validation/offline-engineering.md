# M1 离线工程验证（2026-11-17）

未调用真实模型、搜索或数据服务，未部署。M1 的真实金融研究和质量验收仍未完成。

首次集中组在 `src/apps/api` 执行 `.venv/bin/python -m pytest -q tests/test_research_runtime.py tests/test_research_runtime_api.py tests/test_model_protocol_native.py tests/test_openai_compatible_provider.py tests/test_phase3_providers.py tests/test_provider_structured_capabilities.py tests/test_billing.py tests/test_local_auth.py ../../docs/product/phase-a/359-native-runtime-validation/test_runner.py`：127 项，121 通过，6 项 billing 失败；首次输出未完整落盘。对六项单独复现的原始输出见 [billing 失败记录](offline-billing-failures.txt)。其中 cleanup 测试要求仅凭终态释放未知外呼预留，与当前保守对账语义不一致；其他旧 Challenge 测试使用无法界定容量的 mock 或依赖旧账本结果，不能据此声称全量通过。同步错误状态及旧夹具仍需独立复核。

后续修复后的定向组 `.venv/bin/python -m pytest -q --tb=short tests/test_research_runtime.py tests/test_research_runtime_api.py tests/test_model_protocol_native.py`：29 项通过，原始输出见 [Python 定向结果](offline-final-targeted.txt)。覆盖来源许可、FRED、事实及原生工具反馈，提交失败恢复、累计预算、未知外呼阻断、返回后的原账本结算和协议诊断。恢复接口专项中间输出见 [恢复 API](offline-recovery-api.txt)。

Web `./node_modules/.bin/vitest run src/general-research-client.test.ts`：16 项通过，见 [Web 测试](offline-web-targeted.txt)；`./node_modules/.bin/tsc --noEmit` 通过，见 [类型检查](offline-web-typecheck.txt)。这些离线结果不证明金融研究质量或真实供应商行为。

恢复绑定包含账户模型模式、凭据修订及配置、协议能力、生成参数、提示词和工具定义；平台切换个人凭据及个人凭据旋转的专门 API 测试尚未取得，代码审查不替代该证据。

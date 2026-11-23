# PDF 财报回放

`pdf_replay.py` 使用实际 PDF 字节、`extract_pdf_pages`、正式财报提取和 `read_pages`，无网络或模型。原文及出处、许可、页码和复核数值见 [protocol.md](protocol.md)，逐项原文区间、哈希和输出见 [pdf-replay-results.json](pdf-replay-results.json)。

| 样本 | 期末列绑定 | 金额和范围 | 限制 |
| --- | --- | --- | --- |
| UK DMA 2024–2025 资产负债表摘页 | 2025-03-31、2024-03-31，两列正确 | 资产 273,984 / 279,345，现金 3,231 / 2,619；单位正确识别英镑百万 | 摘页未含附注正文，带附注行不声称附注已读；机构主体只覆盖此页 |
| 自编跨页表 | 2025-12-31、2024-12-31，两列正确 | 主表与续页分开引用；从 PDF 几何定位被拆开的年份和日期 | 几何恢复仅在散件唯一、同一水平带且紧邻时执行，布局和解读标为未独立验证；无原 PDF、OCR 或冲突时仍可弃答 |

已检查的 4/4 列期间正确，0 个错误绑定，样本仅两份，非一般 PDF 的解析率。额外构造反事实：续页列顺序反转时续页数值全部撤回；两个同号附注不绑定；重复几何日期候选不解析。读取第三页前附注未绑定，读后返回同版本、页码与正文范围的 `located_unverified`，不宣称附注语义成立。当前仅绑定唯一、已读、非 OCR 的单行附注，复杂、多行、跨页及同号附注仍需独立处理。

财报及 PDF 几何旧测试合计 23 通过、1 失败；失败项 `test_geometry_candidate_reaches_analysis_with_original_offsets` 为模拟模型输出不符合当前 research draft 合同，在独立 `82900b4` 基线同项以 `InvalidAnalysis` 复现。新回放与上述反事实通过。运行：`PYTHONPATH=src/apps/api src/apps/api/.venv/bin/python src/docs/product/evidence-iteration-acceptance/analysis/pdf_replay.py`。

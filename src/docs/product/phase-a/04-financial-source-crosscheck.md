# A4 财务来源及口径交叉检查

检查日期：2026-11-11。以下为对公开原文与候选事实的机器辅助核对，不是独立人工复核，也不确认使用、导出或商业授权。原始网页是此次读取结果，不能据此证明历史文件未发生修订。

| 样本 | 原文定位及本次核对 | 评估注意点 |
| --- | --- | --- |
| Microsoft FY2023 | [10-K，Segment Results of Operations](https://www.sec.gov/Archives/edgar/data/789019/000095017023035122/msft-20230630.htm)：More Personal Computing FY2022 为 59,941、FY2023 为 54,734；合并收入分别为 198,270、211,915；单位均为百万美元。 | 分部减少 5,207；不能由总收入增长推出所有分部增长。 |
| Apple FY2024 | [公司财务报表，第 1 页，十二个月列](https://www.apple.com/newsroom/pdfs/fy2024-q4/FY24_Q4_Consolidated_Financial_Statements.pdf)：FY2023/FY2024 产品 298,085/294,866，服务 85,200/96,169，总净销售 383,285/391,035；单位百万美元。 | 报表还包含三个月列；不得误用。财年末分别为 2023-09-30、2024-09-28，且与 Microsoft 的 6 月末不同。 |
| Walmart FY2024 | [10-K，Consolidated Results of Operations 和 Consolidated Statements of Income](https://www.sec.gov/Archives/edgar/data/104169/000010416924000056/wmt-20240131.htm)：FY2023/FY2024 总收入 611,289/648,125；FY2024 净销售 642,637、会员及其他收入 5,488、营业利润 27,012；单位百万美元。 | 公司表中营业利润占净销售比例按 1 位小数展示；按总收入与净销售两种分母计算，分别约 4.17% 与 4.20%，不能将未指定分母的问题只按一种答案判分。 |
| Coca-Cola FY2024 | [10-K，Consolidated Statements of Income](https://investors.coca-colacompany.com/filings-reports/annual-filings-10-k/content/0000021344-25-000011/ko-20241231.htm)：2023/2024 净营业收入 45,754/47,061，单位百万美元，年度截至 12 月 31 日。 | 两位小数同比约 2.86%；与财年截至 1 月 31 日的 Walmart 不能视为同一日历期间。 |

本次用 Python `Decimal` 对候选快照的差额与比例做了独立算术复核：Microsoft 分部 −5,207、服务及其他 +21,678、FY2024 总收入 +33,207；Apple 产品 −3,219、服务 +10,969、总净销售 +7,750（均为百万美元）。Walmart 会员及其他收入与净销售之和等于总收入，Walmart 总收入同比约 6.03%、Coca-Cola 同比约 2.86%。原文逐项核对和数值计算是不同检查，算术一致本身不能证明来源准确。

H02 的原题未指定利润率分母；据公司原文检查已在 [候选题版本记录](04-independent-test-candidates.md)中修订并保留旧版。其余题目仍须由真实复核人审定、核对来源授权并记录耗时；这些检查没有产生人工作答基线或模型评估结果。

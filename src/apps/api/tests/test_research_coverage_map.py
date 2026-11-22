"""Tests for the run coverage and gap map (I4). Fixtures are trimmed excerpts of the 365 real-model run exports
(Q-A1/Q-A3/Q-B3 ended mid-run; Q-B1 completed; Q-A2 never obtained data). Model events, rationales and previews are
dropped; tool events, failed accesses, read ranges, acquired actions and the research clock are verbatim."""
from __future__ import annotations

import json

import pytest

from app.research_coverage_map import build_coverage_map, model_summary, trading_days_between, ui_view

FIXTURES = json.loads(r'''{"Q-A1":{"state":{"events":[{"kind":"tool","name":"historical_prices","status":"acquired","summary":{"kind":"historical_prices","title":"ETF:QQQ daily close (2025-11-19 to 2026-11-19)","status":"acquired","end_date":"2026-11-19","start_date":"2025-11-19","instrument_id":"ETF:QQQ"}},{"kind":"tool","name":"historical_prices","status":"acquired","summary":{"kind":"historical_prices","title":"ETF:SPY daily close (2025-11-19 to 2026-11-19)","status":"acquired","end_date":"2026-11-19","start_date":"2025-11-19","instrument_id":"ETF:SPY"}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":12825,"start":0,"title":"ETF:QQQ daily close (2025-11-1","source_id":"source_1f999e1498834802bbc4533d6dbc8834","total_characters":12825},{"end":12854,"start":0,"title":"ETF:SPY daily close (2025-11-1","source_id":"source_9294b16ef0074b4aa65404a448c42f27","total_characters":12854}],"read_count":2}},{"kind":"tool","name":"record_facts","status":"completed","summary":{"kind":"record_facts","recorded_count":17}},{"kind":"tool","name":"news_search","status":"completed","summary":{"kind":"news_search","found_count":9,"denied_count":6,"eligible_count":3},"denied_count":6,"denied_reasons":{"invalid_lead":0,"redirect_link":6,"source_not_authorized":0}},{"kind":"tool","name":"acquire_sources","status":"completed","summary":{"kind":"acquire_sources","items":[{"host":"www.ubp.com","title":"每周展望—美国中期选举撼动AI板块，引发资金涌向避险资产","reason":null,"status":"acquired","total_characters":7002},{"host":"www.guancha.cn","title":"美联储警告：美国AI热潮若落空，美股恐大跌","reason":"source_timeout","status":"failed","total_characters":null},{"host":"www.kvbplus.com","title":"AI支出担忧遇上苹果、AMD财报、美国中期选举与非农数据 -","reason":"http_forbidden_or_challenge","status":"failed","total_characters":null}],"failed_count":2,"acquired_count":1}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":7002,"start":0,"title":"每周展望—美国中期选举撼动AI板块，引发资金涌向避险资产","source_id":"source_ef2e35a81f187464a7b775e0e21c02c3","total_characters":7002}],"read_count":1}},{"kind":"tool","name":"calculate_financials","status":"completed","summary":{"kind":"calculate_financials","calculated_count":13}},{"kind":"tool","name":"record_facts","status":"completed","summary":{"kind":"record_facts","recorded_count":8}},{"kind":"tool","name":"create_chart","status":"created","summary":{"kind":"create_chart","title":"QQQ与SPY近一年走势对比（2025-11-19=100）","chart_id":"chart-e69a155cbb224207bd3a182df4f1af8e","chart_type":"price_series","point_count":504,"series_count":2}},{"kind":"tool","name":"create_chart","status":"created","summary":{"kind":"create_chart","title":"纳斯达克近期走势传导机制与未来情景（宏观→行业→资产）","chart_id":"chart-9cbec781c1f64b3993da13cbd67cb881","chart_type":"causal_graph","point_count":12,"series_count":10}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":1600,"start":1300,"title":"每周展望—美国中期选举撼动AI板块，引发资金涌向避险资产","source_id":"source_ef2e35a81f187464a7b775e0e21c02c3","total_characters":7002},{"end":2750,"start":2300,"title":"每周展望—美国中期选举撼动AI板块，引发资金涌向避险资产","source_id":"source_ef2e35a81f187464a7b775e0e21c02c3","total_characters":7002},{"end":3120,"start":2900,"title":"每周展望—美国中期选举撼动AI板块，引发资金涌向避险资产","source_id":"source_ef2e35a81f187464a7b775e0e21c02c3","total_characters":7002},{"end":3550,"start":3200,"title":"每周展望—美国中期选举撼动AI板块，引发资金涌向避险资产","source_id":"source_ef2e35a81f187464a7b775e0e21c02c3","total_characters":7002},{"end":5800,"start":5400,"title":"每周展望—美国中期选举撼动AI板块，引发资金涌向避险资产","source_id":"source_ef2e35a81f187464a7b775e0e21c02c3","total_characters":7002}],"read_count":5}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":2320,"start":2140,"title":"每周展望—美国中期选举撼动AI板块，引发资金涌向避险资产","source_id":"source_ef2e35a81f187464a7b775e0e21c02c3","total_characters":7002}],"read_count":1}},{"code":"provider_unavailable","kind":"model_failure"}],"user":{"research_clock":{"captured_at":"2026-11-20T06:16:00Z","utc_date":"2026-11-20","exchanges":[{"exchange":"NYSE","local_date":"2026-11-20","session_status":"pre_open","last_completed_session":"2026-11-19"},{"exchange":"NASDAQ","local_date":"2026-11-20","session_status":"pre_open","last_completed_session":"2026-11-19"},{"exchange":"SSE","local_date":"2026-11-20","session_status":"open","last_completed_session":"2026-11-19"}]},"available_historical_price_instruments":["ETF:QQQ","ETF:ONEQ","ETF:SPY"]},"failed_source_accesses":[{"url":"https://www.guancha.cn/GuoJi%C2%B7ZhanLue/2026_11_17_908614.shtml","host":"www.guancha.cn","reason":"source_timeout","status":"failed"},{"url":"https://www.kvbplus.com/en/insights/market-analysis/107254","host":"www.kvbplus.com","reason":"http_forbidden_or_challenge","status":"failed"}],"read_ranges":[{"end":12825,"start":0,"source_id":"source_1f999e1498834802bbc4533d6dbc8834"},{"end":12854,"start":0,"source_id":"source_9294b16ef0074b4aa65404a448c42f27"},{"end":7002,"start":0,"source_id":"source_ef2e35a81f187464a7b775e0e21c02c3"},{"end":1600,"start":1300,"source_id":"source_ef2e35a81f187464a7b775e0e21c02c3"},{"end":2750,"start":2300,"source_id":"source_ef2e35a81f187464a7b775e0e21c02c3"},{"end":3120,"start":2900,"source_id":"source_ef2e35a81f187464a7b775e0e21c02c3"},{"end":3550,"start":3200,"source_id":"source_ef2e35a81f187464a7b775e0e21c02c3"},{"end":5800,"start":5400,"source_id":"source_ef2e35a81f187464a7b775e0e21c02c3"},{"end":2320,"start":2140,"source_id":"source_ef2e35a81f187464a7b775e0e21c02c3"}],"acquired_actions":{"historical_prices|ETF:QQQ|2025-11-19|2026-11-19":["source_1f999e1498834802bbc4533d6dbc8834"],"historical_prices|ETF:SPY|2025-11-19|2026-11-19":["source_9294b16ef0074b4aa65404a448c42f27"],"acquire|https://www.ubp.com/zh/news-insights/newsroom/ai-0":["source_ef2e35a81f187464a7b775e0e21c02c3"]},"pending":null},"sources":[{"title":"ETF:QQQ daily close (2025-11-19 to 2026-11-19)","source_id":"source_1f999e1498834802bbc4533d6dbc8834","obtained_at":"2026-11-20T06:17:03.096489+00:00","initial_prompt_coverage":{"method":"initial_prompt_ranges_v1","status":"complete","text_characters":12825,"selected_characters":12825}},{"title":"ETF:SPY daily close (2025-11-19 to 2026-11-19)","source_id":"source_9294b16ef0074b4aa65404a448c42f27","obtained_at":"2026-11-20T06:17:03.485731+00:00","initial_prompt_coverage":{"method":"initial_prompt_ranges_v1","status":"complete","text_characters":12854,"selected_characters":12854}},{"url":"https://www.ubp.com/zh/news-insights/newsroom/ai-0","title":"每周展望—美国中期选举撼动AI板块，引发资金涌向避险资产","source_id":"source_ef2e35a81f187464a7b775e0e21c02c3","obtained_at":"2026-11-20T06:19:16.688282+00:00","initial_prompt_coverage":{"method":"initial_prompt_ranges_v1","status":"complete","text_characters":7002,"selected_characters":7002}}],"facts":[],"gaps":[{"code":"provider_error"}]},"Q-A2":{"state":{"events":[{"code":"provider_timeout","kind":"model_failure"}],"user":{"research_clock":{"captured_at":"2026-11-20T07:05:00Z","utc_date":"2026-11-20","exchanges":[{"exchange":"NYSE","local_date":"2026-11-20","session_status":"pre_open","last_completed_session":"2026-11-19"},{"exchange":"NASDAQ","local_date":"2026-11-20","session_status":"pre_open","last_completed_session":"2026-11-19"},{"exchange":"SSE","local_date":"2026-11-20","session_status":"closed","last_completed_session":"2026-11-19"}]},"available_historical_price_instruments":["ETF:QQQ","ETF:ONEQ","ETF:SPY"]},"failed_source_accesses":null,"read_ranges":[],"acquired_actions":null,"pending":null},"sources":[],"facts":[],"gaps":[{"code":"provider_timeout"}]},"Q-A3":{"state":{"events":[{"kind":"tool","name":"historical_prices","status":"acquired","summary":{"kind":"historical_prices","title":"ETF:QQQ daily close (2025-11-19 to 2026-11-19)","status":"acquired","end_date":"2026-11-19","start_date":"2025-11-19","instrument_id":"ETF:QQQ"}},{"kind":"tool","name":"historical_prices","status":"acquired","summary":{"kind":"historical_prices","title":"ETF:SPY daily close (2025-11-19 to 2026-11-19)","status":"acquired","end_date":"2026-11-19","start_date":"2025-11-19","instrument_id":"ETF:SPY"}},{"kind":"tool","name":"historical_prices","status":"acquired","summary":{"kind":"historical_prices","title":"ETF:ONEQ daily close (2025-11-19 to 2026-11-19)","status":"acquired","end_date":"2026-11-19","start_date":"2025-11-19","instrument_id":"ETF:ONEQ"}},{"kind":"tool","name":"news_search","status":"completed","summary":{"kind":"news_search","found_count":9,"denied_count":6,"eligible_count":3},"denied_count":6,"denied_reasons":{"invalid_lead":0,"redirect_link":6,"source_not_authorized":0}},{"kind":"tool","name":"read_sources","status":"invalid"},{"kind":"tool","name":"acquire_sources","status":"completed","summary":{"kind":"acquire_sources","items":[{"host":"tickeron.com","title":"Invesco QQQ Trust (QQQ): AI Sp","reason":"http_forbidden_or_challenge","status":"failed","total_characters":null},{"host":"www.top1markets.com","title":"QQQ Stock After the November A","reason":null,"status":"acquired","total_characters":9569}],"failed_count":1,"acquired_count":1}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":12825,"start":0,"title":"ETF:QQQ daily close (2025-11-1","source_id":"source_195c2c4ec5eb4ac4996f141f32444ab3","total_characters":12825},{"end":12854,"start":0,"title":"ETF:SPY daily close (2025-11-1","source_id":"source_27eaac9b2e724608a75253176155520c","total_characters":12854},{"end":12950,"start":0,"title":"ETF:ONEQ daily close (2025-11-","source_id":"source_3ca3f3ae6fad4c6c8f69d47b484e6542","total_characters":12950}],"read_count":3}},{"kind":"tool","name":"record_facts","status":"completed","summary":{"kind":"record_facts","recorded_count":17}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":9569,"start":0,"title":"QQQ Stock After the November A","source_id":"source_79a5e1bae332472b80d4837f86fb3afe","total_characters":9569}],"read_count":1}},{"kind":"tool","name":"news_search","status":"completed","summary":{"kind":"news_search","found_count":8,"denied_count":5,"eligible_count":3},"denied_count":5,"denied_reasons":{"invalid_lead":0,"redirect_link":5,"source_not_authorized":0}},{"kind":"tool","name":"acquire_sources","reason":"http_forbidden_or_challenge","status":"unavailable","summary":{"kind":"acquire_sources","items":[{"host":"www.reuters.com","title":"The 'AI dollar' has arrived. I","reason":"http_forbidden_or_challenge","status":"failed","total_characters":null}],"failed_count":1,"acquired_count":0}},{"kind":"tool","name":"calculate_financials","status":"completed","summary":{"kind":"calculate_financials","calculated_count":14}},{"kind":"tool","name":"calculate_financials","status":"invalid"},{"kind":"tool","name":"calculate_financials","status":"completed","summary":{"kind":"calculate_financials","calculated_count":1}},{"kind":"tool","name":"create_chart","status":"created","summary":{"kind":"create_chart","title":"纳斯达克前瞻：宏观→行业→资产传导与三情景分支","chart_id":"chart-c91b5527b2f841ab857f4974028491b8","chart_type":"causal_graph","point_count":12,"series_count":11}},{"kind":"tool","name":"create_chart","status":"created","summary":{"kind":"create_chart","title":"QQQ vs SPY 近一年走势对比（归一化基期=100，截至2026-11-19收盘）","chart_id":"chart-0bc82d40989746d6a9ba1667509cb276","chart_type":"price_series","point_count":504,"series_count":2}},{"code":"provider_timeout","kind":"model_failure"}],"user":{"research_clock":{"captured_at":"2026-11-20T07:16:00Z","utc_date":"2026-11-20","exchanges":[{"exchange":"NYSE","local_date":"2026-11-20","session_status":"pre_open","last_completed_session":"2026-11-19"},{"exchange":"NASDAQ","local_date":"2026-11-20","session_status":"pre_open","last_completed_session":"2026-11-19"},{"exchange":"SSE","local_date":"2026-11-20","session_status":"closed","last_completed_session":"2026-11-19"}]},"available_historical_price_instruments":["ETF:QQQ","ETF:ONEQ","ETF:SPY"]},"failed_source_accesses":[{"url":"https://tickeron.com/blogs/invesco-qqq-trust-qqq-ai-spending-rate-sensitivity-and-index-outlook-21183","host":"tickeron.com","reason":"http_forbidden_or_challenge","status":"failed"},{"url":"https://www.reuters.com/commentary/reuters-open-interest/ai-dollar-has-arrived-its-direction-hasnt-2026-11-19","host":"www.reuters.com","reason":"http_forbidden_or_challenge","status":"failed"}],"read_ranges":[{"end":12825,"start":0,"source_id":"source_195c2c4ec5eb4ac4996f141f32444ab3"},{"end":12854,"start":0,"source_id":"source_27eaac9b2e724608a75253176155520c"},{"end":12950,"start":0,"source_id":"source_3ca3f3ae6fad4c6c8f69d47b484e6542"},{"end":9569,"start":0,"source_id":"source_79a5e1bae332472b80d4837f86fb3afe"}],"acquired_actions":{"historical_prices|ETF:QQQ|2025-11-19|2026-11-19":["source_195c2c4ec5eb4ac4996f141f32444ab3"],"historical_prices|ETF:SPY|2025-11-19|2026-11-19":["source_27eaac9b2e724608a75253176155520c"],"historical_prices|ETF:ONEQ|2025-11-19|2026-11-19":["source_3ca3f3ae6fad4c6c8f69d47b484e6542"],"acquire|https://www.top1markets.com/pt/news/qqq-stock-nasdaq-100-selloff-ai-capex-nvidia-earnings":["source_79a5e1bae332472b80d4837f86fb3afe"]},"pending":null},"sources":[],"facts":[],"gaps":[]},"Q-B1":{"state":{"events":[{"kind":"tool","name":"news_search","status":"completed","summary":{"kind":"news_search","found_count":7,"denied_count":4,"eligible_count":3},"denied_count":4,"denied_reasons":{"invalid_lead":0,"redirect_link":4,"source_not_authorized":0}},{"kind":"tool","name":"news_search","status":"completed","summary":{"kind":"news_search","found_count":8,"denied_count":5,"eligible_count":3},"denied_count":5,"denied_reasons":{"invalid_lead":0,"redirect_link":5,"source_not_authorized":0}},{"kind":"tool","name":"acquire_sources","status":"completed","summary":{"kind":"acquire_sources","items":[{"host":"www.wsj.com","title":"Stock Market News, Nov. 19, 20","reason":"http_forbidden_or_challenge","status":"failed","total_characters":null},{"host":"finance.yahoo.com","title":"When is the Anthropic IPO? The","reason":null,"status":"acquired","total_characters":3953},{"host":"www.cnbc.com","title":"Anthropic IPO guide: Everythin","reason":null,"status":"acquired","total_characters":11545},{"host":"www.reuters.com","title":"Anthropic's IPO prospectus sho","reason":"http_forbidden_or_challenge","status":"failed","total_characters":null},{"host":"finance.yahoo.com","title":"Anthropic IPO: How to buy a co","reason":null,"status":"acquired","total_characters":14236}],"failed_count":2,"acquired_count":4}},{"kind":"tool","name":"news_search","status":"completed","summary":{"kind":"news_search","found_count":9,"denied_count":6,"eligible_count":3},"denied_count":6,"denied_reasons":{"invalid_lead":0,"redirect_link":6,"source_not_authorized":0}},{"kind":"tool","name":"acquire_sources","status":"completed","summary":{"kind":"acquire_sources","items":[{"host":"www.cnbc.com","title":"Anthropic IPO takeaways: ANT d","reason":null,"status":"acquired","total_characters":46406},{"host":"www.stocktitan.net","title":"Anthropic IPO Live: ANT's Firs","reason":null,"status":"acquired","total_characters":35997},{"host":"finance.yahoo.com","title":"Anthropic stock closes up 17% ","reason":null,"status":"acquired","total_characters":14214}],"failed_count":0,"acquired_count":3}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":14214,"start":0,"title":"Anthropic stock closes up 17% ","source_id":"source_7fb80822e6eb97830c6d08a1944200d7","total_characters":14214}],"read_count":1}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":4700,"start":1800,"title":"Anthropic IPO takeaways: ANT d","source_id":"source_7b095037fb57e4b051c6011ebd6fe5a4","total_characters":46406},{"end":13150,"start":12700,"title":"Anthropic IPO takeaways: ANT d","source_id":"source_7b095037fb57e4b051c6011ebd6fe5a4","total_characters":46406},{"end":4750,"start":2900,"title":"Anthropic IPO Live: ANT's Firs","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc","total_characters":35997},{"end":16450,"start":15900,"title":"Anthropic IPO Live: ANT's Firs","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc","total_characters":35997}],"read_count":4}},{"kind":"tool","name":"sec_company_concept","reason":"identity_not_unique","status":"unknown","summary":{"tag":"RevenueFromContractWithCustomerExcludingAssessedTax","kind":"sec_company_concept","unit":"USD","title":null,"entity":"ANT","status":"unknown","taxonomy":"us-gaap","total_characters":null}},{"kind":"tool","name":"sec_company_concept","reason":"identity_not_unique","status":"unknown","summary":{"tag":"RevenueFromContractWithCustomerExcludingAssessedTax","kind":"sec_company_concept","unit":"USD","title":null,"entity":"Anthropic PBC","status":"unknown","taxonomy":"us-gaap","total_characters":null}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":5000,"start":4740,"title":"Anthropic IPO Live: ANT's Firs","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc","total_characters":35997}],"read_count":1}},{"kind":"tool","name":"record_facts","status":"completed","summary":{"kind":"record_facts","recorded_count":14}},{"kind":"tool","name":"calculate_financials","status":"invalid"},{"kind":"tool","name":"calculate_financials","status":"completed","summary":{"kind":"calculate_financials","calculated_count":4}},{"kind":"tool","name":"create_chart","status":"created","summary":{"kind":"create_chart","title":"Anthropic (ANT) IPO首日价格点 vs 发行价 (2026-11-19)","chart_id":"chart-74cd815cb6d84d11a91767cb660fa808","chart_type":"bar","point_count":6,"series_count":1}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":3780,"start":3590,"title":"Anthropic IPO Live: ANT's Firs","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc","total_characters":35997}],"read_count":1}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":5370,"start":5060,"title":"Anthropic stock closes up 17% ","source_id":"source_7fb80822e6eb97830c6d08a1944200d7","total_characters":14214}],"read_count":1}},{"kind":"submission","status":"completed","stopping_reason":"首日（2026-11-19）核心事实已由三个独立来源交叉验证，并经确定性计算复核媒体百分比；今日（11-20）美股尚未开盘、ANT无成交数据，且可用价格工具不含ANT个股、SEC XBRL实体无法唯一识别——可用验证路径已覆盖或穷尽，继续检索边际价值低。"}],"user":{"research_clock":{"captured_at":"2026-11-20T06:42:00Z","utc_date":"2026-11-20","exchanges":[{"exchange":"NYSE","local_date":"2026-11-20","session_status":"pre_open","last_completed_session":"2026-11-19"},{"exchange":"NASDAQ","local_date":"2026-11-20","session_status":"pre_open","last_completed_session":"2026-11-19"},{"exchange":"SSE","local_date":"2026-11-20","session_status":"open","last_completed_session":"2026-11-19"}]},"available_historical_price_instruments":["ETF:QQQ","ETF:ONEQ","ETF:SPY"]},"failed_source_accesses":[{"url":"https://www.wsj.com/livecoverage/anthropic-ipo-stock-market-today-11-19-2026","host":"www.wsj.com","reason":"http_forbidden_or_challenge","status":"failed"},{"url":"https://www.reuters.com/business/finance/anthropics-ipo-prospectus-shows-sweeping-ai-vision-surging-costs-2026-09-28","host":"www.reuters.com","reason":"http_forbidden_or_challenge","status":"failed"}],"read_ranges":[{"end":14214,"start":0,"source_id":"source_7fb80822e6eb97830c6d08a1944200d7"},{"end":4700,"start":1800,"source_id":"source_7b095037fb57e4b051c6011ebd6fe5a4"},{"end":13150,"start":12700,"source_id":"source_7b095037fb57e4b051c6011ebd6fe5a4"},{"end":4750,"start":2900,"source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"},{"end":16450,"start":15900,"source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"},{"end":5000,"start":4740,"source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"},{"end":3780,"start":3590,"source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"},{"end":5370,"start":5060,"source_id":"source_7fb80822e6eb97830c6d08a1944200d7"}],"acquired_actions":{"acquire|https://leverageshares.com/us/anthropic-ipo-tracker":["source_0f6d39b3597c73f08a980e1932265107"],"acquire|https://www.cnbc.com/2026/11/16/anthropic-ipo-date-price-range.html":["source_7541be46bb578c81b4e03f71f14f0443"],"acquire|https://www.cnbc.com/2026/11/19/anthropic-ipo-ant-live-updates.html":["source_7b095037fb57e4b051c6011ebd6fe5a4"],"acquire|https://www.stocktitan.net/articles/anthropic-ipo-live-ant-first-trading-day-2026":["source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"],"acquire|https://finance.yahoo.com/markets/article/when-is-the-anthropic-ipo-the-stock-could-start-trading-as-soon-as-nov-16-132727631.html":["source_405fe66eaeb0b28292920935dfe45992"],"acquire|https://finance.yahoo.com/personal-finance/investing/article/anthropic-ipo-how-to-buy-a-companys-stock-when-it-ipos-201237173.html":["source_3c162e34ffd7e251b85ced60ca007b06"],"acquire|https://finance.yahoo.com/technology/live/anthropic-ipo-live-anthropic-stock-begins-trading-in-record-81b-raise-with-15t-valuation-192410598.html":["source_7fb80822e6eb97830c6d08a1944200d7"]},"pending":null},"sources":[{"url":"https://finance.yahoo.com/markets/article/when-is-the-anthropic-ipo-the-stock-could-start-trading-as-soon-as-nov-16-132727631.html","title":"When is the Anthropic IPO? The stock could start trading as","source_id":"source_405fe66eaeb0b28292920935dfe45992","obtained_at":"2026-11-20T06:44:22.807502+00:00","initial_prompt_coverage":{"method":"initial_prompt_ranges_v1","status":"unknown","text_characters":3953}},{"url":"https://www.cnbc.com/2026/11/16/anthropic-ipo-date-price-range.html","title":"Anthropic IPO guide: Everything you need to know","source_id":"source_7541be46bb578c81b4e03f71f14f0443","obtained_at":"2026-11-20T06:44:24.252734+00:00","initial_prompt_coverage":{"method":"initial_prompt_ranges_v1","status":"unknown","text_characters":11545}},{"url":"https://finance.yahoo.com/personal-finance/investing/article/anthropic-ipo-how-to-buy-a-companys-stock-when-it-ipos-201237173.html","title":"Anthropic IPO: How to buy a company's stock when it IPOs","source_id":"source_3c162e34ffd7e251b85ced60ca007b06","obtained_at":"2026-11-20T06:44:25.614579+00:00","initial_prompt_coverage":{"method":"initial_prompt_ranges_v1","status":"unknown","text_characters":14236}},{"url":"https://leverageshares.com/us/anthropic-ipo-tracker","title":"Anthropic IPO Tracker: Date, Valuation and S-1 Status","source_id":"source_0f6d39b3597c73f08a980e1932265107","obtained_at":"2026-11-20T06:44:26.226607+00:00","initial_prompt_coverage":{"method":"initial_prompt_ranges_v1","status":"unknown","text_characters":20871}},{"url":"https://www.cnbc.com/2026/11/19/anthropic-ipo-ant-live-updates.html","title":"Anthropic IPO takeaways: ANT debuts with $1.8 trillion","source_id":"source_7b095037fb57e4b051c6011ebd6fe5a4","obtained_at":"2026-11-20T06:44:50.439055+00:00","initial_prompt_coverage":{"method":"initial_prompt_ranges_v1","status":"partial","text_characters":46406,"selected_characters":3350}},{"url":"https://www.stocktitan.net/articles/anthropic-ipo-live-ant-first-trading-day-2026","title":"Anthropic IPO Live: ANT's First Day of Trading, Updated","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc","obtained_at":"2026-11-20T06:44:50.850473+00:00","initial_prompt_coverage":{"method":"initial_prompt_ranges_v1","status":"partial","text_characters":35997,"selected_characters":2650}},{"url":"https://finance.yahoo.com/technology/live/anthropic-ipo-live-anthropic-stock-begins-trading-in-record-81b-raise-with-15t-valuation-192410598.html","title":"Anthropic stock closes up 17% from initial price for $1.27T","source_id":"source_7fb80822e6eb97830c6d08a1944200d7","obtained_at":"2026-11-20T06:44:51.806431+00:00","initial_prompt_coverage":{"method":"initial_prompt_ranges_v1","status":"complete","text_characters":14214,"selected_characters":14214}}],"facts":[{"fact_id":"fact-3afca9773adb441c90ddffde406b4105","label":"ANT IPO发行价","value":"83","unit":"USD per share","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"},{"fact_id":"fact-501b40383228407a9502fc685e488fc2","label":"ANT首日首笔成交价 (11:48 a.m. ET)","value":"111.5","unit":"USD per share","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"},{"fact_id":"fact-35c707526a1e41c8a14d40ae731f902d","label":"ANT首日盘中最高价","value":"114.99","unit":"USD per share","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"},{"fact_id":"fact-e9f2a3dc65fb4a65a1cf2c884648b218","label":"ANT首日盘中最低价 (收盘前)","value":"92.72","unit":"USD per share","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"},{"fact_id":"fact-803a881f73234ad0bcfe1c7e0e405d9e","label":"ANT首日收盘价 (4:00 p.m. ET)","value":"97.17","unit":"USD per share","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"},{"fact_id":"fact-31253e6a502a445caabc0eaa40934181","label":"ANT首日盘后最后一笔成交价 (7:59 p.m. ET)","value":"92.6","unit":"USD per share","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"},{"fact_id":"fact-f0ce6161171845d094755f49225d5f1a","label":"IPO募资总额","value":"81.3","unit":"billions USD","source_id":"source_7fb80822e6eb97830c6d08a1944200d7"},{"fact_id":"fact-f564780804c44fce99d76abb38a28be1","label":"IPO发行估值","value":"1.5","unit":"trillions USD","source_id":"source_7fb80822e6eb97830c6d08a1944200d7"},{"fact_id":"fact-94639da4785b4c449710896e44093eab","label":"首日收盘全稀释市值","value":"1.8","unit":"trillions USD","source_id":"source_7b095037fb57e4b051c6011ebd6fe5a4"},{"fact_id":"fact-69725ff1cd4148b38e684c9540293f37","label":"首日成交量","value":"846","unit":"million shares","source_id":"source_7b095037fb57e4b051c6011ebd6fe5a4"},{"fact_id":"fact-c857f0832b2d481b88c10048d634cd90","label":"首日常规时段成交量 (StockTitan口径)","value":"832","unit":"million shares","source_id":"source_52ea3a5fc0f6b4f4916293a3a4ec9bbc"},{"fact_id":"fact-83222c1f3a1d4d83b0e881c30204fe18","label":"IPO发行股数","value":"980","unit":"million shares","source_id":"source_7b095037fb57e4b051c6011ebd6fe5a4"},{"fact_id":"fact-0f226e9d44de43c0a4685799ef77b49a","label":"2026年前9个月收入 (招股书)","value":"33.24","unit":"billions USD","source_id":"source_7b095037fb57e4b051c6011ebd6fe5a4"},{"fact_id":"fact-c67e08d52e5b4bdea7ffacfeae6ab0cf","label":"IPO承销银行费用","value":"550","unit":"millions USD","source_id":"source_7b095037fb57e4b051c6011ebd6fe5a4"}],"gaps":[{"code":"model_reported"},{"code":"model_reported"},{"code":"model_reported"},{"code":"model_reported"},{"code":"source_selection_partial"}]},"Q-B3":{"state":{"events":[{"kind":"tool","name":"news_search","status":"completed","summary":{"kind":"news_search","found_count":8,"denied_count":5,"eligible_count":3},"denied_count":5,"denied_reasons":{"invalid_lead":0,"redirect_link":5,"source_not_authorized":0}},{"kind":"tool","name":"news_search","status":"completed","summary":{"kind":"news_search","found_count":8,"denied_count":5,"eligible_count":3},"denied_count":5,"denied_reasons":{"invalid_lead":0,"redirect_link":5,"source_not_authorized":0}},{"kind":"tool","name":"acquire_sources","status":"completed","summary":{"kind":"acquire_sources","items":[{"host":"www.sec.gov","title":"Anthropic, PBC - S-1 (Q2)","reason":"source_unavailable","status":"failed"},{"host":"www.sec.gov","title":"EDGAR Filing Documents for 000","status":"acquired","total_characters":5283},{"host":"www.anthropic.com","title":"Anthropic files S-1 with the S","status":"acquired","total_characters":1569},{"host":"www.cnbc.com","title":"Anthropic IPO guide: Everythin","status":"acquired","total_characters":11545},{"host":"finance.yahoo.com","title":"When is the Anthropic IPO? The","status":"acquired","total_characters":3953}],"failed_count":1,"acquired_count":4}},{"kind":"tool","name":"news_search","status":"completed","summary":{"kind":"news_search","found_count":7,"denied_count":4,"eligible_count":3},"denied_count":4,"denied_reasons":{"invalid_lead":0,"redirect_link":4,"source_not_authorized":0}},{"kind":"tool","name":"acquire_sources","status":"completed","summary":{"kind":"acquire_sources","items":[{"host":"www.anthropic.com","title":"Anthropic prices initial publi","status":"acquired","total_characters":1801},{"host":"www.bbc.com","title":"Surge in share price eases aft","status":"acquired","total_characters":21696},{"host":"www.cnn.com","title":"Anthropic valued at $1.76 tril","reason":"source_too_large","status":"failed"}],"failed_count":1,"acquired_count":2}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":5283,"start":0,"title":"EDGAR Filing Documents for 000","source_id":"source_555e8c8e486e4bfecd7d6b0ab76d729e","total_characters":5283},{"end":1569,"start":0,"title":"Anthropic files S-1 with the S","source_id":"source_9abc2e8b912c73e011efb65ad984d460","total_characters":1569},{"end":1801,"start":0,"title":"Anthropic prices initial publi","source_id":"source_20c1eb06a61473df148815b9caa45138","total_characters":1801},{"end":3953,"start":0,"title":"When is the Anthropic IPO? The","source_id":"source_c52cf3d3e62122096accee2618b18f86","total_characters":3953}],"read_count":4}},{"kind":"tool","name":"read_sources","status":"invalid"},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":21696,"start":0,"title":"Surge in share price eases aft","source_id":"source_89a2a28fa41ff7fdd89181c710d31ed7","total_characters":21696}],"read_count":1}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":11545,"start":0,"title":"Anthropic IPO guide: Everythin","source_id":"source_630c50e5c7c65c4f2fe3ef5780096078","total_characters":11545}],"read_count":1}},{"kind":"tool","name":"historical_prices","status":"acquired","summary":{"kind":"historical_prices","title":"ETF:QQQ daily close (2026-11-13 to 2026-11-19)","status":"acquired","end_date":"2026-11-19","start_date":"2026-11-13","instrument_id":"ETF:QQQ"}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":1801,"start":0,"title":"Anthropic prices initial publi","source_id":"source_20c1eb06a61473df148815b9caa45138","total_characters":1801},{"end":5283,"start":0,"title":"EDGAR Filing Documents for 000","source_id":"source_555e8c8e486e4bfecd7d6b0ab76d729e","total_characters":5283},{"end":1569,"start":0,"title":"Anthropic files S-1 with the S","source_id":"source_9abc2e8b912c73e011efb65ad984d460","total_characters":1569},{"end":3953,"start":0,"title":"When is the Anthropic IPO? The","source_id":"source_c52cf3d3e62122096accee2618b18f86","total_characters":3953}],"read_count":4}},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":21696,"start":0,"title":"Surge in share price eases aft","source_id":"source_89a2a28fa41ff7fdd89181c710d31ed7","total_characters":21696},{"end":11545,"start":0,"title":"Anthropic IPO guide: Everythin","source_id":"source_630c50e5c7c65c4f2fe3ef5780096078","total_characters":11545}],"read_count":2}},{"kind":"tool","name":"read_sources","status":"invalid"},{"kind":"tool","name":"read_sources","status":"completed","summary":{"kind":"read_sources","items":[{"end":252,"start":0,"title":"ETF:QQQ daily close (2026-11-1","source_id":"source_ad861f46a7c746158730a5205bdd3701","total_characters":252}],"read_count":1}},{"kind":"tool","name":"record_facts","status":"completed","summary":{"kind":"record_facts","recorded_count":5}}],"user":{"research_clock":{"captured_at":"2026-11-20T07:31:00Z","utc_date":"2026-11-20","exchanges":[{"exchange":"NYSE","local_date":"2026-11-20","session_status":"pre_open","last_completed_session":"2026-11-19"},{"exchange":"NASDAQ","local_date":"2026-11-20","session_status":"pre_open","last_completed_session":"2026-11-19"},{"exchange":"SSE","local_date":"2026-11-20","session_status":"closed","last_completed_session":"2026-11-20"}]},"available_historical_price_instruments":["ETF:QQQ","ETF:ONEQ","ETF:SPY"]},"failed_source_accesses":[{"url":"https://www.sec.gov/Archives/edgar/data/2130357/000162828026067168/anthropicpbc-sx1.htm","host":"www.sec.gov","reason":"source_unavailable","status":"failed"},{"url":"https://www.cnn.com/2026/11/19/business/live-news/anthropic-ipo","host":"www.cnn.com","reason":"source_too_large","status":"failed"}],"read_ranges":[{"end":5283,"start":0,"source_id":"source_555e8c8e486e4bfecd7d6b0ab76d729e"},{"end":1569,"start":0,"source_id":"source_9abc2e8b912c73e011efb65ad984d460"},{"end":1801,"start":0,"source_id":"source_20c1eb06a61473df148815b9caa45138"},{"end":3953,"start":0,"source_id":"source_c52cf3d3e62122096accee2618b18f86"},{"end":21696,"start":0,"source_id":"source_89a2a28fa41ff7fdd89181c710d31ed7"},{"end":11545,"start":0,"source_id":"source_630c50e5c7c65c4f2fe3ef5780096078"},{"end":252,"start":0,"source_id":"source_ad861f46a7c746158730a5205bdd3701"}],"acquired_actions":{"acquire|https://www.anthropic.com/news/s1-sec":["source_9abc2e8b912c73e011efb65ad984d460"],"historical_prices|ETF:QQQ|2026-11-13|2026-11-19":["source_ad861f46a7c746158730a5205bdd3701"],"acquire|https://www.bbc.com/news/live/c933xxm82mr8t":["source_89a2a28fa41ff7fdd89181c710d31ed7"],"acquire|https://www.cnbc.com/2026/11/16/anthropic-ipo-date-price-range.html":["source_630c50e5c7c65c4f2fe3ef5780096078"],"acquire|https://www.anthropic.com/news/anthropic-prices-initial-public-offering":["source_20c1eb06a61473df148815b9caa45138"],"acquire|https://www.sec.gov/Archives/edgar/data/2130357/000162828026067168/0001628280-26-067168-index.htm":["source_555e8c8e486e4bfecd7d6b0ab76d729e"],"acquire|https://finance.yahoo.com/markets/article/when-is-the-anthropic-ipo-the-stock-could-start-trading-as-soon-as-nov-16-132727631.html":["source_c52cf3d3e62122096accee2618b18f86"]},"pending":{"kind":"model"}},"sources":[],"facts":[],"gaps":[]}}''')


def run(name):
    f = FIXTURES[name]
    return build_coverage_map(f["state"], sources=f["sources"], facts=f["facts"], gaps=f["gaps"])


def cat(coverage, name):
    return next(c for c in coverage["categories"] if c["category"] == name)


def codes(coverage):
    return [s["code"] for s in coverage["not_established"]]


# -- Q-A1: prices through 2026-11-19, news partial, failed accesses, run interrupted by a provider error ---------------

def test_qa1_market_prices_obtained_through_last_completed_session():
    m = run("Q-A1")
    prices = cat(m, "market_prices")
    assert prices["status"] == "obtained" and prices["sources_obtained"] == 2
    assert prices["data_as_of"] == "2026-11-19"
    assert {i["instrument"] for i in prices["instruments"]} == {"ETF:QQQ", "ETF:SPY"}
    st = prices["staleness"]
    assert st["reference_session"] == "2026-11-19" and st["reference_basis"] == "earliest_across_exchanges"
    assert st["trading_days_before_last_session"] == 0 and st["days_before_last_session"] == 0 and st["days_before_clock_date"] == 1
    assert st["is_stale"] is False
    assert [a["tool"] for a in prices["attempts"]] == ["historical_prices", "historical_prices"]
    assert prices["attempts"][0]["target"] == "ETF:QQQ" and prices["attempts"][0]["end_date"] == "2026-11-19"


def test_qa1_news_partial_with_failed_accesses_and_reason_codes():
    m = run("Q-A1")
    news = cat(m, "news_web")
    assert news["status"] == "partial" and news["sources_obtained"] == 1
    assert news["failure_reasons"] == {"source_timeout": 1, "http_forbidden_or_challenge": 1}
    assert {f["host"] for f in news["failures"]} == {"www.guancha.cn", "www.kvbplus.com"}
    assert all(f["url"] for f in news["failures"])                      # runtime rows carry the url; event items are not double counted
    assert news["counts"]["leads_found"] == 9 and news["counts"]["leads_denied"] == 6 and news["counts"]["acquired"] == 1
    assert news["data_as_of"] is None and news["published_dates_unknown"] == 1
    assert "accesses_failed" in {s["code"] for s in news["not_established"]}


def test_qa1_other_categories_and_run_status():
    m = run("Q-A1")
    assert [c["category"] for c in m["categories"]] == ["market_prices", "financial_statements", "filings", "news_web", "macro", "uploads", "other"]
    assert cat(m, "financial_statements")["status"] == "not_attempted" and cat(m, "filings")["status"] == "not_attempted"
    assert m["run"]["status"] == "interrupted" and m["run"]["model_failures"] == {"provider_unavailable": 1}
    assert "run_not_submitted" in codes(m)
    assert m["research_clock"]["latest_completed_session"] == "2026-11-19"
    # macro/uploads/other were never asked for: no noise statements for them
    assert not [s for s in m["not_established"] if s["category"] in ("macro", "uploads", "other")]
    assert m["status_counts"] == {"obtained": 1, "partial": 1, "not_attempted": 5}


def test_qa1_documents_reading_coverage():
    m = run("Q-A1")
    docs = {d["source_id"]: d for d in m["documents"]}
    prices_doc = docs["source_1f999e1498834802bbc4533d6dbc8834"]
    assert prices_doc["chars_total"] == 12825 and prices_doc["chars_read"] == 12825 and prices_doc["read_fraction"] == 1.0
    assert docs["source_ef2e35a81f187464a7b775e0e21c02c3"]["chars_read"] == 7002


# -- Q-A3: stopped mid-run ---------------------------------------------------------------------------------------------

def test_qa3_stopped_mid_run_with_older_topic_shape():
    f = FIXTURES["Q-A3"]
    m = build_coverage_map(f["state"])             # no richer source records at all: only checkpoint ids + acquired_actions
    prices = cat(m, "market_prices")
    assert prices["status"] == "obtained" and prices["sources_obtained"] == 3 and prices["data_as_of"] == "2026-11-19"
    assert prices["data_as_of_kind"] == "requested_end_date"             # honest: only the requested end is known
    assert {i["instrument"] for i in prices["instruments"]} == {"ETF:QQQ", "ETF:SPY", "ETF:ONEQ"}
    news = cat(m, "news_web")
    assert news["status"] == "partial" and news["failure_reasons"] == {"http_forbidden_or_challenge": 2}
    assert m["run"]["status"] == "interrupted"


def test_qa2_run_that_never_got_data_yields_an_empty_but_valid_map():
    m = run("Q-A2")
    assert all(c["status"] == "not_attempted" for c in m["categories"])
    assert m["run"]["status"] == "interrupted" and codes(m).count("category_not_attempted") == 4 and "run_not_submitted" in codes(m)


def test_qb3_mid_run_filings_and_news_failures():
    m = run("Q-B3")
    assert m["run"]["status"] == "incomplete" and m["run"]["pending"] == "model"
    filings = cat(m, "filings")
    assert filings["status"] == "partial" and filings["failure_reasons"] == {"source_unavailable": 1}
    assert filings["sources_obtained"] == 1
    news = cat(m, "news_web")
    assert news["status"] == "partial" and news["failure_reasons"] == {"source_too_large": 1}
    assert cat(m, "market_prices")["data_as_of"] == "2026-11-19"


# -- Q-B1: completed run, news only, tools did not cover ANT ----------------------------------------------------------

def test_qb1_completed_run_news_partial_and_sec_identity_failure():
    m = run("Q-B1")
    assert m["run"]["status"] == "submitted" and m["run"]["submission"]["status"] == "completed"
    news = cat(m, "news_web")
    assert news["status"] == "partial" and news["sources_obtained"] == 7
    assert news["failure_reasons"] == {"http_forbidden_or_challenge": 2}
    assert news["denied_lead_reasons"] == {"redirect_link": 15}
    fin = cat(m, "financial_statements")
    assert fin["status"] == "failed" and fin["failure_reasons"] == {"identity_not_unique": 2}
    assert [(a["tool"], a["target"], a["status"]) for a in fin["attempts"]] == [
        ("sec_company_concept", "ANT", "unknown"), ("sec_company_concept", "Anthropic PBC", "unknown")]
    assert cat(m, "market_prices")["status"] == "not_attempted"


def test_qb1_explicit_not_established_no_exchange_data_for_ant():
    m = run("Q-B1")
    ant = [s for s in m["not_established"] if s["code"] == "no_market_data_for_entity"]
    assert len(ant) == 1 and ant[0]["params"]["entity"] == "ANT"
    assert ant[0]["params"]["reason"] == "instrument_not_in_tool_coverage"
    assert ant[0]["params"]["configured_instruments"] == ["ETF:ONEQ", "ETF:QQQ", "ETF:SPY"]
    assert "No exchange price data for ANT" in ant[0]["message"]
    assert any(s["code"] == "acquired_not_read" for s in m["not_established"])


def test_missing_price_data_reason_variants():
    f = FIXTURES["Q-B1"]
    facts = [{"fact_id": "x", "label": "ANT首日收盘价", "value": "97.17", "unit": "USD", "source_id": "unknown"}]
    base = dict(f["state"])
    key_required = base["events"] + [{"kind": "tool", "name": "market_data", "status": "unavailable", "reason": "market_data_key_required"}]
    m = build_coverage_map({**base, "events": key_required}, facts=facts)
    assert cat(m, "market_prices")["status"] == "tool_unavailable"
    assert [s["params"]["reason"] for s in m["not_established"] if s["code"] == "no_market_data_for_entity"] == ["market_data_key_required"]
    # a symbol the tool does cover but nobody requested
    qqq = [{"fact_id": "y", "label": "QQQ收盘价", "value": "645.04", "unit": "USD", "source_id": "unknown"}]
    m = build_coverage_map({**base}, facts=qqq)
    assert [s["params"]["reason"] for s in m["not_established"] if s["code"] == "no_market_data_for_entity"] == ["not_requested"]
    # covered by an obtained market_data source: no statement
    m = build_coverage_map(FIXTURES["Q-A1"]["state"], sources=FIXTURES["Q-A1"]["sources"],
                           facts=[{"fact_id": "z", "label": "QQQ收盘价", "value": "645.04", "unit": "USD", "source_id": "s"}])
    assert "no_market_data_for_entity" not in codes(m)


# -- rules -----------------------------------------------------------------------------------------------------------

def tool(name, status, reason=None, **summary):
    e = {"kind": "tool", "name": name, "status": status}
    if reason:
        e["reason"] = reason
    if summary:
        e["summary"] = summary
    return e


CLOCK = {"captured_at": "2026-11-20T06:16:00Z", "utc_date": "2026-11-20",
         "exchanges": [{"exchange": "NASDAQ", "last_completed_session": "2026-11-19"}, {"exchange": "NYSE", "last_completed_session": "2026-11-19"}]}


@pytest.mark.parametrize("events,failed,expected", [
    ([tool("acquire_sources", "completed", items=[{"host": "www.wsj.com", "status": "failed", "reason": "source_blocked_terms"}])], [], "blocked_by_terms"),
    ([tool("market_data", "unavailable", "market_data_key_required")], [], "tool_unavailable"),
    ([tool("market_data", "denied", "license_platform_key_not_permitted")], [], "license_restricted"),
    ([tool("historical_prices", "failed", "no_bars")], [], "not_found"),
    ([tool("news_search", "completed", found_count=0, denied_count=0, eligible_count=0)], [], "not_found"),
    ([tool("acquire_sources", "completed", items=[{"host": "a.com", "status": "failed", "reason": "http_forbidden_or_challenge"}])], [], "failed"),
    ([tool("acquire_sources", "completed", items=[{"host": "a.com", "status": "failed", "reason": "source_blocked_terms"},
                                                  {"host": "b.com", "status": "failed", "reason": "source_timeout"}])], [], "blocked_by_terms"),
    ([], [], "not_attempted"),
])
def test_status_resolution_when_nothing_was_obtained(events, failed, expected):
    m = build_coverage_map({"events": events, "failed_source_accesses": failed, "user": {"research_clock": CLOCK}})
    name = "market_prices" if events and events[0]["name"] in ("market_data", "historical_prices") else "news_web"
    assert cat(m, name)["status"] == expected


def test_obtained_with_failures_is_partial_and_fred_is_license_restricted():
    events = [tool("acquire_sources", "completed", items=[{"host": "a.com", "status": "acquired", "total_characters": 10},
                                                          {"host": "b.com", "status": "failed", "reason": "source_unavailable"}])]
    m = build_coverage_map({"events": events})
    assert cat(m, "news_web")["status"] == "partial"
    fred = {"source_id": "s1", "url": "https://fred.stlouisfed.org/series/DGS10", "title": "DGS10", "retrieval_method": "web_html"}
    m = build_coverage_map({"events": []}, sources=[fred])
    assert cat(m, "macro")["status"] == "license_restricted" and cat(m, "macro")["failure_reasons"] == {"license_restricted": 1}


def test_stale_market_data_is_flagged_in_trading_days():
    src = {"source_id": "s1", "title": "ETF:QQQ daily close", "kind": "market_data", "retrieval_method": "historical_prices",
           "provenance": {"provider": "twelve_data", "instrument_id": "ETF:QQQ", "exchange": "NASDAQ", "actual_last_date": "2026-11-13"}}
    m = build_coverage_map({"user": {"research_clock": CLOCK}}, sources=[src])
    prices = cat(m, "market_prices")
    st = prices["staleness"]
    assert prices["data_as_of"] == "2026-11-13" and prices["data_as_of_kind"] == "actual_last_date"
    assert st["trading_days_before_last_session"] == 4 and st["days_before_last_session"] == 6 and st["is_stale"] is True
    assert st["reference_basis"] == "exchange" and st["reference_exchange"] == "NASDAQ"
    assert prices["instruments"][0]["exchange"] == "NASDAQ"
    assert "market_data_stale" in codes(m)


def test_trading_days_between_skips_weekends():
    from datetime import date
    assert trading_days_between(date(2026, 11, 13), date(2026, 11, 16)) == 1     # Fri -> Mon
    assert trading_days_between(date(2026, 11, 14), date(2026, 11, 15)) == 0     # Sat -> Sun
    assert trading_days_between(date(2026, 11, 19), date(2026, 11, 19)) == 0
    assert trading_days_between(date(2026, 11, 20), date(2026, 11, 19)) == 0


def test_published_at_used_for_news_as_of_and_clock_accepts_json_string():
    news = [{"source_id": "n1", "url": "https://www.reuters.com/a", "retrieval_method": "web_html", "published_at": "2026-11-18T09:00:00Z"},
            {"source_id": "n2", "url": "https://www.cnbc.com/b", "retrieval_method": "web_html", "published_at": "2026-11-10T09:00:00Z"}]
    m = build_coverage_map({"user": json.dumps({"research_clock": CLOCK})}, sources=news)
    c = cat(m, "news_web")
    assert c["status"] == "obtained" and c["data_as_of"] == "2026-11-18" and c["data_as_of_kind"] == "published_at"
    assert c["data_as_of_min"] == "2026-11-10" and c["staleness"]["days_before_clock_date"] == 2 and c["staleness"]["is_stale"] is None


def test_pdf_pages_read_ocr_and_unsearched():
    pages = {"page_count": 6, "version": 2, "method": "pdftotext_layout_paged", "text_limit_reached": False, "original_retained": True,
             "pages": [[0, 100, "text"], [101, 200, "text"], [201, 201, "needs_ocr"], [202, 300, "ocr"], [None, None, "not_included"],
                       [None, None, "failed"]],
             "ocr": {"4": {"engine": "tesseract", "mean_confidence": 55.0, "low_confidence_count": 3}}}
    src = {"source_id": "pdf1", "title": "S-1", "url": "https://www.sec.gov/s1.pdf", "retrieval_method": "web_pdf", "text": "x" * 300,
           "provenance": {"document_pages": pages}}
    events = [tool("read_sources", "completed", items=[{"source_id": "pdf1", "start": 0, "end": 150, "total_characters": 300}]),
              {"kind": "tool", "name": "request_ocr", "status": "completed", "ocr": {"status": "ocr", "source_id": "pdf1", "pages": [4]}}]
    search = {"searched_sources": [{"source_id": "pdf1", "passages": 3, "pages_total": 6, "unsearched_pages": {"needs_ocr": {"count": 1, "ranges": [[3, 3]]}}}],
              "excluded_sources": [{"source_id": "pdf2", "reason": "not_eligible"}]}
    m = build_coverage_map({"events": events, "read_ranges": [{"source_id": "pdf1", "start": 0, "end": 150}]}, sources=[src], search_coverage=search)
    doc = next(d for d in m["documents"] if d["source_id"] == "pdf1")
    pg = doc["pages"]
    assert doc["category"] == "filings" and doc["chars_read"] == 150 and doc["read_fraction"] == 0.5
    assert pg["total"] == 6 and pg["read"] == 2 and pg["read_ranges"] == [[1, 2]]
    assert pg["by_status"] == {"failed": 1, "needs_ocr": 1, "not_included": 1, "ocr": 1, "text": 2}
    assert pg["unsearched_pages"] == {"failed": [[6, 6]], "needs_ocr": [[3, 3]], "not_included": [[5, 5]]} and pg["unsearched_count"] == 3
    assert pg["unread_text_pages"] == 1 and pg["ocr_low_confidence_pages"] == [4]
    assert doc["ocr"] == {"status": "ocr", "pages_requested": [4]}
    assert doc["search"]["unsearched_pages"]["needs_ocr"]["count"] == 1
    c = cat(m, "filings")
    assert c["status"] == "partial" and "unsearched_pages" in c["partial_signals"]
    assert {"unsearched_pages", "ocr_low_confidence"} <= set(codes(m))


def test_read_ranges_derived_from_events_when_checkpoint_lacks_them():
    src = {"source_id": "w1", "url": "https://www.cnbc.com/a", "retrieval_method": "web_html", "title": "t"}
    events = [tool("read_sources", "completed", items=[{"source_id": "w1", "start": 0, "end": 100, "total_characters": 1000},
                                                       {"source_id": "w1", "start": 50, "end": 200, "total_characters": 1000}])]
    m = build_coverage_map({"events": events}, sources=[src])
    doc = m["documents"][0]
    assert doc["chars_total"] == 1000 and doc["chars_read"] == 200 and doc["read_fraction"] == 0.2


def test_robust_to_garbage_and_older_shapes():
    garbage = {"events": ["x", None, 3, {"kind": "tool"}, {"kind": "tool", "name": "acquire_sources", "summary": "oops"},
                          {"kind": "tool", "name": "historical_prices", "status": "acquired", "summary": {"end_date": "not-a-date"}},
                          {"kind": "weird"}], "read_ranges": [{"source_id": 3}, "x"], "failed_source_accesses": [{"url": None}, 7],
               "acquired_actions": {"historical_prices": "oops"}, "user": "not json {", "_sources": [None, {"source_id": None}]}
    m = build_coverage_map(garbage)
    assert len(m["categories"]) == 7 and m["run"]["status"] == "incomplete"
    assert build_coverage_map(None)["status_counts"] == {"not_attempted": 7}
    assert build_coverage_map({})["research_clock"]["available"] is False
    # no clock: no staleness, nothing raises
    m = build_coverage_map({"events": [tool("historical_prices", "acquired", instrument_id="ETF:QQQ", end_date="2026-11-19")]})
    assert cat(m, "market_prices")["data_as_of"] == "2026-11-19" and cat(m, "market_prices")["staleness"] is None


def test_deterministic_json_serialisable_and_consumers():
    m1, m2 = run("Q-B1"), run("Q-B1")
    assert m1 == m2
    json.dumps(m1)
    summary = model_summary(m1)["coverage"]
    assert summary["run_status"] == "submitted" and "market_prices" in summary["not_attempted"]
    assert {c["category"] for c in summary["categories"]} == {"financial_statements", "news_web"}
    assert any("ANT" in s for s in summary["not_established"])
    ui = ui_view(m1)
    row = next(r for r in ui["rows"] if r["category"] == "news_web")
    assert row["status"] == "partial" and row["status_label"]["zh"] == "部分取得" and row["failures"] and row["label"]["en"] == "News and web"
    m = run("Q-A1")
    price_row = next(r for r in ui_view(m)["rows"] if r["category"] == "market_prices")
    assert price_row["data_as_of"] == "2026-11-19" and price_row["trading_days_behind"] == 0 and price_row["is_stale"] is False


def test_staleness_respects_exchange_holidays_and_reports_holidays_modelled_honestly():
    # NYSE: Wed 2026-11-25 data evaluated after Fri 2026-11-27 close -> skips Thu 2026-11-26 Thanksgiving -> 1 trading day behind.
    clock_thanksgiving = {
        "utc_date": "2026-11-28",
        "exchanges": [{"exchange": "NYSE", "last_completed_session": "2026-11-27"}],
    }
    src_nyse = {
        "source_id": "s-nyse",
        "title": "AAPL daily close",
        "kind": "market_data",
        "retrieval_method": "market_data",
        "provenance": {"provider": "yahoo", "instrument_id": "AAPL", "exchange": "NYSE", "actual_last_date": "2026-11-25"},
    }
    m_nyse = build_coverage_map({"user": {"research_clock": clock_thanksgiving}}, sources=[src_nyse])
    stale_nyse = cat(m_nyse, "market_prices")["staleness"]
    assert stale_nyse["trading_days_before_last_session"] == 1
    assert stale_nyse["holidays_modelled"] is True

    # Unmodelled exchange (ASX) keeps holidays_modelled == False cleanly.
    clock_asx = {
        "utc_date": "2026-11-28",
        "exchanges": [{"exchange": "ASX", "last_completed_session": "2026-11-27"}],
    }
    src_asx = {
        "source_id": "s-asx",
        "title": "BHP daily close",
        "kind": "market_data",
        "retrieval_method": "market_data",
        "provenance": {"provider": "yahoo", "instrument_id": "BHP", "exchange": "ASX", "actual_last_date": "2026-11-25"},
    }
    m_asx = build_coverage_map({"user": {"research_clock": clock_asx}}, sources=[src_asx])
    stale_asx = cat(m_asx, "market_prices")["staleness"]
    assert stale_asx["trading_days_before_last_session"] == 2
    assert stale_asx["holidays_modelled"] is False

    # Out-of-coverage year (2028) for covered exchange reports holidays_modelled == False honestly.
    clock_future = {
        "utc_date": "2028-01-10",
        "exchanges": [{"exchange": "NYSE", "last_completed_session": "2028-01-07"}],
    }
    src_future = {
        "source_id": "s-fut",
        "title": "AAPL 2028",
        "kind": "market_data",
        "retrieval_method": "market_data",
        "provenance": {"provider": "yahoo", "instrument_id": "AAPL", "exchange": "NYSE", "actual_last_date": "2028-01-05"},
    }
    m_future = build_coverage_map({"user": {"research_clock": clock_future}}, sources=[src_future])
    assert cat(m_future, "market_prices")["staleness"]["holidays_modelled"] is False


# Asset registry

`registry/v1/assets.source.json` is the reviewed instrument/proxy source and `assets.json` is its deterministic, content-hashed contract artifact. It covers all 15 benchmark economic exposures, explicit tradable proxies, global equity/bond/commodity ETFs, BTC/ETH, REITs and USD/CNY cash equivalents.

Non-tradable indices, commodities and yields cannot be allocated directly. Every such entry names a registry-resolved tradable proxy. Derivatives are supported by the canonical contract but remain ineligible for Public Default unless complete expiry, multiplier, margin, delta/volatility where applicable, and risk-model metadata are present.

有，而且按你这种“**不做大规模回测，只取少量关键日期/近期趋势，并尽量在用户侧直接拉取**”的需求，其实可以用一套相当轻的组合。

我刚检查了截至 **2026-08-31** 仍在维护、网页和接口仍可访问的来源。我的推荐排序如下。

| 数据                  | 首选                              |   是否 Key | 更新频率            |  适合程度 |
| ------------------- | ------------------------------- | -------: | --------------- | ----: |
| 股票/ETF/指数价格         | **Stooq**                       |        否 | 日线为主            | ⭐⭐⭐⭐⭐ |
| 股票价格备用              | **yfinance / Yahoo Finance**    |        否 | 日线/分钟/实时能力      |  ⭐⭐⭐⭐ |
| Momentum 因子         | **Kenneth French Data Library** |        否 | Daily / Monthly | ⭐⭐⭐⭐⭐ |
| Quality 因子          | **AQR QMJ**                     |        否 | Daily，数据月度更新    | ⭐⭐⭐⭐⭐ |
| Quality 代理          | **Fama-French RMW**             |        否 | Daily           |  ⭐⭐⭐⭐ |
| FX 汇率               | **Frankfurter**                 |        否 | 日频/央行参考汇率       | ⭐⭐⭐⭐⭐ |
| 实时价格 + 技术指标         | Twelve Data                     | 是，免费 Key | 实时              |  ⭐⭐⭐⭐ |
| MOM + FX + 股票统一 API | Alpha Vantage                   | 是，免费 Key | 实时/日频           |   ⭐⭐⭐ |

---

## 1. 市场价格：Stooq

这是我比较推荐你直接接到应用里的公开行情源。

它有非常简单的 CSV 接口：

```text
https://stooq.com/q/d/l/?s=aapl.us&i=d
```

也支持起止时间参数：

```text
https://stooq.com/q/d/l/?s=aapl.us&i=d&d1=20260101&d2=20260831
```

返回基本就是：

```csv
Date,Open,High,Low,Close,Volume
...
```

`pandas-datareader` 自己的 Stooq adapter 目前就是直接请求：

```python
https://stooq.com/q/d/l/
```

并使用 `s / i / d1 / d2` 这些参数。([GitHub][1])

优点是：

* 无 API Key；
* HTTP CSV，非常轻；
* 股票、ETF、指数覆盖不错；
* 很适合你这种只取几个关键时间附近价格的方式；
* 客户端甚至可以直接请求后解析 CSV。

缺点是它**不是交易所官方实时 feed**，也没有你能依赖的商业 SLA。所以我更建议把它定义成：

> `public_market_price_provider`

而不是“交易级实时行情源”。

比如：

```python
GET https://stooq.com/q/d/l/?s=spy.us&i=d&d1=20260801&d2=20260831
```

然后本地只保留：

```text
year_start
month_start
latest
```

三个 anchor。

### yfinance 作为 fallback

`yfinance` 到 2026 年还在活跃维护，2026-06-28 发布了 1.5.1，并已经有 WebSocket / AsyncWebSocket、历史价格、行情搜索等能力；项目本身 Apache-2.0 开源。([GitHub][2])

```python
import yfinance as yf

data = yf.download(
    "AAPL",
    start="2026-01-01",
    end="2026-09-01"
)
```

但这里有一个重要区别：

**yfinance 开源 ≠ Yahoo 行情数据开源。**

项目自己明确说明它是非官方 Yahoo Finance 接口，主要面向 research / educational use，需要遵守 Yahoo 的数据使用条款。([GitHub][2])

所以我建议：

```text
Primary    Stooq
Fallback   yfinance
```

而不要把 Yahoo 当唯一生产数据源。

---

# 2. Momentum：Kenneth French Data Library

如果你说的 Momentum 是**金融因子 Momentum**，而不只是 RSI/MACD 这种技术指标，那我强烈推荐 Kenneth French。

目前官方 Data Library 明确仍提供：

```text
Momentum Factor (Mom)
Momentum Factor (Mom) [Daily]
```

以及：

```text
Developed Momentum
Developed ex-US Momentum
European Momentum
Japanese Momentum
Asia Pacific ex Japan Momentum
North American Momentum
Emerging Momentum
```

而且很多都有 Daily 数据。([Tuck School of Business][3])

官方页面：

[Kenneth French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html?utm_source=chatgpt.com)

实际 ZIP 下载路径就是：

```text
https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Momentum_Factor_daily_CSV.zip
```

这个地址目前也仍被量化研究代码直接用于远程加载。([MDPI][4])

Python：

```python
import pandas as pd

url = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/"
    "ftp/F-F_Momentum_Factor_daily_CSV.zip"
)

mom = pd.read_csv(
    url,
    header=11,
    index_col=0
)
```

### 对你的应用尤其有意义

你其实没必要给每只资产拉一个所谓 `"momentum_score"` API。

更合理的是：

```text
asset price history
        ↓
本地计算 asset momentum

market/regional momentum factor
        ↓
Kenneth French MOM

二者结合
```

比如本地直接算：

```python
mom_1m  = price_t / price_1m_ago - 1
mom_3m  = price_t / price_3m_ago - 1
mom_12m = price_1m_ago / price_12m_ago - 1
```

这样比依赖第三方的黑盒 `"momentum score"` 更可解释。

---

# 3. Quality：AQR Quality Minus Junk

如果你说的是学术资产定价里的 **Quality factor**，这里最好的公开源不是普通行情 API，而是 **AQR QMJ — Quality Minus Junk**。

AQR 到 **2026-06-30** 仍在更新这个数据集。

官方提供：

```text
Quality Minus Junk: Factors, Daily
Quality Minus Junk: Factors, Monthly
Quality-sorted portfolios
```

Daily QMJ 覆盖：

> 美国 + 23 个国际股票市场。

Quality 的构造包括：

* profitability
* growth
* safety
* payout

然后构造 long quality / short junk 因子。([AQR Capital Management][5])

官方：

[AQR Quality Minus Junk Daily](https://www.aqr.com/Insights/Datasets/Quality-Minus-Junk-Factors-Daily?utm_source=chatgpt.com)

这个比把 `"quality"` 简单理解成 PE、ROE 要靠谱得多。

---

## 另一个免费 Quality 代理：Fama-French RMW

Kenneth French 的 5 Factors Daily 目前也仍然公开：

```text
Mkt-RF
SMB
HML
RMW
CMA
RF
```

其中：

```text
RMW = Robust Minus Weak
```

本质是 **operating profitability factor**。

官方页面目前明确提供：

```text
Fama/French 5 Factors (2x3)
Fama/French 5 Factors (2x3) [Daily]
```

以及不同地区版本。([Tuck School of Business][3])

直接：

```text
https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip
```

Python：

```python
ff5 = pd.read_csv(
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/"
    "ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
    header=2,
    index_col=0,
)
```

因此你的 factor schema 可以直接设计成：

```json
{
  "momentum": {
    "source": "kenneth_french",
    "factor": "MOM"
  },
  "quality": {
    "source": "aqr",
    "factor": "QMJ"
  },
  "profitability": {
    "source": "kenneth_french",
    "factor": "RMW"
  }
}
```

---

# 4. FX：Frankfurter，我最推荐

这个几乎完全符合你的要求。

Frankfurter 是：

* **真正开源**
* 免费
* 无账户
* **无 API Key**
* 可以自己 Docker 部署
* 公共实例直接调用
* 数据来自央行/官方金融机构

现在 v2 API 是：

```text
https://api.frankfurter.dev
```

官方明确写着：

> no API key, no account.

([Frankfurter][6])

### 当前汇率

```text
GET https://api.frankfurter.dev/v2/rate/USD/EUR
```

返回类似：

```json
{
  "date": "2026-07-17",
  "base": "USD",
  "quote": "EUR",
  "rate": 0.8739
}
```

官方 Python 文档就是这样调用的。([Frankfurter][6])

多个 currency：

```text
GET /v2/rates?base=USD&quotes=EUR,GBP,JPY
```

Python：

```python
import requests

rates = requests.get(
    "https://api.frankfurter.dev/v2/rates",
    params={
        "base": "USD",
        "quotes": "EUR,CNY,JPY"
    },
).json()
```

历史：

```text
/v2/rates?base=USD&from=2026-01-01&to=2026-08-31
```

甚至可以指定 ECB：

```text
/v2/rate/USD/EUR?providers=ECB
```

([Frankfurter][7])

现在它实际上已经扩展到了 **84 个央行/来源、201 种货币、最早可追溯到 1948 年**。([Frankfurter][8])

### 但要注意

Frankfurter 是：

**reference FX rate**

而不是：

```text
EURUSD tick
bid
ask
spread
1m candle
```

因此：

* 宏观趋势：非常好
* 日线 FX：非常好
* 世界线推演 contextual data：非常好
* 高频交易：不合适

对你反而正合适。

---

# 5. 如果想要一个“统一 API”：Twelve Data

如果你觉得上面四家太散，可以考虑 Twelve Data。

它一个 API 能同时拿：

```text
Stocks
ETF
Forex
Crypto
Price
OHLCV
Technical indicators
```

当前免费 Basic 是：

```text
8 API credits / minute
800 / day
```

包括：

* real-time US equities / ETF
* real-time forex
* real-time crypto
* technical indicators

([Twelve Data][9])

比如：

```text
https://api.twelvedata.com/time_series
    ?symbol=AAPL
    &interval=1day
    &apikey=KEY
```

FX：

```text
symbol=EUR/USD
```

而且有 100+ 技术指标。([Twelve Data][10])

缺点：

**不是开源数据源，需要 API key。**

而且如果是你的 App 内置 Key：

```text
Android APK
Web JS
```

都会很容易被抽出来。

所以它更适合作为：

```text
optional BYOK provider
```

而不是硬编码你的 key。

---

# 6. Alpha Vantage 也值得保留

它有一个很方便的特点：

### Momentum 直接有接口

```text
function=MOM
```

例如：

```text
https://www.alphavantage.co/query
?function=MOM
&symbol=IBM
&interval=daily
&time_period=10
&series_type=close
&apikey=KEY
```

这是官方支持的 MOM indicator。([Alpha Vantage][11])

FX 也直接：

```text
function=FX_DAILY
from_symbol=EUR
to_symbol=USD
```

股票：

```text
TIME_SERIES_DAILY
```

因此 Alpha Vantage 一家就能搞：

```text
price
technical momentum
FX
fundamentals
```

但当前免费标准额度只有：

**25 requests / day**。([Alpha Vantage][12])

所以对普通 App 太低，但对你那种：

> 每次只拉几个重要时间点

反而可能够。

---

# 7. 还有一个我觉得很适合你的：OpenBB

它本身不是数据供应商，而是**开源金融数据统一适配层**。

现在 OpenBB 可以统一接：

```text
yfinance
FamaFrench
ECB
FRED
Alpha Vantage
Tiingo
Polygon
FMP
CFTC
...
```

而且已经直接有：

```text
openbb-yfinance
openbb-famafrench
openbb-ecb
openbb-fred
```

很多不需要 key。其官方 provider 文档在 **2026-08-24** 还刚更新。([GitHub][13])

甚至能直接启动 REST：

```bash
openbb-api
```

然后你的应用统一访问：

```text
/provider/...
```

OpenBB 本身是 AGPLv3 开源。([GitHub][14])

不过对 foresightnine 我反而**不建议整个 OpenBB 搬进去**，有点重。

---

# 我建议你最终就用这一套

```text
                  ForesightNine client
                         │
        ┌────────────────┼────────────────┐
        │                │                │
      Price            Factor             FX
        │                │                │
      Stooq      Kenneth French       Frankfurter
        │          MOM / RMW              │
   yfinance            │              central banks
    fallback          AQR
                     QMJ
```

具体：

```yaml
price:
  primary: stooq
  fallback: yfinance

momentum:
  asset:
    method: local_return
    horizons: [1m, 3m, 12m]
  market_factor:
    provider: kenneth_french
    factor: MOM

quality:
  primary:
    provider: AQR
    factor: QMJ
  fallback_proxy:
    provider: kenneth_french
    factor: RMW

fx:
  primary:
    provider: frankfurter
  provider_preference:
    - ECB
```

### 对你这个产品尤其重要的一点

**Momentum 和 Quality 不要每次请求都重新下载整个历史因子库。**

它们更新得并不快：

```text
AQR QMJ → 日度序列，但数据集大约月度更新
Fama-French → 定期更新
```

所以完全可以：

```text
App bundle/cache
     +
偶尔刷新 factor CSV
```

真正用户请求发生时，只动态拉：

```text
当前价格
近期价格
FX
```

这样每次 worldline request 实际可能就几 KB 数据。([AQR Capital Management][5])

如果只让我给一个**最小、无 Key、马上能实现的 V1**，就是：

**Stooq + Kenneth French MOM/RMW + AQR QMJ + Frankfurter。**

这四个已经能覆盖你说的 **Price + Momentum + Quality + FX**，而且无需让用户注册任何数据供应商账号。

[1]: https://github.com/pydata/pandas-datareader/blob/master/pandas_datareader/stooq.py?utm_source=chatgpt.com "pandas-datareader/pandas_datareader/stooq.py at main · pydata/pandas-datareader · GitHub"
[2]: https://github.com/ranaroussi/yfinance?utm_source=chatgpt.com "GitHub - ranaroussi/yfinance: Download market data from Yahoo! Finance's API · GitHub"
[3]: https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html "Kenneth R. French - Data Library"
[4]: https://www.mdpi.com/2079-9292/14/22/4405?utm_source=chatgpt.com "Fast-Converging and Trustworthy Federated Learning Framework for Privacy-Preserving Stock Price Modeling"
[5]: https://www.aqr.com/Insights/Datasets/Quality-Minus-Junk-Factors-Daily?utm_source=chatgpt.com "Quality Minus Junk: Factors, Daily"
[6]: https://frankfurter.dev/python/?utm_source=chatgpt.com "Frankfurter | Exchange Rates in Python"
[7]: https://frankfurter.dev/currencies/usd/?utm_source=chatgpt.com "Frankfurter | United States Dollar (USD) exchange rates"
[8]: https://frankfurter.dev/?utm_source=chatgpt.com "Frankfurter | Free exchange rates API"
[9]: https://twelvedata.com/pricing?utm_source=chatgpt.com "Individual Pricing - Twelve Data"
[10]: https://twelvedata.com/market-data?utm_source=chatgpt.com "Twelve Data | Financial Market Data APIs"
[11]: https://www.alphavantage.co/documentation/?utm_source=chatgpt.com "API Documentation | Alpha Vantage"
[12]: https://www.alphavantage.co/premium/?utm_source=chatgpt.com "Premium API Key | Alpha Vantage"
[13]: https://github.com/OpenBB-finance/OpenBB/blob/develop/openbb_platform/README.md?utm_source=chatgpt.com "OpenBB/openbb_platform/README.md at develop · OpenBB-finance/OpenBB · GitHub"
[14]: https://github.com/OpenBB-finance/OpenBB?utm_source=chatgpt.com "GitHub - OpenBB-finance/OpenBB: Financial data platform for analysts, quants and AI agents. · GitHub"

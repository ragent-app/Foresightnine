import { Text, View } from "react-native";
import { router } from "expo-router";
import { useMobileStore } from "../src/app-store";
import { canEnterPortfolio } from "../src/archive";
import { t } from "../src/i18n";
import { displayAsset, formatMoney, modeWeight, portfolioMetadata } from "../src/portfolio";
import { BottomNav, Button, Card, Header, Screen, StageRail, Status, styles } from "../src/ui";

function pct(value: number): string { return `${(value * 100).toFixed(1)}%`; }

export default function PortfolioScreen(): React.JSX.Element {
  const { state, portfolio, setPortfolioMode } = useMobileStore();
  if (!canEnterPortfolio(state) || !portfolio) return <Screen><Header back title={t(state.language, "portfolio")} subtitle={t(state.language, "allocationUnavailable")} /><Card tone="warning"><Text style={styles.body}>{t(state.language, "runFmFirst")}</Text><Button onPress={() => router.replace("/worldline")} label={t(state.language, "goWorldline")}>{t(state.language, "goWorldline")}</Button></Card><BottomNav /></Screen>;
  const mode = state.portfolioMode;
  const total = mode === "raw" ? portfolio.rawTotal : portfolio.publicTotal;
  const marketValue = mode === "raw" ? portfolio.rawMarketValue : portfolio.publicMarketValue;
  const cashRemainder = mode === "raw" ? portfolio.rawCashRemainder : portfolio.publicCashRemainder;
  const executable = mode === "raw" ? portfolio.rawImplementable : portfolio.publicImplementable;
  const hasHistoricalOnlyData = portfolio.rows.some((row) => row.freshnessLabel === "historical_only");
  const scenario = portfolio.scenario;
  const navLow = Math.min(...scenario.points.map((point) => point.nav));
  const navHigh = Math.max(...scenario.points.map((point) => point.nav));
  return <Screen><Header back title={t(state.language, "portfolio")} subtitle={t(state.language, "simulationOnly")} /><StageRail active={4} />
    <Status tone={executable ? "good" : "warning"}>{t(state.language, "fmComplete")} · {state.fmStep}</Status>
    {hasHistoricalOnlyData ? <Status tone="warning">{t(state.language, "historicalSimulationOnly")}</Status> : null}
    <Card><Text style={styles.heading}>{t(state.language, "rawPublic")}</Text><Text style={styles.muted}>{t(state.language, "rawPublicBody")}</Text><View style={[styles.split, { marginTop: 4 }]}><View style={styles.flex}><Button variant={mode === "raw" ? "primary" : "secondary"} onPress={() => setPortfolioMode("raw")} label={t(state.language, "showRaw")}>FM Raw</Button></View><View style={styles.flex}><Button variant={mode === "public" ? "primary" : "secondary"} onPress={() => setPortfolioMode("public")} label={t(state.language, "showPublic")}>Public Default</Button></View></View></Card>
    {!executable ? <Status tone="warning">{t(state.language, "executionStatus")}: {t(state.language, "notExecutable")}</Status> : null}
    <Card><Text style={styles.heading}>{t(state.language, "allocationList")} · {mode === "raw" ? "FM Raw" : "Public Default"}</Text>{portfolio.rows.map((row) => { const weight = modeWeight(row, mode); const amount = mode === "raw" ? row.rawAmount : row.publicAmount; const units = mode === "raw" ? row.rawUnits : row.publicUnits; const currentAmountAllowed = mode === "raw" || row.publicExecutionAllowed; const residual = mode === "raw" ? row.rawResidual : row.publicResidual; const delta = row.publicWeight - row.rawWeight; const display = displayAsset(row.asset, state.language); return <View key={row.asset.id} style={styles.listRow}><View style={styles.row}><Text style={[styles.strong, styles.flex]}>{display.name}</Text><Text style={styles.strong}>{(weight * 100).toFixed(1)}%</Text></View><Text style={styles.muted}>{display.role} · {row.asset.instrument}</Text><Text style={styles.tiny}>{t(state.language, "marketValue")} {currentAmountAllowed ? formatMoney(amount, state.currency, state.language) : "—"} · {currentAmountAllowed ? units.toFixed(row.asset.fractional ? 3 : 0) : "—"} {t(state.language, "units")} · {t(state.language, "dataAsOfShort")} {row.asset.asOf}</Text>{residual > 1e-8 ? <Text style={styles.tiny}>{t(state.language, "roundingCash")}: {formatMoney(residual, state.currency, state.language)}</Text> : null}{mode === "public" && Math.abs(delta) > 1e-8 ? <Text style={styles.danger}>{t(state.language, "publicDelta")} {(delta * 100).toFixed(1)}pp</Text> : null}{row.reasons.map((reason) => <Text key={reason} style={styles.tiny}>• {reason}</Text>)}</View>; })}<Text style={styles.body}>{t(state.language, "displayedWeights")}: {(total * 100).toFixed(1)}% · {t(state.language, "marketValue")} {formatMoney(marketValue, state.currency, state.language)}</Text><Text style={styles.tiny}>{t(state.language, "roundingCash")}: {formatMoney(cashRemainder, state.currency, state.language)} · {t(state.language, "cashResidual")}</Text></Card>
    <Card><Text style={styles.heading}>{t(state.language, "scenarioNav")} · {scenario.branch}</Text><View style={styles.chart} accessibilityLabel={t(state.language, "scenarioNav")}><View style={styles.chartBars}>{scenario.points.map((point) => <View key={point.date} style={styles.chartBarColumn}><View style={[styles.chartBar, point.segment === "historical" ? styles.chartHistorical : styles.chartSimulated, { height: 18 + ((point.nav - navLow) / Math.max(navHigh - navLow, 1)) * 70 }]} /><Text style={styles.chartCaption}>{point.date.slice(5)}</Text></View>)}</View></View><Text style={styles.muted}>{t(state.language, "historicalThrough")} {scenario.dataAsOf}; {t(state.language, "simulatedFrom")} {scenario.simulatedFrom}.</Text><Text style={styles.body}>{t(state.language, "scenarioReturn")}: {pct(scenario.scenarioReturn)} · {t(state.language, "maxDrawdown")}: {pct(scenario.maxDrawdown)}</Text><Text style={styles.tiny}>{t(state.language, "mockNavLabel")}. {t(state.language, "scenarioNavBody")}</Text></Card>
    <Card tone="warning"><Text style={styles.heading}>{t(state.language, "riskMethod")}</Text>{portfolio.rawReasons.map((reason) => <Text key={reason} style={styles.muted}>• FM: {reason}</Text>)}<Text style={styles.muted}>{t(state.language, "methodVersion")} {portfolioMetadata.method}; {t(state.language, "dataAsOf")} {portfolioMetadata.dataAsOf}. {t(state.language, "portfolioCaveat")}</Text><Text style={styles.danger}>{t(state.language, "deviceOnly")}</Text></Card>
    <Button onPress={() => router.push("/archive")} label={t(state.language, "saveArchive")}>{t(state.language, "saveArchive")}</Button><BottomNav />
  </Screen>;
}

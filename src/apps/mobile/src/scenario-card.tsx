import { Text, TextInput, View } from "react-native";
import type { Currency, Language, ScenarioNav } from "./types";
import type { ScenarioReturns } from "./scenario-inputs";
import { Card, Status, styles } from "./ui";

export function ScenarioCard({ scenario, language, currency, returns, setReturn }: {
  readonly scenario: ScenarioNav; readonly language: Language; readonly currency: Currency;
  readonly returns: ScenarioReturns; readonly setReturn: (assetId: string, value: string) => void;
}): React.JSX.Element {
  const en = language === "en";
  const shown = scenario.points.filter((_, index) => index % 6 === 0);
  const low = Math.min(...shown.map(point => point.nav)); const high = Math.max(...shown.map(point => point.nav));
  return <Card><Text style={styles.heading}>{en ? "Your scenario assumptions" : "自定义情景假设"} · {scenario.branch}</Text>
    <Text style={styles.muted}>{en ? `Enter each asset's return over this worldline in ${currency}. These are not forecasts.` : `填写该世界线期限内各资产以 ${currency} 计的收益假设，不是预测。`}</Text>
    {scenario.inputs.filter(item => !item.cash).map(item => <View key={item.assetId} style={styles.listRow}>
      <Text style={styles.strong}>{item.assetId} · {(item.weight * 100).toFixed(1)}%</Text>
      <TextInput accessibilityLabel={`${item.assetId} ${en ? "assumed return (%)" : "假设收益率（%）"}`} style={styles.input} value={returns[item.assetId] ?? ""} onChangeText={value => setReturn(item.assetId, value)} keyboardType="numbers-and-punctuation" maxLength={24} placeholder={en ? "-100 to 1000 (%)" : "-100 至 1000（%）"} placeholderTextColor="#74675d" />
      {scenario.missingAssets.includes(item.assetId) && (returns[item.assetId] ?? "").trim() ? <Text style={styles.danger}>{en ? "Enter a number from -100 to 1000." : "请输入 -100 至 1000 的数字。"}</Text> : null}
    </View>)}
    {scenario.status === "ready" ? <>
      <View style={styles.chart} accessibilityLabel={en ? "Hypothetical path from your inputs, no historical prices" : "按输入假设计算的路径，不含历史行情"}><View style={styles.chartBars}>{shown.map((point, index) => <View key={index} style={styles.chartBarColumn}><View style={[styles.chartBar, styles.chartSimulated, { height: 18 + (point.nav - low) / Math.max(high - low, 1) * 70 }]} /><Text style={styles.chartCaption}>{point.date.slice(5)}</Text></View>)}</View></View>
      <Text style={styles.body}>{en ? "Assumed terminal return" : "假设期末收益"}: {(scenario.scenarioReturn! * 100).toFixed(1)}% · {en ? "Drawdown of this assumed path" : "该假设路径回撤"}: {(scenario.maxDrawdown! * 100).toFixed(1)}%</Text>
    </> : <Status tone="warning">{scenario.status === "invalid_inputs" ? (en ? "A valid allocation and worldline period are required." : "需要有效配置金额和世界线起止日期。") : (en ? "Complete all held assets' return assumptions to calculate." : "填写所有持仓资产的收益假设后计算。")}</Status>}
    <Text style={styles.tiny}>{en ? "Cash earns 0%. Fees and taxes are not added. Linear interpolation does not estimate intra-period risk." : "现金收益按 0%；不另计费用和税费；线性路径不代表实际期间风险。"}</Text>
  </Card>;
}

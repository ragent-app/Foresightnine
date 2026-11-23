import { useEffect, useState } from "react";
import { discoverModels, type DiscoveredModel, type ModelDiscoveryRequest } from "../../api/settings";

export function useModelDiscovery(request: ModelDiscoveryRequest | null, model: string) {
  const [catalog, setCatalog] = useState<{ request: ModelDiscoveryRequest; models: DiscoveredModel[]; message: string }>();
  const [detail, setDetail] = useState<{ request: ModelDiscoveryRequest; model: DiscoveredModel }>();
  useEffect(() => {
    if (!request) return;
    const abort = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const { models, upstream_status } = await discoverModels(request, abort.signal);
        if (abort.signal.aborted) return;
        const message = upstream_status ? `无法取得模型列表（HTTP ${upstream_status}），可手动输入。`
          : models.length ? `已发现 ${models.length} 个模型，可从列表选择或手动输入。` : "服务商返回了空列表，可手动输入。";
        setCatalog({ request, models, message });
      } catch {
        if (!abort.signal.aborted) setCatalog({ request, models: [], message: "无法取得模型列表，可手动输入。" });
      }
    }, 600);
    return () => { window.clearTimeout(timer); abort.abort(); };
  }, [request]);
  const current = catalog?.request === request ? catalog : undefined;
  const selected = current?.models.find((entry) => entry.id === model);
  useEffect(() => {
    if (!request || !current || !model || model !== model.trim() || model.length > 128 || selected?.efforts !== undefined) return;
    const abort = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const reply = await discoverModels({ ...request, model }, abort.signal);
        const item = reply.models.find((entry) => entry.id === model);
        if (item && !abort.signal.aborted) setDetail({ request, model: item });
      } catch { /* Discovery is optional. The model and its effort remain editable. */ }
    }, 600);
    return () => { window.clearTimeout(timer); abort.abort(); };
  }, [current, model, request, selected]);
  const info = selected?.efforts !== undefined ? selected : detail && detail.request === request && detail.model.id === model ? detail.model : undefined;
  return {
    models: current?.models ?? [],
    message: current?.message ?? (request ? "正在读取模型列表…" : ""),
    efforts: info?.efforts,
    source: info?.effort_source,
  };
}

import type { AssetRegistryEntry, AssetRegistrySnapshot } from "@foresightnine/contracts";

export function indexRegistry(snapshot: AssetRegistrySnapshot): ReadonlyMap<string, AssetRegistryEntry> {
  const index = new Map<string, AssetRegistryEntry>();
  for (const asset of snapshot.assets) {
    if (index.has(asset.asset_id)) throw new Error(`Duplicate asset_id: ${asset.asset_id}`);
    index.set(asset.asset_id, asset);
  }
  return index;
}

export function assertRegistryInvariants(snapshot: AssetRegistrySnapshot): void {
  const index = indexRegistry(snapshot);
  for (const asset of snapshot.assets) {
    if (!asset.tradable && asset.proxy_instruments.length === 0) {
      throw new Error(`Non-tradable exposure has no proxy: ${asset.asset_id}`);
    }
    for (const proxyId of asset.proxy_instruments) {
      const proxy = index.get(proxyId);
      if (!proxy) throw new Error(`Unknown proxy ${proxyId} for ${asset.asset_id}`);
      if (!proxy.tradable) throw new Error(`Proxy ${proxyId} is not tradable`);
    }
    if (asset.default_allocation_eligible && !asset.tradable) {
      throw new Error(`Default allocation asset is not tradable: ${asset.asset_id}`);
    }
    if ((asset.asset_class === "future" || asset.asset_class === "option" || asset.asset_class === "derivative") && asset.default_allocation_eligible && !asset.derivative) {
      throw new Error(`Eligible derivative lacks complete risk metadata: ${asset.asset_id}`);
    }
  }
}

export function searchRegistry(snapshot: AssetRegistrySnapshot, query: string): readonly AssetRegistryEntry[] {
  const normalized = query.trim().toLocaleLowerCase("en-US");
  if (!normalized) return snapshot.assets;
  return snapshot.assets.filter((asset) =>
    asset.asset_id.toLocaleLowerCase("en-US").includes(normalized) ||
    asset.display_name.toLocaleLowerCase("en-US").includes(normalized) ||
    asset.economic_exposure_id.toLocaleLowerCase("en-US").includes(normalized)
  );
}

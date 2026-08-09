import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { assertRegistryInvariants, indexRegistry, searchRegistry } from "../dist/index.js";

const snapshot = JSON.parse(await readFile(new URL("../registry/v1/assets.json", import.meta.url), "utf8"));

test("registry contains benchmark exposures and their tradable proxies", () => {
  assertRegistryInvariants(snapshot);
  const index = indexRegistry(snapshot);
  for (const id of ["IDX:000300.SH", "IDX:SPX", "IDX:HSI", "IDX:N225", "IDX:SX5E", "IDX:000688.SH", "IDX:SOX", "IDX:NDX", "COM:XAU", "COM:COPPER", "COM:WTI", "CRYPTO:BTC", "CRYPTO:ETH", "YIELD:US10Y", "YIELD:CN10Y"]) {
    assert.ok(index.has(id), id);
  }
  assert.equal(index.get("YIELD:US10Y").tradable, false);
  assert.equal(index.get("ETF:IEF").tradable, true);
});

test("registry spans public MVP classes and deterministic search", () => {
  const classes = new Set(snapshot.assets.map((asset) => asset.asset_class));
  for (const kind of ["index", "etf", "commodity_spot", "commodity_etf", "yield_curve", "crypto", "reit", "cash"]) assert.ok(classes.has(kind), kind);
  assert.deepEqual(searchRegistry(snapshot, "bitcoin").map((asset) => asset.asset_id), ["CRYPTO:BTC", "ETF:IBIT"]);
});

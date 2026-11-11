import { useEffect, useState } from "react";
import type { NavAssumptions } from "./nav-data";
export function navAssumptionsKey(scope: string, bundle: string): string { return `foresightnine.web.nav.v1:${encodeURIComponent(scope)}:${encodeURIComponent(bundle)}`; }
export function decodeNavAssumptions(raw: string | null): NavAssumptions {
  if (!raw || raw.length > 65536) return {};
  try {
    const value: unknown = JSON.parse(raw);
    const record = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
    const safe = (k: string) => k.length < 512 && !["__proto__","constructor","prototype"].includes(k);
    if (!record(value) || Object.keys(value).length > 3 || !Object.entries(value).every(([k,v]) => safe(k) && record(v) && Object.keys(v).length <= 100 && Object.entries(v).every(([id,n]) => safe(id) && typeof n === "string" && n.length <= 24))) return {};
    return value as NavAssumptions;
  } catch { return {}; }
}
/** Separate versioned draft: old workspace decoders remain compatible on rollback. */
export function useNavAssumptions(key: string | null) {
  const [saved, setSaved] = useState<{key:string|null;values:NavAssumptions;error:boolean}>({key:null,values:{},error:false});
  useEffect(() => {
    if (!key) { setSaved({key:null,values:{},error:false}); return; }
    try { setSaved({key,values:decodeNavAssumptions(window.localStorage.getItem(key)),error:false}); }
    catch { setSaved({key,values:{},error:true}); }
  }, [key]);
  // A scope/bundle change must not render or export another account's assumptions.
  const values = saved.key === key ? saved.values : {};
  const setValue = (worldline: string, instrument: string, value: string) => {
    if (!key || saved.key !== key || value.length > 24) return;
    const next = {...values,[worldline]:{...values[worldline],[instrument]:value}};
    let error = false;
    try { window.localStorage.setItem(key,JSON.stringify(next)); } catch { error=true; }
    setSaved({key,values:next,error});
  };
  return {values,setValue,saveError:saved.key === key && saved.error};
}

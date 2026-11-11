import { sha256Hex } from "@foresightnine/fm-engine";

export function researchOwnerScope(session: { readonly account: { readonly account_id: string } } | null, status?: string): string | null {
  if (status === "loading") return null;
  return session ? `account:${session.account.account_id}` : "anonymous";
}

/** Opaque local namespace; never place raw account IDs or emails in filesystem paths. */
export function researchOwnerHash(scope: string): string {
  if (!scope || scope.length > 1024) throw new Error("invalid_research_owner");
  return sha256Hex(`f9-research-owner-v1:${scope}`);
}

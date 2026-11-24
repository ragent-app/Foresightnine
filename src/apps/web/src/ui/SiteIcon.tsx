import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "../api/http";

/** The page may only show same-origin or data: images, so the server fetches each site's icon. */
async function loadSiteIcon(host: string, signal: AbortSignal): Promise<string | null> {
  try {
    const response = await apiRequest<Response>(`/v2/research/favicons/${encodeURIComponent(host)}`, { accept: "image/*", raw: true, signal });
    const type = (response.headers.get("Content-Type") ?? "").split(";")[0]!.trim().toLowerCase();
    if (!/^image\/(png|x-icon|vnd\.microsoft\.icon|gif|jpeg|webp)$/.test(type)) return null;
    const bytes = new Uint8Array(await response.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return `data:${type};base64,${btoa(binary)}`;
  } catch {
    return null;
  }
}

function useSiteIcon(host: string) {
  return useQuery({
    queryKey: ["siteIcon", host],
    queryFn: ({ signal }) => loadSiteIcon(host, signal),
    staleTime: Infinity,
    gcTime: 60 * 60 * 1000,
    retry: false,
  });
}

/** A site's icon, or its initial while the icon loads or when the site has none. */
export function SiteIcon({ host }: { host: string }) {
  const icon = useSiteIcon(host).data;
  const initial = host.replace(/^www\./, "").charAt(0).toUpperCase();
  return icon
    ? <img className="site-icon" src={icon} alt="" width={16} height={16} />
    : <span className="site-icon site-initial" aria-hidden="true">{initial}</span>;
}

/** Overlapping icons for the sites a step or an answer drew on; the names are in the tooltip. */
export function SiteIcons({ hosts, max = 3 }: { hosts: readonly string[]; max?: number }) {
  if (hosts.length === 0) return null;
  const names = hosts.map((host) => host.replace(/^www\./, ""));
  return (
    <span className="site-icons" title={names.join("、")} aria-label={`来源网站：${names.join("、")}`} role="img">
      {hosts.slice(0, max).map((host) => <SiteIcon key={host} host={host} />)}
    </span>
  );
}

import type { ResearchV2 } from "@foresightnine/contracts";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, useMatch, useNavigate } from "react-router-dom";
import { useTopics, queryKeys } from "../api/queries";
import { isActiveStatus, listRuns } from "../api/research";
import { signOut } from "../auth/account";
import { DATE_GROUP_LABEL, dateGroup, type DateGroup } from "../lib/format";
import { topicTitle } from "../lib/research";
import { StatusDot } from "./controls";
import { Icon } from "./icons";

type Tone = "running" | "done" | "partial" | "failed" | "none";

function topicTone(topic: ResearchV2.ResearchTopic): Tone {
  const versions = topic.versions;
  const turns = versions[versions.length - 1]?.content.turns ?? [];
  const last = turns[turns.length - 1];
  if (!last) return "none";
  return last.status === "partial" ? "partial" : "done";
}

const GROUP_ORDER: DateGroup[] = ["today", "week", "earlier"];

export function Sidebar({ drawer, open, onClose, onToggle }: { drawer: boolean; open: boolean; onClose: () => void; onToggle: () => void }) {
  const navigate = useNavigate();
  const topics = useTopics();
  const match = useMatch("/research/:topicId");
  const currentId = match?.params.topicId ?? null;
  const [filter, setFilter] = useState("");
  const asideRef = useRef<HTMLElement>(null);

  // Running state is only known for the open topic; it shares the topic page's runs cache.
  const runs = useQuery({
    queryKey: queryKeys.runs(currentId ?? ""),
    queryFn: ({ signal }) => listRuns(currentId ?? "", signal),
    enabled: currentId !== null,
  });
  const currentRunning = runs.data?.some((item) => isActiveStatus(item.operation.status)) ?? false;

  // A collapsed sidebar or closed drawer must not keep keyboard focus targets.
  useEffect(() => {
    if (asideRef.current) asideRef.current.inert = !open;
  }, [open]);

  useEffect(() => {
    if (!drawer || !open) return undefined;
    asideRef.current?.querySelector<HTMLElement>("button, a, input")?.focus({ preventScroll: true });
    const handler = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [drawer, open, onClose]);

  const groups = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const sorted = [...(topics.data ?? [])].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
    const result = new Map<DateGroup, ResearchV2.ResearchTopic[]>();
    for (const topic of sorted) {
      if (needle && !topicTitle(topic).toLowerCase().includes(needle)) continue;
      const group = dateGroup(topic.updated_at);
      result.set(group, [...(result.get(group) ?? []), topic]);
    }
    return GROUP_ORDER.flatMap((group) => (result.has(group) ? [[group, result.get(group)!] as const] : []));
  }, [topics.data, filter]);

  const className = `side${drawer && open ? " open" : ""}`;
  return (
    <aside ref={asideRef} className={className} aria-label="侧栏">
      <div className="side-head">
        <span className="brand-mark" aria-hidden="true">F9</span>
        <span className="grow" />
        <button type="button" className="icon-btn" onClick={() => navigate("/research")} aria-label="新研究" title="新研究">
          <Icon name="compose" />
        </button>
        <button type="button" className="icon-btn" onClick={onToggle} aria-label="收起侧栏" title="收起侧栏">
          <Icon name="sidebar" />
        </button>
      </div>
      <label className="search">
        <Icon name="search" size={16} />
        <input value={filter} onChange={(event) => setFilter(event.target.value)} aria-label="搜索研究" placeholder="搜索" type="search" />
      </label>
      {topics.isLoading ? <div className="empty">正在加载…</div> : null}
      {topics.isError ? <div className="empty">研究列表暂时无法加载。</div> : null}
      {topics.data && groups.length === 0 ? <div className="empty">{filter.trim() ? "没有匹配的研究。" : "还没有研究。"}</div> : null}
      <nav aria-label="研究列表">
        {groups.map(([group, items]) => (
          <div key={group}>
            <div className="group-label">{DATE_GROUP_LABEL[group]}</div>
            {items.map((topic) => {
              const current = topic.topic_id === currentId;
              const tone = current && currentRunning ? "running" : topicTone(topic);
              const title = topicTitle(topic);
              return (
                <Link key={topic.topic_id} className="row" to={`/research/${encodeURIComponent(topic.topic_id)}`} aria-current={current ? "page" : undefined} title={title}>
                  <StatusDot tone={tone} />
                  <span className="q">{title}</span>
                </Link>
              );
            })}
          </div>
        ))}
      </nav>
      <div className="foot">
        <NavLink to="/settings"><Icon name="settings" />设置</NavLink>
        <button type="button" onClick={() => void signOut()}><Icon name="logout" />退出登录</button>
      </div>
    </aside>
  );
}

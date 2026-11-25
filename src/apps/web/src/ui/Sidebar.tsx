import type { ResearchV2 } from "@foresightnine/contracts";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useMatch, useNavigate } from "react-router-dom";
import { useTopicActions, useTopics, useTurnStatuses } from "../api/queries";
import { useRadarHealth } from "../api/radar";
import { ApiError } from "../api/http";
import { exportFull, latestVersion } from "../api/research";
import { signOut } from "../auth/account";
import { useSession } from "../auth/session";
import { copyText } from "../lib/clipboard";
import { saveJson } from "../lib/download";
import { DATE_GROUP_LABEL, dateGroup, type DateGroup } from "../lib/format";
import { describeError } from "../pages/research/errors";
import { ConfirmDialog } from "./ConfirmDialog";
import { topicTitle } from "../lib/research";
import { StatusDot } from "./controls";
import { Icon } from "./icons";
import { Menu, type MenuEntry } from "./Menu";
import { showToast } from "./toast";
import { useLongPress } from "./useLongPress";

type Tone = "running" | "waiting" | "done" | "partial" | "failed" | "stopped" | "none";

function topicTone(topic: ResearchV2.ResearchTopic): Tone {
  const versions = topic.versions;
  const turns = versions[versions.length - 1]?.content.turns ?? [];
  const last = turns[turns.length - 1];
  if (!last) return "none";
  const lifecycle = (last as ResearchV2.ResearchTurn & { lifecycle_status?: "completed" | "stopped" }).lifecycle_status;
  if (lifecycle === "completed") return "done";
  if (lifecycle === "stopped" || String(last.reason) === "provider_cancelled") return "stopped";
  return last.status === "partial" ? "partial" : "done";
}

const GROUP_ORDER: DateGroup[] = ["today", "week", "earlier"];
/** Only states that differ from “done” are marked; a column of green dots says nothing. */
const TONE_LABEL: Partial<Record<Tone, string>> = { running: "研究中", waiting: "等待你的补充", done: "已完成", partial: "研究限制", failed: "失败", stopped: "已停止" };

export function topicLink(topicId: string): string {
  return `${window.location.origin}/research/${encodeURIComponent(topicId)}`;
}

async function exportTopic(topic: ResearchV2.ResearchTopic): Promise<void> {
  const version = latestVersion(topic);
  try {
    saveJson(await exportFull(topic.topic_id, version.version_id), `foresightnine-${topic.topic_id}-${version.version_id}.json`);
  } catch (error) {
    showToast(error instanceof ApiError && error.status === 403 ? "有资料不允许导出，无法导出完整记录。" : "导出失败，请稍后重试。", { tone: "error" });
  }
}

/**
 * One research row. The same menu opens from the visible “···” button, a right-click, a touch
 * long-press, or the keyboard (Menu key / Shift+F10): the context menu is a shortcut, never the
 * only way to an action.
 */
function TopicRow({ topic, current, tone, items, renaming, onRename }: {
  topic: ResearchV2.ResearchTopic; current: boolean; tone: Tone; items: MenuEntry[];
  renaming: boolean; onRename: (title: string | null) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const longPress = useLongPress(() => setMenuOpen(true));
  const title = topicTitle(topic);
  if (renaming) return <RenameField initial={title} onDone={onRename} />;
  return (
    <div
      className={`row-wrap${menuOpen ? " menu-open" : ""}`}
      onContextMenu={(event) => { event.preventDefault(); setMenuOpen(true); }}
      {...longPress}
    >
      <Link
        className="row"
        to={`/research/${encodeURIComponent(topic.topic_id)}`}
        aria-current={current ? "page" : undefined}
        title={title}
        onKeyDown={(event) => {
          if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
            event.preventDefault();
            setMenuOpen(true);
          }
        }}
      >
        <span className="q">{title}</span>
        {TONE_LABEL[tone] ? <><StatusDot tone={tone === "stopped" ? "failed" : tone === "waiting" ? "partial" : tone} /><span className="sr-only">{TONE_LABEL[tone]}</span></> : null}
      </Link>
      <Menu
        label={`“${title}”的操作`}
        align="start"
        trigger={<Icon name="more" size={16} />}
        triggerClassName="row-more"
        items={items}
        open={menuOpen}
        onOpenChange={setMenuOpen}
      />
    </div>
  );
}

/** Finder-style in-place rename: Enter or leaving the field saves, Esc cancels. */
function RenameField({ initial, onDone }: { initial: string; onDone: (title: string | null) => void }) {
  const [value, setValue] = useState(initial);
  const done = useRef(false);
  const finish = (title: string | null) => {
    if (done.current) return;
    done.current = true;
    onDone(title);
  };
  return (
    <div className="row-wrap">
      <input
        className="row-edit"
        aria-label="研究名称"
        value={value}
        maxLength={200}
        autoFocus
        onFocus={(event) => event.currentTarget.select()}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.nativeEvent.isComposing) { event.preventDefault(); finish(value); }
          if (event.key === "Escape") { event.preventDefault(); finish(null); }
        }}
        onBlur={() => finish(value)}
      />
    </div>
  );
}

export function Sidebar({ drawer, open, onClose, onToggle }: { drawer: boolean; open: boolean; onClose: () => void; onToggle: () => void }) {
  const navigate = useNavigate();
  const topics = useTopics();
  const match = useMatch("/research/:topicId");
  const inboxActive = Boolean(useMatch("/inbox"));
  const radarHealth = useRadarHealth();
  const unreadInbox = radarHealth.data?.unread_inbox_count ?? 0;
  const currentId = match?.params.topicId ?? null;
  const [filter, setFilter] = useState("");
  const asideRef = useRef<HTMLElement>(null);
  const session = useSession();
  const email = session?.account.email ?? "账户";
  const actions = useTopicActions();
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<ResearchV2.ResearchTopic | null>(null);

  // One account-scoped snapshot keeps every visible topic in sync across tabs.
  const statuses = useTurnStatuses();
  const statusByTopic = new Map((statuses.data ?? []).map((status) => [status.topic_id, status]));

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

  // Pinned topics form their own group on top (most recently pinned first); the rest group by date.
  const groups = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const sorted = [...(topics.data ?? [])].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
    const pinned: ResearchV2.ResearchTopic[] = [];
    const result = new Map<DateGroup, ResearchV2.ResearchTopic[]>();
    for (const topic of sorted) {
      // Search the summarised title and the question as asked.
      if (needle && ![topicTitle(topic), topic.request.question ?? ""].some((text) => text.toLowerCase().includes(needle))) continue;
      if (topic.pinned_at) { pinned.push(topic); continue; }
      const group = dateGroup(topic.updated_at);
      result.set(group, [...(result.get(group) ?? []), topic]);
    }
    pinned.sort((a, b) => Date.parse(b.pinned_at ?? "") - Date.parse(a.pinned_at ?? ""));
    return [
      ...(pinned.length > 0 ? [["置顶", pinned] as const] : []),
      ...GROUP_ORDER.flatMap((group) => (result.has(group) ? [[DATE_GROUP_LABEL[group], result.get(group)!] as const] : [])),
    ];
  }, [topics.data, filter]);

  const rename = (topic: ResearchV2.ResearchTopic, title: string | null) => {
    setRenamingId(null);
    if (title === null) return;
    const next = title.trim();
    // Saving the shown name unchanged is a no-op; clearing it restores the question as the name.
    if (next === topicTitle(topic) || (!next && !topic.title)) return;
    actions.update.mutate({ topicId: topic.topic_id, update: { title: next } }, {
      onError: (error) => showToast(`重命名失败：${describeError(error)}`, { tone: "error" }),
    });
  };

  const confirmDelete = (topic: ResearchV2.ResearchTopic) => {
    actions.remove.mutate(topic.topic_id, {
      onSuccess: () => {
        setDeleting(null);
        if (topic.topic_id === currentId) navigate("/research");
        showToast(`已删除“${topicTitle(topic)}”`, {
          action: {
            label: "撤销",
            onAction: () => actions.restore.mutate(topic.topic_id, {
              onSuccess: () => showToast("已恢复"),
              onError: (error) => showToast(`恢复失败：${describeError(error)}`, { tone: "error" }),
            }),
          },
        });
      },
    });
  };

  const actionsFor = (topic: ResearchV2.ResearchTopic, running: boolean): MenuEntry[] => [
    { key: "rename", label: "重命名", icon: "pen", onSelect: () => setRenamingId(topic.topic_id) },
    {
      key: "pin", label: topic.pinned_at ? "取消置顶" : "置顶", icon: "pin",
      onSelect: () => actions.update.mutate({ topicId: topic.topic_id, update: { pinned: !topic.pinned_at } }, {
        onError: (error) => showToast(describeError(error), { tone: "error" }),
      }),
    },
    "separator",
    {
      key: "copy-link", label: "复制链接", icon: "link",
      onSelect: () => void copyText(topicLink(topic.topic_id)).then((ok) => showToast(ok ? "已复制链接" : "无法访问剪贴板", { tone: ok ? "info" : "error" })),
    },
    { key: "export", label: "导出完整记录", icon: "download", onSelect: () => void exportTopic(topic) },
    {
      key: "fork", label: "分叉为新研究", icon: "fork",
      onSelect: () => actions.fork.mutate({ topicId: topic.topic_id, fork: { version_id: latestVersion(topic).version_id } }, {
        onSuccess: (created) => { navigate(`/research/${encodeURIComponent(created.topic_id)}`); showToast("已创建分支"); },
        onError: (error) => showToast(`分叉失败：${describeError(error)}`, { tone: "error" }),
      }),
    },
    "separator",
    { key: "delete", label: running ? "删除（研究进行中）" : "删除…", icon: "trash", destructive: true, disabled: running, onSelect: () => setDeleting(topic) },
  ];

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
      <Link
        className="side-inbox-link"
        to="/inbox"
        aria-current={inboxActive ? "page" : undefined}
      >
        <span>变化收件箱</span>
        {unreadInbox > 0 ? <span className="side-inbox-badge">{unreadInbox}</span> : null}
      </Link>
      {topics.isLoading ? <div className="empty">正在加载…</div> : null}
      {topics.isError ? <div className="empty">研究列表暂时无法加载。</div> : null}
      {topics.data && groups.length === 0 ? <div className="empty">{filter.trim() ? "没有匹配的研究。" : "还没有研究。"}</div> : null}
      <nav aria-label="研究列表">
        {groups.map(([label, items]) => (
          <div key={label}>
            <div className="group-label">{label}</div>
            {items.map((topic) => {
              const current = topic.topic_id === currentId;
              const status = statusByTopic.get(topic.topic_id);
              const isSquadStatus =
                (status as { run_mode?: string } | undefined)?.run_mode === "earnings_squad"
                || (status?.version_id ?? "").startsWith("dossier_");
              const savedVersion = isSquadStatus
                ? Boolean(status?.version_id)
                : Boolean(status?.version_id && topic.versions.some((version) => version.version_id === status.version_id));
              const currentTone: Tone | null = status
                ? status.logical_status === "running" ? "running"
                  : status.logical_status === "awaiting_user" ? "waiting"
                  : status.logical_status === "stopped" ? "stopped"
                  : status.logical_status === "failed" ? "failed"
                  : !savedVersion ? "running"
                  : "done"
                : null;
              const tone = currentTone ?? topicTone(topic);
              return (
                <TopicRow
                  key={topic.topic_id}
                  topic={topic}
                  current={current}
                  tone={tone}
                  items={actionsFor(topic, tone === "running" || tone === "waiting")}
                  renaming={renamingId === topic.topic_id}
                  onRename={(title) => rename(topic, title)}
                />
              );
            })}
          </div>
        ))}
      </nav>
      {deleting ? (
        <ConfirmDialog
          title={`删除“${topicTitle(deleting)}”？`}
          description="这项研究会从列表中移除，30 天内可以撤销删除，之后连同它的版本和结论一起清除。添加过的资料仍保留在你的账户中。"
          error={actions.remove.isError ? describeError(actions.remove.error) : null}
          onCancel={() => { setDeleting(null); actions.remove.reset(); }}
          actions={[{ key: "delete", label: actions.remove.isPending ? "正在删除…" : "删除", tone: "danger", disabled: actions.remove.isPending, onSelect: () => confirmDelete(deleting) }]}
        />
      ) : null}
      <div className="foot">
        <Menu
          label="账户"
          align="start"
          triggerClassName="account"
          trigger={<><span className="avatar" aria-hidden="true">{email.slice(0, 1).toUpperCase()}</span><span className="q">{email}</span></>}
          items={[
            { key: "settings", label: "设置", icon: "settings", onSelect: () => navigate("/settings") },
            "separator",
            { key: "logout", label: "退出登录", icon: "logout", onSelect: () => void signOut() },
          ]}
        />
      </div>
    </aside>
  );
}

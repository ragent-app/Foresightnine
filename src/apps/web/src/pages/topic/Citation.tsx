import type { ResearchV2 } from "@foresightnine/contracts";
import * as Popover from "@radix-ui/react-popover";
import { useState } from "react";
import { VERIFICATION_LABEL } from "../../lib/copy";
import { MEDIA, useCloseOnResize, useMedia } from "../../ui/media";
import { Panel } from "../../ui/Panel";
import { useTopicView } from "./context";

const CITATION_CHECK = VERIFICATION_LABEL.numeric_and_location_match_semantics_not_verified ?? "数字与位置已核对，语义未核验";

function Range({ citation }: { citation: ResearchV2.Citation }) {
  return <>字符 {citation.location.character_start}–{citation.location.character_end}</>;
}

/**
 * Spec §3.3 引用浮层 and §3.4 rule 7: a glass popover from 600px, a bottom sheet below.
 * The popover is non-modal (Radix Popover modal mounts a CSP-blocked scroll lock).
 */
export function CitationButton({ n, citation, claimId, citationIndex }: { n: number; citation: ResearchV2.Citation; claimId: string; citationIndex: number }) {
  const view = useTopicView();
  const tablet = useMedia(MEDIA.tablet);
  const [open, setOpen] = useState(false);
  useCloseOnResize(open, () => setOpen(false));
  const label = `引用 ${n}`;

  if (!tablet) {
    return (
      <button type="button" className="cite-n" aria-label={label} aria-haspopup="dialog" onClick={() => view.openPanel({ kind: "cite", n, claimId, citationIndex })}>
        {n}
      </button>
    );
  }

  const openReader = () => {
    setOpen(false);
    view.openPanel({ kind: "reader", sourceId: citation.source_id, range: [citation.location.character_start, citation.location.character_end] });
  };
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button type="button" className="cite-n" aria-label={label}>{n}</button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="pop" side="bottom" align="start" sideOffset={6} collisionPadding={16} aria-label={label}>
          <span className="meta">{label} · {view.sourceTitle(citation.source_id)}</span>
          <q>{citation.quotation}</q>
          <span className="meta"><Range citation={citation} /> · {CITATION_CHECK}</span>
          <div className="actions">
            <button type="button" className="btn primary" onClick={openReader}>查看原文</button>
            <button type="button" className="btn" onClick={() => { setOpen(false); view.deepDive({ claimId, citationIndex, n }); }}>深挖这条引用</button>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

/** Phone variant: the citation sheet opened through the page's panel state. */
export function CitationSheet({ n, claimId, citationIndex, onClose }: { n: number; claimId: string; citationIndex: number; onClose: () => void }) {
  const view = useTopicView();
  const claim = view.content.claims?.find((item) => item.claim_id === claimId);
  const citation = claim?.citations?.[citationIndex];
  if (!citation) {
    return <Panel title={`引用 ${n}`} onClose={onClose}><p className="empty-note">这条引用已不在当前版本中。</p></Panel>;
  }
  return (
    <Panel
      title={`引用 ${n}`}
      onClose={onClose}
      bodyClassName="tight"
      footerClassName="stretch"
      footer={
        <>
          <button type="button" className="btn lg" onClick={() => view.deepDive({ claimId, citationIndex, n })}>深挖这条引用</button>
          <button
            type="button"
            className="btn primary lg"
            onClick={() => view.openPanel({ kind: "reader", sourceId: citation.source_id, range: [citation.location.character_start, citation.location.character_end] })}
          >
            查看原文
          </button>
        </>
      }
    >
      <span className="meta">{view.sourceTitle(citation.source_id)} · <Range citation={citation} /></span>
      <q className="quote">{citation.quotation}</q>
      <span className="meta">{CITATION_CHECK}</span>
    </Panel>
  );
}

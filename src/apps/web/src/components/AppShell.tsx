import type { ReactNode } from "react";
import type { Stage } from "../types";

const navItems: readonly { stage: Stage; label: string; icon: string }[] = [
  { stage: "thesis", label: "新建推演", icon: "✎" },
  { stage: "challenge", label: "反方审查", icon: "□" },
  { stage: "worldline", label: "世界线", icon: "⑂" },
  { stage: "portfolio", label: "配置方案", icon: "◈" },
  { stage: "nav", label: "组合路径", icon: "⌁" },
  { stage: "methods", label: "方法与风险", icon: "♢" },
];

interface AppShellProps {
  readonly stage: Stage;
  readonly onStage: (stage: Stage) => void;
  readonly children: ReactNode;
  readonly inspector?: ReactNode;
}

export function AppShell({ stage, onStage, children, inspector }: AppShellProps) {
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">跳到主要内容</a>
    <aside className="rail" aria-label="研究导航">
      <div className="brand">ForesightNine</div>
      <nav className="rail-nav">
        {navItems.map((item) => <button key={item.stage} className={`rail-item ${stage === item.stage ? "is-active" : ""}`} aria-current={stage === item.stage ? "page" : undefined} onClick={() => onStage(item.stage)}>
          <span className="rail-icon" aria-hidden="true">{item.icon}</span><span>{item.label}</span>
        </button>)}
      </nav>
      <div className="rail-footer">
        <div className="rail-rule" />
        <span aria-hidden="true">♢</span>
        <span>公开研究模式 · 不连接券商 · 不托管资金</span>
      </div>
    </aside>
    <main id="main-content" className="main-sheet">{children}</main>
    {inspector ? <aside className="inspector" aria-label="研究侧栏">{inspector}</aside> : null}
  </div>;
}

export function Header({ eyebrow, title, action }: { readonly eyebrow?: string; readonly title: string; readonly action?: ReactNode }) {
  return <header className="page-header">
    <div>{eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}<h1>{title}</h1></div>
    {action ? <div className="header-action">{action}</div> : null}
  </header>;
}

export function Disclosure({ compact = false }: { readonly compact?: boolean }) {
  return <p className={`disclosure ${compact ? "disclosure-compact" : ""}`}><span aria-hidden="true">♢</span> {compact ? "情景模拟，不是预测或收益承诺。" : "本产品用于情景研究与教育，不构成投资建议；不连接券商、不下单、不托管资金。"}</p>;
}

# ImageGen prompt record

All concepts were generated on 2026-08-10 with the built-in ImageGen workflow for the `ui-mockup` use case. Each request required a complete production state, code-native text and controls, original branding, readable Chinese copy, and explicit exclusions: no Claude/Anthropic branding, gradients, neon, glassmorphism, bento layout, decorative badges, fake return claims, brokerage controls, stock photography or third-party logos.

The shared visual prompt was:

> Original ForesightNine public scenario-research and portfolio-simulation UI. Calm editorial quality and generous whitespace, warm ivory paper, dark umber text, muted terracotta primary accent, dusty blue weak-branch accent, restrained sage strong/validated accent, serif display headings with legible sans-serif application text, fine dividers, minimal shadows, practical React/Expo controls, 14–16 px desktop chrome and 48 dp Android touch targets. Use a continuous document or conversation surface rather than a card grid.

## Web prompts

1. **Thesis input, 1536×1024 landscape.** Left rail `新建推演 / 研究档案 / 方法与风险`; heading `把观点变成可审计的世界线`; thesis editor with the data-centre electricity sample; capital `¥ 1,000,000`; CTA `开始反方审查`; steps `观点 / 证据 / 世界线 / 配置`; public-research/no-broker/no-custody disclosure.
2. **Challenge and evidence, 1536×1024 landscape.** Continuous transcript titled `反方审查`, round 4, challenge questions, rubric `28 / 40` with causal/time/counterfactual/falsifiability dimensions, an open evidence drawer with grades A/C/E, unresolved-private-premise warning, evidence and continue actions, sticky reply composer.
3. **Worldline and probability, 1536×1024 landscape.** `P(H) 42%`, `P(W₀ | H) 55%`, joint `23.1%`; horizontal now/6/12/24-month weak, main and strong branch timeline; milestones, branch selection, provenance, probability-basis inspector and unvalidated-estimate disclosure.
4. **Local FM progress, 1536×1024 landscape.** `正在用户设备上计算 · 68%`; signed snapshot, factor filter, rank/IC direction, signal normalisation and dead-band trace; factor table with IC/ICIR/quality/status; `score_f = |IC_f| × |ICIR_f|` and raw-weight equation; Web Worker and no-future-data provenance.
5. **Portfolio and stress, 1536×1024 landscape.** FM Raw/Public Default control; ¥1,000,000 allocation table and stacked bar; weak/main/strong scenario sensitivity rows; local calculation/data cutoff; research-only disclosure and export/explanation actions.
6. **Selected-worldline NAV, 1536×1024 landscape.** Large publication-quality normalised NAV chart; solid historical line ending at labelled today/simulation boundary; main median, 10–90% band, weak/strong reference paths; shared-axis drawdown strip; fixed seed, 252-day volatility and adjusted-close assumptions; simulation-not-promise disclosure.
7. **Methodology and risk, 1536×1024 landscape.** Document-style method appendix with anchored sections; evidence grades A–E; probability equations; FM Raw/Public Default comparison; method versions/data cutoff; prominent research/education, no-advice, no-broker, no-order, no-custody statement.

## Android prompts

1. **Thesis input, 1080×1920 portrait.** Single-column thesis editor, amount input, four-step progress, sticky CTA and bottom navigation with Android safe areas.
2. **Challenge conversation, 1080×1920 portrait.** Dialogue-first continuous transcript, score 28/40, rubric, private-premise warning, evidence/continue actions and sticky reply composer.
3. **Evidence workspace, 1080×1920 portrait.** One continuous A/C/E evidence list, first item expanded with publish/retrieval time, source, support and opposition, client-search import and premise actions.
4. **Worldline timeline, 1080×1920 portrait.** Compact three-layer probability strip, weak/main/strong selector, vertical event timeline with occurrence and market-visible times, expanded invalidation condition and probability-basis sheet.
5. **Portfolio NAV, 1080×1920 portrait.** FM Raw/Public Default toggle, historical/simulated NAV boundary, interval and drawdown, allocation list/bar, local-compute metadata and stress-test action.
6. **Share/export, 1080×1920 portrait.** Research preview outline, PDF/JSON/CSV formats, public-link toggle, private-premise hiding, expiry, disclosure and sticky export actions.

### Android portfolio correction pass

After original-resolution inspection, `05-portfolio-nav.png` was edited with the source image attached. The correction prompt preserved the product layout while requiring unmistakable Android status/navigation chrome, removing iOS cues, and replacing the inconsistent currency chart axis with normalized NAV levels around 0.7–1.5. The original is retained at `concepts/android/archive/05-portfolio-nav-v1.png` as QA provenance.

## Reviewer-directed correction prompts

The complete suite received a correction pass after Sol's pre-review:

- **FM correction:** replace MAD wording with cross-sectional mean and population standard deviation (`ddof=0`); define `N` as eligible assets; show clip plus re-normalisation; use arithmetically consistent `|IC|×|ICIR|` examples and negative-IC direction semantics.
- **Probability correction:** use `P(H) 35–49%`, `P(W₀|H) 45–62%`, joint `16–30%` and `model_estimate_unvalidated`; replace additive percentage ledgers with qualitative direction/reason/sensitivity.
- **NAV correction:** weak reference below main and with larger drawdown, strong above main and with smaller drawdown; use line dash, marker/direct labels; preserve today/simulation boundary.
- **Evidence and confirmation correction:** IEA forecast as B authority analysis, C as secondary commentary/expert interview, E as model inference; 28/40 shows `继续补充证据` and a separate caution `带保留意见确认` that persists unresolved issues and AI objections.
- **Portfolio correction:** scenario impact expressed as 24-month relative-NAV ranges; additional Web/Android Public Default state shows Raw→Public deltas, constraint reasons and non-overwrite invariant.
- **Android correction:** all six required states use high-resolution 9:16 artboards with Android three-button navigation; input has a CNY selector; share-link expiry is disabled while public sharing is off.
- **Recovery variants:** additional Web/Android challenge states preserve transcript, evidence and draft while recovering from `Last-Event-ID 17`; retry explicitly avoids duplicate submission or billing.

The bitmap files remain in `docs/design/concepts`; no generated image is loaded by runtime UI code.

### Final specification correction pass

- **FM executable formula:** the active Web FM concept now follows the actual client engine exactly: `r=rank_pct(signal)-0.5`, `c=sign(IC)r`, global `q=|IC||ICIR|`, per-asset valid-support weighted combination, cross-sectional z-score with `ddof=0`, clipping, positive-part transformation and final sum-to-one normalisation.
- **Evidence grade C:** the active methodology concept describes C as secondary commentary or expert interviews, preserving the A–E rubric contract.
- **Android NAV horizon:** both active Raw and Public Default portfolio concepts use history through the 2026-08-10 simulation boundary and a 24-month endpoint of 2028-08-10. Superseded bitmaps remain in the adjacent archive folders.

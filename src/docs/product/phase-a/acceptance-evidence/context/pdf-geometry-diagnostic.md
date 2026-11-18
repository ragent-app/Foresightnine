# PDF geometry: bounded local diagnostic

The historical reference is `original-final-focused.txt` (16 cases: 3 passed, 13 failed). These are historical results, not a run of the new geometry implementation. No model was called in this diagnostic; the 338 evaluation remains unchanged.

The fixed text-only fixtures for the first, annual/quarterly, GDP_AQ, GDP_REV, FED_RATE, FED_DISS, GDP_ESTIMATE, FED_PURCHASES, company-prefix, and ambiguous-table cases do not provide `original_pdf_base64` and its hash to the constructed source. The geometry path therefore cannot run for these fixtures without explicitly constructing a source from the already retained PDF. The existing evidence does not distinguish missing candidate, structural exclusion, selection and budget for each one.

| Historical case | Observable failure and supported classification |
| --- | --- |
| Chinese policy | Requested quotation absent from simulated model input; candidate/selection/structure unknown. No geometry input. |
| Annual and quarterly | One or both required passages absent from simulated model input; candidate/selection/structure unknown. Source is text-only. |
| BEA API English | Operation failed; recorded trace does not establish a unique cause. PDF was uploaded, so geometry is applicable on a new run. |
| BEA API Chinese | Operation completed but generation call was absent; exact gating cause unknown from this log. PDF was uploaded. |
| GDP_AQ | Required original passage absent from bounded input; candidate/selection/structure unknown. No geometry input. |
| GDP_REV | Required original passage absent from bounded input; candidate/selection/structure unknown. No geometry input. |
| FED_RATE | Required original passage absent from bounded input; candidate/selection/structure unknown. No geometry input. |
| FED_DISS | Required original passage absent from bounded input; candidate/selection/structure unknown. No geometry input. |
| Two-PDF API | Operation failed; the existing log does not identify a unique cause. Both PDFs were uploaded. |
| GDP_ESTIMATE | Required material absent from bounded input; candidate/selection/structure unknown. No geometry input. |
| FED_PURCHASES | Required material absent from bounded input; candidate/selection/structure unknown. No geometry input. |
| Company prefix | Required passage absent from bounded input; candidate/selection/structure unknown. No geometry input. |
| Ambiguous table | No facts survived the evidence checks; source quotation was not demonstrated as selected. This does not establish that ambiguity handling passed or that a fact was absent in the document. No geometry input. |

The three historical passing cases are represented by dots in the non-verbose log; their names cannot be recovered reliably from that log alone. No case should be classified as a model-quality failure from a simulated provider assertion.

One separate read-only original check used the retained `337-stage-b-independent-acceptance/snapshots/fed-2020-june.pdf`: its unchanged four-page `-layout` text produced a hash-bound sidecar with 88 uniquely aligned lines and 8 unknown lines. This establishes only that local extraction and exact location mapping ran; it does not establish correct layout semantics, retrieval quality, or that all 88 lines are complete sentences. The selected-geometry integration and wrapped-negation exclusion have local deterministic tests. Page order, column interpretation, table headers and related source semantics remain independently unverified. Complex tables and multi-line sentences require further bounded work.

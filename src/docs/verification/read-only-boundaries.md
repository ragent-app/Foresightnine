# Read-only boundary register

All implementation writes are restricted to `D:\Foresightnine\src`. Git metadata may exist at `D:\Foresightnine\.git`; the approved governance documents at the root remain unchanged.

The following local roots are inputs only and must never be edited by this project:

- `D:\FM acceleration`
- `D:\FM acceleration-deepseek`
- `D:\WL-data-final`
- `D:\ACFM_WL_paperwriting`

The following remote roots are also inputs only when mounted or accessed through the approved server connection:

- `/home/lxx/trade-agent-crisis`
- `/home/lxx/trade-agent-benchmark`

The six exact FM files used as numerical references are pinned in `reference-sources.json`. Entire experiment/data roots are not recursively hashed because active upstream runs may legitimately append results independently of this project. The implementation process must use read/copy operations only, and any copied artefact must retain source/provenance metadata.

The historical DeepSeek credential is registered as **revoked**. Its value, prefix/suffix and recoverable hash are intentionally not recorded. Live-provider tests require a newly issued server-side environment secret and are never a default test dependency.

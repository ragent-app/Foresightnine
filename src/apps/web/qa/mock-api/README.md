# Mock research API (local UI QA)

Run: `node qa/mock-api/server.mjs` (port `MOCK_API_PORT`, default 8787), then in another shell:
`FORESIGHTNINE_DEV_API_TARGET=http://127.0.0.1:8787 VITE_FORESIGHTNINE_LOCAL_AUTH=mock corepack pnpm dev`

- All companies, sources and numbers are fictional QA data; state is in memory and resets on restart.
- Any email and a 12+ char password signs in; each new account gets 3 seeded topics.
- A run question containing `部分` ends `failed` with a saved partial turn; `失败` ends `failed` with no turn (both continuable).
- Topic C has a display-off and a simulated source (source text returns 403); topic A's source forbids export.
- Tests: `node --test qa/mock-api/server.test.mjs`. Step interval: `createServer({ tickMs })`, default 1500 ms.

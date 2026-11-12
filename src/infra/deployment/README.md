# Deployment

**Phase 9 release-candidate deployment notes — pending security, operations, legal, compliance, and release approval. This is not a public-launch declaration.**

Public deployment is a gated release-candidate exercise. The deployer must record the exact commit, environment, data-provider manifest, method version, and rollback target. Offline fixtures may support QA only and must remain labelled as historical/simulated; they are not a substitute for licensed production data.

## Required gates before public production

- target-region legal review, approved privacy policy, service terms, and risk disclosure;
- written market-data and FX licensing/redistribution rights for every production field, symbol, cache, export, and client-delivered shard;
- a newly issued server-side provider key injected through the deployment secret manager or an uncommitted environment; no key in Web/Android bundles, source maps, logs, fixtures, or databases;
- CAPTCHA site/secret configuration, abuse thresholds, IP/session quotas, and daily model-call budgets;
- explicit edge topology: `FORESIGHTNINE_PROXY_MODE=direct` with no trusted
  proxy CIDRs, or `reverse_proxy` with the actual ingress networks in
  `FORESIGHTNINE_TRUSTED_PROXY_CIDRS`; forwarded headers must never be trusted
  from an unlisted peer;
- Redis provisioned with TLS/authentication (the production preflight requires
  a `rediss://` URL), bounded TTLs, backup/restore expectations, and a tested
  failure mode for rate limiting, short-lived cache, and async job state;
- UUID gate: use an approved cryptographically secure UUID generator, validate UUID fields at API boundaries, and verify collision/invalid-input tests before accepting public identifiers;
- observability, redacted logging, alert ownership, incident runbooks, kill switches, and rollback evidence;
- secret scan, dependency/container audit, CORS/CSP review, SSRF review, and data-deletion/retention approval;
- Android release signing: owner-controlled keystore and the four external signing values described in `../../docs/security/android-release-signing.md`; debug APKs are QA-only;
- Android package/application identity, Expo/EAS credentials, Play Console enrollment, privacy declarations, and release artifact provenance approved by the release owner.

## Deployment sequence

1. Verify the repository boundary and generated contracts, run domain/Web/Android/API checks, and record results.
2. Verify the licensed provider manifest and freshness policy configuration. A missing, stale, unavailable, invalid, future-dated, or unsigned snapshot must disable current Public Default amounts.
3. Inject secrets and environment values only in the deployment context; run a redacted configuration check.
4. Apply database migrations and Redis connectivity/TTL checks through the approved change process.
5. Deploy API and clients to a private/staged environment, run smoke, abuse, rollback, and client-parity tests, and inspect logs for secrets or personal data.
6. Obtain written approval from legal, security, privacy, operations, data licensing, and release owners before changing traffic exposure.

## Reproducible staging backup

The local Compose stack has a repository-external backup helper. It writes a
PostgreSQL custom dump, a Redis RDB snapshot, and a SHA-256 sidecar without
printing credentials. The files are not redacted and may contain private
research, account data, and sensitive configuration: restrict access and
apply the deployment's encryption and retention policy:

```bash
bash src/infra/deployment/backup-staging.sh /absolute/path/outside/Foresightnine-backups
```

The helper has no restore mode. A restore is an owner-approved destructive
operation and must be performed into a separately verified database/Redis
target before any traffic is exposed. Do not put backup files in the Git
checkout or commit them.

To exercise research backup and restore without touching the Compose data,
run the opt-in test from `src` with Docker available:

```bash
FORESIGHTNINE_TEST_DISPOSABLE_DOCKER_BACKUP=1 uv run --project apps/api pytest apps/api/tests/test_research_backup_restore.py -q
```

It creates two uniquely named temporary PostgreSQL containers, writes synthetic
accounts and research data, restores a custom dump into the empty second
container, verifies access and operation replay, and removes the containers.
This drill does not test production backup policies, external file stores,
Redis recovery, encryption, or a live environment's credentials.

No step above authorizes brokerage connectivity, order routing, custody, or real-money execution. Those remain out of scope and would require a separate approved product and regulatory design.

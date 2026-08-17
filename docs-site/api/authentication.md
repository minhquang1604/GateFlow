# Authentication

Two credentials are accepted, and the difference between them is the
point.

## Scoped API keys

A **scoped API key** resolves to a named principal, and that name is
what lands in `AuditLog.actor` — derived from a credential the caller
had to possess, so an audit row is evidence rather than a claim.

```bash
# The first key cannot be minted through the API (that needs `admin`),
# so it is created against the database directly — same trust boundary
# as running a migration.
python -m mlops_framework.auth.cli create alice --scopes admin
#     mlops_ak_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
#     This is the only time it will be shown.

curl -X POST localhost:8000/api/api-keys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"name": "airflow-dag", "scopes": ["write"]}'

curl -X POST localhost:8000/api/model-versions/12/rollback \
  -H "Authorization: Bearer $ALICE_KEY"      # actor = "alice", verified
```

| Scope | Grants |
|---|---|
| `read` | Every GET the console renders from |
| `write` | Anything that changes state — promote, rollback, start a run, schedules, policies. Implies `read` |
| `admin` | Managing keys. Implies `write` |

Only a **hash** of a key is stored; the plaintext is returned once and
is unrecoverable, so a database dump yields no usable credential.
Revocation sets a timestamp rather than deleting the row — a key that
acted has to stay resolvable for as long as the audit rows naming it do.

## The shared secret

The **shared secret** (`CONSOLE_WRITE_TOKEN`, `X-Console-Token`) still
works and grants `write`. It is what closed the anonymous-write hole and
what every current deployment is configured with; a request using it
records the unverified `X-Actor` header as its actor, exactly as before.
It deliberately does **not** grant `admin`: a shared secret that could
mint per-principal keys would let anyone holding it manufacture
identities.

!!! warning "Not a long-term credential"
    See [Known Limitations](../operations/known-limitations.md) — the
    shared secret is kept only because every existing deployment,
    including the Airflow DAG, is configured with it. Migrate to keys,
    then unset it.

## Error responses

Refusals distinguish the two failures:

| Status | Meaning |
|---|---|
| `401` | Nothing valid was presented (an unknown key and a wrong key are the same answer, so neither confirms that a string is real) |
| `403` | The caller is known but lacks the scope |
| `503` | The deployment has no keys and no shared secret — it cannot authenticate anyone, and saying so beats a 401 no credential could satisfy |

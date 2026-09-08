# Production deployment: how ki-ops reaches the trading pipeline

Operator runbook for shipping ki-ops to the kelaidata Airflow (MWAA)
production environment. ki-ops is never imported as source by kelaidata —
the DAG shells out to the pinned `ki-ops` CLI installed from a released
wheel. Everything below exists so that the exact bytes running in
production are always traceable to a tagged commit in this repo.

Companion reference on the consumer side: `MWAA_DEPLOYMENT.md` in the
kelaidata repo (canary/prod MWAA mechanics; this document covers the
ki-ops-specific path through it).

## End-to-end flow

```
feature branch
   │  PR (ci: pytest)
   ▼
 uat                          ← integration branch; every feature PR lands here
   │  uat -> main PR (ci + required uat-gate check)
   ▼
 main                         ← protected: PRs only, head must be uat
   │  merge triggers .github/workflows/release.yml
   ▼
GitHub release vX.Y.Z         ← tag derived automatically; wheel + sdist + SHA256SUMS
   │  kelaidata: scripts/fetch_ki_ops_wheel.sh vX.Y.Z  +  ki-ops==X.Y.Z pin
   ▼
kelaidata plugins/ wheelhouse
   │  scripts/mwaa_release.py build   (verifies wheel bytes against the release)
   ▼
s3://kelai-mwaa-dags/releases/<git-sha>/canary/   (immutable)
   │  canary MWAA validates the SHA (ki_ops_gate, nightly KOTL dry-run)
   │  scripts/mwaa_release.py record-canary        (immutable evidence)
   ▼
s3://kelai-mwaa-dags/releases/<git-sha>/prod/
   │  render-prod → mwaa_prod.sh update            (in-place, ~20-35 min)
   ▼
production MWAA (kelaidata-prod-*)
```

Two independent SHAs travel through this flow: the **ki-ops release tag**
(what wheel is installed) and the **kelaidata release git SHA** (what
DAG/plugins bundle is deployed). A ki-ops bump always rides a kelaidata
release.

## Branch and merge policy

- Feature branches PR into **`uat`**, never into `main`.
- `main` is protected: PRs only, no direct pushes, admins included.
- [`ci`](../.github/workflows/ci.yml) runs `pytest` on **every PR** (feature
  → uat and uat → main) and on pushes to `uat`. It exists because tests
  used to run only on tag pushes, which let a born-failing test land on
  `main` in PR #14.
- [`uat-gate`](../.github/workflows/uat-gate.yml) is a **required status
  check** on `main`: it fails any PR into `main` whose head branch is not
  `uat`. The only path to `main` is a `uat -> main` PR.

Net effect: `main` is always a tested superset of `uat`, and every commit
on `main` arrived through two green CI runs.

## How a release is cut

Releases are automatic. Every merge to `main` runs
[`release.yml`](../.github/workflows/release.yml), which:

1. **Derives the next tag** by bumping the patch of the latest `v*` tag
   (`v0.2.0 -> v0.2.1`). Releases are serialized (workflow `concurrency`
   group), so two merges cannot race the derivation; an already-existing
   tag fails the run.
2. **Runs the full test suite** again on the merge commit — a red `main`
   can never release.
3. **Tags the merge commit** and builds the wheel *at the tag*. The version
   comes from the tag via setuptools-scm (`pyproject.toml` has no static
   version), so a released wheel can only ever carry the version of a real
   tag.
4. **Verifies the artifact**: the wheel must be
   `ki_ops-<version>-py3-none-any.whl` (the MWAA wheelhouse install is
   offline; a platform wheel would silently fail to resolve), and the
   workflow installs the built wheel and fails unless the embedded
   `ki_ops.__git_sha__` equals the released commit.
5. **Publishes a GitHub release** with the wheel, the sdist, and
   `SHA256SUMS`.

A tag without a release is harmless (e.g. `v0.2.1`: its release run failed
after tagging, and the next merge released `v0.2.2`) — derivation always
bumps from the latest tag, never from the latest release.

**Manual major/minor bump**: run the release workflow via
`workflow_dispatch` with an explicit `version` input (e.g. `1.0.0`);
subsequent merges derive from that tag (`1.0.1`, `1.0.2`, …).

```bash
gh workflow run release.yml -R KelAI-Tech/ki-ops -f version=1.0.0
```

### Auditability: the embedded git SHA

`setup.py` writes the source commit into the wheel at build time
(`ki_ops/_build_info.py`, surfaced as `ki_ops.__git_sha__` and by
`ki-ops --version`):

```bash
$ ki-ops --version
ki-ops X.Y.Z (git <40-char sha>)
```

Any wheel — released or ad-hoc — identifies its exact source commit.
Wheels built by hand from untagged commits get setuptools-scm dev versions
like `0.2.1.dev3+g1a2b3c4`: self-identifying, and never a match for the
exact `ki-ops==X.Y.Z` pin kelaidata installs with.

## How kelaidata consumes a release

ki-ops ships to MWAA as a pure-python wheel inside kelaidata's
`plugins.zip` wheelhouse, installed offline
(`--find-links /usr/local/airflow/plugins --no-index`). On the kelaidata
side:

```bash
# in the kelaidata repo — vendor the wheel from the GitHub release:
./scripts/fetch_ki_ops_wheel.sh v0.2.2    # latest release at time of writing
```

The script downloads `ki_ops-X.Y.Z-py3-none-any.whl` from the
KelAI-Tech/ki-ops release (authenticated `gh` required), replaces any
previously vendored ki-ops wheel, and errors if the vendored version
disagrees with the `ki-ops==X.Y.Z` pin in `requirements_airflow.txt`.
Without an argument it reads the version from the active pin. Update the
pin and the wheel together.

At build time, `scripts/mwaa_release.py build` **enforces release
provenance** on every vendored ki-ops wheel before packaging `plugins.zip`:

- setuptools-scm dev/local versions (`0.2.1.dev3+g1a2b3c4`) are rejected
  outright;
- the wheel's sha256 must be byte-identical to the entry in the
  `SHA256SUMS` asset of GitHub release `v<version>` (fetched via `gh` at
  build time).

A hand-built wheel cannot impersonate a released version number, and an
untagged wheel cannot ship at all.

**Why**: an untagged `0.3.0` wheel was found running on the airflow canary
with no release behind it; identifying its source required comparing file
blob hashes against repo history to trace it back to commit `3a46a0d`.
The embedded git SHA makes that forensic exercise a one-liner
(`ki-ops --version`), and the build-time provenance check makes the
situation unrepresentable going forward.

## Canary validation

Every kelaidata release — including any ki-ops bump — is validated on the
disposable canary MWAA environment (`kelaidata-canary`, CloudFormation
stack `kelaidata-mwaa-canary`) before prod. The release is stored
immutably under `s3://kelai-mwaa-dags/releases/<git-sha>/canary/`; the
canary is pointed at that SHA (`./scripts/mwaa_canary.sh update` or
`create`).

What the canary exercises on the ki-ops path:

- **`ki_ops_gate`** — the pre-trade gate task in
  `lseg_strategy_pipeline_combo`, immediately before `dollar_to_shares`.
  It shells out to `ki-ops gate` with `--config` pointing at the runtime
  risk YAML on S3. Exit contract: `0` passed (possibly with warnings),
  `1` infra error, `2` blocking risk violation.
- **Nightly KOTL Flex submission dry-run** — the `submit_flex_trades` task
  (downstream of `dollar_to_shares`) submits an AWS Batch Fargate job
  (`kotl-submit` stack, `kelaidata/kotl-submit` image with the vendored
  ki-ops wheel + `[kelaidata,flex,db,secmaster]` extras) running
  `ki-ops kotl submit-kelai` against Flex UAT with
  `KOTL_SUBMIT_DRY_RUN=1`: SOD via Flex `ReplayPositions`, SEDOL
  resolution, orders staged, trade file archived under `trades/canary/`,
  ledger persisted — nothing transmitted.
- **KOTL trade ledger** — the ledger (`kotl_submits`,
  `kotl_working_orders`, `kotl_submit_claims`) is the ground truth the
  submit reads before any order reaches Flex. The canary uses the canary
  namespace (`kelai_canary` on the shared dev MySQL instance); a dedicated
  per-environment ledger RDS (`kotl-db-<env>`, kelaidata
  `infra/rds-kotl.yaml`, secret `kelai/kotl/db-<env>`) replaces the shared
  kelaisim instances, cut over via the `KOTL_DB_SECRET_ID` /
  `KOTL_DB_SCHEMA` runtime variables — never mid-trading-day (the
  once-a-day submission claim lives in the ledger; switching instances
  between a claim and its submission would let a re-run claim the day
  twice).

Evidence that must be green before promotion: all canary DAGs green in
the isolated canary namespaces, the `ki_ops_gate` verdict JSON passing,
and a `SUCCEEDED` KOTL dry-run Batch job. The operator then records
immutable evidence — prod command rendering **refuses to run without it**:

```bash
# in the kelaidata repo:
./scripts/mwaa_release.py record-canary --release-sha "$RELEASE_SHA" \
  --evidence infra/mwaa/canary-evidence.json --bucket kelai-mwaa-dags --region us-east-1
```

## Prod promotion

Steady state (production on a stack-managed `kelaidata-prod-*`
environment): each release is an in-place CloudFormation update, ~20-35
minutes, no cutover, run history preserved. In the kelaidata repo:

```bash
./scripts/mwaa_release.py render-prod --environment "$PROD_MWAA_ENVIRONMENT" \
  --release-sha "$RELEASE_SHA" --bucket kelai-mwaa-dags --region us-east-1
# review; write rendered params to infra/mwaa/prod-parameters.json;
# SAVE the printed rollback parameter file outside the repo.

./scripts/mwaa_prod.sh update --parameters infra/mwaa/prod-parameters.json
```

`render-prod` refuses to render without recorded canary evidence for the
exact release SHA. Full mechanics (first-ever promotion, capacity, IAM,
verification) are in kelaidata's `MWAA_DEPLOYMENT.md`.

### Rollback

Two levels, cheapest first:

1. **Repin ki-ops only** (bad ki-ops release, kelaidata otherwise fine):
   in kelaidata, set `ki-ops==<previous>` in `requirements_airflow.txt`,
   re-vendor (`./scripts/fetch_ki_ops_wheel.sh v<previous>`), rebuild
   (`mwaa_release.py build`), canary-validate, promote. Released wheels
   are immutable GitHub assets, so any prior version is always
   re-fetchable.
2. **Release-prefix rollback** (roll the whole kelaidata deployment back):
   rerun `./scripts/mwaa_prod.sh update` with the saved rollback parameter
   file from the rendered promotion output — it pins the release prefix
   and VersionIds currently live before the update.

### Emergency risk-limit changes: no redeploy

The gate's risk limits are **not** baked into the wheel. The `ki_ops_gate`
task passes `--config s3://kelaitrading/config/ki_ops/risk_management_sma_ima.yaml`,
read at runtime through the ETag cache. Source of record is
[`config/risk_management_sma_ima.yaml`](../config/risk_management_sma_ima.yaml)
in this repo; the runtime copy is the S3 object.

```bash
# change a limit: edit the repo copy (PR into uat), then upload:
aws s3 cp config/risk_management_sma_ima.yaml \
  s3://kelaitrading/config/ki_ops/risk_management_sma_ima.yaml
```

Effective on the next gate run — no wheel release, no MWAA redeploy.
`KI_OPS_GATE_CONFIG` (runtime env, like `KI_OPS_GATE_ENABLED`) repoints
the gate at a different YAML, e.g. for a canary-only experiment.

## Operator checklist: ship a ki-ops change to prod

In ki-ops:

1. PR the feature branch into `uat`; `ci` must be green.
2. Open (or update) the `uat -> main` PR; `ci` + `uat-gate` must be green;
   merge.
3. The release workflow tags and publishes automatically. Confirm:

   ```bash
   gh release view --repo KelAI-Tech/ki-ops   # latest; note vX.Y.Z
   ```

   (Major/minor bump instead:
   `gh workflow run release.yml -R KelAI-Tech/ki-ops -f version=X.Y.0`.)

In kelaidata (clean, reviewed `uat` commit; `git status --short` empty):

4. Vendor the wheel and align the pin:

   ```bash
   ./scripts/fetch_ki_ops_wheel.sh vX.Y.Z
   # set ki-ops==X.Y.Z in requirements_airflow.txt (PR into kelaidata uat)
   ```

5. Build the immutable releases (runs tests; verifies wheel provenance):

   ```bash
   export RELEASE_SHA="$(git rev-parse HEAD)"
   ./scripts/mwaa_release.py build --target canary --bucket kelai-mwaa-dags --region us-east-1
   ./scripts/mwaa_release.py build --target prod   --bucket kelai-mwaa-dags --region us-east-1
   ```

6. Point the canary at the release and validate:

   ```bash
   # copy the new ReleaseSha + VersionIds from the release manifest into
   # infra/mwaa/canary-parameters.json, then:
   ./scripts/mwaa_canary.sh update --parameters infra/mwaa/canary-parameters.json
   ```

   Green required: `ki_ops_gate` verdict, nightly KOTL dry-run Batch job,
   all canary DAGs in their namespaces.

7. Record canary evidence:

   ```bash
   ./scripts/mwaa_release.py record-canary --release-sha "$RELEASE_SHA" \
     --evidence infra/mwaa/canary-evidence.json --bucket kelai-mwaa-dags --region us-east-1
   ```

8. Render and apply the prod update (save the rollback parameter file):

   ```bash
   ./scripts/mwaa_release.py render-prod --environment "$PROD_MWAA_ENVIRONMENT" \
     --release-sha "$RELEASE_SHA" --bucket kelai-mwaa-dags --region us-east-1
   ./scripts/mwaa_prod.sh update --parameters infra/mwaa/prod-parameters.json
   ```

9. Verify on prod: environment `AVAILABLE`, no DAG import errors, next
   `ki_ops_gate` run green, and the deployed version matches:

   ```bash
   # in the gate task logs, the verdict JSON carries ki_ops_version;
   # or from any environment with the wheel installed:
   ki-ops --version    # ki-ops X.Y.Z (git <released sha>)
   ```

10. Roll back if needed: repin the previous ki-ops version + rebuild
    (step 4-5), or rerun step 8's update with the saved rollback
    parameter file.

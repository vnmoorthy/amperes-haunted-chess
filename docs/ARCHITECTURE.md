# Architecture

## Data flow

```
 customer records (JSONL / CSV package)          MuJoCo simulator runs
            │  adapter (read-only)                      │ runner + recorder
            ▼                                           ▼
     canonical Episode (amperes.episode/1)  ◄───────────┘   (one schema for both)
            │
            ├─► verifier (only if the record carries simulator state) ─► outcome.verified
            ├─► quality rules (per episode + dataset level) ─► findings ─► episode quality status
            ├─► operational events (dedup by content digest, linked to episodes)
            ▼
   SQLite index (amperes.db) + gzip JSON artifacts (artifacts/)
            │
            ├─► incidents (deterministic grouping, status history) ─► regression cases (if replay class A/B)
            ├─► ops metrics / bottlenecks / correlation (explicit numerators, denominators, windows)
            ├─► human review + workflow-timing study
            └─► reports: experiment report + bundle, incident report, pilot readiness report
```

## Module boundaries

| Module | Responsibility | Must not |
|---|---|---|
| `schema.py` | Data contract. Unknown means `None`; non-finite values are serialised as strings so files stay strict JSON | Fill in missing values |
| `ingest/base.py` | `Adapter` protocol: `detect`, `parse → ParsedRecord(episode, errors)`, `parse_events` | Write to the source |
| `ingest/pipeline.py` | Stable ids `ep_<sha256(dataset/source_id@record_ref)>`, normalised copies, QC, indexing, source fingerprint | Infer outcomes |
| `quality.py` | `Rule(id, version, dimension, deterministic, applies, check)`; `episode_status` precedence | Produce a composite score that hides defects |
| `incidents.py` | Grouping by exact keys (rule id / task+policy+category / event class+code), allowed transitions, history | Merge by text similarity |
| `ops.py` | Event model, yield, throughput, reliability, bottlenecks (B001–B007), correlation | Treat missing data as zero; assert causes |
| `review.py` | Append-only reviews, precision among reviewed, counterbalanced study | Derive recall from reviews |
| `economics.py` | Value only from measured differences plus explicit assumptions | Produce ROI without inputs |
| `sim/env.py` | MJCF generation from validated `EnvParams`; reset/step | Expose ground truth to the policy except via declared observations |
| `sim/policies.py` | `PolicyAdapter` protocol; scripted baseline; action replay; `make_policy` registry | Load code from experiment files |
| `sim/verifier.py` | Outcome from `state.*` streams + termination only | Read policy annotations or model text |
| `sim/taxonomy.py` | Deterministic categories with evidence, confidence, causal note | Claim causes |
| `sim/reproduce.py` | Replay classes A–E, eligibility, pre-declared criteria | Call a category match "exact" |
| `sim/experiment.py` | Steps 1–8, dev/held-out separation, frozen selection | Pass held-out data to selection |

## Storage model

`$AMPERES_HOME` (default `./amperes_data`):

- `amperes.db` (SQLite) has the tables `datasets`, `experiments`, `runs`, `conditions`, `episodes`, `interventions`,
  `comparisons`, `reproductions`, `regression_suites`, `regression_cases`, `regression_results`, `quality_findings`,
  `incidents`, `incident_members`, `incident_history`, `ops_events`, `reviews`, `workflow_tasks`. Migrations are additive only.
- `artifacts/runs/<run_id>/episodes/*.json.gz` holds simulator episodes (full 50 Hz streams, full-precision actions).
- `artifacts/datasets/<dataset_id>/episodes/*.json.gz` holds normalised imported episodes.
- `artifacts/reports/` holds experiment reports and bundles, detector evaluation, incident exports and pilot reports.

Runs are immutable: a rerun creates a new run id. Conditions are content-addressed (`c_<sha256(params, seed)>`) and
never overwritten.

## Interfaces and extension points

- **New data source:** implement the `Adapter` protocol and call `register()`. Map only what the source states.
- **New quality rule:** decorate a function with `@rule(id, dimension, description, deterministic, applies=...)`.
  The `applies` precondition must check that the schema provides the inputs.
- **New policy:** implement `PolicyAdapter` (`reset`, `act(obs) → (action, done)`, `annotations`, `config_dict`) and add
  it to `make_policy`. A remote VLA would be wrapped here. None is included.
- **New task:** provide an env with `reset/step/metadata`, a verifier over `state.*` streams, and a taxonomy.
- **Diagnostic model:** register a provider in `diagnostics.PROVIDERS`. It is enabled only with two environment flags, and its output is stored as a hypothesis.

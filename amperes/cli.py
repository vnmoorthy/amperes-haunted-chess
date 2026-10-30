"""Command-line interface. Run `python -m amperes.cli --help`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_demo(a) -> int:
    from amperes.report import write_report
    from amperes.sim.experiment import run_experiment

    t0 = time.time()
    s = run_experiment(n_dev=a.n_dev, n_heldout=a.n_heldout, nominal_repeats=a.nominal_repeats,
                       max_repro=a.max_repro, max_guards=a.max_guards, workers=a.workers)
    out = write_report(s)
    print(f"\nexperiment {s['experiment_id']} completed in {time.time() - t0:.1f} s")
    print(f"report: {out / 'report.md'}\nbundle: {out / 'bundle.zip'}")
    for name, c in s["step6_comparison"].items():
        h = c["heldout"]
        print(f"  held-out {name}: {h['baseline']['successes']}/{h['n_pairs']} -> {h['treatment']['successes']}/{h['n_pairs']}"
              f" (fixed {h['fixed_by_treatment']}, broken {h['broken_by_treatment']}, McNemar p={h['mcnemar_exact_p']})")
    return 0


def cmd_report(a) -> int:
    from amperes import store
    from amperes.report import write_report

    rows = store.query("SELECT summary FROM experiments WHERE experiment_id=? AND status='completed'", [a.experiment_id])
    if not rows:
        print(f"error: no completed experiment {a.experiment_id!r}", file=sys.stderr)
        return 2
    print(write_report(json.loads(rows[0]["summary"])))
    return 0


def cmd_reproduce(a) -> int:
    from amperes import store
    from amperes.sim.reproduce import reproduce

    rows = store.query("SELECT artifact_path FROM episodes WHERE episode_id=?", [a.episode_id])
    if not rows or not rows[0]["artifact_path"]:
        print(f"error: unknown episode {a.episode_id!r} or no artifact", file=sys.stderr)
        return 2
    _print(reproduce(store.safe_artifact_path(rows[0]["artifact_path"])))
    return 0


def cmd_regress(a) -> int:
    from amperes import store
    from amperes.regression import run_suite
    from amperes.sim.policies import ScriptedConfig

    cfg = ScriptedConfig(**json.loads(Path(a.policy_config).read_text())) if a.policy_config else ScriptedConfig()
    if not store.query("SELECT 1 FROM regression_suites WHERE suite_id=?", [a.suite_id]):
        print(f"error: unknown suite {a.suite_id!r}", file=sys.stderr)
        return 2
    _print(run_suite(a.suite_id, cfg, a.label or cfg.label()))
    return 0


def cmd_serve(a) -> int:
    import uvicorn

    if a.host not in ("127.0.0.1", "localhost") and not a.allow_remote:
        print("refusing to bind a non-local host without --allow-remote (local-first default)", file=sys.stderr)
        return 2
    uvicorn.run("amperes.api:app", host=a.host, port=a.port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="amperes", description="Amperes Failure Lab (local-first).")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="run the full simulator experiment (steps 1-8) and write the report")
    d.add_argument("--n-dev", type=int, default=400)
    d.add_argument("--n-heldout", type=int, default=1000)
    d.add_argument("--nominal-repeats", type=int, default=5)
    d.add_argument("--max-repro", type=int, default=40)
    d.add_argument("--max-guards", type=int, default=60)
    d.add_argument("--workers", type=int, default=None)
    d.set_defaults(fn=cmd_demo)

    r = sub.add_parser("report", help="regenerate the report for a completed experiment")
    r.add_argument("experiment_id")
    r.set_defaults(fn=cmd_report)

    rp = sub.add_parser("reproduce", help="attempt to reproduce one recorded episode")
    rp.add_argument("episode_id")
    rp.set_defaults(fn=cmd_reproduce)

    rg = sub.add_parser("regress", help="rerun a regression suite against a policy config (JSON file)")
    rg.add_argument("suite_id")
    rg.add_argument("--policy-config", help="JSON file with ScriptedConfig fields; default = baseline")
    rg.add_argument("--label")
    rg.set_defaults(fn=cmd_regress)

    s = sub.add_parser("serve", help="start the local API + UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--allow-remote", action="store_true")
    s.set_defaults(fn=cmd_serve)

    from amperes import cli_data

    cli_data.register(sub)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, FileNotFoundError, PermissionError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

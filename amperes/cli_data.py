"""Data-workflow CLI commands: fixtures, validate, import, qc, incidents, ops, review, study, eval, reports, deletion."""

from __future__ import annotations

import json
import sys
from pathlib import Path


RULE_SETTINGS = {"disabled_rules", "required_metadata", "min_rows", "end_tolerance_s", "stream_end_spread_s", "gap_factor",
                 "plausible_unix_range", "saturation_fraction", "shift_min_episodes", "shift_min_sessions"}


def _json_file(path, allowed: set[str], what: str) -> dict | None:
    if not path:
        return None
    cfg = json.loads(Path(path).read_text())
    bad = set(cfg) - allowed
    if bad:
        raise ValueError(f"unknown {what} settings {sorted(bad)}; allowed: {sorted(allowed)}")
    return cfg


def _p(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


def c_fixtures(a):
    from amperes import fixtures, store

    out = Path(a.out)
    if a.kind == "defects":
        _p(fixtures.defect_corpus(out, store.artifacts_dir() / "fixture_work"))
    else:
        _p(fixtures.ops_package(out))
    return 0


def c_validate(a):
    from amperes.ingest.pipeline import validate_only

    r = validate_only(a.path, a.adapter)
    _p(r)
    return 0 if r["invalid_records"] == 0 and not r["event_errors"] and not r["records_with_dropped_rows"] else 1


def c_import(a):
    from amperes import incidents
    from amperes.ingest.pipeline import import_dataset

    r = import_dataset(a.path, name=a.name, adapter=a.adapter, synthetic=True if a.synthetic else None, note=a.note,
                       ctx=_json_file(a.rules_config, RULE_SETTINGS, "rule"))
    if not a.no_incidents:
        r["incidents"] = len(incidents.build_incidents(r["dataset_id"]))
    r.pop("rejected", None) if a.brief else None
    _p(r)
    return 0


def c_qc(a):
    """Re-run quality checks for a dataset from its stored canonical episodes (works for simulator runs too)."""
    from amperes import store
    from amperes.ingest.pipeline import analyze
    from amperes.provenance import new_id, now_iso
    from amperes.quality import quality_summary
    from amperes.schema import read_episode

    rows = store.query("SELECT episode_id, artifact_path FROM episodes WHERE dataset_id=?", [a.dataset_id])
    if not rows:
        return _err(f"no episodes for dataset {a.dataset_id}")
    eps = [read_episode(store.safe_artifact_path(r["artifact_path"])) for r in rows]
    findings, statuses = analyze(eps, _json_file(a.rules_config, RULE_SETTINGS, "rule"))
    store.execute("DELETE FROM quality_findings WHERE dataset_id=?", [a.dataset_id])
    store.insert_many("quality_findings", [f.as_row(a.dataset_id, new_id("qf"), now_iso()) for f in findings])
    for eid, st in statuses.items():
        store.execute("UPDATE episodes SET quality_status=? WHERE episode_id=?", [st, eid])
    _p(quality_summary(statuses, findings))
    return 0


def c_datasets(a):
    from amperes import store

    _p(store.query("SELECT dataset_id, name, kind, adapter, synthetic, n_records, n_rejected, created_at FROM datasets ORDER BY created_at DESC"))
    return 0


def c_delete(a):
    from amperes import store

    if not a.yes:
        return _err("deletion is permanent; re-run with --yes")
    if not store.query("SELECT 1 FROM datasets WHERE dataset_id=?", [a.dataset_id]):
        return _err(f"unknown dataset {a.dataset_id}")
    _p({"deleted": a.dataset_id, "removed": store.delete_dataset(a.dataset_id)})
    return 0


def c_incidents(a):
    from amperes import incidents, store

    if a.action == "build":
        _p([{k: i[k] for k in ("incident_id", "kind", "title", "status", "n_occurrences", "n_sessions")} for i in incidents.build_incidents(a.target)])
    elif a.action == "list":
        q = "SELECT incident_id, dataset_id, kind, title, severity, status, n_occurrences, n_episodes, n_sessions FROM incidents"
        _p(store.query(q + (" WHERE dataset_id=?" if a.target else "") + " ORDER BY n_occurrences DESC", [a.target] if a.target else []))
    elif a.action == "show":
        _p(incidents.get(a.target))
    elif a.action == "status":
        _p(incidents.transition(a.target, a.to, a.actor, a.note or "", owner=a.owner, resolution=a.resolution))
    elif a.action == "export":
        _p(incidents.export(a.target, store.artifacts_dir() / "reports" / "incidents"))
    elif a.action == "to-regression":
        _p(incidents.to_regression_case(a.target, a.actor))
    return 0


def _filters(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"filter must be key=value, got {it!r}")
        k, v = it.split("=", 1)
        if k not in ("workcell_id", "session_id", "device_id", "task_id", "policy_id", "policy_version", "config_id", "operator_id"):
            raise ValueError(f"unsupported filter key {k!r}")
        out[k] = v
    return out


def c_ops(a):
    from amperes import ops
    from amperes.pilot_report import ANALYSIS_DEFAULTS

    cfg = {**ANALYSIS_DEFAULTS, **(_json_file(a.analysis_config, set(ANALYSIS_DEFAULTS), "analysis") or {})}
    if a.action == "metrics":
        _p(ops.ops_metrics(a.dataset_ids, a.t0, a.t1, _filters(a.filter), usable_statuses=tuple(cfg["usable_statuses"])))
    elif a.action == "bottlenecks":
        _p(ops.bottlenecks(a.dataset_ids, a.t0, a.t1, cfg["rejected_tolerance"], cfg["min_session_episodes"]))
    else:
        _p(ops.correlate(a.dataset_ids, a.t0, a.t1, _filters(a.filter)))
    return 0


def c_review(a):
    from amperes import review

    if a.action == "add":
        _p(review.record_review(a.kind, a.target, a.decision, a.reviewer, a.notes or ""))
    else:
        _p(review.review_report(a.target))
    return 0


def c_study(a):
    from amperes import review

    if a.action == "plan":
        r = review.plan_study(a.target, [x.strip() for x in (a.reviewers or "").split(",") if x.strip()], a.sample_size)
        _p({k: r[k] for k in ("study_id", "conditions", "instructions")} | {"tasks": [{k: t[k] for k in ("task_id", "reviewer", "condition", "sample_id", "task_type")} for t in r["tasks"]]})
    elif a.action == "start":
        review.start_task(a.target)
        print("started")
    elif a.action == "finish":
        review.finish_task(a.target, a.confirmed, a.incorrect, a.missing_info or "", a.notes or "")
        print("finished")
    else:
        _p(review.study_report(a.target))
    return 0


def c_eval(a):
    from amperes import evaluation, fixtures, store

    corpus = Path(a.corpus) if a.corpus else store.artifacts_dir() / "fixtures" / "defect_corpus"
    if not (corpus / "labels.json").exists():
        fixtures.defect_corpus(corpus, store.artifacts_dir() / "fixture_work")
    ev = evaluation.evaluate_corpus(corpus)
    out = store.artifacts_dir() / "reports" / "detector_eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / "detector_eval.json").write_text(json.dumps(ev, indent=2, default=str))
    (out / "detector_eval.md").write_text(evaluation.markdown(ev))
    print(evaluation.markdown(ev))
    print(f"\nwritten: {out / 'detector_eval.md'}")
    return 0


def c_pilot_report(a):
    from amperes import pilot_report, store

    assumptions = json.loads(Path(a.assumptions).read_text()) if a.assumptions else None
    r = pilot_report.build(a.dataset_ids, assumptions, a.study, a.experiment, a.include_latest_sim,
                           _json_file(a.analysis_config, set(pilot_report.ANALYSIS_DEFAULTS), "analysis"))
    out = pilot_report.write(r, store.artifacts_dir() / "reports" / "pilot" / "_".join(a.dataset_ids)[:120])
    _p(out)
    return 0


def c_shadow(a):
    """Shadow mode: import read-only, triage, report. Never commands a robot; proves the source was not modified."""
    from amperes import incidents, pilot_report, store
    from amperes.ingest.pipeline import import_dataset, source_fingerprint

    before = source_fingerprint(Path(a.path).resolve())
    r = import_dataset(a.path, name=a.name or f"shadow:{Path(a.path).name}", adapter=a.adapter,
                       synthetic=True if a.synthetic else None, note="shadow-mode evaluation",
                       ctx=_json_file(a.rules_config, RULE_SETTINGS, "rule"))
    incs = incidents.build_incidents(r["dataset_id"])
    rep = pilot_report.build([r["dataset_id"]], analysis=_json_file(a.analysis_config, set(pilot_report.ANALYSIS_DEFAULTS), "analysis"))
    out = pilot_report.write(rep, store.artifacts_dir() / "reports" / "pilot" / r["dataset_id"])
    after = source_fingerprint(Path(a.path).resolve())
    _p({"dataset_id": r["dataset_id"], "episodes": r["episodes_imported"], "rejected": r["records_rejected"],
        "incidents": len(incs), "report": out, "source_files_checked": len(before), "source_unchanged": before == after,
        "robot_commands_issued": 0, "delete_with": f"python -m amperes.cli delete-dataset {r['dataset_id']} --yes"})
    return 0 if before == after else 1


def c_viam(a):
    """Read-only live capture from a Viam machine (credentials from .env), then import + QC + incidents."""
    import time as _t

    from amperes import store
    from amperes.integrations.viam_live import RecorderConfig, capture_and_import

    cfg = RecorderConfig(seconds=a.seconds, rate_hz=a.rate, arm=a.arm, gripper=None if a.no_gripper else a.gripper,
                         camera=None if a.no_camera else a.camera, camera_every_s=a.camera_every)
    out = store.artifacts_dir() / "viam_sessions" / _t.strftime("%Y%m%dT%H%M%S")
    print(f"recording {a.seconds:.0f} s from the Viam machine in .env (read-only; no commands are sent) -> {out}", file=sys.stderr)
    capture_and_import(cfg, out)
    return 0


def c_similar(a):
    from amperes.signatures import similar_local

    _p(similar_local(a.episode_id, a.k))
    return 0


def c_atlas(a):
    from amperes.integrations import atlas

    if a.action == "sync":
        if not a.target:
            return _err("atlas sync needs a dataset id")
        print("sending episode index rows, findings, incidents, events and signature vectors (no raw streams) to MongoDB Atlas",
              file=sys.stderr)
        _p(atlas.sync(a.target))
    elif a.action == "similar":
        _p(atlas.similar(a.target, a.k, a.workcell))
    else:
        _p(atlas.ping())
    return 0


def c_robot(a):
    """Supervised pick-and-place on the Viam arm. Default is a DRY RUN; motion needs --execute and the ack phrase."""
    import asyncio as _a
    import signal
    import threading
    import time as _t

    from amperes import store
    from amperes.integrations import viam_control as VC

    if a.action == "teach":
        if not a.name:
            return _err("robot teach needs a waypoint name: home, a or b")
        _p(_a.run(VC.teach(a.name)))
        return 0
    if a.action == "waypoints":
        _p(VC.load_waypoints())
        return 0
    if a.action == "chess":
        return c_chess(a)
    if a.action == "scan":
        from amperes.integrations import viam_tasks as VT

        rng = lambda s: tuple(float(v) for v in s.split(","))  # noqa: E731
        cfg = VT.ScanConfig(x_mm=rng(a.x), y_mm=rng(a.y), heights_mm=rng(a.heights), nx=a.nx, ny=a.ny, settle_s=a.settle,
                            sweeps=a.sweeps, video_hz=a.video_hz, max_minutes=a.max_minutes)
        out = store.artifacts_dir() / "robot_sessions" / ("scan_" + _t.strftime("%Y%m%dT%H%M%S"))
        print(VT.describe_box(cfg), file=sys.stderr)
        print(("EXECUTING motion" if a.execute else "DRY RUN (no motion)") + f" -> {out}", file=sys.stderr)
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: (print("\nSTOP requested: halting arm", file=sys.stderr), stop.set()))
        _p(VT.run_scan_and_import(VT.ScanSpec(config=cfg, out_dir=out, execute=a.execute, operator_ack=a.ack or ""), stop_event=stop))
        return 0
    base = VC.TrialConfig(name="baseline", approach_mm=a.approach, grasp_z_offset_mm=a.dz, settle_s=a.settle, trials=a.trials)
    configs = None
    if a.ab:
        fix = VC.TrialConfig(**{**base.__dict__, **json.loads(a.ab), "trials": a.trials})
        if fix.name == "baseline":
            fix.name = "fix"
        configs = [base, fix]
    spec = VC.RunSpec(config=base, execute=a.execute, operator_ack=a.ack or "", configs=configs, object_name=a.object)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: (print("\nSTOP requested: halting arm", file=sys.stderr), stop.set()))
    out = store.artifacts_dir() / "robot_sessions" / _t.strftime("%Y%m%dT%H%M%S")
    print(("EXECUTING motion" if a.execute else "DRY RUN (no motion)") + f" -> {out}", file=sys.stderr)
    _p(VC.run_and_import(spec, out, stop_event=stop))
    return 0


def c_chess(a):
    """robot chess teach a1|h1|graveyard · robot chess cal --king-height 95 · robot chess play [--execute --ack ...]"""
    import asyncio as _a
    import signal
    import threading
    import time as _t

    import chess as _chess

    from amperes import store
    from amperes.integrations import chess_robot as CR

    sub = a.name or "status"
    if sub == "teach":
        if not a.point:
            return _err("robot chess teach needs a point: a1, h1 or graveyard")
        _p(_a.run(CR.teach(a.point)))
        return 0
    if sub == "cal":
        d = CR.BoardCal.save_settings(king_height_mm=a.king_height, grip_fraction=a.grip)
        try:
            print(CR.BoardCal.load().describe(), file=sys.stderr)
        except ValueError as exc:
            print(exc, file=sys.stderr)
        _p(d)
        return 0
    if sub == "detect":
        from amperes.integrations import chess_vision as CV

        r = _a.run(CV.calibrate_from_camera(king_height_mm=a.king_height))
        _p({k: v for k, v in r.items() if k != "pieces_raw"})
        return 0
    if sub == "status":
        try:
            cal = CR.BoardCal.load()
            _p({"calibration": cal.describe(), "envelope": cal.envelope().__dict__, "look_pose": cal.look_pose()})
        except ValueError as exc:
            return _err(str(exc))
        return 0
    if sub != "play":
        return _err("robot chess: teach <point> | detect | cal | status | play")
    cal = CR.BoardCal.load()
    out = store.artifacts_dir() / "robot_sessions" / ("chess_" + _t.strftime("%Y%m%dT%H%M%S"))
    spec = CR.ChessSpec(out_dir=out, execute=a.execute, operator_ack=a.ack or "", robot_color=a.color, skill=a.skill, think_s=a.think,
                        settle_s=a.settle, fen=a.fen)
    engine = CR.Engine(skill=a.skill, think_s=a.think)
    game = CR.Game(engine, robot_color=_chess.WHITE if a.color == "white" else _chess.BLACK, fen=a.fen)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: (print("\nSTOP requested: halting arm", file=sys.stderr), stop.set()))
    print(cal.describe(), file=sys.stderr)
    print(("EXECUTING motion" if a.execute else "DRY RUN (no motion)") + f" -> {out}", file=sys.stderr)

    async def loop():
        sess = await CR.ChessSession(spec, cal, game, stop).connect()
        try:
            while not game.board.is_game_over() and not stop.is_set():
                if game.board.turn == game.robot_color:
                    await sess.play_robot_move()
                    print(game.board, file=sys.stderr)
                    if a.moves and len(sess.session["moves"]) >= a.moves:
                        break
                    continue
                print(f"\n{'White' if game.board.turn else 'Black'} to move. Your move (SAN like Nf3, or UCI like g1f3; 'q' to end): ",
                      end="", file=sys.stderr, flush=True)
                text = await _a.get_event_loop().run_in_executor(None, sys.stdin.readline)
                text = text.strip()
                if text in ("q", "quit", ""):
                    break
                try:
                    game.human_move(text)
                except ValueError as exc:
                    print(exc, file=sys.stderr)
        finally:
            await sess.close()
            engine.close()
        return sess.session

    session = _a.run(loop())
    _p(CR.import_chess_session(session, out))
    return 0


def c_schema(a):
    from amperes.schema import json_schema

    _p(json_schema())
    return 0


def register(sub) -> None:
    f = sub.add_parser("fixtures", help="generate SYNTHETIC fixtures (defect corpus or ops package)")
    f.add_argument("kind", choices=["defects", "ops"])
    f.add_argument("--out", required=True)
    f.set_defaults(fn=c_fixtures)

    v = sub.add_parser("validate", help="validate a dataset against the schema without importing")
    v.add_argument("path")
    v.add_argument("--adapter", default="auto")
    v.set_defaults(fn=c_validate)

    i = sub.add_parser("import", help="import a dataset (read-only on the source) and run quality checks")
    i.add_argument("path")
    i.add_argument("--name")
    i.add_argument("--adapter", default="auto")
    i.add_argument("--synthetic", action="store_true", help="mark the dataset as synthetic")
    i.add_argument("--note")
    i.add_argument("--no-incidents", action="store_true")
    i.add_argument("--brief", action="store_true")
    i.add_argument("--rules-config", help="JSON with customer-agreed rule settings (see INTEGRATION_GUIDE)")
    i.set_defaults(fn=c_import)

    q = sub.add_parser("qc", help="re-run quality checks on a stored dataset (including simulator runs)")
    q.add_argument("dataset_id")
    q.add_argument("--rules-config")
    q.set_defaults(fn=c_qc)

    d = sub.add_parser("datasets", help="list datasets")
    d.set_defaults(fn=c_datasets)

    dl = sub.add_parser("delete-dataset", help="permanently delete a dataset and all derived records/artifacts")
    dl.add_argument("dataset_id")
    dl.add_argument("--yes", action="store_true")
    dl.set_defaults(fn=c_delete)

    inc = sub.add_parser("incidents", help="incident triage")
    inc.add_argument("action", choices=["build", "list", "show", "status", "export", "to-regression"])
    inc.add_argument("target", nargs="?", help="dataset id (build/list) or incident id")
    inc.add_argument("--to", choices=["open", "investigating", "reproduced", "resolved", "closed", "unresolved"])
    inc.add_argument("--actor", default="cli-user")
    inc.add_argument("--note")
    inc.add_argument("--owner")
    inc.add_argument("--resolution")
    inc.set_defaults(fn=c_incidents)

    o = sub.add_parser("ops", help="operational metrics, bottlenecks, cross-session correlation")
    o.add_argument("action", choices=["metrics", "bottlenecks", "correlate"])
    o.add_argument("dataset_ids", nargs="+")
    o.add_argument("--t0", type=float)
    o.add_argument("--t1", type=float)
    o.add_argument("--filter", action="append", help="key=value (workcell_id, session_id, device_id, operator_id, task_id, policy_id, ...)")
    o.add_argument("--analysis-config", help="JSON: usable_statuses, rejected_tolerance, min_session_episodes")
    o.set_defaults(fn=c_ops)

    r = sub.add_parser("review", help="record a human review decision or show review statistics")
    r.add_argument("action", choices=["add", "report"])
    r.add_argument("target", help="finding/incident id (add) or dataset id (report)")
    r.add_argument("--kind", choices=["finding", "incident"], default="finding")
    r.add_argument("--decision", choices=["correct_actionable", "correct_not_actionable", "incorrect", "duplicate",
                                          "insufficient_evidence", "needs_investigation"])
    r.add_argument("--reviewer")
    r.add_argument("--notes")
    r.set_defaults(fn=c_review)

    s = sub.add_parser("study", help="counterbalanced workflow-timing study (existing vs assisted)")
    s.add_argument("action", choices=["plan", "start", "finish", "report"])
    s.add_argument("target", help="dataset id (plan), task id (start/finish), study id (report)")
    s.add_argument("--reviewers")
    s.add_argument("--sample-size", type=int, default=20)
    s.add_argument("--confirmed", type=int, default=0)
    s.add_argument("--incorrect", type=int, default=0)
    s.add_argument("--missing-info")
    s.add_argument("--notes")
    s.set_defaults(fn=c_study)

    e = sub.add_parser("eval-detectors", help="measure rule precision/recall on the labelled SYNTHETIC defect corpus")
    e.add_argument("--corpus")
    e.set_defaults(fn=c_eval)

    pr = sub.add_parser("pilot-report", help="write the pilot readiness report (JSON + Markdown)")
    pr.add_argument("dataset_ids", nargs="+")
    pr.add_argument("--assumptions", help="JSON file with cost assumptions (see docs/ECONOMIC_VALUE_MODEL.md)")
    pr.add_argument("--study")
    pr.add_argument("--experiment", help="include this simulator experiment's results")
    pr.add_argument("--include-latest-sim", action="store_true", help="include the latest simulator experiment")
    pr.add_argument("--analysis-config", help="JSON: usable_statuses, rejected_tolerance, min_session_episodes")
    pr.set_defaults(fn=c_pilot_report)

    sh = sub.add_parser("shadow", help="shadow-mode evaluation: read-only import, triage, report, source-unchanged check")
    sh.add_argument("path")
    sh.add_argument("--name")
    sh.add_argument("--adapter", default="auto")
    sh.add_argument("--synthetic", action="store_true")
    sh.add_argument("--rules-config")
    sh.add_argument("--analysis-config", help="JSON: usable_statuses, rejected_tolerance, min_session_episodes")
    sh.set_defaults(fn=c_shadow)

    vm = sub.add_parser("viam", help="READ-ONLY live telemetry capture from the Viam machine in .env, then audit it")
    vm.add_argument("action", choices=["record"])
    vm.add_argument("--seconds", type=float, default=60)
    vm.add_argument("--rate", type=float, default=4.0, help="target sample rate (Hz)")
    vm.add_argument("--arm", default="arm")
    vm.add_argument("--gripper", default="gripper")
    vm.add_argument("--camera", default="cam")
    vm.add_argument("--camera-every", type=float, default=2.0)
    vm.add_argument("--no-camera", action="store_true")
    vm.add_argument("--no-gripper", action="store_true")
    vm.set_defaults(fn=c_viam)

    si = sub.add_parser("similar", help="find episodes with the most similar recorded failure evidence (local)")
    si.add_argument("episode_id")
    si.add_argument("-k", type=int, default=10)
    si.set_defaults(fn=c_similar)

    at = sub.add_parser("atlas", help="OPT-IN MongoDB Atlas sync / vector search (needs MONGODB_URI in .env)")
    at.add_argument("action", choices=["ping", "sync", "similar"])
    at.add_argument("target", nargs="?", help="dataset id (sync) or episode id (similar)")
    at.add_argument("-k", type=int, default=10)
    at.add_argument("--workcell")
    at.set_defaults(fn=c_atlas)

    rb = sub.add_parser("robot", help="SUPERVISED pick-and-place on the Viam arm (dry run unless --execute + --ack)")
    rb.add_argument("action", choices=["teach", "waypoints", "run", "scan", "chess"])
    rb.add_argument("name", nargs="?", help="waypoint for teach: home, a, b; for chess: teach | cal | status | play")
    rb.add_argument("point", nargs="?", help="chess teach point: a1, h1, graveyard")
    rb.add_argument("--king-height", type=float, help="chess cal: height of the king in mm (sets all piece heights)")
    rb.add_argument("--grip", type=float, help="chess cal: grasp height as a fraction of piece height (default 0.45)")
    rb.add_argument("--color", default="black", choices=["white", "black"], help="chess play: the robot's colour")
    rb.add_argument("--skill", type=int, default=5, help="chess play: Stockfish skill level 0-20")
    rb.add_argument("--think", type=float, default=0.5, help="chess play: engine seconds per move")
    rb.add_argument("--moves", type=int, default=0, help="chess play: stop after this many robot moves (0 = play on)")
    rb.add_argument("--fen", help="chess play: start from this position")
    rb.add_argument("--trials", type=int, default=6, help="trials per config")
    rb.add_argument("--approach", type=float, default=80.0, help="approach height above each spot (mm)")
    rb.add_argument("--dz", type=float, default=0.0, help="grasp height offset vs taught pose (mm)")
    rb.add_argument("--settle", type=float, default=0.3)
    rb.add_argument("--ab", help='JSON overrides for the fix arm, e.g. \'{"name":"lower_grasp","grasp_z_offset_mm":-5}\'')
    rb.add_argument("--object", default="object", help="what is being picked, e.g. 'wooden block' (goes into the task instruction)")
    rb.add_argument("--sweeps", type=int, default=1, help="scan: full passes over the grid")
    rb.add_argument("--x", default="250,450", help="scan: x range in front of the base, mm")
    rb.add_argument("--y", default="-200,200", help="scan: y range, mm")
    rb.add_argument("--heights", default="320,420", help="scan: flange heights, mm (fingertips are 150 mm lower)")
    rb.add_argument("--nx", type=int, default=3)
    rb.add_argument("--ny", type=int, default=3)
    rb.add_argument("--video-hz", type=float, default=2.0, help="scan: continuous wrist-camera frames while pointing down (0 = off)")
    rb.add_argument("--max-minutes", type=float, default=25.0, help="scan: session time cap")
    rb.add_argument("--execute", action="store_true", help="actually move the arm (requires --ack)")
    rb.add_argument("--ack", help="operator acknowledgement: 'I am at <machine> with the e-stop in reach'")
    rb.set_defaults(fn=c_robot)

    sc = sub.add_parser("schema", help="print the canonical episode JSON schema")
    sc.set_defaults(fn=c_schema)

"""LeRobot v3 dataset → canonical `amperes.episode/1` JSONL, so third-party robot-learning data goes through the same
quality rules, incident grouping and reports as our own recordings.

Reads `meta/info.json`, `meta/tasks.parquet`, `meta/episodes/chunk-*/file-*.parquet` and `data/chunk-*/file-*.parquet`
(several episodes per file, split by `episode_index`). Videos are referenced, not decoded. Timestamps stay episode-relative
(`time_base = episode_relative_s`): inventing wall-clock times would be a lie. Outcome labels are `unknown` unless the
dataset carries them; teleoperated demonstrations usually do not.
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path
from typing import Any


def _tasks(root: Path) -> dict[int, str]:
    import pyarrow.parquet as pq

    p = root / "meta" / "tasks.parquet"
    if not p.exists():
        out = {}
        for line in (root / "meta" / "tasks.jsonl").read_text().splitlines():
            d = json.loads(line)
            out[int(d["task_index"])] = d["task"]
        return out
    rows = pq.read_table(p).to_pylist()
    out = {}
    for r in rows:
        text = r.get("task") or r.get("__index_level_0__") or next((v for k, v in r.items() if isinstance(v, str)), "")
        out[int(r["task_index"])] = str(text)
    return out


def _episode_meta(root: Path) -> dict[int, dict[str, Any]]:
    import pyarrow.parquet as pq

    out: dict[int, dict[str, Any]] = {}
    for f in sorted(glob.glob(str(root / "meta" / "episodes" / "chunk-*" / "*.parquet"))):
        for r in pq.read_table(f).to_pylist():
            keep = {k: v for k, v in r.items() if not k.startswith("stats/") or k.endswith("/count")}
            out[int(r["episode_index"])] = keep
    return out


def convert(root: Path, out_dir: Path, dataset_label: str | None = None) -> dict[str, Any]:
    import numpy as np
    import pyarrow.parquet as pq

    from amperes.schema import Episode, Event, Outcome, Ref, RobotInfo, SessionInfo, SourceInfo, Stream, episode_to_json

    root, out_dir = Path(root), Path(out_dir)
    info = json.loads((root / "meta" / "info.json").read_text())
    fps = float(info.get("fps") or 0) or None
    tasks = _tasks(root)
    emeta = _episode_meta(root)
    label = dataset_label or root.name
    robot = info.get("robot_type") or "unknown (LeRobot v3)"
    out_dir.mkdir(parents=True, exist_ok=True)
    feats = info.get("features", {})
    state_names = (feats.get("observation.state", {}).get("names") or [])
    n_eps, n_frames, notes = 0, 0, []
    with open(out_dir / "episodes.jsonl", "w") as fh:
        for f in sorted(glob.glob(str(root / "data" / "chunk-*" / "*.parquet"))):
            t = pq.read_table(f)
            cols = {c: t.column(c).to_pylist() for c in t.column_names}
            ep_idx = np.array(cols["episode_index"])
            for e in sorted(set(ep_idx.tolist())):
                sel = np.nonzero(ep_idx == e)[0]
                ts = [float(cols["timestamp"][i]) for i in sel]
                order = np.argsort(ts)
                sel = sel[order]
                ts = [ts[i] for i in order]
                state = [[float(x) for x in cols["observation.state"][i]] for i in sel]
                action = [[float(x) for x in cols["action"][i]] for i in sel]
                task_idx = int(cols["task_index"][sel[0]]) if "task_index" in cols else 0
                instruction = tasks.get(task_idx, f"task {task_idx}")
                meta = emeta.get(int(e), {})
                eid = f"{label}-ep{int(e):04d}"
                extra: dict[str, Any] = {"lerobot": {"episode_index": int(e), "task_index": task_idx, "length_declared": meta.get("length"),
                                                     "frames_in_data": len(sel), "data_file": os.path.relpath(f, root),
                                                     "videos": {k.split("/")[1]: {"chunk": meta.get(f"videos/{k.split('/')[1]}/chunk_index"),
                                                                                  "file": meta.get(f"videos/{k.split('/')[1]}/file_index")}
                                                                for k in meta if k.startswith("videos/") and k.endswith("/chunk_index")}}}
                events = []
                declared = meta.get("length")
                if declared is not None and int(declared) != len(sel):
                    events.append(Event(t=ts[-1] if ts else None, type="annotation", code="META_LENGTH_MISMATCH",
                                        message=f"meta/episodes says length={declared}, data has {len(sel)} frames", source="lerobot_v3 importer"))
                    notes.append(eid)
                same = sum(1 for s, a in zip(state, action) if s == a)
                if same == len(sel) and len(sel) > 1:
                    events.append(Event(t=ts[0], type="annotation", code="ACTION_EQUALS_STATE",
                                        message="action is identical to observation.state on every frame (leader = follower recording?)",
                                        source="lerobot_v3 importer"))
                streams = {
                    "obs.joint_pos": Stream(kind="observation", t=ts, values=state, units="rad (LeRobot observation.state)",
                                            expected_rate_hz=fps, source="lerobot:observation.state"),
                    "act.joint_target": Stream(kind="action", t=ts, values=action, units="rad (LeRobot action)",
                                               expected_rate_hz=fps, source="lerobot:action"),
                }
                ep = Episode(
                    episode_id=eid,
                    source=SourceInfo(dataset_id=label, adapter="amperes.lerobot_v3@1.0.0", source_uri=f"hf://datasets/{label}",
                                      source_record_id=f"episode_index={int(e)}", synthetic=False),
                    task=Ref(id=re.sub(r"[^a-z0-9]+", "_", instruction.lower()).strip("_")[:60] or f"task_{task_idx}", version="lerobot"),
                    policy=Ref(id="teleoperation", version="unknown"),
                    robot=RobotInfo(embodiment=f"{robot} (LeRobot v3 dataset)", device_id=robot, config_id=None),
                    session=SessionInfo(session_id=label, workcell_id="external", operator_id="unknown"),
                    start_time=ts[0] if ts else 0.0, end_time=ts[-1] if ts else 0.0, time_base="episode_relative_s", streams=streams,
                    outcome=Outcome(label="unknown", provenance="unknown", verified=False, reason="dataset carries no outcome label"),
                    events=events,
                    conditions={"instruction": instruction, "attempt_index": 1, "fps": fps, "state_names": state_names,
                                "cameras": [k for k in feats if k.startswith("observation.images.")]},
                    extra=extra,
                )
                fh.write(episode_to_json(ep) + "\n")
                n_eps += 1
                n_frames += len(sel)
    summary = {"dataset": label, "episodes": n_eps, "frames": n_frames, "fps": fps, "robot": robot, "tasks": len(tasks),
               "declared_episodes": info.get("total_episodes"), "declared_frames": info.get("total_frames"),
               "length_mismatch_episodes": notes, "out_dir": str(out_dir)}
    (out_dir / "convert_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def import_lerobot_v3(root: Path, label: str | None = None, name: str | None = None) -> dict[str, Any]:
    from amperes import incidents, store
    from amperes.ingest.pipeline import import_dataset

    root = Path(root)
    label = label or root.name
    out = store.artifacts_dir() / "external" / label
    summary = convert(root, out, label)
    r = import_dataset(out, name=name or f"EXTERNAL LeRobot v3: {label}", adapter="jsonl", synthetic=False,
                       note=f"third-party LeRobot v3 dataset ({summary['robot']}, {summary['episodes']} episodes, {summary['fps']} fps); outcomes unlabelled",
                       ctx={"required_metadata": ["task.id"]})
    summary.update(dataset_id=r["dataset_id"], quality=r["quality"]["status_counts"], incidents=len(incidents.build_incidents(r["dataset_id"])))
    return summary


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="import a LeRobot v3 dataset directory")
    ap.add_argument("root")
    ap.add_argument("--label")
    ap.add_argument("--name")
    a = ap.parse_args()
    print(json.dumps(import_lerobot_v3(Path(a.root), a.label, a.name), indent=2))

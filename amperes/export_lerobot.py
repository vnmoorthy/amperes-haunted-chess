"""Export store datasets as a LeRobot v2.1-style dataset directory that loads without Amperes code.

Layout: meta/{info.json,episodes.jsonl,tasks.jsonl,stats.json}, data/chunk-XXX/episode_XXXXXX.parquet,
images/<camera>/episode_XXXXXX/frame_XXXXXX.{jpg,png} (per-frame files, not videos), README.md.
Every episode is resampled onto a uniform grid at `fps`; observations take the nearest sample within one frame period
(else NaN), actions are forward-filled from the last command (NaN / 0 before the first one). Nothing is skipped silently.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import sys
import time
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from amperes import __version__, store
from amperes.labels import DEPTH_MAGIC, IMAGE_TYPES
from amperes.provenance import code_digest, now_iso
from amperes.schema import Episode, read_episode

CODEBASE_VERSION = "v2.1"
CHUNKS_SIZE = 1000
MAX_FRAMES = 1_000_000  # per episode; a bogus end_time must not turn into a multi-GB parquet file
DATA_PATH = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
IMAGE_DIR = "images/{camera}/episode_{episode_index:06d}"
POSE_NAMES = ["x", "y", "z", "o_x", "o_y", "o_z", "theta"]
# feature -> (source stream, dims, names, kind)
OBS = {"observation.state": ("obs.joint_pos", 6, [f"joint_{i}" for i in range(6)]),
       "observation.ee_pose": ("obs.ee_pose", 7, POSE_NAMES)}
ACT = {"action": ("act.target_pose", 7, POSE_NAMES, math.nan),
       "action.gripper": ("act.gripper", 1, ["grab"], 0.0)}
NUMERIC = {**{k: (d, n) for k, (_, d, n) in OBS.items()}, **{k: (d, n) for k, (_, d, n, _) in ACT.items()}}
SCALARS = {"timestamp": ("float32", pa.float32()), "frame_index": ("int64", pa.int64()), "episode_index": ("int64", pa.int64()),
           "index": ("int64", pa.int64()), "task_index": ("int64", pa.int64())}
LABELS = {"success": "task completed as judged by outcome.provenance", "failure": "task attempted, did not complete",
          "timeout": "task did not complete within its time budget", "invalid": "episode aborted (safety stop, infrastructure error)",
          "unknown": "no outcome recorded (e.g. dry run or read-only capture)"}


def decode_depth(raw: bytes) -> np.ndarray:
    """Viam raw depth: 8-byte magic (the camera writes DEPTHMAP; the Go constant is named VERSIONX), width/height as
    big-endian uint64, then width*height big-endian uint16 (mm)."""
    if len(raw) < 24 or raw[:8] not in DEPTH_MAGIC:
        raise ValueError("not a Viam raw depth file (DEPTHMAP/VERSIONX header missing)")
    w, h = struct.unpack(">QQ", raw[8:24])
    if len(raw) < 24 + 2 * w * h:
        raise ValueError(f"depth payload truncated: expected {w}x{h} uint16")
    return np.frombuffer(raw, dtype=">u2", count=w * h, offset=24).reshape(h, w).astype(np.uint16)


def encode_depth(raw: bytes, out: Path) -> tuple[int, int]:
    from PIL import Image

    arr = decode_depth(raw)
    Image.fromarray(arr).save(out, format="PNG")  # mode I;16 -> 16-bit grayscale PNG
    return arr.shape[0], arr.shape[1]


def _nearest(t: np.ndarray, grid: np.ndarray, tol: float) -> np.ndarray:
    """Index of the sample nearest to each grid time, -1 when none lies within `tol`."""
    if len(t) == 0:
        return np.full(len(grid), -1)
    j = np.searchsorted(t, grid)
    lo, hi = np.clip(j - 1, 0, len(t) - 1), np.clip(j, 0, len(t) - 1)
    pick = np.where(np.abs(t[hi] - grid) < np.abs(t[lo] - grid), hi, lo)
    return np.where(np.abs(t[pick] - grid) <= tol + 1e-9, pick, -1)


def _latest(t: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Index of the last sample at or before each grid time, -1 when there is none."""
    return np.searchsorted(t, grid + 1e-6, side="right") - 1 if len(t) else np.full(len(grid), -1)


def _sorted(stream) -> tuple[np.ndarray, np.ndarray]:
    t = np.asarray(stream.t, dtype=np.float64)
    v = np.array([[math.nan if x is None else x for x in row] for row in stream.values], dtype=np.float32)
    order = np.argsort(t, kind="stable")
    return t[order], v[order]


def _camera_of(name: str, source: str | None, src_eid: str, cameras: tuple[str, ...]) -> str | None:
    if source in cameras:
        return source
    for cam in sorted(cameras, key=len, reverse=True):
        if name.startswith(f"{src_eid}_{cam}_"):
            return cam
    return None


def _image_refs(ep: Episode, cameras: tuple[str, ...]) -> tuple[list[tuple[float, str, str, bool]], dict[str, int]]:
    """(t, camera, filename, is_depth) for every snapshot event and frame-index stream; ignored-camera counts."""
    src_eid = ep.extra.get("source_episode_id") or ep.episode_id
    refs, ignored = [], {}
    for ev in ep.events:
        if ev.code != "CAMERA_SNAPSHOT" or not ev.message or ev.t is None:
            continue
        cam = _camera_of(ev.message, ev.source, src_eid, cameras)
        if cam is None:
            ignored[ev.source or "?"] = ignored.get(ev.source or "?", 0) + 1
            continue
        refs.append((ev.t, cam, ev.message, ev.message.endswith(".dep")))
    for name, s in ep.streams.items():
        if not (name.startswith("obs.") and name.endswith(".frame")):
            continue
        cam = name[4:-6]
        if cam not in cameras:
            ignored[cam] = ignored.get(cam, 0) + len(s.t)
            continue
        for t, row in zip(s.t, s.values):
            if row and row[0] is not None and math.isfinite(row[0]):
                refs.append((t, cam, f"{src_eid}_{cam}_video_{int(row[0]):05d}.jpg", False))
    return refs, ignored


def _find(name: str, dirs: list[Path]) -> Path | None:
    """Only a plain image file name that lives directly inside one of `dirs` (episode files are data, not paths)."""
    if Path(name).name != name or name.startswith(".") or Path(name).suffix.lower() not in IMAGE_TYPES:
        raise ValueError("not a plain image file name")
    for d in dirs:
        p = (d / name).resolve()
        if p.parent == d.resolve() and p.is_file():
            return p
    return None


def _frames(ep: Episode, fps: float, cameras: tuple[str, ...], img_dirs: list[Path], out_dir: Path, episode_index: int) -> dict[str, Any]:
    """Resample one episode; returns columns, per-feature counts and image bookkeeping, or {'skip': reason}."""
    present = {k for k, s in ep.streams.items() if s.t}
    if not any(src in present for src, _, _ in OBS.values()):
        return {"skip": f"no observation stream among {sorted(src for src, _, _ in OBS.values())}; has {sorted(present)}"}
    for src, dims in [(s, d) for s, d, _ in OBS.values()] + [(s, d) for s, d, _, _ in ACT.values()]:
        if src not in present:
            continue
        s = ep.streams[src]
        if s.dims != dims:
            return {"skip": f"{src} has {s.dims} dims, expected {dims}"}
        if len(s.values) != len(s.t) or any(len(r) != dims for r in s.values):
            return {"skip": f"{src} is ragged ({len(s.t)} timestamps, {len(s.values)} rows, row lengths {sorted({len(r) for r in s.values})})"}
    ts = [t for k in present for t in (min(ep.streams[k].t), max(ep.streams[k].t))]
    relative = ep.time_base == "episode_relative_s"  # stream clocks start at the episode, start_time (if any) is wall clock
    t0 = ep.start_time if ep.start_time is not None and not relative else min(ts)
    t1 = ep.end_time if ep.end_time is not None and not relative else max(ts)
    if t1 < t0:
        return {"skip": f"end_time {t1} before start_time {t0}"}
    n = int(math.floor((t1 - t0) * fps + 1e-6)) + 1
    if n > MAX_FRAMES:
        return {"skip": f"{n} frames at {fps} fps exceeds {MAX_FRAMES} (start_time {t0}, end_time {t1}: check the episode's time bounds)"}
    grid = t0 + np.arange(n) / fps
    tol = 1.0 / fps
    cols: dict[str, Any] = {"timestamp": (np.arange(n) / fps).astype(np.float32), "frame_index": np.arange(n, dtype=np.int64)}
    nan, dropped, units, conv = {}, {}, {}, {}
    for feat, (src, dims, _) in OBS.items():
        if src not in present:
            cols[feat] = np.full((n, dims), np.nan, dtype=np.float32)
            nan[feat] = n
            continue
        t, v = _sorted(ep.streams[src])
        units[src] = ep.streams[src].units
        if feat == "observation.state" and (units[src] or "").strip().lower() in ("rad", "radians"):
            v, conv[feat] = np.degrees(v), "rad->deg"
        idx = _nearest(t, grid, tol)
        out = np.where((idx >= 0)[:, None], v[np.clip(idx, 0, None)], np.nan).astype(np.float32)
        cols[feat], nan[feat] = out, int(np.isnan(out).any(axis=1).sum())
        dropped[feat] = int(len(t) - len(np.unique(idx[idx >= 0])))
    for feat, (src, dims, _, fill) in ACT.items():
        if src not in present:
            cols[feat] = np.full((n, dims), fill, dtype=np.float32)
            nan[feat] = n
            continue
        t, v = _sorted(ep.streams[src])
        units[src] = ep.streams[src].units
        idx = _latest(t, grid)
        cols[feat] = np.where((idx >= 0)[:, None], v[np.clip(idx, 0, None)], fill).astype(np.float32)
        nan[feat] = int((idx < 0).sum())  # frames before the first command
    refs, ignored = _image_refs(ep, cameras)
    images: dict[str, list[str]] = {}
    missing, copied, shapes = {}, {}, {}  # missing: distinct source files that could not be used (ordered)
    for cam in cameras:
        for depth in (False, True):
            sel = [r for r in refs if r[1] == cam and r[3] == depth]
            if not sel:
                continue
            feat = f"observation.images.{cam}{'_depth' if depth else ''}"
            sel.sort(key=lambda r: r[0])
            idx = _nearest(np.array([r[0] for r in sel]), grid, tol)
            paths = [""] * n
            dest_dir = out_dir / IMAGE_DIR.format(camera=cam, episode_index=episode_index)
            for k in np.flatnonzero(idx >= 0):
                name = sel[idx[k]][2]
                try:
                    src_path = _find(name, img_dirs)
                except ValueError as exc:
                    missing[name] = f"rejected: {exc}"
                    continue
                if src_path is None:
                    missing[name] = "not found"
                    continue
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / (f"frame_{k:06d}_depth.png" if depth else f"frame_{k:06d}{src_path.suffix.lower().replace('.jpeg', '.jpg')}")
                try:
                    if depth:
                        h, w = encode_depth(src_path.read_bytes(), dest)
                        shapes.setdefault(feat, [h, w, 1])
                    else:
                        shutil.copyfile(src_path, dest)
                        if feat not in shapes:
                            from PIL import Image

                            with Image.open(dest) as im:
                                shapes[feat] = [im.height, im.width, len(im.getbands())]
                except Exception as exc:
                    missing[name] = f"{type(exc).__name__}: {str(exc)[:80]}"
                    dest.unlink(missing_ok=True)  # never leave a file that is not a decodable frame in the export
                    continue
                paths[k] = str(dest.relative_to(out_dir))
                copied[feat] = copied.get(feat, 0) + 1
            images[feat] = paths
    return {"n": n, "cols": cols, "images": images, "shapes": shapes, "copied": copied, "ignored": ignored,
            "missing": [f"{k} ({v})" for k, v in missing.items()],
            "nan": nan, "dropped": dropped, "units": units, "converted": conv, "t0": t0, "t1": t1,
            "streams_present": sorted(present)}


def _task_text(ep: Episode) -> str:
    instr = ep.conditions.get("instruction")
    return str(instr) if instr else (ep.task.id if ep.task and ep.task.id else "unknown")


def _failure_category(ep: Episode, row: dict[str, Any]) -> str | None:
    if row.get("category"):
        return row["category"]
    o = ep.outcome
    return o.reason if o and o.label in ("failure", "timeout", "invalid") and o.reason else None


def _human_labels(dataset_id: str) -> dict[str, dict[str, Any]]:
    """Latest human label per episode (append-only history, latest wins) plus how many labels it supersedes."""
    out: dict[str, dict[str, Any]] = {}
    for r in store.query("SELECT episode_id, outcome, category, reviewer, created_at, agrees_with_verifier FROM human_labels "
                         "WHERE dataset_id=? ORDER BY created_at, rowid", [dataset_id]):
        n = out.get(r["episode_id"], {}).get("n_labels", 0)
        out[r["episode_id"]] = {**r, "agrees_with_verifier": None if r["agrees_with_verifier"] is None else bool(r["agrees_with_verifier"]), "n_labels": n + 1}
        del out[r["episode_id"]]["episode_id"]
    return out


def _image_dirs(ds: dict[str, Any], ep: Episode, artifact: Path) -> list[Path]:
    dirs = []
    for uri in (ds.get("source_uri"), ep.source.source_uri):
        if uri and "://" not in uri:
            p = Path(uri)
            dirs.append((p if p.is_dir() else p.parent) / "images")
    dirs.append(artifact.parent.parent / "images")
    return [d for d in dirs if d.is_dir()]


def _stats(arrays: dict[str, list[np.ndarray]]) -> dict[str, Any]:
    """Per feature and dimension over all frames, ignoring NaN (LeRobot stats.json shape, plus finite_count)."""
    out = {}
    for feat, parts in arrays.items():
        a = np.concatenate(parts).astype(np.float64)
        a = a.reshape(len(a), -1)
        a = np.where(np.isfinite(a), a, np.nan)
        cnt = np.isfinite(a).sum(axis=0)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN columns are reported as null, not as a warning
            out[feat] = {"min": _l(np.nanmin(a, axis=0)), "max": _l(np.nanmax(a, axis=0)), "mean": _l(np.nanmean(a, axis=0)),
                         "std": _l(np.nanstd(a, axis=0)), "count": [int(len(a))], "finite_count": [int(c) for c in cnt]}
    return out


def _l(a: np.ndarray) -> list[float | None]:
    return [None if not np.isfinite(x) else float(x) for x in a]


def export_lerobot(dataset_ids: list[str], out_dir: Path, fps: float = 3.0, cameras: tuple[str, ...] = ("cam", "cam2"),
                   overwrite: bool = False) -> dict[str, Any]:
    """Export the given store datasets into `out_dir` as a LeRobot v2.1-style dataset. Returns a summary dict."""
    t_start = time.perf_counter()
    out_dir = Path(out_dir)
    if fps <= 0:
        raise ValueError("fps must be positive")
    ids = list(dict.fromkeys(dataset_ids))
    if not ids:
        raise ValueError("no dataset ids given")
    ds_rows = {d["dataset_id"]: d for d in store.query(
        "SELECT dataset_id, name, adapter, source_uri, synthetic, note, created_at FROM datasets WHERE dataset_id IN (%s)" % ",".join("?" * len(ids)), ids)}
    unknown = [i for i in ids if i not in ds_rows]
    if unknown:
        raise ValueError(f"unknown dataset id(s): {unknown}")
    if out_dir.exists() and any(out_dir.iterdir()):  # only after the inputs are known to be valid: never wipe and then fail
        if not overwrite:
            raise FileExistsError(f"{out_dir} is not empty (pass overwrite=True to replace it)")
        if not (out_dir / "meta" / "info.json").is_file():
            raise FileExistsError(f"{out_dir} is not empty and is not a previous export (no meta/info.json); refusing to delete it")
        shutil.rmtree(out_dir)
    for d in (out_dir / "meta", out_dir / "data"):
        d.mkdir(parents=True, exist_ok=True)
    tasks: dict[str, int] = {}
    episodes, skipped, arrays, cam_feats, shapes = [], [], {k: [] for k in NUMERIC}, {}, {}
    missing_files, ignored, nan_total, dropped_total, converted = [], {}, {k: 0 for k in NUMERIC}, {k: 0 for k in OBS}, 0
    robot_types: dict[str, int] = {}
    ei = 0
    for ds_id in ids:
        rows = store.query("SELECT * FROM episodes WHERE dataset_id=? ORDER BY start_time, episode_id", [ds_id])
        human = _human_labels(ds_id)
        for row in rows:
            try:
                art = store.safe_artifact_path(row["artifact_path"])
                ep = read_episode(art)
            except Exception as exc:
                skipped.append({"episode_id": row["episode_id"], "dataset_id": ds_id, "reason": f"cannot read artifact: {type(exc).__name__}: {str(exc)[:120]}"})
                continue
            try:
                fr = _frames(ep, fps, cameras, _image_dirs(ds_rows[ds_id], ep, art), out_dir, ei)
            except Exception as exc:
                fr = {"skip": f"{type(exc).__name__}: {str(exc)[:160]}"}
            if "skip" in fr:
                skipped.append({"episode_id": ep.episode_id, "dataset_id": ds_id, "reason": fr["skip"]})
                continue
            task = _task_text(ep)
            tasks.setdefault(task, len(tasks))
            emb = (ep.robot.embodiment if ep.robot and ep.robot.embodiment else "unknown")
            robot_types[emb] = robot_types.get(emb, 0) + 1
            dry = bool(ep.conditions.get("dry_run")) or bool(ep.source.synthetic_note and ep.source.synthetic_note.startswith("DRY RUN"))
            for feat in fr["images"]:
                cam_feats[feat] = cam_feats.get(feat, 0) + fr["copied"].get(feat, 0)
            shapes.update({k: v for k, v in fr["shapes"].items() if k not in shapes})
            for k in NUMERIC:
                arrays[k].append(fr["cols"][k])
                nan_total[k] += fr["nan"][k]
            for k, v in fr["dropped"].items():
                dropped_total[k] += v
            converted += bool(fr["converted"])
            missing_files += [f"{ep.episode_id}: {m}" for m in fr["missing"]]
            for k, v in fr["ignored"].items():
                ignored[k] = ignored.get(k, 0) + v
            episodes.append({"episode_index": ei, "episode_id": ep.episode_id, "source_episode_id": ep.extra.get("source_episode_id"),
                             "dataset_id": ds_id, "tasks": [task], "task_index": tasks[task], "length": fr["n"],
                             "outcome": ep.outcome.label if ep.outcome else None,
                             "outcome_provenance": ep.outcome.provenance if ep.outcome else None,
                             "failure_category": _failure_category(ep, row), "verified": bool(row.get("verified")),
                             "quality_status": row.get("quality_status"), "human_label": human.get(ep.episode_id),
                             "conditions": ep.conditions, "dry_run": dry,
                             "synthetic": bool(ep.source.synthetic), "synthetic_note": ep.source.synthetic_note,
                             "robot_type": emb, "start_time": fr["t0"], "end_time": fr["t1"], "time_base": ep.time_base,
                             "streams_present": fr["streams_present"], "source_units": fr["units"], "units_converted": fr["converted"],
                             "nan_frames": fr["nan"], "image_frames": fr["copied"], "images_missing": len(fr["missing"]),
                             "_cols": fr["cols"], "_images": fr["images"]})
            ei += 1
    cam_feats = {k: v for k, v in cam_feats.items() if v}  # a camera whose every referenced file was missing has no frames
    image_feats = list(cam_feats)
    schema = pa.schema([(k, pa.list_(pa.float32())) for k in NUMERIC] + [(k, t) for k, (_, t) in SCALARS.items()]
                       + [(k, pa.string()) for k in image_feats])
    index = 0
    for e in episodes:
        cols, imgs, n = e.pop("_cols"), e.pop("_images"), e["length"]
        data = {k: pa.array(cols[k].tolist(), type=pa.list_(pa.float32())) for k in NUMERIC}
        data.update({"timestamp": pa.array(cols["timestamp"], type=pa.float32()), "frame_index": pa.array(cols["frame_index"]),
                     "episode_index": pa.array(np.full(n, e["episode_index"], dtype=np.int64)),
                     "index": pa.array(np.arange(index, index + n, dtype=np.int64)),
                     "task_index": pa.array(np.full(n, e["task_index"], dtype=np.int64))})
        data.update({k: pa.array(imgs.get(k, [""] * n), type=pa.string()) for k in image_feats})
        p = out_dir / DATA_PATH.format(episode_chunk=e["episode_index"] // CHUNKS_SIZE, episode_index=e["episode_index"])
        p.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(data, schema=schema), p)
        e["index_start"], index = index, index + n
    total_frames = index
    features: dict[str, Any] = {k: {"dtype": "float32", "shape": [d], "names": n} for k, (d, n) in NUMERIC.items()}
    features.update({k: {"dtype": name, "shape": [1], "names": None} for k, (name, _) in SCALARS.items()})
    for k in image_feats:
        features[k] = {"dtype": "image", "shape": shapes.get(k), "names": ["height", "width", "channels"],
                       "info": {"storage": "relative path to a per-frame image file (empty string = no frame within 1/fps)",
                                "format": "png16 (uint16 depth in mm)" if k.endswith("_depth") else "jpg/png"}}
    robot_type = max(robot_types, key=robot_types.get) if robot_types else "unknown"
    info = {"codebase_version": CODEBASE_VERSION, "robot_type": robot_type, "total_episodes": len(episodes), "total_frames": total_frames,
            "total_tasks": len(tasks), "total_videos": 0, "total_chunks": max(1, math.ceil(len(episodes) / CHUNKS_SIZE)),
            "chunks_size": CHUNKS_SIZE, "fps": fps, "splits": {"train": f"0:{len(episodes)}"}, "data_path": DATA_PATH,
            "video_path": None, "features": features,
            "amperes": {"note": "observation.images.* columns are relative paths to per-frame image files; there are no videos",
                        "exporter": f"amperes.export_lerobot@{__version__}", "code_digest": code_digest(), "exported_at": now_iso(),
                        "source_schema": "amperes.episode/1",
                        "datasets": [{**{k: ds_rows[i][k] for k in ("dataset_id", "name", "adapter")}, "synthetic": bool(ds_rows[i]["synthetic"])} for i in ids],
                        "robot_types": robot_types, "cameras_requested": list(cameras),
                        "resampling": {"grid": "start_time + k/fps", "observation": "nearest sample within 1/fps else NaN",
                                       "action": "last command at or before t; NaN (action) / 0 (action.gripper) before the first command",
                                       "images": "nearest frame within 1/fps else empty string"},
                        "units": {"observation.state": "deg (rad sources converted)", "observation.ee_pose": "mm + orientation vector + deg",
                                  "action": "mm + orientation vector + deg", "action.gripper": "1=grab, 0=open"}}}
    (out_dir / "meta" / "info.json").write_text(json.dumps(info, indent=1))
    with open(out_dir / "meta" / "episodes.jsonl", "w") as fh:
        for e in episodes:
            fh.write(json.dumps(e, default=str) + "\n")
    with open(out_dir / "meta" / "tasks.jsonl", "w") as fh:
        for task, i in tasks.items():
            fh.write(json.dumps({"task_index": i, "task": task}) + "\n")
    stats = _stats({k: v for k, v in arrays.items() if v})
    if total_frames:
        stats["timestamp"] = _stats({"timestamp": [np.concatenate([np.arange(e["length"]) / fps for e in episodes])]})["timestamp"]
    (out_dir / "meta" / "stats.json").write_text(json.dumps(stats, indent=1))
    summary = {"out_dir": str(out_dir), "fps": fps, "cameras": list(cameras), "codebase_version": CODEBASE_VERSION,
               "datasets": [{"dataset_id": i, "name": ds_rows[i]["name"], "synthetic": bool(ds_rows[i]["synthetic"])} for i in ids],
               "total_episodes": len(episodes), "total_frames": total_frames, "total_tasks": len(tasks),
               "synthetic_episodes": sum(e["synthetic"] for e in episodes), "dry_run_episodes": sum(e["dry_run"] for e in episodes),
               "human_labelled_episodes": sum(e["human_label"] is not None for e in episodes),
               "robot_type": robot_type, "skipped": skipped, "nan_frames": nan_total, "source_samples_dropped": dropped_total,
               "state_rad_to_deg_episodes": converted, "image_frames": cam_feats, "images_missing": len(missing_files),
               "images_missing_examples": missing_files[:20], "images_ignored_cameras": ignored,
               "elapsed_s": round(time.perf_counter() - t_start, 3)}
    (out_dir / "README.md").write_text(_readme(info, summary, episodes))
    (out_dir / "meta" / "export_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary


def _readme(info: dict[str, Any], summary: dict[str, Any], episodes: list[dict[str, Any]]) -> str:
    a = info["amperes"]
    synth = [d for d in summary["datasets"] if d["synthetic"]]
    ds_lines = "\n".join(f"- `{d['dataset_id']}` — {d['name']}{' — **SYNTHETIC**' if d['synthetic'] else ''}" for d in summary["datasets"])
    feat_lines = "\n".join(f"| `{k}` | {f['dtype']} | {f['shape']} | {', '.join(f['names']) if f.get('names') else ''} |" for k, f in info["features"].items())
    skip_lines = "\n".join(f"- `{s['episode_id']}` ({s['dataset_id']}): {s['reason']}" for s in summary["skipped"][:50]) or "- none"
    warn = ""
    if synth:
        warn = (f"\n> **SYNTHETIC DATA WARNING:** {len(synth)} of {len(summary['datasets'])} source dataset(s) are simulator output or "
                f"generated fixtures ({summary['synthetic_episodes']} of {summary['total_episodes']} episodes). Each episode carries "
                f"`synthetic: true/false` in `meta/episodes.jsonl`; filter on it before training on physical-robot data.\n")
    if summary["dry_run_episodes"]:
        warn += (f"\n> **DRY RUN:** {summary['dry_run_episodes']} episode(s) were planned but no motion was executed "
                 f"(`dry_run: true` in `meta/episodes.jsonl`); their actions are plans, not executed commands.\n")
    label_lines = "\n".join(f"- `{k}`: {v}" for k, v in LABELS.items())
    return f"""# LeRobot v2.1-style export from Amperes
{warn}
Exported {a['exported_at']} by `{a['exporter']}` (code digest `{a['code_digest']}`), fps={info['fps']},
{info['total_episodes']} episodes / {info['total_frames']} frames / {info['total_tasks']} task(s), robot_type `{info['robot_type']}`.

## Source datasets
{ds_lines}

## Layout
```
meta/info.json          codebase_version, fps, features, totals, provenance (this file's `amperes` block)
meta/episodes.jsonl     one line per episode: episode_index, episode_id, tasks, length, outcome, failure_category, verified,
                        quality_status, human_label, conditions, dataset_id, dry_run, synthetic, streams_present, nan_frames, image_frames
meta/tasks.jsonl        task_index -> natural-language instruction (conditions.instruction, else task.id)
meta/stats.json         per numeric feature: min/max/mean/std/count over all frames (NaN ignored; finite_count per dim)
meta/export_summary.json  the exporter's summary (skipped episodes with reasons, NaN counts, missing images)
data/chunk-000/episode_000000.parquet   one parquet file per episode ({info['chunks_size']} episodes per chunk)
images/<camera>/episode_000000/frame_000000.jpg          per-frame image files (NOT videos)
images/<camera>/episode_000000/frame_000000_depth.png    16-bit PNG, uint16 depth in mm (decoded from Viam raw depth)
```

## Features
| feature | dtype | shape | names |
|---|---|---|---|
{feat_lines}

`observation.state` is joint position in degrees (sources recorded in radians were converted; see `units_converted` per episode).
`observation.ee_pose` / `action` are end-effector poses: x, y, z in mm, Viam orientation vector (o_x, o_y, o_z) and theta in degrees.
`action` is the last *commanded* target pose at or before the frame time (forward-filled; NaN before the first command).
`action.gripper` is 1 after a grab command, 0 after an open command or before any command.
`observation.images.*` columns hold relative file paths; an empty string means no frame within 1/fps of that time.

## Resampling
Each episode is placed on a uniform grid `t_k = start_time + k/fps` from `start_time` to `end_time`. Observations take the
nearest source sample within 1/fps (otherwise NaN for the whole row); actions are forward-filled; images take the nearest
frame within 1/fps. Per-episode NaN counts are in `meta/episodes.jsonl` (`nan_frames`; for `action` / `action.gripper` this is the
number of frames before the first command, filled with NaN / 0); dataset totals are in `meta/export_summary.json`.
Source samples that fell between grid points are not in this export ({summary['source_samples_dropped']} dropped per feature).

## Outcome / label semantics
`outcome` is the episode label as recorded:
{label_lines}

`outcome_provenance` says who asserted it (`source_label`, `operator`, `amperes_verifier`, `unknown`); `verified` is true only
when an independent verifier checked the label against measured state (for the real arm: gripper grab/holding signals).
`failure_category` is the diagnosis category when Amperes produced one, else the recorded failure reason (e.g. GRASP_FAILURE,
SLIPPAGE, TIMEOUT, SAFETY_STOP, INFRASTRUCTURE_ERROR). `quality_status` is the Amperes data-quality verdict
(`verified_success`, `valid_success_unverified`, `valid_failed_task`, `suspicious`, `invalid_data`, `infrastructure_error`);
only the first three are recommended for training. Failed tasks with valid data are intentionally included.
`human_label` is the latest human review of the episode (`outcome`, `category`, `reviewer`, `created_at`, `agrees_with_verifier`,
`n_labels` = how many labels it supersedes) or null when nobody has reviewed it; it never replaces `outcome`
({summary['human_labelled_episodes']} of {summary['total_episodes']} episodes have one).
Image files are copied only when the episode names a plain file inside its own session `images/` directory; anything else is
listed under `images_missing_examples` in `meta/export_summary.json` as rejected or not found.

## Skipped episodes ({len(summary['skipped'])})
{skip_lines}

## Loading without Amperes
```python
import json, pyarrow.parquet as pq
info = json.load(open("meta/info.json"))
table = pq.read_table("data/chunk-000/episode_000000.parquet")   # or pq.read_table("data") for everything
df = table.to_pandas()
```
Image columns are paths relative to this directory. Depth PNGs load as uint16 arrays (`numpy.array(PIL.Image.open(p))`).
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m amperes.export_lerobot", description="Export store datasets as a LeRobot v2.1-style directory")
    ap.add_argument("out_dir")
    ap.add_argument("dataset_ids", nargs="+")
    ap.add_argument("--fps", type=float, default=3.0)
    ap.add_argument("--cameras", default="cam,cam2")
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    try:
        s = export_lerobot(a.dataset_ids, Path(a.out_dir), a.fps, tuple(c for c in a.cameras.split(",") if c), a.overwrite)
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(s, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

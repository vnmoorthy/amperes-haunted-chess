"""Experiment report: JSON summary, Markdown report, plots, reproducibility manifest and a zip bundle."""

from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from amperes import store  # noqa: E402
from amperes.schema import read_episode  # noqa: E402
from amperes.sim.env import C_OBJECT_OBSTACLE, C_ROBOT_OBSTACLE  # noqa: E402
from amperes.sim.verifier import arr  # noqa: E402

# Amperes AI brand (amperesai.com): monochrome with a light and a dark theme; series told apart by lightness first.
# Every chart is written twice: `<name>.png` (light, used by report.md) and `<name>_dark.png` (dark UI theme).
THEMES = {
    "light": {"ink": "#0c0c0d", "muted": "#5f6167", "line": "#d3d5d9", "panel": "#ffffff", "legend": "#f2f2f4",
              "bad": "#b42318", "warn": "#8a5a00", "ok": "#1a7f4b", "line1": "#5f6167", "line2": "#0c0c0d", "point": "#0c0c0d",
              "arms": {"baseline": "#9a9ca1", "selected": "#0c0c0d", "alternative": "#5f6167", "control": "#d3d5d9"},
              "bar_text": {"baseline": "#0c0c0d", "selected": "#f5f5f6", "alternative": "#f5f5f6", "control": "#0c0c0d"}},
    "dark": {"ink": "#e6e7ea", "muted": "#7d7f84", "line": "#2a2b2f", "panel": "#0a0a0b", "legend": "#111214",
             "bad": "#ef8a80", "warn": "#d9b26a", "ok": "#86c9a0", "line1": "#9aa0a8", "line2": "#f5f5f6", "point": "#f5f5f6",
             "arms": {"baseline": "#84868b", "selected": "#f5f5f6", "alternative": "#cdced1", "control": "#4a4c51"},
             "bar_text": {"baseline": "#000000", "selected": "#000000", "alternative": "#000000", "control": "#e6e7ea"}},
}
SUFFIX = {"light": "", "dark": "_dark"}
ARM_ORDER = ("baseline", "selected", "alternative", "control")


def _rc(t: dict[str, Any]) -> dict[str, Any]:
    return {"figure.facecolor": t["panel"], "axes.facecolor": t["panel"], "savefig.facecolor": t["panel"], "text.color": t["ink"],
            "axes.labelcolor": t["ink"], "axes.titlecolor": t["ink"], "xtick.color": t["muted"], "ytick.color": t["muted"],
            "axes.edgecolor": t["line"], "legend.facecolor": t["legend"], "legend.edgecolor": t["line"], "legend.labelcolor": t["ink"],
            "font.family": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"], "axes.spines.top": False, "axes.spines.right": False}


def _arm_kind(name: str) -> str:
    return name.split(":")[0] if name.split(":")[0] in ARM_ORDER else "control"


def _ordered(arms) -> list[str]:
    """Stored summaries come back with sorted keys; plot arms in their canonical order with fixed colours."""
    return sorted(arms, key=lambda a: ARM_ORDER.index(_arm_kind(a)))


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _ci(c):
    return "n/a" if not c or c[0] is None else f"[{100 * c[0]:.1f}, {100 * c[1]:.1f}]"


def report_dir(exp_id: str) -> Path:
    return store.artifacts_dir() / "reports" / exp_id


def _plots(s: dict[str, Any], out: Path) -> list[str]:
    """Write every chart in both themes; returns all file names (light and dark)."""
    files = []
    for theme, t in THEMES.items():
        with plt.rc_context(_rc(t)):
            files += [f for f in _plots_one(s, out, t, SUFFIX[theme])]
    return files


def _plots_one(s: dict[str, Any], out: Path, t: dict[str, Any], sfx: str) -> list[str]:
    files = []
    arms = _ordered(s["step6_heldout_summaries"])
    sums = s["step6_heldout_summaries"]
    fig, ax = plt.subplots(figsize=(7, 3.6))
    rates = [sums[a]["success_rate"] for a in arms]
    lo = [r - sums[a]["success_rate_ci95"][0] for r, a in zip(rates, arms)]
    hi = [sums[a]["success_rate_ci95"][1] - r for r, a in zip(rates, arms)]
    ax.bar(range(len(arms)), rates, yerr=[lo, hi], capsize=4, color=[t["arms"][_arm_kind(a)] for a in arms], ecolor=t["muted"],
           edgecolor=t["line"], linewidth=0.6)
    ax.set_xticks(range(len(arms)), [a.split(":")[0] + "\n" + (a.split(":")[1] if ":" in a else "") for a in arms], fontsize=8)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("verified success rate")
    ax.set_title(f"Held-out perturbed conditions (n={sums[arms[0]]['n']} each, Wilson 95% CI)", fontsize=10)
    for i, a in enumerate(arms):
        ax.text(i, 0.03, f"{sums[a]['successes']}/{sums[a]['n']}", ha="center", fontsize=8, color=t["bar_text"][_arm_kind(a)])
    fig.tight_layout()
    fig.savefig(out / f"heldout_success{sfx}.png", dpi=130)
    plt.close(fig)
    files.append(f"heldout_success{sfx}.png")

    cats = sorted({c for a in arms for c in sums[a]["failure_categories"]})
    if cats:
        fig, ax = plt.subplots(figsize=(7, 3.4))
        w = 0.8 / len(arms)
        for i, a in enumerate(arms):
            ax.bar(np.arange(len(cats)) + i * w, [sums[a]["failure_categories"].get(c, 0) for c in cats], w, label=a.split(":")[0],
                   color=t["arms"][_arm_kind(a)], edgecolor=t["line"], linewidth=0.6)
        ax.set_xticks(np.arange(len(cats)) + w * (len(arms) - 1) / 2, cats, fontsize=7, rotation=15)
        ax.set_ylabel("episodes")
        ax.set_title("Held-out failure categories by arm (deterministic taxonomy)", fontsize=10)
        ax.legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(out / f"heldout_failure_categories{sfx}.png", dpi=130)
        plt.close(fig)
        files.append(f"heldout_failure_categories{sfx}.png")

    for k, sw in (s.get("step4_variations") or {}).get("sweeps", {}).items():
        pts = sw["points"]
        fig, ax = plt.subplots(figsize=(6, 2.2))
        labels = [p["category"] or p["outcome"] for p in pts]
        uniq = sorted(set(labels))
        ax.scatter([p["value"] for p in pts], [uniq.index(lab) for lab in labels], s=40, c=t["point"])
        ax.set_yticks(range(len(uniq)), uniq, fontsize=8)
        ax.set_xlabel(k)
        ax.set_title(f"One-at-a-time sweep around representative failure: {k}", fontsize=9)
        fig.tight_layout()
        fig.savefig(out / f"sweep_{k}{sfx}.png", dpi=130)
        plt.close(fig)
        files.append(f"sweep_{k}{sfx}.png")

    rep = (s.get("step2_failures") or {}).get("representative")
    if rep:
        ep = read_episode(Path(rep["artifact"]))
        files.append(trajectory_plot(ep, out / f"representative_failure{sfx}.png", t).name)
    return files


def trajectory_plot(ep, path: Path, t: dict[str, Any] | None = None) -> Path:
    t = t or THEMES["light"]
    tt, obj = arr(ep, "state.object_pos")
    _, tcp = arr(ep, "state.tcp_pos")
    _, c = arr(ep, "state.contacts")
    hit = (c[:, 0].astype(int) & (C_OBJECT_OBSTACLE | C_ROBOT_OBSTACLE)) > 0
    p = ep.replay.env_params
    with plt.rc_context(_rc(t)):
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 3.2))
        a1.plot(tt, tcp[:, 2], label="TCP z", color=t["line1"])
        a1.plot(tt, obj[:, 2], label="object z", color=t["line2"])
        if hit.any():
            a1.fill_between(tt, 0, max(0.3, float(np.nanmax(tcp[:, 2]))), where=hit, color=t["bad"], alpha=0.2, label="obstacle contact")
        if p.get("obstacle_height"):
            a1.axhline(p["obstacle_height"], ls="--", color=t["warn"], lw=1, label="obstacle top")
        a1.set_xlabel("t (s)")
        a1.set_ylabel("m")
        a1.legend(fontsize=7)
        a1.set_title(f"{ep.diagnosis.category if ep.diagnosis else ep.outcome.label} — height vs time", fontsize=9)
        a2.plot(tcp[:, 0], tcp[:, 1], color=t["line1"], label="TCP path")
        a2.plot(obj[:, 0], obj[:, 1], color=t["line2"], label="object path")
        r = p["target_half_size"]
        a2.add_patch(plt.Rectangle((p["target_x"] - r, p["target_y"] - r), 2 * r, 2 * r, color=t["ok"], alpha=0.2, label="target"))
        if p.get("obstacle_height"):
            a2.add_patch(plt.Rectangle((-0.12, 0.0), 0.24, 0.02, color=t["warn"], alpha=0.45, label="obstacle"))
        a2.set_aspect("equal")
        a2.set_xlabel("x (m)")
        a2.set_ylabel("y (m)")
        a2.legend(fontsize=7)
        a2.set_title("top-down path", fontsize=9)
        fig.suptitle(f"AMPERES  ·  episode {ep.episode_id} (simulation)", fontsize=8, color=t["muted"])
        fig.tight_layout()
        fig.savefig(path, dpi=120)
        plt.close(fig)
    return path


def markdown(s: dict[str, Any]) -> str:
    b = s["step1_baseline"]
    L = [f"# Experiment report — {s['experiment_id']}", "",
         "> **Scope:** every number below was measured in MuJoCo simulation by this harness. Nothing here is evidence "
         "about physical-robot performance.", "",
         f"- Created: {s['created_at']}", f"- Code digest: `{s['environment']['code_digest']}` "
         f"(git: {s['environment']['git_revision'] or 'not a git repository'})",
         f"- MuJoCo {s['environment']['dependencies']['mujoco']}, Python {s['environment']['python']}, {s['environment']['platform']}",
         f"- Baseline: `{s['baseline_label']}` (scripted state-machine controller, not a learned policy)",
         f"- Primary metric: {s['config']['primary_metric']}", "",
         "## Step 1 — Baseline", "",
         "| split | n | successes | success rate | 95% CI (Wilson) | collisions | timeouts |", "|---|---|---|---|---|---|---|"]
    for split in ("nominal", "dev"):
        x = b[split]
        L.append(f"| {split} | {x['n']} | {x['successes']} | {_pct(x['success_rate'])} | {_ci(x['success_rate_ci95'])} | "
                 f"{_pct(x['collision_rate'])} | {_pct(x['timeout_rate'])} |")
    L += ["", f"Nominal repeats produced identical initial-state digests: **{b['nominal_identical_initial_state']}**.", "",
          "## Step 2 — Failures on the dev set", "",
          "| category | episodes |", "|---|---|"]
    f = s["step2_failures"]
    L += [f"| {k} | {v} |" for k, v in f["dev_failure_counts"].items()]
    rep = f["representative"]
    if rep:
        L += ["", f"Target category (most frequent): **{f['target_category']}**. Representative failure: "
              f"`{rep['episode_id']}` (rule `{rep['rule_id']}`), condition `{rep['condition_id']}` with changed parameters "
              f"`{json.dumps(f['representative_condition']['changed'])}`.", "", "![](representative_failure.png)"]
    r = s["step3_reproduction"]["rates"]
    L += ["", "## Step 3 — Reproduction", "",
          f"Attempted {s['step3_reproduction']['n_attempted_episodes']} dev failures. Criteria were fixed before running "
          "(see `reproduce.CRITERIA`).", "", "| mode | eligible attempts | exact | exact rate | exact or symptom |", "|---|---|---|---|---|"]
    for mode in ("rerun", "action_replay"):
        m = r[mode]
        L.append(f"| {mode} | {m['eligible_attempts']} | {m['exact']} | {_pct(m['exact_rate'])} | {m['exact_or_symptom']} |")
    v = s.get("step4_variations") or {}
    if v:
        L += ["", "## Step 4 — Controlled variations around the representative failure", "",
              f"{v['n']} neighbouring conditions (run `{v['run_id']}`). {v['note']}", "",
              "| parameter | points | same-category fraction | observed transitions |", "|---|---|---|---|"]
        for k, sw in v["sweeps"].items():
            tr = "; ".join(f"{t['from']}→{t['to']} between {t['between'][0]:.4g} and {t['between'][1]:.4g}"
                           for t in sw["observed_transitions"]) or "none"
            L.append(f"| {k} | {len(sw['points'])} | {sw['same_category_fraction']:.2f} | {tr} |")
        L.append(f"| joint jitter | {v['jitter']['n']} | {v['jitter']['same_category'] / max(1, v['jitter']['n']):.2f} | — |")
    iv = s["step5_intervention"]
    L += ["", "## Step 5 — Intervention selection (dev set only)", "", f"Rule: {iv['selection_rule']}. Frozen at {iv['frozen_at']}.", "",
          "| candidate | change | dev successes | fixed | broken | mean duration (s) |", "|---|---|---|---|---|---|"]
    for c in iv["candidates"]:
        d = c["dev"]
        L.append(f"| {c['name']} | `{json.dumps(c['delta'])}` | {d['successes']}/{d['n']} | {d['fixed']} | {d['broken']} | "
                 f"{d['mean_duration_all_s']:.2f} |")
    L += ["", f"Selected: **{iv['selected']}**; alternative ablation: **{iv['alternative']}**.", "",
          "## Step 6 — Held-out and nominal comparison (paired, same conditions)", "",
          "| arm | split | baseline | treatment | Δ success rate | 95% CI (paired bootstrap) | fixed | broken | McNemar exact p |",
          "|---|---|---|---|---|---|---|---|---|"]
    for name, cmps in s["step6_comparison"].items():
        for split in ("heldout", "nominal"):
            c = cmps[split]
            L.append(f"| {name} | {split} | {c['baseline']['successes']}/{c['n_pairs']} | {c['treatment']['successes']}/{c['n_pairs']} | "
                     f"{_pct(c['success_rate_diff'])} | {_ci(c['success_rate_diff_ci95_paired_bootstrap'])} | "
                     f"{c['fixed_by_treatment']} | {c['broken_by_treatment']} | "
                     f"{'n/a' if c['mcnemar_exact_p'] is None else f'{c['mcnemar_exact_p']:.3g}'} |")
    L += ["", "![](heldout_success.png)", "", "![](heldout_failure_categories.png)", "",
          "Execution cost on held-out (simulated seconds of robot time, mean over all episodes):", ""]
    for name, x in s["step6_heldout_summaries"].items():
        L.append(f"- {name}: mean episode duration {x['mean_duration_all_s']:.2f} s; wall-clock compute {x['wall_time_total_s']:.1f} s")
    g = s["step7_regression"]
    L += ["", "## Step 7 — Regression suite", "",
          f"Suite `{g['suite_id']}`: {g['n_cases']} cases built from dev evidence (reproduced failures, baseline successes as "
          "guards, nominal). Because the suite comes from the dev set, its pass rate is not independent of selection; "
          "step 6 is the independent evaluation.", "",
          "| arm | passed | fixed | still failing | regressed | still passing |", "|---|---|---|---|---|---|"]
    for name, x in g["results"].items():
        sc = x["status_counts"]
        L.append(f"| {name} | {x['passed']}/{x['n_cases']} | {sc['fixed']} | {sc['still_failing']} | {sc['regressed']} | {sc['still_passing']} |")
    L += ["", "## Step 8 — Reproducing this report", "", "```bash", "python -m amperes.cli demo --n-dev "
          f"{s['config']['n_dev']} --n-heldout {s['config']['n_heldout']}", "```", "",
          "Seeds: dev master seed "
          f"{s['config']['dev_master_seed']}, held-out master seed {s['config']['heldout_master_seed']}. "
          "See `manifest.json` for run ids and config hashes.", ""]
    return "\n".join(L)


def write_report(s: dict[str, Any]) -> Path:
    out = report_dir(s["experiment_id"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(s, indent=2, default=str))
    plots = _plots(s, out)
    (out / "report.md").write_text(markdown(s))
    runs = store.query("SELECT run_id, kind, policy_label, policy_config, split, n_episodes, status, wall_s FROM runs "
                       "WHERE experiment_id=?", [s["experiment_id"]])
    manifest = {"experiment_id": s["experiment_id"], "environment": s["environment"], "config": s["config"],
                "runs": runs, "regression_suite": s["step7_regression"]["suite_id"],
                "rerun_command": f"python -m amperes.cli demo --n-dev {s['config']['n_dev']} --n-heldout {s['config']['n_heldout']}",
                "files": ["summary.json", "report.md", "episodes.csv", *plots]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    eps = store.query("SELECT e.episode_id, e.run_id, r.kind, e.policy_label, e.condition_id, e.split, e.seed, e.outcome, "
                      "e.reason, e.category, e.rule_id, e.steps, e.sim_duration_s, e.collision, e.replay_class "
                      "FROM episodes e JOIN runs r ON r.run_id=e.run_id WHERE r.experiment_id=? ORDER BY e.episode_id",
                      [s["experiment_id"]])
    with open(out / "episodes.csv", "w", newline="") as fh:
        if eps:
            w = csv.DictWriter(fh, fieldnames=list(eps[0]))
            w.writeheader()
            w.writerows(eps)
    with zipfile.ZipFile(out / "bundle.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in manifest["files"] + ["manifest.json"]:
            z.write(out / f, f)
        rep = s["step2_failures"].get("representative")
        if rep:
            z.write(rep["artifact"], f"episodes/{Path(rep['artifact']).name}")
    return out

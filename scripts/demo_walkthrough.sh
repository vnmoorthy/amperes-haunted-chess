#!/usr/bin/env bash
# Five-minute technical demo. Offline; synthetic/simulated data only. Assumes scripts/reproduce_all.sh has been run
# (it creates the simulator experiment); otherwise run: python -m amperes.cli demo --n-dev 200 --n-heldout 400
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
export AMPERES_HOME=${AMPERES_HOME:-amperes_data}
A="$PY -m amperes.cli"
q() { $PY - "$@"; }        # run a heredoc script (args after -)
pyc() { $PY -c "$1"; }     # parse JSON piped on stdin

echo "### 1. Import a sample dataset (SYNTHETIC 3-workcell package) in shadow mode"
$A fixtures ops --out "$AMPERES_HOME/samples/demo_ops" > /dev/null
DS=$($A shadow "$AMPERES_HOME/samples/demo_ops" --synthetic --name "DEMO (synthetic) ops package" | pyc "import json,sys; r=json.load(sys.stdin); print(r['dataset_id'])")
echo "dataset: $DS"

echo; echo "### 2. Processing result: one status per episode"
q "$DS" <<'EOF'
import sys, os; sys.path.insert(0, "."); from amperes import store
ds = sys.argv[1]
for r in store.query("SELECT quality_status, COUNT(*) n FROM episodes WHERE dataset_id=? GROUP BY 1 ORDER BY 2 DESC", [ds]): print(f"  {r['quality_status']:28s}{r['n']}")
EOF

echo; echo "### 3. A known data defect, with evidence (truncated recording)"
q "$DS" <<'EOF'
import sys, json; sys.path.insert(0, "."); from amperes import store
f = store.query("SELECT episode_id, rule_id, severity, record_ref, observed, expected, explanation FROM quality_findings WHERE dataset_id=? AND rule_id='C004_truncated' LIMIT 1", [sys.argv[1]])[0]
print(json.dumps(f, indent=1))
EOF

echo; echo "### 4. Valid failed task vs invalid/recording-failure record vs correctly unflagged episode"
q "$DS" <<'EOF'
import sys, json; sys.path.insert(0, "."); from amperes import store
ds = sys.argv[1]
for st in ("valid_failed_task", "infrastructure_error", "valid_success_unverified"):
    e = store.query("SELECT episode_id, outcome, outcome_provenance, session_id FROM episodes WHERE dataset_id=? AND quality_status=? LIMIT 1", [ds, st])[0]
    rules = [r["rule_id"] for r in store.query("SELECT rule_id FROM quality_findings WHERE episode_id=?", [e["episode_id"]])]
    print(f"  {st:26s} {e['episode_id']}  outcome={e['outcome']}  findings={rules or 'none'}")
EOF

echo; echo "### 5-6. Incidents: repeated symptom grouped across sessions, with facts vs hypotheses"
INC=$(q "$DS" <<'EOF'
import sys; sys.path.insert(0, "."); from amperes import store
print(store.query("SELECT incident_id FROM incidents WHERE dataset_id=? AND category='data_recording_failure'", [sys.argv[1]])[0]["incident_id"])
EOF
)
$A incidents show "$INC" | pyc "import json,sys; i=json.load(sys.stdin); print(json.dumps({k:i[k] for k in ('title','n_occurrences','n_sessions','grouping_reason','facts','hypotheses')}, indent=1))"
$A ops correlate "$DS" | pyc "import json,sys; c=json.load(sys.stdin); print('association:', json.dumps(c['statistical_association'][0]))"
$A incidents status "$INC" --to investigating --actor demo --note "assigned to camera owner" | pyc "import json,sys; print('history:', [h['to_status'] for h in json.load(sys.stdin)['history']])"

echo; echo "### 7. Replay: imported log is class D (not replayable); simulator failure is class A and reproduces exactly"
$A incidents to-regression "$INC" | pyc "import json,sys; print(json.load(sys.stdin))"
SIMRUN=$(q <<'EOF'
import sys; sys.path.insert(0, "."); from amperes import store
print(store.query("SELECT run_id FROM runs WHERE kind='baseline_dev' ORDER BY started_at DESC LIMIT 1")[0]["run_id"])
EOF
)
SIMEP=$(q "$SIMRUN" <<'EOF'
import sys; sys.path.insert(0, "."); from amperes import store
print(store.query("SELECT episode_id FROM episodes WHERE run_id=? AND category='COLLISION' ORDER BY episode_id LIMIT 1", [sys.argv[1]])[0]["episode_id"])
EOF
)
$A reproduce "$SIMEP" | pyc "import json,sys; print([(r['mode'], r['replay_class'], r['classification'], r['comparison']['max_object_pos_deviation_m']) for r in json.load(sys.stdin)])"

echo; echo "### 8. Turn the simulator failure incident into a regression case and run it against the fix"
$A incidents build "$SIMRUN" > /dev/null
SIMINC=$(q "$SIMRUN" <<'EOF'
import sys; sys.path.insert(0, "."); from amperes import store
print(store.query("SELECT incident_id FROM incidents WHERE dataset_id=? AND category='COLLISION'", [sys.argv[1]])[0]["incident_id"])
EOF
)
SUITE=$($A incidents to-regression "$SIMINC" | pyc "import json,sys; r=json.load(sys.stdin); print(r['suite_id'])")
echo '{"obstacle_aware": true}' > "$AMPERES_HOME/obstacle_aware.json"
$A regress "$SUITE" --label baseline | pyc "import json,sys; r=json.load(sys.stdin); print('baseline   ', r['status_counts'])"
$A regress "$SUITE" --policy-config "$AMPERES_HOME/obstacle_aware.json" --label obstacle_aware | pyc "import json,sys; r=json.load(sys.stdin); print('intervention', r['status_counts'])"

echo; echo "### 9. Export the pilot readiness report"
$A pilot-report "$DS"
echo; echo "### Limitation shown honestly: label flips without state evidence are not detectable"
q <<'EOF'
import json, os
p = os.path.join(os.environ["AMPERES_HOME"], "artifacts/reports/detector_eval/detector_eval.json")
d = json.load(open(p))["by_defect"]["label_flip_without_state"]
print(f"  label_flip_without_state: detected {d['detected']}/{d['n']} (detectable by design: {d['detectable_by_design']})")
EOF

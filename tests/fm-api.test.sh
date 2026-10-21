#!/usr/bin/env bash
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=tests/lib.sh
. "$ROOT/tests/lib.sh"

TMP_ROOT=$(fm_test_tmproot fm-api)
HOME_DIR="$TMP_ROOT/home"
BIN="$TMP_ROOT/bin"
API="$BIN/fm-api.sh"
mkdir -p "$HOME_DIR/projects/demo" "$HOME_DIR/data" "$BIN"
cp "$ROOT/bin/fm-api.sh" "$API"
printf '%s\n' '- demo [local-only] - disposable fixture' > "$HOME_DIR/data/projects.md"

cat > "$BIN/fm-fleet-snapshot.sh" <<'EOF'
#!/usr/bin/env bash
if [ "${FM_API_TEST_SNAPSHOT_FAIL:-0}" = 1 ]; then
  printf '%s\n' 'partial snapshot'
  exit 1
fi
cat "$FM_API_TEST_SNAPSHOT"
EOF
cat > "$BIN/fm-project-mode.sh" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  --raw) printf '%s\n' "${FM_API_TEST_POSTURE:-local-only} off" ;;
  --branch-prefix) printf '%s\n' "${FM_API_TEST_BRANCH_PREFIX:-fm/}" ;;
  --forge) printf '%s\n' "${FM_API_TEST_FORGE:-none}" ;;
esac
EOF
cat > "$BIN/fm-tasks-axi.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' add >> "$FM_API_TEST_ADD_CALLS"
printf '{"ok":true,"task":{"id":"%s"}}\n' "${FM_API_TEST_TASK_ID:-api-canary-1}"
EOF
cat > "$BIN/fm-brief.sh" <<'EOF'
#!/usr/bin/env bash
mkdir -p "$FM_HOME/data/$1"
printf '%s\n' "$*" > "$FM_API_TEST_BRIEF_ARGS"
printf '%s\n' '{TASK}' '{FIRSTMATE_SPEC}' > "$FM_HOME/data/$1/brief.md"
EOF
cat > "$BIN/fm-spawn.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$FM_API_TEST_SPAWN_ARGS"
EOF
cat > "$BIN/fm-send.sh" <<'EOF'
#!/usr/bin/env bash
[ "${FM_HOME:-}" = "$FM_API_TEST_EXPECT_HOME" ] || exit 1
printf '%s\n' "$*" > "$FM_API_TEST_SEND_ARGS"
EOF
cat > "$BIN/fm-control.sh" <<'EOF'
#!/usr/bin/env bash
[ "${FM_HOME:-}" = "$FM_API_TEST_EXPECT_HOME" ] || exit 1
[ "${FM_API_TEST_CONTROL_FAIL:-0}" != 1 ]
EOF
for helper in "$BIN"/*.sh; do
  [ "$helper" = "$API" ] || chmod +x "$helper"
done
export FM_API_TEST_EXPECT_HOME="$HOME_DIR"
ADD_CALLS="$TMP_ROOT/tasks.add"
: > "$ADD_CALLS"
export FM_API_TEST_ADD_CALLS="$ADD_CALLS"

SNAPSHOT="$TMP_ROOT/snapshot.json"
cat > "$SNAPSHOT" <<'EOF'
{"tasks":[
  {"id":"api-canary-1","project":"demo","kind":"ship","current_state":{"state":"parked","detail":"choose A or B"},"hints":{"pending_decision":true,"open_decisions":[{"key":"choice","verb":"needs-decision","summary":"Choose A or B"}]},"endpoint":{"status":"alive"}},
  {"id":"api-canary-2","project":"demo","kind":"ship","current_state":{"state":"done","detail":"finished"},"hints":{"pending_decision":false,"open_decisions":[]},"endpoint":{"status":"absent"}}
]}
EOF
mkdir -p "$HOME_DIR/data/api-canary-1"
printf '%s\n' 'canary result' > "$HOME_DIR/data/api-canary-1/report.md"

CREATE="{\"project\":\"demo\",\"title\":\"safe canary\",\"instruction\":\"Inspect only; preserve \$HOME, quotes: \\\"yes\\\", and {FIRSTMATE_SPEC}.\",\"authority\":{\"inspect\":true,\"edit\":true,\"commit\":true,\"push\":false,\"deploy\":false}}"
created=$(printf '%s' "$CREATE" | FM_HOME="$HOME_DIR" FM_API_TEST_BRIEF_ARGS="$TMP_ROOT/brief.args" FM_API_TEST_SPAWN_ARGS="$TMP_ROOT/spawn.args" \
  "$API" create)
printf '%s\n' "$created" | jq -e '.state == "working" and .task_id == "api-canary-1"' >/dev/null \
  || fail 'create returns JSON with the Firstmate task id'
grep -F "Inspect only; preserve \$HOME, quotes: \"yes\", and {FIRSTMATE_SPEC}." "$HOME_DIR/data/api-canary-1/brief.md" >/dev/null \
  || fail 'create safely fills the generated brief'
grep -F 'Authority: inspect=true' "$HOME_DIR/data/api-canary-1/brief.md" >/dev/null \
  || fail 'task text cannot consume the spec placeholder'
grep -F -- '--branch-prefix fm/ --forge none' "$TMP_ROOT/brief.args" >/dev/null \
  || fail 'create passes registered branch and forge details to the brief'
grep -F -- '--mode local-only --yolo off' "$TMP_ROOT/spawn.args" >/dev/null \
  || fail 'commit/no-push authority maps to local-only with yolo off'
grep -F -- '--branch-prefix fm/' "$TMP_ROOT/spawn.args" >/dev/null \
  || fail 'create passes registered branch details to spawn'
grep -F -- '--harness codex --effort low --codex-appserver' "$TMP_ROOT/spawn.args" >/dev/null \
  || fail 'create uses the fixed supervised Codex app-server transport'
pass 'create safely delegates intake, registered posture, brief, and launch'

FORGE_CREATE=$(printf '%s' "$CREATE" | jq -c '.title = "registered forge" | .authority.push = true | .mode = "no-mistakes"')
printf '%s' "$FORGE_CREATE" | FM_HOME="$HOME_DIR" FM_API_TEST_POSTURE=no-mistakes \
  FM_API_TEST_BRANCH_PREFIX=custom/ FM_API_TEST_FORGE=gerrit FM_API_TEST_TASK_ID=api-canary-2 \
  FM_API_TEST_BRIEF_ARGS="$TMP_ROOT/brief.args" FM_API_TEST_SPAWN_ARGS="$TMP_ROOT/spawn.args" \
  "$API" create >/dev/null || fail 'registered forge task should create'
grep -F -- '--mode no-mistakes --branch-prefix custom/ --forge gerrit' "$TMP_ROOT/brief.args" >/dev/null \
  || fail 'create preserves registered branch and forge details in the brief'
grep -F -- '--mode no-mistakes --yolo off --branch-prefix custom/' "$TMP_ROOT/spawn.args" >/dev/null \
  || fail 'create preserves registered branch details in spawn'
pass 'create preserves project branch and forge registration'

task_id=$(printf '%s\n' "$created" | jq -r .task_id)
status=$(FM_HOME="$HOME_DIR" FM_API_TEST_SNAPSHOT="$SNAPSHOT" "$API" status "$task_id")
printf '%s\n' "$status" | jq -e '.task_id == "api-canary-1" and .state == "needs-user" and .needs_user and .open_decisions[0].key == "choice" and .open_decisions[0].summary == "Choose A or B" and .result == "canary result\n"' >/dev/null \
  || fail 'status exposes the decision key and bounded result under the created task id'
pass 'status projects Firstmate state and result'

listed=$(FM_HOME="$HOME_DIR" FM_API_TEST_SNAPSHOT="$SNAPSHOT" "$API" list)
printf '%s\n' "$listed" | jq -e '.tasks | length == 1 and .[0].task_id == "api-canary-1" and .[0].state == "needs-user" and .[0].open_decisions[0].key == "choice"' >/dev/null \
  || fail 'list returns only nonterminal bounded task projections'
pass 'list returns machine-readable active work'

decision_key=$(printf '%s\n' "$status" | jq -r '.open_decisions[0].key')
steer_request=$(jq -cn --arg key "$decision_key" '{message:"Use option B.",resolve_keys:[$key]}')
add_count_before=$(wc -l < "$ADD_CALLS" | tr -d '[:space:]')
steered=$(printf '%s' "$steer_request" \
  | FM_HOME="$HOME_DIR" FM_API_TEST_SEND_ARGS="$TMP_ROOT/send.args" "$API" steer "$task_id")
printf '%s\n' "$steered" | jq -e --arg task_id "$task_id" '.accepted and .task_id == $task_id' >/dev/null \
  || fail 'steer returns structured delivery acceptance'
grep -F -- "$task_id --resolve-key $decision_key" "$TMP_ROOT/send.args" >/dev/null \
  || fail 'steer resolves the surfaced decision on the original task id'
[ "$(wc -l < "$ADD_CALLS" | tr -d '[:space:]')" = "$add_count_before" ] \
  || fail 'steer must not create a second task'
pass 'steer delegates to the existing message plane'

cancelled=$(FM_HOME="$HOME_DIR" "$API" cancel "$task_id")
printf '%s\n' "$cancelled" | jq -e '.agent_stopped and .task_state_retained and (.semantics | contains("not a durable cancelled state"))' >/dev/null \
  || fail 'cancel reports only the verified exit semantics'
pass 'cancel delegates to verified exit without inventing task state'

rejected=$(printf '%s' '{"project":"demo","title":"unsafe","instruction":"no deploy","authority":{"inspect":true,"edit":true,"commit":true,"push":false,"deploy":true}}' \
  | FM_HOME="$HOME_DIR" "$API" create 2>/dev/null && exit 1 || true)
printf '%s\n' "$rejected" | jq -e '.error.code == "unsupported_authority"' >/dev/null \
  || fail 'unsupported deploy authority fails with structured JSON'
pass 'authority gaps fail closed'

for command in list 'status api-canary-1'; do
  read -r -a args <<< "$command"
  if rejected=$(FM_HOME="$HOME_DIR" FM_API_TEST_SNAPSHOT_FAIL=1 "$API" "${args[@]}"); then
    fail 'snapshot failure must return nonzero'
  fi
  printf '%s\n' "$rejected" | jq -se 'length == 1 and .[0].error.code == "snapshot_failed"' >/dev/null \
    || fail 'snapshot failure returns exactly one original error'
done
pass 'list and status preserve snapshot failures'

for home_mode in default explicit; do
  if [ "$home_mode" = default ]; then
    export FM_API_TEST_EXPECT_HOME="$TMP_ROOT"
    unset FM_HOME FM_ROOT_OVERRIDE
  else
    export FM_API_TEST_EXPECT_HOME="$HOME_DIR" FM_HOME="$HOME_DIR"
  fi
  steered=$(printf '%s' '{"message":"Continue."}' | FM_API_TEST_SEND_ARGS="$TMP_ROOT/send.args" "$API" steer "$task_id")
  printf '%s\n' "$steered" | jq -e '.accepted' >/dev/null || fail 'steer inherits resolved home'
  cancelled=$("$API" cancel "$task_id")
  printf '%s\n' "$cancelled" | jq -e '.agent_stopped' >/dev/null || fail 'cancel inherits resolved home'
done
pass 'lifecycle helpers inherit default and explicit homes'

rm -f "$TMP_ROOT/spawn.args"
for push in true false; do
  for mode in '""' '"local-only"' '"no-mistakes"' '"direct-PR"'; do
    request=$(printf '%s' "$CREATE" | jq --argjson push "$push" --argjson mode "$mode" '.authority.commit = false | .authority.push = $push | .mode = $mode')
    if rejected=$(printf '%s' "$request" | FM_HOME="$HOME_DIR" FM_API_TEST_SPAWN_ARGS="$TMP_ROOT/spawn.args" "$API" create); then
      fail 'no-commit authority must be refused'
    fi
    printf '%s\n' "$rejected" | jq -e '.error.code == "unsupported_authority"' >/dev/null || fail 'no-commit refusal is structured'
  done
done
[ ! -e "$TMP_ROOT/spawn.args" ] || fail 'no-commit authority must not launch'
pass 'no-commit authority fails closed across push and mode combinations'

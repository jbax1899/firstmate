#!/usr/bin/env bash
# fm-api.sh - JSON CLI over Firstmate's existing task and lifecycle owners.
# Usage: fm-api.sh list
#        fm-api.sh status <task-id>
#        fm-api.sh create < request.json
#        fm-api.sh steer <task-id> < request.json
#        fm-api.sh cancel <task-id>
# Create JSON: {"project":"<registered-name>","title":"...","instruction":"...","authority":{"inspect":true,"edit":true,"commit":true,"push":false,"deploy":false},"mode":"local-only"}
# Steer JSON: {"message":"...","resolve_keys":["decision-key"]}
# Create requires commit authority, maps local commits to local-only, and
# pushes to an explicit no-mistakes or direct-PR task; deploy is
# always refused. Every task is created through tasks-axi, fm-brief, and fm-spawn.
# Status/list project fm-fleet-snapshot; steer and cancel call fm-send and
# fm-control. Cancel means verified agent exit only: task state and work remain.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FM_ROOT="${FM_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export FM_HOME="${FM_HOME:-$FM_ROOT}"
DATA="${FM_DATA_OVERRIDE:-$FM_HOME/data}"
PROJECTS="${FM_PROJECTS_OVERRIDE:-$FM_HOME/projects}"
CONFIG="${FM_CONFIG_OVERRIDE:-$FM_HOME/config}"
MAX_REQUEST_BYTES=65536
MAX_RESULT_BYTES=16384

api_error() {
  local code=$1 message=$2 task_id=${3:-}
  jq -cn --arg code "$code" --arg message "$message" --arg task_id "$task_id" \
    '{error:{code:$code,message:$message},task_id:(if $task_id == "" then null else $task_id end)}'
  exit 1
}

read_request() {
  REQUEST=$(head -c "$((MAX_REQUEST_BYTES + 1))") || api_error invalid_input 'Could not read JSON input.'
  [ -n "$REQUEST" ] || api_error invalid_input 'JSON input is required.'
  [ "$(printf '%s' "$REQUEST" | wc -c | tr -d '[:space:]')" -le "$MAX_REQUEST_BYTES" ] \
    || api_error invalid_input 'JSON input exceeds 65536 bytes.'
  printf '%s' "$REQUEST" | jq -e 'type == "object"' >/dev/null 2>&1 \
    || api_error invalid_input 'Input must be one JSON object.'
}

valid_id() {
  case "$1" in ''|.*|*[!A-Za-z0-9._-]*) return 1 ;; esac
  [ "${#1}" -le 64 ] && [ "$1" != . ] && [ "$1" != .. ]
}

# shellcheck disable=SC2016 # This single-quoted jq program is passed as data, not evaluated by Bash.
json_state_filter='def api_task:
  . as $t
  | {task_id:$t.id,
     state:(if $t.hints.pending_decision then "needs-user"
            elif $t.current_state.state == "done" then "complete"
            else ($t.current_state.state // "unknown") end),
     summary:($t.current_state.detail // null),
     needs_user:($t.hints.pending_decision // false),
     open_decisions:($t.hints.open_decisions // []),
     project:($t.project // null),kind:($t.kind // null),
     endpoint_status:($t.endpoint.status // "unknown")};'

project_snapshot() {
  "$SCRIPT_DIR/fm-fleet-snapshot.sh" --json 2>/dev/null
}

usage() {
  sed -n '2,${/^#/!q;p;}' "$0" | sed 's/^# \{0,1\}//'
}

COMMAND=${1:-}
case "$COMMAND" in
  -h|--help) usage; exit 0 ;;
  list)
    [ "$#" -eq 1 ] || api_error invalid_input 'Usage: fm-api.sh list'
    SNAPSHOT=$(project_snapshot) \
      || api_error snapshot_failed 'Firstmate could not produce a fleet snapshot.'
    printf '%s\n' "$SNAPSHOT" | jq -ce --argjson limit 50 \
      "$json_state_filter .tasks as \$all
       | [\$all[] | select(.current_state.state != \"done\" and .current_state.state != \"failed\")]
       | {tasks:(.[0:\$limit] | map(api_task)),truncated:(length > \$limit)}" \
      || api_error snapshot_invalid 'Firstmate returned an invalid task snapshot.'
    ;;
  status)
    if [ "$#" -ne 2 ] || ! valid_id "${2:-}"; then
      api_error invalid_input 'Usage: fm-api.sh status <task-id>'
    fi
    SNAPSHOT=$(project_snapshot) \
      || api_error snapshot_failed 'Firstmate could not produce a fleet snapshot.'
    TASK=$(printf '%s\n' "$SNAPSHOT" | jq -ce --arg id "$2" '.tasks[] | select(.id == $id)' 2>/dev/null) \
      || api_error not_found 'Task is not present in this Firstmate home.'
    RESULT=null
    REPORT="$DATA/$2/report.md"
    [ ! -L "$DATA/$2" ] || api_error unsafe_result 'Refusing to read a report from a symlinked task directory.'
    if [ -L "$REPORT" ]; then api_error unsafe_result 'Refusing to read a symlinked task report.'; fi
    if [ -f "$REPORT" ]; then
      BYTES=$(wc -c < "$REPORT" | tr -d '[:space:]')
      [ "$BYTES" -le "$MAX_RESULT_BYTES" ] || api_error result_too_large 'Task report exceeds the 16384-byte result limit.' "$2"
      RESULT=$(jq -Rn --rawfile result "$REPORT" '$result') || api_error result_unreadable 'Could not encode the task report.' "$2"
    fi
    printf '%s\n' "$TASK" | jq -c --argjson result "$RESULT" "$json_state_filter api_task + {result:\$result}"
    ;;
  create)
    shift
    [ "$#" -eq 0 ] || api_error invalid_input 'Usage: fm-api.sh create < request.json'
    read_request
    PROJECT=$(printf '%s' "$REQUEST" | jq -er '.project | strings | select(test("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"))' 2>/dev/null) \
      || api_error invalid_input 'project must be a registered project name.'
    TITLE=$(printf '%s' "$REQUEST" | jq -er '.title | strings | select(length > 0 and length <= 120 and (contains("\n") | not) and (contains("\r") | not))' 2>/dev/null) \
      || api_error invalid_input 'title must be one non-empty line of at most 120 characters.'
    INSTRUCTION=$(printf '%s' "$REQUEST" | jq -er '.instruction | strings | select(length > 0 and length <= 20000 and (contains("\u0000") | not))' 2>/dev/null) \
      || api_error invalid_input 'instruction must be non-empty, contain no NUL, and be at most 20000 characters.'
    AUTH=$(printf '%s' "$REQUEST" | jq -ce '
      .authority as $a
      | if ($a | type) != "object" or ($a|keys|sort) != ["commit","deploy","edit","inspect","push"] then error("authority fields")
        elif any(["inspect","edit","commit","push","deploy"][]; ($a[.] | type) != "boolean") then error("authority values")
        else $a end' 2>/dev/null) || api_error invalid_authority 'authority must provide boolean inspect, edit, commit, push, and deploy fields.'
    [ "$(printf '%s' "$AUTH" | jq -r .inspect)" = true ] \
      || api_error unsupported_authority 'inspect=true is required.'
    [ "$(printf '%s' "$AUTH" | jq -r .edit)" = true ] \
      || api_error unsupported_authority 'Firstmate does not provide an enforceable no-edit worker capability.'
    [ "$(printf '%s' "$AUTH" | jq -r .deploy)" = false ] \
      || api_error unsupported_authority 'deploy=true is not supported by this interface.'
    COMMIT=$(printf '%s' "$AUTH" | jq -r .commit)
    [ "$COMMIT" = true ] \
      || api_error unsupported_authority 'Firstmate does not provide a no-commit worker contract; scouts permit scratch commits.'
    PUSH=$(printf '%s' "$AUTH" | jq -r .push)
    MODE=$(printf '%s' "$REQUEST" | jq -r '.mode // empty')
    if [ "$PUSH" = true ]; then
      if [ "$MODE" != no-mistakes ] && [ "$MODE" != direct-PR ]; then
        api_error unsupported_authority 'push=true requires commit=true and an explicit no-mistakes or direct-PR mode.'
      fi
    else
      [ -z "$MODE" ] || [ "$MODE" = local-only ] \
        || api_error unsupported_authority 'commit=true with push=false maps only to local-only.'
      MODE=local-only
    fi
    case "$PROJECT" in *..*|.*) api_error invalid_input 'project name is not a safe registry key.' ;; esac
    PROJECT_ROOT=$(CDPATH='' cd -- "$PROJECTS" 2>/dev/null && pwd -P) \
      || api_error project_unavailable 'This home projects/ directory is unavailable.'
    PROJECT_PATH="$PROJECT_ROOT/$PROJECT"
    [ -d "$PROJECT_PATH" ] && [ ! -L "$PROJECT_PATH" ] \
      || api_error project_unavailable 'Project must already exist as a registered, non-symlink directory under this home projects/.'
    [ "$(CDPATH='' cd -- "$PROJECT_PATH" 2>/dev/null && pwd -P)" = "$PROJECT_PATH" ] \
      || api_error project_unavailable 'Project resolves outside its registered projects/ directory.'
    REGISTRY_ERROR=$(mktemp) || api_error internal_error 'Could not create a temporary validation file.'
    if ! REGISTERED_POSTURE=$("$SCRIPT_DIR/fm-project-mode.sh" --raw "$PROJECT" 2>"$REGISTRY_ERROR") \
      || [ -s "$REGISTRY_ERROR" ]; then
      rm -f -- "$REGISTRY_ERROR"
      api_error project_unregistered 'Project is not valid in this home data/projects.md registry.'
    fi
    rm -f -- "$REGISTRY_ERROR"
    REGISTERED_MODE=${REGISTERED_POSTURE%% *}
    case "$REGISTERED_MODE" in no-mistakes|direct-PR|local-only|no-mistakes-prod-only) ;; *) api_error project_unregistered 'Project registry posture is invalid.' ;; esac
    BRANCH_PREFIX=$("$SCRIPT_DIR/fm-project-mode.sh" --branch-prefix "$PROJECT" 2>/dev/null) \
      || api_error project_unregistered 'Project branch-prefix registration is invalid.'
    REGISTERED_FORGE=$("$SCRIPT_DIR/fm-project-mode.sh" --forge "$PROJECT" 2>/dev/null) \
      || api_error project_unregistered 'Project forge registration is invalid.'
    [ ! -e "$CONFIG/crew-dispatch.json" ] && [ ! -L "$CONFIG/crew-dispatch.json" ] \
      || api_error dispatch_requires_intake 'A configured crew-dispatch profile needs Firstmate intake; this API does not select harness, model, or effort.'
    case "$MODE:$REGISTERED_MODE" in
      local-only:local-only) ;;
      no-mistakes:no-mistakes|no-mistakes:no-mistakes-prod-only|no-mistakes:direct-PR|direct-PR:direct-PR) ;;
      *) api_error unsupported_mode 'Requested delivery mode is not supported by the registered project posture.' ;;
    esac
    TASK_JSON=$("$SCRIPT_DIR/fm-tasks-axi.sh" add "$TITLE" --mint --kind ship --repo "$PROJECT" --body "$INSTRUCTION" --json 2>/dev/null) \
      || api_error intake_failed 'Could not create the Firstmate backlog task.'
    TASK_ID=$(printf '%s' "$TASK_JSON" | jq -er '.task.id' 2>/dev/null) \
      || api_error intake_invalid 'tasks-axi did not return a task id.'
    valid_id "$TASK_ID" || api_error intake_invalid 'tasks-axi returned an unsafe task id.'
    BRIEF_ARGS=("$TASK_ID" "$PROJECT" --mode "$MODE" --branch-prefix "$BRANCH_PREFIX" --forge "$REGISTERED_FORGE")
    if ! "$SCRIPT_DIR/fm-brief.sh" "${BRIEF_ARGS[@]}" >/dev/null 2>&1; then
      api_error brief_failed 'Task was queued, but Firstmate could not scaffold its brief.' "$TASK_ID"
    fi
    SPEC="Authority: inspect=true, edit=true, commit=$COMMIT, push=$PUSH, deploy=false. Stay within the Firstmate-isolated worktree. Do not deploy."
    if ! jq -Rrs --arg task "$INSTRUCTION" --arg spec "$SPEC" '
      def replace_first($old; $new): index($old) as $i | if $i == null then error("placeholder missing") else .[:$i] + $new + .[$i + ($old|length):] end;
      replace_first("{FIRSTMATE_SPEC}"; $spec) | replace_first("{TASK}"; $task)
    ' "$DATA/$TASK_ID/brief.md" > "$DATA/$TASK_ID/brief.tmp" 2>/dev/null \
      ; then
      api_error brief_failed 'Task was queued, but the generated brief could not be filled.' "$TASK_ID"
    fi
    mv -- "$DATA/$TASK_ID/brief.tmp" "$DATA/$TASK_ID/brief.md" \
      || api_error brief_failed 'Task was queued, but the generated brief could not be published.' "$TASK_ID"
    SPAWN_ARGS=("$TASK_ID" "$PROJECT_PATH" --mode "$MODE" --yolo off --branch-prefix "$BRANCH_PREFIX" --harness codex --effort low --codex-appserver)
    if ! "$SCRIPT_DIR/fm-spawn.sh" "${SPAWN_ARGS[@]}" >/dev/null 2>&1; then
      api_error spawn_failed 'Task was queued and briefed, but Firstmate could not confirm a worker launch.' "$TASK_ID"
    fi
    jq -cn --arg task_id "$TASK_ID" '{task_id:$task_id,state:"working"}'
    ;;
  steer)
    if [ "$#" -ne 2 ] || ! valid_id "${2:-}"; then
      api_error invalid_input 'Usage: fm-api.sh steer <task-id> < request.json>'
    fi
    read_request
    MESSAGE=$(printf '%s' "$REQUEST" | jq -er '.message | strings | select(length > 0 and length <= 20000)' 2>/dev/null) \
      || api_error invalid_input 'message must be non-empty and at most 20000 characters.'
    KEY_JSON=$(printf '%s' "$REQUEST" | jq -ce '.resolve_keys // [] | if type == "array" and all(.[]; type == "string" and test("^[A-Za-z0-9._-]{1,64}$")) then . else error("invalid resolve_keys") end' 2>/dev/null) \
      || api_error invalid_input 'resolve_keys must be an array of valid decision keys.'
    mapfile -t KEYS < <(printf '%s' "$KEY_JSON" | jq -r '.[]')
    SEND_ARGS=("$2")
    for key in "${KEYS[@]}"; do SEND_ARGS+=(--resolve-key "$key"); done
    SEND_ARGS+=("$MESSAGE")
    "$SCRIPT_DIR/fm-send.sh" "${SEND_ARGS[@]}" >/dev/null 2>&1 \
      || api_error steer_failed 'Firstmate could not confirm delivery of the steering message.' "$2"
    jq -cn --arg task_id "$2" '{task_id:$task_id,accepted:true}'
    ;;
  cancel)
    if [ "$#" -ne 2 ] || ! valid_id "${2:-}"; then
      api_error invalid_input 'Usage: fm-api.sh cancel <task-id>'
    fi
    "$SCRIPT_DIR/fm-control.sh" "$2" exit >/dev/null 2>&1 \
      || api_error cancel_unconfirmed 'Firstmate could not verify agent exit; task and work were not discarded.' "$2"
    jq -cn --arg task_id "$2" '{task_id:$task_id,agent_stopped:true,task_state_retained:true,semantics:"verified agent exit; not a durable cancelled state"}'
    ;;
  *) api_error invalid_command 'Expected one of: create, status, list, steer, cancel.' ;;
esac

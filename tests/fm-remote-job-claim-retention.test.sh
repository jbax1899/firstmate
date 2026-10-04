#!/usr/bin/env bash
# The claim sweep must preserve the former numeric-name eligibility rules.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP_ROOT=$(fm_test_tmproot fm-remote-job-claim-retention)
trap 'rm -rf -- "$TMP_ROOT"' EXIT
export FM_REMOTE_JOB_STATE_ROOT="$TMP_ROOT/state"
. "$ROOT/bin/fm-remote-job-lib.sh"
mkdir -p "$TMP_ROOT/account"
fm_remote_job_prepare_state "$TMP_ROOT/account" || fail "$FM_REMOTE_JOB_ERROR"
for name in 0 notes 1x .private 1 00 01; do
  mkdir "$FM_REMOTE_JOB_SEQ_CLAIMS/$name"
  fm_touch_epoch 946684800 "$FM_REMOTE_JOB_SEQ_CLAIMS/$name"
done
mkdir "$FM_REMOTE_JOB_SEQ_CLAIMS/2"
fm_remote_job_reap_stale "$TMP_ROOT/account" || fail "claim sweep failed"
for name in 0 notes 1x .private 2; do
  assert_present "$FM_REMOTE_JOB_SEQ_CLAIMS/$name" "ineligible or fresh claim $name was reaped"
done
for name in 1 00 01; do
  assert_absent "$FM_REMOTE_JOB_SEQ_CLAIMS/$name" "expired eligible claim $name survived"
done
pass "claim sweep preserves numeric-name eligibility and fresh claims"

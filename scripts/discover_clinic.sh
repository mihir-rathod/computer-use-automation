#!/usr/bin/env bash
# Discovers every clinic capability with the LLM against a running clinic (http://localhost:8100), resetting the
# clinic's data before each run. Commit-capable capabilities run with --commit-mode auto-sandbox, which the clinic
# profile allows because it is a sandbox; against anything real use --commit-mode supervised instead.
#
#   uv run uvicorn clinic.app:app --port 8100 &      # in another terminal
#   scripts/discover_clinic.sh                        # uses your Gemini quota: roughly 8 capabilities x ~8 calls
#
# Existing artifacts are kept; each discovery saves a NEW version as a candidate unless the capability has none yet.
set -u
BASE="${CLINIC_BASE_URL:-http://localhost:8100}"
d() {
  curl -s -X POST "$BASE/_test/reset" >/dev/null
  echo "=== $*"
  uv run python cli.py discover --no-operator-console --max-steps 20 "$@" 2>&1 | grep -E "stop_reason|saved|candidate|current|rror" | head -6
}
d --capability clinic.login --target clinic --param username=frontdesk --param password=desk-demo-123
d --capability clinic.patient_lookup --target clinic --param mrn=LK-100001
d --capability clinic.update_patient_contact --target clinic --param mrn=LK-100001 --param phone=206-555-0199 --param email=avery.new@example.test --param "address=12 Pine St, Oakridge, WA 98021"
d --capability clinic.reschedule_appointment --target clinic --param appointment=A-20002 --param date=2026-03-10 --param time=10:30
d --capability clinic.cancel_appointment --target clinic --commit-mode auto-sandbox --param appointment=A-20003 --param reason=weather
d --capability clinic.issue_refund --target clinic --commit-mode auto-sandbox --param invoice=INV-30001 --param amount=20.00 --param reason=duplicate_payment
d --capability clinic.submit_claim --target clinic --commit-mode auto-sandbox --param invoice=INV-30002 --param amount=124.00
d --capability clinic.write_off_balance --target clinic_supervisor --commit-mode auto-sandbox --param invoice=INV-30002 --param amount=50.00 --param reason=uncollectible
curl -s -X POST "$BASE/_test/reset" >/dev/null

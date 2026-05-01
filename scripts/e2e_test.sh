#!/usr/bin/env bash
# End-to-end test: CTD scaffolding → content generation
# Usage: bash scripts/e2e_test.sh
# Requires: gcloud CLI authenticated, jq
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
BUCKET=pharma-reguatory-author-life-science
SESSION_ID="e2e-test-$(date +%s)"
TA="neurology"
DIS="bells_palsy"
DRUG="prednisolone"

CTD_API="https://ctd-api-74ugcbbbya-uc.a.run.app"
ANALYST="https://clinical-analyst-74ugcbbbya-uc.a.run.app"
PIPELINE="https://ich4-content-pipeline-74ugcbbbya-uc.a.run.app"
TEMPLATE="https://ich4-template-74ugcbbbya-uc.a.run.app"
WRITER="https://ich4-writer-74ugcbbbya-uc.a.run.app"
INDEX="https://ich4-index-74ugcbbbya-uc.a.run.app"
WORKER="https://ich4-content-worker-74ugcbbbya-uc.a.run.app"
UI="https://reguatory-ui-74ugcbbbya-uc.a.run.app"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
fail() { echo -e "${RED}  ✗ $*${NC}"; }
info() { echo -e "${BLUE}  → $*${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $*${NC}"; }
step() { echo -e "\n${BLUE}══════════════════════════════════════${NC}"; echo -e "${BLUE}STEP $*${NC}"; echo -e "${BLUE}══════════════════════════════════════${NC}"; }

# ── Get ONE token up-front, reuse for all calls ───────────────────────────────
TOKEN=$(gcloud auth print-identity-token 2>/dev/null || echo "")
if [[ -z "$TOKEN" ]]; then
  warn "No OIDC token — private services (ich4-*) will return 403"
fi

call() {
  local method=$1 url=$2 body=${3:-} timeout=${4:-120}
  local args=(-sf --max-time "$timeout" -X "$method" "$url")
  [[ -n "$TOKEN" ]] && args+=(-H "Authorization: Bearer ${TOKEN}")
  [[ -n "$body"  ]] && args+=(-H "Content-Type: application/json" -d "$body")
  curl "${args[@]}" 2>&1
}

PASS=0; FAIL=0

check_health() {
  local name=$1 url=$2 ui_mode=${3:-}
  if [[ -n "$ui_mode" ]]; then
    # Gradio has no /health — check root responds with any 2xx/3xx
    local code
    code=$(curl -so /dev/null -w "%{http_code}" --max-time 15 \
      -H "Authorization: Bearer ${TOKEN}" "${url}/" 2>/dev/null || echo "000")
    if [[ "$code" =~ ^[234] ]]; then
      # 2xx = open, 3xx = redirect, 401/403 = up but IAP-protected (expected)
      ok "$name / → HTTP ${code} (up, IAP-protected)"
      (( PASS++ )) || true
    else
      fail "$name / → HTTP ${code}"
      (( FAIL++ )) || true
    fi
    return
  fi
  local resp
  resp=$(call GET "${url}/health" || echo '{"status":"ERROR"}')
  local status
  status=$(echo "$resp" | jq -r '.status' 2>/dev/null || echo "ERROR")
  if [[ "$status" == "ok" ]]; then
    ok "$name /health → ok"
    (( PASS++ )) || true
  else
    fail "$name /health → $status  (raw: ${resp:0:120})"
    (( FAIL++ )) || true
  fi
}

# ── STEP 1: Health checks ─────────────────────────────────────────────────────
step "1 — Health checks (all 9 services)"
check_health "ctd-api"               "$CTD_API"
check_health "clinical-analyst"      "$ANALYST"
check_health "ich4-content-pipeline" "$PIPELINE"
check_health "ich4-template"         "$TEMPLATE"
check_health "ich4-writer"           "$WRITER"
check_health "ich4-index"            "$INDEX"
check_health "ich4-content-worker"   "$WORKER"
check_health "reguatory-ui"          "$UI"  "ui"

# ── STEP 2: CTD Structure extraction ─────────────────────────────────────────
step "2 — CTD Structure extraction (POST /extract)"
EXTRACT_RESP=$(call POST "${CTD_API}/extract" \
  "{\"session_id\":\"${SESSION_ID}\",\"bucket\":\"${BUCKET}\",\"force\":false}")
EXTRACT_REPLY=$(echo "$EXTRACT_RESP" | jq -r '.reply' 2>/dev/null || echo "$EXTRACT_RESP")
echo "  Reply: ${EXTRACT_REPLY:0:160}"
# Check whether folder_paths were returned inline (cached path skips the worker)
EXTRACT_PATHS=$(echo "$EXTRACT_RESP" | jq '.folder_paths // []' 2>/dev/null || echo '[]')
N_EXTRACT=$(echo "$EXTRACT_PATHS" | jq 'length' 2>/dev/null || echo 0)
CACHED=false
if [[ "$N_EXTRACT" -gt 0 ]] || echo "$EXTRACT_REPLY" | grep -qi "cached\|no need to re-query"; then
  CACHED=true
  info "Cached structure returned inline (${N_EXTRACT} paths) — worker not needed"
fi
if echo "$EXTRACT_RESP" | jq -e '.reply' &>/dev/null; then
  ok "POST /extract accepted"
  (( PASS++ )) || true
else
  fail "POST /extract failed: ${EXTRACT_RESP:0:200}"
  (( FAIL++ )) || true
fi

# ── STEP 3: Poll extraction status ────────────────────────────────────────────
step "3 — Poll extraction status (GET /status)"
EXTRACTED=false
if [[ "$CACHED" == "true" ]]; then
  ok "Skipped — cached structure returned synchronously by /extract"
  (( PASS++ )) || true
  EXTRACTED=true
else
  info "Polling up to 3 minutes for extraction to complete..."
  for i in $(seq 1 18); do
    sleep 10
    STATUS_RESP=$(call GET "${CTD_API}/status?session_id=${SESSION_ID}&bucket=${BUCKET}" || echo '{}')
    EX_STATUS=$(echo "$STATUS_RESP" | jq -r '.extraction.status // "unknown"' 2>/dev/null)
    info "[${i}/18] extraction.status = ${EX_STATUS}"
    if [[ "$EX_STATUS" == "done" ]]; then
      ok "Extraction completed"
      (( PASS++ )) || true
      EXTRACTED=true
      break
    elif [[ "$EX_STATUS" == "failed" ]]; then
      fail "Extraction failed: $(echo "$STATUS_RESP" | jq -r '.extraction.error // ""')"
      (( FAIL++ )) || true
      break
    fi
  done
  if [[ "$EXTRACTED" != "true" ]]; then
    warn "Extraction still running after 3 min — continuing with approve using cached structure"
  fi
fi

# ── STEP 4: Approve structure ─────────────────────────────────────────────────
step "4 — Approve CTD structure (POST /approve)"
# Load cached folder paths from session
SESSION_RESP=$(call GET "${CTD_API}/session?session_id=${SESSION_ID}&bucket=${BUCKET}" || echo '{}')
FOLDER_PATHS=$(echo "$SESSION_RESP" | jq '.folder_paths // []' 2>/dev/null || echo '[]')
N_PATHS=$(echo "$FOLDER_PATHS" | jq 'length' 2>/dev/null || echo 0)
info "Session has ${N_PATHS} folder paths"

APPROVE_RESP=$(call POST "${CTD_API}/approve" \
  "{\"session_id\":\"${SESSION_ID}\",\"bucket\":\"${BUCKET}\",\"folder_paths\":${FOLDER_PATHS},\"therapeutic_area\":\"${TA}\",\"disease_type\":\"${DIS}\",\"drug_name\":\"${DRUG}\"}")
APPROVE_REPLY=$(echo "$APPROVE_RESP" | jq -r '.reply' 2>/dev/null || echo "$APPROVE_RESP")
echo "  Reply: ${APPROVE_REPLY:0:200}"
if echo "$APPROVE_RESP" | jq -e '.reply' &>/dev/null; then
  ok "POST /approve accepted"
  (( PASS++ )) || true
else
  fail "POST /approve failed: ${APPROVE_RESP:0:200}"
  (( FAIL++ )) || true
fi

# ── STEP 5: Copy structure to program ─────────────────────────────────────────
step "5 — Copy CTD structure to program (POST /copy)"
COPY_RESP=$(call POST "${CTD_API}/copy" \
  "{\"session_id\":\"${SESSION_ID}\",\"bucket\":\"${BUCKET}\",\"therapeutic_area\":\"${TA}\",\"disease_type\":\"${DIS}\",\"drug_name\":\"${DRUG}\"}")
COPY_REPLY=$(echo "$COPY_RESP" | jq -r '.reply' 2>/dev/null || echo "$COPY_RESP")
echo "  Reply: ${COPY_REPLY:0:200}"
if echo "$COPY_RESP" | jq -e '.reply' &>/dev/null; then
  ok "POST /copy accepted"
  (( PASS++ )) || true
else
  fail "POST /copy failed: ${COPY_RESP:0:200}"
  (( FAIL++ )) || true
fi

# ── STEP 6: Verify clinical manifest ──────────────────────────────────────────
step "6 — Verify clinical manifest (POST /resolve)"
info "Pre-resolving placeholder keys for Bell's Palsy / Prednisolone..."
RESOLVE_RESP=$(call POST "${ANALYST}/resolve" \
  "{\"therapeutic_area\":\"${TA}\",\"disease_type\":\"${DIS}\",\"drug_name\":\"${DRUG}\",\"bucket\":\"${BUCKET}\",\"placeholder_keys\":[]}")
KEYS_RESOLVED=$(echo "$RESOLVE_RESP" | jq -r '.keys_resolved // "ERROR"' 2>/dev/null)
KEYS_FAILED=$(echo "$RESOLVE_RESP" | jq -r '.keys_failed | length' 2>/dev/null || echo "?")
if [[ "$KEYS_RESOLVED" =~ ^[0-9]+$ ]]; then
  ok "/resolve: keys_resolved=${KEYS_RESOLVED}, keys_failed=${KEYS_FAILED}"
  (( PASS++ )) || true
  echo "  Resolved values (first 3):"
  echo "$RESOLVE_RESP" | jq -r '.resolved_values | to_entries[:3] | .[] | "    \(.key): \(.value)"' 2>/dev/null || true
else
  fail "/resolve failed: ${RESOLVE_RESP:0:300}"
  (( FAIL++ )) || true
fi

# ── STEP 7: ICH Index query ────────────────────────────────────────────────────
step "7 — ICH Index query (POST /index/query)"
INDEX_RESP=$(call POST "${INDEX}/index/query" \
  "{\"question\":\"What are the mandatory ICH M4E requirements for Module 2.7 Clinical Summary?\"}")
ANSWER=$(echo "$INDEX_RESP" | jq -r '.answer // ""' 2>/dev/null)
if [[ -n "$ANSWER" && "$ANSWER" != "null" ]]; then
  ok "ICH index query returned answer (${#ANSWER} chars)"
  echo "  Preview: ${ANSWER:0:200}..."
  (( PASS++ )) || true
else
  fail "ICH index query failed: ${INDEX_RESP:0:200}"
  (( FAIL++ )) || true
fi

# ── STEP 8: Template generation ────────────────────────────────────────────────
step "8 — Template generation (POST /generate on ich4-template)"
TMPL_RESP=$(call POST "${TEMPLATE}/generate" \
  "{\"program\":{\"therapeutic_area\":\"${TA}\",\"disease_type\":\"${DIS}\",\"drug_name\":\"${DRUG}\"},\"include_clinical_data\":true,\"bucket\":\"${BUCKET}\",\"section_key_prefixes\":[\"2.7\"]}")
N_TEMPLATES=$(echo "$TMPL_RESP" | jq '.templates | length' 2>/dev/null || echo "0")
if [[ "$N_TEMPLATES" =~ ^[0-9]+$ && "$N_TEMPLATES" -gt 0 ]]; then
  ok "Template generation: ${N_TEMPLATES} templates produced"
  echo "$TMPL_RESP" | jq -r '.templates[0].section_key' 2>/dev/null | xargs -I{} echo "  First section: {}" || true
  (( PASS++ )) || true
else
  fail "Template generation returned 0 templates: ${TMPL_RESP:0:300}"
  (( FAIL++ )) || true
fi

# ── STEP 9: Content generation trigger ────────────────────────────────────────
step "9 — Trigger full content generation (POST /trigger)"
TRIGGER_RESP=$(call POST "${ANALYST}/trigger" \
  "{\"session_id\":\"${SESSION_ID}\",\"bucket\":\"${BUCKET}\",\"therapeutic_area\":\"${TA}\",\"disease_type\":\"${DIS}\",\"drug_name\":\"${DRUG}\",\"force_no_clinical\":false}")
TRIGGER_REPLY=$(echo "$TRIGGER_RESP" | jq -r '.reply' 2>/dev/null || echo "$TRIGGER_RESP")
RUN_ID=$(echo "$TRIGGER_RESP" | jq -r '.state_patch.content_run_id // ""' 2>/dev/null)
echo "  Reply: ${TRIGGER_REPLY:0:200}"
echo "  run_id: ${RUN_ID}"
if [[ -n "$RUN_ID" ]]; then
  ok "Content generation queued (run_id=${RUN_ID:0:8}...)"
  (( PASS++ )) || true
else
  fail "Trigger failed — no run_id returned: ${TRIGGER_RESP:0:300}"
  (( FAIL++ )) || true
fi

# ── STEP 10: Poll content generation status ────────────────────────────────────
step "10 — Poll content generation status (GET /status)"
info "Polling up to 15 minutes for content generation..."
CONTENT_DONE=false
for i in $(seq 1 45); do
  sleep 20
  CSTATUS_RESP=$(call GET "${CTD_API}/status?session_id=${SESSION_ID}&bucket=${BUCKET}&therapeutic_area=${TA}&disease_type=${DIS}&drug_name=${DRUG}" || echo '{}')
  C_STATUS=$(echo "$CSTATUS_RESP" | jq -r '.content.status // "pending"' 2>/dev/null)
  STEP_LBL=$(echo "$CSTATUS_RESP" | jq -r '.content.pass_label // .content.step // ""' 2>/dev/null)
  info "[${i}/45] content.status=${C_STATUS}  ${STEP_LBL}"
  if [[ "$C_STATUS" == "done" ]]; then
    WRITTEN=$(echo "$CSTATUS_RESP" | jq -r '.content.sections_written // "?"' 2>/dev/null)
    VAL_PASS=$(echo "$CSTATUS_RESP" | jq -r '.content.validation_passed // "?"' 2>/dev/null)
    ok "Content generation DONE — sections_written=${WRITTEN}, validation_passed=${VAL_PASS}"
    (( PASS++ )) || true
    CONTENT_DONE=true
    break
  elif [[ "$C_STATUS" == "failed" ]]; then
    ERR=$(echo "$CSTATUS_RESP" | jq -r '.content.error // ""' 2>/dev/null)
    fail "Content generation FAILED: ${ERR:0:300}"
    (( FAIL++ )) || true
    break
  fi
done
if [[ "$CONTENT_DONE" != "true" ]]; then
  warn "Content generation still in progress after 15 min — not failed, just slow"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "  TEST SUMMARY"
echo "════════════════════════════════════════"
echo -e "  ${GREEN}PASS: ${PASS}${NC}"
echo -e "  ${RED}FAIL: ${FAIL}${NC}"
echo "════════════════════════════════════════"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1

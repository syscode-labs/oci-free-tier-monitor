#!/usr/bin/env bash
set -euo pipefail

# Creates the OCI user, group, and policies required by oci-free-tier-monitor.
# Idempotent: safe to re-run; existing resources are left unchanged.
#
# Required env vars (or set in ~/.oci/config):
#   OCI_TENANCY_OCID        – tenancy root OCID
#   OCI_COMPARTMENT_OCID    – compartment to monitor
#
# Optional:
#   OCI_STATE_BUCKET        – if set, adds a bucket-scoped manage-objects policy
#   OCI_MONITOR_GROUP       – group name (default: oci-monitor)
#   OCI_MONITOR_USER        – user name  (default: oci-monitor)

TENANCY_OCID="${OCI_TENANCY_OCID:?OCI_TENANCY_OCID is required}"
COMPARTMENT_OCID="${OCI_COMPARTMENT_OCID:?OCI_COMPARTMENT_OCID is required}"
STATE_BUCKET="${OCI_STATE_BUCKET:-}"
GROUP="${OCI_MONITOR_GROUP:-oci-monitor}"
USER="${OCI_MONITOR_USER:-oci-monitor}"

command -v oci >/dev/null 2>&1 || { echo "oci CLI not found – install from https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm"; exit 1; }

info()    { echo "[+] $*"; }
skip()    { echo "[=] $*"; }
heading() { echo; echo "── $* ──"; }

# ── Group ─────────────────────────────────────────────────────────────────────

heading "Group"
GROUP_ID=$(oci iam group list \
  --compartment-id "$TENANCY_OCID" \
  --name "$GROUP" \
  --query 'data[0].id' \
  --raw-output 2>/dev/null || true)

if [[ -z "$GROUP_ID" || "$GROUP_ID" == "null" ]]; then
  GROUP_ID=$(oci iam group create \
    --compartment-id "$TENANCY_OCID" \
    --name "$GROUP" \
    --description "OCI Monitor group" \
    --query 'data.id' \
    --raw-output)
  info "Created group $GROUP ($GROUP_ID)"
else
  skip "Group $GROUP already exists ($GROUP_ID)"
fi

# ── User ──────────────────────────────────────────────────────────────────────

heading "User"
USER_ID=$(oci iam user list \
  --compartment-id "$TENANCY_OCID" \
  --name "$USER" \
  --query 'data[0].id' \
  --raw-output 2>/dev/null || true)

if [[ -z "$USER_ID" || "$USER_ID" == "null" ]]; then
  USER_ID=$(oci iam user create \
    --compartment-id "$TENANCY_OCID" \
    --name "$USER" \
    --description "OCI Monitor user" \
    --query 'data.id' \
    --raw-output)
  info "Created user $USER ($USER_ID)"
else
  skip "User $USER already exists ($USER_ID)"
fi

# ── Group membership ──────────────────────────────────────────────────────────

heading "Group membership"
MEMBER=$(oci iam group list-users \
  --group-id "$GROUP_ID" \
  --query "data[?id=='$USER_ID'].id | [0]" \
  --raw-output 2>/dev/null || true)

if [[ -z "$MEMBER" || "$MEMBER" == "null" ]]; then
  oci iam group add-user --group-id "$GROUP_ID" --user-id "$USER_ID" >/dev/null
  info "Added $USER to $GROUP"
else
  skip "$USER is already a member of $GROUP"
fi

# ── Policies ──────────────────────────────────────────────────────────────────

_upsert_policy() {
  local name="$1" compartment="$2" description="$3"
  shift 3
  local statements=("$@")

  local json
  json=$(printf '%s\n' "${statements[@]}" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read().splitlines()))")

  local existing
  existing=$(oci iam policy list \
    --compartment-id "$compartment" \
    --name "$name" \
    --query 'data[0].id' \
    --raw-output 2>/dev/null || true)

  if [[ -z "$existing" || "$existing" == "null" ]]; then
    oci iam policy create \
      --compartment-id "$compartment" \
      --name "$name" \
      --description "$description" \
      --statements "$json" \
      --query 'data.id' \
      --raw-output >/dev/null
    info "Created policy $name"
  else
    oci iam policy update \
      --policy-id "$existing" \
      --statements "$json" \
      --force >/dev/null
    skip "Updated policy $name (already existed)"
  fi
}

heading "Tenancy-level policy"
_upsert_policy "oci-monitor-tenancy" "$TENANCY_OCID" "OCI Monitor tenancy-level policy" \
  "Allow group $GROUP to inspect compartments in tenancy" \
  "Allow group $GROUP to read instance-family in tenancy" \
  "Allow group $GROUP to read usage-report in tenancy" \
  "Allow group $GROUP to read tenancies in tenancy" \
  "Allow group $GROUP to read objectstorage-namespaces in tenancy"

heading "Compartment-level policy"
compartment_statements=(
  "Allow group $GROUP to read all-resources in compartment id ${COMPARTMENT_OCID}"
  "Allow group $GROUP to manage public-ips in compartment id ${COMPARTMENT_OCID}"
  "Allow group $GROUP to manage volumes in compartment id ${COMPARTMENT_OCID}"
  "Allow group $GROUP to manage boot-volumes in compartment id ${COMPARTMENT_OCID}"
  "Allow group $GROUP to inspect volume-backups in compartment id ${COMPARTMENT_OCID}"
  "Allow group $GROUP to inspect boot-volume-backups in compartment id ${COMPARTMENT_OCID}"
  "Allow group $GROUP to inspect images in compartment id ${COMPARTMENT_OCID}"
  "Allow group $GROUP to read buckets in compartment id ${COMPARTMENT_OCID}"
  "Allow group $GROUP to read objects in compartment id ${COMPARTMENT_OCID}"
)

if [[ -n "$STATE_BUCKET" ]]; then
  compartment_statements+=(
    "Allow group $GROUP to manage objects in compartment id ${COMPARTMENT_OCID} where target.bucket.name = '${STATE_BUCKET}'"
  )
else
  compartment_statements+=(
    "Allow group $GROUP to manage objects in compartment id ${COMPARTMENT_OCID}"
  )
fi

_upsert_policy "oci-monitor-compartment" "$COMPARTMENT_OCID" "OCI Monitor compartment-level policy" \
  "${compartment_statements[@]}"

# ── Summary ───────────────────────────────────────────────────────────────────

echo
echo "Done. Next: attach an API key to user $USER in the OCI Console"
echo "https://cloud.oracle.com/identity/users/${USER_ID}/api-keys"

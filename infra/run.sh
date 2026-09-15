#!/usr/bin/env bash
# Run Terraform from a container.
#
# This machine has no Terraform installed, and the handoff should not require
# one, so it runs in a throwaway container with infra/ mounted. If you do have
# Terraform natively, set NATIVE=1 and this calls it directly instead.
#
#   ./run.sh init
#   ./run.sh plan
#   ./run.sh apply -auto-approve
#   ./run.sh output -json trino_roles
#
# The Keycloak admin password comes from streaming/.env - it is never written to
# a .tfvars file, and the generated demonstration passwords land in the
# gitignored infra/generated/credentials.json.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
IMAGE="${TERRAFORM_IMAGE:-hashicorp/terraform:1.9}"

if [ -f "$ROOT/streaming/.env" ]; then
  set -a && . "$ROOT/streaming/.env" && set +a
fi

if [ -z "${KEYCLOAK_ADMIN_PASSWORD:-}" ]; then
  echo "KEYCLOAK_ADMIN_PASSWORD is not set. It lives in streaming/.env." >&2
  exit 2
fi

export TF_VAR_keycloak_admin="${KEYCLOAK_ADMIN:-admin}"
export TF_VAR_keycloak_admin_password="$KEYCLOAK_ADMIN_PASSWORD"

if [ "${NATIVE:-0}" = "1" ]; then
  export TF_VAR_keycloak_url="${KEYCLOAK_URL:-http://127.0.0.1:8180}"
  cd "$HERE" && exec terraform "$@"
fi

# Git Bash rewrites anything that looks like a Unix path into a Windows one, so
# /infra arrives at Docker as C:/Program Files/Git/infra. Turn that off, and give
# the mount source as a Windows path since that is what Docker Desktop wants.
MOUNT="$HERE"
if [ -n "${MSYSTEM:-}" ]; then
  export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
  MOUNT="$(cd "$HERE" && pwd -W 2>/dev/null || echo "$HERE")"
fi

# Keycloak is published on the host loopback, which inside a container is the
# container itself - so the URL has to be rewritten, not inherited.
exec docker run --rm -i \
  -v "$MOUNT:/infra" \
  -w /infra \
  -e TF_VAR_keycloak_admin \
  -e TF_VAR_keycloak_admin_password \
  -e TF_VAR_keycloak_url="${TF_VAR_KEYCLOAK_URL_CONTAINER:-http://host.docker.internal:8180}" \
  --add-host host.docker.internal:host-gateway \
  "$IMAGE" "$@"

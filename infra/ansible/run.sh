#!/usr/bin/env bash
# Run Ansible from a container.
#
# Windows cannot be an Ansible control node, and this machine has no Ansible
# installed, so the playbook runs inside a throwaway container with the repo and
# your SSH key mounted. If you do have Ansible natively, set NATIVE=1 and this
# calls it directly instead.
#
#   ./run.sh site.yml                    # configure every VM in the inventory
#   ./run.sh site.yml --check --diff     # show what would change, change nothing
#   ./run.sh site.yml --tags prereqs     # only the OS preparation
#   ./run.sh site.yml --limit nda-node-3 # one machine
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${ANSIBLE_IMAGE:-willhallonline/ansible:2.19-alpine-3.21}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"

if [ ! -f "$HERE/inventory/hosts.yml" ]; then
  cat >&2 <<'MSG'
inventory/hosts.yml does not exist yet.

  cp inventory/hosts.yml.example inventory/hosts.yml

Then fill in each VM's name and static IP. That file is the entire handoff:
nothing else needs editing to deploy onto new machines.
MSG
  exit 2
fi

if [ ! -f "$SSH_KEY" ]; then
  echo "No SSH key at $SSH_KEY. Set SSH_KEY=/path/to/key, or create one with:" >&2
  echo "  ssh-keygen -t ed25519 -f $SSH_KEY" >&2
  echo "Then copy it to every VM:  ssh-copy-id -i $SSH_KEY.pub ubuntu@<ip>" >&2
  exit 2
fi

if [ "${NATIVE:-0}" = "1" ]; then
  cd "$HERE" && exec ansible-playbook "$@"
fi

# --network host so the playbook can reach the VMs on your LAN directly.
exec docker run --rm -it \
  --network host \
  -v "$HERE:/ansible" \
  -v "$SSH_KEY:/root/.ssh/id_rsa:ro" \
  -v "${SSH_KEY}.pub:/root/.ssh/id_rsa.pub:ro" \
  -e ANSIBLE_HOST_KEY_CHECKING=False \
  -e ANSIBLE_CONFIG=/ansible/ansible.cfg \
  -e ANSIBLE_INVENTORY=/ansible/inventory/hosts.yml \
  -w /ansible \
  "$IMAGE" \
  ansible-playbook "$@"

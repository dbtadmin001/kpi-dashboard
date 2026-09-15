# Deploying to the VM cluster

Written for whoever is at the keyboard when the VMs come online.

This machine stays the **dev environment** (Docker Compose, single node). The VMs
become the **cluster**. Nothing here changes the dev setup.

---

## Two deployment targets, and which one is current

| Target | State | Use it for |
|---|---|---|
| **One Ubuntu VM, Compose** | **Current.** Authenticated, governed, digest-pinned, gated by CI | Production today |
| **k3s cluster, `infra/k8s/`** | Manifests predate authentication and the marketplace | Not yet - see the warning below |

> **Do not run `infra/k8s/deploy.sh` on a real cluster.** Those manifests ship a
> Trino with `http-server.http.port=8080`, no `access-control.properties`, no
> group provider and no OIDC - an unauthenticated, ungoverned engine. They also
> contain no API, no identity sync, no audit collector and no marketplace. They
> need regenerating from the current architecture before they mean anything.

The Ansible below prepares VMs for **both**, so building the cluster now is not
wasted work - it is the substrate the platform moves onto once the manifests
catch up.

---

## Production: one VM

### 1. Prepare the VM

```bash
cd infra/ansible
cp inventory/hosts.yml.example inventory/hosts.yml     # add the VM under `platform:`
./run.sh site.yml --tags platform
```

That installs Docker Engine and the compose **plugin**, creates the `nda` deploy
account, creates `/var/lib/nda`, caps container log growth, opens 8443 and 8444
if ufw is active, asserts the VM has at least 16GB, and installs a
`nda-platform.service` unit so the stack returns after a reboot.

`/var/lib/nda`, not `/run/nda`: that directory holds `secrets.env`, the OIDC
client, the TLS keystore, the rendered Trino config and the audit log. `/run` is
a tmpfs on every systemd host, so all of it would vanish at the next boot.

### 2. Create the secrets, once

```bash
ssh nda@<vm> 'python3 -m delivery.secrets init /var/lib/nda'
```

Eleven generated values. It refuses to overwrite an existing file, because
regenerating orphans every password already stored in Postgres, MinIO and SQL
Server.

### 3. Deploy from GitHub Actions

Set in repository settings:

| Kind | Name | Value |
|---|---|---|
| secret | `DEPLOY_SSH_KEY` | private key for `nda@<vm>` |
| secret | `DEPLOY_HOST` | the VM's address |
| secret | `DEPLOY_KNOWN_HOSTS` | `ssh-keyscan <vm>` |
| variable | `KEYCLOAK_PUBLIC_URL` | `https://sso.your.domain` |

Then **Actions -> Deploy -> Run workflow**, give it a commit SHA, and leave
`confirm` as `dry-run` the first time: it renders and validates the topology and
stops. Re-run with `confirm: deploy` to apply.

**The deploy workflow never builds.** It can only use digests produced by the
`publish` job, which only runs after the real-engine integration gate passes. So
what reaches the VM is exactly what was tested, and a SHA that never passed CI
cannot be deployed at all.

After restarting it runs `marketplace.trino_config check` against production -
the same ten RBAC assertions CI makes. A stack that starts but does not enforce
its rules is a failed deployment, not a successful one.

---

## Scaling onto two more VMs

### What works today

```bash
# add the two VMs under k3s_compute in inventory/hosts.yml, then:
./run.sh site.yml
```

Adding a node is one line in the inventory and a re-run. That genuinely works
now - it did not before. The join token was regenerated on every run
(`lookup('password', '/dev/null')` returns a new value each time), so the second
run invented a token the cluster had never seen; existing nodes kept working and
any node added later failed to join. The token is now read back from
`/var/lib/rancher/k3s/server/token` on the running cluster.

Re-running also configures only what changed: `common`, `k8s_prereqs` and
`storage` are idempotent, and the k3s install is guarded by `creates:`.

### What each VM needs

- 4GB RAM minimum (asserted), 16GB if it also runs the platform
- A static IP, in the inventory as `ansible_host`
- 20GB free on `/srv/minio` to carry the `nda.io/storage` label (asserted)
- ufw either inactive or opened for 6443/tcp, 10250/tcp, 8472/udp, 2379:2380/tcp.
  The play now opens these when ufw is active, and says so when it is not.
  8472/udp is the one people forget - without it pods start and cannot reach each
  other across nodes.

### What is still missing before the platform runs on the cluster

The manifests. Specifically they need: the Trino access rules, group file and
OIDC configuration; the API, identity sync and audit collector; and the images
`delivery/release.py` builds, referenced by digest rather than tag. Until then
the cluster is capacity waiting for a workload, and the platform runs on the
single VM.

MinIO needs **four** storage nodes to survive losing one. With three you have a
cluster, not a fault-tolerant object store - the `verify` role says so explicitly
rather than letting you assume otherwise.

---

## What you do when you get home

### 1. Prepare each Ubuntu VM (once per VM)

Every VM needs only three things before Ansible can take over:

- Ubuntu 22.04 or 24.04, reachable on the same LAN
- a **static IP**
- your SSH public key in `~/.ssh/authorized_keys` for a sudo-capable user

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_rsa      # if you do not have a key yet
ssh-copy-id -i ~/.ssh/id_rsa.pub ubuntu@192.168.1.101
```

### 2. Fill in one file

```bash
cd infra/ansible
cp inventory/hosts.yml.example inventory/hosts.yml
```

Add each VM's **name** and **static IP**. That file is the entire handoff — no
other file needs editing to deploy onto new machines.

```yaml
k3s_control:
  hosts:
    nda-node-1:
      ansible_host: 192.168.1.101
      node_labels: { "nda.io/storage": "true" }

k3s_compute:
  hosts:
    nda-node-2: { ansible_host: 192.168.1.102, node_labels: { "nda.io/storage": "true" } }
    nda-node-3: { ansible_host: 192.168.1.103, node_labels: { "nda.io/storage": "true" } }
    nda-node-4: { ansible_host: 192.168.1.104, node_labels: { "nda.io/storage": "true" } }
```

Two rules the playbook enforces for you:

- **One or three control hosts, never two.** Embedded etcd needs an odd number
  to hold quorum; two control nodes are *less* available than one. The play
  refuses to run otherwise.
- **Four storage nodes** for a fault-tolerant object store. Fewer works, but
  MinIO cannot then survive losing a node, and the play warns you.

### 3. Build the cluster

```bash
./run.sh site.yml --check      # dry run: shows every change, makes none
./run.sh site.yml              # for real, ~10 minutes
```

Ansible runs in a container, so nothing needs installing on Windows. It sets
hostnames and `/etc/hosts`, disables swap, applies the kernel settings
Kubernetes needs, raises file limits, prepares data directories, installs k3s,
joins the workers, labels the nodes, and waits until every node reports `Ready`.

It is idempotent. **Adding a VM later is: add a line to the inventory, run it
again.** Existing nodes are left alone.

### 4. Deploy the platform

```bash
export KUBECONFIG=$PWD/kubeconfig     # written by step 3
cd ../k8s
./deploy.sh --dry-run                 # validate first
./deploy.sh
```

Secrets come from `streaming/.env`, so credentials live in exactly one place and
never appear in a manifest. The OPA policy is taken from the Terraform output in
`infra/generated/` — run `terraform apply` in `infra/` first if you have not.

---

## How the workloads are placed

The two tiers are scheduled deliberately differently, and the difference matters.

| Tier | Services | Placement | Why |
|---|---|---|---|
| **Storage / log** | MinIO (4), Kafka (3) | **Hard** anti-affinity — at most one pod per node | Two members on one node means losing that node loses two members, which is exactly the failure the redundancy is sized to survive |
| **Compute** | Trino, Flink, Spark workers | **Even spread**, more pods than nodes allowed | Capacity should grow past node count; `kubectl scale` is the right lever |
| **Source** | SQL Server (1) | Single, node-local volume | It is the system of record. A second replica is a second truth |

Every stateful service has a **PodDisruptionBudget**, so a node drain or rolling
upgrade can never take enough members at once to lose quorum.

### Adding capacity

```bash
kubectl -n nda-platform scale deploy/trino-worker      --replicas=6
kubectl -n nda-platform scale deploy/flink-taskmanager --replicas=6
kubectl -n nda-platform scale deploy/spark-worker      --replicas=6
```

These **never touch MinIO, Kafka or SQL Server**. Compute scales freely; the
stateful tier does not move.

### Scaling storage is different

MinIO's replica count defines its erasure set. Changing it on a live cluster
reshapes that set — it is a **migration, not a rolling update**. Scale in
multiples of four and plan it deliberately.

---

## Checking it worked

```bash
kubectl get nodes -o wide
kubectl get pods -A -o wide --sort-by=.spec.nodeName

# Each MinIO and Kafka pod should be on a different node:
kubectl -n nda-platform get pods -l app=minio -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName
kubectl -n nda-platform get pods -l app=kafka -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName
```

### Prove fault tolerance

```bash
kubectl drain nda-node-3 --ignore-daemonsets --delete-emptydir-data
kubectl get pods -A -o wide          # workloads reschedule; PDBs hold quorum
kubectl uncordon nda-node-3
```

---

## What is verified and what is not

**Verified on this machine, without any VMs:**

- All 33 Kubernetes resources validate against the v1.31 schema (`kubeconform -strict`)
- The playbook passes `--syntax-check` and resolves a 1-control + 3-compute inventory
- The quorum guardrail refuses a 2-control-node inventory with a clear message
- Both shell scripts pass `bash -n`

**Not yet exercised, because it needs real machines:**

- k3s install and join over SSH
- MinIO forming a distributed erasure set across four nodes
- Actual failover behaviour under a node drain

Expect to iterate on the first real run. The `--check` flag and `--dry-run`
exist so the first thing you do is look rather than change.

---

## Known gaps to close after the first successful deploy

- **Single control plane** unless you list three hosts. Fine for a lab, not for
  anything that matters.
- **`start-dev` Keycloak** uses an embedded H2 store; move it to Postgres before
  this is anything but a demo.
- **No Ingress.** k3s ships with Traefik disabled here; reach services with
  `kubectl port-forward` until an ingress and DNS are agreed.
- **No backups.** MinIO holds the lakehouse and nothing copies it elsewhere.
- **The Flink job is still submitted by hand** — the same gap as on the dev
  machine. Note this is a *supervision* gap, not a scheduling one: a streaming
  job never completes, so there is nothing to put on a cron. What is missing is
  a control loop that notices the job is gone and restores it from its last
  checkpoint. That is the Flink Kubernetes Operator's job, not Airflow's and not
  a CronJob's. Airflow still owns everything genuinely scheduled around it —
  snapshot expiry, compaction, catalog re-ingestion, and taking a savepoint
  before a version upgrade.

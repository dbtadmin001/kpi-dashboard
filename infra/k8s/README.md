# Kubernetes manifests

**Generated. Do not edit `generated.yaml` by hand.**

```bash
python -m delivery.kubernetes render --nodes 3 -o infra/k8s/generated.yaml
```

They come from the same `images.lock.json` / `images.built.json` as the Compose
topology and the same `marketplace/` declarations, so the two deployment targets
cannot disagree about what the platform is.

The previous hand-written manifests were deleted. They had drifted into a
different architecture: a Trino on plain HTTP with no access control, no group
provider and no OIDC, and no API, audit collector or identity sync anywhere.
Applying them would have replaced the governed platform with an ungoverned one,
and the only thing stopping that was somebody remembering not to.

## Before applying

```bash
kubectl create secret generic nda-secrets -n nda-platform --from-env-file=<runtime>/secrets.env
kubectl create secret generic nda-secrets -n nda-pipeline --from-env-file=<runtime>/secrets.env
kubectl create configmap opa-policy -n nda-pipeline   --from-file=authz.rego=infra/generated/authz.rego   --from-file=data.json=infra/generated/data.json
```

Label the nodes that carry an object-store disk - the Ansible `verify` role does
this from the inventory:

```bash
kubectl label node <node> nda.io/storage=true
```

## Validating a change

```bash
python -m delivery.validate      # renders, checks volumes and YAML round-trip
```

For the real thing, against the same k3s version Ansible installs:

```bash
docker run -d --privileged --name k3s-check   -e K3S_KUBECONFIG_OUTPUT=/output/kubeconfig.yaml -e K3S_KUBECONFIG_MODE=666   -v "$PWD/.k3s:/output" -p 16443:6443 rancher/k3s:v1.31.5-k3s1   server --disable traefik --disable servicelb --tls-san 127.0.0.1
sed -i 's#:6443#:16443#' .k3s/kubeconfig.yaml
KUBECONFIG=.k3s/kubeconfig.yaml kubectl create ns nda-platform
KUBECONFIG=.k3s/kubeconfig.yaml kubectl create ns nda-pipeline
KUBECONFIG=.k3s/kubeconfig.yaml kubectl apply --dry-run=server --validate=strict -f generated.yaml
docker rm -f k3s-check
```

That is how the two bugs in the first generated version were found: a
`volumeMount` naming a volume the pod never declared, and `ACCEPT_EULA: Y`
emitted bare - PyYAML does not think a lone `Y` is a boolean, Go's YAML parser
does, so Kubernetes received `true` and rejected the StatefulSet.

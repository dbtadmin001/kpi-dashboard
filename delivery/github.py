"""Configure the repository for deployment, without clicking through settings.

There is a persistent fear that CI means copying an env file into a web form.
It does not, and the shape of what GitHub actually needs is worth stating:

  CI (build, test, publish)   nothing. GITHUB_TOKEN is issued per run.
  Deploy                      three secrets and one variable, set once.
  The platform's own secrets  never leave the VM. `delivery.secrets init`
                              generates them there, and the deploy workflow
                              only checks that the file exists.

So the whole manual surface is one SSH key and an address. This sets them, and
generates the key if there is not one already.

    python -m delivery.github check
    python -m delivery.github setup --host nda-app-1.internal \\
                                    --keycloak-url https://sso.nda.example
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = ROOT / "infra" / "generated" / "deploy_key"

# (name, kind, why). `vars` are visible in logs; `secrets` are not.
NEEDED = [
    ("DEPLOY_SSH_KEY", "secret", "private key for the nda account on the VM"),
    ("DEPLOY_HOST", "secret", "the VM's address"),
    ("DEPLOY_KNOWN_HOSTS", "secret", "ssh-keyscan output, so the host is verified"),
    ("KEYCLOAK_PUBLIC_URL", "variable", "the browser-facing Keycloak URL"),
]


def _gh(*args, **kwargs):
    kwargs.setdefault("check", True)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    try:
        return subprocess.run(["gh", *args], **kwargs)
    except FileNotFoundError:
        raise SystemExit("The GitHub CLI is not installed: https://cli.github.com") from None


def _require_auth():
    result = _gh("auth", "status", check=False)
    if result.returncode != 0:
        raise SystemExit(
            "Not signed in to GitHub." + os.linesep +
            "  gh auth login          # as the account that can WRITE to the repo" + os.linesep +
            "  gh auth setup-git")


def repository(explicit=None):
    if explicit:
        return explicit
    remote = subprocess.run(["git", "remote", "get-url", "origin"],
                            capture_output=True, text=True, cwd=ROOT).stdout.strip()
    if "github.com" not in remote:
        raise SystemExit(f"origin is not a GitHub remote: {remote or '(unset)'}")
    return remote.split("github.com")[1].lstrip(":/").removesuffix(".git")


def check(repo=None):
    """What is set and what is missing. No value is ever printed."""
    _require_auth()
    repo = repository(repo)
    secrets = {s["name"] for s in json.loads(
        _gh("secret", "list", "--repo", repo, "--json", "name").stdout or "[]")}
    variables = {v["name"] for v in json.loads(
        _gh("variable", "list", "--repo", repo, "--json", "name").stdout or "[]")}
    print(f"  {repo}{os.linesep}")
    missing = []
    for name, kind, why in NEEDED:
        present = name in (secrets if kind == "secret" else variables)
        missing += [] if present else [name]
        print(f"  [{'ok' if present else '  '}] {name:22} {kind:8} {why}")
    print()
    if missing:
        print(f"{len(missing)} missing. Set them all with:")
        print("  python -m delivery.github setup --host <vm> --keycloak-url <url>")
        return 1
    print("Deployment is configured. CI needs nothing beyond this.")
    return 0


def ensure_key():
    """A deploy key dedicated to this, not somebody's personal key."""
    if KEY.exists():
        print(f"  using the existing deploy key at {KEY}")
        return KEY
    KEY.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "nda-deploy",
                    "-f", str(KEY)], check=True, capture_output=True)
    print(f"  generated a new deploy key at {KEY}")
    return KEY


def known_hosts(host):
    result = subprocess.run(["ssh-keyscan", "-H", host],
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or not result.stdout.strip():
        raise SystemExit(
            f"ssh-keyscan found no host key for {host}."
            + os.linesep + "  Is the VM up and reachable from here?"
            + os.linesep + "  Without this, the deploy would have to skip host verification,"
            + os.linesep + "  which is how you end up trusting whatever answers on that address.")
    return result.stdout


def setup(host, keycloak_url, repo=None):
    _require_auth()
    repo = repository(repo)
    key = ensure_key()
    print(f"  collecting the host key for {host}")
    hosts = known_hosts(host)

    values = {
        "DEPLOY_SSH_KEY": key.read_text(encoding="utf-8"),
        "DEPLOY_HOST": host,
        "DEPLOY_KNOWN_HOSTS": hosts,
    }
    for name, kind, _ in NEEDED:
        if kind == "secret":
            _gh("secret", "set", name, "--repo", repo, "--body", values[name])
        else:
            _gh("variable", "set", name, "--repo", repo, "--body", keycloak_url)
        print(f"  set {kind} {name}")

    public = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    print(f"{os.linesep}One thing left, on the VM - authorise this key:")
    print(f"{os.linesep}  ssh-copy-id -f -i {key}.pub nda@{host}{os.linesep}")
    print("or append it to /home/nda/.ssh/authorized_keys:")
    print(f"{os.linesep}  {public}{os.linesep}")
    print("Then: Actions -> Deploy -> Run workflow (leave confirm as dry-run first).")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Configure the repo for deployment")
    parser.add_argument("action", choices=["check", "setup"])
    parser.add_argument("--host", help="The production VM's address")
    parser.add_argument("--keycloak-url", help="Public HTTPS URL of Keycloak")
    parser.add_argument("--repo", default=None, help="owner/name (default: from origin)")
    args = parser.parse_args()
    if args.action == "check":
        return check(args.repo)
    if not args.host or not args.keycloak_url:
        raise SystemExit("setup needs --host and --keycloak-url")
    return setup(args.host, args.keycloak_url, args.repo)


if __name__ == "__main__":
    sys.exit(main())

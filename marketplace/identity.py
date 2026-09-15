"""Keycloak is the directory. Trino reads it; nothing restates it.

Access here is granted to *groups*, never to people. A Keycloak group carries its
marketplace role as the `trino_role` attribute, Terraform puts it there, and this
module resolves group membership into the file Trino's group provider reads. An
administrator adds a joiner in the Keycloak console and assigns a group - no code
change, no redeploy, no per-user grant anywhere.

    Keycloak group          trino_role        Trino access rules (from products.py)
    ma-analysts       -->   analyst      -->  marketplace + nda_gold, entity_id masked
    data-engineering  -->   data_engineer -->  everything, read-write
    public            -->   business_user -->  4 of 5 certified products

Two design points worth knowing:

*   **Subgroups inherit.** A child group with no `trino_role` of its own takes its
    parent's, so `ma-analysts / senior` can exist for dashboard purposes without
    anyone having to remember to re-state its warehouse role.

*   **Failure is stale, not open.** If Keycloak cannot be reached, this refuses to
    write rather than falling back to a guess. Trino keeps serving the last file
    it read, so a directory outage freezes membership instead of either dropping
    everyone's access or inventing some. Only --offline overrides that, loudly.

    python -m marketplace.identity show          # who Keycloak says is in what
    python -m marketplace.identity sync          # render groups.txt + rules.json
    python -m marketplace.identity watch         # keep them following Keycloak
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .products import ROLES, SEED_MEMBERS, sandbox_schema

REALM = os.environ.get("KEYCLOAK_REALM", "nda")
ROLE_ATTRIBUTE = "trino_role"
PAGE = 200

# Engine identities, not people. marketplace_owner owns the certified views and
# nda_dashboard owns the nda_gold.all_* views those read, so if either loses the
# administrator role every view in the chain fails its DEFINER check. They are in
# Keycloak too - that is where an auditor looks - but they are also pinned here so
# that a directory outage can never take the serving layer down with it.
PLATFORM_IDENTITIES = {"administrator": ("marketplace_owner", "nda_dashboard", "admin")}


class DirectoryUnavailable(RuntimeError):
    """Keycloak could not be reached or refused us. Never resolved by guessing."""


# --------------------------------------------------------------------------
# Keycloak admin API
# --------------------------------------------------------------------------
def _base():
    return os.environ.get("KEYCLOAK_URL", "http://127.0.0.1:8180").rstrip("/")


def token():
    """A short-lived admin token. admin-cli is a public client, so no secret."""
    try:
        username = os.environ["KEYCLOAK_ADMIN"]
        password = os.environ["KEYCLOAK_ADMIN_PASSWORD"]
    except KeyError as missing:
        raise DirectoryUnavailable(
            f"{missing} is not set. Load the environment first:"
            f"{chr(10)}  set -a && . ./streaming/.env && set +a") from None
    body = urllib.parse.urlencode({
        "grant_type": "password", "client_id": "admin-cli",
        "username": username, "password": password}).encode()
    url = f"{_base()}/realms/master/protocol/openid-connect/token"
    try:
        with urllib.request.urlopen(url, body, timeout=15) as response:
            return json.load(response)["access_token"]
    except (urllib.error.URLError, OSError, KeyError) as error:
        raise DirectoryUnavailable(f"Cannot authenticate to Keycloak at {_base()}: {error}") from None


def api(path, bearer):
    url = f"{_base()}/admin/realms/{REALM}{path}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise DirectoryUnavailable(f"Keycloak {error.code} on {path}") from None
    except (urllib.error.URLError, OSError) as error:
        raise DirectoryUnavailable(f"Keycloak unreachable on {path}: {error}") from None


def _attribute(group, name):
    """Keycloak returns every attribute as a list, even single-valued ones."""
    values = (group.get("attributes") or {}).get(name) or []
    return values[0].strip() if values and values[0] else None


def _children(group, bearer):
    """Subgroups, whichever way this Keycloak version chooses to report them."""
    nested = group.get("subGroups") or []
    if not nested and group.get("subGroupCount"):
        nested = api(f"/groups/{group['id']}/children?max={PAGE}", bearer)
    return nested


def _members(group_id, bearer):
    people, first = [], 0
    while True:
        page = api(f"/groups/{group_id}/members?first={first}&max={PAGE}", bearer)
        people.extend(page)
        if len(page) < PAGE:
            return people
        first += PAGE


def directory(bearer=None):
    """Every group in the realm with a marketplace role, and who is in it.

    Returns [{name, path, role, inherited, members:[{username, enabled}]}].
    """
    bearer = bearer or token()
    found = []

    def walk(group, inherited_role):
        detail = api(f"/groups/{group['id']}", bearer)
        role = _attribute(detail, ROLE_ATTRIBUTE) or inherited_role
        if role:
            members = [{"username": m["username"], "enabled": m.get("enabled", True)}
                       for m in _members(group["id"], bearer)]
            found.append({"name": detail["name"], "path": detail.get("path", "/" + detail["name"]),
                          "role": role, "inherited": _attribute(detail, ROLE_ATTRIBUTE) is None,
                          "members": sorted(members, key=lambda m: m["username"])})
        for child in _children(detail, bearer):
            walk(child, role)

    for top in api(f"/groups?max={PAGE}", bearer):
        walk(top, None)
    return found


# --------------------------------------------------------------------------
# Resolution into Trino's group file
# --------------------------------------------------------------------------
def role_members(offline=False, groups=None):
    """{role: [usernames]}, resolved from Keycloak group membership.

    A user in two groups holds both roles; Trino's rules are first-match, so the
    more permissive rule wins - which is what "an analyst who is also an engineer"
    should mean. Disabled accounts are dropped: a suspended user keeps their
    identity but loses their access, without anyone editing a policy.
    """
    resolved = {role: set(users) for role, users in PLATFORM_IDENTITIES.items()}
    if offline:
        for role, users in SEED_MEMBERS.items():
            resolved.setdefault(role, set()).update(users)
        return {role: sorted(users) for role, users in resolved.items() if users}

    unknown = {}
    groups = directory() if groups is None else groups
    if not groups:
        # An empty result means nobody carries a role, which is a misconfigured
        # realm rather than an organisation with no staff. Treating it as real
        # would quietly revoke everyone, so refuse instead.
        raise DirectoryUnavailable(
            f"No group in realm {REALM} carries a {ROLE_ATTRIBUTE} attribute."
            f"{chr(10)}  Apply the Terraform in infra/ first:  ./infra/run.sh apply")
    for group in groups:
        if group["role"] not in ROLES:
            unknown[group["name"]] = group["role"]
            continue
        resolved.setdefault(group["role"], set()).update(
            member["username"] for member in group["members"] if member["enabled"])
    if unknown:
        # A typo in a group attribute must not silently mean "no access": say so.
        detail = ", ".join(f"{name}={role!r}" for name, role in unknown.items())
        raise DirectoryUnavailable(
            f"Keycloak group(s) carry a {ROLE_ATTRIBUTE} that is not a known role: {detail}."
            f"{chr(10)}  Known roles: {', '.join(ROLES)}")
    return {role: sorted(users) for role, users in resolved.items() if users}


def governed_users(members=None):
    """Every principal the directory knows, for deriving personal sandboxes.

    A sandbox is not an access grant - it is the workspace that falls out of
    holding any role at all - so this is derived from group membership rather
    than maintained as a list of people.
    """
    members = members or role_members()
    everyone = {user for users in members.values() for user in users}
    return sorted(user for user in everyone if _sandboxable(user))


def _sandboxable(user):
    try:
        sandbox_schema(user)
        return True
    except ValueError:
        return False


def group_file(members):
    """Trino's file group provider format: one `role:user,user` line per role."""
    return "".join(f"{role}:{','.join(users)}{chr(10)}"
                   for role, users in sorted(members.items()))


def provision(users, quiet=False):
    """Create the schema behind each user's sandbox rule.

    An access rule granting ownership of `sandbox_alice_nakato` does not conjure
    the schema; without this, a new joiner's first CREATE VIEW fails with "Schema
    does not exist" and looks like a permissions problem. Run as the marketplace
    owner, who may create any schema, so a brand-new user does not have to wait
    for the 30-second rule refresh before their sandbox works.

    Never drops anything - that is retention's job, and it needs its own consent.
    """
    from .products import sandbox_schema

    try:
        from .auth import connect_any
        conn = connect_any("marketplace_owner", catalog="iceberg")
    except Exception as error:                      # noqa: BLE001 - reported, not raised
        if not quiet:
            print(f"  (skipped sandbox provisioning: {str(error).splitlines()[0][:90]})")
        return []
    created = []
    try:
        cursor = conn.cursor()
        cursor.execute("SHOW SCHEMAS FROM iceberg")
        existing = {row[0] for row in cursor.fetchall()}
        for user in users:
            schema = sandbox_schema(user)
            if schema in existing:
                continue
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS iceberg.{schema}")
            cursor.fetchall()
            created.append(schema)
        cursor.close()
    finally:
        conn.close()
    if created and not quiet:
        print(f"  provisioned {len(created)} sandbox schema(s): {', '.join(created)}")
    return created


def sync(offline=False, quiet=False, provision_sandboxes=True):
    """Render groups.txt and rules.json from the directory. Writes only on success.

    Trino re-reads both files every 30 seconds, so this never restarts anything.
    """
    from .build import OUT, access_rules

    members = role_members(offline=offline)
    users = governed_users(members)
    root = OUT / "trino"
    root.mkdir(parents=True, exist_ok=True)
    (root / "groups.txt").write_text(group_file(members), encoding="utf-8")
    # Impersonation must be resolved here too, not only in trino_config: `identity
    # sync` is the documented way to apply a membership change, so if it wrote
    # rules without impersonators it would silently revoke the grant every time
    # anyone added a user.
    from .trino_config import impersonators
    (root / "rules.json").write_text(
        json.dumps(access_rules(users=users, impersonators=impersonators(members)),
                   indent=2) + chr(10), encoding="utf-8")
    if not quiet:
        source = "seed list (OFFLINE)" if offline else f"Keycloak realm {REALM}"
        print(f"Synced from {source}: {sum(len(u) for u in members.values())} memberships "
              f"across {len(members)} roles, {len(users)} sandboxes")
        print(f"  {root / 'groups.txt'}  (Trino reloads within 30s)")
    if provision_sandboxes:
        provision(users, quiet=quiet)
    return members


def show(offline=False):
    if offline:
        print(f"OFFLINE - showing the seed list, not the directory{chr(10)}")
        for role, users in sorted(SEED_MEMBERS.items()):
            print(f"  {role:16} {', '.join(users)}")
        return
    groups = directory()
    members = role_members(groups=groups)   # resolve before printing, so a bad
                                            # trino_role is reported, not half-shown
    print(f"Keycloak realm {REALM} at {_base()}{chr(10)}")
    print(f"  {'group':20} {'-> role':18} members")
    for group in sorted(groups, key=lambda g: g["path"]):
        names = [m["username"] + ("" if m["enabled"] else " (disabled)") for m in group["members"]]
        marker = " (inherited)" if group["inherited"] else ""
        print(f"  {group['path'][1:]:20} {'-> ' + group['role'] + marker:18} "
              f"{', '.join(names) if names else '-'}")
    print()
    for role, users in sorted(members.items()):
        pinned = set(PLATFORM_IDENTITIES.get(role, ()))
        rendered = [u + (" *" if u in pinned else "") for u in users]
        print(f"  {role:16} {', '.join(rendered)}")
    print(f"{chr(10)}  * pinned platform identity, present regardless of the directory")


def watch(interval=60, offline=False):
    """Follow the directory. Survives a Keycloak restart without losing membership."""
    print(f"Following Keycloak every {interval}s. Ctrl-C to stop.")
    previous = None
    while True:
        try:
            members = sync(offline=offline, quiet=True)
            if members != previous:
                changed = "initial" if previous is None else "changed"
                print(f"  [{time.strftime('%H:%M:%S')}] membership {changed}: " +
                      "; ".join(f"{r}={len(u)}" for r, u in sorted(members.items())), flush=True)
                previous = members
        except DirectoryUnavailable as error:
            # Deliberately not fatal and deliberately not a fallback: Trino keeps
            # serving the last file it read until the directory comes back.
            print(f"  [{time.strftime('%H:%M:%S')}] {error}; keeping the last known "
                  f"membership in place", flush=True)
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Resolve Trino access from Keycloak groups")
    parser.add_argument("action", choices=["show", "sync", "watch", "provision"])
    parser.add_argument("--offline", action="store_true",
                        help="Use the seed list instead of Keycloak. Bootstrap only.")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between polls when watching")
    args = parser.parse_args()
    try:
        if args.action == "show":
            show(args.offline)
        elif args.action == "sync":
            sync(args.offline)
        elif args.action == "provision":
            created = provision(governed_users(role_members(offline=args.offline)))
            print(f"{len(created)} sandbox schema(s) created" if created
                  else "Every governed user already has a sandbox")
        else:
            watch(args.interval, args.offline)
    except DirectoryUnavailable as error:
        raise SystemExit(
            f"{error}{chr(10)}{chr(10)}"
            f"Nothing was written - Trino keeps the membership it already has."
            f"{chr(10)}Use --offline only to bootstrap before Keycloak exists.")


if __name__ == "__main__":
    main()

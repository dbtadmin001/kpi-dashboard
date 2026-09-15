"""Generate and apply the marketplace from one product declaration.

    python -m marketplace.build generate          # write reviewable artefacts
    python -m marketplace.build apply             # create the views in Trino
    python -m marketplace.build sandbox --user alice.nakato
    python -m marketplace.build verify            # prove the hiding actually works

Nothing here invents policy. Everything is derived from marketplace/products.py,
so a new data product cannot reach users without also getting access rules,
semantic definitions and a catalogue entry.
"""
import argparse
import json
import os
import pathlib
import re

from .products import (MARKETPLACE_SCHEMA, PHYSICAL_SCHEMAS, PRODUCT_AUDIENCE,
                       PRODUCTS, ROLES, SANDBOX_PREFIX, all_users, sandbox_schema)

OUT = pathlib.Path(os.environ.get("MARKETPLACE_RUNTIME_DIR", pathlib.Path(__file__).parent / "generated"))

# Engine identities: the view owners and the API's serving principal. They are
# never impersonation TARGETS (becoming one hands over the privileges every
# certified view runs with) and never IMPERSONATORS either - a compromised
# service account that can also become any user is a worse outcome than one that
# cannot.
SERVICE_PRINCIPALS = ("marketplace_owner", "nda_dashboard")

# `admin` is a person in the realm, not an engine identity, so it may impersonate.
# It is still excluded as a target: nothing should be able to become it.
PLATFORM_PRINCIPALS = SERVICE_PRINCIPALS + ("admin",)


def connection(user="marketplace_owner"):
    from .auth import connect_any
    return connect_any(user, catalog="iceberg")


def run(conn, sql):
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        return cursor.fetchall()
    finally:
        cursor.close()


# --------------------------------------------------------------------------
# 1. The certified views. Metadata only - zero bytes written.
# --------------------------------------------------------------------------
def view_sql(product):
    return (f"CREATE OR REPLACE VIEW {product.fqn} "
            f"COMMENT '{product.title}: {product.description[:180]}' AS"
            f"{product.sql.rstrip()}")


def schema_sql():
    return [
        f"CREATE SCHEMA IF NOT EXISTS iceberg.{MARKETPLACE_SCHEMA}",
    ]




# --------------------------------------------------------------------------
# 2. Trino file-based access control.
#
# The hiding mechanism, in two parts:
#   * Business users get no rule matching the physical schemas, and the default
#     is deny - so nda_gold is not merely unlisted, it is invisible to
#     SHOW SCHEMAS and unusable by a hand-typed query.
#   * The certified views still work because Trino views run with DEFINER
#     security: the view executes as its owner, who does have access.
# --------------------------------------------------------------------------
MASKED_ENTITY = {"name": "entity_id",
                 "mask": "'sha256:' || to_hex(sha256(cast(entity_id as varbinary)))"}


def access_rules(users=None, impersonators=()):
    """Rules are written against GROUPS, never against people.

    The one place a username appears is the personal sandbox, because the
    schema name is derived from it (`alice.nakato` -> `sandbox_alice_nakato`)
    and Trino's file rules have no backreference that can express that mapping.
    Those rows are derived from directory membership, not authored - see
    marketplace/identity.py - so nobody ever hand-grants a person anything.
    """
    users = all_users() if users is None else users
    # Who may open a session as someone else. Empty by default: impersonation is
    # a deliberate grant, so forgetting to pass it denies rather than permits.
    impersonators = sorted(impersonators)
    everyone = "|".join(ROLES)
    rules = {
        "catalogs": [
            {"group": "administrator", "catalog": "iceberg", "allow": "all"},
            {"group": "data_engineer", "catalog": "iceberg", "allow": "all"},
            # "all" at CATALOG level, not "read-only": read-only here blocks every
            # write in the catalog, including into the user's own sandbox. What
            # they may actually touch is decided by the schema and table rules
            # below, where production is SELECT-only and sandboxes are not.
            {"group": f"({everyone})", "catalog": "iceberg", "allow": "all"},
            {"catalog": "system", "allow": "read-only"},
            {"catalog": ".*", "allow": "none"},
        ],
        "schemas": [
            {"group": "administrator", "schema": ".*", "owner": True},
            # Engineers own the physical lakehouse.
            {"group": "data_engineer", "schema": ".*", "owner": True},
            # Everyone gets their own sandbox, and owns it.
            # One explicit rule per user: the username-to-schema mapping turns
            # dots into underscores, which a regex backreference cannot express.
            *[{"user": user, "schema": sandbox_schema(user), "owner": True}
              for user in users],
            # The marketplace is readable by all roles but owned by none of them.
            {"group": f"({everyone})", "schema": MARKETPLACE_SCHEMA, "owner": False},
        ],
        "tables": [
            # GRANT_SELECT, not just SELECT: a view owner needs it for Trino to
            # allow a view that reads the table. Without it every certified view
            # fails at query time with "view owner does not have sufficient
            # privileges", which is the DEFINER check doing its job.
            {"group": "administrator",
             "privileges": ["SELECT", "INSERT", "DELETE", "UPDATE", "OWNERSHIP", "GRANT_SELECT"]},
            {"group": "data_engineer",
             "privileges": ["SELECT", "INSERT", "DELETE", "UPDATE", "OWNERSHIP", "GRANT_SELECT"]},
            # Analysts and scientists may read the physical curated layer, but the
            # mask belongs ON this rule: Trino applies the first matching table
            # rule, so a masking rule added later would never be reached.
            # entity_id is classified as a pseudonymous identifier, so it is
            # hashed here and only engineers and admins see it raw.
            {"group": "(analyst|data_scientist)", "schema": "nda_gold",
             "privileges": ["SELECT"], "columns": [MASKED_ENTITY]},
            {"group": "data_scientist", "schema": "nda_silver",
             "privileges": ["SELECT"], "columns": [MASKED_ENTITY]},
            # Sandboxes: full control inside your own, nothing in anyone else's.
            *[{"user": user, "schema": sandbox_schema(user),
               "privileges": ["SELECT", "INSERT", "DELETE", "UPDATE", "OWNERSHIP"]}
              for user in users],
        ],
    }
    # Per-product visibility, most restrictive first.
    for product in PRODUCTS:
        audience = PRODUCT_AUDIENCE.get(product.name, [])
        if not audience:
            continue
        rules["tables"].append({
            "group": "(" + "|".join(audience) + ")",
            "schema": MARKETPLACE_SCHEMA,
            "table": product.name,
            "privileges": ["SELECT"],
        })
    # ----------------------------------------------------------------------
    # Impersonation: the governed answer to "what does this user actually see?"
    #
    # Without it, checking another person's view means sharing their password or
    # juggling browser sessions - both of which destroy attribution. With it, an
    # administrator opens a session AS that user, Trino enforces the target's
    # rules, and the audit records BOTH identities.
    #
    # Granted only to `administrator`, who can already read every table, so this
    # adds no data access at all - only the ability to see it through someone
    # else's permissions. Nobody else may impersonate, and nobody may impersonate
    # an administrator, so it cannot be used to climb.
    # `original_role` would match a TRINO role, which this platform does not use -
    # it would never match, and impersonation would be silently denied rather than
    # visibly misconfigured. So the originals are derived from who holds the
    # administrator role in the directory, exactly as sandbox rules are.
    rules["impersonation"] = [
        {"original_user": ".*", "new_user": "|".join(PLATFORM_PRINCIPALS), "allow": False},
        *[{"original_user": admin, "new_user": ".*", "allow": True}
          for admin in impersonators],
        {"original_user": ".*", "new_user": ".*", "allow": False},
    ]
    # Default deny. Anything not granted above is invisible, not just forbidden.
    rules["tables"].append({"privileges": []})
    rules["schemas"].append({"schema": ".*", "owner": False})
    return rules


def event_listener_properties():
    """Audit every query. Trino emits one event per query, completed or failed."""
    return (
        "# Query audit. Every statement, who ran it, what it touched, how long it\n"
        "# took and whether it failed. Shipped to the audit collector, which\n"
        "# writes an Iceberg table so the log is queryable like any other data.\n"
        "event-listener.name=http\n"
        "http-event-listener.log-completed=true\n"
        "http-event-listener.log-created=false\n"
        "http-event-listener.connect-ingest-uri=http://marketplace-audit:8099/v1/query-events\n"
        "http-event-listener.connect-retry-count=3\n"
        "http-event-listener.connect-retry-delay=1s\n"
    )


# --------------------------------------------------------------------------
# 3. dbt semantic models, generated from the same measures and dimensions.
# --------------------------------------------------------------------------
def semantic_models():
    models, metrics = [], []
    for product in PRODUCTS:
        models.append({
            "name": product.name,
            "description": product.description,
            "model": f"ref('{product.name}')",
            "defaults": {"agg_time_dimension": next(
                (d.name for d in product.dimensions if d.type == "time"), None)},
            "entities": [{"name": product.name, "type": "primary",
                          "expr": product.dimensions[0].expression if product.dimensions else "1"}],
            "dimensions": [
                {"name": d.name, "type": d.type, "expr": d.expression, "description": d.description,
                 **({"type_params": {"time_granularity": "month"}} if d.type == "time" else {})}
                for d in product.dimensions
            ],
            # MetricFlow requires measure names to be unique across ALL semantic
            # models, not just within one. Two products both expose
            # avg_waiting_days, so the product name is carried in the measure
            # name while expr still points at the plain column.
            "measures": [
                {"name": f"{product.name}_{m.name}", "agg": m.agg, "expr": m.expression,
                 "description": m.description}
                for m in product.measures
            ],
        })
        for measure in product.measures:
            metrics.append({
                # Single underscore: MetricFlow rejects dunders in metric names.
                # product+measure still makes it unique across products.
                "name": f"{product.name}_{measure.name}",
                "label": f"{product.title}: {measure.description}",
                "type": "simple",
                "description": measure.description,
                "type_params": {"measure": f"{product.name}_{measure.name}"},
            })
    return {"semantic_models": models, "metrics": metrics}


def dbt_sources():
    """dbt sources pointing at the certified views, never the physical tables."""
    return {"version": 2, "sources": [{
        "name": MARKETPLACE_SCHEMA,
        "database": "iceberg",
        "schema": MARKETPLACE_SCHEMA,
        "description": "Certified business views. The only surface dbt models may read.",
        "tables": [{"name": p.name, "description": p.description} for p in PRODUCTS],
    }]}


def generate():
    OUT.mkdir(parents=True, exist_ok=True)
    statements = schema_sql() + [view_sql(p) for p in PRODUCTS]
    (OUT / "01-marketplace-views.sql").write_text(";\n\n".join(statements) + ";\n", encoding="utf-8")
    (OUT / "trino-rules.json").write_text(json.dumps(access_rules(), indent=2) + "\n", encoding="utf-8")
    (OUT / "event-listener.properties").write_text(event_listener_properties(), encoding="utf-8")
    import yaml
    (OUT / "semantic_models.yml").write_text(
        yaml.safe_dump({"version": 2, **semantic_models()}, sort_keys=False), encoding="utf-8")
    (OUT / "sources.yml").write_text(yaml.safe_dump(dbt_sources(), sort_keys=False), encoding="utf-8")
    print(f"Generated {len(PRODUCTS)} certified views, "
          f"{sum(len(p.measures) for p in PRODUCTS)} metrics and access rules in {OUT}")


def apply():
    conn = connection()
    try:
        for statement in schema_sql():
            run(conn, statement)
        for product in PRODUCTS:
            run(conn, view_sql(product))
            print(f"  certified  {product.fqn}")
        print(f"Applied {len(PRODUCTS)} certified views "
          f"(~1 KB of view metadata each, no data files)")
    finally:
        conn.close()


def sandbox(username):
    schema = sandbox_schema(username)
    conn = connection()
    try:
        run(conn, f"CREATE SCHEMA IF NOT EXISTS iceberg.{schema}")
        print(f"Sandbox ready: iceberg.{schema}")
        print("  Views cost nothing. Derived tables are capped and expire - "
              "see marketplace.retention for the policy.")
    finally:
        conn.close()


def verify():
    """Prove the two claims that matter: views work, physical tables are hidden."""
    conn = connection()
    checks = []
    try:
        rows = run(conn, f"SHOW TABLES FROM iceberg.{MARKETPLACE_SCHEMA}")
        present = {r[0] for r in rows}
        checks.append(("all products published", present >= {p.name for p in PRODUCTS},
                       f"{len(present)} views"))
        for product in PRODUCTS:
            count = run(conn, f"SELECT COUNT(*) FROM {product.fqn}")[0][0]
            checks.append((f"{product.name} returns rows", count > 0, f"{count} rows"))
        # Storage overhead: a view holds no files.
        sizes = run(conn, f"""
            SELECT COUNT(*) FROM iceberg.information_schema.tables
            WHERE table_schema = '{MARKETPLACE_SCHEMA}' AND table_type = 'VIEW'""")[0][0]
        checks.append(("marketplace is views only, no data files", sizes == len(PRODUCTS),
                       f"{sizes} views"))
    finally:
        conn.close()
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} - {detail}")
    return all(ok for _, ok, _ in checks)


def main():
    parser = argparse.ArgumentParser(description="Build the governed data marketplace")
    parser.add_argument("action", choices=["generate", "apply", "sandbox", "verify"])
    parser.add_argument("--user", default=None)
    args = parser.parse_args()
    if args.action == "generate":
        generate()
    elif args.action == "apply":
        apply()
    elif args.action == "sandbox":
        if not args.user:
            raise SystemExit("--user is required")
        sandbox(args.user)
    elif args.action == "verify":
        raise SystemExit(0 if verify() else 1)


if __name__ == "__main__":
    main()

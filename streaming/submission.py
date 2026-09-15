"""Real application intake, separate from the seeded simulation.

A submitted application_id must be unique across every process, which the nine
per-table primary keys cannot express. dbo.application_registry is the arbiter:
its primary key, not the preceding SELECT, is what makes a duplicate impossible
under concurrency. The SELECT only turns the race into a friendly error.
"""
import argparse
from datetime import datetime, timedelta
import os
import re
from uuid import uuid4
from .contracts import COLUMNS, PROCESSES, ROUTES

TYPES = {"MA": ("new", "renewal", "variation"), "CT": ("new", "amendment"), "GMP": ("inspection",)}
SLA_DAYS = 90
ID_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{8,64}$")
DUPLICATE_ERRORS = (2601, 2627)  # unique index / primary key violation


class DuplicateApplication(Exception):
    """Raised when the application_id already exists in any process."""


class InvalidSubmission(ValueError):
    """Raised when the payload fails the intake contract."""


def find(conn, application_id):
    """Return (process_code, source, submitted_at) if the id is already taken."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT process_code, source, submitted_at FROM dbo.application_registry WHERE application_id=%s",
                       (application_id,))
        return cursor.fetchone()


def validate(process, application_type, route, application_id):
    if process not in PROCESSES:
        raise InvalidSubmission(f"process must be one of {', '.join(PROCESSES)}")
    if application_type not in TYPES[process]:
        raise InvalidSubmission(f"{process} application_type must be one of {', '.join(TYPES[process])}")
    if route not in ROUTES:
        raise InvalidSubmission(f"route must be one of {', '.join(ROUTES)}")
    if not ID_PATTERN.match(application_id):
        raise InvalidSubmission("application_id must be 8-64 chars of letters, digits, ':', '_' or '-'")


def register(conn, application_id, process, source, when):
    """Claim the id. Returns False if another row already holds it."""
    import pymssql
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO dbo.application_registry (application_id, process_code, source, submitted_at)"
                           " VALUES (%s,%s,%s,%s)", (application_id, process, source, when))
        return True
    except pymssql.Error as error:
        number = error.args[0] if error.args and isinstance(error.args[0], int) else None
        if number in DUPLICATE_ERRORS:
            return False
        raise


def submit(conn, process, application_type, route, entity_id, application_id=None, received_at=None):
    """Record a user-submitted application as RECEIVED, rejecting duplicate ids."""
    application_id = application_id or str(uuid4())
    validate(process, application_type, route, application_id)
    received_at = received_at or datetime.now()
    existing = find(conn, application_id)
    if existing:
        conn.rollback()
        raise DuplicateApplication(f"application_id already submitted to {existing[0]} on {existing[2]:%Y-%m-%d}")
    if not register(conn, application_id, process, "submission", received_at):
        conn.rollback()
        raise DuplicateApplication("application_id was claimed concurrently")
    row = {
        "record_id": application_id, "application_id": application_id, "entity_id": entity_id,
        "process_code": process, "application_type": application_type, "activity_type": "application",
        "route": route, "cohort_month": received_at.strftime("%Y-%m"), "received_at": received_at,
        "due_at": received_at + timedelta(days=SLA_DAYS), "completed_at": None, "updated_at": received_at,
        "status": "RECEIVED", "outcome": None, "touch_days": 0., "wait_days": 0., "revision": 1,
    }
    fields = list(COLUMNS)
    with conn.cursor() as cursor:
        cursor.execute(f"INSERT INTO dbo.{process.lower()}_applications ({','.join(fields)})"
                       f" VALUES ({','.join(['%s'] * len(fields))})", tuple(row[k] for k in fields))
    conn.commit()
    return row


def claim_simulated(conn, application_id, process, when):
    """Reserve a seeded id without failing a replay that already reserved it."""
    if find(conn, application_id):
        return False
    return register(conn, application_id, process, "simulation", when)


def main():
    parser = argparse.ArgumentParser(description="Submit one application through the intake contract")
    parser.add_argument("--process", required=True, choices=list(PROCESSES))
    parser.add_argument("--type", required=True, dest="application_type")
    parser.add_argument("--route", default=ROUTES[0], choices=list(ROUTES))
    parser.add_argument("--entity-id", required=True)
    parser.add_argument("--application-id", default=None, help="Omit to mint a fresh UUID")
    args = parser.parse_args()
    from .simulator import connect
    conn = connect()
    try:
        row = submit(conn, args.process, args.application_type, args.route, args.entity_id, args.application_id)
        print(f"Accepted {row['application_id']} for {row['process_code']} due {row['due_at']:%Y-%m-%d}")
    except DuplicateApplication as error:
        raise SystemExit(f"Rejected: {error}")
    except InvalidSubmission as error:
        raise SystemExit(f"Rejected: {error}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

"""What every collector returns, and the identity the resolver gives it.

A collector never invents a URN. It reports what it saw, under whatever names its
own provider uses, and declares edges by those native names. Minting the URN and
deciding that two sightings are the same thing is the resolver's job - which is
what stops a new plugin from quietly corrupting the join between everything else.

    Entity          one thing a provider reported
    CandidateEdge   a relationship, expressed in native names
    Reference       how an edge points at something before URNs exist
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import re

# urn:infra:<provider>:<kind>:<scope>/<name>
URN = "urn:infra:{provider}:{kind}:{scope}/{name}"
_UNSAFE = re.compile(r"[^A-Za-z0-9._@:/-]+")

# Relationship vocabulary. Kept small on purpose: an edge type nobody can define
# in one line is a type the UI cannot render a legend for.
EDGE_TYPES = {
    "MANAGES":      "infrastructure-as-code declares this runtime object",
    "CONTAINS":     "structural nesting (namespace holds deployment)",
    "DEPENDS_ON":   "needs it to function",
    "EXPOSES":      "makes reachable (service, ingress, published port)",
    "STORES_IN":    "persists data here",
    "DERIVES_FROM": "data lineage (view over table)",
    "GRANTS":       "principal to capability",
    "MEMBER_OF":    "user to group, group to role",
}

# How confident the resolver is that two sightings are one thing. Surfaced in the
# UI rather than hidden, because a wrong join people can see is fixable.
EXPLICIT = 1.0      # both sides published the same native id
DECLARED = 0.9      # one side's attributes name the other
CONVENTIONAL = 0.6  # a configured pattern matched, e.g. s3://<bucket>/


def slug(value: Any) -> str:
    """Make a URN segment out of whatever a provider called something."""
    text = str(value if value is not None else "").strip()
    return _UNSAFE.sub("-", text).strip("-") or "unnamed"


def make_urn(provider: str, kind: str, scope: str, name: str) -> str:
    return URN.format(provider=slug(provider), kind=slug(kind),
                      scope=slug(scope or "default"), name=slug(name))


@dataclass
class Reference:
    """An edge target, before anything has a URN.

    Either a native id the target also publishes (`source` + `native_id`), or a
    direct guess at the target's coordinates. The resolver tries the native id
    first because that is the only form it can verify.
    """
    source: Optional[str] = None
    native_id: Optional[str] = None
    provider: Optional[str] = None
    kind: Optional[str] = None
    scope: Optional[str] = None
    name: Optional[str] = None

    def key(self) -> Optional[str]:
        if self.source and self.native_id:
            return f"{self.source}::{self.native_id}"
        return None


@dataclass
class CandidateEdge:
    type: str
    target: Reference
    rule: str = "declared"
    confidence: float = DECLARED
    via: Optional[str] = None

    def __post_init__(self):
        if self.type not in EDGE_TYPES:
            raise ValueError(f"unknown edge type {self.type!r}; "
                             f"add it to EDGE_TYPES with a one-line meaning")


@dataclass
class Entity:
    provider: str
    kind: str
    name: str
    scope: str = "default"
    # Facets the inventory filters on. Absent is honest; do not invent an owner.
    owner: Optional[str] = None
    environment: Optional[str] = None
    status: str = "unknown"
    tags: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    # Ids OTHER providers might also publish. This is what makes an explicit
    # join possible, so a collector should emit every id it legitimately knows.
    native_ids: Dict[str, str] = field(default_factory=dict)
    edges: List[CandidateEdge] = field(default_factory=list)

    @property
    def urn(self) -> str:
        return make_urn(self.provider, self.kind, self.scope, self.name)

    def alias_keys(self):
        return [f"{source}::{value}" for source, value in self.native_ids.items() if value]

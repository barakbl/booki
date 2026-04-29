"""
smart_lists — virtual lists defined declaratively in config.toml.

Each `[smart_list.<name>]` block becomes a SmartList: a set of predicates
(AND-combined) and an optional `order` directive. The predicates are
evaluated against a bookmark's frontmatter dict at query time, so smart
lists are always live — there's no membership to maintain.

Predicate syntax in TOML:

    [smart_list.youtube_unwatched]
    source   = "youtube"          # equality
    watched  = false              # bool — missing field counts as false
    order    = "synced_at desc"   # sort key + direction (optional)

    [smart_list.recent]
    synced_at = "> 2026-12-12"    # comparisons via prefix on string values
    tags      = ["ai", "ml"]      # list value → ANY-of semantics

Operator prefixes recognized at the start of a string value:
    >= <= != > < =     (longest match wins)

Field aliases (user-friendly name → actual frontmatter field):
    synced_at   → last_sync
    created_at  → date_bookmarked
    added_at    → date_bookmarked
    enriched_at → last_enriched
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


FIELD_ALIASES = {
    "synced_at":   "last_sync",
    "created_at":  "date_bookmarked",
    "added_at":    "date_bookmarked",
    "enriched_at": "last_enriched",
}

# Keys inside a smart-list block that aren't predicates.
RESERVED = {"order", "label", "icon", "description"}

# Op prefixes — order matters (longest first).
_OP_PREFIXES: list[tuple[str, str]] = [
    (">=", "gte"), ("<=", "lte"), ("!=", "ne"),
    (">",  "gt"),  ("<",  "lt"),  ("=",  "eq"),
]


@dataclass
class Predicate:
    field: str
    op:    str        # "eq" | "ne" | "gt" | "gte" | "lt" | "lte" | "any"
    value: Any

    def to_dict(self) -> dict:
        return {"field": self.field, "op": self.op, "value": self.value}


@dataclass
class SmartList:
    name:        str
    label:       str
    icon:        str
    description: str
    predicates:  list[Predicate] = field(default_factory=list)
    order:       Optional[tuple[str, str]] = None  # (field, "asc"|"desc")

    def to_dict(self) -> dict:
        return {
            "name":        self.name,
            "label":       self.label,
            "icon":        self.icon,
            "description": self.description,
            "predicates":  [p.to_dict() for p in self.predicates],
            "order":       list(self.order) if self.order else None,
        }


# ─── Parsing ──────────────────────────────────────────────────────────────────

def _resolve_field(name: str) -> str:
    return FIELD_ALIASES.get(name, name)


def _coerce(s: str) -> Any:
    """str → bool|int|float|str (best-effort)."""
    low = s.lower().strip()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try: return int(s)
    except ValueError: pass
    try: return float(s)
    except ValueError: pass
    return s


def parse_value(raw: Any) -> tuple[str, Any]:
    """Return (op, value) for one predicate entry."""
    if isinstance(raw, list):
        return ("any", list(raw))
    if isinstance(raw, str):
        s = raw.strip()
        for prefix, op in _OP_PREFIXES:
            if s.startswith(prefix):
                return (op, _coerce(s[len(prefix):].strip()))
        return ("eq", _coerce(s))
    return ("eq", raw)


def parse_order(raw: Any) -> Optional[tuple[str, str]]:
    if not isinstance(raw, str):
        return None
    parts = raw.strip().split()
    if not parts:
        return None
    f = _resolve_field(parts[0])
    direction = (parts[1] if len(parts) > 1 else "desc").lower()
    if direction not in ("asc", "desc"):
        direction = "desc"
    return (f, direction)


def parse_smart_lists(cfg: dict) -> list[SmartList]:
    """Pull all `[smart_list.<name>]` blocks out of a parsed TOML config."""
    raw = (cfg.get("smart_list") or {})
    out: list[SmartList] = []
    if not isinstance(raw, dict):
        return out
    for name, body in raw.items():
        if not isinstance(body, dict):
            continue
        preds: list[Predicate] = []
        for k, v in body.items():
            if k in RESERVED:
                continue
            preds.append(Predicate(
                field=_resolve_field(str(k)),
                **dict(zip(("op", "value"), parse_value(v))),
            ))
        out.append(SmartList(
            name=str(name),
            label=str(body.get("label") or name),
            icon=str(body.get("icon") or "⚡"),
            description=str(body.get("description") or ""),
            predicates=preds,
            order=parse_order(body.get("order")),
        ))
    return out


# ─── Evaluation ───────────────────────────────────────────────────────────────

def _eq(actual: Any, expected: Any) -> bool:
    """Equality with a few sensible coercions:

    * Bool predicate: missing/falsy field counts as False.
    * List on the item side: scalar `expected` matches if any element equals it.
    * Otherwise: string-compare.
    """
    if isinstance(expected, bool):
        return bool(actual) == expected
    if expected is None:
        return actual in (None, "", [])
    if isinstance(actual, list):
        return any(str(x) == str(expected) for x in actual)
    return str(actual) == str(expected)


def _any(actual: Any, wanted: list) -> bool:
    if not wanted:
        return False
    pool = actual if isinstance(actual, list) else ([actual] if actual not in (None, "") else [])
    have = {str(x) for x in pool}
    return any(str(w) in have for w in wanted)


def _cmp(left: Any, right: Any) -> Optional[int]:
    """Return -1/0/1 (or None if incomparable). Coerces ints + numeric strings to numbers."""
    if left in (None, "") or right in (None, ""):
        return None
    # Numeric coercion when one side is numeric.
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        try:
            l = float(left); r = float(right)
            return -1 if l < r else (1 if l > r else 0)
        except (TypeError, ValueError):
            pass
    try:
        if left < right:  return -1
        if left > right:  return 1
        return 0
    except TypeError:
        l, r = str(left), str(right)
        return -1 if l < r else (1 if l > r else 0)


def matches(fm: dict, sl: SmartList) -> bool:
    """All predicates must match (AND)."""
    for p in sl.predicates:
        actual = fm.get(p.field)
        if p.op == "eq":
            if not _eq(actual, p.value): return False
        elif p.op == "ne":
            if _eq(actual, p.value): return False
        elif p.op == "any":
            if not _any(actual, p.value): return False
        else:
            c = _cmp(actual, p.value)
            if c is None: return False
            if p.op == "gt"  and not c > 0:  return False
            if p.op == "gte" and not c >= 0: return False
            if p.op == "lt"  and not c < 0:  return False
            if p.op == "lte" and not c <= 0: return False
    return True


def apply_order(items: list[tuple[str, dict]], order: Optional[tuple[str, str]]
                ) -> list[tuple[str, dict]]:
    """Sort (bid, fm) tuples by `order`. Items missing the field sort last."""
    if not order:
        return items
    field_, direction = order
    reverse = (direction == "desc")

    def key(pair):
        v = pair[1].get(field_)
        # Tuple: (is_missing, value). Missing always sorts last regardless of direction.
        if v in (None, ""):
            return (1, "")
        if isinstance(v, (int, float)):
            return (0, v)
        return (0, str(v))

    out = sorted(items, key=key, reverse=reverse)
    # If reverse=True we still want missing-last; flip back.
    if reverse:
        present = [p for p in out if p[1].get(field_) not in (None, "")]
        missing = [p for p in out if p[1].get(field_) in (None, "")]
        return present + missing
    return out

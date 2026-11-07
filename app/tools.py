"""Tools Claude can call to query the incident collection.

Claude never writes raw MongoDB queries: it fills in a fixed set of filters,
which are validated here and turned into a query. That keeps the bot read-only
and stops it from running arbitrary operators against the database.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from pymongo.collection import Collection

# filter name -> document field
CATEGORY_FILTERS = {
    "degree_of_injury": "degree_of_injury",
    "event_type": "event_type",
    "nature_of_injury": "nature_of_injury",
    "part_of_body": "part_of_body",
    "environmental_factor": "environmental_factor",
    "human_factor": "human_factor",
    "task_assigned": "task_assigned",
    "construction_end_use": "construction.end_use",
    "project_type": "construction.project_type",
    "project_cost": "construction.project_cost",
}

GROUP_BY_FIELDS = {**CATEGORY_FILTERS, "year": "year", "month": "month"}

MAX_SEARCH_RESULTS = 25


class ToolInputError(ValueError):
    """The model sent input that doesn't fit the tool's schema."""


def category_values(coll: Collection) -> dict[str, list[str]]:
    """The values each category filter can take, read from the data."""
    return {
        name: sorted(v for v in coll.distinct(field) if isinstance(v, str) and v)
        for name, field in CATEGORY_FILTERS.items()
    }


def _filters_schema(values: dict[str, list[str]]) -> dict:
    props: dict[str, Any] = {
        name: {"type": "string", "enum": vals} for name, vals in values.items()
    }
    props.update(
        {
            "date_from": {"type": "string", "description": "Inclusive start date, YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "Inclusive end date, YYYY-MM-DD"},
            "keyword": {
                "type": "string",
                "description": "Exact OSHA keyword tag, lowercase (e.g. 'ladder', 'scaffold', 'forklift', 'lockout').",
            },
            "text": {
                "type": "string",
                "description": "Case-insensitive phrase to find in the incident description or abstract text.",
            },
        }
    )
    return {
        "type": "object",
        "description": "All filters are optional and combined with AND.",
        "properties": props,
        "additionalProperties": False,
    }


def tool_definitions(values: dict[str, list[str]]) -> list[dict]:
    filters = _filters_schema(values)
    return [
        {
            "name": "search_incidents",
            "description": (
                "Find individual OSHA incident reports (July 2015 - August 2017). Returns up to `limit` "
                "matches, newest first, with a short excerpt of each abstract, plus the total number "
                "of matches. Use it to find examples and case studies."
            ),
            "eager_input_streaming": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "filters": filters,
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "incident_statistics",
            "description": (
                "Count incidents grouped by one field (with a fatal / nonfatal split), after applying "
                "optional filters. Use it for 'how many', 'most common', trends over time and comparisons. "
                "With group_by omitted it returns only the total count."
            ),
            "eager_input_streaming": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "group_by": {"type": "string", "enum": sorted(GROUP_BY_FIELDS)},
                    "filters": filters,
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "get_incident",
            "description": "Get the full record of one incident, including the complete abstract text, by its id.",
            "eager_input_streaming": True,
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Incident id from search results"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    ]


# --------------------------------------------------------------------------- validation


def _parse_date(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ToolInputError(f"{name} must be a YYYY-MM-DD string")
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ToolInputError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def _int_in_range(value: Any, name: str, lo: int, hi: int, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ToolInputError(f"{name} must be an integer from {lo} to {hi}")
    return value


def _check_keys(obj: dict, allowed: set[str], where: str) -> None:
    extra = set(obj) - allowed
    if extra:
        raise ToolInputError(f"Unknown field(s) in {where}: {', '.join(sorted(extra))}")


def build_query(filters: Any, values: dict[str, list[str]]) -> dict:
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise ToolInputError("filters must be an object")
    _check_keys(filters, set(CATEGORY_FILTERS) | {"date_from", "date_to", "keyword", "text"}, "filters")

    query: dict[str, Any] = {}
    for name, field in CATEGORY_FILTERS.items():
        if name in filters:
            val = filters[name]
            if val not in values.get(name, []):
                raise ToolInputError(f"{name} must be one of {values.get(name, [])}, got {val!r}")
            query[field] = val

    date_range: dict[str, datetime] = {}
    if "date_from" in filters:
        date_range["$gte"] = _parse_date(filters["date_from"], "date_from")
    if "date_to" in filters:
        date_range["$lt"] = _parse_date(filters["date_to"], "date_to") + timedelta(days=1)
    if date_range:
        query["event_date"] = date_range

    if "keyword" in filters:
        kw = filters["keyword"]
        if not isinstance(kw, str) or not kw.strip():
            raise ToolInputError("keyword must be a non-empty string")
        query["keywords"] = kw.strip().lower()

    if "text" in filters:
        text = filters["text"]
        if not isinstance(text, str) or not text.strip():
            raise ToolInputError("text must be a non-empty string")
        pattern = {"$regex": re.escape(text.strip()), "$options": "i"}
        query["$or"] = [{"description": pattern}, {"abstract": pattern}]

    return query


# --------------------------------------------------------------------------- tools


def _summary(doc: dict) -> dict:
    abstract = doc.get("abstract") or ""
    return {
        "id": doc["_id"],
        "date": doc["event_date"].strftime("%Y-%m-%d"),
        "description": doc.get("description"),
        "degree_of_injury": doc.get("degree_of_injury"),
        "event_type": doc.get("event_type"),
        "nature_of_injury": doc.get("nature_of_injury"),
        "part_of_body": doc.get("part_of_body"),
        "excerpt": abstract[:400] + ("..." if len(abstract) > 400 else ""),
    }


def search_incidents(coll: Collection, values: dict, args: dict) -> dict:
    _check_keys(args, {"filters", "limit"}, "input")
    query = build_query(args.get("filters"), values)
    limit = _int_in_range(args.get("limit"), "limit", 1, MAX_SEARCH_RESULTS, 10)
    total = coll.count_documents(query)
    docs = coll.find(query).sort("event_date", -1).limit(limit)
    return {"total_matches": total, "returned": [_summary(d) for d in docs]}


def incident_statistics(coll: Collection, values: dict, args: dict) -> dict:
    _check_keys(args, {"group_by", "filters", "top_n"}, "input")
    query = build_query(args.get("filters"), values)
    group_by = args.get("group_by")
    if group_by is None:
        total = coll.count_documents(query)
        fatal = coll.count_documents({**query, "degree_of_injury": "Fatal"}) if "degree_of_injury" not in query else None
        return {"total": total, "fatal": fatal}
    if group_by not in GROUP_BY_FIELDS:
        raise ToolInputError(f"group_by must be one of {sorted(GROUP_BY_FIELDS)}")
    top_n = _int_in_range(args.get("top_n"), "top_n", 1, 50, 15)

    if group_by == "month":
        key: Any = {"$dateToString": {"format": "%Y-%m", "date": "$event_date"}}
    else:
        key = f"${GROUP_BY_FIELDS[group_by]}"
    sort = {"_id": 1} if group_by in ("year", "month") else {"count": -1}
    pipeline = [
        {"$match": query},
        {
            "$group": {
                "_id": key,
                "count": {"$sum": 1},
                "fatal": {"$sum": {"$cond": [{"$eq": ["$degree_of_injury", "Fatal"]}, 1, 0]}},
            }
        },
        {"$sort": sort},
    ]
    groups = list(coll.aggregate(pipeline))
    total = sum(g["count"] for g in groups)
    rows = [
        {"value": g["_id"] if g["_id"] is not None else "(not recorded)", "count": g["count"], "fatal": g["fatal"]}
        for g in groups
    ]
    if group_by not in ("year", "month"):
        rows = rows[:top_n]
    return {"group_by": group_by, "total_incidents": total, "groups": rows}


def get_incident(coll: Collection, values: dict, args: dict) -> dict:
    _check_keys(args, {"id"}, "input")
    incident_id = args.get("id")
    if not isinstance(incident_id, str) or not incident_id:
        raise ToolInputError("id must be a non-empty string")
    doc = coll.find_one({"_id": incident_id})
    if doc is None:
        raise ToolInputError(f"No incident with id {incident_id!r}")
    doc["id"] = doc.pop("_id")
    doc["event_date"] = doc["event_date"].strftime("%Y-%m-%d")
    return doc


HANDLERS = {
    "search_incidents": search_incidents,
    "incident_statistics": incident_statistics,
    "get_incident": get_incident,
}


def run_tool(coll: Collection, values: dict, name: str, args: Any) -> tuple[str, bool]:
    """Run a tool. Returns (result JSON, is_error)."""
    handler = HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool {name!r}"}), True
    if not isinstance(args, dict):
        return json.dumps({"error": "Tool input must be a JSON object"}), True
    try:
        return json.dumps(handler(coll, values, args), default=str), False
    except ToolInputError as exc:
        return json.dumps({"error": str(exc)}), True

"""Turn the OSHA CSV into MongoDB documents."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

import pandas as pd

# CSV label column -> document field
CATEGORY_COLUMNS = {
    "Degree of Injury": "degree_of_injury",
    "Nature of Injury": "nature_of_injury",
    "Part of Body": "part_of_body",
    "Event type": "event_type",
    "Environmental Factor": "environmental_factor",
    "Human Factor": "human_factor",
    "Task Assigned": "task_assigned",
}

CONSTRUCTION_COLUMNS = {
    "Construction End Use": "end_use",
    "Project Cost": "project_cost",
    "Project Type": "project_type",
}


def _clean(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def read_csv(path: str) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding_errors="replace")


def rows_to_documents(df: pd.DataFrame) -> Iterator[dict]:
    """One document per CSV row.

    A report (summary_nr) can list several injured employees, so the same
    summary_nr may appear on more than one row; `_id` adds a row suffix.
    """
    seen: dict[str, int] = {}
    for row in df.to_dict("records"):
        nr = _clean(row["summary_nr"])
        n = seen.get(nr, 0)
        seen[nr] = n + 1

        date = datetime.strptime(row["Event Date"].strip(), "%m/%d/%Y")
        keywords = [k.strip().lower() for k in (row.get("Event Keywords") or "").split(",") if k.strip()]

        doc: dict[str, Any] = {
            "_id": f"{nr}-{n}",
            "summary_nr": nr,
            "event_date": date,
            "year": date.year,
            "description": _clean(row.get("Event Description")),
            "abstract": _clean(row.get("Abstract Text")),
            "keywords": keywords,
        }
        for col, field in CATEGORY_COLUMNS.items():
            doc[field] = _clean(row.get(col))

        construction = {f: _clean(row.get(c)) for c, f in CONSTRUCTION_COLUMNS.items()}
        stories = _clean(row.get("Building Stories"))
        if stories and stories != "0":
            construction["building_stories"] = int(stories)
        construction = {k: v for k, v in construction.items() if v}
        if construction:
            doc["construction"] = construction

        hazsub = _clean(row.get("hazsub"))
        if hazsub and hazsub != "0":
            doc["hazardous_substance_code"] = hazsub
        fat_cause = _clean(row.get("fat_cause"))
        if fat_cause and fat_cause != "0":
            doc["fatality_cause_code"] = int(fat_cause)
        # fall_ht is 0 in every row of the source file, so it is not loaded.

        yield doc


INDEXES = [
    [("event_date", 1)],
    [("degree_of_injury", 1), ("event_type", 1)],
    [("nature_of_injury", 1)],
    [("part_of_body", 1)],
    [("keywords", 1)],
    [("construction.end_use", 1)],
]

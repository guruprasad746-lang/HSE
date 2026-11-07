"""Tests run against an in-memory MongoDB (mongomock) and a scripted fake Claude client."""

import json
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.ingest import read_csv, rows_to_documents
from app.main import create_app
from app.tools import category_values, run_tool, tool_definitions

CSV = Path(__file__).resolve().parent.parent / "OSHA HSE DATA_ALL ABSTRACTS 15-17_FINAL.csv"


@pytest.fixture(scope="module")
def docs():
    return list(rows_to_documents(read_csv(str(CSV))))


@pytest.fixture()
def coll(docs):
    c = mongomock.MongoClient()["hse"]["incidents"]
    c.insert_many([dict(d) for d in docs])
    return c


@pytest.fixture()
def values(coll):
    return category_values(coll)


def call(coll, values, name, args):
    out, is_error = run_tool(coll, values, name, args)
    return json.loads(out), is_error


# ------------------------------------------------------------------ ingest


def test_ingest_keeps_every_row_with_unique_ids(docs):
    assert len(docs) == 4847
    assert len({d["_id"] for d in docs}) == 4847
    assert sum(d["degree_of_injury"] == "Fatal" for d in docs) == 2964


def test_ingest_shapes_fields(docs):
    first = docs[0]
    assert first["_id"] == "220982664-0"
    assert first["year"] == 2017
    assert "amputation" in first["keywords"]
    assert "construction" not in first  # blank construction columns are dropped
    with_construction = next(d for d in docs if "construction" in d)
    assert with_construction["construction"]["end_use"]


# ------------------------------------------------------------------ tools


def test_statistics_total_and_fatal(coll, values):
    out, err = call(coll, values, "incident_statistics", {})
    assert not err and out == {"total": 4847, "fatal": 2964}


def test_statistics_group_by_event_type(coll, values):
    out, err = call(coll, values, "incident_statistics", {"group_by": "event_type", "top_n": 3})
    assert not err
    assert [g["value"] for g in out["groups"]] == ["Fall (from elevation)", "Struck-by", "Caught in or between"]
    assert out["groups"][0]["count"] == 1179


def test_statistics_by_month_is_chronological(coll, values):
    out, _ = call(coll, values, "incident_statistics", {"group_by": "month"})
    months = [g["value"] for g in out["groups"]]
    assert months == sorted(months) and months[0] == "2015-07"


def test_search_with_filters(coll, values):
    out, err = call(
        coll, values, "search_incidents",
        {"filters": {"degree_of_injury": "Fatal", "event_type": "Shock", "date_from": "2017-01-01"}, "limit": 5},
    )
    assert not err
    assert 0 < len(out["returned"]) <= 5
    assert all(r["event_type"] == "Shock" and r["date"] >= "2017-01-01" for r in out["returned"])
    dates = [r["date"] for r in out["returned"]]
    assert dates == sorted(dates, reverse=True)


def test_search_text_is_literal_not_regex(coll, values):
    out, err = call(coll, values, "search_incidents", {"filters": {"text": "forklift"}})
    assert not err and out["total_matches"] > 0
    out, err = call(coll, values, "search_incidents", {"filters": {"text": ".*"}})
    assert not err and out["total_matches"] == 0


def test_get_incident(coll, values):
    out, err = call(coll, values, "get_incident", {"id": "220982664-0"})
    assert not err and out["id"] == "220982664-0" and out["abstract"].startswith("At 9:00 a.m.")


@pytest.mark.parametrize(
    "name,args",
    [
        ("search_incidents", {"filters": {"event_type": "Alien abduction"}}),
        ("search_incidents", {"filters": {"$where": "1"}}),
        ("search_incidents", {"limit": 1000}),
        ("incident_statistics", {"group_by": "abstract"}),
        ("incident_statistics", {"filters": {"date_from": "last year"}}),
        ("get_incident", {"id": "nope"}),
        ("drop_database", {}),
    ],
)
def test_bad_input_returns_error(coll, values, name, args):
    out, err = call(coll, values, name, args)
    assert err and "error" in out


def test_tool_schemas_list_real_values(values):
    tools = {t["name"]: t for t in tool_definitions(values)}
    enum = tools["search_incidents"]["input_schema"]["properties"]["filters"]["properties"]["event_type"]["enum"]
    assert "Fall (from elevation)" in enum


# ------------------------------------------------------------------ chat loop with a fake Claude


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for block in self.message.content:
                if block.type == "text":
                    yield SimpleNamespace(type="text", text=block.text)
        return gen()

    async def get_final_message(self):
        return self.message


class FakeClient:
    """Plays back scripted responses and records each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        self.requests.append({**params, "messages": list(params["messages"])})
        return FakeStream(self.responses.pop(0))


def msg(stop_reason, *blocks):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def stream_events(client, body):
    with client.stream("POST", "/api/chat", json=body) as r:
        assert r.status_code == 200
        raw = r.read().decode()
    return [json.loads(line[6:]) for line in raw.split("\n\n") if line.startswith("data: ")]


def test_chat_runs_tool_then_answers(coll):
    fake = FakeClient([
        msg("tool_use", text("Let me check."), tool_use("t1", "incident_statistics", {"group_by": "event_type", "top_n": 1})),
        msg("end_turn", text("Falls from elevation are the most common (1,179).")),
    ])
    with TestClient(create_app(collection=coll, client=fake)) as client:
        assert client.get("/api/health").json()["incidents"] == 4847
        events = stream_events(client, {"conversation_id": "c1", "message": "Most common event?"})

    assert [e["type"] for e in events] == ["text", "tool", "text", "done"]
    second = fake.requests[1]
    assert second["model"] == "claude-opus-5-5"
    assert second["fallbacks"] == "default"
    result = second["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "t1" and not result["is_error"]
    assert json.loads(result["content"])["groups"][0]["value"] == "Fall (from elevation)"


def test_conversation_history_carries_over_and_rolls_back_on_refusal(coll):
    fake = FakeClient([
        msg("end_turn", text("Hello.")),
        msg("refusal"),
        msg("end_turn", text("Still here.")),
    ])
    with TestClient(create_app(collection=coll, client=fake)) as client:
        stream_events(client, {"conversation_id": "c2", "message": "hi"})
        events = stream_events(client, {"conversation_id": "c2", "message": "bad"})
        assert events[-1]["type"] == "error"
        stream_events(client, {"conversation_id": "c2", "message": "again"})

    # Third request: first exchange kept, refused turn removed.
    roles = [(m["role"], m["content"] if isinstance(m["content"], str) else "...") for m in fake.requests[2]["messages"]]
    assert roles == [("user", "hi"), ("assistant", "..."), ("user", "again")]

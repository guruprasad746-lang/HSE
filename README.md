# HSE Chatbot

A chat assistant for health and safety questions, answered from the OSHA incident
dataset in this repo (4,847 serious-injury and fatality reports, July 2015 – August 2017)
stored in MongoDB Atlas. Claude answers by calling read-only query tools, so figures
and examples come from the data and cite incident ids.

```
browser ──► FastAPI (app/main.py) ──► Claude (app/agent.py) ──tool calls──► MongoDB Atlas (app/tools.py)
```

## Setup

### 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

### 2. Configure Atlas

In the Atlas UI for **Project 0 → Cluster0**:

1. **Database Access**: create a database user with read/write on the `hse` database
   (the loader writes; the chatbot only reads, so a read-only user is fine for running the app).
2. **Network Access**: add the IP address of the machine that will run the loader and server.
3. **Connect → Drivers**: copy the connection string.

```bash
cp .env.example .env
```

Fill in `MONGODB_URI` (with the user and password) and `ANTHROPIC_API_KEY` in `.env`.

### 3. Load the data

```bash
python scripts/load_data.py
```

This loads every CSV row into `hse.incidents` and creates indexes. It is safe to re-run;
add `--drop` to start from an empty collection.

### 4. Run

```bash
uvicorn app.main:app --reload
```

Open http://localhost:8000.

## What it can answer

- "What are the most common event types in fatal incidents?"
- "Show recent forklift incidents where the task wasn't regularly assigned"
- "How did falls from elevation trend by month in 2016?"
- "Summarise incident 220982664-0 and suggest controls"

## How it works

**Data model** (`app/ingest.py`). There is one document per CSV row. A report
(`summary_nr`) can cover several injured workers, so `_id` is `summary_nr-n`. Coded
columns become readable fields (`event_type`, `nature_of_injury`, …). The construction
fields sit in a `construction` sub-document, which is only present when the CSV has
values for them. Keywords are stored as a lowercase array. `fall_ht` is `0` in every
source row, so it isn't loaded.

**Tools** (`app/tools.py`). Claude doesn't write MongoDB queries. It calls three tools
with fixed, validated filters, and the allowed values for each category are read from
the database when the server starts:

| Tool | Purpose |
|---|---|
| `incident_statistics` | Counts grouped by a field (or by year or month), with a fatal / nonfatal split |
| `search_incidents` | Matching incidents, newest first, with excerpts (up to 25) |
| `get_incident` | One complete record |

Invalid input, such as an unknown value or a bad date, goes back to Claude as a tool
error so it can correct itself.

**Model** (`app/agent.py`). Uses `claude-opus-5-5` with adaptive thinking and
streaming. Effort is `medium`, which you can change with `CLAUDE_EFFORT`. The system
prompt and tool definitions are prompt-cached. Server-side refusal fallbacks
(`fallbacks: "default"`) are switched on, so a safety-classifier decline is retried
on a fallback model automatically. To turn this off, remove `betas` and `fallbacks`
from `_request_params`.

**API**
- `POST /api/chat` `{conversation_id, message}` returns a server-sent event stream
  of `text`, `tool`, `error` and `done` events.
- `DELETE /api/conversations/{id}` clears a conversation.
- `GET /api/health` returns the document count.

## Limitations

- Conversations are held in server memory. They are lost on restart and aren't
  shared between workers. For production, store them in MongoDB or Redis.
- There is no authentication. Put the app behind your SSO or a reverse proxy before
  sharing it.
- Text search is a case-insensitive substring match, which is fine for 5k documents.
  For larger data or semantic search, add an Atlas Search index.

## Tests

```bash
python -m pytest -q
```

The tests run against an in-memory MongoDB (`mongomock`) and a scripted fake Claude
client, so they need neither Atlas nor an API key.

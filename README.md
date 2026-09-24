# KnessetLM

The motivation for this project is to use modern AI tools to process and analyze the activity of the Israeli Knesset.
The main event here is a web app (currently called "מעורב ירושלמי") hosting an AI research agent that accesses the Knesset API and pre-processed protocols in order to answer different questions, emphesizing transparancy in the process and answer sources.

However, this repo features a few standalone offerings you might find useful:
- Under `utils/` you can find python wrappers for Knesset API access, and implementation of tools that allow you to give Knesset DB access to any supporting LLM.
- Under `src/summarization` you can find a standalone agent that summarizes protocols.
- The agent is based on a generic custom implementation of an llm state-machine, that comes with a web UI for designing machines and an engine that runs them. The designer UI can be found under `devtools/designer`, and the engine at `src/agent`.
Access different features via `scripts/`.

The agents support local LLMs via llama.cpp, or Google API calls.

---

## What The Agent Does

- Answers natural-language questions about Knesset Members (MKs) and committees in Hebrew
- Retrieves and synthesizes evidence from meeting protocols and official Knesset data (voting + bills)
- Sources every claim with citations back to the original transcript or API result

---

## Architecture

**Key components:**

| Path | Role |
|------|------|
| `src/agent/` | Core agentic engine, running a preconfigured state machine and special subgraphs |
| `src/agent/subgraph/` | Interface for a state machine node that can run multiple LLM calls with special logic |
| `src/agent/plan_execute/` | Core plan-and-execute subgraph |
| `src/agent/research_agent/` | Knesset-specific plan-and-execute research agent |
| `src/utils/` | Knesset API data access and tools for your favorite LLMs |
| `src/retrieval/` | `knesset.db` schema and FTS5 keyword queries over meeting protocols |
| `src/indexing/` | Speaker label → MK resolution |
| `src/summarization/` | Protocol summarizer |
| `machines/` | JSON state machines driving the agent flow |
| `web/` | Chat UI served by `scripts/run_web.py` |

---

## Setup

**Requirements:**
- Python 3.10+

1. Install dependencies (project uses standard `pip`; no `requirements.txt` yet — see imports in `src/`).
   ```
   python -m pip install -r requirements.txt
   ```
2. Edit `src/config.py` models section to choose which models handle which part of the agent.
   If anything is set to `"local"`, you will need to run a local llama-server and adjust parameters in the config (context size, etc).
   If anything is set to `"gemini-..."` or anything else that uses the google api, you will have to save your
   Google AI API key to this environment server: `GOOGLE_API_KEY`.
3. The agent will query DBs you have to create (described under Data Sources and Processing below).
   Here too you should select which models are used for summarizing, and prepare yourself - it's going to take some time.
   ```
   cd src
   python ../scripts/process_knesset.py
   ```
   To save time on summarization, you can use:
   ```
   cd src
   python ../scripts/summarize_knesset_batches.py
   ```
   which utilizes the Google batch API to summarize a bunch of protocols at one API call.
4. Run the web UI:
   ```
   python scripts/run_web.py
   ```
   Open `http://localhost:5000` in a browser.

To run the designer and design your own agents, use:
```
python devtools/designer/app.py
```

---

## Data Sources and Processing

For API queries, the tools use the official Knesset OData v4 api at `knesset.gov.il`, and also the REST API provided by the Open Knesset project at `oknesset.org`.
API queries are cached localy up to 1 week.

The pre-processing of the Knesset protocols is done as such:
- First, the protocol is summarized by a standalone agent. The summarization contains main subjects discussed, and main opinions expressed as bullet lists.
- Then, speeches, summary topics, opinions and attendance are stored in one SQLite DB with FTS5 keyword indexes (`scripts/build_knesset_db.py`).

Raw protocols go in `Data/raw_transcriptions/<knesset_num>/<committee>/` as text files.
Summaries go in `Data/summaries/<knesset_num>/<committee>/` as text files.
SQL DB lives in `Data/knesset.db`.

---

## Agent Tools

The research agent has access to the following tools:

### Entity Resolution

| Tool | What it does |
|------|-------------|
| `find_mk` | Fuzzy-resolve an MK name to a full profile: party/faction history, committee positions, ministerial roles. |
| `find_committee` | Fuzzy-resolve a committee name to its ID and current member list. |
| `find_party` | Fuzzy-match a party/faction name and return its full member list for a given Knesset. |

### Committee Protocols (`Data/knesset.db`)

| Tool | What it does |
|------|-------------|
| `query_protocols` | FTS5 keyword search (or listing, with an empty query) over summary topics, verified opinions and transcript speeches. Filters: MK, party, committees, meeting ids, date range. Also reads a meeting's summary or pages through its transcript. |
| `get_meeting_attendance` | Who attended a meeting: MKs (with party) first, then guests. |

### Bills and Votes (live Knesset OData)

| Tool | What it does |
|------|-------------|
| `query_bills` | Search bills by Hebrew title words within a Knesset. |
| `get_bill` | Bill metadata (status, initiators, documents), optionally with the extracted bill text. |
| `query_votes` | Plenum votes — by keyword, by MK, both (how the MK voted), or the most recent. |

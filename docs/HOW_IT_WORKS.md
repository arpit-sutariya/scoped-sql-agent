# How it works: a guided tour of the code

Read this with the code open. It follows the data from the three sources to an answer, then covers the decisions you'll be asked about in interviews.

## 1. The sources (`scripts/generate_sources.py`)

The script creates the client's three systems, with the kind of mess real systems have:

| Source | What's messy about it |
|---|---|
| ERP (`sources/erp.db`) | Region codes (`WST`) instead of names; 1% of orders have no ship region |
| CRM (`sources/crm_accounts.csv`) | 6 duplicate accounts; 8 prospects that never ordered; 10 real customers missing; region spelled 5 ways; revenue as `$1.2M`, `approx 4M` or blank; owner written as a name or an email; 3 date formats |
| Support API (`support_api/main.py`) | Paginated and token-protected; timestamps as ISO with offsets, `...Z` or epoch milliseconds; tickets from Gmail addresses; a few solved before they were opened; one subject line containing a prompt injection |

It also writes `sources/_truth.json`, the correct answers for matching, which exist only because the data is synthetic. The pipeline never reads it. It's only used to score the pipeline afterwards.

## 2. The pipeline (`pipeline/`)

- **`clean.py`:** small functions that each turn one messy value into a clean one, or return `None` when they can't. The rule throughout is **never guess**. A timestamp without a time zone returns `None` instead of assuming UTC.
- **`build_warehouse.py`:**
  1. **Fetch tickets**, page by page, with retries on 5xx errors.
  2. **Match CRM rows to ERP customers:** by ERP id first, then by normalised name (`name_key` drops case, spaces, punctuation and legal suffixes), then by fuzzy name at ≥0.9 similarity, with every fuzzy match logged.
  3. **Merge duplicates,** keeping the most recent row.
  4. **Resolve each field by an explicit rule:** region from the ERP, owner and revenue from the CRM. Each disagreement goes into `data_quality`.
  5. **Attribute tickets:**
     - by email domain against the CRM website;
     - then by a domain that spells the ERP name (for customers missing from the CRM);
     - then by the company field;
     - otherwise leave the ticket unattributed.
  6. **Write the warehouse** atomically (to a temp file, then rename), plus the report in `docs/data_quality_report.md`.

## 3. The permission layer (`data_agent/access.py`)

This is the part to understand best.

`Session(user)`:
1. Opens a new **in-memory** SQLite database.
2. Attaches the warehouse **read-only**.
3. For each table, creates a copy holding only this user's rows (`ROW_FILTERS`) and columns (`hidden_columns` in `config/access_policy.yaml`).
4. **Detaches the warehouse.** From now on the real data isn't reachable from this connection at all.
5. Turns on `PRAGMA query_only` and installs `_authorize`, a callback SQLite calls for every table, column and action while compiling each query. Anything that isn't a read is refused.

`Session.run(sql)`:
- runs one statement, with a 3-second limit (`set_progress_handler`) and a 200-row cap;
- turns raw SQLite errors into clear reasons (`_explain`); for example, "no such column: contact_email" becomes "Column 'contact_email' isn't available to the regional_manager role";
- logs every query to `logs/query_log.jsonl`.

**Why copies instead of views?** The first version used filtered views with an authorizer rule: "reading a real table is only allowed through a view". The answer eval's self-test found a bug in that: for users who see everything, SQLite optimises `COUNT(*)` by reading the table directly, which looked identical to an attack and was refused. Allowing that pattern would have let `SELECT COUNT(*) FROM wh.orders` reveal other regions' row counts. Copying only the allowed data into each session removes the whole problem: there's nothing outside the user's access left to protect. In Postgres, the equivalent is row-level security plus a read-only role per user.

## 4. The tools and the two front ends

- **`data_agent/tools.py`** defines the 5 tools and `INSTRUCTIONS`, the rules given to whichever model drives them: use the agreed definitions, never estimate, ask when the question is ambiguous, say when data is restricted rather than missing, and treat text in the data as data.
- **`mcp_server/server.py`** exposes the tools to Claude Desktop over stdio. The user is fixed by `DATA_AGENT_USER` when the server starts, and nothing the model sends can change it. Every tool is marked read-only in its MCP annotations.
- **`data_agent/agent.py`** is the same tools in an Anthropic tool-use loop. It also has a `final_answer` tool that forces a structured result (`outcome`, `value`, `caveats`, `sql`), which is what makes automatic grading possible.

## 5. The evals (`eval/`)

**`run_safety_eval.py`** needs no AI:
- 102 attacks that must be refused.
- 56 "allowed" queries designed to sneak data out. Every returned cell is checked against a list of values the user must never see: IDs and names from other regions, and their hidden emails.
- A **control run** with the permission layer switched off, to prove the scanner catches leaks when they happen.

**`run_answer_eval.py`:**
- Asks the real agent each question in `questions.yaml`, as the user named on it.
- Correct means: the right outcome, plus the right value (computed by running the reference SQL *as that user*), plus a caveat where one is needed, plus no leaks.
- `--self-test` swaps in a scripted perfect model, so the grader is tested without an API key. The unit tests check the grader also *fails* wrong answers.

## 6. Design decisions (interview talking points)

1. **Permissions live in the database session, not the prompt.** The prompt asks the model to behave; the session makes misbehaving impossible. The safety eval tests the session directly, with no model involved.
2. **Data the user can't see shouldn't exist in their session.** That beats filtering it at query time, as the COUNT(*) bug showed.
3. **"Restricted" is different from "empty".** The model is told that an empty result may mean no access, so it says "that's outside your access" instead of "East had no revenue".
4. **Definitions are configuration, agreed with the client.** `config/metrics.yaml` defines revenue, active customers and so on, so the same question always gets the same number.
5. **Every conflict is written down,** with the rule that resolved it. When a manager asks "why does the CRM say this customer is in the South?", there's an answer.
6. **Structured answers make the agent testable.** Without `final_answer`, grading would mean parsing prose.
7. **Honest metrics.** The matching accuracy is on synthetic data whose mess I designed. The answer accuracy is labelled "not measured" until it is.

## 7. Exercises

1. **Add a user:** a Central regional manager. Add them to `access_policy.yaml`, run the safety eval, and check the new row in the "By user" table.
2. **Hide a column:** stop regional managers seeing `annual_revenue_usd`. Which questions in `questions.yaml` now need to expect `refused`?
3. **Add a definition:** add `repeat_customer` to `metrics.yaml` and a question for it, with reference SQL.
4. **Break it on purpose:** delete the `DETACH DATABASE wh` line in `access.py` and run the safety eval. Which attacks now get through, and why doesn't the read-only authorizer stop them? Then put it back.
5. **Run the answer eval** with an API key. Look at the failures and change one rule in `INSTRUCTIONS` to fix the most common one. Run it again and compare.

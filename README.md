# Tejaskrit — magicpin Vera AI Challenge Bot

Final corrected FastAPI bot for the magicpin Vera AI Challenge.

## Endpoints

```text
GET  /v1/healthz
GET  /v1/metadata
POST /v1/context
POST /v1/tick
POST /v1/reply
POST /v1/teardown   # optional cleanup for local tests
```

Non-`/v1` aliases are also included for convenience, but submit the base URL only.

## What was fixed after the 40/100 evaluation

- `/v1/reply` now branches by `from_role`, intent, and conversation state.
- Customer replies such as `Yes please book me for Wed 5 Nov, 6pm` are treated as slot/booking confirmations, not merchant draft approvals.
- WhatsApp Business auto-replies are detected statefully across repeated canned messages: first response = one owner-facing prompt, second = wait, third+ = end.
- Trigger coverage is stronger: the bot trusts `available_triggers`, supports trigger IDs that reference merchant/customer in either top-level fields or `payload`, and includes fallback composition for new/hidden trigger kinds.
- Trigger-specific reply grounding was added for regulation/compliance, research/CDE, performance, review, competitor, and planning intents.
- Compositions now use more context facts: source citations, dates, deadlines, performance metrics, peer CTR, locality, offer, customer state, slots, and language preference.
- Added a pure `compose(category, merchant, trigger, customer=None)` function for any offline/module scorer.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Check:

```bash
curl http://localhost:8080/v1/healthz
curl http://localhost:8080/v1/metadata
```

## Render deployment

Use this start command:

```bash
uvicorn bot:app --host 0.0.0.0 --port $PORT
```

Recommended environment variables:

```text
TEAM_NAME=Tejaskrit
CONTACT_EMAIL=dixitkrishna7777@gmail.com
USE_GEMINI_POLISH=false
```

Keep `USE_GEMINI_POLISH=false` for the final attempt. The deterministic router is faster, stable, and avoids hallucinated facts.

## Local recheck commands used

```bash
python -m py_compile bot.py
uvicorn bot:app --host 127.0.0.1 --port 18083
python judge_simulator.py   # official simulator imported with a dummy scoring provider for endpoint/replay checks
```

Passed locally:

```text
Warmup: PASS
Context push: PASS
Auto-reply replay: PASS; ended on repeated auto-reply
Intent transition: PASS; switched to action mode
Hostile/STOP handling: PASS; ended conversation
Seed trigger coverage: 25/25 returned non-empty actions when contexts were present
Expanded dataset schema check: 100/100 triggers produced valid schema or intentionally skipped only no-reminder-opt-in customer triggers
```

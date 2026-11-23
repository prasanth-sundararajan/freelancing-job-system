# Agentic Freelancing System

Agents find Freelancer.com jobs, screen and price them, write proposals, draft
client replies, and build and test the work for won jobs, with hard guardrails
in code. Agent behaviour lives in [skills.md](skills.md) (Role, Task, Guardrails,
Context, Few-shots, Response); each agent's system prompt is built from its section.

**You stay in control of everything outbound.** Freelancer prohibits automatic
bidders, so you submit bids yourself; client replies and deliveries also wait
for your approval.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\set_key.py DEEPSEEK_API_KEY          # or ANTHROPIC_API_KEY + provider: anthropic
.\.venv\Scripts\python.exe scripts\set_key.py FREELANCER_OAUTH_TOKEN    # accounts.freelancer.com/settings/develop (30 days)
```

## Daily workflow

All commands: `$env:PYTHONPATH = "src"; .\.venv\Scripts\python.exe -m freelance_agents.cli <command>`

| Step | Command | What happens |
|---|---|---|
| Automatic, every 3 hours | (scheduled task) | `scout --freelancer`, `run`, `inbox`; log in `data/logs/` |
| Review bids | `queue` | Proposals with links; submit the good ones on freelancer.com |
| Record a bid | `approve <job_id>` / `reject <job_id>` | Marks it submitted or skipped |
| Client replied | `replies` → `send-reply <id>` | Review the draft; send it (or `--text "your words"`) |
| You won | `start-project <job_id> --notes "agreed scope"` | Records the contract |
| Build it | `build <job_id>` | Planner → Builder → QA (tests, up to 3 rounds) → package |
| Deliver | `deliveries` | Zip + handoff notes in `data/deliveries/`; upload to the client |
| Check | `status`, `show <job_id>` | States, costs, every agent output |

Scheduled task: `scripts\schedule_task.py` (`--hours N`, `--remove`).

### Ops agent (self-healing, every 15 minutes)

`scripts\watchdog.py`, scheduled with `scripts\schedule_task.py --watchdog`. It checks the
bot heartbeat, the bot's Chrome and Freelancer sign-in, the scheduled task, missed or failed
cycles, the Freelancer token, the DeepSeek balance and stuck jobs, and diagnoses unknown log
errors with the model. It fixes only from an allowlist (restart bot/Chrome, re-create the
task, re-run a cycle, retry a job once after a temporary error, stop a stuck job), at most once
per hour per fix; everything else becomes a Telegram alert. It never edits code, config or
credentials, never bids, never messages clients. Log: `data/logs/watchdog.log`.

## Safety defaults

- `dry_run: true`: bids and messages are logged, not sent. Set `false` to let `send-reply` and `approve --submit` actually post.
- `kill_switch: true` stops everything.
- Guardrails in `src/freelance_agents/guardrails.py` run on every model output (contact details, discounts, scope changes, currency, margin, QA verdicts, secrets, state transitions).
- Builder sandbox (`sandbox.py`): project folder only, `python`/`pytest`/allowlisted `pip install` only, no API keys in its environment, time limits. **Without Docker there's no network or CPU/memory isolation.**

## Layout

| File | Purpose |
|---|---|
| `skills.md` | Agent definitions |
| `config.yaml` | Thresholds, search, models, prices, sandbox, safety switches |
| `src/freelance_agents/agents.py` | The 12 agents (Builder uses tools) |
| `src/freelance_agents/orchestrator.py` | Lead pipeline: discovered → … → awaiting_approval |
| `src/freelance_agents/inbox.py` | Client messages: drafts and human-approved sends |
| `src/freelance_agents/delivery.py` | Won jobs: plan → build → QA → package |
| `src/freelance_agents/sandbox.py` | Confined workspace for Builder/QA |
| `src/freelance_agents/freelancer.py` | Freelancer.com API (search, messages, single approved bids) |
| `src/freelance_agents/guardrails.py` | Code-enforced guardrails |
| `src/freelance_agents/llm.py` | DeepSeek / Claude calls, structured output, tool loop, cost tracking |
| `scripts/` | `run_cycle.py`, `schedule_task.py`, `set_key.py`, `update_profile_skills.py` |
| `profile/` | Freelancer profile and portfolio texts, backups |

## Not done yet

- Win detection: you run `start-project` when a client awards you (award status isn't read from the API yet).
- Uploading deliveries and milestone requests through the API (you upload the zip yourself).
- Docker sandbox, plagiarism check, Finance agent reports, dashboard.

Tests: `.\.venv\Scripts\python.exe -m pytest -q` (no API keys needed).

# arc-voice-agents

Voice agents for alarm receiving centres (ARCs), built like production software: every agent is a versioned spec in git, every change runs through tests and scenario evals, and anything that can dispatch police or the fire service sits behind a guarded tool, not a prompt.

An alarm fires, the receiver posts a Contact ID signal, the right agent calls the keyholder, verifies them, works out what happened and ends the call with an auditable disposition.

```
alarm panel ──► receiver ──► POST /v1/signals ──► route by category ──► agent spec (YAML, versioned)
                (Contact ID,    HMAC signed,                              │
                 SIA DC-05)     idempotent                               ▼
                                                   voice platform ◄── POST /v1/incidents/{id}/turns
                                                  (STT / TTS / SIP)       │   LLM picks an intent,
                                                                          │   the spec picks the next step
                                                                          ▼
                                                                 guarded tools ──► police / fire / keyholder SMS /
                                                                                    engineer / human operator queue
```

## Why it is built this way

**Agents as code.** Each agent in [`agents/`](agents) shares one shape: persona, system prompt, allowed tools, a step graph and the dispositions it can end in. A prompt change is a reviewed diff with a semver bump, and an incident records which agent version handled it.

**The spec is checked before it ships.** `arcvoice validate` rejects specs with unknown steps, undeclared tools, unhandled intents, unreachable steps, unknown dispositions, or any step that can never reach the end of a call.

**The LLM classifies, it does not decide.** On each turn the model picks one intent from the short list the current step allows (JSON schema output, temperature 0, 4 second timeout). Anything else becomes `unclear`, and the step asks again or hands off to a human. Branching, tool calls and dispositions come from the spec, so every path is testable.

**Guardrails live in tools.** `request_police_dispatch` refuses unless there are grounds an ARC could defend (keyholder confirmed intrusion, duress code, or a panic signal). No prompt can talk the agent past it.

**Safety defaults per alarm type.** Intruder calls hand off to a human when in doubt. Fire calls do the opposite: an unclear answer, or a "no fire" that cannot be verified with the site password, calls the fire service.

**Duress handling.** If a keyholder gives the duress word instead of the password, the agent carries on as if all is fine, closes the call politely, and silently requests police.

**Privacy.** Passcodes are redacted from the stored transcript and never written to the tool log. The site address and zone are only read out after verification.

## Quick start

```bash
pip install -e ".[dev]"

arcvoice validate                       # check every agent spec
arcvoice eval                           # run the scenario evals (offline, deterministic)
arcvoice chat --event 130 --zone 003    # talk to the intruder agent in your terminal
arcvoice chat --event 111 --zone 005    # fire agent
pytest -q                               # unit, engine and API tests
```

Run the API with Postgres:

```bash
docker compose up --build
python scripts/send_signal.py --account 1234 --event 130 --zone 001

curl -s -X POST localhost:8080/v1/incidents/<id>/turns \
  -H "Authorization: Bearer dev-tools-token" -H "content-type: application/json" \
  -d '{"utterance": "yes, speaking"}'
```

Demo sites: account `1234` (password `bluebell`, duress `sunflower`) and `5678` (password `granite`, duress `willow`).

Set `ARC_LLM_API_KEY` (and optionally `ARC_LLM_BASE_URL`, `ARC_LLM_MODEL`) to classify with any OpenAI compatible model instead of the offline keyword classifier.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/signals` | Receiver webhook. Verifies `X-Signature: sha256=...`, parses and checksums the Contact ID frame, ignores restores, tests and open/close reports, deduplicates retransmissions, opens an incident and returns the agent's first line. |
| POST | `/v1/incidents/{id}/turns` | One caller utterance in, one agent reply out, plus any actions taken. This is the hook for a voice platform (ElevenLabs, Twilio, a SIP gateway) that handles speech. |
| GET | `/v1/incidents/{id}` | State, transcript, disposition and audit trail. |
| POST | `/v1/tools/{name}` | Server tool endpoint for hosted voice agents that call tools directly. Only tools declared by the incident's agent are allowed. |
| GET | `/healthz`, `/readyz`, `/metrics` | Liveness, readiness (DB check and loaded agent versions), Prometheus counters. |

Logs are structured JSON with a request id on every line, ready for Cloud Logging.

## Evals

Scenarios in [`evals/scenarios/`](evals/scenarios) are scripted calls with expected outcomes: disposition, the exact set of actions taken, phrases the agent must say, and phrases it must never say (for example, the address before verification, or the word "police" during a duress call).

```
PASS duress word triggers silent police dispatch
PASS no site details before the caller is verified
PASS unclear answer on a fire call defaults to dispatch
...
12/12 passed (100%), gate 100%
```

CI runs them offline on every PR and requires 100%. With a model key configured, the same scenarios run against the live model with a 90% gate, which is how a prompt or model change gets promoted.

## CI/CD and infrastructure

* [`ci.yml`](.github/workflows/ci.yml): ruff, mypy (strict), spec validation, tests on SQLite, API tests on a real Postgres service, offline evals, then a container build and smoke test against `/readyz`.
* [`deploy.yml`](.github/workflows/deploy.yml): build once, push to Artifact Registry with keyless auth (Workload Identity Federation), deploy to **dev** with no traffic, smoke test the tagged revision, shift traffic. **Prod** waits for a manual approval, runs a 10% canary, then rolls out. Rollback is one traffic command to the previous revision.
* [`infra/terraform`](infra/terraform): Cloud Run service per environment, runtime service account, Secret Manager secrets, minimum instances in prod so a live alarm never hits a cold start, and a 5xx alert policy.

## Layout

```
agents/                 versioned agent specs (YAML)
evals/scenarios/        scripted calls with expected outcomes
src/arcvoice/
  contact_id.py         Contact ID (SIA DC-05) parser with checksum validation
  spec.py               AgentSpec schema, graph validation, YAML 1.2 loader
  engine.py             turn based conversation runtime, state is plain JSON
  llm.py                intent classifiers (offline keyword, OpenAI compatible)
  tools.py              guarded tools: dispatch, keyholder SMS, engineer, handoff
  store.py              SQLAlchemy Core, SQLite for dev, Postgres in prod
  api.py                FastAPI service
  observability.py      JSON logging, request ids, metrics
  evals.py, cli.py
infra/terraform/        GCP: Cloud Run, Secret Manager, alerting
.github/workflows/      CI and promotion pipeline
```

## A small thing worth knowing

PyYAML follows YAML 1.1, where `on`, `yes` and `no` are booleans. In a spec full of `on:` transitions and callers saying "yes", that silently corrupts the graph. The first version of this repo hit exactly that, and the spec validator caught it before a single test ran. The loader now uses YAML 1.2 booleans, with a test to keep it that way.

## Status

A reference implementation I built to explore how voice agents for alarm monitoring can be standardised, tested and deployed safely. The demo sites, codes and phone numbers are fake. It is not affiliated with any ARC or vendor.

MIT licensed.

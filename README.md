# industrial-agent-evals

Evals for agents that answer operator questions about industrial and OT data: tags, trends, alarms, and events. This repository is the trace-collection base. It records full runs so a domain expert can review them. It does not score answers.

## Method

The order of work is error analysis first, following [Hamel Husain's evals FAQ](https://hamelhusain.substack.com/p/evals-faq) and the [evals-skills](https://github.com/hamelsmu/evals-skills) notes:

1. Collect traces of the assistant doing the task. That is this repository.
2. Open coding. One domain expert reads each trace and writes pass or fail, plus a note on the first failure. `review-traces` stores that in `annotations/open_codes.csv`.
3. Axial coding. Group the notes into a failure taxonomy and count them.
4. Code checks. Turn the common, objective failures into deterministic checks.
5. An LLM judge only where a code check cannot do the job, and only after the judge is aligned to the human labels.

Most of the effort is reading traces. Automated checks come after the failures are known. A judge that has not been aligned to a domain expert does not decide quality.

There are no expected answers in `questions/seed.yaml`. Writing them up front skips the step that shows how the assistant actually fails.

## Plant

`data/` is a seeded historian for a small Riverside cooling-water plant:

- P-101A / P-101B pump station
- E-201 heat exchanger
- TK-301 basin and the cooling-water header
- 20 tags (flow, pressure, temperature, level, valve position, run status, motor current)
- 1 March 2026 00:00 UTC through 4 March 2026 00:00 UTC, one sample every 5 minutes
- an alarm log with priority, return to normal, and acknowledgement
- an operator event log

The series has a quiet first day, a pump trip and alarm flood on the morning of 2 March (bearing temperature rises, then flow is lost, then the standby pump starts), a slow upward drift in E-201 process outlet temperature, a bypass valve that drifts open on the morning of 3 March, and a chattering basin-level alarm that afternoon.

Files: `tags.csv`, `history.csv`, `alarms.csv`, `events.csv`. Rewrite them with the same seed:

```bash
uv run generate-plant
```

The seed is 42.

## Quickstart

```bash
uv sync --group dev
uv run pytest
uv run collect-traces --fake
uv run review-traces
```

`--fake` uses a deterministic stub client, so tests and CI need no API key. Traces worth reviewing come from a real model. Run `uv run collect-traces` after setting the variables below.

`pip install -e ".[dev]"` works if you are not using uv. The commands are `generate-plant`, `collect-traces`, and `review-traces`.

## Model configuration

The assistant calls an OpenAI-compatible chat completions endpoint.

| Variable | Role |
| --- | --- |
| `OPENAI_BASE_URL` | API root. Default `https://api.openai.com/v1`. |
| `OPENAI_API_KEY` | Bearer token. Required unless `--fake`. |
| `OPENAI_MODEL` | Model name. Required unless `--fake`. |

```bash
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4.1-mini
uv run collect-traces
```

Temperature is sent as 0. The key is not written into the trace.

## Tools

The assistant has four tools. Windows are UTC, start inclusive, end exclusive (`2026-03-02T06:00:00Z`).

- `search_tags` — tag name, description, engineering unit, range, alarm limits
- `read_history` — samples for one tag in a window (downsampled past 48 points)
- `tag_stats` — count, mean, min, max, standard deviation, slope per hour
- `query_alarms` — alarms that overlap a window, with an onset-only filter, acknowledgement fields, and optional operator events

## Questions

`questions/seed.yaml` holds 30 questions. Each has an `id` and a `category`.

| Category | What it asks |
| --- | --- |
| `tag_lookup` | Find a tag, its unit, range, or alarm limits. |
| `trend_root_cause` | What a tag did across a window, including the trip and the drift. |
| `alarm_triage` | Floods, acknowledgement delay, chattering, priority. |
| `unit_conversion` | Report a tool value in another unit and state the factor. |
| `ambiguous_unanswerable` | The historian cannot answer. The assistant should say it does not know. |
| `multi_step` | More than one tag or window, including which event came first. |

`collect-traces --ids q01,q07` runs a subset.

## Traces

Each run writes `traces/<id>.json`:

- question id, category, and text
- model name
- start time and duration
- every message (system, user, assistant, tool)
- each tool call with arguments, result, and duration
- final answer
- error string, if the call failed

A failed call is still written, so the failure can be reviewed. `collect-traces` exits non-zero if any trace has an error.

## Open coding

```bash
uv run review-traces
```

The script prints one unannotated trace at a time. Mark it pass or fail. On a fail, write what the first failure was. The row is appended to `annotations/open_codes.csv` (or updated if that question id is already there). Pass rows store an empty note.

```bash
uv run review-traces --list
uv run review-traces --trace traces/q07.json --full
uv run review-traces --trace traces/q07.json --verdict fail --note "Reported flow in the wrong unit."
```

Read the first failure. A wrong tool argument early in the trace makes the final sentence wrong too, and coding only the sentence hides the cause.

## Tests

`pytest` checks that the seed is stable, that the plant has a flood and a chattering alarm, that the tools respect time windows, and that `--fake` traces for the seed questions have the fields above. The GitHub Actions workflow runs those tests and `collect-traces --fake`.

## Layout

```
plant_evals/     assistant, tools, plant writer, trace collector, review script
data/            tags, history, alarms, events
questions/       seed questions
traces/          one JSON file per run (not checked in)
annotations/     open_codes.csv
tests/
```

## Roadmap

1. Traces and open coding. This step. Review the 30 seed questions on a real model.
2. Keep reviewing fresh traces until new ones stop showing new failure modes. A hundred traces is a reasonable first target.
3. Axial coding: a failure taxonomy with a count on each mode.
4. Deterministic checks for the objective failures (unknown tag, wrong unit factor, missed an unacknowledged urgent alarm, answered a question the historian cannot answer).
5. Align an LLM judge to the human labels only for checks that code cannot express. Hold out labels. Do not tune on the test split.
6. Repeat open coding when the prompt, tools, or model change. The checks drift when the task drifts.

## License

MIT.

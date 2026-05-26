# Frontend mode loop fix

Date: 2026-05-26

## Context

The web UI still exposed four mode entries and could carry the frontend-only
`learning` mode into `/sessions/{id}/chat`, while the backend chat request model
only accepts runtime `AgentMode` values such as `chat`.

## Changes

- Reduced the home mode cards and bottom mode toolbar to two product entries:
  `闲谈` and `研习`.
- Removed visible Ask / Study entry points and labels from the web UI.
- Mapped the frontend `研习` state to backend `chat` when sending messages; the
  learning-unit session metadata now remains the source of truth for internal
  alignment / teach turns.
- Kept loaded sessions semantically stable by deriving the frontend mode from
  `session.learning_unit_id` instead of raw runtime `session.mode`.
- Recovered automatically from active-unit conflicts by loading the existing
  unfinished learning unit and selecting its session instead of showing a dead
  `close it first` error.
- Renamed visible learning-unit copy to `研习卷` for frontend consistency.
- Added static frontend regression tests for the two-entry UI and the
  `研习 -> chat` request mapping.
- Added a regression test for the active-unit recovery path.

## Verification

- `node --check web/static/app.js`
- `node --check web/static/learning-unit-ui.js`
- `node --test tests/test_web_static_app.js`
- `pytest -q`
- `curl -fsS http://127.0.0.1:8000/health`
- `curl -fsS http://127.0.0.1:8000/ui/index.html`

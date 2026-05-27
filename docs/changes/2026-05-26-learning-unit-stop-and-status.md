# Learning unit stop and status fix

Date: 2026-05-26

## Context

The learning-unit MVP requires either a minimum validation loop or an explicit
user stop action. The implementation only treated `consolidated` as terminal,
so any unfinished learning unit could permanently block creation of another
unit.

The frontend also made learning mode hard to perceive once the user scrolled
into the message history.

## Changes

- Added `stopped` as a terminal learning-unit phase that releases the active
  unit invariant without counting as a completed / consolidated unit.
- Added `stop_reason` and `stopped_at` fields to learning units.
- Added `LearningAgentSystem.stop_learning_unit()`.
- Added `POST /learning-units/{unit_id}/stop`.
- Added `learning_unit.stopped` product event, plus a phase transition event.
- Added a `先学到这里` action in the learning-unit card.
- Added a stopped-state notice in the learning-unit card.
- Added topbar learning-state updates via `learning-unit:state`.
- Changed active-unit conflict recovery so a new topic is not automatically sent
  into the existing unfinished unit.
- Added regression coverage for state transitions, active-unit lookup, API,
  events, and frontend static behavior.

## Verification

- `node --check web/static/app.js && node --check web/static/learning-unit-ui.js`
- `node --test tests/test_web_static_app.js`
- `pytest tests/test_mode_layering.py tests/test_learning_unit_store.py tests/test_learning_unit_api.py tests/test_learning_unit_events.py -q`
- `pytest -q`

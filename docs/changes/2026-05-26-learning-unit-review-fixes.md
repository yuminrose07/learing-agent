# Learning Unit review fixes

Date: 2026-05-26

## Context

Fix issues found while reviewing commits from `79e2e9478fc761f136d6420876b6ddbf24d4184d` to `HEAD`.

## Changes

- Fixed `GET /learning-units/metrics` route ordering so it is not shadowed by `GET /learning-units/{unit_id}`.
- Changed the web learning-mode creation source from invalid `web_welcome` to the existing `user_written` source.
- Added outputting-stage question rendering to the learning-unit card, including question progress and consolidated feedback card details.
- Forwarded TEACH SSE metadata (`question_index`, `question_total`, `verdict`, `feedback_card`) to the frontend.
- Adjusted windowed consolidation and teach-entry metrics so numerator units must also be in the window-created denominator cohort.
- Preserved one ASK turn for legacy `phase=aligning` units projected to `alignment_state=active`.

## Verification

- `pytest tests/test_learning_unit_api.py tests/test_learning_unit_metrics.py tests/test_mode_layering.py tests/test_learning_unit_acceptance.py -q`
- `node --check web/static/learning-unit-ui.js`

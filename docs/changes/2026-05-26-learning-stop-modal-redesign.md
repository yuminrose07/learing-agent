# Learning stop modal redesign

Date: 2026-05-26

## Context

The learning-unit stop flow ended with a small bottom toast plus an immediate
return path, which made the completion feedback easy to miss. The toast also
sat close to the input area, so the end-of-unit state looked cramped and
unfinished.

## Changes

- Replaced the stop toast with a dedicated centered completion modal for the
  learning flow.
- Added explicit follow-up actions: stay on the finished unit to review, or
  start a new topic.
- Passed the current learning objective through the `learning-unit:stopped`
  event so the modal can summarize which unit just ended.
- Refined the inline stopped-state note to feel calmer and less visually harsh
  after the modal is dismissed.
- Added static regression coverage for the new modal entry points and stop event
  payload.

## Verification

- `node --check web/static/app.js`
- `node --check web/static/learning-unit-ui.js`
- `node --test tests/test_web_static_app.js`

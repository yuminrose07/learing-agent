# Debug Session: web-search-turn-stuck

Status: [OPEN]

## Symptom

- Frontend Chat turn can trigger `web_search` / `web_fetch`.
- SSL issue is fixed, and tool execution can complete.
- The assistant final message does not reliably land in session messages / UI.
- The turn appears to keep running or stalls after tool execution.

## Scope

- Runtime turn completion
- Tool loop stop condition
- SSE / session message persistence handoff

## Hypotheses

1. Tool calls complete, but the model keeps requesting more tools because the tool results are not summarized or bounded correctly.
2. The assistant final message is generated, but persistence into session state fails after streaming/tool events.
3. SSE streaming finishes abnormally, so the UI never receives the terminal assistant payload even though runtime progressed.
4. A post-tool error path marks the turn as unfinished, leaving runtime state locked or pending.
5. Study/Chat shared front-end state is reusing stale session data, masking a completed backend turn as an unfinished UI turn.

## Evidence Log

- `sess-dec0178b` in backend logs:
  - multiple cycles of `streaming -> executing_tool -> building_context -> calling_llm -> streaming`
  - final state reached `streaming -> completed`
- `/sessions/sess-dec0178b/events?limit=400` shows:
  - several assistant `message_end` events with `tool_calls=True` and empty content
  - `tool.exec_completed web_search`
  - multiple `tool.exec_completed web_fetch`
  - final assistant `message_end` with real content and `tool_calls=False`
- `/sessions/sess-dec0178b` shows 2 UI messages:
  - user message
  - final assistant answer persisted successfully
- Frontend browser snapshot now shows:
  - final assistant answer rendered
  - source links rendered
- Residual browser console errors:
  - `GET /health` had one `ERR_CONNECTION_REFUSED` during manual server restart
  - `POST /sessions/sess-dec0178b/chat` showed `ERR_ABORTED`, but the final answer still rendered and persisted
- Detailed tool sequence for `sess-dec0178b`:
  - assistant first called `web_search(query="FastAPI StreamingResponse official documentation site:fastapi.tiangolo.com", source_preferences=["official_docs"], top_k=5)`
  - search returned FastAPI official pages, but top results were classified as `blog` instead of stable `official_docs`
  - assistant then called two parallel fetches at `offset=0`:
    - `stream-data`
    - `custom-response`
  - both fetches returned very noisy full-page text including navigation / locale / menu content
  - assistant then called the same two pages again with `offset=12000`
  - assistant then called `stream-data` again with `offset=24000`
  - the last paginated fetch failed twice with `UNEXPECTED_EOF_WHILE_READING`
  - after these reads, assistant produced the final answer and persisted it successfully

## Hypothesis Status

1. Tool calls complete, but the model keeps requesting more tools because the tool results are not summarized or bounded correctly.
   - Supported. The model made paginated `web_fetch` calls (`offset=0 -> 12000 -> 24000`) before producing the final answer.
2. The assistant final message is generated, but persistence into session state fails after streaming/tool events.
   - Rejected. Final assistant message persisted and is visible via `/sessions/{id}`.
3. SSE streaming finishes abnormally, so the UI never receives the terminal assistant payload even though runtime progressed.
   - Rejected for the verified run. Final answer rendered in UI after the turn completed.
4. A post-tool error path marks the turn as unfinished, leaving runtime state locked or pending.
   - Rejected for the verified run. Runtime reached `completed`.
5. Study/Chat shared front-end state is reusing stale session data, masking a completed backend turn as an unfinished UI turn.
   - Not the root cause for the verified Chat run. Study flow still needs separate validation.

## Refined Diagnosis

- The main issue is not execution correctness but evidence acquisition cost.
- Current `web_fetch` output is too noisy and page-like; it does not isolate the most relevant正文区域 early enough.
- Because fetched pages are long and truncated by character window, the model chooses paginated continuation to gain confidence before answering.
- Source classification also weakens confidence:
  - FastAPI official docs pages are currently often labeled `blog`, not `official_docs`.
- Result: the model behaves conservatively and spends extra turns fetching more evidence before synthesis.

## Next Step

- If optimization is needed, focus on reducing excessive `web_fetch` loops rather than fixing persistence/SSE correctness.

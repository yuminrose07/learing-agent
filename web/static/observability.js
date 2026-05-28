/**
 * Learning-Agent · L3 Session Events viewer
 *
 * 单页 vanilla JS。从 GET /sessions 拿会话列表，选中一个后调
 * GET /sessions/{id}/events 一次性拉全量事件，前端做 visibility / type
 * 过滤。无 SSE、无构建步骤、无前端框架。
 */

const API_BASE = typeof window !== 'undefined'
    && window.location
    && typeof window.location.origin === 'string'
    && window.location.origin.startsWith('http')
    ? window.location.origin
    : '';

// 截断长 UUID 用的展示长度；事件 / parent / sessionId 用同一规则保持一致。
const ID_DISPLAY_PREFIX = 12;

const state = {
    sessions: [],
    currentSessionId: null,
    events: [],
    eventBySeq: new Map(),
    eventIdToSeq: new Map(),
    visibilityFilter: new Set(['agent', 'system', 'observability', 'ui']),
    typeFilter: '',
    activeTab: 'conversation',
    showTechDetails: false,
    learningUnitWindowDays: 7,
    learningUnitMetrics: null,
    learningUnitMetricsLoaded: false,
};

const els = {
    sessionList: document.getElementById('session-list'),
    currentSession: document.getElementById('current-session'),
    statsText: document.getElementById('stats-text'),
    timeline: document.getElementById('timeline'),
    typeInput: document.getElementById('type-filter'),
    btnRefresh: document.getElementById('btn-refresh'),
    visChips: document.querySelectorAll('.vis-chip'),
    tabs: document.querySelectorAll('.tab'),
    tabPanels: document.querySelectorAll('.tab-panel'),
    // 对话回放 tab
    conversation: document.getElementById('conversation'),
    convCurrentSession: document.getElementById('conv-current-session'),
    convStatsText: document.getElementById('conv-stats-text'),
    convBtnRefresh: document.getElementById('conv-btn-refresh'),
    convShowTech: document.getElementById('conv-show-tech'),
    // 学习卷指标
    luMetricsGrid: document.getElementById('lu-metrics-grid'),
    luWindowSelect: document.getElementById('lu-window-select'),
    luBtnRefresh: document.getElementById('lu-btn-refresh'),
    luStatsText: document.getElementById('lu-stats-text'),
    luDiagnostics: document.getElementById('lu-diagnostics'),
};

// ─── API ───

async function apiGet(path) {
    const res = await fetch(API_BASE + path, { headers: { 'Content-Type': 'application/json' } });
    if (!res.ok) {
        const detail = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    return res.json();
}

// ─── Sessions ───

async function loadSessions() {
    try {
        const sessions = await apiGet('/sessions');
        state.sessions = Array.isArray(sessions) ? sessions : [];
        renderSessionList();
    } catch (err) {
        els.sessionList.innerHTML = `<div class="session-empty is-error">加载会话失败: ${escapeHtml(err.message)}</div>`;
    }
}

function renderSessionList() {
    if (state.sessions.length === 0) {
        els.sessionList.innerHTML = '<div class="session-empty">暂无会话</div>';
        return;
    }
    const html = state.sessions.map((s) => {
        const sid = s.id || s.session_id || '';
        const title = s.title || '(untitled)';
        const isActive = sid === state.currentSessionId;
        return `
            <div class="session-row ${isActive ? 'active' : ''}" data-session-id="${escapeAttr(sid)}" title="${escapeAttr(sid)}">
                <div class="session-id">${escapeHtml(shortId(sid))}</div>
                <div class="session-title">${escapeHtml(title)}</div>
            </div>
        `;
    }).join('');
    els.sessionList.innerHTML = html;
    els.sessionList.querySelectorAll('.session-row').forEach((row) => {
        row.addEventListener('click', () => {
            const sid = row.getAttribute('data-session-id');
            selectSession(sid);
        });
    });
}

async function selectSession(sessionId) {
    if (!sessionId) return;
    state.currentSessionId = sessionId;
    renderSessionList();
    els.currentSession.textContent = shortId(sessionId);
    els.currentSession.title = sessionId;
    els.convCurrentSession.textContent = shortId(sessionId);
    els.convCurrentSession.title = sessionId;
    els.statsText.textContent = '加载中…';
    els.convStatsText.textContent = '加载中…';
    els.timeline.innerHTML = '<div class="empty">加载中…</div>';
    els.conversation.innerHTML = '<div class="empty">加载中…</div>';
    try {
        const data = await apiGet(`/sessions/${encodeURIComponent(sessionId)}/events?limit=5000`);
        state.events = Array.isArray(data.events) ? data.events : [];
        rebuildIndexes();
        renderTimeline();
        renderConversation();
    } catch (err) {
        els.timeline.innerHTML = `<div class="empty is-error">加载事件失败: ${escapeHtml(err.message)}</div>`;
        els.conversation.innerHTML = `<div class="empty is-error">加载事件失败: ${escapeHtml(err.message)}</div>`;
    }
}

function rebuildIndexes() {
    state.eventBySeq.clear();
    state.eventIdToSeq.clear();
    for (const e of state.events) {
        if (typeof e.seq === 'number') state.eventBySeq.set(e.seq, e);
        if (e.event_id && typeof e.seq === 'number') state.eventIdToSeq.set(e.event_id, e.seq);
    }
}

// ─── Filtering ───

function applyFilters(events) {
    const types = state.typeFilter.trim().toLowerCase();
    return events.filter((e) => {
        if (!state.visibilityFilter.has(e.visibility)) return false;
        if (types && !(e.type || '').toLowerCase().includes(types)) return false;
        return true;
    });
}

// ─── Conversation (人类视角回放) ───
//
// 把 L1 事件流降维成对话语义卡片。事件→卡片映射：
//   message.user_appended (entry.role=user)   →  用户气泡
//   message_end (entry.role=assistant)        →  assistant 气泡 (+token usage)
//   message_end (entry.role=tool)             →  对应 tool 卡片的 result 区
//   tool.exec_started + completed/failed 配对 →  一张 tool 卡片含耗时
//   message.stream_failed / interrupted       →  红色 banner
//   tool.call_failed metadata.synthetic        →  紫色 banner (orphan 补偿)
//   compaction.*                              →  蓝色窄条
//   session.created / message.assistant_started 等 → 默认不显示，"显示技术细节"打开才出
//
// 不做后端改动；纯前端转换。

const TECH_ONLY_TYPES = new Set([
    'session.created',
    'session.learning_unit_bound',
    'session.mode_changed',
    'session.title_updated',
    'session.status_changed',
    'message.assistant_started',
    'message.patch',
]);

function renderConversation() {
    const events = state.events || [];
    if (events.length === 0) {
        els.conversation.innerHTML = '<div class="empty">这个会话还没有任何事件</div>';
        els.convStatsText.textContent = '0 events';
        return;
    }
    const cards = buildConversationCards(events, { showTech: state.showTechDetails });
    if (cards.length === 0) {
        els.conversation.innerHTML = '<div class="empty">这个会话没有用户/助手/工具事件</div>';
        els.convStatsText.textContent = `${events.length} events`;
        return;
    }
    const stats = summarizeConversation(cards);
    els.convStatsText.textContent = stats;
    els.conversation.innerHTML = cards.map(renderCard).join('');
}

function buildConversationCards(events, opts) {
    // 1) 先把 tool.exec_started 按 call_id 聚到 _started[call_id]
    //    后续 completed/failed 配对成 tool 卡片
    // 2) 单次扫描按 seq 顺序构造卡片
    const cards = [];
    const startedByCallId = new Map();   // call_id -> { event, cardIndex }
    const showTech = opts && opts.showTech;

    for (const ev of events) {
        const t = ev.type || '';
        const payload = ev.payload || {};

        // 技术信号默认不显示
        if (!showTech && TECH_ONLY_TYPES.has(t)) {
            continue;
        }

        // ── 消息类 ──
        if (t === 'message.user_appended') {
            const entry = payload.entry || {};
            cards.push({
                kind: 'user',
                seq: ev.seq,
                ts: ev.ts || entry.timestamp,
                text: entry.content || '',
                metadata: entry.metadata || {},
            });
            continue;
        }
        if (t === 'message_end') {
            const entry = payload.entry || {};
            const role = entry.role || 'assistant';
            if (role === 'assistant') {
                cards.push({
                    kind: 'assistant',
                    seq: ev.seq,
                    ts: ev.ts || entry.timestamp,
                    text: entry.content || '',
                    metadata: entry.metadata || {},
                    reasoning: entry.metadata?.reasoning_content || '',
                    turnUsage: entry.metadata?.turn_usage || null,
                    hasToolCalls: !!entry.metadata?.has_tool_calls,
                });
            }
            // 注：tool 消息 L1 写的是 tool.call_completed/failed（不是 message_end），
            // 见 session_manager._event_type_for_entry。message_end 只来 assistant 行。
            continue;
        }

        // ── tool 消息持久化（session_manager 写的 L1 事件）──
        // 把 entry.tool_results 回填到上面对应 call_id 的 tool 卡里。
        if (t === 'tool.call_completed' || t === 'tool.call_failed') {
            const entry = payload.entry || {};
            const trs = entry.tool_results || [];
            const isError = t === 'tool.call_failed';
            for (const tr of trs) {
                const callId = tr.tool_call_id;
                const target = callId ? findToolCardByCallId(cards, callId) : null;
                if (target) {
                    target.persistedResult = tr.result;
                    target.persistedIsError = isError || !!tr.is_error;
                    target.toolMessageMeta = entry.metadata || {};
                    // synthetic orphan 补偿单独打 banner（前面已通过同类型事件 hit；
                    // 这里只是回填正常的 tool 卡 result）
                } else {
                    // 没找到对应 started（极端：started 漏发或 events 截断）
                    cards.push({
                        kind: 'tool',
                        seq: ev.seq,
                        ts: ev.ts || entry.timestamp,
                        toolId: tr.tool_id || '?',
                        callId: tr.tool_call_id || '',
                        args: null,
                        persistedResult: tr.result,
                        persistedIsError: isError || !!tr.is_error,
                        toolMessageMeta: entry.metadata || {},
                    });
                }
            }
            // synthetic orphan 补偿的 banner（不影响上面的回填）
            const meta = entry.metadata || {};
            if (t === 'tool.call_failed' && meta.synthetic) {
                cards.push({
                    kind: 'banner',
                    tone: 'warning',
                    seq: ev.seq,
                    ts: ev.ts,
                    title: '⊘ 孤儿工具调用 synthetic 补偿',
                    detail: `tool=${meta.tool_id || '?'} reason=${meta.reason || '?'}`,
                });
            }
            continue;
        }

        // ── 工具执行（observability visibility）──
        if (t === 'tool.exec_started') {
            const callId = payload.call_id;
            const card = {
                kind: 'tool',
                seq: ev.seq,
                ts: ev.ts,
                toolId: payload.tool_name || '?',
                callId: callId || '',
                args: payload.args || {},
                attempt: payload.attempt || 0,
                timeout: payload.timeout,
                latencyMs: null,
                execResult: null,
                execError: null,
                persistedResult: null,
                persistedIsError: false,
            };
            cards.push(card);
            if (callId) startedByCallId.set(callId, card);
            continue;
        }
        if (t === 'tool.exec_completed') {
            const callId = payload.call_id;
            const card = callId ? startedByCallId.get(callId) : null;
            if (card) {
                card.latencyMs = payload.latency_ms;
                card.execResult = payload.result;
                card.execTruncated = !!payload.result_truncated;
                card.execResultSize = payload.result_size;
            }
            continue;
        }
        if (t === 'tool.exec_failed') {
            const callId = payload.call_id;
            const card = callId ? startedByCallId.get(callId) : null;
            if (card) {
                card.latencyMs = payload.latency_ms;
                card.execError = `${payload.error_type || 'Error'}: ${payload.error_message || ''}`;
            }
            continue;
        }

        // ── 兜底 / 中断信号 ──
        if (t === 'message.stream_failed' || t === 'message.interrupted') {
            cards.push({
                kind: 'banner',
                tone: 'danger',
                seq: ev.seq,
                ts: ev.ts,
                title: t === 'message.stream_failed' ? '⚠ 流中断' : '⚠ 消息中断',
                detail: payload.reason || payload.error || payload.error_message || JSON.stringify(payload),
            });
            continue;
        }

        // ── compaction 压缩 ──
        if (t === 'compaction.summary_added' || t === 'compaction.anchor_moved' || t === 'compaction.rebase_completed') {
            cards.push({
                kind: 'compaction',
                seq: ev.seq,
                ts: ev.ts,
                type: t,
                payload,
            });
            continue;
        }

        // 默认：技术细节模式下显示，否则忽略
        if (showTech) {
            cards.push({
                kind: 'misc',
                seq: ev.seq,
                ts: ev.ts,
                type: t,
                visibility: ev.visibility,
                payload,
            });
        }
    }
    return cards;
}

function findToolCardByCallId(cards, callId) {
    if (!callId) return null;
    for (let i = cards.length - 1; i >= 0; i--) {
        const c = cards[i];
        if (c.kind === 'tool' && c.callId === callId) return c;
    }
    return null;
}

function summarizeConversation(cards) {
    const counts = { user: 0, assistant: 0, tool: 0, banner: 0 };
    let totalToolMs = 0;
    let totalPromptTokens = 0;
    let totalCompletionTokens = 0;
    for (const c of cards) {
        if (c.kind === 'user' || c.kind === 'assistant' || c.kind === 'tool' || c.kind === 'banner') {
            counts[c.kind]++;
        }
        if (c.kind === 'tool' && typeof c.latencyMs === 'number') totalToolMs += c.latencyMs;
        if (c.kind === 'assistant' && c.turnUsage) {
            totalPromptTokens += c.turnUsage.actual_prompt_tokens || 0;
            totalCompletionTokens += c.turnUsage.actual_completion_tokens || 0;
        }
    }
    const parts = [];
    parts.push(`${counts.user} 问 · ${counts.assistant} 答`);
    if (counts.tool) parts.push(`${counts.tool} 工具调用 (${(totalToolMs / 1000).toFixed(1)}s)`);
    if (counts.banner) parts.push(`${counts.banner} 异常`);
    if (totalPromptTokens || totalCompletionTokens) {
        parts.push(`tokens ${totalPromptTokens}→${totalCompletionTokens}`);
    }
    return parts.join(' · ');
}

function renderCard(card) {
    switch (card.kind) {
        case 'user': return renderUserCard(card);
        case 'assistant': return renderAssistantCard(card);
        case 'tool': return renderToolCard(card);
        case 'banner': return renderBannerCard(card);
        case 'compaction': return renderCompactionCard(card);
        case 'misc': return renderMiscCard(card);
        default: return '';
    }
}

function renderUserCard(c) {
    return `
        <div class="conv-card conv-user" data-seq="${c.seq}">
            <div class="conv-card-head">
                <span class="conv-role">用户</span>
                <span class="conv-ts">${escapeHtml(shortTs(c.ts))}</span>
            </div>
            <div class="conv-bubble">${escapeHtml(c.text || '(空)')}</div>
        </div>
    `;
}

function renderAssistantCard(c) {
    const usage = c.turnUsage;
    const usageHtml = usage
        ? `<span class="conv-meta-chip" title="${escapeAttr('prompt+completion tokens')}">tokens ${usage.actual_prompt_tokens ?? '?'}→${usage.actual_completion_tokens ?? '?'}</span>`
        : '';
    const turnHtml = c.metadata?.turn ? `<span class="conv-meta-chip">turn ${escapeHtml(String(c.metadata.turn))}</span>` : '';
    const reasoningHtml = c.reasoning && c.reasoning.trim()
        ? `<details class="conv-reasoning"><summary>reasoning</summary><pre>${escapeHtml(c.reasoning)}</pre></details>`
        : '';
    const continuationHint = c.hasToolCalls
        ? `<div class="conv-cont-hint">↓ 继续调用工具</div>`
        : '';
    return `
        <div class="conv-card conv-assistant" data-seq="${c.seq}">
            <div class="conv-card-head">
                <span class="conv-role">助手</span>
                <span class="conv-ts">${escapeHtml(shortTs(c.ts))}</span>
                ${turnHtml}
                ${usageHtml}
            </div>
            <div class="conv-bubble">${escapeHtml(c.text || '(空)')}</div>
            ${reasoningHtml}
            ${continuationHint}
        </div>
    `;
}

function renderToolCard(c) {
    const isError = !!c.execError || c.persistedIsError;
    const cls = isError ? 'conv-tool conv-tool-error' : 'conv-tool';
    const latency = (typeof c.latencyMs === 'number')
        ? `<span class="conv-meta-chip mono">${(c.latencyMs).toFixed(0)} ms</span>`
        : '<span class="conv-meta-chip mono dim">…</span>';
    const attempt = (c.attempt && c.attempt > 0) ? `<span class="conv-meta-chip">重试 ${c.attempt}</span>` : '';
    const argsHtml = c.args
        ? `<details class="conv-tool-args"><summary>参数</summary><pre>${escapeHtml(safeStringify(c.args))}</pre></details>`
        : '';
    const resultText = c.execError
        ? c.execError
        : (c.persistedResult != null ? String(c.persistedResult) : (c.execResult != null ? String(c.execResult) : '(进行中)'));
    const truncatedHint = c.execTruncated
        ? `<span class="conv-trunc-hint">已截断 · 原 ${c.execResultSize} chars</span>`
        : '';
    const resultCls = isError ? 'conv-tool-result is-error' : 'conv-tool-result';
    return `
        <div class="conv-card ${cls}" data-seq="${c.seq}">
            <div class="conv-card-head">
                <span class="conv-tool-icon">⚙</span>
                <span class="conv-role conv-tool-name">${escapeHtml(c.toolId)}</span>
                <span class="conv-ts">${escapeHtml(shortTs(c.ts))}</span>
                ${latency}
                ${attempt}
                ${isError ? '<span class="conv-meta-chip danger">失败</span>' : ''}
            </div>
            ${argsHtml}
            <pre class="${resultCls}">${escapeHtml(truncateForCard(resultText, 1200))}</pre>
            ${truncatedHint}
        </div>
    `;
}

function renderBannerCard(c) {
    return `
        <div class="conv-card conv-banner conv-banner-${c.tone}" data-seq="${c.seq}">
            <div class="conv-banner-title">${escapeHtml(c.title)}</div>
            <div class="conv-banner-detail">${escapeHtml(c.detail || '')}</div>
            <div class="conv-ts">${escapeHtml(shortTs(c.ts))}</div>
        </div>
    `;
}

function renderCompactionCard(c) {
    let summary = '';
    if (c.type === 'compaction.summary_added') summary = '📝 上下文压缩 · 摘要已写入';
    else if (c.type === 'compaction.anchor_moved') summary = '⤴ 压缩锚点移动';
    else summary = '✓ 压缩完成';
    return `
        <div class="conv-card conv-compaction" data-seq="${c.seq}">
            <span class="conv-compaction-icon">🗜</span>
            <span class="conv-compaction-text">${escapeHtml(summary)}</span>
            <span class="conv-ts">${escapeHtml(shortTs(c.ts))}</span>
        </div>
    `;
}

function renderMiscCard(c) {
    return `
        <div class="conv-card conv-misc" data-seq="${c.seq}">
            <div class="conv-card-head">
                <span class="conv-role conv-misc-type">${escapeHtml(c.type)}</span>
                <span class="conv-meta-chip">${escapeHtml(c.visibility || '?')}</span>
                <span class="conv-ts">${escapeHtml(shortTs(c.ts))}</span>
            </div>
            <pre class="conv-misc-payload">${escapeHtml(safeStringify(c.payload))}</pre>
        </div>
    `;
}

function truncateForCard(s, limit) {
    if (typeof s !== 'string') {
        try { s = JSON.stringify(s); } catch (_) { s = String(s); }
    }
    if (!s) return '';
    if (s.length <= limit) return s;
    return s.slice(0, limit) + `\n…<+${s.length - limit} chars>`;
}

// ─── Timeline rendering ───

function renderTimeline() {
    const all = state.events;
    const filtered = applyFilters(all);
    const visibleSeqs = new Set(filtered.map((e) => e.seq));

    if (all.length === 0) {
        els.timeline.innerHTML = '<div class="empty">这个会话还没有任何事件</div>';
        els.statsText.textContent = '0 events';
        return;
    }

    const failureCount = filtered.filter(isFailure).length;
    els.statsText.textContent = `${filtered.length}/${all.length} events · ${failureCount} failures`;

    if (filtered.length === 0) {
        els.timeline.innerHTML = '<div class="empty">没有匹配当前过滤条件的事件</div>';
        return;
    }

    const html = filtered.map((e) => renderEventRow(e, visibleSeqs)).join('');
    els.timeline.innerHTML = html;
    // Wire up parent jump links
    els.timeline.querySelectorAll('.parent-link a[data-jump-seq]').forEach((a) => {
        a.addEventListener('click', (ev) => {
            ev.preventDefault();
            const seq = parseInt(a.getAttribute('data-jump-seq'), 10);
            jumpToSeq(seq);
        });
    });
}

function renderEventRow(event, visibleSeqs) {
    const seq = event.seq;
    const visibility = event.visibility || 'agent';
    const eventType = event.type || '';
    const tsShort = shortTs(event.ts);
    const tsFull = fullTs(event.ts);
    const failureClass = isFailure(event) ? ' is-failure' : '';
    const parentId = event.parent_event_id;
    let parentHtml = '';
    if (parentId) {
        const parentSeq = state.eventIdToSeq.get(parentId);
        if (parentSeq == null) {
            parentHtml = `<span class="parent-link parent-missing">↳ parent ${escapeHtml(parentId.slice(0, 12))}… (not in file)</span>`;
        } else if (visibleSeqs.has(parentSeq)) {
            parentHtml = `<span class="parent-link">↳ parent <a href="#evt-${parentSeq}" data-jump-seq="${parentSeq}">#${parentSeq}</a></span>`;
        } else {
            parentHtml = `<span class="parent-link parent-hidden">↳ parent #${parentSeq} (hidden by filter)</span>`;
        }
    }
    const payloadStr = safeStringify(event.payload || {});
    const eventIdHtml = event.event_id
        ? `<code title="${escapeAttr(event.event_id)}">event_id=${escapeHtml(shortId(event.event_id))}</code>`
        : '';
    const parentMetaHtml = parentId
        ? `<code title="${escapeAttr(parentId)}">parent=${escapeHtml(shortId(parentId))}</code>`
        : '';

    return `
        <details class="event-row${failureClass}" id="evt-${seq}">
          <summary>
            <span class="seq">#${seq}</span>
            <span class="ts" title="${escapeAttr(tsFull)}">${escapeHtml(tsShort)}</span>
            <span class="event-type">${escapeHtml(eventType)}</span>
            ${visChip(visibility)}
            ${parentHtml}
          </summary>
          <div class="event-body">
            <div class="event-meta">
              ${eventIdHtml}
              <code>ts=${escapeHtml(tsFull)}</code>
              ${parentMetaHtml}
            </div>
            <pre class="payload">${escapeHtml(payloadStr)}</pre>
          </div>
        </details>
    `;
}

function jumpToSeq(seq) {
    const el = document.getElementById(`evt-${seq}`);
    if (!el) return;
    // mark transient highlight
    els.timeline.querySelectorAll('[data-target="true"]').forEach((n) => n.removeAttribute('data-target'));
    el.setAttribute('data-target', 'true');
    if (!el.hasAttribute('open')) el.setAttribute('open', '');
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// ─── Utilities ───

function isFailure(event) {
    const t = event.type || '';
    if (t === 'tool.exec_failed' || t === 'tool.call_failed' || t === 'message.stream_failed') return true;
    const payload = event.payload || {};
    return !!(payload.error_type || payload.error);
}

function visChip(visibility) {
    // 复用顶部过滤器同一套 token class (.vis-chip[data-vis=...])；不再行内打色，
    // 避免事件行的小标和顶部过滤器是同一含义却走两套配色。`is-inline` 仅作尺寸微调。
    const vis = visibility || 'agent';
    return `<span class="vis-chip is-inline" data-vis="${escapeAttr(vis)}">${escapeHtml(vis)}</span>`;
}

function shortTs(raw) {
    if (!raw) return '-';
    const d = parseDate(raw);
    if (!d) return raw;
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    return `${hh}:${mm}:${ss}`;
}

function fullTs(raw) {
    if (!raw) return '-';
    const d = parseDate(raw);
    if (!d) return raw;
    return d.toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC');
}

function parseDate(raw) {
    try {
        const d = new Date(raw);
        return isNaN(d.getTime()) ? null : d;
    } catch {
        return null;
    }
}

function safeStringify(obj) {
    try {
        return JSON.stringify(obj, null, 2);
    } catch {
        return String(obj);
    }
}

function shortId(id) {
    // UUID 类长 ID 在 toolbar / event-meta 里完整铺开会撑破排版，统一截前 12 字符 + ellipsis。
    if (!id) return '';
    const s = String(id);
    return s.length <= ID_DISPLAY_PREFIX ? s : `${s.slice(0, ID_DISPLAY_PREFIX)}…`;
}

function escapeHtml(text) {
    if (text == null) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function escapeAttr(text) {
    return escapeHtml(text);
}

function debounce(fn, ms) {
    let h = null;
    return (...args) => {
        if (h != null) clearTimeout(h);
        h = setTimeout(() => { h = null; fn(...args); }, ms);
    };
}

// 刷新类按钮的 loading 套子：禁用 + 替换文案，结束（含错误）自动复原。
async function withButtonLoading(btn, loadingLabel, action) {
    if (!btn) return action();
    const originalLabel = btn.textContent;
    const originalDisabled = btn.disabled;
    btn.disabled = true;
    btn.textContent = loadingLabel;
    try {
        return await action();
    } finally {
        btn.textContent = originalLabel;
        btn.disabled = originalDisabled;
    }
}

// ─── Wiring ───

els.visChips.forEach((chip) => {
    const vis = chip.getAttribute('data-vis');
    if (state.visibilityFilter.has(vis)) chip.classList.remove('off'); else chip.classList.add('off');
    chip.addEventListener('click', () => {
        if (state.visibilityFilter.has(vis)) {
            state.visibilityFilter.delete(vis);
            chip.classList.add('off');
        } else {
            state.visibilityFilter.add(vis);
            chip.classList.remove('off');
        }
        renderTimeline();
    });
});

els.typeInput.addEventListener('input', debounce((e) => {
    state.typeFilter = e.target.value;
    renderTimeline();
}, 250));

els.btnRefresh.addEventListener('click', () => withButtonLoading(els.btnRefresh, '加载中…', async () => {
    await loadSessions();
    if (state.currentSessionId) {
        await selectSession(state.currentSessionId);
    }
}));

if (els.convBtnRefresh) {
    els.convBtnRefresh.addEventListener('click', () => withButtonLoading(els.convBtnRefresh, '加载中…', async () => {
        await loadSessions();
        if (state.currentSessionId) {
            await selectSession(state.currentSessionId);
        }
    }));
}
if (els.convShowTech) {
    els.convShowTech.addEventListener('change', () => {
        state.showTechDetails = !!els.convShowTech.checked;
        renderConversation();
    });
}

// ─── Tabs ───

els.tabs.forEach((btn) => {
    btn.addEventListener('click', () => {
        const target = btn.getAttribute('data-tab-target');
        switchTab(target);
    });
});

function switchTab(target) {
    if (!target || target === state.activeTab) return;
    state.activeTab = target;
    els.tabs.forEach((btn) => {
        const isActive = btn.getAttribute('data-tab-target') === target;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    els.tabPanels.forEach((panel) => {
        const isActive = panel.getAttribute('data-tab') === target;
        panel.classList.toggle('active', isActive);
        if (isActive) panel.removeAttribute('hidden');
        else panel.setAttribute('hidden', '');
    });
    if (target === 'learning-units' && !state.learningUnitMetricsLoaded) {
        loadLearningUnitMetrics();
    }
}

// ─── Learning unit metrics ───

if (els.luWindowSelect) {
    els.luWindowSelect.addEventListener('change', () => {
        const v = els.luWindowSelect.value;
        state.learningUnitWindowDays = v === 'all' ? null : parseInt(v, 10);
        loadLearningUnitMetrics();
    });
}
if (els.luBtnRefresh) {
    els.luBtnRefresh.addEventListener('click', () => withButtonLoading(els.luBtnRefresh, '加载中…', () => loadLearningUnitMetrics()));
}

async function loadLearningUnitMetrics() {
    if (!els.luMetricsGrid) return;
    const wd = state.learningUnitWindowDays;
    // Backend treats negative as "no window"; pass -1 for the "全部" option.
    const wdParam = wd == null ? -1 : wd;
    els.luStatsText.textContent = '加载中…';
    try {
        const data = await apiGet(`/learning-units/metrics?window_days=${encodeURIComponent(wdParam)}`);
        state.learningUnitMetrics = data;
        state.learningUnitMetricsLoaded = true;
        renderLearningUnitMetrics();
    } catch (err) {
        // 失败时把所有数值清空，避免「7 天数据 + 30 天窗口标签」这种静默错位。
        state.learningUnitMetrics = null;
        state.learningUnitMetricsLoaded = false;
        clearLearningUnitMetrics();
        els.luStatsText.textContent = '';
        if (els.luDiagnostics) {
            els.luDiagnostics.innerHTML = `<span class="empty is-error">加载指标失败: ${escapeHtml(err.message)}</span>`;
        }
    }
}

function clearLearningUnitMetrics() {
    // 把 4 张卡片的 value / footnote 全部重置为占位，并加上 .is-empty 类。
    ['ttfv.p50', 'ttfv.p90', 'consolidation.ratio', 'teach.ratio', 'reuse.ratio'].forEach((k) => setBind(k, '—'));
    setBind('ttfv.sample', '样本 —');
    setBind('consolidation.fraction', '— / —');
    setBind('teach.fraction', '— / —');
    setBind('reuse.fraction', '— / —');
    if (els.luMetricsGrid) {
        els.luMetricsGrid.querySelectorAll('.metric-card').forEach((card) => card.classList.add('is-empty'));
    }
}

function markMetricCardEmpty(metricName, isEmpty) {
    if (!els.luMetricsGrid) return;
    const card = els.luMetricsGrid.querySelector(`.metric-card[data-metric="${metricName}"]`);
    if (card) card.classList.toggle('is-empty', isEmpty);
}

function renderLearningUnitMetrics() {
    const m = state.learningUnitMetrics;
    if (!m) return;

    // TTFV
    const ttfv = m.ttfv || {};
    const ttfvSamples = ttfv.sample_size ?? 0;
    setBind('ttfv.p50', formatSeconds(ttfv.p50_seconds));
    setBind('ttfv.p90', formatSeconds(ttfv.p90_seconds));
    setBind('ttfv.sample', `样本 ${ttfvSamples}`);
    markMetricCardEmpty('ttfv', ttfvSamples === 0);

    // Consolidation
    const cons = m.consolidation_rate || {};
    const consDenom = cons.denominator ?? 0;
    setBind('consolidation.ratio', formatRatio(cons.ratio));
    setBind('consolidation.fraction', `${cons.numerator ?? 0} / ${consDenom}`);
    markMetricCardEmpty('consolidation', consDenom === 0);

    // Teach entry
    const teach = m.teach_entry_rate || {};
    const teachDenom = teach.denominator ?? 0;
    setBind('teach.ratio', formatRatio(teach.ratio));
    setBind('teach.fraction', `${teach.numerator ?? 0} / ${teachDenom}`);
    markMetricCardEmpty('teach-entry', teachDenom === 0);

    // Reuse intent
    const reuse = m.reuse_intent_rate || {};
    const reuseDenom = reuse.denominator ?? 0;
    setBind('reuse.ratio', formatRatio(reuse.ratio));
    setBind('reuse.fraction', `${reuse.numerator ?? 0} / ${reuseDenom}`);
    markMetricCardEmpty('reuse-intent', reuseDenom === 0);

    // Top-line stats
    const generated = m.generated_at ? fullTs(m.generated_at) : '-';
    const windowLabel = m.window_days == null ? '全部' : `${m.window_days} 天`;
    els.luStatsText.textContent = `窗口 ${windowLabel} · 生成于 ${generated}`;

    // Diagnostics
    if (els.luDiagnostics) {
        const diag = m.diagnostics || {};
        const winStart = m.window_start ? fullTs(m.window_start) : '不裁窗口';
        els.luDiagnostics.innerHTML = `
            <code>窗口起点 ${escapeHtml(winStart)}</code>
            <code>总事件 ${diag.total_events ?? 0}</code>
            <code>学习卷事件 ${diag.learning_unit_events ?? 0}</code>
        `;
    }
}

function setBind(key, value) {
    const node = document.querySelector(`[data-bind="${key}"]`);
    if (node) node.textContent = value;
}

function formatSeconds(seconds) {
    if (seconds == null) return '—';
    if (seconds < 1) return `${(seconds * 1000).toFixed(0)} ms`;
    if (seconds < 60) return `${seconds.toFixed(1)} s`;
    if (seconds < 3600) return `${(seconds / 60).toFixed(1)} min`;
    return `${(seconds / 3600).toFixed(1)} h`;
}

function formatRatio(ratio) {
    if (ratio == null) return '—';
    return `${(ratio * 100).toFixed(1)}%`;
}

// ─── Init ───

loadSessions();

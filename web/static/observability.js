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

const VIS_COLORS = {
    agent: '#2563eb',
    system: '#64748b',
    ui: '#0891b2',
    observability: '#ea580c',
};

const state = {
    sessions: [],
    currentSessionId: null,
    events: [],
    eventBySeq: new Map(),
    eventIdToSeq: new Map(),
    visibilityFilter: new Set(['agent', 'system', 'observability', 'ui']),
    typeFilter: '',
};

const els = {
    sessionList: document.getElementById('session-list'),
    currentSession: document.getElementById('current-session'),
    statsText: document.getElementById('stats-text'),
    timeline: document.getElementById('timeline'),
    typeInput: document.getElementById('type-filter'),
    btnRefresh: document.getElementById('btn-refresh'),
    visChips: document.querySelectorAll('.vis-chip'),
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
        els.sessionList.innerHTML = `<div class="session-empty" style="color:#b91c1c">加载会话失败: ${escapeHtml(err.message)}</div>`;
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
            <div class="session-row ${isActive ? 'active' : ''}" data-session-id="${escapeAttr(sid)}">
                <div class="session-id">${escapeHtml(sid)}</div>
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
    els.currentSession.textContent = sessionId;
    els.statsText.textContent = '加载中…';
    els.timeline.innerHTML = '<div class="empty">加载中…</div>';
    try {
        const data = await apiGet(`/sessions/${encodeURIComponent(sessionId)}/events?limit=5000`);
        state.events = Array.isArray(data.events) ? data.events : [];
        rebuildIndexes();
        renderTimeline();
    } catch (err) {
        els.timeline.innerHTML = `<div class="empty" style="color:#b91c1c">加载事件失败: ${escapeHtml(err.message)}</div>`;
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
    const eventIdHtml = event.event_id ? `<code>event_id=${escapeHtml(event.event_id)}</code>` : '';
    const parentMetaHtml = parentId ? `<code>parent=${escapeHtml(parentId)}</code>` : '';

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
    const color = VIS_COLORS[visibility] || '#64748b';
    return `<span class="vis-chip-inline" style="display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;border:1px solid ${color}55;background:${color}1a;color:${color}">${escapeHtml(visibility)}</span>`;
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

els.typeInput.addEventListener('input', (e) => {
    state.typeFilter = e.target.value;
    renderTimeline();
});

els.btnRefresh.addEventListener('click', async () => {
    await loadSessions();
    if (state.currentSessionId) {
        await selectSession(state.currentSessionId);
    }
});

// ─── Init ───

loadSessions();

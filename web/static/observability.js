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
    activeTab: 'events',
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
    els.statsText.textContent = '加载中…';
    els.timeline.innerHTML = '<div class="empty">加载中…</div>';
    try {
        const data = await apiGet(`/sessions/${encodeURIComponent(sessionId)}/events?limit=5000`);
        state.events = Array.isArray(data.events) ? data.events : [];
        rebuildIndexes();
        renderTimeline();
    } catch (err) {
        els.timeline.innerHTML = `<div class="empty is-error">加载事件失败: ${escapeHtml(err.message)}</div>`;
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

/**
 * Learning-Agent Observability 面板
 */

const API_BASE = 'http://127.0.0.1:8000';

// ─── DOM 元素 ───
const els = {
    navItems: document.querySelectorAll('.obs-nav li'),
    panels: document.querySelectorAll('.obs-panel'),
    panelTitle: document.getElementById('panel-title'),
    btnRefresh: document.getElementById('btn-refresh'),
    tracesList: document.getElementById('traces-list'),
    eventsTbody: document.getElementById('events-tbody'),
    metricsGrid: document.getElementById('metrics-grid'),
    logsErrorsTbody: document.getElementById('logs-errors-tbody'),
    logsEventsTbody: document.getElementById('logs-events-tbody'),
    errorsList: document.getElementById('errors-list'),
    errorFilter: document.getElementById('error-filter'),
    errorCount: document.getElementById('error-count'),
    runtimesContainer: document.getElementById('runtimes-container'),
};

let currentTab = 'traces';

// ─── API ───

async function api(method, path) {
    const res = await fetch(API_BASE + path, { method, headers: { 'Content-Type': 'application/json' } });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '未知错误' }));
        throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
}

// ─── Tab 切换 ───

function switchTab(tab) {
    currentTab = tab;
    els.navItems.forEach(li => li.classList.toggle('active', li.dataset.tab === tab));
    els.panels.forEach(p => p.classList.toggle('active', p.id === `panel-${tab}`));
    const titles = { traces: 'Traces', events: 'Events', metrics: 'Metrics', errors: 'Errors', runtimes: 'Session Runtimes', logs: 'Logs' };
    els.panelTitle.textContent = titles[tab] || tab;
    loadTabData(tab);
    if (tab === 'runtimes') {
        startRuntimeRefresh();
    } else {
        stopRuntimeRefresh();
    }
    if (tab === 'events') {
        startEventStream();
    } else {
        stopEventStream();
    }
}

els.navItems.forEach(li => {
    li.addEventListener('click', () => switchTab(li.dataset.tab));
});

els.btnRefresh.addEventListener('click', () => loadTabData(currentTab));

// ─── Copy Errors for AI ───
document.getElementById('btn-copy-errors').addEventListener('click', copyAllVisibleErrors);

// ─── 数据加载 ───

async function loadTabData(tab) {
    try {
        if (tab === 'traces') await loadTraces();
        else if (tab === 'events') await loadEvents();
        else if (tab === 'metrics') await loadMetrics();
        else if (tab === 'errors') await loadErrors();
        else if (tab === 'runtimes') await loadRuntimes();
        else if (tab === 'logs') await loadLogs();
    } catch (err) {
        console.error(`加载 ${tab} 失败:`, err);
    }
}

// ─── Traces ───

async function loadTraces() {
    const traces = await api('GET', '/observability/traces?limit=50');
    els.tracesList.innerHTML = '';
    if (!traces || traces.length === 0) {
        els.tracesList.innerHTML = '<div class="obs-empty">暂无 Trace 数据</div>';
        return;
    }

    // 填充 session filter
    const filterEl = document.getElementById('trace-session-filter');
    const sessions = [...new Set(traces.map(t => t.session_id).filter(Boolean))];
    if (filterEl) {
        const currentVal = filterEl.value;
        filterEl.innerHTML = '<option value="">全部</option>' + sessions.map(s => `<option value="${s}">${shortId(s)}</option>`).join('');
        filterEl.value = sessions.includes(currentVal) ? currentVal : '';
        filterEl.onchange = () => loadTraces();
    }

    const sessionFilter = filterEl ? filterEl.value : '';
    const filtered = sessionFilter ? traces.filter(t => t.session_id === sessionFilter) : traces;

    for (const t of filtered) {
        const card = document.createElement('div');
        card.className = 'trace-card';
        card.dataset.traceId = t.trace_id;

        const timeStr = t.timestamp ? new Date(t.timestamp).toLocaleString() : '-';
        const durStr = t.duration_ms != null ? `${t.duration_ms}ms` : '-';

        card.innerHTML = `
            <div class="trace-header">
                <div class="trace-id">${t.trace_id}</div>
                <div class="trace-time">${timeStr}</div>
            </div>
            <div class="trace-meta">
                <span>session: ${shortId(t.session_id)}</span>
                <span>spans: ${t.span_count}</span>
                <span>duration: ${durStr}</span>
            </div>
            <div class="trace-detail" id="detail-${t.trace_id}">
                <div style="display: flex; gap: 8px; margin-bottom: 12px;">
                    <button class="obs-refresh" onclick="event.stopPropagation(); showTraceFlow('${t.trace_id}', '${t.session_id || ''}', this)">📝 Full Flow</button>
                    <button class="obs-refresh" onclick="event.stopPropagation(); showTraceRaw('${t.trace_id}', this)">查看原始 JSON</button>
                    <button class="obs-refresh" onclick="event.stopPropagation(); copyTraceErrors('${t.trace_id}', '${t.session_id || ''}', this)">📋 Copy Errors</button>
                </div>
                <div class="flow-panel hidden" id="flow-${t.trace_id}"></div>
                <div class="span-list" id="spans-${t.trace_id}"></div>
                <pre class="json-preview hidden" id="raw-${t.trace_id}"></pre>
            </div>
        `;

        card.addEventListener('click', (e) => {
            if (e.target.closest('.trace-detail') || e.target.closest('button')) return;
            toggleTraceDetail(card, t.trace_id);
        });

        els.tracesList.appendChild(card);
    }
}

async function toggleTraceDetail(card, traceId) {
    const detail = card.querySelector('.trace-detail');
    const isOpen = detail.classList.contains('open');

    // 关闭其他
    document.querySelectorAll('.trace-detail.open').forEach(d => d.classList.remove('open'));
    document.querySelectorAll('.trace-card.selected').forEach(c => c.classList.remove('selected'));

    if (isOpen) return;

    detail.classList.add('open');
    card.classList.add('selected');

    const spansContainer = document.getElementById(`spans-${traceId}`);
    if (spansContainer.children.length === 0) {
        try {
            const trace = await api('GET', `/observability/traces/${traceId}`);
            const spanTree = buildSpanTree(trace.spans || []);
            spansContainer.innerHTML = renderSpanTree(spanTree, trace.duration_ms || 1);
        } catch (err) {
            spansContainer.innerHTML = `<div style="color:var(--obs-danger)">加载失败: ${err.message}</div>`;
        }
    }
}

function renderSpans(container, spans, totalMs) {
    container.innerHTML = '';
    if (!spans.length) {
        container.innerHTML = '<div style="color:var(--obs-muted)">无 Span 数据</div>';
        return;
    }

    // 计算最大 duration 用于比例
    const maxDur = Math.max(...spans.map(s => s.duration_ms || 0), 1);

    for (const span of spans) {
        const dur = span.duration_ms || 0;
        const pct = Math.max((dur / maxDur) * 100, 2);
        const hasError = !!span.error;

        const row = document.createElement('div');
        row.className = 'span-row';
        row.innerHTML = `
            <div class="span-name">${span.name}</div>
            <div class="span-bar-wrap">
                <div class="span-bar ${hasError ? 'error' : ''}" style="width: ${pct}%"></div>
            </div>
            <div class="span-duration">${dur}ms</div>
        `;
        container.appendChild(row);

        // Tags
        const tagKeys = Object.keys(span.tags || {});
        if (tagKeys.length > 0 || hasError) {
            const tagsDiv = document.createElement('div');
            tagsDiv.className = 'span-tags';
            if (hasError) {
                tagsDiv.innerHTML += `<span class="span-tag" style="color:var(--obs-danger)">error: ${span.error}</span>`;
            }
            for (const k of tagKeys) {
                const v = span.tags[k];
                tagsDiv.innerHTML += `<span class="span-tag">${k}: ${v}</span>`;
            }
            container.appendChild(tagsDiv);
        }
    }
}

function buildSpanTree(spans) {
    const byId = new Map(spans.map(s => [s.span_id, { ...s, children: [] }]));
    const roots = [];
    for (const span of byId.values()) {
        if (span.parent_id && byId.has(span.parent_id)) {
            byId.get(span.parent_id).children.push(span);
        } else {
            roots.push(span);
        }
    }
    return roots;
}

function renderSpanTree(nodes, totalMs, depth = 0) {
    if (!nodes || nodes.length === 0) {
        return '<div style="color:var(--obs-muted)">无 Span 数据</div>';
    }
    const maxDur = Math.max(...nodes.map(s => s.duration_ms || 0), 1);
    let html = '';
    for (const n of nodes) {
        const dur = n.duration_ms || 0;
        const pct = Math.max((dur / maxDur) * 100, 2);
        const hasError = !!n.error;
        html += `
            <div class="span-row" style="padding-left: ${depth * 20 + 12}px">
                <div class="span-name">${n.name}</div>
                <div class="span-bar-wrap">
                    <div class="span-bar ${hasError ? 'error' : ''}" style="width: ${pct}%"></div>
                </div>
                <div class="span-duration">${dur}ms</div>
            </div>
        `;
        const tagKeys = Object.keys(n.tags || {});
        if (tagKeys.length > 0 || hasError) {
            html += `<div class="span-tags" style="padding-left: ${depth * 20 + 132}px">`;
            if (hasError) {
                html += `<span class="span-tag" style="color:var(--obs-danger)">error: ${n.error}</span>`;
            }
            for (const k of tagKeys) {
                html += `<span class="span-tag">${k}: ${n.tags[k]}</span>`;
            }
            html += `</div>`;
        }
        if (n.children && n.children.length > 0) {
            html += renderSpanTree(n.children, totalMs, depth + 1);
        }
    }
    return html;
}

async function showTraceFlow(traceId, sessionId, btn) {
    const panel = document.getElementById(`flow-${traceId}`);
    if (!panel.classList.contains('hidden')) {
        panel.classList.add('hidden');
        btn.textContent = '📝 Full Flow';
        return;
    }
    if (!sessionId) {
        panel.innerHTML = '<div style="color:var(--obs-muted)">无 Session 信息</div>';
        panel.classList.remove('hidden');
        btn.textContent = '📝 关闭 Flow';
        return;
    }
    try {
        const flow = await api('GET', `/observability/flows/${sessionId}`);
        renderFlow(panel, flow.steps || []);
        panel.classList.remove('hidden');
        btn.textContent = '📝 关闭 Flow';
    } catch (err) {
        panel.innerHTML = `<div style="color:var(--obs-danger)">加载 Flow 失败: ${err.message}</div>`;
        panel.classList.remove('hidden');
        btn.textContent = '📝 关闭 Flow';
    }
}

function renderFlow(container, steps) {
    container.innerHTML = '';
    if (!steps || steps.length === 0) {
        container.innerHTML = '<div style="color:var(--obs-muted)">暂无流程数据</div>';
        return;
    }

    const PHASE_META = {
        user_input: { icon: '📝', label: '用户输入', color: '#58a6ff' },
        intent_parse: { icon: '🧩', label: '意图解析', color: '#a371f7' },
        context_build: { icon: '📦', label: '上下文组装', color: '#d29922' },
        llm_stream_start: { icon: '🤖', label: 'LLM 流式开始', color: '#3fb950' },
        llm_stream_end: { icon: '✅', label: 'LLM 流式结束', color: '#3fb950' },
        tool_call: { icon: '🔧', label: '工具调用', color: '#f0883e' },
        tool_result: { icon: '📤', label: '工具结果', color: '#58a6ff' },
        final_response: { icon: '💬', label: '最终响应', color: '#79c0ff' },
    };

    const timeline = document.createElement('div');
    timeline.className = 'flow-timeline';

    for (const step of steps) {
        const meta = PHASE_META[step.phase] || { icon: '•', label: step.phase, color: '#8b949e' };
        const item = document.createElement('div');
        item.className = 'flow-item';

        let detailHtml = '';
        const detail = step.detail || {};

        if (step.phase === 'user_input') {
            detailHtml = `<div class="flow-detail-text">${escapeHtml(detail.content || '')}</div>`;
        } else if (step.phase === 'intent_parse') {
            detailHtml = `<div class="flow-detail-text">类型: ${detail.type || '-'} | 置信度: ${detail.confidence || '-'} | 需澄清: ${detail.needs_clarification ? '是' : '否'}</div>`;
        } else if (step.phase === 'context_build') {
            const msgs = (detail.messages || []).map(m => `<span class="flow-tag" style="border-color:${roleColor(m.role)}">${m.role}</span> ${escapeHtml(m.content?.slice(0, 120) || '')}${m.content?.length > 120 ? '…' : ''}`).join('</div><div class="flow-msg">');
            detailHtml = `<div class="flow-detail-text">消息数: ${detail.message_count || 0}</div><div class="flow-msg-list">${msgs}</div>`;
        } else if (step.phase === 'llm_stream_start') {
            detailHtml = `<div class="flow-detail-text">首段内容: ${escapeHtml(detail.first_chunk || '')}</div>`;
        } else if (step.phase === 'llm_stream_end') {
            detailHtml = `<div class="flow-detail-text">结束原因: <code>${detail.finish_reason || '-'}</code></div>`;
        } else if (step.phase === 'tool_call') {
            detailHtml = `<div class="flow-detail-text">工具: <code>${detail.tool_id || '-'}</code></div><pre class="flow-json">${JSON.stringify(detail.arguments || {}, null, 2)}</pre>`;
        } else if (step.phase === 'tool_result') {
            const status = detail.success ? '<span style="color:var(--obs-success)">成功</span>' : `<span style="color:var(--obs-danger)">失败: ${escapeHtml(detail.error || '')}</span>`;
            detailHtml = `<div class="flow-detail-text">状态: ${status}</div><pre class="flow-json">${JSON.stringify(detail.result || {}, null, 2)}</pre>`;
        } else if (step.phase === 'final_response') {
            detailHtml = `<div class="flow-detail-text">${escapeHtml(detail.content || '').slice(0, 800)}${(detail.content || '').length > 800 ? '…' : ''}</div>`;
        } else {
            detailHtml = `<pre class="flow-json">${JSON.stringify(detail, null, 2)}</pre>`;
        }

        item.innerHTML = `
            <div class="flow-marker" style="color:${meta.color}">${meta.icon}</div>
            <div class="flow-body">
                <div class="flow-title">${meta.label} <span class="flow-time">${step.time ? new Date(step.time).toLocaleTimeString() : ''}</span></div>
                ${detailHtml}
            </div>
        `;
        timeline.appendChild(item);
    }

    container.appendChild(timeline);
}

function roleColor(role) {
    const map = { system: '#8b949e', user: '#58a6ff', assistant: '#3fb950', tool: '#d29922' };
    return map[role] || '#8b949e';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function showTraceRaw(traceId, btn) {
    const pre = document.getElementById(`raw-${traceId}`);
    if (!pre.classList.contains('hidden')) {
        pre.classList.add('hidden');
        if (btn) btn.textContent = '查看原始 JSON';
        return;
    }
    try {
        const trace = await api('GET', `/observability/traces/${traceId}`);
        pre.textContent = JSON.stringify(trace, null, 2);
        pre.classList.remove('hidden');
        if (btn) btn.textContent = '关闭 JSON';
    } catch (err) {
        pre.textContent = '加载失败: ' + err.message;
        pre.classList.remove('hidden');
    }
}

// ─── Events ───

async function loadEvents() {
    const events = await api('GET', '/observability/events?limit=200');
    els.eventsTbody.innerHTML = '';
    if (!events || events.length === 0) {
        els.eventsTbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--obs-muted)">暂无事件</td></tr>';
        return;
    }

    for (const ev of events) {
        const tr = document.createElement('tr');
        const time = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : '-';
        const levelBadge = levelBadgeHtml(ev.level || 'info');
        tr.innerHTML = `
            <td>${time}</td>
            <td><code>${ev.type || '-'}</code></td>
            <td>${ev.source || '-'}</td>
            <td>${shortId(ev.session_id)}</td>
            <td>${levelBadge}</td>
        `;
        els.eventsTbody.appendChild(tr);
    }
}

// ─── Metrics ───

async function loadMetrics() {
    const data = await api('GET', '/observability/metrics');
    els.metricsGrid.innerHTML = '';

    const renderCard = (label, value, sub = '') => {
        const card = document.createElement('div');
        card.className = 'metric-card';
        card.innerHTML = `
            <div class="metric-label">${label}</div>
            <div class="metric-value">${value}</div>
            ${sub ? `<div class="metric-sub">${sub}</div>` : ''}
        `;
        els.metricsGrid.appendChild(card);
    };

    // 简单展平 metrics
    for (const [category, items] of Object.entries(data)) {
        for (const [name, val] of Object.entries(items)) {
            if (typeof val === 'number') {
                renderCard(`${category}.${name}`, formatNumber(val));
            } else if (typeof val === 'object' && val !== null) {
                // histogram 类型，展示关键分位
                const sub = Object.entries(val)
                    .filter(([k]) => ['min', 'max', 'p50', 'p95', 'p99'].includes(k))
                    .map(([k, v]) => `${k}: ${formatNumber(v)}`)
                    .join(' | ');
                renderCard(`${category}.${name}`, formatNumber(val.p50 || val.mean || 0), sub);
            }
        }
    }

    if (els.metricsGrid.children.length === 0) {
        els.metricsGrid.innerHTML = '<div class="obs-empty">暂无指标数据</div>';
    }
}

// ─── Errors ───

let _cachedErrors = [];

async function loadErrors() {
    const data = await api('GET', '/observability/errors?limit=500');
    _cachedErrors = data.errors || [];
    renderErrors(_cachedErrors);

    // 绑定过滤器
    if (els.errorFilter) {
        els.errorFilter.onchange = () => {
            const filter = els.errorFilter.value;
            let filtered = _cachedErrors;
            if (filter === 'error') filtered = _cachedErrors.filter(e => e.level === 'error');
            else if (filter === 'warn') filtered = _cachedErrors.filter(e => e.level === 'warn');
            else if (filter !== 'all') filtered = _cachedErrors.filter(e => e.category === filter);
            renderErrors(filtered);
        };
    }
}

function renderErrors(errors) {
    els.errorsList.innerHTML = '';
    if (!errors || errors.length === 0) {
        els.errorsList.innerHTML = '<div class="obs-empty">暂无错误记录</div>';
        if (els.errorCount) els.errorCount.textContent = '0 条';
        return;
    }

    if (els.errorCount) els.errorCount.textContent = `${errors.length} 条`;

    const timeline = document.createElement('div');
    timeline.className = 'flow-timeline';

    for (const err of errors) {
        const item = document.createElement('div');
        item.className = 'flow-item';

        const meta = _errorMeta(err);
        const timeStr = err.timestamp ? new Date(err.timestamp).toLocaleString() : '-';
        const detailsHtml = _renderErrorDetails(err);

        item.innerHTML = `
            <div class="flow-marker" style="color:${meta.color}">${meta.icon}</div>
            <div class="flow-body">
                <div class="flow-title">
                    ${meta.label}
                    <span class="badge ${err.level === 'error' ? 'badge-error' : 'badge-warn'}">${err.level || 'error'}</span>
                    <span class="badge badge-info">${err.category || '-'}</span>
                    <span class="flow-time">${timeStr}</span>
                </div>
                <div class="flow-detail-text" style="color: var(--obs-text); margin-bottom: 6px;">${escapeHtml(err.message || '-')}</div>
                <div style="display: flex; gap: 12px; font-size: 0.75rem; color: var(--obs-muted); margin-bottom: 6px;">
                    <span>trace: ${shortId(err.trace_id)}</span>
                    <span>session: ${shortId(err.session_id)}</span>
                    <span>source: ${err.source || '-'}</span>
                    <span>source_type: ${err.source_type || '-'}</span>
                </div>
                ${detailsHtml}
            </div>
        `;
        timeline.appendChild(item);
    }

    els.errorsList.appendChild(timeline);
}

function _errorMeta(err) {
    const type = err.type || '';
    const category = err.category || '';

    const map = {
        'agent.toolBanned': { icon: '🚫', label: '工具被禁用', color: '#f85149' },
        'agent.toolValidationFailed': { icon: '⚠️', label: '参数校验失败', color: '#d29922' },
        'agent.toolPermissionDenied': { icon: '🔒', label: '权限被拒绝', color: '#f85149' },
        'agent.toolPermissionAsk': { icon: '❓', label: '需要确认', color: '#d29922' },
        'agent.toolResult': { icon: '🔧', label: '工具执行失败', color: '#f85149' },
        'agent.turnRetry': { icon: '🔄', label: 'Turn 重试', color: '#d29922' },
        'agent.contextCompressed': { icon: '📦', label: '上下文压缩', color: '#d29922' },
        'agent.stateChanged': { icon: '💥', label: 'Agent 错误状态', color: '#f85149' },
        'span.error': { icon: '📍', label: 'Trace Span 错误', color: '#f85149' },
        'audit.error': { icon: '📋', label: '审计错误', color: '#f85149' },
    };

    if (map[type]) return map[type];

    // 按 category 兜底
    const catMap = {
        tool: { icon: '🔧', label: '工具错误', color: '#f85149' },
        validation: { icon: '⚠️', label: '校验错误', color: '#d29922' },
        permission: { icon: '🔒', label: '权限错误', color: '#f85149' },
        llm: { icon: '🤖', label: 'LLM 错误', color: '#f85149' },
        system: { icon: '💥', label: '系统错误', color: '#f85149' },
        trace: { icon: '📍', label: 'Trace 错误', color: '#f85149' },
        audit: { icon: '📋', label: '审计错误', color: '#f85149' },
    };

    return catMap[category] || { icon: '•', label: type || '未知错误', color: '#8b949e' };
}

function _renderErrorDetails(err) {
    const details = err.details || {};
    if (!details || Object.keys(details).length === 0) return '';

    // 针对不同类型做格式化展示
    const type = err.type || '';
    let extraHtml = '';

    if (type === 'agent.toolBanned') {
        extraHtml = `
            <div style="display: flex; gap: 12px; flex-wrap: wrap; font-size: 0.78rem; color: var(--obs-muted);">
                <span>tool_id: <code>${details.tool_id || '-'}</code></span>
                <span>turn: ${details.turn || '-'}</span>
                <span>窗口内失败: ${details.failures_in_window || '-'}</span>
            </div>`;
    } else if (type === 'agent.toolValidationFailed') {
        const errors = (details.errors || []).map(e => `<li>${escapeHtml(e.param || '')}: ${escapeHtml(e.issue || '')}</li>`).join('');
        extraHtml = `
            <div style="font-size: 0.78rem; color: var(--obs-muted);">
                <span>tool_id: <code>${details.tool_id || '-'}</code></span> | turn: ${details.turn || '-'}
                ${errors ? `<ul style="margin: 4px 0 0 16px; padding: 0;">${errors}</ul>` : ''}
            </div>`;
    } else if (type === 'agent.toolPermissionDenied' || type === 'agent.toolPermissionAsk') {
        extraHtml = `
            <div style="font-size: 0.78rem; color: var(--obs-muted);">
                <span>tool_id: <code>${details.tool_id || '-'}</code></span>
                <div style="margin-top: 4px;">reason: ${escapeHtml(details.reason || details.message || '-')}</div>
            </div>`;
    } else if (type === 'agent.toolResult') {
        extraHtml = `
            <div style="font-size: 0.78rem; color: var(--obs-muted);">
                <span>tool_id: <code>${details.tool_id || '-'}</code></span>
                <pre class="flow-json" style="margin-top: 6px; font-size: 0.75rem;">${JSON.stringify(details.result || {}, null, 2)}</pre>
            </div>`;
    } else if (type === 'agent.turnRetry') {
        extraHtml = `
            <div style="font-size: 0.78rem; color: var(--obs-muted);">
                <span>attempt: ${details.attempt || '-'}</span> | reason: ${escapeHtml(details.reason || '-')}
            </div>`;
    } else if (type === 'span.error') {
        extraHtml = `
            <div style="font-size: 0.78rem; color: var(--obs-muted);">
                <span>span: ${details.span_name || '-'}</span> | duration: ${details.duration_ms || '-'}ms
                ${details.tags ? `<div style="margin-top: 4px;">tags: <code>${escapeHtml(JSON.stringify(details.tags))}</code></div>` : ''}
            </div>`;
    }

    // 兜底 JSON
    const jsonStr = JSON.stringify(details, null, 2);
    const showJson = !extraHtml || jsonStr.length < 800;

    return extraHtml + (showJson ? `<pre class="flow-json" style="margin-top: 8px; font-size: 0.75rem;">${escapeHtml(jsonStr)}</pre>` : '');
}

// ─── Logs ───

async function loadLogs() {
    const data = await api('GET', '/observability/logs?limit=200');

    // Errors
    els.logsErrorsTbody.innerHTML = '';
    const errors = data.trace_errors || [];
    if (errors.length === 0) {
        els.logsErrorsTbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--obs-muted)">暂无 Trace 错误</td></tr>';
    } else {
        for (const err of errors) {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><code>${shortId(err.trace_id)}</code></td>
                <td>${err.span_name || '-'}</td>
                <td style="color:var(--obs-danger)">${err.error}</td>
                <td>${err.timestamp ? new Date(err.timestamp).toLocaleString() : '-'}</td>
            `;
            els.logsErrorsTbody.appendChild(tr);
        }
    }

    // Events
    els.logsEventsTbody.innerHTML = '';
    const eventLogs = data.event_logs || [];
    if (eventLogs.length === 0) {
        els.logsEventsTbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--obs-muted)">暂无日志</td></tr>';
    } else {
        // 只展示最近 100 条，避免过多
        const recent = eventLogs.slice(-100);
        for (const log of recent) {
            const tr = document.createElement('tr');
            const time = log.timestamp ? new Date(log.timestamp).toLocaleTimeString() : '-';
            tr.innerHTML = `
                <td>${time}</td>
                <td>${levelBadgeHtml(log.level || 'info')}</td>
                <td><code>${log.type || '-'}</code></td>
                <td>${log.source || '-'}</td>
                <td>${shortId(log.session_id)}</td>
            `;
            els.logsEventsTbody.appendChild(tr);
        }
    }
}

// ─── 工具函数 ───

function shortId(id) {
    if (!id) return '-';
    return id.length > 12 ? id.slice(0, 10) + '…' : id;
}

function levelBadgeHtml(level) {
    const map = {
        info: 'badge-info',
        warn: 'badge-warn',
        warning: 'badge-warn',
        error: 'badge-error',
        critical: 'badge-error',
        debug: 'badge-success',
    };
    const cls = map[level] || 'badge-info';
    return `<span class="badge ${cls}">${level}</span>`;
}

// ─── Copy to Clipboard ───

async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        showToast('已复制到剪贴板');
    } catch (e) {
        // fallback
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast('已复制到剪贴板');
    }
}

function showToast(msg, type = 'info') {
    const div = document.createElement('div');
    div.textContent = msg;
    const borderColor = type === 'error' ? 'var(--obs-danger)' : 'var(--obs-border)';
    div.style.cssText = `
        position: fixed; bottom: 24px; right: 24px; z-index: 9999;
        background: var(--obs-panel); color: var(--obs-text);
        border: 1px solid ${borderColor}; border-radius: 8px;
        padding: 10px 18px; font-size: 0.85rem;
        box-shadow: 0 4px 20px rgba(0,0,0,0.4);
    `;
    document.body.appendChild(div);
    setTimeout(() => { div.style.opacity = '0'; div.style.transition = 'opacity 0.3s'; }, 1800);
    setTimeout(() => div.remove(), 2200);
}

/**
 * 将错误列表格式化为 AI 友好的诊断文本。
 * 原则：信息密度高、保留原始 details、去掉装饰、按类型聚合。
 */
function formatErrorsForAI(errors) {
    if (!errors || errors.length === 0) return 'No errors found.';

    // 1. 过滤：只保留 error / warn（丢弃 info/debug）
    const relevant = errors.filter(e => (e.level === 'error' || e.level === 'warn'));
    if (relevant.length === 0) return 'No errors found.';

    // 2. 聚合：按 (type + source + category + message) 分组
    const groups = [];
    const keyMap = new Map(); // key -> group index

    for (const e of relevant) {
        const key = `${e.type || ''}||${e.source || ''}||${e.category || ''}||${e.message || ''}`;
        if (keyMap.has(key)) {
            const g = groups[keyMap.get(key)];
            g.count += 1;
            g.samples.push({ ts: e.timestamp, sid: e.session_id, tid: e.trace_id, detail: e.details });
        } else {
            keyMap.set(key, groups.length);
            groups.push({
                type: e.type,
                source: e.source,
                category: e.category,
                message: e.message,
                level: e.level,
                count: 1,
                representative: e.details || {},
                samples: [{ ts: e.timestamp, sid: e.session_id, tid: e.trace_id, detail: e.details }],
            });
        }
    }

    // 3. 构建输出
    const lines = [];
    lines.push('--- Error Context ---');
    lines.push(`Generated: ${new Date().toISOString()}`);
    lines.push(`Total unique error types: ${groups.length} (${relevant.length} occurrences)`);
    lines.push('');

    groups.forEach((g, idx) => {
        const header = g.count > 1 ? `[${idx + 1}] ${g.type} (×${g.count})` : `[${idx + 1}] ${g.type}`;
        lines.push(header);
        lines.push(`source: ${g.source || '-'}`);
        lines.push(`category: ${g.category || '-'}`);
        lines.push(`level: ${g.level || '-'}`);
        lines.push(`message: ${g.message || '-'}`);

        // representative details（取第一条的非空详情）
        const rep = g.representative || {};
        if (Object.keys(rep).length > 0) {
            lines.push('details:');
            _writeDetails(lines, rep, 2);
        }

        // 若有多条，给出样本（时间 + session，帮助 AI 判断时序和影响范围）
        if (g.count > 1) {
            lines.push('samples:');
            // 最多给 5 个样本，避免 token 爆炸
            const shown = g.samples.slice(0, 5);
            for (const s of shown) {
                const t = s.ts ? new Date(s.ts).toISOString() : '-';
                lines.push(`  - ${t}, session=${s.sid || '-'}, trace=${s.tid || '-'}`);
            }
            if (g.samples.length > 5) {
                lines.push(`  ... and ${g.samples.length - 5} more`);
            }
        } else {
            const s = g.samples[0];
            const t = s.ts ? new Date(s.ts).toISOString() : '-';
            lines.push(`occurred: ${t}, session=${s.sid || '-'}, trace=${s.tid || '-'}`);
        }
        lines.push('');
    });

    return lines.join('\n');
}

function _writeDetails(lines, obj, indent) {
    const prefix = '  '.repeat(indent);
    for (const [k, v] of Object.entries(obj)) {
        if (v === null || v === undefined) continue;
        if (typeof v === 'object') {
            if (Array.isArray(v)) {
                if (v.length === 0) continue;
                lines.push(`${prefix}${k}:`);
                for (const item of v) {
                    if (typeof item === 'object') {
                        lines.push(`${prefix}  -`);
                        _writeDetails(lines, item, indent + 2);
                    } else {
                        lines.push(`${prefix}  - ${item}`);
                    }
                }
            } else {
                if (Object.keys(v).length === 0) continue;
                lines.push(`${prefix}${k}:`);
                _writeDetails(lines, v, indent + 1);
            }
        } else {
            lines.push(`${prefix}${k}: ${v}`);
        }
    }
}

async function copyAllVisibleErrors() {
    // 使用当前过滤后的错误（与用户看到的一致）
    const filter = els.errorFilter ? els.errorFilter.value : 'all';
    let source = _cachedErrors;
    if (filter === 'error') source = _cachedErrors.filter(e => e.level === 'error');
    else if (filter === 'warn') source = _cachedErrors.filter(e => e.level === 'warn');
    else if (filter !== 'all') source = _cachedErrors.filter(e => e.category === filter);

    const text = formatErrorsForAI(source);
    await copyToClipboard(text);
}

async function copyTraceErrors(traceId, sessionId, btn) {
    // 优先从已缓存的错误中过滤（sessionId 可能为空，此时只按 traceId 过滤）
    let errs = _cachedErrors.filter(e => {
        if (e.trace_id === traceId) return true;
        if (sessionId && e.session_id === sessionId) return true;
        return false;
    });
    // 若缓存中没有，尝试从当前 Trace 的原始 JSON 中提取 span errors（兜底）
    if (errs.length === 0) {
        try {
            const trace = await api('GET', `/observability/traces/${traceId}`);
            errs = _extractErrorsFromTrace(trace);
        } catch (e) {
            showToast('加载 Trace 错误失败');
            return;
        }
    }
    if (errs.length === 0) {
        showToast('该 Trace 下未发现错误');
        return;
    }
    const text = formatErrorsForAI(errs);
    await copyToClipboard(text);
}

function _extractErrorsFromTrace(trace) {
    const errs = [];
    const sid = trace.session_id;
    const tid = trace.trace_id;
    for (const sp of (trace.spans || [])) {
        if (sp.error) {
            errs.push({
                timestamp: sp.end_time || trace.timestamp,
                level: 'error',
                category: 'trace',
                type: 'span.error',
                source: 'agent_loop',
                message: sp.error,
                trace_id: tid,
                session_id: sid,
                details: { span_name: sp.name, duration_ms: sp.duration_ms, tags: sp.tags || {} },
            });
        }
    }
    return errs;
}

function formatNumber(n) {
    if (n === undefined || n === null) return '-';
    if (typeof n !== 'number') return String(n);
    if (Number.isInteger(n)) return n.toString();
    return n.toFixed(2);
}

// ─── Runtimes ───

let runtimeRefreshInterval = null;

async function loadRuntimes() {
    try {
        const data = await api('GET', '/observability/runtimes');
        const runtimeCountEl = document.getElementById('runtime-count');
        const sessionCountEl = document.getElementById('session-count');
        if (runtimeCountEl) runtimeCountEl.textContent = data.active_runtime_count;
        if (sessionCountEl) sessionCountEl.textContent = data.total_session_count;

        const container = els.runtimesContainer;
        if (!data.runtimes || data.runtimes.length === 0) {
            container.innerHTML = '<div class="obs-empty">暂无活跃的运行时</div>';
            return;
        }

        container.innerHTML = `
            <table class="obs-table" id="runtimes-table">
                <thead>
                    <tr>
                        <th>Session ID</th>
                        <th>State</th>
                        <th>Chat-Only</th>
                        <th>Lock</th>
                        <th>Active Spans</th>
                        <th>Banned Tools</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody></tbody>
            </table>
        `;

        const tbody = container.querySelector('tbody');
        for (const r of data.runtimes) {
            const tr = document.createElement('tr');
            const banned = (r.failure_tracker?.banned_tools || []).join(', ') || 'none';
            const chatOnlyBadge = r.chat_only_mode
                ? `<span class="badge badge-warn">⚠️ true</span>`
                : `<span class="badge badge-success">false</span>`;
            const lockStatus = r.lock_acquired ? '🔒 locked' : 'unlocked';
            const activeSpans = r.trace ? (r.trace.active_spans.join(', ') || 'none') : 'N/A';
            const stateBadgeClass = (() => {
                const s = r.state;
                if (s === 'error') return 'badge-error';
                if (s === 'executing_tool') return 'badge-warn';
                if (s === 'completed' || s === 'idle') return 'badge-success';
                return 'badge-info';
            })();
            tr.innerHTML = `
                <td><code>${shortId(r.session_id)}</code></td>
                <td><span class="badge ${stateBadgeClass}">${r.state}</span></td>
                <td>${chatOnlyBadge}</td>
                <td>${lockStatus}</td>
                <td>${activeSpans}</td>
                <td style="max-width: 200px; word-break: break-word;">${banned}</td>
                <td><button class="obs-refresh" onclick="resetRuntime('${r.session_id}')">Reset</button></td>
            `;
            tbody.appendChild(tr);
        }
    } catch (err) {
        console.error('加载 runtimes 失败:', err);
        els.runtimesContainer.innerHTML = `<div class="obs-empty" style="color:var(--obs-danger)">加载失败: ${err.message}</div>`;
    }
}

async function resetRuntime(sessionId) {
    if (!confirm(`Reset runtime for session ${sessionId}?`)) return;
    try {
        await api('POST', `/sessions/${sessionId}/reset-runtime`);
        showToast(`Runtime reset for ${sessionId}`);
        loadRuntimes();
    } catch (err) {
        showToast(`Reset failed: ${err.message}`, 'error');
    }
}

function startRuntimeRefresh() {
    if (runtimeRefreshInterval) clearInterval(runtimeRefreshInterval);
    runtimeRefreshInterval = setInterval(loadRuntimes, 5000);
}

function stopRuntimeRefresh() {
    if (runtimeRefreshInterval) {
        clearInterval(runtimeRefreshInterval);
        runtimeRefreshInterval = null;
    }
}

// ─── SSE Events Stream ───

let eventSource = null;

function startEventStream() {
    if (eventSource) return;
    eventSource = new EventSource(`${API_BASE}/observability/events/stream`);
    eventSource.onmessage = (e) => {
        try {
            const event = JSON.parse(e.data);
            prependEventToTable(event);
        } catch (err) {
            // ignore malformed SSE data
        }
    };
    eventSource.onerror = () => {
        // auto-reconnect by browser; if permanently failed, close
        // stopEventStream();
    };
}

function stopEventStream() {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
}

function prependEventToTable(event) {
    const tbody = els.eventsTbody;
    if (!tbody) return;
    // Remove empty row if present
    if (tbody.children.length === 1 && tbody.children[0].textContent.includes('暂无事件')) {
        tbody.innerHTML = '';
    }
    const tr = document.createElement('tr');
    const time = event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : '-';
    const levelBadge = levelBadgeHtml(event.level || 'info');
    tr.innerHTML = `
        <td>${time}</td>
        <td><code>${event.type || '-'}</code></td>
        <td>${event.source || '-'}</td>
        <td>${shortId(event.session_id)}</td>
        <td>${levelBadge}</td>
    `;
    tbody.insertBefore(tr, tbody.firstChild);
    // Limit to 200 rows
    while (tbody.children.length > 200) {
        tbody.removeChild(tbody.lastChild);
    }
}

// ─── 初始化 ───

async function init() {
    await loadTabData('traces');
}

init();

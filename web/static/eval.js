/**
 * Learning-Agent · E2E 评测控制台前端
 *
 * 单页 vanilla JS。后端 API 一律在 /api/eval 下。
 *
 * 数据流:
 * - 启动:并行拉 /datasets + /runs,渲染左栏
 * - 点 dataset → 展示 dataset 详情面板,启用"运行"按钮
 * - 点"运行" → POST /runs → 拿 run_id + total_cases → 切换到 run 面板
 *   → 渲染 N 个 pending 行 → 开 EventSource 实时染色
 * - 点历史 run → fetch /runs/{id} → 渲染 results
 * - 点任一 case 行 → fetch /runs/{id}/cases/{cid} → 弹 modal
 */

const API_BASE = (typeof window !== 'undefined'
    && window.location
    && typeof window.location.origin === 'string'
    && window.location.origin.startsWith('http'))
    ? window.location.origin
    : '';

const state = {
    datasets: [],
    runs: [],
    selected: { kind: null, id: null }, // 'dataset' | 'run' | 'live'
    liveRun: null,
    eventSource: null,
};

const els = {
    datasetList: document.getElementById('dataset-list'),
    runList: document.getElementById('run-list'),
    btnRefreshRuns: document.getElementById('btn-refresh-runs'),
    btnRun: document.getElementById('btn-run'),

    viewTitle: document.getElementById('view-title'),
    viewStats: document.getElementById('view-stats'),

    panelDataset: document.getElementById('panel-dataset'),
    datasetTitle: document.getElementById('dataset-title'),
    datasetMeta: document.getElementById('dataset-meta'),
    datasetDesc: document.getElementById('dataset-desc'),

    panelRun: document.getElementById('panel-run'),
    runEyebrow: document.getElementById('run-eyebrow'),
    runTitle: document.getElementById('run-title'),
    runStatus: document.getElementById('run-status'),
    runPassed: document.getElementById('run-passed'),
    runTotal: document.getElementById('run-total'),
    runFailed: document.getElementById('run-failed'),
    runDuration: document.getElementById('run-duration'),
    runSession: document.getElementById('run-session'),
    progressBar: document.getElementById('progress-bar'),
    progressFill: document.getElementById('progress-fill'),
    caseList: document.getElementById('case-list'),

    modal: document.getElementById('case-modal'),
    modalStatus: document.getElementById('modal-status'),
    modalTitle: document.getElementById('modal-title'),
    modalCaseId: document.getElementById('modal-case-id'),
    modalDuration: document.getElementById('modal-duration'),
    modalToolCount: document.getElementById('modal-tool-count'),
    modalErrorRow: document.getElementById('modal-error-row'),
    modalError: document.getElementById('modal-error'),
    modalPrompt: document.getElementById('modal-prompt'),
    modalResponse: document.getElementById('modal-response'),
    modalTools: document.getElementById('modal-tools'),
    modalToolsSection: document.getElementById('modal-tools-section'),
};

// ────────────────────────────
// API
// ────────────────────────────

async function api(path, init = {}) {
    const resp = await fetch(API_BASE + path, {
        headers: { 'Content-Type': 'application/json' },
        ...init,
    });
    if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        throw new Error(`${resp.status} ${resp.statusText} ${path} ${text}`);
    }
    return resp.json();
}

// ────────────────────────────
// 渲染:左栏
// ────────────────────────────

function renderDatasetList() {
    if (!state.datasets.length) {
        els.datasetList.innerHTML = '<div class="empty">无数据集</div>';
        return;
    }
    els.datasetList.innerHTML = '';
    for (const ds of state.datasets) {
        const item = document.createElement('div');
        item.className = 'entity-item';
        if (state.selected.kind === 'dataset' && state.selected.id === ds.id) {
            item.classList.add('active');
        }
        item.innerHTML = `
            <div class="entity-item-title">${escapeHtml(ds.id)}</div>
            <div class="entity-item-sub">
                <span>${ds.case_count} case</span>
                <span>·</span>
                <span>${escapeHtml(ds.session_title || ds.suite_id)}</span>
            </div>
        `;
        item.addEventListener('click', () => selectDataset(ds.id));
        els.datasetList.appendChild(item);
    }
}

function renderRunList() {
    if (!state.runs.length) {
        els.runList.innerHTML = '<div class="empty">尚无运行记录</div>';
        return;
    }
    els.runList.innerHTML = '';
    for (const run of state.runs) {
        const item = document.createElement('div');
        item.className = 'entity-item';
        const isLive = state.selected.kind === 'live' && state.selected.id === run.run_id;
        const isPicked = state.selected.kind === 'run' && state.selected.id === run.run_id;
        if (isLive || isPicked) item.classList.add('active');

        const statusBadge = run.status === 'running' ? '◷ 运行中'
            : run.failed > 0 ? `✗ ${run.passed}/${run.total_cases}`
            : `✓ ${run.passed}/${run.total_cases}`;

        item.innerHTML = `
            <div class="entity-item-title">${escapeHtml(run.dataset_id || '?')}</div>
            <div class="entity-item-sub">
                <span>${statusBadge}</span>
                <span>·</span>
                <span>${escapeHtml(formatTs(run.started_at))}</span>
            </div>
        `;
        item.title = run.run_id;
        item.addEventListener('click', () => selectRun(run.run_id));
        els.runList.appendChild(item);
    }
}

// ────────────────────────────
// 渲染:主区
// ────────────────────────────

function renderDatasetPanel(ds) {
    els.panelDataset.hidden = false;
    els.panelRun.hidden = true;
    els.viewTitle.textContent = ds.id;
    els.viewStats.textContent = `${ds.case_count} case`;
    els.btnRun.disabled = !!state.liveRun;

    els.datasetTitle.textContent = ds.session_title || ds.suite_id;
    els.datasetMeta.innerHTML = `
        <dt>id</dt><dd>${escapeHtml(ds.id)}</dd>
        <dt>suite_id</dt><dd>${escapeHtml(ds.suite_id)}</dd>
        <dt>case_count</dt><dd>${ds.case_count}</dd>
        <dt>path</dt><dd>${escapeHtml(ds.path)}</dd>
    `;
    els.datasetDesc.textContent = ds.description || '(无描述)';
}

function renderRunPanel(run, results) {
    els.panelDataset.hidden = true;
    els.panelRun.hidden = false;

    els.viewTitle.textContent = run.run_id || '—';
    els.viewStats.textContent = `${run.dataset_id || ''}`;

    els.runEyebrow.textContent = run.status === 'running' ? '运行中' : '已完成';
    els.runTitle.textContent = run.dataset_id || run.run_id || '—';
    els.runStatus.textContent = statusLabel(run);
    els.runStatus.className = 'badge ' + statusClass(run);
    els.runPassed.textContent = run.passed ?? 0;
    els.runTotal.textContent = run.total_cases ?? (results ? results.length : 0);
    els.runFailed.textContent = run.failed ?? 0;
    els.runDuration.textContent = formatDuration(run);
    els.runSession.textContent = run.session_id || '—';

    els.progressBar.hidden = run.status !== 'running';
    updateProgressBar(run);

    if (!results || !results.length) {
        els.caseList.innerHTML = '<div class="empty">尚无 case 数据</div>';
        return;
    }
    els.caseList.innerHTML = '';
    for (let i = 0; i < results.length; i++) {
        const r = results[i];
        els.caseList.appendChild(makeCaseRow(i, r, run.run_id));
    }
}

function makeCaseRow(index, result, runId) {
    const row = document.createElement('div');
    row.className = 'case-row ' + caseStateClass(result);
    row.dataset.index = String(index);
    if (result.case_id) row.dataset.caseId = result.case_id;

    const icon = caseIcon(result);
    const title = result.title || '';
    const tools = (result.tool_calls_count ?? result.tool_calls?.length ?? 0);
    const dur = result.duration_ms != null ? `${(result.duration_ms / 1000).toFixed(1)}s` : '—';

    row.innerHTML = `
        <div class="case-icon">${icon}</div>
        <div class="case-id-title">
            <div class="case-id">${escapeHtml(result.case_id || `(case ${index + 1})`)}</div>
            <div class="case-title">${escapeHtml(title)}</div>
        </div>
        <div class="case-tools">${tools} tools</div>
        <div class="case-duration">${dur}</div>
    `;
    row.addEventListener('click', () => {
        if (result.case_id) openCaseModal(runId, result.case_id, result);
    });
    return row;
}

function caseStateClass(result) {
    if (result.status === 'pending' || result.case_id == null) return 'pending';
    if (result.status === 'running') return 'running';
    if (result.success === true) return 'success';
    if (result.success === false) return 'failed';
    return 'pending';
}
function caseIcon(result) {
    if (result.status === 'running') return '◷';
    if (result.success === true) return '✓';
    if (result.success === false) return '✗';
    return '·';
}

function statusLabel(run) {
    if (run.status === 'running') return '运行中';
    if ((run.failed || 0) > 0) return '有失败';
    if ((run.total_cases || 0) > 0) return '全通过';
    return '已完成';
}
function statusClass(run) {
    if (run.status === 'running') return 'running';
    if ((run.failed || 0) > 0) return 'failed';
    return 'done';
}

function updateProgressBar(run) {
    const total = run.total_cases || 0;
    const done = (run.passed || 0) + (run.failed || 0);
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    els.progressFill.style.width = pct + '%';
}

// ────────────────────────────
// 选择/触发
// ────────────────────────────

function selectDataset(id) {
    const ds = state.datasets.find(d => d.id === id);
    if (!ds) return;
    state.selected = { kind: 'dataset', id };
    renderDatasetList();
    renderRunList();
    renderDatasetPanel(ds);
}

async function selectRun(runId) {
    state.selected = { kind: 'run', id: runId };
    renderDatasetList();
    renderRunList();
    els.viewTitle.textContent = runId;
    els.viewStats.textContent = '加载中…';
    try {
        const data = await api(`/api/eval/runs/${encodeURIComponent(runId)}`);
        const runMeta = {
            run_id: data.run_id,
            dataset_id: data.dataset_id,
            session_id: data.manifest?.session_id,
            total_cases: data.summary?.total_cases ?? data.results.length,
            passed: data.summary?.passed ?? data.results.filter(r => r.success).length,
            failed: data.summary?.failed ?? data.results.filter(r => !r.success).length,
            duration_ms: data.summary?.total_duration_ms,
            status: data.status,
            started_at: data.manifest?.start_time,
            ended_at: data.manifest?.end_time,
        };
        renderRunPanel(runMeta, data.results);
    } catch (e) {
        els.viewStats.textContent = '加载失败';
        els.caseList.innerHTML = `<div class="empty">${escapeHtml(String(e))}</div>`;
    }
}

async function triggerRun() {
    if (state.selected.kind !== 'dataset') return;
    if (state.liveRun) return;
    const datasetId = state.selected.id;
    els.btnRun.disabled = true;

    let resp;
    try {
        resp = await api('/api/eval/runs', {
            method: 'POST',
            body: JSON.stringify({ dataset_id: datasetId }),
        });
    } catch (e) {
        alert('启动失败: ' + e.message);
        els.btnRun.disabled = false;
        return;
    }

    const live = {
        run_id: resp.run_id,
        dataset_id: resp.dataset_id,
        total_cases: resp.total_cases,
        passed: 0,
        failed: 0,
        duration_ms: 0,
        status: 'running',
        started_at: new Date().toISOString(),
        session_id: null,
        results: Array.from({ length: resp.total_cases }, (_, i) => ({
            status: 'pending',
            case_id: null,
            title: '',
            success: null,
            duration_ms: null,
        })),
    };
    state.liveRun = live;
    state.selected = { kind: 'live', id: live.run_id };

    // 立刻插入 runs 头部 + 切到 run 面板
    state.runs.unshift({
        run_id: live.run_id,
        dataset_id: live.dataset_id,
        started_at: live.started_at,
        total_cases: live.total_cases,
        passed: 0,
        failed: 0,
        status: 'running',
    });
    renderRunList();
    renderRunPanel(live, live.results);

    openEventStream(resp.stream_url || `/api/eval/runs/${encodeURIComponent(live.run_id)}/stream`);
}

function openEventStream(url) {
    if (state.eventSource) {
        try { state.eventSource.close(); } catch (_) {}
    }
    const es = new EventSource(API_BASE + url);
    state.eventSource = es;

    es.addEventListener('run_started', (ev) => {
        const data = safeParse(ev.data);
        if (!data) return;
        const live = state.liveRun;
        if (!live) return;
        live.session_id = data.session_id;
        if (data.total_cases) {
            live.total_cases = data.total_cases;
            // 不重置已有 results,只补齐长度。
            while (live.results.length < data.total_cases) {
                live.results.push({ status: 'pending', case_id: null });
            }
        }
        renderRunPanel(live, live.results);
    });

    es.addEventListener('case_done', (ev) => {
        const data = safeParse(ev.data);
        if (!data) return;
        const live = state.liveRun;
        if (!live) return;
        const idx = data.index ?? -1;
        if (idx >= 0 && idx < live.results.length) {
            live.results[idx] = {
                case_id: data.case_id,
                title: data.title,
                success: data.success,
                error: data.error,
                duration_ms: data.duration_ms,
                tool_calls_count: data.tool_calls_count,
                status: 'done',
            };
        } else {
            live.results.push({
                case_id: data.case_id,
                title: data.title,
                success: data.success,
                error: data.error,
                duration_ms: data.duration_ms,
                tool_calls_count: data.tool_calls_count,
                status: 'done',
            });
        }
        if (data.success) live.passed += 1; else live.failed += 1;
        // 也同步左栏 runs 项
        const r = state.runs.find(x => x.run_id === live.run_id);
        if (r) { r.passed = live.passed; r.failed = live.failed; }
        renderRunPanel(live, live.results);
        renderRunList();
    });

    es.addEventListener('run_finished', (ev) => {
        const data = safeParse(ev.data);
        if (!data) return;
        const live = state.liveRun;
        if (!live) return;
        live.status = 'done';
        if (data.summary) {
            live.passed = data.summary.passed ?? live.passed;
            live.failed = data.summary.failed ?? live.failed;
            live.duration_ms = data.summary.total_duration_ms;
            live.total_cases = data.summary.total_cases ?? live.total_cases;
        }
        const r = state.runs.find(x => x.run_id === live.run_id);
        if (r) {
            r.status = 'done';
            r.passed = live.passed;
            r.failed = live.failed;
            r.total_cases = live.total_cases;
            r.ended_at = new Date().toISOString();
        }
        renderRunPanel(live, live.results);
        renderRunList();
    });

    es.addEventListener('done', () => {
        const live = state.liveRun;
        if (live) live.status = 'done';
        closeEventStream();
        // run 跑完后顺手刷一遍 runs(拿到正式 manifest)
        loadRuns().catch(() => {});
    });

    es.addEventListener('error', (ev) => {
        // EventSource 心跳错误也会触发,只有彻底断了才关
        if (es.readyState === EventSource.CLOSED) {
            closeEventStream();
        }
    });
}

function closeEventStream() {
    if (state.eventSource) {
        try { state.eventSource.close(); } catch (_) {}
        state.eventSource = null;
    }
    if (state.liveRun) {
        state.liveRun = null;
    }
    els.btnRun.disabled = state.selected.kind !== 'dataset';
}

// ────────────────────────────
// Modal
// ────────────────────────────

async function openCaseModal(runId, caseId, fallback) {
    els.modal.hidden = false;
    els.modalTitle.textContent = caseId;
    els.modalCaseId.textContent = caseId;
    els.modalStatus.textContent = '加载中…';
    els.modalStatus.className = 'modal-status';
    els.modalPrompt.textContent = '—';
    els.modalResponse.textContent = '—';
    els.modalTools.textContent = '—';
    els.modalErrorRow.hidden = true;

    if (!runId || !caseId) {
        els.modalStatus.textContent = '无数据';
        return;
    }

    let request = null, response = null;
    try {
        const data = await api(`/api/eval/runs/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}`);
        request = data.request;
        response = data.response;
    } catch (e) {
        // 落盘前(刚跑完)case 目录可能还没有,只展示 fallback
    }

    const userPrompt = pickUserPrompt(request, fallback);
    const responseText = response?.response_text ?? response?.content ?? fallback?.response_text ?? fallback?.error ?? '(无响应)';
    const toolCalls = response?.tool_calls ?? fallback?.tool_calls ?? [];
    const duration = fallback?.duration_ms ?? response?.duration_ms;
    const success = fallback?.success ?? (response ? !response.error : null);

    els.modalTitle.textContent = (fallback?.title || caseId);
    els.modalStatus.textContent = success === true ? '✓ 成功' : (success === false ? '✗ 失败' : '—');
    els.modalStatus.className = 'modal-status ' + (success === true ? 'success' : success === false ? 'failed' : '');
    els.modalDuration.textContent = duration != null ? `${duration} ms` : '—';
    els.modalToolCount.textContent = String(toolCalls.length);
    if (fallback?.error) {
        els.modalErrorRow.hidden = false;
        els.modalError.textContent = String(fallback.error);
    }
    els.modalPrompt.textContent = userPrompt || '(无用户输入)';
    els.modalResponse.textContent = responseText || '(无响应)';
    els.modalTools.textContent = toolCalls.length
        ? JSON.stringify(toolCalls, null, 2)
        : '(无工具调用)';
    els.modalToolsSection.hidden = !toolCalls.length;
}

function pickUserPrompt(request, fallback) {
    // request 可能是 {user_message, case_id, title}(runner 落盘),也可能是 chat 请求结构。
    if (!request && fallback) return fallback.user_message || fallback.message || '';
    if (typeof request === 'string') return request;
    return request?.user_message
        || request?.message
        || request?.input?.message
        || request?.user_prompt
        || JSON.stringify(request, null, 2);
}

function closeModal() {
    els.modal.hidden = true;
}

// ────────────────────────────
// 加载入口
// ────────────────────────────

async function loadDatasets() {
    const data = await api('/api/eval/datasets');
    state.datasets = data.datasets || [];
    renderDatasetList();
}
async function loadRuns() {
    const data = await api('/api/eval/runs');
    state.runs = data.runs || [];
    renderRunList();
}

// ────────────────────────────
// 工具函数
// ────────────────────────────

function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
function safeParse(s) {
    try { return JSON.parse(s); } catch { return null; }
}
function formatTs(s) {
    if (!s) return '—';
    // "2026-05-27T095315Z" → "05-27 09:53"
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2})(\d{2})(\d{2})Z?$/);
    if (m) return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
    return s;
}
function formatDuration(run) {
    const ms = run.duration_ms ?? run.total_duration_ms;
    if (ms == null) {
        if (run.started_at && run.ended_at) {
            return calcDuration(run.started_at, run.ended_at);
        }
        return '—';
    }
    return `${(ms / 1000).toFixed(1)}s`;
}
function calcDuration(start, end) {
    try {
        const a = new Date(start.replace(/(\d{4}-\d{2}-\d{2}T)(\d{2})(\d{2})(\d{2})Z/, '$1$2:$3:$4Z'));
        const b = new Date(end.replace(/(\d{4}-\d{2}-\d{2}T)(\d{2})(\d{2})(\d{2})Z/, '$1$2:$3:$4Z'));
        const sec = (b - a) / 1000;
        if (isNaN(sec) || sec < 0) return '—';
        return `${sec.toFixed(1)}s`;
    } catch { return '—'; }
}

// ────────────────────────────
// 事件绑定
// ────────────────────────────

els.btnRun.addEventListener('click', triggerRun);
els.btnRefreshRuns.addEventListener('click', () => loadRuns().catch(console.error));
els.modal.addEventListener('click', (ev) => {
    if (ev.target.dataset.close === '1') closeModal();
});
document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !els.modal.hidden) closeModal();
});

// 启动
Promise.all([loadDatasets(), loadRuns()]).catch(err => {
    console.error('initial load failed:', err);
});

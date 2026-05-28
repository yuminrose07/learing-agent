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
    datasetDetails: new Map(),
    caseQuery: '',
    datasetQuery: '',
    runQuery: '',
    runResultFilter: 'all',
    currentRunView: null,
    caseRunResults: new Map(),
    dirtyCases: new Set(),
    caseDrafts: new Map(),
    deletedCase: null,
    liveRun: null,
    eventSource: null,
};

const els = {
    datasetList: document.getElementById('dataset-list'),
    runList: document.getElementById('run-list'),
    datasetSearch: document.getElementById('dataset-search'),
    runSearch: document.getElementById('run-search'),
    btnRefreshRuns: document.getElementById('btn-refresh-runs'),
    btnRun: document.getElementById('btn-run'),
    runScope: document.getElementById('run-scope'),

    viewTitle: document.getElementById('view-title'),
    viewStats: document.getElementById('view-stats'),

    panelDataset: document.getElementById('panel-dataset'),
    datasetTitle: document.getElementById('dataset-title'),
    datasetMeta: document.getElementById('dataset-meta'),
    datasetDesc: document.getElementById('dataset-desc'),
    caseSearch: document.getElementById('case-search'),
    btnAddCase: document.getElementById('btn-add-case'),
    btnCompile: document.getElementById('btn-compile'),
    datasetAlert: document.getElementById('dataset-alert'),
    datasetCaseEditor: document.getElementById('dataset-case-editor'),

    panelRun: document.getElementById('panel-run'),
    runEyebrow: document.getElementById('run-eyebrow'),
    runTitle: document.getElementById('run-title'),
    runStatus: document.getElementById('run-status'),
    runPassed: document.getElementById('run-passed'),
    runTotal: document.getElementById('run-total'),
    runFailed: document.getElementById('run-failed'),
    runSkipped: document.getElementById('run-skipped'),
    runDuration: document.getElementById('run-duration'),
    runSession: document.getElementById('run-session'),
    progressBar: document.getElementById('progress-bar'),
    progressFill: document.getElementById('progress-fill'),
    runFilter: document.getElementById('run-filter'),
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
    modalFailReasonsSection: document.getElementById('modal-fail-reasons-section'),
    modalFailReasons: document.getElementById('modal-fail-reasons'),
    modalEventsSection: document.getElementById('modal-events-section'),
    modalEventsCount: document.getElementById('modal-events-count'),
    modalEvents: document.getElementById('modal-events'),
    modalUnresolvedSection: document.getElementById('modal-unresolved-section'),
    modalUnresolvedCount: document.getElementById('modal-unresolved-count'),
    modalUnresolved: document.getElementById('modal-unresolved'),
    btnModalRerun: document.getElementById('btn-modal-rerun'),
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
    const datasets = filterDatasets(state.datasets);
    if (!datasets.length) {
        els.datasetList.innerHTML = '<div class="empty">无数据集</div>';
        return;
    }
    els.datasetList.innerHTML = '';
    for (const ds of datasets) {
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
                ${datasetLastRunHtml(ds)}
                ${datasetCompileHtml(ds)}
            </div>
        `;
        item.addEventListener('click', () => selectDataset(ds.id));
        els.datasetList.appendChild(item);
    }
}

function renderRunList() {
    const runs = filterRuns(state.runs);
    if (!runs.length) {
        els.runList.innerHTML = '<div class="empty">尚无运行记录</div>';
        return;
    }
    els.runList.innerHTML = '';
    for (const run of runs) {
        const item = document.createElement('div');
        item.className = 'entity-item run-item';
        const isLive = state.selected.kind === 'live' && state.selected.id === run.run_id;
        const isPicked = state.selected.kind === 'run' && state.selected.id === run.run_id;
        if (isLive || isPicked) item.classList.add('active');

        const failed = Number(run.failed || 0);
        const skipped = Number(run.skipped || 0);
        const passed = Number(run.passed || 0);
        const total = Number(run.total_cases || 0);
        const isRunning = run.status === 'running';
        const statusBadge = isRunning ? '◷ 运行中'
            : failed > 0 ? `✗ ${passed}/${total}`
            : skipped > 0 ? `⊘ ${passed}/${total} · ${skipped}跳过`
            : `✓ ${passed}/${total}`;

        item.innerHTML = `
            <div class="entity-item-title">${escapeHtml(run.dataset_id || '?')}</div>
            <div class="entity-item-sub">
                <span>${statusBadge}</span>
                <span>·</span>
                <span>${escapeHtml(formatTs(run.started_at))}</span>
            </div>
            <button class="entity-item-delete" type="button"
                title="${isRunning ? '运行中无法删除' : '删除该运行结果'}"
                aria-label="删除运行结果"
                ${isRunning ? 'disabled' : ''}>✕</button>
        `;
        item.title = run.run_id;
        item.addEventListener('click', () => selectRun(run.run_id));
        const delBtn = item.querySelector('.entity-item-delete');
        if (delBtn) {
            delBtn.addEventListener('click', (ev) => {
                ev.stopPropagation();
                if (delBtn.disabled) return;
                deleteRun(run.run_id).catch((e) => alert(`删除失败: ${e.message || e}`));
            });
        }
        els.runList.appendChild(item);
    }
}

async function deleteRun(runId) {
    if (!confirm(`确认删除运行结果\n${runId}\n\n会删掉该次运行的证据包、进度文件、runner 日志。不可恢复。`)) {
        return;
    }
    await api(`/api/eval/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' });
    state.runs = state.runs.filter((r) => r.run_id !== runId);
    if (state.selected.kind === 'run' && state.selected.id === runId) {
        state.selected = { kind: null, id: null };
        els.viewTitle.textContent = '选择一个数据集或运行结果';
        els.viewStats.textContent = '';
        els.caseList.innerHTML = '<div class="empty">已删除该运行结果</div>';
    }
    renderRunList();
    loadDatasets().catch(() => {});  // 数据集列表的 last_run 摘要可能要刷新
}

function datasetLastRunHtml(ds) {
    const run = ds.last_run;
    if (!run) return '';
    const failed = Number(run.failed || 0);
    const skipped = Number(run.skipped || 0);
    const passed = Number(run.passed || 0);
    const total = Number(run.total_cases || 0);
    const cls = run.status === 'running' ? 'running' : failed > 0 ? 'failed' : skipped > 0 ? 'skipped' : 'success';
    const label = run.status === 'running' ? '运行中'
        : skipped > 0 ? `${passed}/${total} 通过 · ${skipped}跳过`
        : `${passed}/${total} 通过`;
    return `<span>·</span><span class="entity-run-status ${cls}">${escapeHtml(label)}</span>`;
}

function datasetCompileHtml(ds) {
    const stateName = ds.compile_status?.state;
    if (!stateName || stateName === 'json_only') return '';
    const label = stateName === 'current' ? '已编译'
        : stateName === 'stale' ? '待编译'
        : stateName === 'source_only' ? '未编译'
        : stateName === 'missing' ? '缺 JSON'
        : '';
    if (!label) return '';
    return `<span>·</span><span class="entity-compile-status ${escapeHtml(stateName)}">${escapeHtml(label)}</span>`;
}

function compileStatusText(ds) {
    const stateName = ds?.compile_status?.state;
    const label = stateName === 'current' ? '已编译'
        : stateName === 'stale' ? '待编译'
        : stateName === 'source_only' ? '未编译'
        : stateName === 'json_only' ? '仅 JSON'
        : stateName === 'missing' ? '缺少 JSON'
        : '未知';
    const path = ds?.compile_status?.json_path;
    return path ? `${label} · ${path}` : label;
}

function datasetMetaHtml(ds) {
    return `
        <dt>id</dt><dd>${escapeHtml(ds.id)}</dd>
        <dt>suite_id</dt><dd>${escapeHtml(ds.suite_id)}</dd>
        <dt>case_count</dt><dd>${ds.case_count}</dd>
        <dt>source</dt><dd>${escapeHtml(ds.source_type || 'json')}</dd>
        <dt>compile</dt><dd>${escapeHtml(compileStatusText(ds))}</dd>
        <dt>path</dt><dd>${escapeHtml(ds.source_path || ds.path)}</dd>
    `;
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
    els.runScope.disabled = false;

    els.datasetTitle.textContent = ds.session_title || ds.suite_id;
    els.datasetMeta.innerHTML = datasetMetaHtml(ds);
    els.datasetDesc.textContent = ds.description || '(无描述)';
    hideDatasetAlert();
    els.caseSearch.value = state.caseQuery || '';
    els.datasetCaseEditor.innerHTML = '<div class="empty">加载 case…</div>';
    loadDatasetDetail(ds.id).catch((e) => {
        els.datasetCaseEditor.innerHTML = `<div class="empty">加载失败: ${escapeHtml(String(e))}</div>`;
    });
}

function renderDatasetCases(detail) {
    const cases = detail?.cases || [];
    const editable = detail?.editable === true;
    const filtered = visibleDatasetCases(detail);

    if (!editable) {
        els.btnAddCase.disabled = true;
        els.btnCompile.disabled = true;
    } else {
        els.btnAddCase.disabled = false;
        els.btnCompile.disabled = false;
    }

    if (!filtered.length) {
        els.datasetCaseEditor.innerHTML = '<div class="empty">没有匹配的 case</div>';
        return;
    }

    els.datasetCaseEditor.innerHTML = '';
    for (const item of filtered) {
        const block = document.createElement('section');
        block.className = 'dataset-case-block';
        block.dataset.caseId = item.id;
        const isDraft = String(item.id || '').startsWith('__new__');
        const draft = getCaseDraft(detail.dataset.id, item.id, item);
        const visibleId = isDraft ? draft.id : item.id;
        const runState = !isDraft ? state.caseRunResults.get(caseResultKey(detail.dataset.id, item.id)) : null;
        const isDirty = isCaseDirty(detail.dataset.id, item.id);
        if (runState?.status) block.classList.add(runState.status);
        if (isDirty) block.classList.add('dirty');
        const statusText = isDirty ? '未保存' : (runState ? formatCaseRunStatus(runState) : '');
        block.innerHTML = `
            <div class="dataset-case-header">
                <input class="dataset-case-id-input" value="${escapeHtml(visibleId)}" ${isDraft ? '' : 'readonly'} ${editable ? '' : 'disabled'}>
                <div class="dataset-case-actions">
                    <button class="mini-action" type="button" data-action="run" ${state.liveRun || isDirty ? 'disabled' : ''}>运行</button>
                    <button class="mini-action" type="button" data-action="save" ${editable && isDirty ? '' : 'disabled'}>保存</button>
                    <button class="mini-action danger" type="button" data-action="delete" ${editable ? '' : 'disabled'}>删除</button>
                </div>
            </div>
            <textarea class="dataset-case-content" spellcheck="false" ${editable ? '' : 'readonly'}>${escapeHtml(draft.message || '')}</textarea>
            <div class="dataset-case-status ${escapeHtml(isDirty ? 'dirty' : (runState?.status || ''))}" aria-live="polite">${escapeHtml(statusText)}</div>
        `;
        block.addEventListener('click', onDatasetCaseAction);
        block.addEventListener('input', onDatasetCaseInput);
        els.datasetCaseEditor.appendChild(block);
    }
}

function renderRunPanel(run, results) {
    state.currentRunView = { run, results: results || [] };
    els.panelDataset.hidden = true;
    els.panelRun.hidden = false;
    els.btnRun.disabled = true;
    els.runScope.disabled = true;

    els.viewTitle.textContent = run.run_id || '—';
    els.viewStats.textContent = `${run.dataset_id || ''}`;

    els.runEyebrow.textContent = run.status === 'running' ? '运行中' : '已完成';
    els.runTitle.textContent = run.dataset_id || run.run_id || '—';
    els.runStatus.textContent = statusLabel(run);
    els.runStatus.className = 'badge ' + statusClass(run);
    els.runPassed.textContent = run.passed ?? 0;
    els.runTotal.textContent = run.total_cases ?? (results ? results.length : 0);
    els.runFailed.textContent = run.failed ?? 0;
    els.runSkipped.textContent = run.skipped ?? 0;
    els.runDuration.textContent = formatDuration(run);
    els.runSession.textContent = run.session_id || '—';

    els.progressBar.hidden = run.status !== 'running';
    updateProgressBar(run);
    renderRunFilter();

    if (!results || !results.length) {
        els.caseList.innerHTML = '<div class="empty">尚无 case 数据</div>';
        return;
    }
    const visibleResults = filterRunResults(results);
    if (!visibleResults.length) {
        els.caseList.innerHTML = '<div class="empty">没有匹配的运行结果</div>';
        return;
    }
    els.caseList.innerHTML = '';
    for (let i = 0; i < visibleResults.length; i++) {
        const r = visibleResults[i];
        els.caseList.appendChild(makeCaseRow(r.__index ?? i, r, run.run_id));
    }
}

function renderRunFilter() {
    for (const btn of els.runFilter.querySelectorAll('.filter-btn')) {
        btn.classList.toggle('active', btn.dataset.filter === state.runResultFilter);
    }
}

function visibleDatasetCases(detail) {
    const cases = detail?.cases || [];
    const query = (state.caseQuery || '').trim().toLowerCase();
    if (!query) return cases;
    return cases.filter(c => String(c.id || '').toLowerCase().includes(query)
        || String(getCaseDraft(detail.dataset.id, c.id, c).message || '').toLowerCase().includes(query));
}

function makeCaseRow(index, result, runId) {
    const row = document.createElement('div');
    row.className = 'case-row ' + caseStateClass(result);
    row.dataset.index = String(index);
    if (result.case_id) row.dataset.caseId = result.case_id;

    const icon = caseIcon(result);
    const stateName = caseStateClass(result);
    const title = stateName === 'skipped' ? (result.skipped_reason || result.title || '') : (result.title || '');
    const tools = stateName === 'skipped' ? 'skipped' : `${(result.tool_calls_count ?? result.tool_calls?.length ?? 0)} tools`;
    const dur = result.duration_ms != null ? `${(result.duration_ms / 1000).toFixed(1)}s` : '—';

    row.innerHTML = `
        <div class="case-icon">${icon}</div>
        <div class="case-id-title">
            <div class="case-id">${escapeHtml(result.case_id || `(case ${index + 1})`)}</div>
            <div class="case-title">${escapeHtml(title)}</div>
        </div>
        <div class="case-tools">${escapeHtml(tools)}</div>
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
    if (result.status === 'skipped' || result.verdict === 'skipped' || result.skipped_reason) return 'skipped';
    if (result.verdict === 'pass') return 'success';
    if (result.verdict === 'fail') return 'failed';
    if (result.success === true) return 'success';
    if (result.success === false) return 'failed';
    return 'pending';
}
function caseIcon(result) {
    const stateName = caseStateClass(result);
    if (stateName === 'running') return '◷';
    if (stateName === 'skipped') return '⊘';
    if (stateName === 'success') return '✓';
    if (stateName === 'failed') return '✗';
    return '·';
}

function statusLabel(run) {
    if (run.status === 'running') return '运行中';
    if ((run.failed || 0) > 0) return '有失败';
    if ((run.skipped || 0) > 0) return '有跳过';
    if ((run.total_cases || 0) > 0) return '全通过';
    return '已完成';
}
function statusClass(run) {
    if (run.status === 'running') return 'running';
    if ((run.failed || 0) > 0) return 'failed';
    if ((run.skipped || 0) > 0) return 'skipped';
    return 'done';
}

function updateProgressBar(run) {
    const total = run.total_cases || 0;
    const done = (run.passed || 0) + (run.failed || 0) + (run.skipped || 0);
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    els.progressFill.style.width = pct + '%';
}

// ────────────────────────────
// 选择/触发
// ────────────────────────────

function selectDataset(id) {
    if (state.selected.kind === 'dataset' && state.selected.id === id) return;
    if (!confirmDiscardUnsaved()) return;
    const ds = state.datasets.find(d => d.id === id);
    if (!ds) return;
    state.selected = { kind: 'dataset', id };
    state.caseQuery = '';
    renderDatasetList();
    renderRunList();
    renderDatasetPanel(ds);
}

async function loadDatasetDetail(datasetId, { force = false } = {}) {
    if (!force && state.datasetDetails.has(datasetId)) {
        renderDatasetCases(state.datasetDetails.get(datasetId));
        return state.datasetDetails.get(datasetId);
    }
    clearDatasetDrafts(datasetId);
    const detail = await api(`/api/eval/datasets/${encodeURIComponent(datasetId)}`);
    state.datasetDetails.set(datasetId, detail);
    renderDatasetCases(detail);
    const ds = state.datasets.find(d => d.id === datasetId);
    if (ds && detail.dataset) {
        Object.assign(ds, detail.dataset);
        renderDatasetList();
        if (state.selected.kind === 'dataset' && state.selected.id === datasetId) {
            els.viewStats.textContent = `${ds.case_count} case`;
            els.datasetMeta.innerHTML = datasetMetaHtml(ds);
        }
    }
    return detail;
}

async function selectRun(runId) {
    if (!confirmDiscardUnsaved()) return;
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
            passed: data.summary?.passed ?? data.results.filter(r => caseStateClass(r) === 'success').length,
            failed: data.summary?.failed ?? data.results.filter(r => caseStateClass(r) === 'failed').length,
            skipped: data.summary?.skipped ?? data.results.filter(r => caseStateClass(r) === 'skipped').length,
            duration_ms: data.summary?.total_duration_ms,
            status: data.status,
            started_at: data.manifest?.start_time,
            ended_at: data.manifest?.end_time,
        };
        recordRunCaseResults(data.dataset_id, data.run_id, data.results);
        renderRunPanel(runMeta, data.results);
    } catch (e) {
        els.viewStats.textContent = '加载失败';
        els.caseList.innerHTML = `<div class="empty">${escapeHtml(String(e))}</div>`;
    }
}

async function triggerRun(caseId = null) {
    if (state.selected.kind !== 'dataset') return;
    if (state.liveRun) return;
    if (hasUnsavedCases()) {
        showDatasetAlert('有未保存的 case，请先保存或放弃修改后再运行。', { tone: 'error' });
        return;
    }
    const datasetId = state.selected.id;
    const detail = state.datasetDetails.get(datasetId) || await loadDatasetDetail(datasetId);
    const selectedCaseIds = resolveRunCaseIds(caseId, detail);
    if (selectedCaseIds === false) return;
    els.btnRun.disabled = true;
    els.runScope.disabled = true;
    const isSingleCase = selectedCaseIds.length === 1;
    if (isSingleCase) {
        state.caseRunResults.set(caseResultKey(datasetId, selectedCaseIds[0]), { status: 'running' });
    } else {
        const idsToMark = selectedCaseIds.length ? selectedCaseIds : (detail?.cases || []).map(item => item.id);
        for (const id of idsToMark) {
            state.caseRunResults.set(caseResultKey(datasetId, id), { status: 'running' });
        }
    }
    if (detail) renderDatasetCases(detail);

    let resp;
    const body = { dataset_id: datasetId };
    if (API_BASE) {
        body.base_url = API_BASE;
    }
    if (selectedCaseIds.length === 1) {
        body.case_id = selectedCaseIds[0];
    } else if (selectedCaseIds.length > 1 && selectedCaseIds.length < (detail?.cases?.length || 0)) {
        body.case_ids = selectedCaseIds;
    }
    try {
        resp = await api('/api/eval/runs', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    } catch (e) {
        alert('启动失败: ' + e.message);
        const idsToMark = selectedCaseIds.length ? selectedCaseIds : (detail?.cases || []).map(item => item.id);
        for (const id of idsToMark) {
            state.caseRunResults.set(caseResultKey(datasetId, id), { status: 'failed', error: e.message });
        }
        if (detail) renderDatasetCases(detail);
        els.btnRun.disabled = false;
        els.runScope.disabled = false;
        return;
    }

    const live = {
        run_id: resp.run_id,
        dataset_id: resp.dataset_id,
        case_id: resp.case_id,
        case_ids: resp.case_ids || selectedCaseIds,
        total_cases: resp.total_cases,
        passed: 0,
        failed: 0,
        skipped: 0,
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
        inline_case_mode: isSingleCase,
    };
    state.liveRun = live;
    if (!live.inline_case_mode) {
        state.selected = { kind: 'live', id: live.run_id };
    }

    // 立刻插入 runs 头部 + 切到 run 面板
    state.runs.unshift({
        run_id: live.run_id,
        dataset_id: live.dataset_id,
        started_at: live.started_at,
        total_cases: live.total_cases,
        passed: 0,
        failed: 0,
        skipped: 0,
        status: 'running',
    });
    renderRunList();
    if (!live.inline_case_mode) {
        renderRunPanel(live, live.results);
    } else {
        els.btnRun.disabled = true;
        els.runScope.disabled = true;
    }

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
        if (!live.inline_case_mode) renderRunPanel(live, live.results);
    });

    es.addEventListener('case_done', (ev) => {
        const data = safeParse(ev.data);
        if (!data) return;
        const live = state.liveRun;
        if (!live) return;
        const idx = data.index ?? -1;
        const resultRecord = {
            case_id: data.case_id,
            title: data.title,
            success: data.success,
            verdict: data.verdict,
            skipped_reason: data.skipped_reason,
            error: data.error,
            duration_ms: data.duration_ms,
            tool_calls_count: data.tool_calls_count,
            status: 'done',
        };
        if (idx >= 0 && idx < live.results.length) {
            live.results[idx] = resultRecord;
        } else {
            live.results.push(resultRecord);
        }
        const outcome = caseStateClass(resultRecord);
        if (outcome === 'success') live.passed += 1;
        else if (outcome === 'failed') live.failed += 1;
        else if (outcome === 'skipped') live.skipped += 1;
        state.caseRunResults.set(caseResultKey(live.dataset_id, data.case_id), {
            status: outcome,
            error: data.error || data.skipped_reason,
            duration_ms: data.duration_ms,
            run_id: live.run_id,
        });
        const detail = state.datasetDetails.get(live.dataset_id);
        if (detail && state.selected.kind === 'dataset' && state.selected.id === live.dataset_id) {
            renderDatasetCases(detail);
        }
        // 也同步左栏 runs 项
        const r = state.runs.find(x => x.run_id === live.run_id);
        if (r) { r.passed = live.passed; r.failed = live.failed; r.skipped = live.skipped; }
        if (!live.inline_case_mode) renderRunPanel(live, live.results);
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
            live.skipped = data.summary.skipped ?? live.skipped;
            live.duration_ms = data.summary.total_duration_ms;
            live.total_cases = data.summary.total_cases ?? live.total_cases;
        }
        const r = state.runs.find(x => x.run_id === live.run_id);
        if (r) {
            r.status = 'done';
            r.passed = live.passed;
            r.failed = live.failed;
            r.skipped = live.skipped;
            r.total_cases = live.total_cases;
            r.ended_at = new Date().toISOString();
        }
        if (!live.inline_case_mode) renderRunPanel(live, live.results);
        renderRunList();
        updateDatasetLastRunFromLive(live);
    });

    es.addEventListener('done', () => {
        const live = state.liveRun;
        if (live) live.status = 'done';
        closeEventStream();
        // run 跑完后顺手刷一遍 runs(拿到正式 manifest)
        loadRuns().catch(() => {});
        loadDatasets().catch(() => {});
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
    els.runScope.disabled = state.selected.kind !== 'dataset';
    if (state.selected.kind === 'dataset') {
        const detail = state.datasetDetails.get(state.selected.id);
        if (detail) renderDatasetCases(detail);
    }
}

// ────────────────────────────
// Dataset case CRUD
// ────────────────────────────

async function onDatasetCaseAction(ev) {
    const btn = ev.target.closest('button[data-action]');
    if (!btn) return;
    const block = ev.currentTarget;
    const action = btn.dataset.action;
    const caseId = block.dataset.caseId;
    const textarea = block.querySelector('.dataset-case-content');
    const idInput = block.querySelector('.dataset-case-id-input');
    const status = block.querySelector('.dataset-case-status');
    const datasetId = state.selected.id;

    if (action === 'run') {
        await triggerRun(idInput.value || caseId);
        return;
    }

    if (action === 'save') {
        await saveCaseInput(datasetId, caseId, idInput.value, textarea.value, status);
        return;
    }

    if (action === 'delete') {
        await deleteCase(datasetId, caseId, status);
    }
}

function onDatasetCaseInput(ev) {
    const target = ev.target;
    if (!target.classList.contains('dataset-case-content') && !target.classList.contains('dataset-case-id-input')) {
        return;
    }
    const block = ev.currentTarget;
    const datasetId = state.selected.id;
    const caseId = block.dataset.caseId;
    const textarea = block.querySelector('.dataset-case-content');
    const idInput = block.querySelector('.dataset-case-id-input');
    const detail = state.datasetDetails.get(datasetId);
    const original = detail?.cases?.find(item => item.id === caseId) || {};
    const isDraft = String(caseId || '').startsWith('__new__');
    const draft = {
        id: idInput.value,
        message: textarea.value,
    };
    const changed = isDraft
        ? Boolean(draft.id.trim() || draft.message)
        : draft.message !== String(original.message || '');
    const key = caseResultKey(datasetId, caseId);
    if (changed) {
        state.caseDrafts.set(key, draft);
        state.dirtyCases.add(key);
    } else {
        state.caseDrafts.delete(key);
        state.dirtyCases.delete(key);
    }
    block.classList.toggle('dirty', changed);
    const saveBtn = block.querySelector('button[data-action="save"]');
    const runBtn = block.querySelector('button[data-action="run"]');
    const status = block.querySelector('.dataset-case-status');
    if (saveBtn) saveBtn.disabled = !changed;
    if (runBtn) runBtn.disabled = !!state.liveRun || changed;
    if (status) {
        const runState = state.caseRunResults.get(key);
        status.className = 'dataset-case-status ' + (changed ? 'dirty' : (runState?.status || ''));
        status.textContent = changed ? '未保存' : (runState ? formatCaseRunStatus(runState) : '');
    }
}

async function saveCaseInput(datasetId, caseId, newCaseId, message, statusEl) {
    const draft = state.caseDrafts.get(caseResultKey(datasetId, caseId));
    if (draft) {
        newCaseId = draft.id;
        message = draft.message;
    }
    if (!newCaseId.trim()) {
        statusEl.textContent = 'case id 不能为空';
        return;
    }
    statusEl.textContent = '保存中…';
    try {
        const isNew = caseId.startsWith('__new__');
        const path = isNew
            ? `/api/eval/datasets/${encodeURIComponent(datasetId)}/cases`
            : `/api/eval/datasets/${encodeURIComponent(datasetId)}/cases/${encodeURIComponent(caseId)}`;
        await api(path, {
            method: isNew ? 'POST' : 'PUT',
            body: JSON.stringify({ id: newCaseId, message }),
        });
        clearCaseDraft(datasetId, caseId);
        statusEl.textContent = '已保存';
        await loadDatasets();
        await loadDatasetDetail(datasetId, { force: true });
    } catch (e) {
        statusEl.textContent = '保存失败: ' + e.message;
    }
}

async function deleteCase(datasetId, caseId, statusEl) {
    if (caseId.startsWith('__new__')) {
        clearCaseDraft(datasetId, caseId);
        const detail = state.datasetDetails.get(datasetId);
        if (detail) {
            detail.cases = detail.cases.filter(item => item.id !== caseId);
            renderDatasetCases(detail);
        }
        return;
    }
    if (!confirm(`删除 case ${caseId}?`)) return;
    statusEl.textContent = '删除中…';
    try {
        const resp = await api(`/api/eval/datasets/${encodeURIComponent(datasetId)}/cases/${encodeURIComponent(caseId)}`, {
            method: 'DELETE',
        });
        clearCaseDraft(datasetId, caseId);
        state.deletedCase = { datasetId, caseId, undoToken: resp.undo_token };
        await loadDatasets();
        await loadDatasetDetail(datasetId, { force: true });
        showDatasetAlert(`已删除 ${caseId}`, {
            actionText: '撤销',
            onAction: () => undoDeleteCase().catch(console.error),
        });
    } catch (e) {
        statusEl.textContent = '删除失败: ' + e.message;
    }
}

async function undoDeleteCase() {
    const deleted = state.deletedCase;
    if (!deleted) return;
    await api(`/api/eval/datasets/${encodeURIComponent(deleted.datasetId)}/cases/undo-delete`, {
        method: 'POST',
        body: JSON.stringify({ undo_token: deleted.undoToken }),
    });
    state.deletedCase = null;
    hideDatasetAlert();
    await loadDatasets();
    await loadDatasetDetail(deleted.datasetId, { force: true });
}

function addCaseDraft() {
    if (state.selected.kind !== 'dataset') return;
    const datasetId = state.selected.id;
    const detail = state.datasetDetails.get(datasetId);
    if (!detail || detail.editable !== true) return;
    const stamp = Date.now();
    const caseId = `__new__${stamp}`;
    const draftId = `new-case-${stamp}`;
    detail.cases.unshift({
        id: caseId,
        draft_id: draftId,
        index: -1,
        message: '',
    });
    const key = caseResultKey(datasetId, caseId);
    state.caseDrafts.set(key, {
        id: draftId,
        message: '',
    });
    state.dirtyCases.add(key);
    state.caseQuery = '';
    els.caseSearch.value = '';
    renderDatasetCases(detail);
}

async function compileSelectedDataset() {
    if (state.selected.kind !== 'dataset') return;
    if (hasUnsavedCases()) {
        showDatasetAlert('有未保存的 case，请先保存后再编译。', { tone: 'error' });
        return;
    }
    const datasetId = state.selected.id;
    els.btnCompile.disabled = true;
    const oldText = els.btnCompile.textContent;
    els.btnCompile.textContent = '编译中…';
    try {
        const resp = await api(`/api/eval/datasets/${encodeURIComponent(datasetId)}/compile`, {
            method: 'POST',
        });
        showDatasetAlert(`编译通过：${resp.case_count} case`);
        els.btnCompile.textContent = `通过 ${resp.case_count} case`;
        await loadDatasets();
        await loadDatasetDetail(datasetId, { force: true });
        setTimeout(() => { els.btnCompile.textContent = oldText; }, 1600);
    } catch (e) {
        els.btnCompile.textContent = '编译失败';
        showDatasetAlert('编译失败：' + e.message, { tone: 'error' });
        setTimeout(() => { els.btnCompile.textContent = oldText; }, 2200);
    } finally {
        els.btnCompile.disabled = false;
    }
}

function showDatasetAlert(message, options = {}) {
    els.datasetAlert.hidden = false;
    els.datasetAlert.className = 'dataset-alert ' + (options.tone === 'error' ? 'error' : '');
    els.datasetAlert.textContent = message;
    if (options.actionText && typeof options.onAction === 'function') {
        const btn = document.createElement('button');
        btn.className = 'mini-action';
        btn.type = 'button';
        btn.textContent = options.actionText;
        btn.addEventListener('click', options.onAction);
        els.datasetAlert.appendChild(btn);
    }
}

function hideDatasetAlert() {
    els.datasetAlert.hidden = true;
    els.datasetAlert.className = 'dataset-alert';
    els.datasetAlert.textContent = '';
}

function getCaseDraft(datasetId, caseId, item = {}) {
    return state.caseDrafts.get(caseResultKey(datasetId, caseId)) || {
        id: item.draft_id || item.id || '',
        message: item.message || '',
    };
}

function isCaseDirty(datasetId, caseId) {
    return state.dirtyCases.has(caseResultKey(datasetId, caseId));
}

function hasUnsavedCases() {
    return state.dirtyCases.size > 0;
}

function clearCaseDraft(datasetId, caseId) {
    const key = caseResultKey(datasetId, caseId);
    state.caseDrafts.delete(key);
    state.dirtyCases.delete(key);
}

function clearDatasetDrafts(datasetId) {
    const prefix = `${datasetId}::`;
    for (const key of Array.from(state.caseDrafts.keys())) {
        if (key.startsWith(prefix)) state.caseDrafts.delete(key);
    }
    for (const key of Array.from(state.dirtyCases)) {
        if (key.startsWith(prefix)) state.dirtyCases.delete(key);
    }
}

function confirmDiscardUnsaved() {
    if (!hasUnsavedCases()) return true;
    const ok = confirm('有未保存的 case 修改，离开会丢弃这些修改。继续？');
    if (!ok) return false;
    for (const key of Array.from(state.dirtyCases)) {
        const [datasetId, caseId] = key.split('::');
        const detail = state.datasetDetails.get(datasetId);
        if (detail && caseId?.startsWith('__new__')) {
            detail.cases = detail.cases.filter(item => item.id !== caseId);
        }
    }
    state.caseDrafts.clear();
    state.dirtyCases.clear();
    return true;
}

function resolveRunCaseIds(caseId, detail) {
    const cases = detail?.cases || [];
    if (caseId) return [caseId];
    const scope = els.runScope.value || 'all';
    if (scope === 'all') return [];
    if (scope === 'visible') {
        const ids = visibleDatasetCases(detail)
            .map(item => item.id)
            .filter(id => id && !String(id).startsWith('__new__'));
        if (!ids.length) {
            showDatasetAlert('当前搜索结果里没有可运行的 case。', { tone: 'error' });
            return false;
        }
        if (ids.length === cases.length) return [];
        return ids;
    }
    if (scope === 'failed') {
        const fromCurrentSession = cases
            .map(item => item.id)
            .filter(id => state.caseRunResults.get(caseResultKey(detail.dataset.id, id))?.status === 'failed');
        const ds = state.datasets.find(item => item.id === detail.dataset.id);
        const fromLatestRun = Array.isArray(ds?.last_run?.failed_case_ids) ? ds.last_run.failed_case_ids : [];
        const ids = Array.from(new Set([...fromCurrentSession, ...fromLatestRun]))
            .filter(id => cases.some(item => item.id === id));
        if (!ids.length) {
            showDatasetAlert('当前数据集没有最近失败的 case。先运行一次或打开失败结果后再试。', { tone: 'error' });
            return false;
        }
        return ids;
    }
    return [];
}

function updateDatasetLastRunFromLive(live) {
    const ds = state.datasets.find(item => item.id === live.dataset_id);
    if (!ds) return;
    ds.last_run = {
        run_id: live.run_id,
        dataset_id: live.dataset_id,
        started_at: live.started_at,
        ended_at: new Date().toISOString(),
        total_cases: live.total_cases,
        passed: live.passed,
        failed: live.failed,
        skipped: live.skipped,
        status: live.status,
        failed_case_ids: live.results
            .filter(item => caseStateClass(item) === 'failed' && item.case_id)
            .map(item => item.case_id),
    };
    renderDatasetList();
}

function recordRunCaseResults(datasetId, runId, results) {
    for (const item of results || []) {
        if (!item.case_id) continue;
        const status = caseStateClass(item);
        if (!['success', 'failed', 'skipped'].includes(status)) continue;
        state.caseRunResults.set(caseResultKey(datasetId, item.case_id), {
            status,
            error: item.error || item.skipped_reason,
            duration_ms: item.duration_ms,
            run_id: runId,
        });
    }
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
    els.modalFailReasonsSection.hidden = true;
    els.modalFailReasons.innerHTML = '';
    els.modalEventsSection.hidden = true;
    els.modalEvents.innerHTML = '';
    els.modalEventsCount.textContent = '';
    els.modalUnresolvedSection.hidden = true;
    els.modalUnresolved.textContent = '';
    els.modalUnresolvedCount.textContent = '';

    // 暂存当前 case 信息供"再跑"按钮使用
    state.modalCase = { runId, caseId, fallback };
    if (els.btnModalRerun) {
        // 默认 disabled，下面拿到 dataset_id 后再决定
        els.btnModalRerun.disabled = true;
    }

    if (!runId || !caseId) {
        els.modalStatus.textContent = '无数据';
        return;
    }

    let request = null, response = null, events = [], unresolved = [], result = null;
    try {
        const data = await api(`/api/eval/runs/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}`);
        request = data.request;
        response = data.response;
        events = data.events || [];
        unresolved = data.unresolved_failures || [];
        result = data.result || null;
    } catch (e) {
        // 落盘前(刚跑完)case 目录可能还没有,只展示 fallback
    }

    const userPrompt = pickUserPrompt(request, fallback);
    const responseText = response?.response_text ?? response?.content ?? fallback?.response_text ?? fallback?.error ?? '(无响应)';
    const toolCalls = response?.tool_calls ?? fallback?.tool_calls ?? [];
    const duration = fallback?.duration_ms ?? response?.duration_ms ?? result?.duration_ms;
    const modalResult = {
        case_id: caseId,
        status: 'done',
        success: fallback?.success ?? response?.success ?? result?.success,
        verdict: fallback?.verdict ?? response?.verdict ?? result?.verdict,
        skipped_reason: fallback?.skipped_reason ?? response?.skipped_reason ?? result?.skipped_reason,
        error: fallback?.error ?? response?.error ?? result?.error,
    };
    const modalState = caseStateClass(modalResult);

    els.modalTitle.textContent = (fallback?.title || result?.title || caseId);
    els.modalStatus.textContent = modalState === 'success' ? '✓ 成功'
        : modalState === 'failed' ? '✗ 失败'
        : modalState === 'skipped' ? '⊘ 跳过'
        : '—';
    els.modalStatus.className = 'modal-status ' + (['success', 'failed', 'skipped'].includes(modalState) ? modalState : '');
    els.modalDuration.textContent = duration != null ? `${duration} ms` : '—';
    els.modalToolCount.textContent = String(toolCalls.length);
    if (modalResult.error || modalResult.skipped_reason) {
        els.modalErrorRow.hidden = false;
        els.modalError.textContent = String(modalResult.error || modalResult.skipped_reason);
    }
    els.modalPrompt.textContent = userPrompt || '(无用户输入)';
    els.modalResponse.textContent = responseText || '(无响应)';
    els.modalTools.textContent = toolCalls.length
        ? JSON.stringify(toolCalls, null, 2)
        : '(无工具调用)';
    els.modalToolsSection.hidden = !toolCalls.length;

    // ── 失败原因 ──
    const failReasons = (response?.fail_reasons ?? result?.fail_reasons ?? []).filter(Boolean);
    if (failReasons.length) {
        els.modalFailReasonsSection.hidden = false;
        els.modalFailReasons.innerHTML = '';
        for (const reason of failReasons) {
            const li = document.createElement('li');
            li.textContent = String(reason);
            els.modalFailReasons.appendChild(li);
        }
    }

    // ── L1 事件流 ──
    if (events.length) {
        els.modalEventsSection.hidden = false;
        els.modalEventsCount.textContent = `(${events.length})`;
        els.modalEvents.innerHTML = '';
        for (const ev of events) {
            const li = document.createElement('li');
            li.className = 'modal-event-item';
            const type = String(ev.type || '?');
            const vis = ev.visibility ? ` [${ev.visibility}]` : '';
            const seq = ev.seq != null ? `#${ev.seq} ` : '';
            const ts = ev.ts || ev.timestamp || '';
            const payloadStr = ev.payload ? compactPayload(ev.payload) : '';
            li.innerHTML = `
                <div class="modal-event-head">
                    <span class="modal-event-seq">${escapeHtml(seq)}</span>
                    <span class="modal-event-type">${escapeHtml(type)}</span>
                    <span class="modal-event-meta">${escapeHtml(vis)} ${escapeHtml(ts)}</span>
                </div>
                ${payloadStr ? `<pre class="modal-event-payload">${escapeHtml(payloadStr)}</pre>` : ''}
            `;
            els.modalEvents.appendChild(li);
        }
    }

    // ── 未恢复失败 ──
    if (unresolved.length) {
        els.modalUnresolvedSection.hidden = false;
        els.modalUnresolvedCount.textContent = `(${unresolved.length})`;
        els.modalUnresolved.textContent = unresolved
            .map((u) => JSON.stringify(u, null, 2))
            .join('\n\n');
    }

    // ── 再跑按钮：必须能定位到 dataset。从 fallback / result / state 推 ──
    if (els.btnModalRerun) {
        const datasetId = fallback?.dataset_id
            || result?.dataset_id
            || state.runs.find((r) => r.run_id === runId)?.dataset_id
            || (state.liveRun?.run_id === runId ? state.liveRun.dataset_id : null);
        if (datasetId && !state.liveRun) {
            state.modalCase.datasetId = datasetId;
            els.btnModalRerun.disabled = false;
            els.btnModalRerun.title = '再跑这个 case';
        } else {
            els.btnModalRerun.disabled = true;
            els.btnModalRerun.title = state.liveRun ? '有 run 在进行中，无法再跑' : '无法定位数据集';
        }
    }
}

function compactPayload(payload) {
    // 把 payload 转成单行紧凑 JSON；过长（>400 字符）截断显示。
    try {
        const s = JSON.stringify(payload, null, 2);
        return s.length > 800 ? s.slice(0, 800) + `\n…<+${s.length - 800} chars>` : s;
    } catch (_) {
        return String(payload);
    }
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
function filterDatasets(items) {
    const q = (state.datasetQuery || '').trim().toLowerCase();
    if (!q) return items;
    return items.filter(ds => [
        ds.id,
        ds.suite_id,
        ds.description,
        ds.session_title,
        ds.source_type,
        ds.path,
    ].some(value => String(value || '').toLowerCase().includes(q)));
}
function filterRuns(items) {
    const q = (state.runQuery || '').trim().toLowerCase();
    if (!q) return items;
    return items.filter(run => [
        run.run_id,
        run.dataset_id,
        run.status,
        run.session_id,
        formatTs(run.started_at),
    ].some(value => String(value || '').toLowerCase().includes(q)));
}
function filterRunResults(items) {
    const withIndex = items.map((item, index) => ({ ...item, __index: index }));
    switch (state.runResultFilter) {
        case 'failed':
            return withIndex.filter(item => caseStateClass(item) === 'failed');
        case 'success':
            return withIndex.filter(item => caseStateClass(item) === 'success');
        case 'skipped':
            return withIndex.filter(item => caseStateClass(item) === 'skipped');
        case 'running':
            return withIndex.filter(item => item.status === 'running' || item.status === 'pending');
        default:
            return withIndex;
    }
}
function caseResultKey(datasetId, caseId) {
    return `${datasetId}::${caseId}`;
}
function formatCaseRunStatus(result) {
    if (result.status === 'running') return '运行中…';
    if (result.status === 'success') {
        const dur = result.duration_ms != null ? ` · ${(result.duration_ms / 1000).toFixed(1)}s` : '';
        return `最近运行通过${dur}`;
    }
    if (result.status === 'failed') {
        const err = result.error ? ` · ${result.error}` : '';
        return `最近运行失败${err}`;
    }
    if (result.status === 'skipped') {
        const reason = result.error ? ` · ${result.error}` : '';
        return `最近运行跳过${reason}`;
    }
    return '';
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

els.btnRun.addEventListener('click', () => triggerRun());
els.btnRefreshRuns.addEventListener('click', () => loadRuns().catch(console.error));
els.datasetSearch.addEventListener('input', () => {
    state.datasetQuery = els.datasetSearch.value || '';
    renderDatasetList();
});
els.runSearch.addEventListener('input', () => {
    state.runQuery = els.runSearch.value || '';
    renderRunList();
});
els.btnAddCase.addEventListener('click', addCaseDraft);
els.btnCompile.addEventListener('click', () => compileSelectedDataset().catch(console.error));
if (els.btnModalRerun) {
    els.btnModalRerun.addEventListener('click', async () => {
        if (els.btnModalRerun.disabled) return;
        const mc = state.modalCase || {};
        if (!mc.datasetId || !mc.caseId) return;
        // 切到该数据集后用单 case 模式跑（triggerRun 会读 state.selected）
        try {
            if (state.selected.kind !== 'dataset' || state.selected.id !== mc.datasetId) {
                await selectDataset(mc.datasetId);
            }
        } catch (_) { /* 选数据集失败也允许直接 trigger */ }
        closeModal();
        triggerRun(mc.caseId);
    });
}
els.caseSearch.addEventListener('input', () => {
    state.caseQuery = els.caseSearch.value || '';
    if (state.selected.kind !== 'dataset') return;
    const detail = state.datasetDetails.get(state.selected.id);
    if (detail) renderDatasetCases(detail);
});
els.runFilter.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-filter]');
    if (!btn) return;
    state.runResultFilter = btn.dataset.filter || 'all';
    if (state.currentRunView) {
        renderRunPanel(state.currentRunView.run, state.currentRunView.results);
    } else {
        renderRunFilter();
    }
});
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

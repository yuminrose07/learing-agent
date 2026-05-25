/**
 * Learning-Agent · 学习卷前端 UI 模块
 *
 * 把 adaptive alignment §7 的视觉契约从 app.js 隔离出来：
 * - 顶部目标卡（phase chip + working_objective + objective_status 徽标 + assumption_note）
 * - 非阻塞建议条（alignment_state="suggested" 时显示，提供"先按这个学" / "帮我收窄"两个按钮）
 * - absorbing 阶段右下角 TEACH 入口（"讲讲看"）
 * - consolidated 阶段反馈条（复用意愿赞 / 否）
 *
 * 不直接读 app.js 的内部状态。订阅两个 CustomEvent：
 *   - `learning-unit:session-loaded`  detail = { session, sessionId }
 *   - `learning-unit:metadata`        detail = SSE delta payload（已包含 learning_unit_* 字段）
 *
 * 也响应 `learning-unit:reset`（切换到非学习卷会话时清空 UI）。
 *
 * 后端依赖：
 *   - GET    /learning-units/{id}
 *   - POST   /learning-units            { seed_text, source }
 *   - POST   /learning-units/{id}/accept-assumption
 *   - POST   /learning-units/{id}/align
 *   - POST   /learning-units/{id}/advance              { target_phase }
 *   - POST   /learning-units/{id}/reuse-feedback       { value: "yes" | "no" }
 *
 * 没有 UI 自动化测试环境，本模块保持极简：单文件、零依赖、纯 DOM。
 */
(function () {
    'use strict';

    const API_BASE = typeof window !== 'undefined'
        && window.location
        && typeof window.location.origin === 'string'
        && window.location.origin.startsWith('http')
        ? window.location.origin
        : '';

    const PHASE_LABEL = {
        absorbing: '学习中',
        outputting: '复述检验',
        consolidated: '已收束',
    };

    const OBJECTIVE_STATUS_LABEL = {
        working: '工作目标',
        refined: '已收窄',
        confirmed: '已确认',
    };

    /** Frontend representation of the active learning unit, kept in sync with SSE deltas. */
    const state = {
        sessionId: null,
        unitId: null,
        phase: null,
        alignmentState: null,
        objectiveStatus: null,
        objectiveText: '',
        assumptionNote: '',
        alignmentReason: '',
        reuseRecorded: false,
        // Avoid duplicate in-flight POSTs when users mash buttons.
        pendingAction: null,
    };

    let cardEl = null;

    function api(method, path, body) {
        const opts = {
            method,
            headers: { 'Content-Type': 'application/json' },
        };
        if (body !== undefined) opts.body = JSON.stringify(body);
        return fetch(API_BASE + path, opts).then(async (res) => {
            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            return res.json();
        });
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

    function ensureCard() {
        if (cardEl) return cardEl;
        const host = document.getElementById('learning-unit-card');
        if (!host) return null;
        cardEl = host;
        cardEl.addEventListener('click', onCardClick);
        return cardEl;
    }

    function reset() {
        state.sessionId = null;
        state.unitId = null;
        state.phase = null;
        state.alignmentState = null;
        state.objectiveStatus = null;
        state.objectiveText = '';
        state.assumptionNote = '';
        state.alignmentReason = '';
        state.reuseRecorded = false;
        state.pendingAction = null;
        render();
    }

    function hydrateFromUnit(unit, sessionId) {
        if (!unit) {
            reset();
            return;
        }
        state.sessionId = sessionId || state.sessionId;
        state.unitId = unit.id;
        state.phase = unit.phase || 'absorbing';
        state.alignmentState = unit.alignment_state || 'idle';
        state.objectiveStatus = unit.objective_status || 'working';
        state.objectiveText = (unit.objective && unit.objective.text) || unit.working_objective || '';
        state.assumptionNote = unit.assumption_note || '';
        state.alignmentReason = unit.alignment_reason || '';
        state.reuseRecorded = false;
        render();
    }

    function applyDelta(delta) {
        if (!delta || typeof delta !== 'object') return;
        if (!delta.learning_unit_id) return;
        let changed = false;
        if (state.unitId !== delta.learning_unit_id) {
            state.unitId = delta.learning_unit_id;
            // New unit on this stream — drop stale text fields until /units/{id} fetch returns.
            state.objectiveText = '';
            state.assumptionNote = '';
            state.alignmentReason = '';
            changed = true;
        }
        const fields = {
            phase: delta.learning_unit_phase,
            alignmentState: delta.alignment_state,
            objectiveStatus: delta.objective_status,
            alignmentReason: delta.alignment_reason,
            assumptionNote: delta.assumption_note,
        };
        for (const [k, v] of Object.entries(fields)) {
            if (v !== undefined && v !== null && state[k] !== v) {
                state[k] = v;
                changed = true;
            }
        }
        if (changed) {
            render();
            // Hydrate the rest from the canonical unit doc when phase or id changed.
            refreshUnit().catch(() => {});
        }
    }

    async function refreshUnit() {
        if (!state.unitId) return;
        try {
            const unit = await api('GET', `/learning-units/${encodeURIComponent(state.unitId)}`);
            // Preserve sessionId set by caller; the unit doc carries no session pointer
            // unless we set one server-side.
            const sid = state.sessionId;
            hydrateFromUnit(unit, sid);
        } catch (err) {
            console.warn('刷新学习卷失败:', err);
        }
    }

    function render() {
        const card = ensureCard();
        if (!card) return;
        if (!state.unitId) {
            card.classList.add('hidden');
            card.innerHTML = '';
            return;
        }
        card.classList.remove('hidden');
        const phase = state.phase || 'absorbing';
        const status = state.objectiveStatus || 'working';
        const phaseLabel = PHASE_LABEL[phase] || phase;
        const statusLabel = OBJECTIVE_STATUS_LABEL[status] || status;

        const showSuggestion = state.alignmentState === 'suggested' && phase === 'absorbing';
        const showTeachEntry = phase === 'absorbing';
        const showReuse = phase === 'consolidated' && !state.reuseRecorded;
        const showReuseDone = phase === 'consolidated' && state.reuseRecorded;

        card.innerHTML = `
            <div class="lu-card-head">
                <div class="lu-phase-row" aria-label="学习卷阶段">
                    ${renderPhasePill('absorbing', phase)}
                    <span class="lu-phase-sep">›</span>
                    ${renderPhasePill('outputting', phase)}
                    <span class="lu-phase-sep">›</span>
                    ${renderPhasePill('consolidated', phase)}
                </div>
                <span class="lu-status-badge lu-status-${escapeHtml(status)}">${escapeHtml(statusLabel)}</span>
            </div>
            <div class="lu-objective">
                <span class="lu-objective-kicker">学习卷</span>
                <p class="lu-objective-text">${state.objectiveText ? escapeHtml(state.objectiveText) : '<span class="lu-empty">尚未生成工作目标</span>'}</p>
            </div>
            ${state.assumptionNote ? `<div class="lu-assumption">${escapeHtml(state.assumptionNote)}</div>` : ''}
            ${showSuggestion ? renderSuggestionBar() : ''}
            <div class="lu-actions">
                ${showTeachEntry ? `
                    <button class="lu-btn lu-btn-primary" data-action="advance-outputting" ${state.pendingAction ? 'disabled' : ''}>讲讲看</button>
                    <button class="lu-btn lu-btn-secondary" data-action="advance-outputting-3" ${state.pendingAction ? 'disabled' : ''}>做 3 题</button>
                ` : ''}
                ${showReuse ? `
                    <span class="lu-reuse-prompt">下次还会用学习模式吗？</span>
                    <button class="lu-btn lu-btn-primary" data-action="reuse-yes" ${state.pendingAction ? 'disabled' : ''}>会用</button>
                    <button class="lu-btn lu-btn-secondary" data-action="reuse-no" ${state.pendingAction ? 'disabled' : ''}>不会</button>
                ` : ''}
                ${showReuseDone ? `<span class="lu-reuse-done">谢谢反馈</span>` : ''}
            </div>
        `;
    }

    function renderPhasePill(target, current) {
        const order = ['absorbing', 'outputting', 'consolidated'];
        const targetIdx = order.indexOf(target);
        const currentIdx = order.indexOf(current);
        let cls = 'lu-phase';
        if (target === current) cls += ' is-current';
        else if (targetIdx < currentIdx) cls += ' is-past';
        return `<span class="${cls}">${escapeHtml(PHASE_LABEL[target] || target)}</span>`;
    }

    function renderSuggestionBar() {
        // adaptive alignment §7.2: "这卷我先按 X 展开；如果你想更聚焦，我可以再收窄。"
        return `
            <div class="lu-suggestion-bar" role="note">
                <span class="lu-suggestion-text">这卷我先按上面这个理解展开；如果你想更聚焦，我可以再收窄。</span>
                <div class="lu-suggestion-actions">
                    <button class="lu-btn lu-btn-primary" data-action="accept-assumption" ${state.pendingAction ? 'disabled' : ''}>先按这个学</button>
                    <button class="lu-btn lu-btn-secondary" data-action="align" ${state.pendingAction ? 'disabled' : ''}>帮我收窄</button>
                </div>
            </div>
        `;
    }

    function onCardClick(ev) {
        const btn = ev.target.closest('[data-action]');
        if (!btn || btn.disabled) return;
        const action = btn.getAttribute('data-action');
        handleAction(action).catch((err) => {
            console.warn('学习卷动作失败:', err);
            alert('操作失败: ' + err.message);
        }).finally(() => {
            state.pendingAction = null;
            render();
        });
        state.pendingAction = action;
        render();
    }

    async function handleAction(action) {
        const uid = state.unitId;
        if (!uid) return;
        switch (action) {
            case 'accept-assumption': {
                const unit = await api('POST', `/learning-units/${encodeURIComponent(uid)}/accept-assumption`);
                hydrateFromUnit(unit, state.sessionId);
                return;
            }
            case 'align': {
                const unit = await api('POST', `/learning-units/${encodeURIComponent(uid)}/align`);
                hydrateFromUnit(unit, state.sessionId);
                return;
            }
            case 'advance-outputting':
            case 'advance-outputting-3': {
                const unit = await api(
                    'POST',
                    `/learning-units/${encodeURIComponent(uid)}/advance`,
                    { target_phase: 'outputting' },
                );
                hydrateFromUnit(unit, state.sessionId);
                return;
            }
            case 'reuse-yes':
            case 'reuse-no': {
                const value = action === 'reuse-yes' ? 'yes' : 'no';
                await api('POST', `/learning-units/${encodeURIComponent(uid)}/reuse-feedback`, { value });
                state.reuseRecorded = true;
                return;
            }
            default:
                return;
        }
    }

    // ─── Event hooks (called from app.js) ───

    document.addEventListener('learning-unit:reset', () => reset());

    document.addEventListener('learning-unit:session-loaded', async (ev) => {
        const detail = ev.detail || {};
        const session = detail.session;
        const sessionId = detail.sessionId || (session && session.id);
        if (!sessionId) {
            reset();
            return;
        }
        state.sessionId = sessionId;
        const unitId = session && session.learning_unit_id;
        if (!unitId) {
            reset();
            return;
        }
        state.unitId = unitId;
        try {
            const unit = await api('GET', `/learning-units/${encodeURIComponent(unitId)}`);
            hydrateFromUnit(unit, sessionId);
        } catch (err) {
            console.warn('加载学习卷失败:', err);
        }
    });

    document.addEventListener('learning-unit:metadata', (ev) => {
        applyDelta(ev.detail || {});
    });

    /**
     * Promotes a fresh chat by creating a learning unit instead. Called by app.js
     * when the user clicks the "学习模式" welcome card and sends their first message.
     * Resolves to the created session id (or null on failure).
     */
    window.__createLearningUnit = async function createLearningUnit(seedText) {
        try {
            const res = await api('POST', '/learning-units', {
                seed_text: seedText || '',
                source: 'web_welcome',
            });
            state.sessionId = res.session_id || null;
            return res;
        } catch (err) {
            console.warn('创建学习卷失败:', err);
            alert('开新学习卷失败: ' + err.message);
            return null;
        }
    };

    // Allow other modules / tests to inspect.
    window.__learningUnitUI = { state, reset, refreshUnit };
})();

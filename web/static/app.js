/**
 * Learning-Agent Web 前端 v2
 * 参考 ChatGPT / Gemini 设计
 */

const API_BASE = typeof window !== 'undefined'
    && window.location
    && typeof window.location.origin === 'string'
    && window.location.origin.startsWith('http')
    ? window.location.origin
    : '';

let currentSessionId = null;
let currentSessionTitle = null;
let isStreaming = false;
let deleteTargetId = null;
let currentMode = 'chat';
let currentView = 'home';
let currentPersonaKey = null;
let currentPersonaName = '';
let currentLearningPhase = null;
let currentCompanion = {
    enabled: false,
    style: 'off',
    adviceLevel: 'low',
};

// ─── DOM 元素 ───
const els = {
    sidebar: document.getElementById('sidebar'),
    sidebarOverlay: document.getElementById('sidebar-overlay'),
    sidebarScroll: document.getElementById('sidebar-scroll'),
    messages: document.getElementById('messages'),
    chatArea: document.getElementById('chat-area'),
    welcomeScreen: document.getElementById('welcome-screen'),
    messageInput: document.getElementById('message-input'),
    btnSend: document.getElementById('btn-send'),
    btnNewChat: document.getElementById('btn-new-chat'),
    btnMenu: document.getElementById('btn-menu'),
    btnMemory: document.getElementById('btn-memory'),
    btnSave: document.getElementById('btn-save'),
    btnModeChat: document.getElementById('btn-mode-chat'),
    btnModeLearning: document.getElementById('btn-mode-learning'),
    topbar: document.querySelector('.topbar'),
    topbarTitle: document.getElementById('topbar-title'),
    topbarSubtitle: document.getElementById('topbar-subtitle'),
    topbarCompanion: document.getElementById('topbar-companion'),
    companionTrigger: document.getElementById('companion-trigger'),
    companionTriggerValue: document.getElementById('companion-trigger-value'),
    companionMenu: document.getElementById('companion-menu'),
    topbarThinking: document.getElementById('topbar-thinking'),
    thinkingTrigger: document.getElementById('thinking-trigger'),
    thinkingTriggerValue: document.getElementById('thinking-trigger-value'),
    thinkingMenu: document.getElementById('thinking-menu'),
    apiStatus: document.getElementById('api-status'),
    typingIndicator: document.getElementById('typing-indicator'),
    memoryModal: document.getElementById('memory-modal'),
    memoryContent: document.getElementById('memory-content'),
    deleteModal: document.getElementById('delete-modal'),
    learningStopModal: document.getElementById('learning-stop-modal'),
    learningStopModalTitle: document.getElementById('learning-stop-modal-title'),
    learningStopModalText: document.getElementById('learning-stop-modal-text'),
    learningStopModalObjective: document.getElementById('learning-stop-modal-objective'),
    btnStayLearningStop: document.getElementById('btn-stay-learning-stop'),
    btnNewTopicAfterStop: document.getElementById('btn-new-topic-after-stop'),
    learningResumeModal: document.getElementById('learning-resume-modal'),
    learningResumeModalObjective: document.getElementById('learning-resume-modal-objective'),
    btnContinueActive: document.getElementById('btn-continue-active'),
    btnStopAndNew: document.getElementById('btn-stop-and-new'),
    welcomeActiveUnits: document.getElementById('welcome-active-units'),
    btnCancelDelete: document.getElementById('btn-cancel-delete'),
    btnConfirmDelete: document.getElementById('btn-confirm-delete'),
};

// Neutral default + curated philosopher overlays.
// `mark` is the single ideograph used as an inline avatar — no external image fetch.
const PERSONA_META = {
    neutral:    { name: '默认',     mark: '學', short: '不附加思想风格，平实地回应。' },
    socrates:   { name: '苏格拉底', mark: '蘇', short: '诘问者：用递进的提问逼近真意。' },
    feynman:    { name: '费曼',     mark: '費', short: '拆解者：把复杂概念翻译成可触摸的类比。' },
    montaigne:  { name: '蒙田',     mark: '蒙', short: '漫谈者：散笔随谈，多侧面、不急于收束。' },
    zhu_xi:     { name: '朱熹',     mark: '朱', short: '格物者：循序渐进，由表及里逐层推演。' },
    descartes:  { name: '笛卡尔',   mark: '笛', short: '存疑者：拆出可疑前提，再清楚明白地重建。' },
};

const PHILOSOPHER_ORDER = ['socrates', 'feynman', 'montaigne', 'zhu_xi', 'descartes'];

// Populated from GET /personas at init. Falls back to PERSONA_META above.
let personaCatalog = null;

const COMPANION_META = {
    off: { name: '关闭', mark: '·', short: '保持普通闲谈，不附加陪伴语气。' },
    warm_girlfriend: { name: '温柔', mark: '☾', short: '先接住疲惫和压力，少建议。' },
    playful_girlfriend: { name: '活泼', mark: '☾', short: '轻快一点，帮你转移注意力。' },
    quiet_companion: { name: '安静', mark: '☾', short: '话少、稳定，适合只想有人陪着。' },
};

// Populated from GET /companion-styles at init. Falls back to COMPANION_META.
let companionCatalog = null;

const FRONTEND_MODES = new Set(['chat', 'learning']);

function normalizeFrontendMode(mode) {
    return FRONTEND_MODES.has(mode) ? mode : 'chat';
}

function modeFromSession(session) {
    return session?.learning_unit_id ? 'learning' : 'chat';
}

function backendModeForFrontendMode(_mode) {
    // "learning" is a product/UI mode. The backend chooses its internal turn type
    // from the learning_unit_id on the session, so chat is the only public mode
    // this two-button frontend needs to send.
    return 'chat';
}

const LEARNING_PHASE_LABELS = {
    absorbing: '研习中',
    outputting: '复述检验',
    consolidated: '已收束',
    stopped: '已停止',
};

// ─── 聊天气泡渲染器（弱化文档感，保留必要 Markdown） ───

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function getMarkdownItFactory() {
    if (typeof window !== 'undefined' && typeof window.markdownit === 'function') {
        return window.markdownit;
    }
    if (typeof globalThis !== 'undefined' && typeof globalThis.markdownit === 'function') {
        return globalThis.markdownit;
    }
    return null;
}

function createMarkdownRenderer() {
    const markdownItFactory = getMarkdownItFactory();
    if (!markdownItFactory) return null;

    const md = markdownItFactory({
        html: false,
        breaks: true,
        linkify: true,
        typographer: false,
    });

    const defaultLinkOpen =
        md.renderer.rules.link_open ||
        function renderLinkOpen(tokens, idx, options, env, self) {
            return self.renderToken(tokens, idx, options);
        };

    md.renderer.rules.link_open = function renderLinkOpen(tokens, idx, options, env, self) {
        const token = tokens[idx];
        token.attrSet('target', '_blank');
        token.attrSet('rel', 'noopener noreferrer');
        return defaultLinkOpen(tokens, idx, options, env, self);
    };

    return md;
}

function normalizeMarkdownSource(text) {
    return String(text || '')
        .replace(/\r\n?/g, '\n')
        .replace(/<br\s*\/?>/gi, '\n');
}

const markdownRenderer = createMarkdownRenderer();

function renderPlainText(text) {
    if (!text) return '';
    return text
        .trim()
        .split(/\n{2,}/)
        .map(block => `<p>${escapeHtml(block).replace(/\n/g, '<br>')}</p>`)
        .join('');
}

function renderChatMarkdown(text) {
    if (!text) return '';
    const source = normalizeMarkdownSource(text).trim();
    if (!source) return '';

    if (!markdownRenderer) {
        console.warn('[chat] markdown-it 未加载，回退为纯文本渲染');
        return renderPlainText(source);
    }

    try {
        return markdownRenderer.render(source);
    } catch (error) {
        console.warn('[chat] Markdown 渲染失败，回退为纯文本渲染', error);
        return renderPlainText(source);
    }
}

// ─── 工具函数 ───

function autoResizeTextarea() {
    els.messageInput.style.height = 'auto';
    els.messageInput.style.height = Math.min(els.messageInput.scrollHeight, 200) + 'px';
}

function scrollToBottom() {
    els.chatArea.scrollTop = els.chatArea.scrollHeight;
}

function formatDateLabel(date) {
    const d = new Date(date);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yest = new Date(today);
    yest.setDate(yest.getDate() - 1);
    const week = new Date(today);
    week.setDate(week.getDate() - 7);

    if (d >= today) return 'today';
    if (d >= yest) return 'yesterday';
    if (d >= week) return 'week';
    return 'earlier';
}

const GROUP_NAMES = {
    today: 'Today',
    yesterday: 'Yesterday',
    week: 'Previous 7 Days',
    earlier: 'Earlier',
};

function getPersonaMeta(personaKey) {
    if (!personaKey) return PERSONA_META.neutral;
    return PERSONA_META[personaKey] || PERSONA_META.neutral;
}

function updateViewTheme(view = currentView) {
    document.body.classList.remove('home-view', 'chat-view');
    if (view === 'chat') {
        document.body.classList.add('chat-view');
    } else if (view === 'home') {
        document.body.classList.add('home-view');
    }
}

function getPersonaMark(personaKey) {
    return getPersonaMeta(personaKey).mark || '學';
}

function getPersonaDisplayName(personaKey) {
    return getPersonaMeta(personaKey).name || '默认';
}

function getCompanionMeta(styleKey) {
    if (!styleKey) return COMPANION_META.off;
    return COMPANION_META[styleKey] || COMPANION_META.off;
}

function getCompanionDisplayName(styleKey, enabled = true) {
    if (!enabled || !styleKey || styleKey === 'off') return COMPANION_META.off.name;
    return getCompanionMeta(styleKey).name || styleKey;
}

function normalizeCompanionSettings(raw = {}) {
    const style = raw.style || raw.companion_style || 'off';
    const enabled = Boolean(raw.enabled) && style !== 'off';
    return {
        enabled,
        style: enabled ? style : 'off',
        adviceLevel: raw.advice_level || raw.adviceLevel || raw.companion_advice_level || 'low',
    };
}

function companionSettingsFromSession(session) {
    const meta = session?.mode_metadata || {};
    const enabled = meta.chat_profile === 'companion' && Boolean(meta.companion_style);
    return normalizeCompanionSettings({
        enabled,
        style: enabled ? meta.companion_style : 'off',
        advice_level: meta.companion_advice_level || 'low',
    });
}

function resolvePersonaKeyForMode(mode, session = null) {
    // 思路 (persona overlay) 现在只服务研习；闲聊已与研学分离，恒为 neutral。
    if (normalizeFrontendMode(mode) === 'chat') return 'neutral';
    const stored = session ? session.mode_metadata?.chat_persona_key : null;
    return stored || currentPersonaKey || 'neutral';
}

function getModeLabel(mode) {
    return {
        chat: '闲谈',
        learning: '研习',
    }[normalizeFrontendMode(mode)] || '闲谈';
}

function getSessionTypeTitle(mode) {
    return normalizeFrontendMode(mode) === 'learning'
        ? '研习会话'
        : '闲谈会话';
}

function parseUsageNumber(value) {
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string' && value.trim() !== '') {
        const parsed = Number(value);
        if (Number.isFinite(parsed)) return parsed;
    }
    return null;
}

function normalizeUsage(rawUsage) {
    if (!rawUsage || typeof rawUsage !== 'object') return null;
    const usage = {
        estimatedPromptTokens: parseUsageNumber(rawUsage.estimated_prompt_tokens),
        actualPromptTokens: parseUsageNumber(rawUsage.actual_prompt_tokens),
        actualCompletionTokens: parseUsageNumber(rawUsage.actual_completion_tokens),
        actualTotalTokens: parseUsageNumber(rawUsage.actual_total_tokens),
        contextLimit: parseUsageNumber(rawUsage.context_limit),
        utilizationRatio: parseUsageNumber(rawUsage.utilization_ratio),
        isEstimated: rawUsage.is_estimated !== false,
    };
    const hasDisplayableValue = [
        usage.estimatedPromptTokens,
        usage.actualPromptTokens,
        usage.actualCompletionTokens,
        usage.actualTotalTokens,
        usage.contextLimit,
        usage.utilizationRatio,
    ].some(value => value !== null);
    return hasDisplayableValue ? usage : null;
}

function formatUsageNumber(value) {
    if (value === null) return '--';
    return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value);
}

function formatUsagePercent(value) {
    if (value === null) return '--';
    const percentValue = value * 100;
    if (value > 0 && value < 0.01) {
        return `${percentValue.toFixed(2).replace(/\.?0+$/, '')}%`;
    }
    return `${percentValue.toFixed(1)}%`;
}

function ensureAssistantMessageBody(content) {
    let body = content.querySelector('.message-body');
    if (!body) {
        body = document.createElement('div');
        body.className = 'message-body';
        content.appendChild(body);
    }
    return body;
}

function upsertAssistantUsage(content, rawUsage) {
    let block = content.querySelector('.usage-block');
    const usage = normalizeUsage(rawUsage);
    if (!usage) {
        if (block) block.remove();
        return;
    }

    if (!block) {
        block = document.createElement('div');
        block.className = 'usage-block';
        content.appendChild(block);
    }

    const primaryTokens = usage.actualPromptTokens ?? usage.estimatedPromptTokens ?? usage.actualTotalTokens;
    const primaryLabel = usage.actualPromptTokens !== null || usage.estimatedPromptTokens !== null
        ? '上下文'
        : '总量';
    const summaryParts = [];
    if (usage.contextLimit !== null) {
        summaryParts.push(`上限 ${formatUsageNumber(usage.contextLimit)}`);
    }
    if (usage.utilizationRatio !== null) {
        summaryParts.push(`占比 ${formatUsagePercent(usage.utilizationRatio)}`);
    }
    const detailParts = [];
    if (usage.actualCompletionTokens !== null) {
        detailParts.push(`输出 ${formatUsageNumber(usage.actualCompletionTokens)}`);
    }
    if (usage.actualTotalTokens !== null) {
        detailParts.push(`总计 ${formatUsageNumber(usage.actualTotalTokens)}`);
    }

    block.innerHTML = `
        <div class="usage-line">
            <span class="usage-label">${escapeHtml(primaryLabel)}</span>
            <strong class="usage-value">${escapeHtml(formatUsageNumber(primaryTokens))}</strong>
            ${summaryParts.length ? `<span class="usage-meta">${escapeHtml(summaryParts.join(' · '))}</span>` : ''}
            <span class="usage-state ${usage.isEstimated ? 'estimated' : 'actual'}">${usage.isEstimated ? '估算' : '已对账'}</span>
        </div>
        ${detailParts.length ? `<div class="usage-detail">${escapeHtml(detailParts.join(' · '))}</div>` : ''}
    `;
}

function getHomeSubtitle(mode) {
    if (normalizeFrontendMode(mode) === 'learning') {
        return '研习：围绕一个主题开研习卷，先收束目标，再推进讲讲看与反馈。';
    }
    if (currentCompanion.enabled) {
        return `闲谈：${getCompanionDisplayName(currentCompanion.style)}陪伴已开启，适合休息和减压。`;
    }
    return '闲谈：直接输入即可开始轻量对话。';
}

// ─── API ───

async function api(method, path, body = null) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(API_BASE + path, opts);
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '未知错误' }));
        const error = new Error(err.detail || `HTTP ${res.status}`);
        error.status = res.status;
        error.payload = err;
        throw error;
    }
    return res.status === 204 ? null : res.json();
}

async function checkHealth() {
    try {
        const data = await api('GET', '/health');
        els.apiStatus.textContent = `API 在线 v${data.version}`;
        els.apiStatus.classList.remove('offline');
        els.apiStatus.classList.add('online');
        return true;
    } catch {
        els.apiStatus.textContent = 'API 离线';
        els.apiStatus.classList.remove('online');
        els.apiStatus.classList.add('offline');
        return false;
    }
}

async function loadSessions() {
    try {
        const sessions = await api('GET', '/sessions');
        renderSessionList(sessions);
    } catch (err) {
        console.error('加载会话失败:', err);
    }
}

async function createSession() {
    try {
        const session = await api('POST', '/sessions', { title: 'New chat' });
        await loadSessions();
        return session;
    } catch (err) {
        alert('创建会话失败: ' + err.message);
        return null;
    }
}

async function renameSession(sessionId, newTitle) {
    try {
        await api('PUT', `/sessions/${sessionId}`, { title: newTitle });
        await loadSessions();
        if (currentSessionId === sessionId) {
            currentSessionTitle = newTitle;
            els.topbarTitle.textContent = newTitle;
        }
    } catch (err) {
        alert('重命名失败: ' + err.message);
    }
}

async function deleteSession(sessionId) {
    try {
        await api('DELETE', `/sessions/${sessionId}`);
        await loadSessions();
        if (currentSessionId === sessionId) {
            currentSessionId = null;
            currentSessionTitle = null;
            localStorage.removeItem('lastSessionId');
            showWelcome();
        }
        // 删除后自动保存系统状态
        await saveState();
    } catch (err) {
        alert('删除失败: ' + err.message);
    }
}

function shortId(id) {
    if (!id) return '-';
    return id.length > 12 ? id.slice(0, 10) + '…' : id;
}

async function resetSessionRuntime(sessionId) {
    try {
        await api('POST', `/sessions/${sessionId}/reset-runtime`);
        showToast(`Session ${shortId(sessionId)} runtime reset`);
    } catch (err) {
        showToast(`Reset failed: ${err.message}`, 'error');
    }
}

async function loadMemory() {
    try {
        const data = await api('GET', '/memory');
        els.memoryContent.textContent = JSON.stringify(data, null, 2);
        els.memoryModal.classList.remove('hidden');
    } catch (err) {
        alert('加载记忆失败: ' + err.message);
    }
}

async function saveState() {
    try {
        await api('POST', '/save');
        showToast('状态已保存');
    } catch (err) {
        alert('保存失败: ' + err.message);
    }
}

// ─── UI 渲染 ───

function showWelcome() {
    currentView = 'home';
    currentLearningPhase = null;
    updateViewTheme('home');
    els.welcomeScreen.classList.remove('hidden');
    els.messages.innerHTML = '';
    els.topbarTitle.textContent = '學齋';
    els.topbarSubtitle.textContent = getHomeSubtitle(currentMode);
    currentSessionId = null;
    currentSessionTitle = null;
    currentPersonaKey = null;
    currentPersonaName = '';
    syncThinkingPickerLabel('neutral');
    syncCompanionPickerLabel(currentCompanion);
    document.dispatchEvent(new CustomEvent('learning-unit:reset'));
    els.messageInput.disabled = false;
    els.btnSend.disabled = false;
    updateInputPlaceholderFromSession(null);
    updateModeToolbar();
    updateHomeModeCards();
    refreshWelcomeActiveUnits().catch(() => {});
    els.messageInput.focus();
}

// ─── 欢迎页"未完成研习卷"横幅 ───
// 数据来自 GET /learning-units（已有全量返回，前端过滤 phase ∈ {absorbing, outputting}）。
// 横幅出现条件 = 有进行中卷；列表为空时整块隐藏不占位。
async function fetchActiveLearningUnits() {
    const data = await api('GET', '/learning-units');
    const units = Array.isArray(data) ? data : (Array.isArray(data?.units) ? data.units : []);
    return units
        .filter(u => u && (u.phase === 'absorbing' || u.phase === 'outputting'))
        .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
}

function renderWelcomeActiveUnits(units) {
    const host = els.welcomeActiveUnits;
    if (!host) return;
    if (!units || units.length === 0) {
        host.classList.add('hidden');
        host.innerHTML = '';
        return;
    }
    const phaseLabel = (p) => (p === 'outputting' ? '复述检验' : '研习中');
    const html = units.map((u) => {
        const sid = u.session_id || '';
        const uid = u.id || '';
        const objective = (u.objective && u.objective.text) || u.working_objective || '（未确定主题）';
        const phase = u.phase || 'absorbing';
        return `
            <div class="welcome-active-unit-card" data-unit-id="${escapeHtml(uid)}" data-session-id="${escapeHtml(sid)}">
                <div class="welcome-active-unit-head">
                    <span class="welcome-active-unit-kicker">还有一卷未完成</span>
                    <span class="lu-phase is-current">${escapeHtml(phaseLabel(phase))}</span>
                </div>
                <p class="welcome-active-unit-title">${escapeHtml(objective)}</p>
                <div class="welcome-active-unit-actions">
                    <button class="lu-btn lu-btn-secondary" type="button" data-action="stop"
                            data-unit-id="${escapeHtml(uid)}">先停掉这卷</button>
                    <button class="lu-btn lu-btn-primary" type="button" data-action="continue"
                            data-unit-id="${escapeHtml(uid)}" data-session-id="${escapeHtml(sid)}">继续这一卷</button>
                </div>
            </div>
        `;
    }).join('');
    host.innerHTML = html;
    host.classList.remove('hidden');
}

async function refreshWelcomeActiveUnits() {
    if (!els.welcomeActiveUnits) return;
    try {
        const units = await fetchActiveLearningUnits();
        renderWelcomeActiveUnits(units);
    } catch (err) {
        // 接口失败不影响首页主体——静默隐藏，不打扰用户。
        els.welcomeActiveUnits.classList.add('hidden');
        els.welcomeActiveUnits.innerHTML = '';
        console.warn('拉取未完成研习卷失败:', err);
    }
}

async function stopLearningUnitById(unitId) {
    return api('POST', `/learning-units/${encodeURIComponent(unitId)}/stop`, { reason: 'user_stopped' });
}

function closeLearningStopModal() {
    if (!els.learningStopModal) return;
    els.learningStopModal.classList.add('hidden');
    els.learningStopModal.setAttribute('aria-hidden', 'true');
}

function openLearningStopModal(detail = {}) {
    if (!els.learningStopModal) return;
    const objective = (detail.objectiveText || '').trim() || '当前研习主题';
    els.learningStopModalTitle.textContent = '这一卷先学到这里';
    els.learningStopModalText.textContent = '本卷内容会保留在历史中。你可以留在这里回看，也可以现在直接开始一个新主题。';
    els.learningStopModalObjective.textContent = objective;
    els.learningStopModal.classList.remove('hidden');
    els.learningStopModal.setAttribute('aria-hidden', 'false');
}

// 复用研习卷确认弹窗 —— POST /learning-units 返回 409 时由 sendMessage 调起，让用户
// 显式选择「继续这一卷」还是「先停掉它，按新主题继续」。
// state 用闭包变量保存当前面对的 activeUnit + 原始 seedText（用户刚刚打的字），
// 两个按钮和事件绑定区共享这份 state。
let pendingResumeContext = null;

function openLearningResumeModal({ activeUnit, seedText }) {
    if (!els.learningResumeModal) return;
    pendingResumeContext = { activeUnit, seedText };
    const objective = (activeUnit?.objective?.text || activeUnit?.working_objective || '').trim() || '（未确定主题）';
    if (els.learningResumeModalObjective) {
        els.learningResumeModalObjective.textContent = objective;
    }
    els.learningResumeModal.classList.remove('hidden');
    els.learningResumeModal.setAttribute('aria-hidden', 'false');
}

function closeLearningResumeModal() {
    if (!els.learningResumeModal) return;
    els.learningResumeModal.classList.add('hidden');
    els.learningResumeModal.setAttribute('aria-hidden', 'true');
    pendingResumeContext = null;
}

function hideWelcome() {
    currentView = 'chat';
    updateViewTheme('chat');
    els.welcomeScreen.classList.add('hidden');
}

function updateTopbarPersona(personaKey, _mode = currentMode) {
    // 思路 picker is the canonical surface — we keep currentPersonaKey in sync
    // and refresh the picker label, but do not paint an avatar/subtitle blurb.
    const key = personaKey || 'neutral';
    currentPersonaKey = key === 'neutral' ? null : key;
    currentPersonaName = getPersonaDisplayName(key);
    syncThinkingPickerLabel(key);
}

function syncTopbarFromSession(session, title) {
    els.topbarTitle.textContent = title || session?.title || currentSessionTitle || '學齋';
    const semanticMode = session ? modeFromSession(session) : currentMode;
    const personaKey = resolvePersonaKeyForMode(semanticMode, session);
    if (semanticMode === 'chat') {
        syncCompanionPickerLabel(companionSettingsFromSession(session));
    }
    updateTopbarPersona(personaKey, semanticMode);
    els.topbarSubtitle.textContent = semanticMode === 'learning'
        ? '研习中 · 正在加载研习卷'
        : (currentCompanion.enabled
            ? `${getCompanionDisplayName(currentCompanion.style)}陪伴 · ${currentSessionTitle || title || '闲谈'}`
            : (currentSessionTitle || title || ''));
}

function updateHomeModeCards() {
    document.querySelectorAll('.home-mode-card').forEach(card => {
        const isActive = card.dataset.mode === currentMode && !card.disabled;
        card.classList.toggle('active', isActive);
    });
}

// ─── 陪伴 (companion / 减压) picker ───

function companionCatalogEntries() {
    if (companionCatalog && Array.isArray(companionCatalog.styles)) {
        return companionCatalog.styles;
    }
    return Object.entries(COMPANION_META).map(([key, meta]) => ({
        key,
        display_name: meta.name,
    }));
}

function syncCompanionPickerLabel(settings = currentCompanion) {
    if (!els.companionTriggerValue) return;
    const normalized = normalizeCompanionSettings(settings);
    currentCompanion = normalized;
    els.companionTriggerValue.textContent = getCompanionDisplayName(
        normalized.style,
        normalized.enabled
    );
    if (els.topbarCompanion) {
        els.topbarCompanion.dataset.companionStyle = normalized.style;
        els.topbarCompanion.classList.toggle('has-companion', normalized.enabled);
    }
    if (els.companionMenu) {
        els.companionMenu.querySelectorAll('.companion-option').forEach(opt => {
            opt.classList.toggle('active', opt.dataset.companionStyle === normalized.style);
        });
    }
    if (currentView === 'home' && currentMode === 'chat') {
        els.topbarSubtitle.textContent = getHomeSubtitle(currentMode);
    } else if (currentView === 'chat' && currentMode === 'chat') {
        const title = currentSessionTitle || els.topbarTitle.textContent || '闲谈';
        els.topbarSubtitle.textContent = normalized.enabled
            ? `${getCompanionDisplayName(normalized.style)}陪伴 · ${title}`
            : title;
    }
}

function renderCompanionMenu() {
    if (!els.companionMenu) return;
    const entries = companionCatalogEntries();
    els.companionMenu.innerHTML = entries.map(entry => {
        const key = entry.key || 'off';
        const meta = getCompanionMeta(key);
        const name = entry.display_name || meta.name;
        return `
            <button type="button" class="companion-option" data-companion-style="${escapeHtml(key)}" role="option">
                <span class="companion-option-mark">${escapeHtml(meta.mark || '·')}</span>
                <span class="companion-option-copy">
                    <span class="companion-option-name">${escapeHtml(name)}</span>
                    <span class="companion-option-short">${escapeHtml(meta.short || '')}</span>
                </span>
            </button>
        `;
    }).join('');
    els.companionMenu.querySelectorAll('.companion-option').forEach(btn => {
        btn.addEventListener('click', () => {
            const style = btn.dataset.companionStyle || 'off';
            applyCompanionSelection(style);
            closeCompanionMenu();
        });
    });
    syncCompanionPickerLabel(currentCompanion);
}

function openCompanionMenu() {
    if (!els.companionMenu) return;
    els.companionMenu.classList.remove('hidden');
    els.companionTrigger.setAttribute('aria-expanded', 'true');
    els.topbarCompanion.classList.add('is-open');
}

function closeCompanionMenu() {
    if (!els.companionMenu) return;
    els.companionMenu.classList.add('hidden');
    els.companionTrigger.setAttribute('aria-expanded', 'false');
    els.topbarCompanion.classList.remove('is-open');
}

async function applyCompanionSelection(styleKey) {
    const style = styleKey || 'off';
    const next = normalizeCompanionSettings({
        enabled: style !== 'off',
        style,
        advice_level: currentCompanion.adviceLevel || 'low',
    });
    syncCompanionPickerLabel(next);

    if (!currentSessionId || currentMode !== 'chat') return;
    try {
        const saved = await api('PUT', `/sessions/${currentSessionId}/companion`, {
            enabled: next.enabled,
            style: next.style,
            advice_level: next.adviceLevel,
        });
        syncCompanionPickerLabel(normalizeCompanionSettings(saved));
    } catch (err) {
        console.warn('更新陪伴失败:', err);
        showToast('切换陪伴失败：' + err.message);
    }
}

async function bindCurrentCompanionToSession(settings = currentCompanion) {
    if (!currentSessionId || currentMode !== 'chat') return;
    const normalized = normalizeCompanionSettings(settings);
    try {
        await api('PUT', `/sessions/${currentSessionId}/companion`, {
            enabled: normalized.enabled,
            style: normalized.style,
            advice_level: normalized.adviceLevel,
        });
    } catch (err) {
        console.warn('绑定陪伴到新会话失败:', err);
    }
}

async function loadCompanionCatalog() {
    try {
        companionCatalog = await api('GET', '/companion-styles');
    } catch (err) {
        console.warn('载入陪伴列表失败，使用本地默认列表:', err);
        companionCatalog = null;
    }
    renderCompanionMenu();
}

function setupCompanionPicker() {
    if (!els.companionTrigger) return;
    els.companionTrigger.addEventListener('click', (e) => {
        e.stopPropagation();
        if (els.companionMenu.classList.contains('hidden')) {
            openCompanionMenu();
        } else {
            closeCompanionMenu();
        }
    });
    document.addEventListener('click', (e) => {
        if (!els.topbarCompanion.contains(e.target)) closeCompanionMenu();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeCompanionMenu();
    });
}

// ─── 思路 (persona overlay) picker ───

function personaCatalogEntries() {
    if (personaCatalog && Array.isArray(personaCatalog.philosophers)) {
        return [personaCatalog.default, ...personaCatalog.philosophers];
    }
    return ['neutral', ...PHILOSOPHER_ORDER].map(key => {
        const meta = getPersonaMeta(key);
        return { key, display_name: meta.name, role_name: '' };
    });
}

function syncThinkingPickerLabel(personaKey) {
    if (!els.thinkingTriggerValue) return;
    const key = personaKey || 'neutral';
    const meta = getPersonaMeta(key);
    els.thinkingTriggerValue.textContent = meta.name;
    if (els.topbarThinking) {
        els.topbarThinking.dataset.personaKey = key;
        els.topbarThinking.classList.toggle('has-overlay', key !== 'neutral');
    }
    if (els.thinkingMenu) {
        els.thinkingMenu.querySelectorAll('.thinking-option').forEach(opt => {
            opt.classList.toggle('active', opt.dataset.personaKey === key);
        });
    }
}

function renderThinkingMenu() {
    if (!els.thinkingMenu) return;
    const entries = personaCatalogEntries();
    els.thinkingMenu.innerHTML = entries.map(entry => {
        const key = entry.key;
        const meta = getPersonaMeta(key);
        const name = entry.display_name || meta.name;
        const short = meta.short || '';
        const role = entry.role_name || '';
        return `
            <button type="button" class="thinking-option" data-persona-key="${escapeHtml(key)}" role="option">
                <span class="thinking-option-mark">${escapeHtml(meta.mark || '·')}</span>
                <span class="thinking-option-copy">
                    <span class="thinking-option-name">${escapeHtml(name)}${role ? `<em>· ${escapeHtml(role)}</em>` : ''}</span>
                    <span class="thinking-option-short">${escapeHtml(short)}</span>
                </span>
            </button>
        `;
    }).join('');
    els.thinkingMenu.querySelectorAll('.thinking-option').forEach(btn => {
        btn.addEventListener('click', () => {
            const key = btn.dataset.personaKey || 'neutral';
            applyPersonaSelection(key);
            closeThinkingMenu();
        });
    });
    syncThinkingPickerLabel(currentPersonaKey || 'neutral');
}

function openThinkingMenu() {
    if (!els.thinkingMenu) return;
    els.thinkingMenu.classList.remove('hidden');
    els.thinkingTrigger.setAttribute('aria-expanded', 'true');
    els.topbarThinking.classList.add('is-open');
}

function closeThinkingMenu() {
    if (!els.thinkingMenu) return;
    els.thinkingMenu.classList.add('hidden');
    els.thinkingTrigger.setAttribute('aria-expanded', 'false');
    els.topbarThinking.classList.remove('is-open');
}

async function applyPersonaSelection(personaKey) {
    const key = personaKey || 'neutral';
    // Optimistic local update so the picker feels instant.
    currentPersonaKey = key === 'neutral' ? null : key;
    currentPersonaName = getPersonaDisplayName(key);
    syncThinkingPickerLabel(key);

    if (!currentSessionId) return; // welcome screen — bind when session is created
    try {
        await api('PUT', `/sessions/${currentSessionId}/persona`, { persona_key: key });
    } catch (err) {
        console.warn('更新思路失败:', err);
        showToast('切换思路失败：' + err.message);
    }
}

async function loadPersonaCatalog() {
    try {
        personaCatalog = await api('GET', '/personas');
    } catch (err) {
        console.warn('载入思路列表失败，使用本地默认列表:', err);
        personaCatalog = null;
    }
    renderThinkingMenu();
}

function setupThinkingPicker() {
    if (!els.thinkingTrigger) return;
    els.thinkingTrigger.addEventListener('click', (e) => {
        e.stopPropagation();
        if (els.thinkingMenu.classList.contains('hidden')) {
            openThinkingMenu();
        } else {
            closeThinkingMenu();
        }
    });
    document.addEventListener('click', (e) => {
        if (!els.topbarThinking.contains(e.target)) closeThinkingMenu();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeThinkingMenu();
    });
}

function renderSessionList(sessions) {
    els.sidebarScroll.innerHTML = '';
    if (!sessions || sessions.length === 0) {
        els.sidebarScroll.innerHTML = '<div class="sidebar-empty">尚无会话记录</div>';
        return;
    }

    // 按时间倒序
    const sorted = [...sessions].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

    // 分组
    const groups = { today: [], yesterday: [], week: [], earlier: [] };
    for (const s of sorted) {
        const label = formatDateLabel(s.created_at);
        groups[label].push(s);
    }

    for (const key of ['today', 'yesterday', 'week', 'earlier']) {
        const items = groups[key];
        if (!items.length) continue;

        const groupDiv = document.createElement('div');
        groupDiv.className = 'session-group';

        const labelDiv = document.createElement('div');
        labelDiv.className = 'group-label';
        labelDiv.textContent = GROUP_NAMES[key];
        groupDiv.appendChild(labelDiv);

        const ul = document.createElement('ul');
        ul.className = 'session-list';

        for (const s of items) {
            const sessionMode = modeFromSession(s);
            const sessionModeLabel = getModeLabel(sessionMode);
            const li = document.createElement('li');
            li.className = `session-item session-item-${sessionMode}`;
            li.dataset.id = s.id;
            li.dataset.mode = sessionMode;
            if (s.id === currentSessionId) li.classList.add('active');

            // 图标
            li.innerHTML = `
                <span class="session-icon" aria-hidden="true">
                    ${sessionMode === 'learning'
                        ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"></path><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"></path></svg>'
                        : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>'}
                </span>
                <span class="session-main">
                    <span class="session-title">${escapeHtml(s.title || s.id)}</span>
                    <span class="session-mode-badge session-mode-${sessionMode}" title="${getSessionTypeTitle(sessionMode)}">${sessionModeLabel}</span>
                </span>
                <div class="session-actions">
                    <button class="btn-edit" title="重命名">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
                    </button>
                    <button class="btn-reset" title="重置运行时状态">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg>
                    </button>
                    <button class="btn-delete" title="删除">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                    </button>
                </div>
            `;

            // 点击切换会话（排除操作按钮）
            li.addEventListener('click', (e) => {
                if (e.target.closest('.session-actions')) return;
                selectSession(s.id, s.title || s.id);
                closeSidebar();
            });

            // 重命名
            li.querySelector('.btn-edit').addEventListener('click', (e) => {
                e.stopPropagation();
                startInlineEdit(li, s.id, s.title || s.id);
            });

            // 重置运行时
            li.querySelector('.btn-reset').addEventListener('click', (e) => {
                e.stopPropagation();
                resetSessionRuntime(s.id);
            });

            // 删除
            li.querySelector('.btn-delete').addEventListener('click', (e) => {
                e.stopPropagation();
                deleteTargetId = s.id;
                els.deleteModal.classList.remove('hidden');
            });

            ul.appendChild(li);
        }

        groupDiv.appendChild(ul);
        els.sidebarScroll.appendChild(groupDiv);
    }
}

function startInlineEdit(li, sessionId, currentTitle) {
    const titleSpan = li.querySelector('.session-title');
    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentTitle;
    titleSpan.innerHTML = '';
    titleSpan.appendChild(input);
    input.focus();
    input.select();

    const save = () => {
        const newTitle = input.value.trim();
        if (newTitle && newTitle !== currentTitle) {
            renameSession(sessionId, newTitle);
        } else {
            titleSpan.textContent = currentTitle;
        }
    };

    input.addEventListener('blur', save);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            input.blur();
        } else if (e.key === 'Escape') {
            titleSpan.textContent = currentTitle;
        }
    });
}

async function selectSession(id, title) {
    currentSessionId = id;
    currentSessionTitle = title;
    localStorage.setItem('lastSessionId', id);
    hideWelcome();
    clearMessages();

    // 高亮
    document.querySelectorAll('.session-item').forEach(li => {
        li.classList.toggle('active', li.dataset.id === id);
    });

    try {
        const session = await api('GET', `/sessions/${id}`);
        currentMode = modeFromSession(session);
        updateModeToolbar();
        updateHomeModeCards();
        syncTopbarFromSession(session, title || session.title || id);
        await loadSessionHistory(id, session);
        updateInputPlaceholderFromSession(session);
        // adaptive alignment §11.3: 让研习卷 UI 模块同步目标卡 / 进度 / 建议条。
        document.dispatchEvent(new CustomEvent('learning-unit:session-loaded', {
            detail: { session, sessionId: id },
        }));
    } catch (err) {
        console.error('加载会话失败:', err);
    }

    els.messageInput.disabled = false;
    els.btnSend.disabled = false;
    els.messageInput.focus();
}

async function loadSessionHistory(sessionId, sessionData = null) {
    try {
        const session = sessionData || await api('GET', `/sessions/${sessionId}`);
        const messages = Array.isArray(session.messages)
            ? session.messages
            : (session.entries || []).filter(e =>
                e.type === 'message' &&
                (e.role === 'user' || e.role === 'assistant')
            );
        for (const entry of messages) {
            renderHistoryMessage(entry.role, entry.content, entry.metadata || {});
        }
        scrollToBottom();
    } catch (err) {
        console.error('加载历史消息失败:', err);
    }
}

function renderHistoryMessage(role, text, metadata = {}) {
    addMessage(role, text, {
        mode: metadata.learning_unit_id ? 'learning' : normalizeFrontendMode(metadata.mode),
        alignment: Boolean(metadata.alignment),
        personaKey: metadata.persona_key || '',
        personaName: metadata.persona_name || '',
        companionStyle: metadata.companion_style || '',
        companionStyleName: metadata.companion_style_name || '',
        companionEnabled: Boolean(metadata.companion_enabled),
        usage: metadata.usage || metadata.turn_usage || null,
    });
}

function clearMessages() {
    els.messages.innerHTML = '';
}

function addMessage(role, text, options = {}) {
    const msg = document.createElement('div');
    msg.className = `message ${role}`;
    if (options.mode === 'learning') {
        msg.classList.add('learning-mode');
    }
    if (options.alignment) {
        msg.classList.add('alignment');
    }

    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    if (role === 'assistant') {
        const personaKey = options.personaKey || 'neutral';
        avatar.classList.add('persona-mark');
        avatar.dataset.personaKey = personaKey;
        if (options.companionEnabled) {
            avatar.classList.add('companion-mark');
            avatar.textContent = getCompanionMeta(options.companionStyle).mark || '☾';
        } else {
            avatar.textContent = getPersonaMark(personaKey);
        }
    } else if (role === 'user') {
        avatar.classList.add('user-mark');
        avatar.textContent = '我';
    } else {
        avatar.textContent = '·';
    }

    const content = document.createElement('div');
    content.className = 'message-content';
    if (role === 'assistant') {
        content.dataset.raw = text || '';
        const body = ensureAssistantMessageBody(content);
        body.innerHTML = text ? renderChatMarkdown(text) : '<span class="typing-cursor"></span>';
        if (options.personaKey) {
            content.dataset.personaKey = options.personaKey;
        }
        if (options.personaName) {
            content.dataset.personaName = options.personaName;
            upsertAssistantPersonaBadge(content, options.personaName);
        }
        if (options.companionEnabled) {
            content.dataset.companionStyle = options.companionStyle || '';
            content.dataset.companionStyleName = options.companionStyleName || getCompanionDisplayName(options.companionStyle);
            upsertAssistantCompanionBadge(content, content.dataset.companionStyleName);
        }
        upsertAssistantUsage(content, options.usage || null);
    } else {
        content.innerHTML = renderPlainText(text);
    }

    msg.appendChild(avatar);
    msg.appendChild(content);
    els.messages.appendChild(msg);
    scrollToBottom();
    return content;
}

function upsertAssistantPersonaBadge(content, personaName) {
    if (!personaName) return;
    let badge = content.querySelector('.persona-badge');
    if (!badge) {
        badge = document.createElement('span');
        badge.className = 'persona-badge';
        const body = content.querySelector('.message-body');
        if (body) {
            content.insertBefore(badge, body);
        } else {
            content.insertBefore(badge, content.firstChild);
        }
    }
    badge.textContent = personaName;
}

function upsertAssistantCompanionBadge(content, styleName) {
    if (!styleName) return;
    let badge = content.querySelector('.companion-badge');
    if (!badge) {
        badge = document.createElement('span');
        badge.className = 'companion-badge';
        const body = content.querySelector('.message-body');
        if (body) {
            content.insertBefore(badge, body);
        } else {
            content.insertBefore(badge, content.firstChild);
        }
    }
    badge.textContent = `☾ ${styleName}陪伴`;
}

function setLastAssistantPersona(personaKey, personaName) {
    if (!personaKey && !personaName) return;
    const contents = els.messages.querySelectorAll('.message.assistant .message-content');
    if (contents.length === 0) return;
    const last = contents[contents.length - 1];
    const meta = getPersonaMeta(personaKey) || {};
    const resolvedName = personaName || meta.name || '';
    const resolvedKey = personaKey || last.dataset.personaKey || currentPersonaKey || '';
    if (resolvedKey) {
        last.dataset.personaKey = resolvedKey;
    }
    if (resolvedName) {
        last.dataset.personaName = resolvedName;
        upsertAssistantPersonaBadge(last, resolvedName);
    }

    const messages = els.messages.querySelectorAll('.message.assistant');
    if (messages.length > 0 && resolvedKey) {
        const lastMessage = messages[messages.length - 1];
        const avatar = lastMessage.querySelector('.message-avatar');
        if (avatar) {
            avatar.classList.add('persona-mark');
            avatar.dataset.personaKey = resolvedKey;
            avatar.textContent = getPersonaMark(resolvedKey);
        }
    }

    if (resolvedKey) {
        updateTopbarPersona(resolvedKey, currentMode);
    }
}

function setLastAssistantCompanion(styleKey, styleName) {
    const contents = els.messages.querySelectorAll('.message.assistant .message-content');
    if (contents.length === 0) return;
    const last = contents[contents.length - 1];
    const resolvedStyle = styleKey || last.dataset.companionStyle || 'warm_girlfriend';
    const resolvedName = styleName || getCompanionDisplayName(resolvedStyle);
    last.dataset.companionStyle = resolvedStyle;
    last.dataset.companionStyleName = resolvedName;
    upsertAssistantCompanionBadge(last, resolvedName);

    const messages = els.messages.querySelectorAll('.message.assistant');
    if (messages.length > 0) {
        const lastMessage = messages[messages.length - 1];
        const avatar = lastMessage.querySelector('.message-avatar');
        if (avatar) {
            avatar.classList.add('companion-mark');
            avatar.textContent = getCompanionMeta(resolvedStyle).mark || '☾';
        }
    }
}

function appendToLastMessage(text) {
    const contents = els.messages.querySelectorAll('.message.assistant .message-content');
    if (contents.length === 0) return;
    const last = contents[contents.length - 1];
    let raw = last.dataset.raw || '';
    raw += text;
    last.dataset.raw = raw;
    const body = ensureAssistantMessageBody(last);
    body.innerHTML = renderChatMarkdown(raw) + '<span class="typing-cursor"></span>';
    upsertAssistantPersonaBadge(last, last.dataset.personaName || '');
    upsertAssistantCompanionBadge(last, last.dataset.companionStyleName || '');
    scrollToBottom();
}

function finalizeLastMessage() {
    const contents = els.messages.querySelectorAll('.message.assistant .message-content');
    if (contents.length === 0) return;
    const last = contents[contents.length - 1];
    const cursor = last.querySelector('.typing-cursor');
    if (cursor) cursor.remove();
}

function setTyping(show) {
    els.typingIndicator.classList.toggle('hidden', !show);
}

function showToast(message) {
    // 简单 toast
    const toast = document.createElement('div');
    toast.textContent = message;
    toast.style.cssText = `
        position: fixed;
        bottom: 80px;
        left: 50%;
        transform: translateX(-50%);
        background: var(--bg-secondary);
        color: var(--text-primary);
        padding: 10px 20px;
        border-radius: var(--radius);
        border: 1px solid var(--border);
        font-size: 0.875rem;
        z-index: 300;
        animation: fadeIn 0.3s ease;
    `;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 2000);
}

if (typeof window !== 'undefined') {
    window.__appShowToast = showToast;
}

// ─── SSE 对话 ───

async function sendMessage(text) {
    if (isStreaming || !text.trim()) return;

    const requestedMode = normalizeFrontendMode(currentMode);
    // 闲聊不再带思路覆层；只有研习才把已选思路绑定到新会话。
    const requestedPersonaKey = requestedMode === 'chat' ? null : currentPersonaKey;

    if (!currentSessionId) {
        // adaptive alignment §6.1: "研习" 起手不走普通 POST /sessions —— 而是
        // POST /learning-units，让后端创建一个挂着 unit 的 session，并把首条用户消息
        // 作为 seed_text 走 absorbing 阶段。
        if (requestedMode === 'learning') {
            if (typeof window.__createLearningUnit !== 'function') {
                showToast('研习模块尚未加载完成，请稍后再试', 'error');
                return;
            }
            const created = await window.__createLearningUnit(text.trim());
            if (!created || !created.session_id) {
                return;
            }
            if (created.reused_active_unit) {
                // 409 路径不再静默 hydrate + toast；交给用户在 modal 里明确选。
                // 原文本回写到输入框，用户决定后可以再发送或修改。
                els.messageInput.value = text.trim();
                autoResizeTextarea();
                openLearningResumeModal({
                    activeUnit: created,
                    seedText: text.trim(),
                });
                return;
            }
            await selectSession(created.session_id, created.objective?.text || '研习');
        } else {
            const companionToBind = { ...currentCompanion };
            const session = await createSession();
            if (!session) return;
            await selectSession(session.id, session.title || 'New chat');
            await bindCurrentCompanionToSession(companionToBind);
            syncCompanionPickerLabel(companionToBind);
        }
        if (requestedPersonaKey) {
            try {
                await api('PUT', `/sessions/${currentSessionId}/persona`, { persona_key: requestedPersonaKey });
                updateTopbarPersona(requestedPersonaKey, currentMode);
            } catch (err) {
                console.warn('绑定思路到新会话失败:', err);
            }
        }
    }

    const isFirstMessage = els.messages.children.length === 0;
    const uiMode = normalizeFrontendMode(currentMode);
    const backendMode = backendModeForFrontendMode(uiMode);
    const personaKey = resolvePersonaKeyForMode(uiMode);
    const personaName = getPersonaMeta(personaKey)?.name || '';

    hideWelcome();
    addMessage('user', text.trim(), { mode: uiMode });

    // 如果是第一条消息，自动用消息内容填充会话标题
    if (isFirstMessage) {
        const autoTitle = text.trim().slice(0, 20) + (text.trim().length > 20 ? '…' : '');
        renameSession(currentSessionId, autoTitle).catch(() => {});
    }
    els.messageInput.value = '';
    els.messageInput.style.height = 'auto';
    els.messageInput.disabled = true;
    els.btnSend.disabled = true;
    isStreaming = true;
    setTyping(true);

    // 对齐轮用特殊样式
    addMessage('assistant', '', {
        mode: uiMode,
        alignment: false,
        personaKey,
        personaName,
    });

    try {
        const response = await fetch(
            `${API_BASE}/sessions/${currentSessionId}/chat`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text.trim(), stream: true, mode: backendMode }),
            }
        );

        if (!response.ok) {
            const err = await response.json().catch(() => ({ detail: '请求失败' }));
            throw new Error(err.detail);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const blocks = buffer.split('\n\n');
            buffer = blocks.pop() || '';

            for (const block of blocks) {
                const lines = block.split('\n');
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    const dataStr = line.slice(6).trim();
                    if (dataStr === '[DONE]') continue;

                    try {
                        const data = JSON.parse(dataStr);
                        if (data.error) throw new Error(data.error);
                        if (data.persona_key || data.persona_name) {
                            setLastAssistantPersona(data.persona_key, data.persona_name);
                        }
                        if (data.companion_enabled) {
                            setLastAssistantCompanion(data.companion_style, data.companion_style_name);
                            syncCompanionPickerLabel({
                                enabled: true,
                                style: data.companion_style,
                                advice_level: data.companion_advice_level,
                            });
                        }
                        if (data.alignment) {
                            const messages = els.messages.querySelectorAll('.message.assistant');
                            const lastMessage = messages[messages.length - 1];
                            if (lastMessage) lastMessage.classList.add('alignment');
                        }
                        if (data.usage) {
                            const contents = els.messages.querySelectorAll('.message.assistant .message-content');
                            if (contents.length > 0) {
                                upsertAssistantUsage(contents[contents.length - 1], data.usage);
                            }
                        }
                        if (typeof data.content === 'string' && data.content.length > 0) {
                            appendToLastMessage(data.content);
                        }
                        // adaptive alignment §11.3: 通知研习卷 UI 模块更新目标卡 / 进度 / 建议条。
                        if (data.learning_unit_id) {
                            document.dispatchEvent(new CustomEvent('learning-unit:metadata', {
                                detail: data,
                            }));
                        }
                    } catch (e) {
                        if (e instanceof SyntaxError) {
                            console.warn('SSE parse error:', dataStr);
                            continue;
                        }
                        throw e;
                    }
                }
            }
        }

        // 流结束后同步服务端状态与当前前端模式
        await updateInputPlaceholder();

    } catch (err) {
        appendToLastMessage(`\n\n**错误：** ${err.message}`);
    } finally {
        finalizeLastMessage();
        isStreaming = false;
        setTyping(false);
        els.messageInput.disabled = false;
        els.btnSend.disabled = false;
        els.messageInput.focus();
    }
}

async function updateInputPlaceholder() {
    if (!currentSessionId) return;
    try {
        const session = await api('GET', `/sessions/${currentSessionId}`);
        updateInputPlaceholderFromSession(session);
    } catch (err) {
        console.warn('更新输入提示失败:', err);
    }
}

function updateInputPlaceholderFromSession(session) {
    if (session) {
        currentMode = modeFromSession(session);
    }
    updateModeToolbar();
    updateHomeModeCards();
    if (!session) {
        if (currentMode === 'learning') {
            els.messageInput.placeholder = '输入想研习的主题、概念或材料...';
        } else {
            els.messageInput.placeholder = '随便聊点什么，直接开始吧...';
        }
        return;
    }
    if (currentMode === 'learning' && currentLearningPhase === 'stopped') {
        els.messageInput.placeholder = '这卷已先学到这里。新建研习主题即可继续...';
    } else if (currentMode === 'learning' && currentLearningPhase === 'consolidated') {
        els.messageInput.placeholder = '这卷已收束。可以新建研习主题继续...';
    } else if (currentMode === 'learning') {
        els.messageInput.placeholder = '继续围绕这一卷研习...';
    } else {
        els.messageInput.placeholder = '继续闲谈...';
    }
}

async function switchMode(mode) {
    mode = normalizeFrontendMode(mode);
    if (currentMode === mode) {
        if (currentSessionId && mode === 'learning') {
            localStorage.removeItem('lastSessionId');
            document.querySelectorAll('.session-item').forEach(li => li.classList.remove('active'));
            showWelcome();
        }
        return;
    }

    currentMode = mode;
    updateModeToolbar();
    updateHomeModeCards();

    if (!currentSessionId) {
        updateInputPlaceholderFromSession(null);
        if (currentView !== 'chat') {
            els.topbarSubtitle.textContent = getHomeSubtitle(mode);
        }
        return;
    }

    // The toolbar chooses what the next conversation should be. Existing
    // sessions keep their own learning_unit_id/mode history, so switching
    // between 闲谈 and 研习 starts from a clean composer.
    localStorage.removeItem('lastSessionId');
    document.querySelectorAll('.session-item').forEach(li => li.classList.remove('active'));
    showWelcome();
}

function syncThinkingPickerVisibility() {
    // 思路 (persona overlay) 仅服务研习；闲聊已分离，不再暴露思路选择。
    if (!els.topbarThinking) return;
    const hideForChat = currentMode === 'chat';
    els.topbarThinking.classList.toggle('hidden', hideForChat);
    if (hideForChat) closeThinkingMenu();
}

function syncCompanionPickerVisibility() {
    if (!els.topbarCompanion) return;
    const showForChat = currentMode === 'chat';
    els.topbarCompanion.classList.toggle('hidden', !showForChat);
    if (!showForChat) closeCompanionMenu();
}

function updateModeToolbar() {
    [els.btnModeChat, els.btnModeLearning].forEach(btn => {
        if (btn) btn.classList.remove('active');
    });
    const activeBtn = {
        chat: els.btnModeChat,
        learning: els.btnModeLearning,
    }[currentMode];
    if (activeBtn) activeBtn.classList.add('active');
    syncCompanionPickerVisibility();
    syncThinkingPickerVisibility();
    // 把当前模式反映到 body，驱动 mode 维度的视觉（闲聊青瓷 / 研习朱砂）。
    document.body.classList.toggle('mode-chat', currentMode === 'chat');
    document.body.classList.toggle('mode-learning', currentMode === 'learning');
}

// ─── 侧边栏交互 ───

function openSidebar() {
    els.sidebar.classList.add('open');
    els.sidebarOverlay.classList.add('show');
}

function closeSidebar() {
    els.sidebar.classList.remove('open');
    els.sidebarOverlay.classList.remove('show');
}

// ─── 事件绑定 ───

els.btnNewChat.addEventListener('click', () => {
    localStorage.removeItem('lastSessionId');
    showWelcome();
    document.querySelectorAll('.session-item').forEach(li => li.classList.remove('active'));
    closeSidebar();
});

els.btnMenu.addEventListener('click', openSidebar);
els.sidebarOverlay.addEventListener('click', closeSidebar);

els.btnMemory.addEventListener('click', loadMemory);
els.btnSave.addEventListener('click', saveState);

els.btnSend.addEventListener('click', () => sendMessage(els.messageInput.value));

els.messageInput.addEventListener('keydown', (e) => {
    if (e.isComposing) return;
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        sendMessage(els.messageInput.value);
    }
});

els.messageInput.addEventListener('input', autoResizeTextarea);

if (els.btnModeChat) {
    els.btnModeChat.addEventListener('click', () => switchMode('chat'));
}
if (els.btnModeLearning) {
    els.btnModeLearning.addEventListener('click', () => switchMode('learning'));
}
document.querySelectorAll('.home-mode-card').forEach(card => {
    if (card.disabled) return;
    card.addEventListener('click', () => switchMode(card.dataset.mode));
});

document.addEventListener('learning-unit:state', (ev) => {
    const detail = ev.detail || {};
    if (detail.sessionId && detail.sessionId !== currentSessionId) return;
    if (!detail.unitId) {
        currentLearningPhase = null;
        return;
    }
    currentLearningPhase = detail.phase || null;
    if (currentMode !== 'learning') return;
    const phaseLabel = LEARNING_PHASE_LABELS[currentLearningPhase] || '研习中';
    const objective = detail.objectiveText || '研习卷';
    els.topbarSubtitle.textContent = `${phaseLabel} · ${objective}`;
    if (currentLearningPhase === 'stopped') {
        els.messageInput.placeholder = '这卷已先学到这里。新建研习主题即可继续...';
    } else if (currentLearningPhase === 'consolidated') {
        els.messageInput.placeholder = '这卷已收束。可以新建研习主题继续...';
    }
});

document.addEventListener('learning-unit:stopped', (ev) => {
    const detail = ev.detail || {};
    if (detail.sessionId && detail.sessionId !== currentSessionId) return;
    currentMode = 'learning';
    currentLearningPhase = 'stopped';
    openLearningStopModal(detail);
});

// 建议卡片
document.querySelectorAll('.suggestion-card').forEach(card => {
    card.addEventListener('click', async () => {
        const mode = normalizeFrontendMode(card.dataset.mode);
        if (mode) {
            await switchMode(mode);
        }
        const text = card.dataset.text;
        if (text) sendMessage(text);
    });
});

// (visibility-change avatar refresh removed along with external avatar fetch.)

// 弹窗关闭
[els.memoryModal, els.deleteModal, els.learningStopModal, els.learningResumeModal].forEach(modal => {
    if (!modal) return;
    modal.querySelector('.modal-overlay').addEventListener('click', () => {
        modal.classList.add('hidden');
        if (modal === els.learningStopModal) {
            modal.setAttribute('aria-hidden', 'true');
        }
        if (modal === els.learningResumeModal) {
            closeLearningResumeModal();
        }
    });
    modal.querySelector('.btn-close').addEventListener('click', () => {
        modal.classList.add('hidden');
        if (modal === els.learningStopModal) {
            modal.setAttribute('aria-hidden', 'true');
        }
        if (modal === els.learningResumeModal) {
            closeLearningResumeModal();
        }
    });
});

els.btnCancelDelete.addEventListener('click', () => {
    els.deleteModal.classList.add('hidden');
    deleteTargetId = null;
});

if (els.btnStayLearningStop) {
    els.btnStayLearningStop.addEventListener('click', () => {
        closeLearningStopModal();
    });
}

if (els.btnNewTopicAfterStop) {
    els.btnNewTopicAfterStop.addEventListener('click', () => {
        closeLearningStopModal();
        localStorage.removeItem('lastSessionId');
        document.querySelectorAll('.session-item').forEach(li => li.classList.remove('active'));
        showWelcome();
    });
}

// 研习卷复用确认弹窗 —— 由 sendMessage 在 reused_active_unit 时打开。
if (els.btnContinueActive) {
    els.btnContinueActive.addEventListener('click', async () => {
        const ctx = pendingResumeContext;
        if (!ctx || !ctx.activeUnit || !ctx.activeUnit.session_id) {
            closeLearningResumeModal();
            return;
        }
        const unit = ctx.activeUnit;
        const seedText = ctx.seedText || '';
        closeLearningResumeModal();
        try {
            await selectSession(unit.session_id, unit.objective?.text || '研习');
            els.messageInput.value = seedText;
            autoResizeTextarea();
            els.messageInput.focus();
        } catch (err) {
            console.warn('继续未完成研习卷失败:', err);
            alert('打开研习卷失败: ' + err.message);
        }
    });
}

if (els.btnStopAndNew) {
    els.btnStopAndNew.addEventListener('click', async () => {
        const ctx = pendingResumeContext;
        if (!ctx || !ctx.activeUnit || !ctx.activeUnit.id) {
            closeLearningResumeModal();
            return;
        }
        const oldUnitId = ctx.activeUnit.id;
        const seedText = ctx.seedText || '';
        els.btnStopAndNew.disabled = true;
        els.btnContinueActive && (els.btnContinueActive.disabled = true);
        try {
            await stopLearningUnitById(oldUnitId);
            closeLearningResumeModal();
            // 老卷停掉后，再走一次创建路径，这次后端不会再 409。
            if (typeof window.__createLearningUnit !== 'function') {
                showToast('研习模块尚未加载完成，请稍后再试');
                return;
            }
            const created = await window.__createLearningUnit(seedText);
            if (created && created.session_id && !created.reused_active_unit) {
                await selectSession(created.session_id, created.objective?.text || '研习');
                els.messageInput.value = seedText;
                autoResizeTextarea();
                els.messageInput.focus();
            }
        } catch (err) {
            console.warn('停掉旧卷开新主题失败:', err);
            alert('操作失败: ' + err.message);
        } finally {
            els.btnStopAndNew.disabled = false;
            els.btnContinueActive && (els.btnContinueActive.disabled = false);
        }
    });
}

// 欢迎页"未完成研习卷"横幅 —— 事件代理，区分 continue / stop 两个动作。
if (els.welcomeActiveUnits) {
    els.welcomeActiveUnits.addEventListener('click', async (ev) => {
        const btn = ev.target.closest('button[data-action]');
        if (!btn) return;
        const action = btn.getAttribute('data-action');
        const unitId = btn.getAttribute('data-unit-id');
        const sessionId = btn.getAttribute('data-session-id');
        if (action === 'continue') {
            if (!sessionId) return;
            try {
                await switchMode('learning');
                await selectSession(sessionId, '研习');
            } catch (err) {
                console.warn('从欢迎页进入未完成研习卷失败:', err);
                alert('打开研习卷失败: ' + err.message);
            }
            return;
        }
        if (action === 'stop') {
            if (!unitId) return;
            btn.disabled = true;
            try {
                await stopLearningUnitById(unitId);
                await refreshWelcomeActiveUnits();
            } catch (err) {
                console.warn('停掉研习卷失败:', err);
                alert('停掉研习卷失败: ' + err.message);
                btn.disabled = false;
            }
        }
    });
}

els.btnConfirmDelete.addEventListener('click', () => {
    if (deleteTargetId) {
        deleteSession(deleteTargetId);
        deleteTargetId = null;
    }
    els.deleteModal.classList.add('hidden');
});

// 顶部栏滚动阴影
els.chatArea.addEventListener('scroll', () => {
    if (els.chatArea.scrollTop > 10) {
        els.topbar.classList.add('scrolled');
    } else {
        els.topbar.classList.remove('scrolled');
    }
});

// ─── 初始化 ───

async function init() {
    updateViewTheme(currentView);
    setupCompanionPicker();
    setupThinkingPicker();
    await loadCompanionCatalog();
    await loadPersonaCatalog();
    updateModeToolbar();
    els.topbarSubtitle.textContent = '正在载入会话…';
    const ok = await checkHealth();
    if (ok) {
        await loadSessions();
        // 恢复上次选中的会话
        const lastId = localStorage.getItem('lastSessionId');
        if (lastId) {
            // 尝试从列表中找到该会话
            const items = document.querySelectorAll('.session-item');
            let found = false;
            for (const li of items) {
                if (li.dataset.id === lastId) {
                    const title = li.querySelector('.session-title')?.textContent || lastId;
                    await selectSession(lastId, title);
                    found = true;
                    break;
                }
            }
            // 若缓存的会话已不存在，清除缓存并回到欢迎页
            if (!found) {
                localStorage.removeItem('lastSessionId');
                showWelcome();
            }
        } else {
            showWelcome();
        }
    } else {
        showWelcome();
    }
    setInterval(checkHealth, 15000);
}

init();

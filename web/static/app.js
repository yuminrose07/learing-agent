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
    btnModeAsk: document.getElementById('btn-mode-ask'),
    btnModeStudy: document.getElementById('btn-mode-study'),
    btnModeLearning: document.getElementById('btn-mode-learning'),
    topbar: document.querySelector('.topbar'),
    topbarTitle: document.getElementById('topbar-title'),
    topbarSubtitle: document.getElementById('topbar-subtitle'),
    topbarThinking: document.getElementById('topbar-thinking'),
    thinkingTrigger: document.getElementById('thinking-trigger'),
    thinkingTriggerValue: document.getElementById('thinking-trigger-value'),
    thinkingMenu: document.getElementById('thinking-menu'),
    apiStatus: document.getElementById('api-status'),
    typingIndicator: document.getElementById('typing-indicator'),
    memoryModal: document.getElementById('memory-modal'),
    memoryContent: document.getElementById('memory-content'),
    deleteModal: document.getElementById('delete-modal'),
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

function resolvePersonaKeyForMode(_mode, session = null) {
    // Personas are universal across modes — pick whatever the session has stored,
    // falling back to the in-memory selection, then NEUTRAL.
    const stored = session ? session.mode_metadata?.chat_persona_key : null;
    return stored || currentPersonaKey || 'neutral';
}

function getModeLabel(mode) {
    return {
        chat: 'Chat 模式',
        ask: 'Ask 模式',
        study: 'Study 模式',
    }[mode] || '对话模式';
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
    if (mode === 'ask') {
        return '问道模式：先与你对齐目标，再开始作答。';
    }
    if (mode === 'study') {
        return '研习模式：由资深学伴主持，深度展开（即将上线）。';
    }
    return '直接输入即可开新一卷。';
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
        throw new Error(err.detail || `HTTP ${res.status}`);
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
    document.dispatchEvent(new CustomEvent('learning-unit:reset'));
    els.messageInput.disabled = false;
    els.btnSend.disabled = false;
    updateAskPlaceholderFromSession(null);
    updateModeToolbar();
    updateHomeModeCards();
    els.messageInput.focus();
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
    const personaKey = resolvePersonaKeyForMode(session?.mode || currentMode, session);
    updateTopbarPersona(personaKey, session?.mode || currentMode);
    els.topbarSubtitle.textContent = currentSessionTitle || title || '';
}

function updateHomeModeCards() {
    document.querySelectorAll('.home-mode-card').forEach(card => {
        const isActive = card.dataset.mode === currentMode && !card.disabled;
        card.classList.toggle('active', isActive);
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
            const li = document.createElement('li');
            li.className = 'session-item';
            li.dataset.id = s.id;
            if (s.id === currentSessionId) li.classList.add('active');

            // 图标
            li.innerHTML = `
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
                <span class="session-title">${escapeHtml(s.title || s.id)}</span>
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
        currentMode = session.mode || 'chat';
        updateModeToolbar();
        updateHomeModeCards();
        syncTopbarFromSession(session, title || session.title || id);
        await loadSessionHistory(id, session);
        updateAskPlaceholderFromSession(session);
        // adaptive alignment §11.3: 让学习卷 UI 模块同步目标卡 / 进度 / 建议条。
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
        mode: metadata.mode || 'chat',
        alignment: Boolean(metadata.alignment),
        personaKey: metadata.persona_key || '',
        personaName: metadata.persona_name || '',
        usage: metadata.usage || metadata.turn_usage || null,
    });
}

function clearMessages() {
    els.messages.innerHTML = '';
}

function addMessage(role, text, options = {}) {
    const msg = document.createElement('div');
    msg.className = `message ${role}`;
    if (options.mode === 'ask') {
        msg.classList.add('ask-mode');
    }
    if (options.mode === 'study') {
        msg.classList.add('study-mode');
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
        avatar.textContent = getPersonaMark(personaKey);
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
        upsertAssistantUsage(content, options.usage || null);
    } else {
        content.innerHTML = renderPlainText(text);
    }

    // Ask 模式标签
    if (role === 'user' && options.mode === 'ask') {
        const badge = document.createElement('span');
        badge.className = 'ask-badge';
        badge.textContent = 'Ask';
        content.insertBefore(badge, content.firstChild);
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

// ─── SSE 对话 ───

async function sendMessage(text) {
    if (isStreaming || !text.trim()) return;

    if (!currentSessionId) {
        // adaptive alignment §6.1: "学习模式" 起手不走普通 POST /sessions —— 而是
        // POST /learning-units，让后端创建一个挂着 unit 的 session，并把首条用户消息
        // 作为 seed_text 走 absorbing 阶段。
        if (currentMode === 'learning' && typeof window.__createLearningUnit === 'function') {
            const created = await window.__createLearningUnit(text.trim());
            if (!created || !created.session_id) {
                return;
            }
            await selectSession(created.session_id, created.objective?.text || 'Learning unit');
        } else {
            const session = await createSession();
            if (!session) return;
            await selectSession(session.id, session.title || 'New chat');
            if (currentPersonaKey) {
                try {
                    await api('PUT', `/sessions/${currentSessionId}/persona`, { persona_key: currentPersonaKey });
                } catch (err) {
                    console.warn('绑定思路到新会话失败:', err);
                }
            }
        }
    }

    const isFirstMessage = els.messages.children.length === 0;

    hideWelcome();
    addMessage('user', text.trim(), { mode: currentMode });

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
        mode: currentMode,
        alignment: currentMode === 'ask',
        personaKey: resolvePersonaKeyForMode(currentMode),
        personaName: getPersonaMeta(resolvePersonaKeyForMode(currentMode))?.name || '',
    });

    try {
        const response = await fetch(
            `${API_BASE}/sessions/${currentSessionId}/chat`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text.trim(), stream: true, mode: currentMode }),
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
                        if (data.usage) {
                            const contents = els.messages.querySelectorAll('.message.assistant .message-content');
                            if (contents.length > 0) {
                                upsertAssistantUsage(contents[contents.length - 1], data.usage);
                            }
                        }
                        if (typeof data.content === 'string' && data.content.length > 0) {
                            appendToLastMessage(data.content);
                        }
                        // adaptive alignment §11.3: 通知学习卷 UI 模块更新目标卡 / 进度 / 建议条。
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

        // 流结束后同步服务端 ask 状态与当前模式
        await updateAskPlaceholder();

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

async function updateAskPlaceholder() {
    if (!currentSessionId) return;
    try {
        const session = await api('GET', `/sessions/${currentSessionId}`);
        updateAskPlaceholderFromSession(session);
    } catch (err) {
        console.warn('更新 ask placeholder 失败:', err);
    }
}

function updateAskPlaceholderFromSession(session) {
    if (session) {
        currentMode = session.mode || 'chat';
    }
    updateModeToolbar();
    updateHomeModeCards();
    if (!session) {
        if (currentMode === 'ask') {
            els.messageInput.placeholder = '描述想做的事，先与你对齐目标再展开...';
        } else {
            els.messageInput.placeholder = '向学伴提问，直接开始吧...';
        }
        return;
    }
    const askStatus = session.ask_state?.status;
    if (askStatus === 'aligning') {
        els.messageInput.placeholder = '请确认或修正上述理解…（回复”确认”开始回答）';
    } else {
        els.messageInput.placeholder = currentMode === 'ask'
            ? '继续补充需要对齐的要求...'
            : '向学伴提问，直接开始吧...';
    }
}

async function switchMode(mode) {
    if (currentMode === mode) return;

    const previousMode = currentMode;
    currentMode = mode;
    updateModeToolbar();
    updateHomeModeCards();

    if (!currentSessionId) {
        updateAskPlaceholderFromSession(null);
        if (currentView !== 'chat') {
            els.topbarSubtitle.textContent = getHomeSubtitle(mode);
        }
        return;
    }

    // adaptive alignment: "learning" 不是后端 AgentMode；它是"会话挂了一个学习卷"的
    // 前端语义。新开会话才能创建学习卷，已有会话切到 learning 没意义——
    // 静默回退到上一个模式，避免出现一个"卡在 learning 但后端没卷"的灰态。
    if (mode === 'learning') {
        currentMode = previousMode;
        updateModeToolbar();
        updateHomeModeCards();
        return;
    }

    try {
        await api('PUT', `/sessions/${currentSessionId}/mode`, { mode });
        await updateAskPlaceholder();
    } catch (err) {
        currentMode = previousMode;
        updateModeToolbar();
        alert('切换模式失败: ' + err.message);
    }
}

function updateModeToolbar() {
    [els.btnModeChat, els.btnModeAsk, els.btnModeStudy, els.btnModeLearning].forEach(btn => {
        if (btn) btn.classList.remove('active');
    });
    const activeBtn = {
        chat: els.btnModeChat,
        ask: els.btnModeAsk,
        study: els.btnModeStudy,
        learning: els.btnModeLearning,
    }[currentMode];
    if (activeBtn) activeBtn.classList.add('active');
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
if (els.btnModeAsk) {
    els.btnModeAsk.addEventListener('click', () => switchMode('ask'));
}
if (els.btnModeLearning) {
    els.btnModeLearning.addEventListener('click', () => switchMode('learning'));
}
document.querySelectorAll('.home-mode-card').forEach(card => {
    if (card.disabled) return;
    card.addEventListener('click', () => switchMode(card.dataset.mode));
});

// 建议卡片
document.querySelectorAll('.suggestion-card').forEach(card => {
    card.addEventListener('click', () => {
        const mode = card.dataset.mode;
        if (mode) {
            currentMode = mode;
            updateModeToolbar();
            updateHomeModeCards();
            updateAskPlaceholderFromSession(null);
            if (currentView !== 'chat') {
                els.topbarSubtitle.textContent = getHomeSubtitle(mode);
            }
        }
        const text = card.dataset.text;
        if (text) sendMessage(text);
    });
});

// (visibility-change avatar refresh removed along with external avatar fetch.)

// 弹窗关闭
[els.memoryModal, els.deleteModal].forEach(modal => {
    modal.querySelector('.modal-overlay').addEventListener('click', () => {
        modal.classList.add('hidden');
    });
    modal.querySelector('.btn-close').addEventListener('click', () => {
        modal.classList.add('hidden');
    });
});

els.btnCancelDelete.addEventListener('click', () => {
    els.deleteModal.classList.add('hidden');
    deleteTargetId = null;
});

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
    setupThinkingPicker();
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

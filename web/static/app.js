/**
 * Learning-Agent Web 前端 v2
 * 参考 ChatGPT / Gemini 设计
 */

const API_BASE = 'http://127.0.0.1:8000';

let currentSessionId = null;
let currentSessionTitle = null;
let isStreaming = false;
let deleteTargetId = null;
let currentMode = 'chat';
let currentView = 'home';
let currentPersonaKey = null;
let currentPersonaName = '';
let avatarRefreshSeed = String(Date.now());
let avatarRefreshTimer = null;

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
    topbar: document.querySelector('.topbar'),
    topbarTitle: document.getElementById('topbar-title'),
    topbarSubtitle: document.getElementById('topbar-subtitle'),
    topbarPersona: document.getElementById('topbar-persona'),
    topbarPersonaAvatar: document.getElementById('topbar-persona-avatar'),
    topbarPersonaName: document.getElementById('topbar-persona-name'),
    topbarPersonaMode: document.getElementById('topbar-persona-mode'),
    apiStatus: document.getElementById('api-status'),
    typingIndicator: document.getElementById('typing-indicator'),
    memoryModal: document.getElementById('memory-modal'),
    memoryContent: document.getElementById('memory-content'),
    deleteModal: document.getElementById('delete-modal'),
    btnCancelDelete: document.getElementById('btn-cancel-delete'),
    btnConfirmDelete: document.getElementById('btn-confirm-delete'),
    personaGalleryGrid: document.getElementById('persona-gallery-grid'),
};

const PERSONA_PROMPTS = {
    empress_shen_qingyi: 'Chinese imperial court chibi portrait, refined palace style, elegant hanfu, premium game UI character icon style, empress, daughter of the prime minister, childhood sweetheart of the emperor, graceful and intelligent, calm eyes, dark teal and ivory hanfu, phoenix hairpin, warm and dignified, dark-background friendly, waist-up portrait, centered composition, not childish, no text, no watermark',
    noble_consort_gu_mingyan: 'Chinese imperial court chibi portrait, refined palace style, elegant hanfu, premium game UI character icon style, noble consort, daughter of a grand duke, aristocratic and composed, highly perceptive, subtle knowing smile, crimson and gold hanfu, peony hair ornaments, graceful and prestigious aura, dark-background friendly, waist-up portrait, centered composition, not childish, no text, no watermark',
    virtuous_consort_pei_ruotang: 'Chinese imperial court chibi portrait, refined palace style, elegant hanfu, premium game UI character icon style, virtuous consort, daughter of the minister of revenue, gentle and studious, soft scholarly charm, warm patient expression, lotus pink and light apricot hanfu, delicate bookish accessories, elegant and tender atmosphere, dark-background friendly, waist-up portrait, centered composition, not childish, no text, no watermark',
    shu_consort_lu_zhiwei: 'Chinese imperial court chibi portrait, refined palace style, elegant hanfu, premium game UI character icon style, shu consort, daughter of a general, straightforward and pure-hearted, sincere and kind, bright clear eyes, slightly shy and inexperienced in romance, jade green and silver hanfu, simple refined hairpiece, clean and refreshing aura, dark-background friendly, waist-up portrait, centered composition, adult, not childish, no text, no watermark',
    zhaoyi_su_lingxi: 'Chinese imperial court chibi portrait, refined palace style, elegant hanfu, premium game UI character icon style, zhaoyi, strikingly beautiful and alluring, mature flirtatious charm, playful smirk, elegant sensuality, rose red and deep purple hanfu with gold details, elaborate floral hair ornaments, magnetic presence, adult feminine charm, dark-background friendly, waist-up portrait, centered composition, not vulgar, no text, no watermark',
};

const PERSONA_META = {
    empress_shen_qingyi: {
        name: '皇后·沈清仪',
        short: '丞相之女，青梅竹马，聪明端庄，最善伴君深学。',
        mode: 'study',
    },
    noble_consort_gu_mingyan: {
        name: '贵妃·顾明嫣',
        short: '镇国公之女，矜贵机敏，最会替皇上收口圣意。',
        mode: 'ask',
    },
    virtuous_consort_pei_ruotang: {
        name: '贤妃·裴若棠',
        short: '户部尚书之女，温柔好学，身上藏着一点旧事。',
        mode: 'chat',
    },
    shu_consort_lu_zhiwei: {
        name: '淑妃·陆知微',
        short: '将军之女，率直清爽，心思干净得像一阵风。',
        mode: 'chat',
    },
    zhaoyi_su_lingxi: {
        name: '昭仪·苏灵犀',
        short: '明艳妩媚，最会拿捏气氛，也最懂如何撩皇上。',
        mode: 'chat',
    },
};

const FIXED_MODE_PERSONAS = {
    ask: 'noble_consort_gu_mingyan',
    study: 'empress_shen_qingyi',
};

const EMPEROR_AVATAR_PROMPT = 'Chinese imperial court chibi portrait, refined palace style, elegant dragon robe, premium game UI character icon style, young emperor, noble and composed, confident gentle gaze, black hair with imperial crown, dark gold and deep black robe, subtle dragon embroidery, dignified and handsome, dark-background friendly, waist-up portrait, centered composition, not childish, no text, no watermark';
const AVATAR_REFRESH_INTERVAL_MS = 4000;
const AVATAR_REFRESH_MAX_ROUNDS = 5;

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
    return PERSONA_META[personaKey] || null;
}

function updateViewTheme(view = currentView) {
    document.body.classList.remove('home-view', 'chat-view');
    if (view === 'chat') {
        document.body.classList.add('chat-view');
    } else if (view === 'home') {
        document.body.classList.add('home-view');
    }
}

function buildPersonaAvatarUrl(personaKey) {
    const prompt = PERSONA_PROMPTS[personaKey];
    if (!prompt) return '';
    return `https://coresg-normal.trae.ai/api/ide/v1/text_to_image?prompt=${encodeURIComponent(prompt)}&image_size=square_hd&avatar_key=${encodeURIComponent(personaKey)}&refresh=${encodeURIComponent(avatarRefreshSeed)}`;
}

function getUserAvatar() {
    return `https://coresg-normal.trae.ai/api/ide/v1/text_to_image?prompt=${encodeURIComponent(EMPEROR_AVATAR_PROMPT)}&image_size=square_hd&avatar_key=emperor&refresh=${encodeURIComponent(avatarRefreshSeed)}`;
}

function getPersonaAvatar(personaKey) {
    return buildPersonaAvatarUrl(personaKey);
}

function resolvePersonaKeyForMode(mode, session = null) {
    if (mode === 'chat') {
        return session
            ? (session.mode_metadata?.chat_persona_key || null)
            : (currentPersonaKey || null);
    }
    return FIXED_MODE_PERSONAS[mode] || null;
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
        return '贵妃已经候着了，皇上可直接下旨，让她先替你对齐目标。';
    }
    if (mode === 'study') {
        return '皇后伴读尚在筹备，皇上可先用 Chat 或 Ask。';
    }
    return '今夜尚未翻牌，皇上可直接下旨。';
}

function refreshAvatarImages() {
    document.querySelectorAll('img[data-persona-key]').forEach(img => {
        const personaKey = img.dataset.personaKey;
        if (!personaKey) return;
        img.src = getPersonaAvatar(personaKey);
    });
    document.querySelectorAll('img[data-avatar-kind="emperor"]').forEach(img => {
        img.src = getUserAvatar();
    });
}

function scheduleAvatarRefresh() {
    if (avatarRefreshTimer) {
        clearTimeout(avatarRefreshTimer);
        avatarRefreshTimer = null;
    }

    let round = 0;
    const tick = () => {
        if (round >= AVATAR_REFRESH_MAX_ROUNDS) {
            avatarRefreshTimer = null;
            return;
        }
        round += 1;
        avatarRefreshSeed = `${Date.now()}-${round}`;
        refreshAvatarImages();
        avatarRefreshTimer = setTimeout(tick, AVATAR_REFRESH_INTERVAL_MS);
    };

    avatarRefreshTimer = setTimeout(tick, AVATAR_REFRESH_INTERVAL_MS);
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
    els.topbarTitle.textContent = 'Learning-Agent';
    els.topbarSubtitle.textContent = getHomeSubtitle(currentMode);
    currentSessionId = null;
    currentSessionTitle = null;
    currentPersonaKey = null;
    currentPersonaName = '';
    els.topbarPersona.classList.add('hidden');
    els.messageInput.disabled = false;
    els.btnSend.disabled = false;
    updateAskPlaceholderFromSession(null);
    updateModeToolbar();
    updateHomeModeCards();
    const personaKey = resolvePersonaKeyForMode(currentMode);
    if (personaKey && currentMode !== 'chat') {
        updateTopbarPersona(personaKey, currentMode);
    }
    els.messageInput.focus();
}

function hideWelcome() {
    currentView = 'chat';
    updateViewTheme('chat');
    els.welcomeScreen.classList.add('hidden');
}

function updateTopbarPersona(personaKey, mode = currentMode) {
    const meta = getPersonaMeta(personaKey);
    if (!meta) {
        currentPersonaKey = null;
        currentPersonaName = '';
        els.topbarPersona.classList.add('hidden');
        els.topbarSubtitle.textContent = currentSessionTitle || getHomeSubtitle(currentMode);
        return;
    }

    currentPersonaKey = personaKey;
    currentPersonaName = meta.name;
    els.topbarPersonaAvatar.dataset.personaKey = personaKey;
    els.topbarPersonaAvatar.src = getPersonaAvatar(personaKey);
    els.topbarPersonaAvatar.alt = `${meta.name}头像`;
    els.topbarPersonaName.textContent = meta.name;
    els.topbarPersonaMode.textContent = getModeLabel(mode);
    els.topbarPersona.classList.remove('hidden');
    els.topbarSubtitle.textContent =
        mode === 'chat'
            ? `今夜由 ${meta.name} 侍奉皇上。`
            : `${meta.name} 正在为皇上处理这一轮。`;
}

function syncTopbarFromSession(session, title) {
    els.topbarTitle.textContent = title || session?.title || currentSessionTitle || 'Learning-Agent';
    const personaKey = resolvePersonaKeyForMode(session?.mode || currentMode, session);
    if (personaKey) {
        updateTopbarPersona(personaKey, session?.mode || currentMode);
    } else {
        els.topbarSubtitle.textContent = currentSessionTitle || '皇上可直接下旨。';
        els.topbarPersona.classList.add('hidden');
    }
}

function updateHomeModeCards() {
    document.querySelectorAll('.home-mode-card').forEach(card => {
        const isActive = card.dataset.mode === currentMode && !card.disabled;
        card.classList.toggle('active', isActive);
    });
}

function renderPersonaGallery() {
    if (!els.personaGalleryGrid) return;
    els.personaGalleryGrid.innerHTML = '';
    for (const key of [
        'empress_shen_qingyi',
        'noble_consort_gu_mingyan',
        'virtuous_consort_pei_ruotang',
        'shu_consort_lu_zhiwei',
        'zhaoyi_su_lingxi',
    ]) {
        const meta = getPersonaMeta(key);
        const card = document.createElement('div');
        card.className = 'persona-card';
        card.innerHTML = `
            <img class="persona-card-avatar" data-persona-key="${key}" src="${getPersonaAvatar(key)}" alt="${meta.name}头像">
            <div class="persona-card-name">${escapeHtml(meta.name)}</div>
            <div class="persona-card-desc">${escapeHtml(meta.short)}</div>
        `;
        els.personaGalleryGrid.appendChild(card);
    }
    refreshAvatarImages();
}

function renderSessionList(sessions) {
    els.sidebarScroll.innerHTML = '';
    if (!sessions || sessions.length === 0) {
        els.sidebarScroll.innerHTML = '<div class="sidebar-empty">今夜尚无寝殿记录</div>';
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
    if (role === 'assistant' && options.personaKey) {
        avatar.innerHTML = `<img class="message-avatar-img" data-persona-key="${escapeHtml(options.personaKey)}" src="${getPersonaAvatar(options.personaKey)}" alt="${escapeHtml(options.personaName || '角色')}头像">`;
    } else if (role === 'user') {
        avatar.innerHTML = `<img class="message-avatar-img" data-avatar-kind="emperor" src="${getUserAvatar()}" alt="皇上头像">`;
    } else {
        avatar.textContent = '🧠';
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
            avatar.innerHTML = `<img class="message-avatar-img" data-persona-key="${escapeHtml(resolvedKey)}" src="${getPersonaAvatar(resolvedKey)}" alt="${escapeHtml(resolvedName || '角色')}头像">`;
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
        const session = await createSession();
        if (!session) return;
        await selectSession(session.id, session.title || 'New chat');
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
            els.messageInput.placeholder = '请告诉贵妃，皇上想先对齐什么任务...';
        } else {
            els.messageInput.placeholder = '给爱妃下一道旨意，直接开始吧...';
        }
        return;
    }
    const askStatus = session.ask_state?.status;
    if (askStatus === 'aligning') {
        els.messageInput.placeholder = '请确认或修正上述理解…（回复“确认”开始回答）';
    } else {
        els.messageInput.placeholder = currentMode === 'ask'
            ? '请继续告诉贵妃，需要对齐哪些要求...'
            : '给爱妃下一道旨意，直接开始吧...';
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
        const personaKey = resolvePersonaKeyForMode(mode);
        if (personaKey && currentView !== 'chat') {
            updateTopbarPersona(personaKey, mode);
        } else if (currentView !== 'chat') {
            els.topbarPersona.classList.add('hidden');
            els.topbarSubtitle.textContent = getHomeSubtitle(mode);
        }
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
    [els.btnModeChat, els.btnModeAsk, els.btnModeStudy].forEach(btn => {
        if (btn) btn.classList.remove('active');
    });
    const activeBtn = {
        chat: els.btnModeChat,
        ask: els.btnModeAsk,
        study: els.btnModeStudy,
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
            const personaKey = resolvePersonaKeyForMode(mode);
            if (personaKey && currentView !== 'chat') {
                updateTopbarPersona(personaKey, mode);
            } else if (currentView !== 'chat') {
                els.topbarPersona.classList.add('hidden');
                els.topbarSubtitle.textContent = getHomeSubtitle(mode);
            }
        }
        const text = card.dataset.text;
        if (text) sendMessage(text);
    });
});

document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        avatarRefreshSeed = `${Date.now()}-visible`;
        refreshAvatarImages();
        scheduleAvatarRefresh();
    }
});

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
    renderPersonaGallery();
    refreshAvatarImages();
    scheduleAvatarRefresh();
    updateModeToolbar();
    els.topbarSubtitle.textContent = '正在替皇上整理上次的寝殿...';
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

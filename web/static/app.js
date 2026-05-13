/**
 * Learning-Agent Web 前端 v2
 * 参考 ChatGPT / Gemini 设计
 */

const API_BASE = 'http://127.0.0.1:8000';

let currentSessionId = null;
let currentSessionTitle = null;
let isStreaming = false;
let deleteTargetId = null;
let isAskMode = false;  // Ask 对齐模式开关

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
    btnAskMode: document.getElementById('btn-ask-mode'),
    topbar: document.querySelector('.topbar'),
    topbarTitle: document.getElementById('topbar-title'),
    apiStatus: document.getElementById('api-status'),
    typingIndicator: document.getElementById('typing-indicator'),
    memoryModal: document.getElementById('memory-modal'),
    memoryContent: document.getElementById('memory-content'),
    deleteModal: document.getElementById('delete-modal'),
    btnCancelDelete: document.getElementById('btn-cancel-delete'),
    btnConfirmDelete: document.getElementById('btn-confirm-delete'),
};

// ─── Markdown 解析器（轻量） ───

function parseMarkdown(text) {
    if (!text) return '';

    // 转义基础 HTML
    let html = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');

    // 代码块（保留换行）
    const codeBlocks = [];
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (match, lang, code) => {
        const idx = codeBlocks.length;
        codeBlocks.push(`<pre><code>${code.trimEnd()}</code></pre>`);
        return `\x00BLOCK${idx}\x00`;
    });

    // 行内代码
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    // 粗体 / 斜体
    html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<em><strong>$1</strong></em>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    html = html.replace(/__(.+?)__/g, '<strong>$1</strong>');
    html = html.replace(/_(.+?)_/g, '<em>$1</em>');

    // 链接
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

    // 水平线
    html = html.replace(/^---+$/gm, '<hr>');

    // 引用块
    html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
    // 合并相邻引用
    html = html.replace(/(<blockquote>.*?<\/blockquote>\n?)+/g, (match) => {
        const inner = match.replace(/<\/?blockquote>/g, '').replace(/\n/g, '<br>');
        return `<blockquote>${inner}</blockquote>`;
    });

    // 列表：先标记，再包裹
    html = html.replace(/^(\s*)[-*+]\s+(.+)$/gm, (match, indent, content) => {
        return `<li style="margin-left:${indent.length * 12}px">${content}</li>`;
    });
    html = html.replace(/(<li[^>]*>.*?<\/li>\n?)+/g, '<ul>$&</ul>');

    // 有序列表
    html = html.replace(/^(\s*)\d+\.\s+(.+)$/gm, (match, indent, content) => {
        return `<li style="margin-left:${indent.length * 12}px">${content}</li>`;
    });
    html = html.replace(/(<li[^>]*>.*?<\/li>\n?)+/g, (match) => {
        // 避免重复包裹已经被 <ul> 包裹的
        if (match.trim().startsWith('<ul>')) return match;
        return `<ol>${match}</ol>`;
    });

    // 段落：按双换行分段
    const paragraphs = html.split(/\n\n+/);
    html = paragraphs.map(p => {
        const trimmed = p.trim();
        if (!trimmed) return '';
        if (trimmed.startsWith('<') && !trimmed.startsWith('<br>')) {
            // 已经是块级标签
            return trimmed;
        }
        return `<p>${trimmed.replace(/\n/g, '<br>')}</p>`;
    }).join('\n');

    // 还原代码块
    codeBlocks.forEach((block, idx) => {
        html = html.replace(`\x00BLOCK${idx}\x00`, block);
    });

    return html;
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
        selectSession(session.id, session.title);
    } catch (err) {
        alert('创建会话失败: ' + err.message);
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
    els.welcomeScreen.classList.remove('hidden');
    els.messages.innerHTML = '';
    els.topbarTitle.textContent = 'Learning-Agent';
    els.messageInput.disabled = true;
    els.btnSend.disabled = true;
}

function hideWelcome() {
    els.welcomeScreen.classList.add('hidden');
}

function renderSessionList(sessions) {
    els.sidebarScroll.innerHTML = '';
    if (!sessions || sessions.length === 0) {
        els.sidebarScroll.innerHTML = '<div style="padding: 16px; color: var(--text-muted); font-size: 0.8rem; text-align: center;">暂无会话</div>';
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

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
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
    els.topbarTitle.textContent = title || id;
    hideWelcome();
    clearMessages();

    // 高亮
    document.querySelectorAll('.session-item').forEach(li => {
        li.classList.toggle('active', li.dataset.id === id);
    });

    // 加载历史消息
    await loadSessionHistory(id);

    els.messageInput.disabled = false;
    els.btnSend.disabled = false;
    els.messageInput.placeholder = 'Message Learning-Agent...';
    els.messageInput.focus();
}

async function loadSessionHistory(sessionId) {
    try {
        const session = await api('GET', `/sessions/${sessionId}`);
        const entries = session.entries || [];
        // 过滤出用户和助手的消息，跳过 system / fork_point
        const messages = entries.filter(e =>
            e.type === 'message' &&
            (e.role === 'user' || e.role === 'assistant')
        );
        for (const entry of messages) {
            renderHistoryMessage(entry.role, entry.content);
        }
        scrollToBottom();
    } catch (err) {
        console.error('加载历史消息失败:', err);
    }
}

function renderHistoryMessage(role, text) {
    const msg = document.createElement('div');
    msg.className = `message ${role}`;

    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = role === 'user' ? '我' : '🧠';

    const content = document.createElement('div');
    content.className = 'message-content';
    content.innerHTML = parseMarkdown(text);

    msg.appendChild(avatar);
    msg.appendChild(content);
    els.messages.appendChild(msg);
}

function clearMessages() {
    els.messages.innerHTML = '';
}

function addMessage(role, text, options = {}) {
    const msg = document.createElement('div');
    msg.className = `message ${role}`;
    if (options.askMode) {
        msg.classList.add('ask-mode');
    }
    if (options.alignment) {
        msg.classList.add('alignment');
    }

    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = role === 'user' ? '我' : '🧠';

    const content = document.createElement('div');
    content.className = 'message-content';
    if (role === 'assistant') {
        // 初始为空，流式追加
        content.innerHTML = text ? parseMarkdown(text) : '<span class="typing-cursor"></span>';
    } else {
        content.innerHTML = parseMarkdown(text);
    }

    // Ask 模式标签
    if (role === 'user' && options.askMode) {
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

function appendToLastMessage(text) {
    const contents = els.messages.querySelectorAll('.message.assistant .message-content');
    if (contents.length === 0) return;
    const last = contents[contents.length - 1];
    // 移除打字光标
    const cursor = last.querySelector('.typing-cursor');
    if (cursor) cursor.remove();

    // 获取当前原始文本（从 data-raw 属性）
    let raw = last.dataset.raw || '';
    raw += text;
    last.dataset.raw = raw;
    last.innerHTML = parseMarkdown(raw) + '<span class="typing-cursor"></span>';
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
    if (!currentSessionId || isStreaming || !text.trim()) return;

    const isFirstMessage = els.messages.children.length === 0;

    hideWelcome();
    addMessage('user', text.trim(), { askMode: isAskMode });

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
    addMessage('assistant', '', { alignment: isAskMode });

    try {
        const response = await fetch(
            `${API_BASE}/sessions/${currentSessionId}/chat`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text.trim(), stream: true, ask_mode: isAskMode }),
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

        // 流结束后，检查是否需要更新 placeholder（进入 ask_pending 状态）
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
        const askStatus = session.ask_state?.status;
        if (askStatus === 'pending') {
            els.messageInput.placeholder = '请确认或修正上述理解…（回复「确认」开始回答）';
        } else {
            els.messageInput.placeholder = 'Message Learning-Agent…';
        }
    } catch (err) {
        console.warn('更新 ask placeholder 失败:', err);
    }
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
    createSession();
    closeSidebar();
});

els.btnMenu.addEventListener('click', openSidebar);
els.sidebarOverlay.addEventListener('click', closeSidebar);

els.btnMemory.addEventListener('click', loadMemory);
els.btnSave.addEventListener('click', saveState);

els.btnSend.addEventListener('click', () => sendMessage(els.messageInput.value));

els.messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage(els.messageInput.value);
    }
});

els.messageInput.addEventListener('input', autoResizeTextarea);

// Ask 模式切换
if (els.btnAskMode) {
    els.btnAskMode.addEventListener('click', () => {
        isAskMode = !isAskMode;
        els.btnAskMode.classList.toggle('active', isAskMode);
        console.log('Ask mode:', isAskMode);
    });
}

// 建议卡片
document.querySelectorAll('.suggestion-card').forEach(card => {
    card.addEventListener('click', () => {
        const text = card.dataset.text;
        if (text) sendMessage(text);
    });
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
            }
        }
    }
    setInterval(checkHealth, 15000);
}

init();

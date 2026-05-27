const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP_PATH = path.join(__dirname, '..', 'web', 'static', 'app.js');
const INDEX_PATH = path.join(__dirname, '..', 'web', 'index.html');
const LEARNING_UNIT_UI_PATH = path.join(__dirname, '..', 'web', 'static', 'learning-unit-ui.js');
const STYLE_PATH = path.join(__dirname, '..', 'web', 'static', 'style.css');
const MARKDOWN_IT_PATH = path.join(__dirname, '..', 'web', 'static', 'vendor', 'markdown-it.min.js');
const APP_SOURCE = fs.readFileSync(APP_PATH, 'utf8');
const INDEX_SOURCE = fs.readFileSync(INDEX_PATH, 'utf8');
const LEARNING_UNIT_UI_SOURCE = fs.readFileSync(LEARNING_UNIT_UI_PATH, 'utf8');
const STYLE_SOURCE = fs.readFileSync(STYLE_PATH, 'utf8');
const MARKDOWN_IT_SOURCE = fs.readFileSync(MARKDOWN_IT_PATH, 'utf8');
const APP_SOURCE_BEFORE_BINDINGS = APP_SOURCE.split('// ─── 事件绑定 ───')[0];

class FakeClassList {
    constructor(owner) {
        this.owner = owner;
        this.classes = new Set();
    }

    setFromString(value) {
        this.classes = new Set(String(value || '').split(/\s+/).filter(Boolean));
        this._sync();
    }

    add(...names) {
        names.filter(Boolean).forEach(name => this.classes.add(name));
        this._sync();
    }

    remove(...names) {
        names.forEach(name => this.classes.delete(name));
        this._sync();
    }

    toggle(name, force) {
        if (force === true) {
            this.classes.add(name);
        } else if (force === false) {
            this.classes.delete(name);
        } else if (this.classes.has(name)) {
            this.classes.delete(name);
        } else {
            this.classes.add(name);
        }
        this._sync();
        return this.classes.has(name);
    }

    contains(name) {
        return this.classes.has(name);
    }

    _sync() {
        this.owner._className = Array.from(this.classes).join(' ');
    }
}

class FakeElement {
    constructor(tagName = 'div', ownerDocument = null) {
        this.tagName = String(tagName).toUpperCase();
        this.ownerDocument = ownerDocument;
        this.children = [];
        this.parentNode = null;
        this.dataset = {};
        this.style = {};
        this.listeners = new Map();
        this.attributes = new Map();
        this._className = '';
        this.classList = new FakeClassList(this);
        this._innerHTML = '';
        this._textContent = '';
        this.value = '';
        this.disabled = false;
        this.placeholder = '';
        this.scrollTop = 0;
        this.scrollHeight = 0;
    }

    get className() {
        return this._className;
    }

    set className(value) {
        this.classList.setFromString(value);
    }

    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
        this._textContent = this._innerHTML.replace(/<[^>]+>/g, '');
        this.children = [];
    }

    get textContent() {
        return this._textContent;
    }

    set textContent(value) {
        this._textContent = String(value);
        this._innerHTML = this._textContent;
        this.children = [];
    }

    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    insertBefore(child, referenceNode) {
        child.parentNode = this;
        const index = this.children.indexOf(referenceNode);
        if (index === -1) {
            this.children.push(child);
        } else {
            this.children.splice(index, 0, child);
        }
        return child;
    }

    remove() {
        if (!this.parentNode) return;
        const siblings = this.parentNode.children;
        const index = siblings.indexOf(this);
        if (index >= 0) siblings.splice(index, 1);
        this.parentNode = null;
    }

    addEventListener(type, handler) {
        const handlers = this.listeners.get(type) || [];
        handlers.push(handler);
        this.listeners.set(type, handlers);
    }

    focus() {}

    select() {}

    closest(selector) {
        let node = this;
        while (node) {
            if (matchesSelector(node, selector)) return node;
            node = node.parentNode;
        }
        return null;
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }

    querySelectorAll(selector) {
        return findMatches(this.children, selector);
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
        if (name === 'id') this.id = String(value);
    }
}

class FakeDocument {
    constructor() {
        this.elementsById = new Map();
        this.listeners = new Map();
        this.body = new FakeElement('body', this);
        this.body.className = '';
        this._topbar = new FakeElement('div', this);
        this._topbar.className = 'topbar';
    }

    createElement(tagName) {
        return new FakeElement(tagName, this);
    }

    getElementById(id) {
        if (!this.elementsById.has(id)) {
            const element = new FakeElement('div', this);
            element.id = id;
            if (id === 'message-input') {
                element.value = '';
                element.style.height = 'auto';
                element.scrollHeight = 48;
            }
            if (id === 'chat-area') {
                element.scrollHeight = 240;
            }
            this.elementsById.set(id, element);
        }
        return this.elementsById.get(id);
    }

    querySelector(selector) {
        if (selector === '.topbar') return this._topbar;
        return null;
    }

    querySelectorAll() {
        return [];
    }

    addEventListener(type, handler) {
        const handlers = this.listeners.get(type) || [];
        handlers.push(handler);
        this.listeners.set(type, handlers);
    }

    dispatchEvent(event) {
        const handlers = this.listeners.get(event.type) || [];
        for (const handler of handlers) handler(event);
        return true;
    }
}

function matchesSimpleSelector(node, selectorPart) {
    if (!selectorPart) return false;
    if (selectorPart.startsWith('.')) {
        const classes = selectorPart.split('.').filter(Boolean);
        return classes.every(name => node.classList.contains(name));
    }
    return node.tagName.toLowerCase() === selectorPart.toLowerCase();
}

function matchesSelector(node, selector) {
    const parts = selector.trim().split(/\s+/).filter(Boolean);
    if (parts.length === 0) return false;
    return matchesSelectorParts(node, parts.length - 1, parts);
}

function matchesSelectorParts(node, index, parts) {
    if (!node || index < 0) return index < 0;
    if (!matchesSimpleSelector(node, parts[index])) return false;
    if (index === 0) return true;
    let parent = node.parentNode;
    while (parent) {
        if (matchesSelectorParts(parent, index - 1, parts)) return true;
        parent = parent.parentNode;
    }
    return false;
}

function collectDescendants(node, result) {
    for (const child of node.children) {
        result.push(child);
        collectDescendants(child, result);
    }
}

function findMatches(nodes, selector) {
    const pool = [];
    for (const node of nodes) {
        pool.push(node);
        collectDescendants(node, pool);
    }
    return pool.filter(node => matchesSelector(node, selector));
}

function loadAppForTest() {
    const document = new FakeDocument();
    const sandbox = {
        module: { exports: {} },
        exports: {},
        console,
        document,
        window: { document },
        Intl,
        Date,
        Math,
        JSON,
        encodeURIComponent,
        decodeURIComponent,
        setTimeout: () => 0,
        clearTimeout: () => {},
        fetch: async () => ({
            ok: true,
            status: 200,
            json: async () => ({}),
        }),
        localStorage: {
            getItem: () => null,
            setItem: () => {},
            removeItem: () => {},
        },
        alert: () => {},
    };

    const markdownItModule = { exports: {} };
    vm.runInNewContext(MARKDOWN_IT_SOURCE, {
        module: markdownItModule,
        exports: markdownItModule.exports,
    }, { filename: MARKDOWN_IT_PATH });
    sandbox.markdownit = markdownItModule.exports;
    sandbox.window.markdownit = markdownItModule.exports;

    const exportCode = `
module.exports = {
    upsertAssistantUsage,
    renderHistoryMessage,
    renderChatMarkdown,
    addMessage,
    normalizeUsage,
    normalizeFrontendMode,
    modeFromSession,
    backendModeForFrontendMode,
    normalizeCompanionSettings,
    formatUsageNumber,
    formatUsagePercent,
    els,
};
`;

    vm.runInNewContext(APP_SOURCE_BEFORE_BINDINGS + exportCode, sandbox, { filename: APP_PATH });
    return sandbox.module.exports;
}

function loadLearningUnitUIForTest(fetchImpl, extraWindow = {}) {
    const document = new FakeDocument();
    const sandbox = {
        console,
        document,
        window: {
            document,
            location: { origin: 'http://127.0.0.1:8000' },
            ...extraWindow,
        },
        fetch: fetchImpl,
        alert: extraWindow.alert || (() => {}),
        encodeURIComponent,
        CustomEvent: class CustomEvent {
            constructor(type, init = {}) {
                this.type = type;
                this.detail = init.detail;
            }
        },
    };

    vm.runInNewContext(LEARNING_UNIT_UI_SOURCE, sandbox, { filename: LEARNING_UNIT_UI_PATH });
    return sandbox;
}

test('upsertAssistantUsage 区分 estimated 与 actual 展示', () => {
    const { upsertAssistantUsage } = loadAppForTest();

    const estimatedContent = new FakeElement('div');
    upsertAssistantUsage(estimatedContent, {
        estimated_prompt_tokens: 1200,
        context_limit: 128000,
        utilization_ratio: 0.0094,
        is_estimated: true,
    });

    const estimatedBlock = estimatedContent.querySelector('.usage-block');
    assert.ok(estimatedBlock);
    assert.match(estimatedBlock.innerHTML, /usage-state estimated/);
    assert.match(estimatedBlock.innerHTML, /估算/);
    assert.match(estimatedBlock.innerHTML, /1,200/);
    assert.match(estimatedBlock.innerHTML, /上限 128,000/);
    assert.match(estimatedBlock.innerHTML, /占比 0\.94%/);

    const actualContent = new FakeElement('div');
    upsertAssistantUsage(actualContent, {
        actual_prompt_tokens: 1024,
        actual_completion_tokens: 256,
        actual_total_tokens: 1280,
        context_limit: 128000,
        utilization_ratio: 0.01,
        is_estimated: false,
    });

    const actualBlock = actualContent.querySelector('.usage-block');
    assert.ok(actualBlock);
    assert.match(actualBlock.innerHTML, /usage-state actual/);
    assert.match(actualBlock.innerHTML, /已对账/);
    assert.match(actualBlock.innerHTML, /1,024/);
    assert.match(actualBlock.innerHTML, /输出 256/);
    assert.match(actualBlock.innerHTML, /总计 1,280/);
    assert.match(actualBlock.innerHTML, /占比 1\.0%/);
});

test('upsertAssistantUsage 在 usage 缺失时优雅降级', () => {
    const { upsertAssistantUsage } = loadAppForTest();
    const content = new FakeElement('div');

    upsertAssistantUsage(content, {
        estimated_prompt_tokens: 320,
        context_limit: 128000,
        utilization_ratio: 0.0025,
    });
    assert.ok(content.querySelector('.usage-block'));

    upsertAssistantUsage(content, { is_estimated: false });
    assert.equal(content.querySelector('.usage-block'), null);
    assert.equal(content.children.length, 0);

    upsertAssistantUsage(content, null);
    assert.equal(content.querySelector('.usage-block'), null);
});

test('renderHistoryMessage 从 metadata.turn_usage 回放 usage 展示', () => {
    const { renderHistoryMessage, els } = loadAppForTest();

    renderHistoryMessage('assistant', '历史回放消息', {
        mode: 'chat',
        persona_key: 'neutral',
        persona_name: '默认',
        turn_usage: {
            estimated_prompt_tokens: 640,
            context_limit: 128000,
            utilization_ratio: 0.005,
            is_estimated: true,
        },
    });

    assert.equal(els.messages.children.length, 1);

    const assistantMessage = els.messages.children[0];
    const content = assistantMessage.querySelector('.message-content');
    const body = content.querySelector('.message-body');
    const usageBlock = content.querySelector('.usage-block');

    assert.ok(body);
    assert.match(body.innerHTML, /历史回放消息/);
    assert.ok(usageBlock);
    assert.match(usageBlock.innerHTML, /640/);
    assert.match(usageBlock.innerHTML, /上限 128,000/);
    assert.match(usageBlock.innerHTML, /占比 0\.5%/);
    assert.match(usageBlock.innerHTML, /估算/);
});

test('前端只暴露闲谈与研习入口', () => {
    assert.match(INDEX_SOURCE, /id="btn-mode-chat"/);
    assert.match(INDEX_SOURCE, /id="btn-mode-learning"/);
    assert.match(INDEX_SOURCE, />闲谈</);
    assert.match(INDEX_SOURCE, />研习</);
    assert.match(INDEX_SOURCE, /闲谈和研习彼此独立/);
    assert.doesNotMatch(INDEX_SOURCE, /btn-mode-ask|btn-mode-study/);
    assert.doesNotMatch(INDEX_SOURCE, /data-mode="ask"|data-mode="study"/);
    assert.doesNotMatch(INDEX_SOURCE, /Ask|Study|问道|学习模式/);
});

test('闲谈页暴露小月亮陪伴切换入口', () => {
    assert.match(INDEX_SOURCE, /id="topbar-companion"/);
    assert.match(INDEX_SOURCE, /id="companion-trigger"/);
    assert.match(INDEX_SOURCE, /companion-trigger-label">陪伴/);
    assert.ok(APP_SOURCE.includes("GET', '/companion-styles'"));
    assert.ok(APP_SOURCE.includes("PUT', `/sessions/${currentSessionId}/companion`"));
    assert.match(APP_SOURCE, /syncCompanionPickerVisibility/);
    assert.match(APP_SOURCE, /setupCompanionPicker/);
    assert.match(STYLE_SOURCE, /\.companion-trigger::before/);
    assert.match(STYLE_SOURCE, /content: "☾"/);
});

test('历史回放能显示陪伴徽章', () => {
    const { renderHistoryMessage, els, normalizeCompanionSettings } = loadAppForTest();

    assert.equal(
        JSON.stringify(normalizeCompanionSettings({
            enabled: true,
            style: 'warm_girlfriend',
            advice_level: 'none',
        })),
        JSON.stringify({ enabled: true, style: 'warm_girlfriend', adviceLevel: 'none' })
    );

    renderHistoryMessage('assistant', '先歇一下。', {
        mode: 'chat',
        companion_enabled: true,
        companion_style: 'warm_girlfriend',
        companion_style_name: '温柔',
    });

    const assistantMessage = els.messages.children[0];
    const content = assistantMessage.querySelector('.message-content');
    const badge = content.querySelector('.companion-badge');
    const avatar = assistantMessage.querySelector('.message-avatar');

    assert.ok(badge);
    assert.match(badge.textContent, /☾ 温柔陪伴/);
    assert.ok(avatar.classList.contains('companion-mark'));
    assert.equal(avatar.textContent, '☾');
});

test('历史会话列表用服务端绑定状态区分闲谈与研习', () => {
    assert.match(APP_SOURCE, /const sessionMode = modeFromSession\(s\)/);
    assert.match(APP_SOURCE, /session-mode-badge session-mode-\$\{sessionMode\}/);
    assert.match(APP_SOURCE, /li\.dataset\.mode = sessionMode/);
    assert.match(STYLE_SOURCE, /\.session-mode-chat/);
    assert.match(STYLE_SOURCE, /\.session-mode-learning/);
});

test('研习 UI 暴露停止动作和常驻状态事件', () => {
    assert.match(LEARNING_UNIT_UI_SOURCE, /data-action="stop"/);
    assert.match(LEARNING_UNIT_UI_SOURCE, /先学到这里/);
    assert.ok(LEARNING_UNIT_UI_SOURCE.includes('/learning-units/${encodeURIComponent(uid)}/stop'));
    assert.match(LEARNING_UNIT_UI_SOURCE, /learning-unit:state/);
    assert.match(INDEX_SOURCE, /id="learning-stop-modal"/);
    assert.match(INDEX_SOURCE, /开始新主题/);
    assert.match(APP_SOURCE, /openLearningStopModal/);
    assert.match(APP_SOURCE, /LEARNING_PHASE_LABELS/);
});

test('欢迎页暴露未完成研习卷入口与显式复用确认弹窗', () => {
    // 欢迎页占位 + 研习未完成 modal 必须存在于 index.html。
    assert.match(INDEX_SOURCE, /id="welcome-active-units"/);
    assert.match(INDEX_SOURCE, /id="learning-resume-modal"/);
    assert.match(INDEX_SOURCE, /id="btn-continue-active"/);
    assert.match(INDEX_SOURCE, /id="btn-stop-and-new"/);
    // app.js 提供横幅渲染 + 复用 modal 的 open/close 入口。
    assert.match(APP_SOURCE, /fetchActiveLearningUnits/);
    assert.match(APP_SOURCE, /renderWelcomeActiveUnits/);
    assert.match(APP_SOURCE, /openLearningResumeModal/);
    assert.match(APP_SOURCE, /closeLearningResumeModal/);
    // 旧的静默 toast 不应再出现 —— 已替换为显式 modal。
    assert.doesNotMatch(APP_SOURCE, /已有未停止的研习卷[;；]先学到这里后再发送新主题/);
    assert.doesNotMatch(LEARNING_UNIT_UI_SOURCE, /已回到未完成的研习卷/);
});

test('研习前端模式发送到后端时映射为 chat', () => {
    const {
        normalizeFrontendMode,
        modeFromSession,
        backendModeForFrontendMode,
    } = loadAppForTest();

    assert.equal(normalizeFrontendMode('chat'), 'chat');
    assert.equal(normalizeFrontendMode('learning'), 'learning');
    assert.equal(normalizeFrontendMode('ask'), 'chat');
    assert.equal(modeFromSession({ mode: 'chat', learning_unit_id: 'unit-1' }), 'learning');
    assert.equal(modeFromSession({ mode: 'ask', learning_unit_id: null }), 'chat');
    assert.equal(backendModeForFrontendMode('learning'), 'chat');
    assert.equal(backendModeForFrontendMode('chat'), 'chat');
    assert.doesNotMatch(APP_SOURCE, /mode:\s*currentMode/);
    assert.match(APP_SOURCE, /created\.reused_active_unit/);
});

test('创建研习卷遇到 active unit 冲突时仅返回标记不再自动 hydrate 或弹 toast', async () => {
    const calls = [];
    const alerts = [];
    const toasts = [];
    const sandbox = loadLearningUnitUIForTest(async (url, opts) => {
        calls.push({ url, method: opts.method, body: opts.body });
        if (url.endsWith('/learning-units') && opts.method === 'POST') {
            return {
                ok: false,
                status: 409,
                json: async () => ({
                    detail: 'An active learning unit already exists; close it first.',
                    active_unit_id: 'lu-active',
                }),
            };
        }
        if (url.endsWith('/learning-units/lu-active') && opts.method === 'GET') {
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    id: 'lu-active',
                    session_id: 'sess-active',
                    phase: 'absorbing',
                    alignment_state: 'none',
                    objective_status: 'working',
                    objective: { text: '未完成研习卷' },
                }),
            };
        }
        throw new Error(`Unexpected request: ${opts.method} ${url}`);
    }, {
        alert: (message) => alerts.push(message),
        __appShowToast: (message) => toasts.push(message),
    });

    const result = await sandbox.window.__createLearningUnit('新的主题');

    // 仍然返回带 reused_active_unit 标记的 unit，让 app.js 触发 #learning-resume-modal。
    assert.equal(result.session_id, 'sess-active');
    assert.equal(result.id, 'lu-active');
    assert.equal(result.reused_active_unit, true);
    // 关键变化：不再自动 hydrate 内部 state（推迟到 app.js 由用户确认后通过
    // selectSession 触发 learning-unit:session-loaded 走标准 hydrate 路径），
    // 也不再静默弹"已回到未完成的研习卷" toast。
    assert.equal(sandbox.window.__learningUnitUI.state.unitId, null);
    assert.equal(sandbox.window.__learningUnitUI.state.sessionId, null);
    assert.deepEqual(alerts, []);
    assert.deepEqual(toasts, []);
    assert.equal(calls.length, 2);
});

test('研习卷停止动作会调用 stop API 并派发带主题的 stopped 事件', async () => {
    const calls = [];
    const stoppedEvents = [];
    const sandbox = loadLearningUnitUIForTest(async (url, opts) => {
        calls.push({ url, method: opts.method, body: opts.body });
        if (url.endsWith('/learning-units/lu-stop') && opts.method === 'GET') {
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    id: 'lu-stop',
                    session_id: 'sess-stop',
                    phase: 'absorbing',
                    alignment_state: 'none',
                    objective_status: 'working',
                    objective: { text: '停止前目标' },
                }),
            };
        }
        if (url.endsWith('/learning-units/lu-stop/stop') && opts.method === 'POST') {
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    id: 'lu-stop',
                    session_id: 'sess-stop',
                    phase: 'stopped',
                    alignment_state: 'skipped',
                    objective_status: 'working',
                    objective: { text: '停止前目标' },
                }),
            };
        }
        throw new Error(`Unexpected request: ${opts.method} ${url}`);
    });

    sandbox.document.addEventListener('learning-unit:stopped', (ev) => {
        stoppedEvents.push(ev.detail);
    });

    sandbox.window.__learningUnitUI.state.sessionId = 'sess-stop';
    sandbox.window.__learningUnitUI.state.unitId = 'lu-stop';
    await sandbox.window.__learningUnitUI.refreshUnit();

    const card = sandbox.document.getElementById('learning-unit-card');
    const stopButton = {
        disabled: false,
        parentNode: card,
        getAttribute(name) {
            return name === 'data-action' ? 'stop' : null;
        },
        closest() {
            return this;
        },
    };
    const clickHandlers = card.listeners.get('click') || [];
    assert.equal(clickHandlers.length, 1);

    clickHandlers[0]({ target: stopButton });
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));

    assert.equal(sandbox.window.__learningUnitUI.state.phase, 'stopped');
    assert.equal(stoppedEvents.length, 1);
    assert.equal(stoppedEvents[0].sessionId, 'sess-stop');
    assert.equal(stoppedEvents[0].unitId, 'lu-stop');
    assert.equal(stoppedEvents[0].phase, 'stopped');
    assert.equal(stoppedEvents[0].objectiveText, '停止前目标');
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[1], {
        url: 'http://127.0.0.1:8000/learning-units/lu-stop/stop',
        method: 'POST',
        body: JSON.stringify({ reason: 'user_stopped' }),
    });
});

test('absorbing 阶段渲染铸造阶段标签（入局），无 learning_action 时不显示动作徽章', async () => {
    const sandbox = loadLearningUnitUIForTest(async (url, opts) => {
        if (url.endsWith('/learning-units/lu-forge') && opts.method === 'GET') {
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    id: 'lu-forge',
                    session_id: 'sess-forge',
                    phase: 'absorbing',
                    alignment_state: 'idle',
                    objective_status: 'working',
                    objective: { text: '理解 attention' },
                    forge_stage: 'entry',
                    temperature_state: 'steady',
                }),
            };
        }
        throw new Error(`Unexpected request: ${opts.method} ${url}`);
    });

    sandbox.window.__learningUnitUI.state.sessionId = 'sess-forge';
    sandbox.window.__learningUnitUI.state.unitId = 'lu-forge';
    await sandbox.window.__learningUnitUI.refreshUnit();

    const card = sandbox.document.getElementById('learning-unit-card');
    assert.match(card.innerHTML, /lu-forge-row/);
    assert.match(card.innerHTML, /lu-forge-stage/);
    assert.match(card.innerHTML, /入局/);
    // 没有 learning_action 时不渲染动作徽章
    assert.ok(!card.innerHTML.includes('lu-learning-action'));
    assert.equal(sandbox.window.__learningUnitUI.state.forgeStage, 'entry');
});

test('absorbing 阶段渲染碰撞阶段与学习动作徽章（准备试答）', async () => {
    const sandbox = loadLearningUnitUIForTest(async (url, opts) => {
        if (url.endsWith('/learning-units/lu-forge2') && opts.method === 'GET') {
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    id: 'lu-forge2',
                    session_id: 'sess-forge2',
                    phase: 'absorbing',
                    alignment_state: 'idle',
                    objective_status: 'working',
                    objective: { text: '理解 attention' },
                    forge_stage: 'collision',
                    temperature_state: 'steady',
                    learning_action: 'prepare_to_guess',
                }),
            };
        }
        throw new Error(`Unexpected request: ${opts.method} ${url}`);
    });

    sandbox.window.__learningUnitUI.state.sessionId = 'sess-forge2';
    sandbox.window.__learningUnitUI.state.unitId = 'lu-forge2';
    await sandbox.window.__learningUnitUI.refreshUnit();

    const card = sandbox.document.getElementById('learning-unit-card');
    assert.match(card.innerHTML, /碰撞/);
    assert.match(card.innerHTML, /lu-learning-action/);
    assert.match(card.innerHTML, /准备试答/);
    assert.equal(sandbox.window.__learningUnitUI.state.forgeStage, 'collision');
    assert.equal(sandbox.window.__learningUnitUI.state.learningAction, 'prepare_to_guess');
});

test('renderChatMarkdown 使用 markdown-it 渲染表格与 br 换行', () => {
    const { renderChatMarkdown } = loadAppForTest();
    const html = renderChatMarkdown([
        '黑盒测试——只看输赢，不问招式<br>把 Agent 当成一个密不透风的铁箱子。',
        '',
        '| 类型 | 观测重点 |',
        '|------|----------|',
        '| 黑盒 | 输入输出对、任务成功率、用户满意度 |',
    ].join('\n'));

    assert.match(html, /<p>黑盒测试——只看输赢，不问招式<br>\n?把 Agent 当成一个密不透风的铁箱子。<\/p>/);
    assert.match(html, /<table>/);
    assert.match(html, /<thead>/);
    assert.match(html, /<tbody>/);
    assert.match(html, /<th>类型<\/th>/);
    assert.match(html, /<td>黑盒<\/td>/);
    assert.doesNotMatch(html, /&lt;br&gt;/);
});

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP_PATH = path.join(__dirname, '..', 'web', 'static', 'app.js');
const MARKDOWN_IT_PATH = path.join(__dirname, '..', 'web', 'static', 'vendor', 'markdown-it.min.js');
const APP_SOURCE = fs.readFileSync(APP_PATH, 'utf8');
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

    addEventListener() {}
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
    formatUsageNumber,
    formatUsagePercent,
    els,
};
`;

    vm.runInNewContext(APP_SOURCE_BEFORE_BINDINGS + exportCode, sandbox, { filename: APP_PATH });
    return sandbox.module.exports;
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

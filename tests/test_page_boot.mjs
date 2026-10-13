// Boot the WriterSystem page script in a stub DOM.
// Catches startup ReferenceErrors (missing helpers) that leave the file tree stuck on "Loading".
//
//   node tests/test_page_boot.mjs
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

const root = new URL('../', import.meta.url);
const html = await readFile(new URL('app/templates/index.html', root), 'utf8');

const REQUIRED_IDS = [
    'file-tree-container',
    'file-sidebar',
    'vault-select',
    'editor-textarea',
    'filename-display',
    'tab-editor',
    'chat-input',
    'guppy-input',
    'todo-new-input',
    'find-query',
    'secure-secret',
];

for (const id of REQUIRED_IDS) {
    assert.match(html, new RegExp(`id="${id}"`));
}
assert.match(html, /id="editor-textarea" inputmode="text"/);

function tokenClass(selector) {
    if (selector.startsWith('#')) return { type: 'id', value: selector.slice(1) };
    if (selector.startsWith('.')) return { type: 'class', value: selector.slice(1) };
    return { type: 'tag', value: selector.toLowerCase() };
}

function createElement(tag, attrs = {}) {
    const classes = new Set(String(attrs.class || '').split(/\s+/).filter(Boolean));
    const dataset = {};
    const listeners = {};
    const style = {
        setProperty(name, value) { this[name] = value; },
        height: 'auto',
        overflowY: 'hidden',
    };
    const children = [];
    const el = {
        tagName: tag.toUpperCase(),
        id: attrs.id || '',
        type: attrs.type || '',
        name: attrs.name || '',
        value: attrs.value || '',
        disabled: Object.hasOwn(attrs, 'disabled'),
        checked: Object.hasOwn(attrs, 'checked'),
        hidden: classes.has('hidden'),
        placeholder: attrs.placeholder || '',
        title: attrs.title || '',
        textContent: '',
        innerHTML: '',
        parentElement: null,
        scrollHeight: 24,
        clientWidth: 240,
        offsetHeight: 48,
        style,
        dataset,
        children,
        classList: {
            add(...names) { names.forEach((name) => classes.add(name)); },
            remove(...names) { names.forEach((name) => classes.delete(name)); },
            contains(name) { return classes.has(name); },
            toggle(name, force) {
                const on = force === undefined ? !classes.has(name) : !!force;
                if (on) classes.add(name);
                else classes.delete(name);
                return on;
            },
        },
        addEventListener(name, fn) {
            (listeners[name] ||= []).push(fn);
        },
        removeEventListener(name, fn) {
            listeners[name] = (listeners[name] || []).filter((item) => item !== fn);
        },
        dispatchEvent(event) {
            (listeners[event.type] || []).forEach((fn) => fn(event));
            return true;
        },
        setAttribute(name, value) {
            if (name === 'class') {
                classes.clear();
                String(value).split(/\s+/).filter(Boolean).forEach((item) => classes.add(item));
            } else if (name.startsWith('data-')) {
                dataset[name.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] = String(value);
            }
        },
        getAttribute(name) {
            if (name === 'class') return [...classes].join(' ');
            if (name === 'id') return el.id;
            return null;
        },
        appendChild(child) {
            child.parentElement = el;
            children.push(child);
            return child;
        },
        focus() { document.activeElement = el; },
        blur() {},
        select() {},
        setSelectionRange() {},
        getBoundingClientRect() {
            return { left: 0, right: 100, top: 0, bottom: 20, width: 100, height: 20 };
        },
        querySelector(selector) {
            return el.querySelectorAll(selector)[0] || null;
        },
        querySelectorAll(selector) {
            return queryAll(children, selector);
        },
        closest() { return null; },
        matches(selector) {
            const parts = selector.split(',').map((part) => part.trim());
            return parts.some((part) => {
                const token = tokenClass(part.split(/\s+/).pop());
                if (token.type === 'id') return el.id === token.value;
                if (token.type === 'class') return classes.has(token.value);
                return el.tagName === token.value.toUpperCase();
            });
        },
    };
    Object.keys(attrs).forEach((key) => {
        if (key.startsWith('data-')) {
            dataset[key.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] = attrs[key];
        }
    });
    Object.defineProperty(el, 'innerHTML', {
        get() { return this._html || ''; },
        set(value) { this._html = String(value); },
    });
    return el;
}

const elementsById = new Map();
const allElements = [];
for (const match of html.matchAll(/<([a-zA-Z0-9]+)([^>]*)>/g)) {
    const tag = match[1];
    const raw = match[2];
    const attrs = {};
    for (const attr of raw.matchAll(/([:@]?[a-zA-Z0-9:-]+)(?:=(?:"([^"]*)"|'([^']*)'))?/g)) {
        attrs[attr[1]] = attr[2] ?? attr[3] ?? '';
    }
    const el = createElement(tag, attrs);
    allElements.push(el);
    if (el.id) elementsById.set(el.id, el);
}

function queryAll(pool, selector) {
    const groups = selector.split(',').map((part) => part.trim());
    const found = [];
    groups.forEach((group) => {
        const token = tokenClass(group.split(/\s+/).pop());
        pool.forEach((el) => {
            if (token.type === 'id' && el.id === token.value) found.push(el);
            if (token.type === 'class' && el.classList.contains(token.value)) found.push(el);
            if (token.type === 'tag' && el.tagName === token.value.toUpperCase()) found.push(el);
        });
    });
    return found;
}

const listeners = {};
const store = {};
const fetches = [];
const documentElement = createElement('html', { lang: 'en' });
const body = createElement('body', { class: 'bg-gray-900' });
const document = {
    documentElement,
    body,
    visibilityState: 'visible',
    activeElement: null,
    getElementById(id) { return elementsById.get(id) || null; },
    querySelector(selector) { return document.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector) { return queryAll(allElements, selector); },
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    dispatchEvent(event) {
        (listeners[event.type] || []).forEach((fn) => fn(event));
        return true;
    },
    createElement(tag) { return createElement(tag); },
};

function jsonResponse(data) {
    return {
        ok: true,
        status: 200,
        json: async () => data,
        text: async () => JSON.stringify(data),
        headers: { get: () => null },
    };
}

async function fakeFetch(url) {
    const href = String(url);
    fetches.push(href);
    if (href.includes('/api/vaults')) return jsonResponse({ vaults: ['books'] });
    if (href.includes('/api/files')) {
        return jsonResponse({
            files: ['chapter.md', 'notes/scene.md'],
            folders: ['', 'notes'],
            mtimes: { 'chapter.md': 1, notes: 1, 'notes/scene.md': 1 },
        });
    }
    if (href.includes('/api/chat/history')) return jsonResponse({ history: [] });
    if (href.includes('/api/prompt-files')) {
        return jsonResponse({ vault: 'books', selected: [], available: [] });
    }
    if (href.includes('/api/todos')) return jsonResponse({ items: [] });
    return { ok: false, status: 404, json: async () => ({}), text: async () => '' };
}

const window = {
    document,
    localStorage: {
        getItem(key) { return Object.hasOwn(store, key) ? store[key] : null; },
        setItem(key, value) { store[key] = String(value); },
        removeItem(key) { delete store[key]; },
    },
    matchMedia() {
        return { matches: false, addEventListener() {}, removeEventListener() {} };
    },
    addEventListener(name, fn) { document.addEventListener(name, fn); },
    fetch: fakeFetch,
    crypto: webcrypto,
    navigator: { userAgent: 'NodeTest', maxTouchPoints: 0, onLine: true },
    screen: { width: 1024, height: 768 },
    location: { href: 'http://127.0.0.1:8000/', pathname: '/' },
    innerWidth: 1024,
    devicePixelRatio: 1,
    requestAnimationFrame(fn) { return setTimeout(fn, 0); },
    ResizeObserver: class { observe() {} disconnect() {} },
    marked: { parse: (text) => text },
    DOMPurify: { sanitize: (text) => text },
    console,
    Date,
    Set,
    Map,
    WeakMap,
    JSON,
    Math,
    Number,
    String,
    Boolean,
    Array,
    Object,
    Promise,
    Error,
    TypeError,
    Uint8Array,
    AbortSignal,
    TextEncoder,
    TextDecoder,
    btoa,
    atob,
    URL,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
};
window.window = window;
window.document = document;
window.globalThis = window;

const context = vm.createContext(window);
const scripts = [
    'app/static/editor-tools.js',
    'app/static/file-batch.js',
    'app/static/composer-keys.js',
    'app/static/chat-send.js',
    'app/static/vault-crypto.js',
];
for (const path of scripts) {
    vm.runInContext(
        await readFile(new URL(path, root), 'utf8'),
        context,
        { filename: path },
    );
}

const inline = [...html.matchAll(/<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)<\/script>/g)]
    .filter((match) => !/type="(?:importmap|module)"/.test(match[1]) && match[2].trim())
    .map((match) => match[2]);
assert.equal(inline.length, 1);

vm.runInContext(inline[0], context, { filename: 'index.html' });
window.document.dispatchEvent({ type: 'DOMContentLoaded', persisted: false });

for (let i = 0; i < 20; i += 1) {
    if (fetches.some((url) => url.includes('/api/files'))) break;
    await new Promise((resolve) => setImmediate(resolve));
}

assert.ok(fetches.some((url) => url.includes('/api/vaults')), 'startup must load vaults');
assert.ok(fetches.some((url) => url.includes('/api/files')), 'startup must load the file tree');
assert.match(elementsById.get('file-tree-container').innerHTML, /chapter\.md/);
assert.match(elementsById.get('file-tree-container').innerHTML, /scene\.md/);
assert.doesNotMatch(elementsById.get('file-tree-container').innerHTML, /Loading file tree/);
assert.equal(elementsById.get('vault-select').innerHTML.includes('books'), true);
assert.equal(typeof window.loadFiles, 'function');
assert.equal(typeof window.renderTreeHTML, 'function');
assert.equal(typeof window.loadVaults, 'function');
assert.equal(typeof window.bindFieldIsolation, 'undefined');
assert.equal(elementsById.get('editor-textarea').getAttribute('id'), 'editor-textarea');

console.log('page boot components loaded');
if (window.secureIdleTimer) clearInterval(window.secureIdleTimer);
process.exit(0);

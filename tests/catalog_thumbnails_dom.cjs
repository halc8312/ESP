// Execute the shipped asset against a small DOM and virtual clock: no browser,
// network, dependencies, or test-only exports in the application are required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname, '../static/js/catalog_requests.js'), 'utf8');
const camel = name => name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());

class Element {
    constructor(tag = 'div', className = '') {
        this.tagName = tag.toUpperCase();
        this.className = className;
        this.children = [];
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this.events = new Map();
        this.textContent = '';
        this.value = '';
        this.hidden = false;
        this.disabled = false;
        this.isConnected = true;
        this.rect = {top: 10, bottom: 110, left: 10, right: 110};
        this.firstChild = {textContent: ''};
        this.classList = {
            contains: name => this.className.split(' ').includes(name),
            add: name => { this.className = [...new Set([...this.className.split(' '), name])].join(' '); },
            remove: name => { this.className = this.className.split(' ').filter(value => value !== name).join(' '); },
            toggle: (name, force) => {
                const enabled = force === undefined ? !this.classList.contains(name) : force;
                this.classList[enabled ? 'add' : 'remove'](name);
            }
        };
    }
    set innerHTML(_value) { throw new Error('HTML injection is not allowed in this DOM'); }
    appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
    append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    replaceWith(node) {
        if (!this.parentNode) return;
        const index = this.parentNode.children.indexOf(this);
        if (index >= 0) this.parentNode.children.splice(index, 1, node);
        node.parentNode = this.parentNode;
        this.parentNode = null;
    }
    remove() {
        if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(node => node !== this);
        this.parentNode = null;
    }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name.startsWith('data-')) this.dataset[camel(name.slice(5))] = String(value);
    }
    removeAttribute(name) {
        delete this.attributes[name];
        if (name.startsWith('data-')) delete this.dataset[camel(name.slice(5))];
    }
    addEventListener(name, callback) {
        if (!this.events.has(name)) this.events.set(name, []);
        this.events.get(name).push(callback);
    }
    dispatch(name) { (this.events.get(name) || []).forEach(callback => callback({preventDefault() {}})); }
    getBoundingClientRect() { return this.rect; }
    focus() {}
    closest() { return null; }
    showModal() { this.open = true; }
    close() { this.open = false; this.dispatch('close'); }
    matches(selector) {
        const className = selector.match(/\.([\w-]+)/);
        if (className && !this.classList.contains(className[1])) return false;
        const tag = selector.match(/^[a-z]+/i);
        if (tag && this.tagName !== tag[0].toUpperCase()) return false;
        for (const match of selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
            const value = match[1].startsWith('data-') ? this.dataset[camel(match[1].slice(5))] : this.attributes[match[1]];
            if (value === undefined || (match[2] !== undefined && value !== match[2])) return false;
        }
        return !selector.includes(':not(:disabled)') || !this.disabled;
    }
    querySelectorAll(selector) {
        return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

async function flush() { for (let index = 0; index < 15; index += 1) await Promise.resolve(); }

class Clock {
    now = 0;
    nextId = 0;
    timers = new Map();
    setTimeout = (callback, delay) => {
        const id = ++this.nextId;
        this.timers.set(id, {at: this.now + delay, callback});
        return id;
    };
    clearTimeout = id => this.timers.delete(id);
    async advance(milliseconds) {
        const target = this.now + milliseconds;
        let steps = 0;
        while (true) {
            const next = [...this.timers].filter(([, timer]) => timer.at <= target)
                .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
            if (!next) break;
            assert.ok(++steps < 1000, 'bounded timer work');
            this.now = next[1].at;
            this.timers.delete(next[0]);
            next[1].callback();
            await flush();
        }
        this.now = target;
        await flush();
    }
}

const idsIn = url => new URL(url, 'https://catalog.example').searchParams.get('product_ids').split(',').map(Number);
const response = (data, status = 200) => ({ok: status === 200, status, headers: {get: () => null}, json: async () => data});
const pendingResponse = url => response({items: idsIn(url).map(product_id => ({product_id, status: 'pending', thumb_url: ''}))});

function setup({count = 1, thumbnailUrl = '/catalog/public-token/thumbnails', responder = pendingResponse, existingImage = false} = {}) {
    const clock = new Clock();
    const body = new Element('body');
    const controls = new Map();
    for (const match of source.matchAll(/byId\('([^']+)'\)/g)) {
        if (!controls.has(match[1])) { const node = new Element(); controls.set(match[1], node); body.appendChild(node); }
    }
    const cards = [];
    const configItems = [];
    for (let productId = 1; productId <= count; productId += 1) {
        const card = new Element('article', 'product-card');
        card.dataset.productId = String(productId);
        const media = new Element('div', 'product-card-media');
        const placeholder = new Element(existingImage ? 'img' : 'div', existingImage ? 'product-card-image' : 'product-card-image-placeholder');
        if (existingImage) placeholder.src = '/media/existing.png';
        media.appendChild(placeholder);
        const price = new Element('div', 'product-card-price'); price.textContent = '¥1,200';
        const stock = new Element('span', 'stock-badge'); stock.textContent = 'Stock not checked';
        const button = new Element('button'); button.dataset.requestProductId = String(productId);
        card.append(media, price, stock, button);
        body.appendChild(card);
        cards.push(card);
        configItems.push({product_id: productId, title: '<private markup>', price: 1200, stock: 0,
            in_stock: false, detail_status: 'pending', thumb_url: '', image_urls: []});
    }
    const config = {token: 'public-token', thumbnail_url: thumbnailUrl, items: configItems,
        details_url: '/catalog/public-token/products/0/details', submit_url: '/catalog/public-token/requests', csrf_token: 'csrf'};
    const configNode = new Element(); configNode.textContent = JSON.stringify(config);
    controls.set('catalogRequestConfig', configNode);
    const document = {
        body, hidden: false, documentElement: {clientHeight: 800, clientWidth: 1200},
        getElementById: id => controls.get(id) || null,
        createElement: tag => new Element(tag),
        querySelectorAll: selector => body.querySelectorAll(selector),
        querySelector: selector => body.querySelector(selector)
    };
    const events = new Map();
    const storage = new Map();
    const window = {
        location: {origin: 'https://catalog.example'}, innerHeight: 800, innerWidth: 1200,
        getComputedStyle: card => ({display: card.computedDisplay || card.style.display || 'block', visibility: 'visible'}),
        setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, crypto: webcrypto,
        sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
        addEventListener: (name, callback) => events.set(name, callback),
        removeEventListener: name => events.delete(name)
    };
    const calls = [];
    const cache = {1: {product_id: 1, price: 1200, stock: 0, detail_status: 'pending', description: 'Retained details', image_urls: []}};
    class FakeDate extends Date { static now() { return clock.now; } }
    const context = vm.createContext({document, window, URL, AbortController, Date: FakeDate, Uint8Array,
        DETAIL_CACHE: cache, fetch: (url, options) => {
            calls.push({url, options, at: clock.now});
            return Promise.resolve().then(() => responder(url, options));
        }});
    vm.runInContext(source, context, {filename: 'catalog_requests.js'});
    return {clock, cards, controls, calls, cache, document, events, storage};
}

test('no endpoint or no missing cards means no polling', async () => {
    for (const options of [{thumbnailUrl: undefined}, {thumbnailUrl: null}, {existingImage: true}]) {
        // Explicit null is the JSON representation of a disabled endpoint.
        const page = setup({...options, thumbnailUrl: options.thumbnailUrl === undefined && !options.existingImage ? null : options.thumbnailUrl});
        await page.clock.advance(600000);
        assert.equal(page.calls.length, 0);
        assert.equal(page.clock.timers.size, 0);
    }
});

test('only visible missing cards are read; scrolling and filtering are rechecked', async () => {
    const page = setup({count: 4});
    page.cards[1].style.display = 'none';
    page.cards[2].computedDisplay = 'none';
    page.cards[3].rect = {top: 1000, bottom: 1100, left: 10, right: 110};
    await page.clock.advance(5000);
    assert.deepEqual(idsIn(page.calls[0].url), [1]);
    page.cards[0].hidden = true;
    page.cards[1].style.display = '';
    page.cards[3].rect = {top: 10, bottom: 110, left: 10, right: 110};
    await page.clock.advance(5000);
    assert.deepEqual(idsIn(page.calls[1].url), [2, 4]);
    page.document.hidden = true;
    await page.clock.advance(300000);
    assert.equal(page.calls.length, 2);
});

test('one batch contains at most 50 IDs and rotates beyond the first pending batch', async () => {
    const page = setup({count: 60});
    await page.clock.advance(10000);
    assert.equal(page.calls.length, 2);
    assert.ok(page.calls.every(call => idsIn(call.url).length === 50));
    assert.deepEqual(idsIn(page.calls[1].url).slice(0, 10), [51, 52, 53, 54, 55, 56, 57, 58, 59, 60]);
    assert.equal(new Set(page.calls.flatMap(call => idsIn(call.url))).size, 60);
});

test('pending polling ends within five minutes and never POSTs or overlaps intervals', async () => {
    const page = setup();
    await page.clock.advance(600000);
    assert.equal(page.calls.length, 59);
    assert.ok(page.calls.every(call => call.options.method === 'GET' && call.at < 300000));
    assert.ok(page.calls.slice(1).every((call, index) => call.at - page.calls[index].at >= 5000));
    assert.equal(page.clock.timers.size, 0);
});

for (const status of [401, 403, 404, 429, 500]) {
    test(`HTTP ${status} stops further thumbnail requests`, async () => {
        const page = setup({responder: () => response({}, status)});
        await page.clock.advance(600000);
        assert.equal(page.calls.length, 1);
        assert.ok(page.cards[0].querySelector('.product-card-image-placeholder'));
    });
}

for (const failure of ['network', 'json', 'shape', 'foreign', 'duplicate', 'status']) {
    test(`${failure} response fails closed without retrying`, async () => {
        const page = setup({responder: () => {
            if (failure === 'network') throw new Error('disconnected');
            if (failure === 'json') return {ok: true, json: async () => { throw new Error('invalid JSON'); }};
            if (failure === 'shape') return response({});
            const row = {product_id: failure === 'foreign' ? 999 : 1, status: failure === 'status' ? 'complete' : 'ready', thumb_url: '/media/ready.png'};
            return response({items: failure === 'duplicate' ? [row, row] : [row]});
        }});
        await page.clock.advance(600000);
        assert.equal(page.calls.length, 1);
        assert.equal(page.cards[0].querySelector('.product-card-image'), null);
    });
}

test('ready image replaces the placeholder while prices, stock, and detail state stay unchanged', async () => {
    const page = setup({responder: () => response({items: [{product_id: 1, status: 'ready', thumb_url: '/media/product-delivery/thumbnail/1/claim/0.png', price: 1, stock: 99, in_stock: true, detail_status: 'complete'}]})});
    await page.clock.advance(600000);
    const image = page.cards[0].querySelector('.product-card-image');
    assert.equal(image.src, '/media/product-delivery/thumbnail/1/claim/0.png');
    assert.equal(image.alt, '<private markup>');
    assert.equal(page.cards[0].querySelector('.product-card-image-placeholder'), null);
    assert.equal(page.cards[0].querySelector('.product-card-price').textContent, '¥1,200');
    assert.equal(page.cards[0].querySelector('.stock-badge').textContent, 'Stock not checked');
    assert.equal(page.cards[0].querySelector('[data-request-product-id]').textContent, 'Check availability');
    assert.equal(page.cache[1].price, 1200);
    assert.equal(page.cache[1].detail_status, 'pending');
    assert.equal(page.cache[1].description, 'Retained details');
    assert.equal(page.cache[1].image_urls[0], image.src);
    assert.equal(page.calls.length, 1);
});

test('an unavailable thumbnail is terminal without changing the availability button', async () => {
    const page = setup({responder: () => response({items: [{product_id: 1, status: 'unavailable', thumb_url: ''}]})});
    await page.clock.advance(600000);
    assert.equal(page.calls.length, 1);
    assert.equal(page.cards[0].querySelector('[data-request-product-id]').textContent, 'Check availability');
});

for (const url of ['https://supplier.example/private.jpg', '//supplier.example/a.png', '/media/../private.png', '/media/%2e%2e/private.png', '/media/image.png?source=private', '/media/image.png#private', '/media/\\supplier/a.png', 'data:image/png;base64,AA']) {
    test(`unsafe thumbnail URL is not assigned: ${url}`, async () => {
        const page = setup({responder: () => response({items: [{product_id: 1, status: 'ready', thumb_url: url}]})});
        await page.clock.advance(600000);
        assert.equal(page.cards[0].querySelector('.product-card-image'), null);
        assert.equal(page.calls.length, 1);
    });
}

test('a timeout aborts the single outstanding GET and stops polling', async () => {
    const page = setup({responder: (_url, options) => new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    })});
    await page.clock.advance(19999);
    assert.equal(page.calls.length, 1);
    assert.equal(page.calls[0].options.signal.aborted, false);
    await page.clock.advance(1);
    assert.equal(page.calls[0].options.signal.aborted, true);
    await page.clock.advance(600000);
    assert.equal(page.calls.length, 1);
});

test('pagehide cancels polling and ignores a late response', async () => {
    let resolve;
    const page = setup({responder: () => new Promise(done => { resolve = done; })});
    await page.clock.advance(5000);
    page.events.get('pagehide')();
    assert.equal(page.calls[0].options.signal.aborted, true);
    resolve(response({items: [{product_id: 1, status: 'ready', thumb_url: '/media/late.png'}]}));
    await flush();
    await page.clock.advance(600000);
    assert.equal(page.cards[0].querySelector('.product-card-image'), null);
    assert.equal(page.calls.length, 1);
});

test('the global deadline also stops a transport that ignores abort', async () => {
    let resolve;
    const page = setup({responder: () => new Promise(done => { resolve = done; })});
    await page.clock.advance(300000);
    assert.equal(page.calls.length, 1);
    assert.equal(page.calls[0].options.signal.aborted, true);
    resolve(response({items: [{product_id: 1, status: 'ready', thumb_url: '/media/late.png'}]}));
    await flush();
    await page.clock.advance(300000);
    assert.equal(page.cards[0].querySelector('.product-card-image'), null);
    assert.equal(page.clock.timers.size, 0);
});

test('a late thumbnail preserves a newer explicit availability result and its request price', async () => {
    let resolveThumbnail;
    const page = setup({responder: (_url, options) => {
        if (options.method === 'GET') return new Promise(done => { resolveThumbnail = done; });
        return response({status: 'ready', item: {product_id: 1, title: 'Verified item', price: 1500, stock: 2,
            in_stock: true, detail_status: 'complete', image_urls: []}});
    }});
    await page.clock.advance(5000);
    const button = page.cards[0].querySelector('[data-request-product-id]');
    button.dispatch('click');
    await flush();
    assert.equal(button.textContent, 'Add');
    resolveThumbnail(response({items: [{product_id: 1, status: 'ready', thumb_url: '/media/ready.png', price: 1, stock: 0}]}));
    await flush();
    button.dispatch('click');
    await flush();
    assert.equal(button.textContent, 'Added (1)');
    const saved = JSON.parse(page.storage.get('esp.catalog.request.public-token'));
    assert.equal(saved.items[0].expected_price_jpy, 1500);
    assert.equal(page.calls.filter(call => call.options.method === 'POST').length, 1);
});

test('a missing image file restores the placeholder without repeated GETs', async () => {
    const page = setup({responder: () => response({items: [{product_id: 1, status: 'ready', thumb_url: '/media/ready.png'}]})});
    await page.clock.advance(5000);
    page.cards[0].querySelector('.product-card-image').dispatch('error');
    assert.ok(page.cards[0].querySelector('.product-card-image-placeholder'));
    await page.clock.advance(600000);
    assert.equal(page.calls.length, 1);
});

test('an external or unrelated endpoint cannot receive catalog IDs', async () => {
    for (const thumbnailUrl of ['https://other.example/catalog/token/thumbnails', '/catalog/token/requests']) {
        const page = setup({thumbnailUrl});
        await page.clock.advance(600000);
        assert.equal(page.calls.length, 0);
    }
});

test('thumbnail refresh never performs availability checks; an explicit check still needs a second Add click', async () => {
    const page = setup({responder: (url, options) => {
        if (options.method === 'GET') return response({items: [{product_id: 1, status: 'ready', thumb_url: '/media/ready.png'}]});
        return response({status: 'ready', item: {product_id: 1, title: 'Verified item', price: 1500, stock: 2, in_stock: true, detail_status: 'complete', image_urls: ['/media/ready.png']}});
    }});
    await page.clock.advance(5000);
    assert.equal(page.calls.length, 1);
    assert.equal(page.calls[0].options.method, 'GET');
    const button = page.cards[0].querySelector('[data-request-product-id]');
    button.dispatch('click');
    await flush();
    assert.equal(page.calls.filter(call => call.options.method === 'POST').length, 1);
    assert.match(page.calls[1].url, /products\/1\/details$/);
    assert.equal(button.textContent, 'Add');
    assert.equal(page.controls.get('catalogRequestCount').textContent, '(0)');
    button.dispatch('click');
    await flush();
    assert.equal(button.textContent, 'Added (1)');
    assert.equal(page.controls.get('catalogRequestCount').textContent, '(1)');
    assert.equal(page.calls.length, 2);
});

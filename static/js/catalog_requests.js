(function () {
    'use strict';

    const configNode = document.getElementById('catalogRequestConfig');
    if (!configNode) return;
    const config = JSON.parse(configNode.textContent);
    const byId = id => document.getElementById(id);
    const dialog = byId('catalogRequestDialog');
    const form = byId('catalogRequestForm');
    const fields = byId('catalogRequestFields');
    const list = byId('catalogRequestItems');
    const submitButton = byId('catalogRequestSubmit');
    const reconfirm = byId('catalogRequestReconfirm');
    const instagram = byId('catalogRequestInstagram');
    const buyerName = byId('catalogRequestName');
    const message = byId('catalogRequestMessage');
    const storageKey = 'esp.catalog.request.' + config.token;
    const maxItems = 50;
    const items = new Map(config.items.map(item => [item.product_id, item]));
    let selection = [];
    let submissionKey = '';
    let attemptedFingerprint = null;
    let submitting = false;
    let needsReconfirm = false;
    let successReference = '';
    let lastFocused = null;
    const unavailableIds = new Set();
    const detailChecks = new Map();
    const detailPollDuration = 120000;

    function unchecked(item) {
        return item && (item.detail_status === 'none' || item.detail_status === 'pending');
    }

    function createKey() {
        if (typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
        const bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    }

    function validPrice(price) {
        return price === null || (Number.isSafeInteger(price) && price >= 0);
    }

    function itemTitle(item, id) {
        return item ? (item.title_en || item.title || 'Item #' + id) : 'Item #' + id;
    }

    function itemLimit(item) {
        return item && item.in_stock ? Math.max(0, Math.min(99, Number(item.stock) || 0)) : 0;
    }

    function available(entry) {
        return !unavailableIds.has(entry.product_id) && itemLimit(items.get(entry.product_id)) > 0;
    }

    function priceLabel(value) {
        return value === null ? 'Price on request' : '¥' + value.toLocaleString('en-US');
    }

    function saveSelection() {
        try {
            if (!selection.length) {
                window.sessionStorage.removeItem(storageKey);
            } else {
                // Customer contact details and messages stay only in the form.
                window.sessionStorage.setItem(storageKey, JSON.stringify({
                    items: selection,
                    submission_key: submissionKey
                }));
            }
        } catch (_error) {
            // The request still works when browser storage is disabled or full.
        }
    }

    function restoreSelection() {
        try {
            const stored = JSON.parse(window.sessionStorage.getItem(storageKey) || 'null');
            if (!stored || !Array.isArray(stored.items)) return;
            const seen = new Set();
            stored.items.slice(0, maxItems).forEach(entry => {
                if (!entry || !Number.isSafeInteger(entry.product_id) || entry.product_id <= 0
                    || seen.has(entry.product_id) || !Number.isInteger(entry.quantity)
                    || entry.quantity < 1 || entry.quantity > 99 || !validPrice(entry.expected_price_jpy)) return;
                seen.add(entry.product_id);
                const item = items.get(entry.product_id);
                const restored = {
                    product_id: entry.product_id,
                    quantity: entry.quantity,
                    expected_price_jpy: entry.expected_price_jpy
                };
                if (!item || !item.in_stock || entry.quantity > itemLimit(item)) needsReconfirm = true;
                if (item && restored.expected_price_jpy !== item.price) {
                    restored.expected_price_jpy = item.price;
                    needsReconfirm = true;
                }
                selection.push(restored);
            });
            if (typeof stored.submission_key === 'string'
                && /^(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i.test(stored.submission_key)) {
                submissionKey = stored.submission_key;
            }
        } catch (_error) {
            // Ignore damaged data, without preventing an in-memory request.
        }
    }

    function announce(text) {
        byId('catalogRequestAnnounce').textContent = text;
    }

    function feedback(text) {
        const node = byId('catalogRequestFeedback');
        node.textContent = text;
        node.hidden = !text;
    }

    function resetAttempt() {
        if (attemptedFingerprint !== null) {
            submissionKey = createKey();
            attemptedFingerprint = null;
        }
        saveSelection();
    }

    function requireReconfirmation() {
        needsReconfirm = true;
        reconfirm.checked = false;
        reconfirm.required = true;
        reconfirm.disabled = false;
        byId('catalogRequestReconfirmLabel').hidden = false;
    }

    function clearReconfirmation() {
        needsReconfirm = false;
        reconfirm.checked = false;
        reconfirm.required = false;
        reconfirm.disabled = true;
        byId('catalogRequestReconfirmLabel').hidden = true;
    }

    function refreshControls() {
        const hasInvalidItem = selection.some(entry => !available(entry)
            || entry.quantity > itemLimit(items.get(entry.product_id)));
        const count = selection.reduce((total, entry) => total + entry.quantity, 0);
        const openButton = byId('catalogRequestOpen');
        byId('catalogRequestBar').hidden = !selection.length && !successReference;
        document.body.classList.toggle('has-catalog-request', Boolean(selection.length || successReference));
        openButton.firstChild.textContent = successReference ? 'View confirmation ' : 'Review request ';
        byId('catalogRequestCount').textContent = successReference ? '' : '(' + count + ')';
        byId('catalogRequestEmpty').hidden = selection.length > 0;
        byId('catalogRequestClear').disabled = submitting || !selection.length;
        submitButton.disabled = submitting || !selection.length || hasInvalidItem || (needsReconfirm && !reconfirm.checked);
        submitButton.textContent = submitting ? 'Sending…' : 'Send request';
        document.querySelectorAll('[data-request-product-id]').forEach(button => {
            const id = Number(button.dataset.requestProductId);
            const item = items.get(id);
            const entry = selection.find(selected => selected.product_id === id);
            const isAvailable = !unavailableIds.has(id) && itemLimit(item) > 0;
            const atLimit = !entry && selection.length >= maxItems;
            const detailCheck = detailChecks.get(id);
            if (unchecked(item)) {
                const waiting = Boolean(detailCheck && detailCheck.retryAt > Date.now());
                button.disabled = submitting || Boolean(detailCheck && detailCheck.active) || waiting;
                button.classList.remove('is-added');
                button.textContent = detailCheck && detailCheck.active ? 'Checking…'
                    : waiting ? 'Check again later' : detailCheck ? 'Check again' : 'Check availability';
                button.setAttribute('aria-label', 'Check availability of ' + itemTitle(item, id));
                return;
            }
            button.disabled = submitting || !isAvailable || atLimit;
            button.classList.toggle('is-added', Boolean(entry));
            button.textContent = !isAvailable ? 'Unavailable' : entry ? 'Added (' + entry.quantity + ')' : atLimit ? 'Limit reached' : 'Add';
            button.setAttribute('aria-label', !isAvailable
                ? itemTitle(item, id) + ' is unavailable'
                : entry ? 'Review ' + itemTitle(item, id) + ' in your request'
                : atLimit ? 'You can select up to 50 different items'
                : 'Add ' + itemTitle(item, id) + ' to your request');
        });
    }

    function refreshSubtotal() {
        let subtotal = 0;
        let hasUnknownPrice = false;
        selection.forEach(entry => {
            if (entry.expected_price_jpy === null) hasUnknownPrice = true;
            else subtotal += entry.expected_price_jpy * entry.quantity;
        });
        byId('catalogRequestSubtotalLabel').textContent = hasUnknownPrice
            ? 'Known-price items subtotal (JPY)' : 'Reference subtotal (JPY)';
        byId('catalogRequestSubtotal').textContent = priceLabel(subtotal) + (hasUnknownPrice ? ' + prices on request' : '');
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function renderSelection() {
        if (!selection.length) {
            clearReconfirmation();
            feedback('');
        }
        list.replaceChildren();
        selection.forEach(entry => {
            const item = items.get(entry.product_id);
            const title = itemTitle(item, entry.product_id);
            const row = element('li', 'catalog-request-item');
            const details = element('div', 'catalog-request-item-details');
            details.appendChild(element('strong', 'catalog-request-item-title', title));
            details.appendChild(element('span', 'catalog-request-item-price', priceLabel(entry.expected_price_jpy) + ' / item'));
            const problem = !available(entry) ? 'This item is no longer available. Please remove it to continue.'
                : entry.quantity > itemLimit(item) ? 'Only ' + itemLimit(item) + ' available. Please reduce the quantity.' : '';
            if (problem) {
                row.classList.add('has-issue');
                details.appendChild(element('p', 'catalog-request-item-issue', problem));
            }
            const controls = element('div', 'catalog-request-item-controls');
            const label = element('label', '', 'Qty');
            const quantity = element('input', 'catalog-request-quantity');
            quantity.type = 'number';
            quantity.min = '1';
            quantity.max = String(Math.max(1, itemLimit(item)));
            quantity.step = '1';
            quantity.required = true;
            quantity.value = String(entry.quantity);
            quantity.disabled = !available(entry);
            quantity.id = 'catalogRequestQuantity' + entry.product_id;
            label.htmlFor = quantity.id;
            quantity.setAttribute('aria-label', 'Quantity for ' + title);
            quantity.addEventListener('input', function () {
                if (!quantity.validity.valid) return;
                entry.quantity = quantity.valueAsNumber;
                resetAttempt();
                if (needsReconfirm) reconfirm.checked = false;
                // Keep the active input in place while the customer types.
                const issue = details.querySelector('.catalog-request-item-issue');
                if (issue && available(entry) && entry.quantity <= itemLimit(item)) {
                    issue.remove();
                    row.classList.remove('has-issue');
                }
                refreshSubtotal();
                refreshControls();
            });
            const remove = element('button', 'catalog-request-text-btn', 'Remove');
            remove.type = 'button';
            remove.setAttribute('aria-label', 'Remove ' + title + ' from your request');
            remove.addEventListener('click', function () {
                selection = selection.filter(selected => selected.product_id !== entry.product_id);
                resetAttempt();
                if (needsReconfirm) reconfirm.checked = false;
                renderSelection();
                const nextInput = list.querySelector('input:not(:disabled)');
                (nextInput || instagram).focus();
                announce('Item removed from your request.');
            });
            controls.append(label, quantity, remove);
            row.append(details, controls);
            list.appendChild(row);
        });
        refreshSubtotal();
        refreshControls();
    }

    function openDialog() {
        if (dialog.open) return;
        lastFocused = document.activeElement;
        dialog.showModal();
        document.body.classList.add('catalog-request-open');
        byId('catalogRequestTitle').focus();
    }

    function clearSuccess() {
        successReference = '';
        byId('catalogRequestSuccess').hidden = true;
        form.hidden = false;
        feedback('');
    }

    function refreshCard(item, id) {
        const card = document.querySelector('.product-card[data-product-id="' + id + '"]');
        if (!card) return;
        if (item) {
            card.dataset.jpyPrice = item.price === null ? '' : String(item.price);
            const price = card.querySelector('.product-card-price');
            if (item.price === null) {
                price.removeAttribute('data-jpy-price');
                price.textContent = 'Price on request';
            } else {
                price.dataset.jpyPrice = String(item.price);
                price.textContent = typeof formatPriceFromJpy === 'function'
                    ? formatPriceFromJpy(item.price, currentCurrency()) : priceLabel(item.price);
            }
        }
        const inStock = item && itemLimit(item) > 0;
        const stock = card.querySelector('.stock-badge');
        stock.textContent = unchecked(item) ? 'Stock not checked' : inStock ? 'In Stock' : 'Unavailable';
        stock.className = 'stock-badge ' + (inStock ? 'in-stock' : 'out-of-stock');
        const availabilityLabel = card.querySelector('[data-availability-label]');
        if (availabilityLabel) availabilityLabel.textContent = unchecked(item) ? 'Stock not checked' : inStock ? 'Available' : 'Unavailable';
        let quantity = card.querySelector('.stock-qty');
        if (!quantity && inStock) {
            quantity = document.createElement('span');
            quantity.className = 'stock-qty';
            stock.parentNode.appendChild(quantity);
        }
        if (quantity) quantity.textContent = unchecked(item) ? '' : 'Qty: ' + (inStock ? item.stock : 0);
        if (typeof DETAIL_CACHE !== 'undefined') delete DETAIL_CACHE[id];
    }

    function applyAvailabilityItem(item, id) {
        if (!item || item.product_id !== id) return;
        items.set(id, item);
        unavailableIds.delete(id);
        const entry = selection.find(selected => selected.product_id === id);
        if (entry && (entry.expected_price_jpy !== item.price || itemLimit(item) < entry.quantity)) {
            entry.expected_price_jpy = item.price;
            resetAttempt();
            requireReconfirmation();
        }
        refreshCard(item, id);
        renderSelection();
    }

    async function checkAvailability(id) {
        if (submitting || !unchecked(items.get(id))) return;
        const previous = detailChecks.get(id);
        if (previous && (previous.active || previous.retryAt > Date.now())) return;
        const state = {active: true, retryAt: 0};
        detailChecks.set(id, state);
        refreshControls();
        announce('Checking availability of ' + itemTitle(items.get(id), id) + '.');
        const deadline = Date.now() + detailPollDuration;
        let method = 'POST';
        try {
            while (Date.now() < deadline) {
                const controller = new AbortController();
                const timeout = window.setTimeout(() => controller.abort(), 15000);
                let response, data;
                try {
                    response = await fetch(config.details_url.replace('/products/0/details', '/products/' + id + '/details'), {
                        method: method,
                        credentials: 'same-origin',
                        headers: {'Accept': 'application/json', 'X-CSRFToken': config.csrf_token},
                        signal: controller.signal
                    });
                    data = await response.json().catch(() => ({}));
                } finally {
                    window.clearTimeout(timeout);
                }
                if (!response.ok) {
                    const retry = Number(response.headers.get('Retry-After')) || Number(data.retry_after_seconds) || 30;
                    state.retryAt = Date.now() + Math.max(3, Math.min(3600, retry)) * 1000;
                    if (response.status === 404) {
                        items.delete(id);
                        unavailableIds.add(id);
                        refreshCard(null, id);
                    }
                    throw new Error('Availability check unavailable');
                }
                applyAvailabilityItem(data.item, id);
                if (data.status === 'ready') {
                    announce(itemTitle(items.get(id), id) + ' is available. Review the current price and select Add.');
                    return;
                }
                if (data.status === 'unavailable') {
                    announce(itemTitle(items.get(id), id) + ' is unavailable.');
                    return;
                }
                if (data.status !== 'pending') {
                    state.retryAt = Date.now() + 30000;
                    break;
                }
                const delay = Math.max(3, Math.min(3600, Number(data.retry_after_seconds) || 3)) * 1000;
                if (Date.now() + delay >= deadline) {
                    state.retryAt = Date.now() + delay;
                    break;
                }
                await new Promise(resolve => window.setTimeout(resolve, delay));
                method = 'GET';
            }
            announce('Availability is still being checked. Please check again shortly.');
        } catch (_error) {
            if (!state.retryAt) state.retryAt = Date.now() + 30000;
            announce('Availability could not be confirmed yet. Please check again shortly.');
        } finally {
            state.active = false;
            refreshControls();
            if (state.retryAt > Date.now()) {
                window.setTimeout(refreshControls, state.retryAt - Date.now() + 100);
            }
        }
    }

    function applyCatalogUpdate(currentItems) {
        const updates = new Map((Array.isArray(currentItems) ? currentItems : [])
            .filter(item => item && Number.isSafeInteger(item.product_id))
            .map(item => [item.product_id, item]));
        selection.forEach(entry => {
            const item = updates.get(entry.product_id);
            if (item) {
                items.set(entry.product_id, item);
                entry.expected_price_jpy = item.price;
                unavailableIds.delete(entry.product_id);
            } else {
                unavailableIds.add(entry.product_id);
            }
            refreshCard(item, entry.product_id);
        });
        resetAttempt();
        requireReconfirmation();
        renderSelection();
        if (typeof applySort === 'function') applySort();
        else if (typeof filterProducts === 'function') filterProducts();
    }

    document.querySelectorAll('[data-request-product-id]').forEach(button => {
        button.addEventListener('click', function () {
            const id = Number(button.dataset.requestProductId);
            if (unchecked(items.get(id))) {
                checkAvailability(id);
                return;
            }
            if (selection.some(entry => entry.product_id === id)) {
                openDialog();
                return;
            }
            const item = items.get(id);
            if (!item || itemLimit(item) < 1 || selection.length >= maxItems || submitting) return;
            clearSuccess();
            if (!submissionKey) submissionKey = createKey();
            selection.push({product_id: id, quantity: 1, expected_price_jpy: item.price});
            resetAttempt();
            renderSelection();
            announce(itemTitle(item, id) + ' added to your request.');
        });
    });

    byId('catalogRequestOpen').addEventListener('click', openDialog);
    byId('catalogRequestClose').addEventListener('click', () => dialog.close());
    byId('catalogRequestDone').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', function () {
        document.body.classList.remove('catalog-request-open');
        const target = lastFocused && lastFocused.isConnected && !lastFocused.disabled
            ? lastFocused : byId('catalogRequestOpen');
        if (!target.closest('[hidden]')) target.focus();
    });
    byId('catalogRequestClear').addEventListener('click', function () {
        selection = [];
        resetAttempt();
        renderSelection();
        instagram.focus();
        announce('All items removed from your request.');
    });
    [instagram, buyerName, message].forEach(input => input.addEventListener('input', resetAttempt));
    reconfirm.addEventListener('change', refreshControls);

    form.addEventListener('submit', async function (event) {
        event.preventDefault();
        if (submitting || submitButton.disabled || !form.reportValidity()) return;
        const payload = {
            buyer_instagram: instagram.value.trim().replace(/^@/, ''),
            buyer_name: buyerName.value.trim(),
            message: message.value.trim(),
            items: selection.map(entry => ({...entry}))
        };
        const fingerprint = JSON.stringify(payload);
        if (!submissionKey || (attemptedFingerprint !== null && attemptedFingerprint !== fingerprint)) {
            submissionKey = createKey();
        }
        attemptedFingerprint = fingerprint;
        payload.submission_key = submissionKey;
        saveSelection();
        submitting = true;
        fields.disabled = true;
        form.setAttribute('aria-busy', 'true');
        feedback('Sending your request…');
        refreshControls();
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 20000);
        try {
            const response = await fetch(config.submit_url, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-CSRFToken': config.csrf_token
                },
                body: JSON.stringify(payload),
                signal: controller.signal
            });
            const data = await response.json().catch(() => ({}));
            if (response.ok && data.ok === true && typeof data.reference === 'string') {
                successReference = data.reference;
                selection = [];
                submissionKey = '';
                attemptedFingerprint = null;
                saveSelection();
                form.reset();
                clearReconfirmation();
                byId('catalogRequestReference').textContent = successReference;
                byId('catalogRequestSuccess').hidden = false;
                form.hidden = true;
                feedback('');
                announce('Request received. Your reference is ' + successReference + '.');
                if (dialog.open) byId('catalogRequestDone').focus();
                renderSelection();
            } else if (response.status === 409 && ['catalog_changed', 'items_unavailable'].includes(data.code)) {
                applyCatalogUpdate(data.items);
                feedback('The catalog has changed. Review the updated prices and quantities below. Remove unavailable items, then confirm the details before sending again.');
            } else if (response.status === 409 && data.code === 'duplicate_submission') {
                resetAttempt();
                requireReconfirmation();
                feedback('An earlier request used different details. Please review this request and confirm before sending it again.');
            } else if (response.status === 404) {
                feedback('This catalog is no longer accepting requests. Please contact the shop on Instagram.');
            } else if (response.status === 429) {
                feedback('Too many requests. Please wait a little before trying again. Your items and message are still here.');
            } else if (response.status === 400) {
                feedback(data.code === 'validation_error' && typeof data.error === 'string'
                    ? data.error
                    : 'We could not send your request. Copy your message before refreshing this catalog, then try again.');
            } else {
                feedback('We could not confirm whether your request was received. Please retry with the same details; the same request will only be recorded once.');
            }
        } catch (_error) {
            // Keep the key after a timeout: the server may already have saved it.
            feedback('We could not confirm whether your request was received. Check your connection and retry with the same details; the same request will only be recorded once.');
        } finally {
            window.clearTimeout(timeout);
            submitting = false;
            fields.disabled = false;
            form.removeAttribute('aria-busy');
            refreshControls();
        }
    });

    restoreSelection();
    if (selection.length && !submissionKey) submissionKey = createKey();
    if (needsReconfirm) {
        requireReconfirmation();
        feedback('Your saved selection has changed. Review the current items, quantities and reference prices before sending.');
    }
    saveSelection();
    renderSelection();
})();

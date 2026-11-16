(() => {
  'use strict';

  const VERSION = '2026-11-15.4';
  const $ = (s, r = document) => r.querySelector(s);
  const ico = (n, cls = '') => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* quota / private mode */ } },
  };

  const DEFAULTS = {
    provider: '', apiKey: '', baseUrl: '', backend: '', token: '', model: '', temperature: 0.7, system: '', theme: 'auto',
    fontSize: 'normal', enterSend: window.matchMedia('(hover: hover)').matches,
  };
  const MAX_FILE = 200 * 1024; // 200 KB per attached text file

  let settings = { ...DEFAULTS, ...store.get('gdai.settings', {}) };
  if (!settings.provider) settings.provider = settings.backend ? 'backend' : 'demo';
  if (settings.model === 'default') settings.model = '';
  let chats = store.get('gdai.chats', []);
  let activeId = store.get('gdai.active', null);
  let attachments = [];
  let abortCtl = null;
  let busy = false;

  const els = {
    app: $('#app'), list: $('#chatList'), messages: $('#messages'), input: $('#input'),
    form: $('#composer'), send: $('#sendBtn'), title: $('#chatTitle'), badge: $('#modeBadge'),
    chips: $('#chips'), search: $('#search'), toBottom: $('#toBottom'), modal: $('#settings'), toast: $('#toast'),
  };

  const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const saveChats = () => { store.set('gdai.chats', chats); store.set('gdai.active', activeId); };
  const active = () => chats.find(c => c.id === activeId) || null;
  const backendUrl = () => settings.backend.trim().replace(/\/+$/, '');
  const authHeaders = (extra = {}) => settings.token ? { ...extra, Authorization: 'Bearer ' + settings.token.trim() } : extra;
  // Тихие запросы к серверу (переименование, удаление): ошибки не мешают работе интерфейса
  const bg = (method, path, body) => {
    if (settings.provider !== 'backend' || !backendUrl()) return;
    fetch(backendUrl() + path, { method, headers: authHeaders(body ? { 'Content-Type': 'application/json' } : {}), body: body ? JSON.stringify(body) : undefined }).catch(() => {});
  };

  /* ---------- Markdown (small, safe) ---------- */
  const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function inline(t) {
    t = esc(t);
    t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return t;
  }

  function block(text) {
    let out = '', para = [], list = null;
    const flushP = () => { if (para.length) { out += '<p>' + para.map(inline).join('<br>') + '</p>'; para = []; } };
    const flushL = () => { if (list) { out += `<${list.t}>${list.items.map(i => `<li>${inline(i)}</li>`).join('')}</${list.t}>`; list = null; } };
    for (const line of text.split('\n')) {
      let m;
      if ((m = line.match(/^(#{1,3})\s+(.*)$/))) { flushP(); flushL(); out += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; }
      else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { flushP(); if (!list || list.t !== 'ul') { flushL(); list = { t: 'ul', items: [] }; } list.items.push(m[1]); }
      else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushP(); if (!list || list.t !== 'ol') { flushL(); list = { t: 'ol', items: [] }; } list.items.push(m[1]); }
      else if (!line.trim()) { flushP(); flushL(); }
      else { flushL(); para.push(line); }
    }
    flushP(); flushL();
    return out;
  }

  function md(src) {
    return src.split('```').map((p, i) => {
      if (i % 2 === 0) return block(p);
      const nl = p.indexOf('\n');
      let lang = '', code = p;
      if (nl >= 0) { lang = p.slice(0, nl).trim(); code = p.slice(nl + 1); }
      code = code.replace(/\n$/, '');
      return `<div class="code"><div class="code-head"><span>${esc(lang || 'code')}</span><button class="copy" type="button">${ico('copy')}<span>Копировать</span></button></div><pre><code>${esc(code)}</code></pre></div>`;
    }).join('');
  }

  /* ---------- Rendering ---------- */
  const lastTs = c => (c.messages?.length ? c.messages[c.messages.length - 1].ts : 0) || c.created || 0;
  function dateGroup(ts) {
    const d0 = new Date(); d0.setHours(0, 0, 0, 0);
    const days = Math.floor((d0 - ts) / 86400000) + (ts >= d0 ? 0 : 1);
    if (ts >= d0) return 'Сегодня';
    if (days <= 1) return 'Вчера';
    if (days <= 7) return 'Последние 7 дней';
    return 'Ранее';
  }

  function renderChatList() {
    const q = els.search.value.trim().toLowerCase();
    const items = chats.filter(c => !q || c.title.toLowerCase().includes(q))
      .sort((a, b) => lastTs(b) - lastTs(a));
    els.list.innerHTML = '';
    if (!items.length) { els.list.innerHTML = `<div class="empty-list">${q ? 'Ничего не найдено' : 'Чатов пока нет'}</div>`; return; }
    let group = '';
    for (const c of items) {
      const g = dateGroup(lastTs(c));
      if (g !== group) { group = g; const h = document.createElement('div'); h.className = 'group'; h.textContent = g; els.list.appendChild(h); }
      const row = document.createElement('div');
      row.className = 'chat-item' + (c.id === activeId ? ' active' : '');
      row.dataset.id = c.id;
      row.innerHTML = `<span class="name">${esc(c.title)}</span>
        <button class="act" type="button" data-a="menu" aria-haspopup="menu" title="Действия" aria-label="Действия с чатом">${ico('dots')}</button>`;
      els.list.appendChild(row);
    }
  }

  function msgNode(m, idx, isLast) {
    const el = document.createElement('div');
    el.className = 'msg ' + m.role;
    el.dataset.idx = idx;
    const tools = m.role === 'assistant'
      ? `<div class="msg-tools"><button data-a="copy" type="button">${ico('copy')}<span>Копировать</span></button>${isLast ? `<button data-a="regen" type="button">${ico('refresh')}<span>Заново</span></button>` : ''}</div>` : '';
    el.innerHTML = `<div class="avatar">${m.role === 'user' ? 'Я' : 'AI'}</div><div class="body-wrap" style="flex:1;min-width:0"><div class="body"></div>${tools}</div>`;
    setBody(el, m);
    return el;
  }

  function setBody(el, m, streaming = false) {
    const body = $('.body', el);
    if (m.role === 'user') body.innerHTML = block(m.content).replace(/<h[1-3]>/g, '<p>').replace(/<\/h[1-3]>/g, '</p>');
    else body.innerHTML = md(m.content || '') + (m.error ? `<p class="error-text">${esc(m.error)}</p>` : '');
    body.classList.toggle('typing', streaming);
  }

  const SUGGESTIONS = [
    'Придумай концепт 2D-игры и разбей на этапы разработки',
    'Напиши базовый игровой цикл на JavaScript (canvas)',
    'Объясни, как устроен ECS и когда он нужен',
    'Помоги спроектировать систему инвентаря',
  ];

  function renderMessages() {
    const chat = active();
    els.messages.innerHTML = '';
    els.title.textContent = chat ? chat.title : 'GameDev AI';
    if (!chat || !chat.messages.length) {
      const w = document.createElement('div');
      w.className = 'welcome';
      w.innerHTML = '<img class="welcome-logo" src="icons/logo.svg" width="64" height="64" alt=""><h1>Чем помочь в разработке игры?</h1><p>Код, механики, баланс, архитектура — спрашивай.</p><div class="suggest"></div>';
      for (const s of SUGGESTIONS) {
        const b = document.createElement('button'); b.type = 'button'; b.textContent = s;
        b.onclick = () => { els.input.value = s; autosize(); els.input.focus(); };
        $('.suggest', w).appendChild(b);
      }
      els.messages.appendChild(w);
      return;
    }
    chat.messages.forEach((m, i) => els.messages.appendChild(msgNode(m, i, i === chat.messages.length - 1)));
    scrollBottom(true);
  }

  function nearBottom() { const m = els.messages; return m.scrollHeight - m.scrollTop - m.clientHeight < 120; }
  function scrollBottom(force) { if (force || nearBottom()) els.messages.scrollTop = els.messages.scrollHeight; }

  function renderChips() {
    els.chips.innerHTML = '';
    attachments.forEach((a, i) => {
      const c = document.createElement('span');
      c.className = 'chip';
      c.innerHTML = `${esc(a.name)}<button type="button" aria-label="Убрать" data-i="${i}">${ico('x')}</button>`;
      els.chips.appendChild(c);
    });
  }

  function renderBadge() {
    const p = settings.provider;
    const names = { anthropic: 'Claude', openai: 'ИИ', backend: 'сервер', demo: 'демо' };
    const online = p !== 'demo';
    els.badge.textContent = names[p] || 'демо';
    els.badge.classList.toggle('online', online);
    els.badge.title = online ? 'Подключён: ' + (settings.model || 'модель по умолчанию') : 'Демо-режим: настоящего ИИ нет. Выбери провайдера в Настройках';
  }

  function setBusy(b) {
    busy = b;
    els.send.classList.toggle('stop', b);
    els.send.innerHTML = ico(b ? 'stop' : 'up');
    els.send.setAttribute('aria-label', b ? 'Остановить' : 'Отправить');
  }

  /* ---------- Chats ---------- */
  function newChat() {
    const c = { id: uid(), title: 'Новый чат', messages: [], created: Date.now() };
    chats.unshift(c); activeId = c.id; saveChats();
    renderChatList(); renderMessages(); closeMenu(); els.input.focus();
    return c;
  }

  function selectChat(id) {
    if (busy) abortCtl?.abort();
    activeId = id; saveChats(); renderChatList(); renderMessages(); closeMenu();
  }

  function deleteChat(id) {
    if (!confirm('Удалить чат?')) return;
    if (busy && id === activeId) abortCtl?.abort();
    chats = chats.filter(c => c.id !== id);
    bg('DELETE', '/api/chats/' + encodeURIComponent(id));
    if (activeId === id) activeId = chats[0]?.id || null;
    saveChats(); renderChatList(); renderMessages();
  }

  function renameChat(id) {
    const c = chats.find(x => x.id === id); if (!c) return;
    const t = prompt('Название чата', c.title);
    if (t && t.trim()) { c.title = t.trim().slice(0, 80); bg('PATCH', '/api/chats/' + encodeURIComponent(id), { title: c.title }); saveChats(); renderChatList(); if (id === activeId) els.title.textContent = c.title; }
  }

  function exportChat(id) {
    const c = id ? chats.find(x => x.id === id) : active();
    if (!c || !c.messages.length) { toast('В чате пока нет сообщений'); return; }
    const text = c.messages.map(m => `### ${m.role === 'user' ? 'Я' : 'AI'}\n\n${m.content}\n`).join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([`# ${c.title}\n\n${text}`], { type: 'text/markdown' }));
    a.download = (c.title.replace(/[^\wЀ-ӿ-]+/g, '_') || 'chat') + '.md';
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  /* ---------- Sending ---------- */
  async function send() {
    if (busy) { abortCtl?.abort(); return; }
    const text = els.input.value.trim();
    if (!text && !attachments.length) return;

    let chat = active() || newChat();
    let content = text;
    for (const a of attachments) content += `\n\n**Файл: ${a.name}**\n\`\`\`\n${a.text}\n\`\`\``;
    attachments = []; renderChips();

    chat.messages.push({ role: 'user', content: content.trim(), ts: Date.now() });
    if (chat.title === 'Новый чат') { chat.title = (text || 'Файлы').slice(0, 48); renderChatList(); }
    els.input.value = ''; autosize();
    await reply(chat);
  }

  async function reply(chat) {
    const msg = { role: 'assistant', content: '', ts: Date.now() };
    chat.messages.push(msg);
    saveChats(); renderMessages();
    const node = els.messages.lastElementChild;
    const update = () => { setBody(node, msg, true); scrollBottom(); };

    abortCtl = new AbortController();
    setBusy(true);
    try {
      const history = chat.messages.slice(0, -1).map(({ role, content }) => ({ role, content }));
      if (settings.provider === 'backend') {
        if (!backendUrl()) throw new Error('Не указан адрес сервера. Открой Настройки.');
        await streamBackend(chat, history, msg, update, abortCtl.signal);
      } else if (settings.provider === 'anthropic' || settings.provider === 'openai') {
        await streamProvider(history, msg, update, abortCtl.signal);
      } else {
        await streamDemo(history, msg, update, abortCtl.signal);
      }
    } catch (e) {
      if (e.name !== 'AbortError') msg.error = 'Ошибка: ' + (e.message || e);
    } finally {
      setBusy(false); abortCtl = null;
      if (!msg.content && !msg.error) msg.content = '_(остановлено)_';
      saveChats(); renderMessages();
    }
  }

  async function streamBackend(chat, history, msg, update, signal) {
    // Бесплатный Render засыпает: если ответа нет дольше 4 с, говорим об этом
    const wake = setTimeout(() => toast('Сервер просыпается, это займёт до минуты…'), 4000);
    const post = () => fetch(backendUrl() + '/api/chat', {
      method: 'POST', signal,
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ chat_id: chat.id, messages: history, model: settings.model, temperature: settings.temperature, system: settings.system || null }),
    });
    let res;
    try {
      try { res = await post(); }
      catch (e) {
        if (e.name === 'AbortError') throw e;
        // Скорее всего сервер спал: ждём, пока он проснётся, и повторяем один раз
        if (!(await wakeServer(signal))) throw new Error('Сервер не ответил за 90 секунд. Если ссылка сервера открывается в Safari, то в Render → Environment проверь CORS_ORIGINS (должно быть https://dobroe345-cyber.github.io). Иначе проверь адрес в Настройках.');
        res = await post();
      }
    } catch (e) {
      if (e.name === 'AbortError') throw e;
      throw new Error('Не удалось подключиться к серверу (адрес, интернет или CORS). Сообщение сохранено только на этом устройстве.');
    } finally { clearTimeout(wake); }
    if (res.status === 401) throw new Error('Сервер не принял токен. Проверь «Токен доступа» в Настройках.');
    if (res.status === 503) throw new Error('Сейчас нет доступных моделей (GPU выключен, резервные не настроены). Вопрос сохранён на сервере, повтори позже.');
    if (!res.ok) throw new Error('Сервер ответил HTTP ' + res.status);
    setConn('ok', 'Сервер на связи');
    const used = res.headers.get('X-Provider'), from = res.headers.get('X-Fallback');
    if (from && used) toast(`Основная модель недоступна, отвечает «${used}»`);
    const reader = res.body.getReader(), dec = new TextDecoder();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      msg.content += dec.decode(value, { stream: true });
      update();
    }
  }

  // Ждёт, пока бесплатный сервер проснётся (опрашивает /api/health до 90 секунд)
  async function wakeServer(signal) {
    const end = Date.now() + 90000;
    while (Date.now() < end) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      try {
        const r = await fetch(backendUrl() + '/api/health', { signal });
        if (r.ok) return true;
      } catch (e) { if (e.name === 'AbortError') throw e; }
      await new Promise(r => setTimeout(r, 3000));
    }
    return false;
  }

  async function wakeServerAt(url) {
    const end = Date.now() + 90000;
    while (Date.now() < end) {
      try { if ((await fetch(url + '/api/health')).ok) return true; } catch { /* ещё спит */ }
      await new Promise(r => setTimeout(r, 3000));
    }
    return false;
  }

  // Подтягивает переписки с сервера (после перезапуска Render, на другом устройстве, после очистки браузера)
  async function syncFromServer() {
    if (settings.provider !== 'backend' || !backendUrl() || !settings.token) return;
    try {
      const r = await fetch(backendUrl() + '/api/chats', { headers: authHeaders() });
      if (!r.ok) return;
      const remote = await r.json();
      let changed = false;
      for (const rc of remote) {
        const id = rc.id || rc.chat_id;
        const local = chats.find(c => c.id === id);
        if (local && local.messages.length && !busy) {
          const rr = await fetch(backendUrl() + '/api/chats/' + encodeURIComponent(id), { headers: authHeaders() });
          if (!rr.ok) continue;
          const full = await rr.json();
          if ((full.messages || []).length <= local.messages.length) continue;
          local.messages = serverMessages(full.messages); changed = true;
        } else if (!local) {
          const rr = await fetch(backendUrl() + '/api/chats/' + encodeURIComponent(id), { headers: authHeaders() });
          if (!rr.ok) continue;
          const full = await rr.json();
          chats.push({ id, title: full.title || rc.title || 'Чат', messages: serverMessages(full.messages), created: (rc.created_at || 0) * 1000 || Date.now() });
          changed = true;
        }
      }
      if (changed) {
        chats.sort((a, b) => (b.created || 0) - (a.created || 0));
        if (!chats.find(c => c.id === activeId)) activeId = chats[0]?.id || null;
        saveChats(); renderChatList(); renderMessages();
      }
    } catch { /* сервер спит или недоступен: работаем с локальной копией */ }
  }

  function serverMessages(list) {
    return (list || []).map(m => {
      const o = { role: m.role, content: m.content || '', ts: (m.ts || 0) * 1000 || Date.now() };
      if (m.status === 'interrupted') o.content += (o.content ? '\n\n' : '') + '_(ответ оборвался, нажми «повторить»)_';
      return o;
    });
  }

  // Настоящий ИИ: запрос из браузера к Anthropic или OpenAI-совместимому API
  async function streamProvider(history, msg, update, signal) {
    const cfg = {
      provider: settings.provider,
      apiKey: settings.apiKey.trim(),
      baseUrl: settings.baseUrl.trim(),
      model: settings.model.trim(),
      temperature: settings.temperature,
      system: settings.system.trim() || GDAI.DEFAULT_SYSTEM,
      messages: history,
      signal,
    };
    for await (const chunk of GDAI.stream(cfg)) { msg.content += chunk; update(); }
  }

  // Демо-режим: работает без сервера (например, на GitHub Pages)
  async function streamDemo(history, msg, update, signal) {
    const last = history[history.length - 1]?.content || '';
    const preview = last.length > 200 ? last.slice(0, 200) + '…' : last;
    const text = `Сейчас включён **демо-режим**: бэкенд не подключён, поэтому настоящего ИИ-ответа нет.\n\nТвоё сообщение получено:\n\n> ${preview.replace(/\n/g, ' ')}\n\nЧтобы получить настоящие ответы, открой **Настройки** (⚙), выбери провайдера (Claude, OpenAI-совместимый или свой сервер) и вставь API-ключ.\n\n\`\`\`js\n// пример: игровой цикл\nfunction loop(t) {\n  update(t);\n  render();\n  requestAnimationFrame(loop);\n}\nrequestAnimationFrame(loop);\n\`\`\``;
    const full = text + settingsInfo();
    for (const tok of full.match(/\s*\S+/g) || []) {
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
      msg.content += tok; update();
      await new Promise(r => setTimeout(r, 18));
    }
  }

  function regenerate() {
    const chat = active(); if (!chat || busy) return;
    if (chat.messages.at(-1)?.role === 'assistant') chat.messages.pop();
    reply(chat);
  }

  /* ---------- Files ---------- */
  async function addFiles(files) {
    for (const f of files) {
      if (f.size > MAX_FILE) { alert(`«${f.name}» больше ${MAX_FILE / 1024} КБ — пропущен.`); continue; }
      try {
        const text = await f.text();
        if (text.includes('\u0000')) { alert(`«${f.name}» похож на бинарный файл — пропущен.`); continue; }
        attachments.push({ name: f.name, text });
      } catch { alert(`Не удалось прочитать «${f.name}».`); }
    }
    renderChips();
  }

  /* ---------- UI helpers ---------- */
  function autosize() { els.input.style.height = 'auto'; els.input.style.height = Math.min(els.input.scrollHeight, 200) + 'px'; }
  const isMobile = () => window.matchMedia('(max-width: 800px)').matches;
  function setCollapsed(v) { els.app.classList.toggle('side-collapsed', v); store.set('gdai.sideCollapsed', v); }
  function closeMenu() { els.app.classList.remove('menu-open'); }
  function toggleSide() {
    if (isMobile()) els.app.classList.toggle('menu-open');
    else setCollapsed(!els.app.classList.contains('side-collapsed'));
  }
  function hideSide() { if (isMobile()) closeMenu(); else setCollapsed(true); }

  // Копирование с запасным способом (если у браузера нет доступа к буферу обмена)
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return; } catch { /* пробуем запасной путь */ }
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
    try { document.execCommand('copy'); } finally { ta.remove(); }
  }

  function flash(btn, label = 'Скопировано') {
    const html = btn.innerHTML;
    btn.innerHTML = ico('check') + `<span>${label}</span>`;
    setTimeout(() => { btn.innerHTML = html; }, 1200);
  }

  /* ---------- Всплывающее меню ---------- */
  const pop = $('#popMenu');
  let popOwner = null;
  function hidePop() { pop.classList.add('hidden'); popOwner = null; }
  function showPop(anchor, items) {
    if (popOwner === anchor) { hidePop(); return; }          // повторное нажатие закрывает
    pop._items = items;
    pop.innerHTML = items.map((it, i) => it.sep ? '<div class="sep"></div>'
      : `<button type="button" role="menuitem" data-i="${i}"${it.danger ? ' class="danger"' : ''}>${ico(it.icon)}<span>${esc(it.label)}</span></button>`).join('');
    pop.classList.remove('hidden');
    popOwner = anchor;
    const r = anchor.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
    const left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8);
    let top = r.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    pop.style.left = left + 'px'; pop.style.top = top + 'px';
  }
  pop.addEventListener('click', e => {
    const b = e.target.closest('button[data-i]'); if (!b) return;
    const it = pop._items[+b.dataset.i]; hidePop(); it.run();
  });
  document.addEventListener('pointerdown', e => {
    if (pop.classList.contains('hidden') || pop.contains(e.target)) return;
    if (e.target.closest('[data-a="menu"], #moreBtn')) return;   // их обработчик сам решит, закрыть или переключить
    hidePop();
  });
  window.addEventListener('resize', hidePop);
  els.messages.addEventListener('scroll', hidePop, { passive: true });

  const THEME_NAMES = { auto: 'как в системе', dark: 'тёмная', light: 'светлая' };
  function chatMenu(anchor, id) {
    showPop(anchor, [
      { icon: 'pencil', label: 'Переименовать', run: () => renameChat(id) },
      { icon: 'download', label: 'Экспорт в Markdown', run: () => exportChat(id) },
      { sep: true },
      { icon: 'trash', label: 'Удалить', danger: true, run: () => deleteChat(id) },
    ]);
  }
  function moreMenu(anchor) {
    const c = active(), items = [];
    if (c) items.push(
      { icon: 'pencil', label: 'Переименовать чат', run: () => renameChat(c.id) },
      { icon: 'download', label: 'Экспорт в Markdown', run: () => exportChat(c.id) },
      { icon: 'trash', label: 'Удалить чат', danger: true, run: () => deleteChat(c.id) },
      { sep: true });
    items.push(
      { icon: 'plus', label: 'Новый чат', run: newChat },
      { icon: settings.theme === 'dark' ? 'moon' : settings.theme === 'light' ? 'sun' : 'auto', label: 'Тема: ' + THEME_NAMES[settings.theme], run: cycleTheme },
      { icon: 'sliders', label: 'Настройки', run: openSettings });
    showPop(anchor, items);
  }

  /* ---------- Состояние подключения ---------- */
  let connSeq = 0;
  function setConn(state, text) {
    $('#connDot').className = 'dot ' + state;
    $('#connText').textContent = text;
  }
  async function checkServer() {
    const seq = ++connSeq, p = settings.provider;
    if (p !== 'backend') {
      setConn(p === 'demo' ? 'off' : 'ok', p === 'demo' ? 'Демо-режим: ИИ не подключён' : p === 'anthropic' ? 'Claude: прямой ключ' : 'ИИ: прямой ключ');
      return;
    }
    if (!backendUrl()) { setConn('bad', 'Не указан адрес сервера'); return; }
    const url = backendUrl();
    setConn('wait', 'Проверяю сервер…');
    try { if ((await fetch(url + '/api/health')).ok) { if (seq === connSeq) setConn('ok', 'Сервер на связи'); return; } } catch { /* спит */ }
    if (seq === connSeq) setConn('wait', 'Сервер просыпается…');
    const ok = await wakeServerAt(url);
    if (seq === connSeq) ok ? setConn('ok', 'Сервер на связи') : setConn('bad', 'Нет связи с сервером');
  }

  /* ---------- Вкладки настроек ---------- */
  function showTab(name) {
    document.querySelectorAll('#settingsTabs button').forEach(b => {
      const on = b.dataset.tab === name;
      b.classList.toggle('on', on); b.setAttribute('aria-selected', on);
    });
    document.querySelectorAll('.settings-body > section[data-tab]').forEach(sec => sec.classList.toggle('hidden', sec.dataset.tab !== name));
  }

  let toastTimer = null;
  function toast(text) {
    els.toast.textContent = text;
    els.toast.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => els.toast.classList.add('hidden'), 1800);
  }

  function applyFont() {
    const px = { small: '14px', normal: '16px', large: '18px' }[settings.fontSize] || '16px';
    document.documentElement.style.setProperty('--fs', px);
  }

  // Показывает в демо-режиме, какие настройки сейчас применяются
  function settingsInfo() {
    return `\n\n---\n**Текущие настройки:** модель — \`${settings.model || 'по умолчанию'}\`, температура — \`${settings.temperature}\`, системная инструкция — ${settings.system.trim() ? 'задана' : 'не задана'}.`;
  }

  function applyTheme() {
    const t = settings.theme;
    if (t === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', t);
    const use = $('#themeIcon use');
    if (use) use.setAttribute('href', '#i-' + (t === 'dark' ? 'moon' : t === 'light' ? 'sun' : 'auto'));
  }

  function cycleTheme() {
    const order = ['auto', 'dark', 'light'];
    settings.theme = order[(order.indexOf(settings.theme) + 1) % order.length];
    store.set('gdai.settings', settings); applyTheme();
    toast('Тема: ' + THEME_NAMES[settings.theme]);
  }

  function syncProviderFields() {
    const p = $('#sProvider').value;
    document.querySelectorAll('[data-for]').forEach(el => {
      el.classList.toggle('hidden', !el.dataset.for.split(' ').includes(p));
    });
    $('#sModel').placeholder = p === 'anthropic' ? 'claude-haiku-5-5' : p === 'openai' ? 'например: gpt-4o-mini' : 'по умолчанию';
  }

  function openSettings() {
    $('#sProvider').value = settings.provider;
    $('#sKey').value = settings.apiKey;
    $('#sBase').value = settings.baseUrl;
    syncProviderFields();
    $('#sBackend').value = settings.backend;
    $('#sToken').value = settings.token || '';
    $('#sModel').value = settings.model;
    $('#sTemp').value = settings.temperature;
    $('#sTempVal').textContent = settings.temperature;
    $('#sSystem').value = settings.system;
    $('#sTheme').value = settings.theme;
    $('#sFont').value = settings.fontSize;
    $('#sEnter').checked = !!settings.enterSend;
    $('#testResult').textContent = '';
    $('#appVersion').textContent = 'Версия интерфейса: ' + VERSION;
    hidePop(); showTab('conn');
    els.modal.classList.remove('hidden');
  }

  function closeSettings() { els.modal.classList.add('hidden'); }

  function saveSettings() {
    const temp = parseFloat($('#sTemp').value);
    settings = {
      backend: $('#sBackend').value.trim(),
      token: $('#sToken').value.trim(),
      provider: $('#sProvider').value,
      apiKey: $('#sKey').value.trim(),
      baseUrl: $('#sBase').value.trim(),
      model: $('#sModel').value.trim(),
      temperature: Number.isFinite(temp) ? temp : 0.7,
      system: $('#sSystem').value,
      theme: $('#sTheme').value,
      fontSize: $('#sFont').value,
      enterSend: $('#sEnter').checked,
    };
    store.set('gdai.settings', settings);
    applyTheme(); applyFont(); renderBadge();
    closeSettings();
    toast('Настройки сохранены');
    syncFromServer(); checkServer();
  }

  async function testConnection() {
    const out = $('#testResult'), provider = $('#sProvider').value;
    if (provider === 'demo') { out.textContent = 'Демо-режим: подключение не нужно'; return; }
    if (provider === 'backend') return testBackendServer();
    out.textContent = 'Проверяю…';
    const ctl = new AbortController();
    const cfg = {
      provider, apiKey: $('#sKey').value.trim(), baseUrl: $('#sBase').value.trim(),
      model: $('#sModel').value.trim(), temperature: 0.7, system: '',
      messages: [{ role: 'user', content: 'Ответь одним словом: ок' }], signal: ctl.signal,
    };
    try {
      let got = false;
      for await (const chunk of GDAI.stream(cfg)) {
        if (chunk) { out.textContent = '✓ Работает. Ответ: ' + chunk.trim().slice(0, 30); got = true; break; }
      }
      ctl.abort();
      if (!got) out.textContent = '✗ Сервис вернул пустой ответ';
    } catch (e) { out.textContent = '✗ ' + (e.message || e); }
  }

  async function testBackendServer() {
    const out = $('#testResult'), url = $('#sBackend').value.trim().replace(/\/+$/, '');
    if (!url) { out.textContent = 'Адрес не указан — будет демо-режим'; return; }
    if (location.protocol === 'https:' && url.startsWith('http:')) {
      out.textContent = '✗ Сайт на HTTPS не может обращаться к http-серверу. Нужен адрес https://…';
      return;
    }
    out.textContent = 'Проверяю (сервер может просыпаться до минуты)…';
    try {
      const r = await fetch(url + '/api/health').catch(async () => { out.textContent = 'Сервер просыпается, жду…'; return (await wakeServerAt(url)) ? fetch(url + '/api/health') : Promise.reject(); });
      if (!r.ok) { out.textContent = 'Ошибка HTTP ' + r.status; return; }
      const hj = await r.json().catch(() => ({}));
      const ver = `(сервер v${hj.version || '?'}, интерфейс v${VERSION})`;
      const token = $('#sToken').value.trim();
      if (!token) { out.textContent = '✓ Сервер отвечает ' + ver + '. Укажи токен доступа, чтобы проверить остальное'; return; }
      const sr = await fetch(url + '/api/status?deep=1', { headers: { Authorization: 'Bearer ' + token } });
      if (sr.status === 401) { out.textContent = '✗ Сервер работает, но токен неверный'; return; }
      if (!sr.ok) { out.textContent = '✗ Статус: HTTP ' + sr.status; return; }
      const st = await sr.json();
      const alive = Object.entries(st.models_alive || {}).filter(([, v]) => v).map(([k]) => k);
      out.textContent = '✓ Связь есть ' + ver + '. Рабочие модели: ' + (alive.join(', ') || 'нет') +
        (st.warnings?.length ? '\n⚠ ' + st.warnings.join('\n⚠ ') : '');
    } catch { out.textContent = '✗ Не удалось подключиться (адрес / CORS?)'; }
  }

  /* ---------- Events ---------- */
  els.form.addEventListener('submit', e => { e.preventDefault(); send(); });
  els.input.addEventListener('input', autosize);
  els.input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && settings.enterSend) { e.preventDefault(); send(); }
  });
  $('#fileInput').addEventListener('change', e => { addFiles([...e.target.files]); e.target.value = ''; });
  els.chips.addEventListener('click', e => { const b = e.target.closest('button[data-i]'); if (b) { attachments.splice(+b.dataset.i, 1); renderChips(); } });

  // drag & drop файлов
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => { e.preventDefault(); if (e.dataTransfer?.files?.length) addFiles([...e.dataTransfer.files]); });

  $('#newChat').onclick = newChat;
  $('#menuBtn').onclick = toggleSide;
  $('#closeSide').onclick = hideSide;
  $('#moreBtn').onclick = e => moreMenu(e.currentTarget);
  $('#closeSettings').onclick = closeSettings;
  $('#backdrop').onclick = closeMenu;
  $('#themeBtn').onclick = cycleTheme;
  $('#openSettings').onclick = openSettings;
  els.search.addEventListener('input', renderChatList);

  els.list.addEventListener('click', e => {
    const row = e.target.closest('.chat-item'); if (!row) return;
    const a = e.target.closest('.act');
    if (a) { e.stopPropagation(); chatMenu(a, row.dataset.id); }
    else selectChat(row.dataset.id);
  });
  els.list.addEventListener('contextmenu', e => {            // правая кнопка мыши / долгое нажатие
    const row = e.target.closest('.chat-item'); if (!row) return;
    e.preventDefault(); chatMenu(row.querySelector('.act'), row.dataset.id);
  });

  els.messages.addEventListener('click', e => {
    const copy = e.target.closest('.copy');
    if (copy) {
      const code = copy.closest('.code').querySelector('code').textContent;
      copyText(code).then(() => flash(copy));
      return;
    }
    const t = e.target.closest('.msg-tools button'); if (!t) return;
    if (t.dataset.a === 'regen') regenerate();
    if (t.dataset.a === 'copy') {
      const m = active()?.messages[+t.closest('.msg').dataset.idx];
      if (m) copyText(m.content).then(() => flash(t));
    }
  });

  els.messages.addEventListener('scroll', () => els.toBottom.classList.toggle('hidden', nearBottom()));
  els.toBottom.onclick = () => scrollBottom(true);

  $('#sTemp').addEventListener('input', e => { $('#sTempVal').textContent = e.target.value; });
  $('#testBackend').onclick = testConnection;
  $('#sProvider').addEventListener('change', syncProviderFields);
  $('#clearAll').onclick = () => {
    if (!confirm('Удалить ВСЕ чаты? Это нельзя отменить.')) return;
    abortCtl?.abort(); chats = []; activeId = null; saveChats(); renderChatList(); renderMessages();
    closeSettings(); toast('Все чаты удалены');
  };
  $('#settingsTabs').addEventListener('click', e => { const b = e.target.closest('button[data-tab]'); if (b) showTab(b.dataset.tab); });
  $('#saveSettings').onclick = saveSettings;
  $('#cancelSettings').onclick = closeSettings;
  els.modal.addEventListener('click', e => { if (e.target === els.modal) closeSettings(); });

  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); newChat(); }
    if (e.key === 'Escape') { hidePop(); closeMenu(); closeSettings(); }
  });

  /* ---------- Init ---------- */
  if (!chats.find(c => c.id === activeId)) activeId = chats[0]?.id || null;
  els.app.classList.toggle('side-collapsed', !!store.get('gdai.sideCollapsed', false));
  applyTheme(); applyFont(); renderBadge(); renderChatList(); renderMessages(); renderChips();
  syncFromServer(); checkServer();
})();

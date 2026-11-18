(() => {
  'use strict';

  const VERSION = '2026-11-18.1';
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
  let agentOn = !!store.get('gdai.agent', false);
  let agentTarget = store.get('gdai.agentTarget', 'project') === 'github' ? 'github' : 'project';
  let agentAsk = store.get('gdai.agentAsk', true) !== false;
  const agentTimers = new Map();   // id задачи -> таймер опроса
  const TERMINAL = ['done', 'failed', 'cancelled'];
  const AUTH_MSG = 'Сервер не принял вход. Открой Настройки → Подключение и войди по почте заново (или проверь токен).';
  const fmtNum = n => Number(n || 0).toLocaleString('ru-RU');
  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + ' с';
    if (sec < 3600) return Math.floor(sec / 60) + ' мин ' + (sec % 60 ? (sec % 60) + ' с' : '');
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h + ' ч' + (m ? ' ' + m + ' мин' : '');
  }

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
    const tools = m.role === 'assistant' && !(m.task && !TERMINAL.includes(m.task.status))
      ? `<div class="msg-tools"><button data-a="copy" type="button">${ico('copy')}<span>Копировать</span></button>${isLast && !m.task ? `<button data-a="regen" type="button">${ico('refresh')}<span>Заново</span></button>` : ''}</div>` : '';
    el.innerHTML = `<div class="avatar">${m.role === 'user' ? 'Я' : 'AI'}</div><div class="body-wrap" style="flex:1;min-width:0"><div class="body"></div>${tools}</div>`;
    setBody(el, m);
    return el;
  }

  function setBody(el, m, streaming = false) {
    const body = $('.body', el);
    if (m.role === 'user') body.innerHTML = block(m.content).replace(/<h[1-3]>/g, '<p>').replace(/<\/h[1-3]>/g, '</p>');
    else if (m.task && !TERMINAL.includes(m.task.status)) body.innerHTML = agentCard(m);
    else body.innerHTML = md(m.content || '') + (m.error ? `<p class="error-text">${esc(m.error)}</p>` : '')
      + (m.task && m.task.status !== 'done' && m.task.id ? `<button class="btn small" type="button" data-a="agent-retry">${ico('refresh')}<span>Запустить снова</span></button>` : '')
      + ((m.task?.status === 'done' && m.task.project) || m.project ? projectActions() : '');
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

    if (agentOn) {
      content = content.trim();
      if (settings.provider !== 'backend' || !backendUrl()) { toast('Агент работает через твой сервер: выбери «Свой сервер» в Настройках'); return; }
      if (content.length > 4000) { toast('Задача длиннее 4000 символов: сократи или убери файл'); return; }
    }
    chat.messages.push({ role: 'user', content: content.trim(), ts: Date.now() });
    if (chat.title === 'Новый чат') { chat.title = (text || 'Файлы').slice(0, 48); renderChatList(); }
    els.input.value = ''; autosize();
    if (agentOn) await startAgent(chat, content); else await reply(chat);
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
      const history = chat.messages.slice(0, -1).filter(m => m.content).map(({ role, content }) => ({ role, content }));
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

  /* ---------- Агент: правки кода в GitHub (задача «code» на сервере) ---------- */
  const tfirst = s => String(s || '').split('\n')[0].slice(0, 300);

  function logHtml(t) {
    const rows = (t.events || []).slice(-7).map((e, i, a) =>
      `<li class="${e.l === 'warn' || e.l === 'error' ? 'warn' : ''}${i === a.length - 1 ? ' now' : ''}">${esc(e.m)}</li>`).join('');
    return `<ul class="agent-log">${rows || '<li class="now">Ок, приступаю…</li>'}</ul>`;
  }

  function askBlock(t) {
    const q = t.question;
    const opts = (q.options || []).map((o, i) => `<button class="opt" type="button" data-a="agent-opt" data-i="${i}">${esc(o)}</button>`).join('');
    return `<div class="agent-ask" data-qid="${q.id}"><p class="ask-q">${esc(q.text)}</p>
      ${opts ? `<div class="opts">${opts}</div>` : ''}
      <div class="ask-row"><input class="ask-in" type="text" maxlength="300" enterkeyhint="send" placeholder="Или напиши свой ответ" value="${esc(t.draft || '')}"><button class="btn small primary" type="button" data-a="agent-send">Ответить</button></div>
      <button class="btn small auto" type="button" data-a="agent-auto" title="Агент сам выберет разумный вариант и больше спрашивать не будет">${ico('spark')}<span>Решай сам</span></button></div>`;
  }

  function projectActions() {
    return `<div class="agent-actions"><button class="btn small" type="button" data-a="proj-open">${ico('play')}<span>Открыть</span></button><button class="btn small" type="button" data-a="proj-zip">${ico('download')}<span>Скачать .zip</span></button></div>`;
  }

  function agentCard(m) {
    const t = m.task, asking = t.status === 'waiting' && t.question, pct = Math.round(Math.max(0.03, Math.min(1, t.progress || 0)) * 100);
    const head = asking ? 'Агент ждёт твоего ответа' : t.status === 'queued' && !t.answered ? (t.retry ? 'Жду повтора' : 'Жду очереди') : 'Агент работает';
    const note = t.offline ? '<p class="agent-note">Нет связи с сервером — задача продолжается там, интерфейс догонит, когда связь вернётся.</p>'
      : t.retry ? `<p class="agent-note">${esc(t.retry)}</p>` : '';
    return `<div class="agent-card${asking ? ' asking' : ''}"><div class="agent-head">${ico('agent')}<strong>${head}</strong><span class="agent-pct">${pct}%</span>
      <button class="btn small" type="button" data-a="agent-cancel">Остановить</button></div>
      <div class="agent-bar"><i style="width:${pct}%"></i></div>${asking ? askBlock(t) : ''}${note}${logHtml(t)}</div>`;
  }

  async function agentApi(path, method = 'GET', body) {
    const go = () => fetch(backendUrl() + path, { method, headers: authHeaders(body ? { 'Content-Type': 'application/json' } : {}), body: body ? JSON.stringify(body) : undefined });
    try { return await go(); }
    catch { if (!(await wakeServer())) throw new Error('Сервер не ответил за 90 секунд (он мог спать на бесплатном тарифе). Попробуй ещё раз.'); return go(); }
  }

  function repaint(chat, msg, full) {
    if (chat.id !== activeId) return;
    if (full) { renderMessages(); return; }
    const node = els.messages.querySelector(`.msg[data-idx="${chat.messages.indexOf(msg)}"]`);
    if (!node) return;
    const t = msg.task, card = $('.agent-card', node), ask = card && $('.agent-ask', card);
    // Пока вопрос тот же, его не перерисовываем: иначе пропадёт то, что человек уже печатает
    if (t && t.status === 'waiting' && t.question && ask && ask.dataset.qid === String(t.question.id)) {
      const log = $('.agent-log', card); if (log) log.outerHTML = logHtml(t);
      return;
    }
    setBody(node, msg); scrollBottom();
  }

  async function startAgent(chat, goal) {
    const msg = { role: 'assistant', content: '', ts: Date.now(), task: { id: null, status: 'queued', progress: 0, events: [] } };
    chat.messages.push(msg); saveChats(); renderMessages();
    toast(agentTarget === 'github' ? 'Агент запущен: он откроет pull request, main не трогает' : 'Агент запущен: файлы сохранятся в проекте на сервере');
    try {
      const r = await agentApi('/api/tasks', 'POST', { kind: 'code', title: goal.slice(0, 60), chat_id: chat.id, payload: { goal, chat_id: chat.id, max_steps: 16, mode: agentTarget, ask: agentAsk } });
      if (r.status === 401) throw new Error(AUTH_MSG);
      if (!r.ok) throw new Error(r.status === 400 ? 'Сервер старой версии: обнови сервер на Render (Manual Deploy).' : 'Сервер ответил HTTP ' + r.status);
      msg.task.id = (await r.json()).id;
      setConn('ok', 'Сервер на связи');
    } catch (e) {
      msg.task = null; msg.error = 'Ошибка: ' + (e.message || e);
      saveChats(); renderMessages(); return;
    }
    saveChats(); trackTask(chat, msg);
  }

  function applyTask(chat, msg, t) {
    const k = msg.task;
    k.status = t.status; k.progress = t.progress || 0; k.offline = false;
    k.events = (t.events || []).slice(-8).map(e => ({ m: e.message, l: e.level }));
    k.retry = t.status === 'queued' && t.error ? tfirst(t.error) : '';
    k.question = t.status === 'waiting' ? (t.state?.question || null) : null;
    if (t.status === 'done') { msg.content = t.result?.final || 'Готово.'; msg.error = ''; k.pr = t.result?.pr || null; k.project = t.result?.project || null; }
    else if (t.status === 'failed') { msg.error = tfirst(t.error) || 'Задача не выполнена'; }
    else if (t.status === 'cancelled') { msg.content = '_(агент остановлен)_'; msg.error = ''; }
  }

  function trackTask(chat, msg) {
    const id = msg.task?.id;
    if (!id || TERMINAL.includes(msg.task.status)) return;
    clearTimeout(agentTimers.get(id));
    let fails = 0;
    const tick = async () => {
      let delay = 2500;
      try {
        const r = await fetch(backendUrl() + '/api/tasks/' + encodeURIComponent(id), { headers: authHeaders() });
        if (r.status === 404) { msg.task.status = 'failed'; msg.error = 'Сервер не знает эту задачу (база данных была пуста после перезапуска). Запусти заново.'; }
        else if (r.status === 401) { msg.task.status = 'failed'; msg.error = AUTH_MSG; }
        else if (!r.ok) throw new Error('HTTP ' + r.status);
        else { fails = 0; applyTask(chat, msg, await r.json()); }
      } catch { fails++; msg.task.offline = true; delay = Math.min(15000, 3000 * fails); }
      saveChats();
      if (TERMINAL.includes(msg.task.status)) { agentTimers.delete(id); repaint(chat, msg, true); renderChatList(); }
      else { repaint(chat, msg, false); agentTimers.set(id, setTimeout(tick, delay)); }
    };
    agentTimers.set(id, setTimeout(tick, 300));
  }

  // После перезагрузки страницы или возвращения в браузер: продолжаем следить за незавершёнными задачами
  function resumeTasks() {
    if (settings.provider !== 'backend' || !backendUrl()) return;
    for (const c of chats) for (const m of c.messages) if (m.task?.id && !TERMINAL.includes(m.task.status)) trackTask(c, m);
  }

  async function agentAction(btn) {
    const chat = active(); if (!chat) return;
    const msg = chat.messages[+btn.closest('.msg').dataset.idx]; if (!msg) return;
    const act = btn.dataset.a;
    if (act === 'proj-open') return openPreview(msg.task?.project || msg.project || chat.id, chat.title);
    if (act === 'proj-zip') return downloadZip(msg.task?.project || msg.project || chat.id, chat.title);
    if (!msg.task?.id) return;
    if (act === 'agent-opt' || act === 'agent-send' || act === 'agent-auto') return agentAnswer(btn, chat, msg);
    try {
      const r = await agentApi('/api/tasks/' + encodeURIComponent(msg.task.id) + (act === 'agent-cancel' ? '/cancel' : '/retry'), 'POST');
      if (!r.ok) { toast('Сервер ответил HTTP ' + r.status); return; }
      const j = await r.json().catch(() => ({}));
      if (act === 'agent-retry' && !j.retried) { toast('Эту задачу нельзя перезапустить: напиши её заново'); return; }
      if (act === 'agent-retry') { msg.task.status = 'queued'; msg.error = ''; msg.content = ''; saveChats(); renderMessages(); trackTask(chat, msg); }
      else { toast('Останавливаю…'); trackTask(chat, msg); }
    } catch (e) { toast(e.message || 'Нет связи с сервером'); }
  }

  // Ответ на вопрос агента: вариант, свой текст или «Решай сам»
  async function agentAnswer(btn, chat, msg) {
    const t = msg.task, q = t.question, card = btn.closest('.agent-ask'); if (!q || !card) return;
    const act = btn.dataset.a, body = { qid: q.id, text: '', auto: false };
    let shown;
    if (act === 'agent-auto') { body.auto = true; shown = 'Решай сам'; }
    else if (act === 'agent-opt') shown = body.text = String((q.options || [])[+btn.dataset.i] || '');
    else shown = body.text = ($('.ask-in', card).value || '').trim();
    if (!shown) { toast('Выбери вариант или напиши ответ'); return; }
    const lock = v => card.querySelectorAll('button, input').forEach(x => { x.disabled = v; });
    lock(true);
    try {
      const r = await agentApi('/api/tasks/' + encodeURIComponent(t.id) + '/answer', 'POST', body);
      if (r.status === 401) throw new Error(AUTH_MSG);
      if (r.status === 409) { toast('Этот вопрос уже не актуален, обновляю…'); lock(false); trackTask(chat, msg); return; }
      if (!r.ok) throw new Error('Сервер ответил HTTP ' + r.status);
    } catch (e) { toast(e.message || 'Нет связи с сервером'); lock(false); return; }
    t.status = 'queued'; t.question = null; t.draft = ''; t.answered = true;
    t.events = [...(t.events || []), { m: 'Ты: ' + shown, l: 'info' }].slice(-8);
    saveChats(); repaint(chat, msg, false); trackTask(chat, msg);
  }

  /* ---------- Проект агента на сервере: предпросмотр и zip ---------- */
  let previewPid = '', previewTitle = '';
  async function projectFetch(pid, what) {
    const r = await agentApi('/api/projects/' + encodeURIComponent(pid) + '/' + what);
    if (r.status === 401) throw new Error(AUTH_MSG);
    if (r.status === 404) throw new Error(what === 'preview' ? 'В проекте нет html-файла, открывать нечего. Можно скачать .zip' : 'Проект пуст или удалён (без базы данных он пропадает при перезапуске сервера)');
    if (!r.ok) throw new Error('Сервер ответил HTTP ' + r.status);
    return r;
  }
  async function openPreview(pid, title) {
    try {
      const html = await (await projectFetch(pid, 'preview')).text();
      previewPid = pid; previewTitle = title || '';
      $('#previewTitle').textContent = title ? 'Предпросмотр: ' + title.slice(0, 40) : 'Предпросмотр';
      $('#previewFrame').srcdoc = html;
      $('#preview').classList.remove('hidden');
    } catch (e) { toast(e.message || 'Не удалось открыть проект'); }
  }
  function closePreview() { $('#preview').classList.add('hidden'); $('#previewFrame').srcdoc = ''; }   // пустой srcdoc останавливает игру и звук
  async function reloadPreview() {
    try { const html = await (await projectFetch(previewPid, 'preview')).text(); const f = $('#previewFrame'); f.srcdoc = ''; setTimeout(() => { f.srcdoc = html; }, 30); }
    catch (e) { toast(e.message || 'Не удалось обновить'); }
  }
  async function downloadZip(pid, title) {
    try {
      const blob = await (await projectFetch(pid, 'zip')).blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = ((title || '').replace(/[^\wЀ-ӿ-]+/g, '_').replace(/^_+|_+$/g, '') || 'project') + '.zip';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    } catch (e) { toast(e.message || 'Не удалось скачать'); }
  }

  function renderAgentToggle() {
    const b = $('#agentBtn');
    b.classList.toggle('on', agentOn); b.setAttribute('aria-pressed', agentOn);
    $('#agentOpts').classList.toggle('hidden', !agentOn);
    $('#agentAsk').checked = agentAsk;
    document.querySelectorAll('#agentOpts [data-target]').forEach(x => {
      const on = x.dataset.target === agentTarget; x.classList.toggle('on', on); x.setAttribute('aria-pressed', on);
    });
    els.input.placeholder = !agentOn ? 'Напиши задачу…' : agentTarget === 'github' ? 'Что изменить в коде?' : 'Что создать? Например: html-игра';
    $('#hint').textContent = !agentOn ? 'ИИ может ошибаться, проверяй код.'
      : agentTarget === 'github' ? 'Агент не пишет в main: он открывает pull request, а Merge нажимаешь ты в GitHub.'
        : 'Агент сохраняет файлы в проект на сервере: после ответа их можно открыть и скачать .zip.';
  }

  async function githubInfo() {
    if (settings.provider !== 'backend' || !backendUrl() || !settings.token) return null;
    try { const r = await agentApi('/api/status'); return r.ok ? (await r.json()).github || null : null; } catch { return null; }
  }

  async function githubWarn() {
    const g = await githubInfo();
    if (g && !g.connected) toast('GitHub не подключён: в Render → Environment добавь GITHUB_TOKEN (docs/GITHUB.md) или выбери «Проект»');
  }

  async function toggleAgent() {
    agentOn = !agentOn; store.set('gdai.agent', agentOn); renderAgentToggle();
    if (!agentOn) return;
    if (settings.provider !== 'backend' || !backendUrl()) { toast('Агенту нужен твой сервер: выбери «Свой сервер» в Настройках'); return; }
    if (agentTarget === 'github') githubWarn();
  }

  function setTarget(t) {
    agentTarget = t === 'github' ? 'github' : 'project'; store.set('gdai.agentTarget', agentTarget); renderAgentToggle();
    if (agentTarget === 'github') githubWarn();
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
    if (res.status === 401) throw new Error(AUTH_MSG);
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
          local.messages = serverMessages(full.messages, id); changed = true;
        } else if (!local) {
          const rr = await fetch(backendUrl() + '/api/chats/' + encodeURIComponent(id), { headers: authHeaders() });
          if (!rr.ok) continue;
          const full = await rr.json();
          chats.push({ id, title: full.title || rc.title || 'Чат', messages: serverMessages(full.messages, id), created: (rc.created_at || 0) * 1000 || Date.now() });
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

  function serverMessages(list, chatId) {
    return (list || []).map(m => {
      const o = { role: m.role, content: m.content || '', ts: (m.ts || 0) * 1000 || Date.now() };
      if (m.role === 'assistant' && chatId && o.content.includes('Проект сохранён на сервере')) o.project = chatId;   // кнопки «Открыть» и «.zip» и на другом устройстве
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
    if (name === 'limits') startLimits(); else stopLimits();
  }

  let toastTimer = null;
  function toast(text) {
    els.toast.textContent = text;
    els.toast.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => els.toast.classList.add('hidden'), Math.min(7000, Math.max(1800, text.length * 55)));
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
    $('#sEmail').value = settings.email || '';
    $('#sCode').value = ''; $('#codeRow').classList.add('hidden'); $('#codeBtns').classList.add('hidden');
    authMsg(''); renderAuth(); loadAuthInfo();
    $('#appVersion').textContent = 'Версия интерфейса: ' + VERSION;
    const gs = $('#ghStatus'); gs.textContent = '';
    githubInfo().then(g => { if (g) gs.textContent = g.connected ? 'GitHub для агента: подключён (' + g.repos.join(', ') + ')' : 'GitHub для агента: не подключён. Добавь GITHUB_TOKEN в Render → Environment.'; });
    hidePop(); closeMenu(); showTab('conn');       // на телефоне боковое меню не должно оставаться открытым под окном
    els.modal.classList.remove('hidden');
  }

  function closeSettings() { els.modal.classList.add('hidden'); stopLimits(); }

  function saveSettings() {
    const temp = parseFloat($('#sTemp').value);
    settings = {
      backend: $('#sBackend').value.trim(),
      token: $('#sToken').value.trim(),
      email: settings.email || '',
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
        '\nGitHub для агента: ' + (st.github?.connected ? 'подключён (' + st.github.repos.join(', ') + ')' : 'не подключён (нужен GITHUB_TOKEN)') +
        (st.warnings?.length ? '\n⚠ ' + st.warnings.join('\n⚠ ') : '');
    } catch { out.textContent = '✗ Не удалось подключиться (адрес / CORS?)'; }
  }

  /* ---------- Вход по почте ---------- */
  const isSession = t => /^gdai_/.test(t || '');
  const loggedEmail = () => (isSession(settings.token) && settings.email) || '';
  const formBase = () => ($('#sBackend').value || '').trim().replace(/\/+$/, '');
  function authMsg(text, bad) { const o = $('#authMsg'); o.textContent = text || ''; o.classList.toggle('bad', !!bad); }

  function renderAuth() {
    const who = loggedEmail();
    $('#authIn').classList.toggle('hidden', !!who);
    $('#authOut').classList.toggle('hidden', !who);
    $('#authWho').textContent = who;
    $('#authHint').textContent = !who && $('#sToken').value.trim() && !isSession($('#sToken').value.trim())
      ? 'Токен доступа уже введён: эта почта привяжется к серверу, и дальше токен не понадобится.' : '';
  }

  async function authCall(path, body) {
    const base = formBase();
    if (!base) throw new Error('Сначала укажи адрес своего сервера выше');
    if (location.protocol === 'https:' && base.startsWith('http:')) throw new Error('Сайт на HTTPS не может обращаться к http-серверу. Нужен адрес https://…');
    const tok = $('#sToken').value.trim();
    const go = () => fetch(base + path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(tok ? { Authorization: 'Bearer ' + tok } : {}) }, body: JSON.stringify(body) });
    let r;
    try { r = await go(); }
    catch {
      authMsg('Сервер просыпается, это займёт до минуты…');
      if (!(await wakeServerAt(base))) throw new Error('Сервер не ответил. Проверь адрес и CORS_ORIGINS в Render.');
      r = await go();
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(String(j.detail || j.error || j.message || 'Сервер ответил HTTP ' + r.status));
    return j;
  }

  let resendTimer = null;
  function holdResend(sec) {
    const btn = $('#mailSend'), label = btn.querySelector('span');
    clearInterval(resendTimer);
    let left = sec; btn.disabled = true;
    const tick = () => {
      if (left <= 0) { clearInterval(resendTimer); btn.disabled = false; label.textContent = 'Отправить код ещё раз'; return; }
      label.textContent = `Ещё раз через ${left} с`; left--;
    };
    tick(); resendTimer = setInterval(tick, 1000);
  }

  async function mailSend() {
    const email = $('#sEmail').value.trim().toLowerCase();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { authMsg('Введи почту целиком, например you@gmail.com', true); return; }
    const btn = $('#mailSend'); btn.disabled = true; authMsg('Отправляю код…');
    try {
      const j = await authCall('/api/auth/request', { email });
      $('#codeRow').classList.remove('hidden'); $('#codeBtns').classList.remove('hidden'); $('#sCode').focus();
      authMsg(j.link ? `Код отправлен на ${email}. Введи его, чтобы привязать почту к серверу. Загляни и в «Спам».`
        : `Если эта почта разрешена на сервере, код уже отправлен на ${email}. Он действует 10 минут. Загляни и в «Спам».`);
      holdResend(45);
    } catch (e) { authMsg(e.message, true); btn.disabled = false; }
  }

  async function mailVerify() {
    const email = $('#sEmail').value.trim().toLowerCase(), code = $('#sCode').value.replace(/\D/g, '');
    if (code.length !== 6) { authMsg('Код — 6 цифр из письма', true); return; }
    const btn = $('#mailVerify'); btn.disabled = true; authMsg('Проверяю код…');
    try {
      const j = await authCall('/api/auth/verify', { email, code });
      settings = { ...settings, provider: 'backend', backend: formBase(), token: j.token, email: j.email || email };
      store.set('gdai.settings', settings);
      $('#sProvider').value = 'backend'; syncProviderFields(); $('#sToken').value = j.token;
      $('#sCode').value = ''; $('#codeRow').classList.add('hidden'); $('#codeBtns').classList.add('hidden');
      clearInterval(resendTimer); $('#mailSend').disabled = false; $('#mailSend span').textContent = 'Получить код';
      authMsg(''); renderAuth(); renderBadge(); $('#tokenBox').open = false;
      toast(j.linked ? 'Почта привязана, вы вошли' : 'Вы вошли');
      syncFromServer(); checkServer(); resumeTasks();
    } catch (e) { authMsg(e.message, true); }
    finally { btn.disabled = false; }
  }

  async function mailLogout() {
    const base = backendUrl() || formBase();
    if (base && isSession(settings.token)) { try { await fetch(base + '/api/auth/logout', { method: 'POST', headers: authHeaders() }); } catch { /* сервер спит: токен всё равно забываем здесь */ } }
    settings = { ...settings, token: '', email: '' }; store.set('gdai.settings', settings);
    $('#sToken').value = ''; renderAuth(); renderBadge(); checkServer(); toast('Вы вышли');
  }

  async function loadAuthInfo() {
    const base = formBase(), note = $('#authHint');
    $('#tokenBox').open = !loggedEmail() && !!$('#sToken').value.trim();
    if (!base || $('#sProvider').value !== 'backend') return;
    try {
      const r = await fetch(base + '/api/auth/info'); if (!r.ok) return;
      if (!(await r.json()).email_login) { note.textContent = 'На сервере вход по почте ещё не включён: добавь MAIL_API_KEY в Render (docs/AUTH.md). Пока входи по токену ниже.'; $('#tokenBox').open = true; }
    } catch { /* сервер спит: узнаем при нажатии «Получить код» */ }
  }

  /* ---------- Лимиты ---------- */
  let limTimer = null, limTicks = 0, limAt = 0;
  const cd = (sec) => `<b class="cd" data-end="${limAt + sec * 1000}">${fmtDur(sec)}</b>`;

  function limRow(label, k) {
    const frac = k.limit ? Math.max(0, Math.min(1, k.remaining / k.limit)) : 0;
    const sub = k.reset_passed ? 'лимит уже восстановился; точная цифра появится после следующего ответа ИИ'
      : k.reset_in_s == null ? '' : 'сброс через ' + cd(k.reset_in_s);
    return `<div class="lim-row"><div class="lim-top"><span>${label}</span><span>${k.reset_passed ? '≥ ' : ''}<b>${fmtNum(k.remaining)}</b> из ${fmtNum(k.limit)}</span></div>
      <div class="meter${frac < 0.2 && !k.reset_passed ? ' low' : ''}"><i style="width:${(frac * 100).toFixed(1)}%"></i></div><div class="lim-sub">${sub}</div></div>`;
  }

  function renderLimits(d) {
    limAt = Date.now();
    const parts = (d.providers || []).map(p => {
      const rows = (p.requests ? limRow('Запросов осталось', p.requests) : '') + (p.tokens ? limRow('Токенов осталось', p.tokens) : '');
      const body = p.data ? (rows || '<p class="lim-sub">Сервис не сообщает лимиты в ответах.</p>')
        : '<p class="lim-sub">Данных пока нет: отправь любое сообщение через сервер, и после ответа ИИ здесь появятся остатки.</p>';
      return `<div class="lim-card"><div class="lim-title"><strong>${esc(p.label)}</strong><span class="muted">${esc(p.model || '')}</span></div>${body}
        ${p.retry_in_s ? `<p class="lim-warn">Сервис просит подождать ещё ${cd(p.retry_in_s)}</p>` : ''}
        ${p.data ? `<p class="lim-sub">данные обновлены ${fmtDur(p.seen_ago_s)} назад</p>` : ''}</div>`;
    });
    if (!parts.length) parts.push('<div class="lim-card"><p class="lim-sub">Нет настроенных моделей (проверь GPU_BASE_URL и ключ в Render).</p></div>');
    const u = d.usage || {}, at = new Date(limAt + (u.reset_in_s || 0) * 1000);
    const days = (u.last_days || []).slice(0, 7).map(x => `<div>${esc(x.day)} — ${fmtNum(x.requests)} запр., ≈ ${fmtNum(x.approx_tokens)} ток.</div>`).join('');
    parts.push(`<div class="lim-card"><div class="lim-title"><strong>Расход через этот сервер</strong><span class="muted">сегодня (по UTC)</span></div>
      <div class="lim-row"><div class="lim-top"><span>Запросов</span><b>${fmtNum(u.requests)}</b></div></div>
      <div class="lim-row"><div class="lim-top"><span>Токенов (оценка)</span><b>≈ ${fmtNum(u.approx_tokens)}</b></div></div>
      <div class="lim-sub">новый счёт через ${cd(u.reset_in_s || 0)} (в ${at.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })} по твоему времени)</div>
      ${days ? `<details><summary class="lim-sub">Последние дни</summary><div class="lim-days">${days}</div></details>` : ''}</div>
      <p class="lim-sub">Токены считаются приблизительно (по длине текста). Запросы у бесплатных сервисов обычно считаются за сутки, токены — за минуту.</p>`);
    $('#limBody').innerHTML = parts.join('');
  }

  async function loadLimits() {
    const box = $('#limBody');
    if (settings.provider !== 'backend' || !backendUrl()) { box.innerHTML = '<p class="muted">Лимиты показывает твой сервер: выбери «Свой сервер», укажи адрес, войди и нажми «Сохранить».</p>'; return; }
    if (!settings.token) { box.innerHTML = '<p class="muted">Сначала войди по почте (вкладка «Подключение») или укажи токен.</p>'; return; }
    try {
      const r = await agentApi('/api/limits');
      if (r.status === 401) { box.innerHTML = `<p class="muted bad">${esc(AUTH_MSG)}</p>`; return; }
      if (r.status === 404) { box.innerHTML = '<p class="muted">Сервер старой версии, лимитов нет: обнови его на Render (Manual Deploy).</p>'; return; }
      if (!r.ok) { box.innerHTML = `<p class="muted bad">Сервер ответил HTTP ${r.status}</p>`; return; }
      renderLimits(await r.json());
    } catch (e) { box.innerHTML = `<p class="muted bad">${esc(e.message || 'Нет связи с сервером')}</p>`; }
  }

  function tickLimits() {
    document.querySelectorAll('#limBody .cd').forEach(el => {
      const left = Math.round((+el.dataset.end - Date.now()) / 1000);
      el.textContent = left > 0 ? fmtDur(left) : 'сейчас';
    });
    if (++limTicks % 20 === 0) loadLimits();               // раз в 20 секунд подтягиваем свежие цифры
  }
  function startLimits() { stopLimits(); limTicks = 0; $('#limBody').innerHTML = '<p class="muted">Загружаю…</p>'; loadLimits(); limTimer = setInterval(tickLimits, 1000); }
  function stopLimits() { clearInterval(limTimer); limTimer = null; }

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

  $('#agentBtn').onclick = toggleAgent;
  $('#agentOpts').addEventListener('click', e => { const b = e.target.closest('[data-target]'); if (b) setTarget(b.dataset.target); });
  $('#agentAsk').addEventListener('change', e => { agentAsk = e.target.checked; store.set('gdai.agentAsk', agentAsk); });
  $('#mailSend').onclick = mailSend;
  $('#mailVerify').onclick = mailVerify;
  $('#mailLogout').onclick = mailLogout;
  $('#sCode').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); mailVerify(); } });
  $('#sEmail').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); mailSend(); } });
  $('#sToken').addEventListener('input', renderAuth);
  let infoTimer = null;     // адрес сервера вводят уже после открытия настроек: проверяем, умеет ли он почту, когда ввод закончен
  const recheckAuth = () => { clearTimeout(infoTimer); infoTimer = setTimeout(loadAuthInfo, 600); };
  $('#sBackend').addEventListener('input', recheckAuth);
  $('#sProvider').addEventListener('change', recheckAuth);
  $('#limRefresh').onclick = loadLimits;
  $('#closePreview').onclick = closePreview;
  $('#previewReload').onclick = reloadPreview;
  $('#preview').addEventListener('click', e => { if (e.target === $('#preview')) closePreview(); });
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
    const ab = e.target.closest('[data-a^="agent-"], [data-a^="proj-"]');
    if (ab) { agentAction(ab); return; }
    const t = e.target.closest('.msg-tools button'); if (!t) return;
    if (t.dataset.a === 'regen') regenerate();
    if (t.dataset.a === 'copy') {
      const m = active()?.messages[+t.closest('.msg').dataset.idx];
      if (m) copyText(m.content).then(() => flash(t));
    }
  });

  // набранный, но ещё не отправленный ответ агенту не теряется при перерисовке
  els.messages.addEventListener('input', e => {
    const inp = e.target.closest('.ask-in'); if (!inp) return;
    const m = active()?.messages[+inp.closest('.msg').dataset.idx]; if (m?.task) m.task.draft = inp.value;
  });
  els.messages.addEventListener('keydown', e => {
    if (e.key !== 'Enter' || e.isComposing || !e.target.closest('.ask-in')) return;
    e.preventDefault(); e.target.closest('.agent-ask').querySelector('[data-a=agent-send]').click();
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
    if (e.key === 'Escape') { hidePop(); closeMenu(); closeSettings(); closePreview(); }
  });

  /* ---------- Init ---------- */
  if (!chats.find(c => c.id === activeId)) activeId = chats[0]?.id || null;
  els.app.classList.toggle('side-collapsed', !!store.get('gdai.sideCollapsed', false));
  applyTheme(); applyFont(); renderBadge(); renderChatList(); renderMessages(); renderChips();
  renderAgentToggle(); resumeTasks();
  document.addEventListener('visibilitychange', () => { if (!document.hidden) resumeTasks(); });
  syncFromServer(); checkServer();
})();

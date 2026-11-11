(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* quota / private mode */ } },
  };

  const DEFAULTS = {
    backend: '', model: 'default', temperature: 0.7, system: '', theme: 'auto',
    fontSize: 'normal', enterSend: window.matchMedia('(hover: hover)').matches,
  };
  const MAX_FILE = 200 * 1024; // 200 KB per attached text file

  let settings = { ...DEFAULTS, ...store.get('gdai.settings', {}) };
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
      return `<div class="code"><div class="code-head"><span>${esc(lang || 'code')}</span><button class="copy" type="button">Копировать</button></div><pre><code>${esc(code)}</code></pre></div>`;
    }).join('');
  }

  /* ---------- Rendering ---------- */
  function renderChatList() {
    const q = els.search.value.trim().toLowerCase();
    const items = chats.filter(c => !q || c.title.toLowerCase().includes(q));
    els.list.innerHTML = '';
    if (!items.length) { els.list.innerHTML = '<div class="empty-list">Чатов пока нет</div>'; return; }
    for (const c of items) {
      const row = document.createElement('div');
      row.className = 'chat-item' + (c.id === activeId ? ' active' : '');
      row.dataset.id = c.id;
      row.innerHTML = `<span class="name">${esc(c.title)}</span>
        <button class="act" data-a="rename" title="Переименовать" aria-label="Переименовать">✎</button>
        <button class="act" data-a="delete" title="Удалить" aria-label="Удалить">🗑</button>`;
      els.list.appendChild(row);
    }
  }

  function msgNode(m, idx, isLast) {
    const el = document.createElement('div');
    el.className = 'msg ' + m.role;
    el.dataset.idx = idx;
    const tools = m.role === 'assistant'
      ? `<div class="msg-tools"><button data-a="copy" type="button">Копировать</button>${isLast ? '<button data-a="regen" type="button">↻ Заново</button>' : ''}</div>` : '';
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
      w.innerHTML = '<h1>Чем помочь в разработке игры?</h1><p>Код, механики, баланс, архитектура — спрашивай.</p><div class="suggest"></div>';
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
      c.innerHTML = `${esc(a.name)}<button type="button" aria-label="Убрать" data-i="${i}">✕</button>`;
      els.chips.appendChild(c);
    });
  }

  function renderBadge() {
    const online = !!backendUrl();
    els.badge.textContent = online ? 'сервер' : 'демо';
    els.badge.classList.toggle('online', online);
    els.badge.title = online ? backendUrl() : 'Бэкенд не задан — работает демо-режим';
  }

  function setBusy(b) {
    busy = b;
    els.send.classList.toggle('stop', b);
    els.send.textContent = b ? '■' : '➤';
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
    if (activeId === id) activeId = chats[0]?.id || null;
    saveChats(); renderChatList(); renderMessages();
  }

  function renameChat(id) {
    const c = chats.find(x => x.id === id); if (!c) return;
    const t = prompt('Название чата', c.title);
    if (t && t.trim()) { c.title = t.trim().slice(0, 80); saveChats(); renderChatList(); if (id === activeId) els.title.textContent = c.title; }
  }

  function exportChat() {
    const c = active(); if (!c || !c.messages.length) return;
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
      if (backendUrl()) await streamBackend(chat, history, msg, update, abortCtl.signal);
      else await streamDemo(history, msg, update, abortCtl.signal);
    } catch (e) {
      if (e.name !== 'AbortError') msg.error = 'Ошибка: ' + (e.message || e);
    } finally {
      setBusy(false); abortCtl = null;
      if (!msg.content && !msg.error) msg.content = '_(остановлено)_';
      saveChats(); renderMessages();
    }
  }

  async function streamBackend(chat, history, msg, update, signal) {
    const res = await fetch(backendUrl() + '/api/chat', {
      method: 'POST', signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chat.id, messages: history, model: settings.model, temperature: settings.temperature, system: settings.system || null }),
    });
    if (!res.ok) throw new Error('Сервер ответил HTTP ' + res.status);
    const reader = res.body.getReader(), dec = new TextDecoder();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      msg.content += dec.decode(value, { stream: true });
      update();
    }
  }

  // Демо-режим: работает без сервера (например, на GitHub Pages)
  async function streamDemo(history, msg, update, signal) {
    const last = history[history.length - 1]?.content || '';
    const preview = last.length > 200 ? last.slice(0, 200) + '…' : last;
    const text = `Сейчас включён **демо-режим**: бэкенд не подключён, поэтому настоящего ИИ-ответа нет.\n\nТвоё сообщение получено:\n\n> ${preview.replace(/\n/g, ' ')}\n\nЧтобы подключить ИИ, запусти сервер (\`uvicorn main:app\` в папке \`backend\`) и укажи его адрес в **Настройках**.\n\n\`\`\`js\n// пример: игровой цикл\nfunction loop(t) {\n  update(t);\n  render();\n  requestAnimationFrame(loop);\n}\nrequestAnimationFrame(loop);\n\`\`\``;
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
    return `\n\n---\n**Текущие настройки:** модель — \`${settings.model}\`, температура — \`${settings.temperature}\`, системная инструкция — ${settings.system.trim() ? 'задана' : 'не задана'}.`;
  }

  function applyTheme() {
    const t = settings.theme;
    if (t === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', t);
  }

  function cycleTheme() {
    const order = ['auto', 'dark', 'light'];
    settings.theme = order[(order.indexOf(settings.theme) + 1) % order.length];
    store.set('gdai.settings', settings); applyTheme();
    toast('Тема: ' + { auto: 'как в системе', dark: 'тёмная', light: 'светлая' }[settings.theme]);
  }

  function openSettings() {
    $('#sBackend').value = settings.backend;
    $('#sModel').value = settings.model;
    $('#sTemp').value = settings.temperature;
    $('#sTempVal').textContent = settings.temperature;
    $('#sSystem').value = settings.system;
    $('#sTheme').value = settings.theme;
    $('#sFont').value = settings.fontSize;
    $('#sEnter').checked = !!settings.enterSend;
    $('#testResult').textContent = '';
    els.modal.classList.remove('hidden');
  }

  function closeSettings() { els.modal.classList.add('hidden'); }

  function saveSettings() {
    const temp = parseFloat($('#sTemp').value);
    settings = {
      backend: $('#sBackend').value.trim(),
      model: $('#sModel').value.trim() || 'default',
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
  }

  async function testBackend() {
    const out = $('#testResult'), url = $('#sBackend').value.trim().replace(/\/+$/, '');
    if (!url) { out.textContent = 'Адрес не указан — будет демо-режим'; return; }
    if (location.protocol === 'https:' && url.startsWith('http:')) {
      out.textContent = '✗ Сайт на HTTPS не может обращаться к http-серверу. Нужен адрес https://…';
      return;
    }
    out.textContent = 'Проверяю…';
    try {
      const r = await fetch(url + '/api/health');
      const j = await r.json();
      out.textContent = r.ok ? '✓ Связь есть' + (j.provider ? ` (провайдер: ${j.provider})` : '') : 'Ошибка HTTP ' + r.status;
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
  $('#topSettings').onclick = openSettings;
  $('#backdrop').onclick = closeMenu;
  $('#themeBtn').onclick = cycleTheme;
  $('#exportBtn').onclick = exportChat;
  $('#openSettings').onclick = openSettings;
  els.search.addEventListener('input', renderChatList);

  els.list.addEventListener('click', e => {
    const row = e.target.closest('.chat-item'); if (!row) return;
    const a = e.target.closest('.act');
    if (a) { e.stopPropagation(); a.dataset.a === 'delete' ? deleteChat(row.dataset.id) : renameChat(row.dataset.id); }
    else selectChat(row.dataset.id);
  });

  els.messages.addEventListener('click', e => {
    const copy = e.target.closest('.copy');
    if (copy) {
      const code = copy.closest('.code').querySelector('code').textContent;
      navigator.clipboard?.writeText(code).then(() => { copy.textContent = 'Скопировано'; setTimeout(() => copy.textContent = 'Копировать', 1200); });
      return;
    }
    const t = e.target.closest('.msg-tools button'); if (!t) return;
    if (t.dataset.a === 'regen') regenerate();
    if (t.dataset.a === 'copy') {
      const m = active()?.messages[+t.closest('.msg').dataset.idx];
      if (m) navigator.clipboard?.writeText(m.content).then(() => { t.textContent = 'Скопировано'; setTimeout(() => t.textContent = 'Копировать', 1200); });
    }
  });

  els.messages.addEventListener('scroll', () => els.toBottom.classList.toggle('hidden', nearBottom()));
  els.toBottom.onclick = () => scrollBottom(true);

  $('#sTemp').addEventListener('input', e => { $('#sTempVal').textContent = e.target.value; });
  $('#testBackend').onclick = testBackend;
  $('#clearAll').onclick = () => {
    if (!confirm('Удалить ВСЕ чаты? Это нельзя отменить.')) return;
    abortCtl?.abort(); chats = []; activeId = null; saveChats(); renderChatList(); renderMessages();
    closeSettings(); toast('Все чаты удалены');
  };
  $('#saveSettings').onclick = saveSettings;
  $('#cancelSettings').onclick = closeSettings;
  els.modal.addEventListener('click', e => { if (e.target === els.modal) closeSettings(); });

  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); newChat(); }
    if (e.key === 'Escape') { closeMenu(); closeSettings(); }
  });

  /* ---------- Init ---------- */
  if (!chats.find(c => c.id === activeId)) activeId = chats[0]?.id || null;
  els.app.classList.toggle('side-collapsed', !!store.get('gdai.sideCollapsed', false));
  applyTheme(); applyFont(); renderBadge(); renderChatList(); renderMessages(); renderChips();
})();

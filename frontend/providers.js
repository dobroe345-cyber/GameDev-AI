/* GameDev AI — подключение к настоящим ИИ-моделям прямо из браузера.
 * Ключ хранится только в localStorage этого браузера и отправляется только выбранному провайдеру.
 */
(() => {
  'use strict';

  const DEFAULT_SYSTEM = [
    'Ты — GameDev AI, помощник по разработке игр.',
    'Помогаешь с геймдизайном, механиками, балансом, архитектурой и кодом (JavaScript/Canvas, Unity C#, Godot GDScript, Unreal C++, Python, C++).',
    'Отвечай по-русски, если пользователь не просит иначе.',
    'Будь конкретным: давай рабочий код в блоках с указанием языка и коротко объясняй ключевые решения.',
    'Если не хватает данных — задай один уточняющий вопрос.',
    'Не выдумывай функции и API: если не уверен, так и скажи.',
  ].join('\n');

  // Температура 0.7 = «по умолчанию»: тогда параметр не отправляем,
  // потому что часть новых моделей не принимает нестандартные значения.
  const tempFor = cfg => (Math.abs(Number(cfg.temperature) - 0.7) < 1e-9 ? undefined : Number(cfg.temperature));

  // Приводит историю к виду, который принимают API: только user/assistant,
  // без пустых сообщений, роли чередуются, первой идёт реплика пользователя.
  function clean(messages) {
    const out = [];
    for (const m of messages) {
      const text = (m.content || '').trim();
      if (!text || (m.role !== 'user' && m.role !== 'assistant')) continue;
      if (text === '_(остановлено)_') continue;
      const last = out[out.length - 1];
      if (last && last.role === m.role) last.content += '\n\n' + text;
      else out.push({ role: m.role, content: text });
    }
    let tail = out.slice(-40);
    while (tail.length && tail[0].role !== 'user') tail.shift();
    return tail;
  }

  async function doFetch(url, init, who) {
    try {
      return await fetch(url, init);
    } catch (e) {
      if (e.name === 'AbortError') throw e;
      throw new Error(`${who}: не удалось подключиться. Проверь интернет, адрес и то, что сервис разрешает запросы из браузера (CORS).`);
    }
  }

  async function httpError(res, who) {
    let detail = '';
    try {
      const t = await res.text();
      try { const j = JSON.parse(t); detail = j.error?.message || j.message || j.detail || t; } catch { detail = t; }
    } catch { /* тело недоступно */ }
    const hints = {
      400: 'Запрос отклонён — проверь название модели и настройки.',
      401: 'Неверный или отсутствующий API-ключ.',
      403: 'Доступ запрещён — проверь ключ и права.',
      404: 'Не найдено — проверь адрес и название модели.',
      429: 'Слишком много запросов или закончился лимит.',
    };
    return new Error(`${who}: HTTP ${res.status}. ${hints[res.status] || ''} ${String(detail).slice(0, 300)}`.replace(/\s+/g, ' ').trim());
  }

  // Разбор потока Server-Sent Events: отдаёт содержимое строк data:
  async function* sseData(res) {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let m;
      while ((m = /\r?\n\r?\n/.exec(buf))) {
        const raw = buf.slice(0, m.index);
        buf = buf.slice(m.index + m[0].length);
        const data = raw.split(/\r?\n/).filter(l => l.startsWith('data:')).map(l => l.slice(5).trimStart()).join('\n');
        if (data) yield data;
      }
    }
  }

  async function* anthropic(cfg) {
    if (!cfg.apiKey) throw new Error('Не указан API-ключ. Открой Настройки и вставь ключ Anthropic.');
    const body = {
      model: cfg.model || 'claude-haiku-5-5',
      max_tokens: 4096,
      stream: true,
      messages: clean(cfg.messages),
    };
    if (cfg.system) body.system = cfg.system;
    const t = tempFor(cfg);
    if (t !== undefined) body.temperature = Math.min(t, 1);

    const res = await doFetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      signal: cfg.signal,
      headers: {
        'content-type': 'application/json',
        'x-api-key': cfg.apiKey,
        'anthropic-version': '2023-06-01',
        'anthropic-dangerous-direct-browser-access': 'true',
      },
      body: JSON.stringify(body),
    }, 'Anthropic');
    if (!res.ok) throw await httpError(res, 'Anthropic');

    for await (const data of sseData(res)) {
      let ev;
      try { ev = JSON.parse(data); } catch { continue; }
      if (ev.type === 'content_block_delta' && ev.delta?.type === 'text_delta') yield ev.delta.text;
      else if (ev.type === 'error') throw new Error('Anthropic: ' + (ev.error?.message || 'ошибка потока'));
    }
  }

  async function* openaiCompatible(cfg) {
    if (!cfg.model) throw new Error('Не указана модель. Открой Настройки → Модель.');
    const base = (cfg.baseUrl || 'https://api.openai.com/v1').replace(/\/+$/, '');
    const messages = clean(cfg.messages);
    if (cfg.system) messages.unshift({ role: 'system', content: cfg.system });
    const body = { model: cfg.model, messages, stream: true };
    const t = tempFor(cfg);
    if (t !== undefined) body.temperature = t;

    const headers = { 'content-type': 'application/json' };
    if (cfg.apiKey) headers.authorization = 'Bearer ' + cfg.apiKey;

    const res = await doFetch(base + '/chat/completions', {
      method: 'POST', signal: cfg.signal, headers, body: JSON.stringify(body),
    }, 'ИИ-сервис');
    if (!res.ok) throw await httpError(res, 'ИИ-сервис');

    for await (const data of sseData(res)) {
      if (data === '[DONE]') return;
      let ev;
      try { ev = JSON.parse(data); } catch { continue; }
      if (ev.error) throw new Error('ИИ-сервис: ' + (ev.error.message || 'ошибка потока'));
      const delta = ev.choices?.[0]?.delta?.content;
      if (delta) yield delta;
    }
  }

  function stream(cfg) {
    if (cfg.provider === 'anthropic') return anthropic(cfg);
    if (cfg.provider === 'openai') return openaiCompatible(cfg);
    throw new Error('Неизвестный провайдер: ' + cfg.provider);
  }

  window.GDAI = { DEFAULT_SYSTEM, stream };
})();

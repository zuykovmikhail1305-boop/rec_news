// Чат с ассистентом: история — GET /api/chat, вопрос — POST /api/chat (ответ строится до минуты).
(() => {
  const root = document.querySelector('[data-chat]');
  if (!root) return;
  const userId = root.dataset.userId;
  const body = root.querySelector('[data-chat-body]');
  const form = root.querySelector('[data-chat-form]');
  const input = form.querySelector('input');
  const sugg = root.querySelector('[data-chat-sugg]');
  const api = '/api/chat?user_id=' + encodeURIComponent(userId);
  let loaded = false, busy = false, newDialog = false;

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  };
  const scroll = () => { body.scrollTop = body.scrollHeight; };

  function userMsg(text) { body.append(el('div', 'msg me', text)); }

  function botMsg(answer) {
    const msg = el('div', 'msg bot');
    if (answer.clarify) msg.append(el('p', '', answer.clarify));
    if (answer.sections && answer.sections.length) {
      for (const section of answer.sections) {
        const label = section.origin === 'bank' ? 'Позиция Альфа-Банка' : 'Вывод AI';
        msg.append(el('div', 'ml ' + (section.origin === 'bank' ? 'b' : 'a'), section.title || label));
        const p = el('p', '', section.text);
        if (section.sources && section.sources.length) p.append(el('span', 'chat-refs', ' [' + section.sources.join(', ') + ']'));
        msg.append(p);
      }
    } else if (!answer.clarify) {
      msg.append(el('p', '', answer.text || 'Ответ не получен.'));
    }
    if (answer.sources && answer.sources.length) {
      const list = el('div', 'dis');
      list.append(el('b', '', 'Источники'));
      for (const source of answer.sources) {
        const row = el('div', 'chat-src');
        const name = source.id + ' · ' + (source.title || '') + (source.source ? ' — ' + source.source : '');
        if (source.url && /^https?:\/\//.test(source.url)) {
          const link = el('a', '', name);
          link.href = source.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
          row.append(link);
        } else row.textContent = name;
        list.append(row);
      }
      msg.append(list);
    }
    msg.append(el('div', 'dis', 'Не является индивидуальной инвестиционной рекомендацией.'));
    body.append(msg);
  }

  function errorMsg(text) { body.append(el('div', 'msg bot chat-error', text)); }

  async function loadHistory() {
    loaded = true;
    try {
      const response = await fetch(api, { headers: { Accept: 'application/json' } });
      if (!response.ok) return;
      const history = await response.json();
      for (const turn of history.turns || []) {
        if (turn.role === 'user') userMsg(turn.text);
        else if (turn.answer) botMsg(turn.answer);
        else body.append(el('div', 'msg bot', turn.text));
      }
      if ((history.turns || []).length) sugg.classList.add('hidden');
      scroll();
    } catch (_) { /* история — не обязательна */ }
  }

  async function send(text) {
    text = text.trim();
    if (!text || busy) return;
    busy = true;
    sugg.classList.add('hidden');
    userMsg(text);
    const typing = el('div', 'msg bot');
    typing.innerHTML = '<span class="typing"><i></i><i></i><i></i></span> Ассистент готовит ответ…';
    body.append(typing);
    scroll();
    input.value = '';
    input.disabled = true;
    try {
      const response = await fetch(api, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ message: text, new_dialog: newDialog }),
      });
      const data = await response.json().catch(() => ({}));
      typing.remove();
      if (response.ok) { botMsg(data); newDialog = false; }
      else errorMsg(data.detail || 'Ассистент не ответил. Попробуйте ещё раз.');
    } catch (_) {
      typing.remove();
      errorMsg('Нет связи с сервером. Попробуйте ещё раз.');
    } finally {
      busy = false;
      input.disabled = false;
      input.focus();
      scroll();
    }
  }

  document.querySelector('[data-chat-open]').addEventListener('click', () => {
    root.classList.remove('hidden');
    if (!loaded) loadHistory();
    input.focus();
  });
  root.querySelector('[data-chat-close]').addEventListener('click', () => root.classList.add('hidden'));
  root.querySelector('[data-chat-new]').addEventListener('click', () => {
    if (busy) return;
    newDialog = true;
    body.replaceChildren();
    sugg.classList.remove('hidden');
  });
  form.addEventListener('submit', (event) => { event.preventDefault(); send(input.value); });
  sugg.addEventListener('click', (event) => { if (event.target.tagName === 'BUTTON') send(event.target.textContent); });
})();

(() => {
  'use strict';
  const feedRoot = document.querySelector('[data-insights-root]');
  const detailRoot = document.querySelector('[data-insight-detail]');
  if (!feedRoot && !detailRoot) return;
  const root = feedRoot || detailRoot;
  const expectedUser = root.dataset.userId;
  const requests = new Set();
  let timer = null, active = true, busy = false, polls = 0;
  const message = document.querySelector(feedRoot ? '[data-insights-message]' : '[data-card-message]');
  const retryButton = root.querySelector('[data-retry-insights]');
  const show = text => { if (active && message) message.textContent = text || ''; };

  async function request(url, options = {}) {
    const controller = new AbortController();
    requests.add(controller);
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store',
        ...options, signal: controller.signal});
      if (response.status === 401) { window.location.assign('/login'); return null; }
      const data = await response.json();
      if (!active) return null;
      if (data.user_id && data.user_id !== expectedUser) { window.location.reload(); return null; }
      return {response, data};
    } finally { clearTimeout(timeout); requests.delete(controller); }
  }

  function schedule(poll) {
    clearTimeout(timer);
    if (!active || !poll) return;
    if (++polls > 180) { show('Подготовка занимает больше времени. Обновите страницу немного позже.'); return; }
    timer = setTimeout(() => update().catch(() => show('Не удалось обновить данные. Обновите страницу или попробуйте ещё раз.')), 5000);
  }

  async function update() {
    const url = feedRoot ? '/api/insights?offset=' + encodeURIComponent(root.dataset.offset || '0') :
      '/api/insights/' + encodeURIComponent(root.dataset.newsId);
    const result = await request(url);
    if (!result) return;
    const {response, data} = result;
    if (!response.ok) {
      show(data.detail || data.error || 'Не удалось обновить данные. Попробуйте ещё раз.');
      return;
    }
    if (data.html) root.querySelector(feedRoot ? '[data-insights-feed]' : '[data-insight-card]').innerHTML = data.html;
    schedule(data.poll);
  }

  async function generate() {
    if (busy || !active) return;
    clearTimeout(timer); polls = 0; busy = true;
    if (retryButton) retryButton.disabled = true;
    show('Проверяем готовность инсайтов…');
    try {
      const body = new URLSearchParams({csrf: root.dataset.csrf});
      const result = await request('/api/insights/generate', {method: 'POST', body});
      if (!result) return;
      if (!result.response.ok) show(result.data.detail || 'Не удалось запустить подготовку.');
      else show('');
      await update();
    } catch (error) {
      if (active) show('Не удалось связаться с сервисом. Нажмите «Обновить», чтобы повторить.');
    } finally {
      busy = false;
      if (retryButton && active) retryButton.disabled = false;
    }
  }

  window.addEventListener('pagehide', () => {
    active = false; clearTimeout(timer);
    requests.forEach(controller => controller.abort());
  });
  window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
  if (feedRoot) {
    retryButton.addEventListener('click', generate);
    generate();
  } else if (root.dataset.poll === 'true') schedule(true);
})();

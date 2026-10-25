(() => {
  'use strict';
  const feedRoot = document.querySelector('[data-insights-root]');
  const detailRoot = document.querySelector('[data-insight-detail]');
  if (!feedRoot && !detailRoot) return;
  const root = feedRoot || detailRoot;
  const expectedUser = root.dataset.userId;
  const requests = new Set();
  let timer = null, active = true, busy = false, polls = 0, needsGeneration = true;
  const message = document.querySelector(feedRoot ? '[data-insights-message]' : '[data-card-message]');
  const retryButton = root.querySelector('[data-retry-insights]');
  const show = text => { if (active && message) message.textContent = text || ''; };

  async function request(url, options = {}) {
    const controller = new AbortController();
    requests.add(controller);
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const target = new URL(url, window.location.origin);
      target.searchParams.set('user_id', expectedUser);
      const response = await fetch(target, {credentials: 'omit', cache: 'no-store',
        ...options, signal: controller.signal});
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
    timer = setTimeout(() => (needsGeneration ? generate() : update()).catch(() => show('Не удалось обновить данные. Обновите страницу или попробуйте ещё раз.')), 5000);
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
    return data;
  }

  async function generate() {
    if (busy || !active) return;
    clearTimeout(timer); busy = true;
    if (retryButton) retryButton.disabled = true;
    show('Проверяем готовность инсайтов…');
    try {
      const url = feedRoot ? '/api/insights/generate' : '/api/insights/' + encodeURIComponent(root.dataset.newsId) + '/generate';
      const result = await request(url, {method: 'POST'});
      if (!result) return;
      if (result.data.retry) {
        needsGeneration = true;
        show(result.data.detail || 'Ожидаем завершения текущей подготовки…');
        const state = await update();
        if (state && state.recovered_cards) {
          needsGeneration = false;
          show('');
          schedule(state.poll);
        } else schedule(true);
      } else if (!result.response.ok) {
        show(result.data.detail || 'Не удалось запустить подготовку.');
      } else {
        needsGeneration = false;
        show('');
        await update();
      }
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
  if (retryButton) retryButton.addEventListener('click', () => {
    needsGeneration = true; polls = 0; generate();
  });
  generate();
})();

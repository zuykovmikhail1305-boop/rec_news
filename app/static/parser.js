(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const inputs = [...document.querySelectorAll('[name="source"]')];
  const settings = [...inputs, $('#poll-interval'), $('#budget-scenario')];
  const keys = ['sources', 'collect', 'dedupe', 'gemma', 'qwen', 'glm'];
  const counters = ['sources', 'collected', 'unique', 'relevant', 'enriched', 'insights'];
  let generation = 0;
  let running = false;
  let raw = [];
  const selected = () => inputs.filter(input => input.checked).map(input => input.value);
  function budget(value) {
    $('#budget-used').textContent = value;
    $('#budget-bar').style.width = `${Math.min(100, value / 5)}%`;
  }
  function log(text) {
    const item = document.createElement('li');
    item.textContent = text;
    $('#trace-log').append(item);
  }
  function reset() {
    generation++;
    running = false;
    raw = [];
    settings.forEach(input => { input.disabled = false; });
    keys.forEach((key, index) => {
      const card = $(`[data-stage="${key}"]`);
      card.classList.remove('active', 'complete', 'deferred');
      card.querySelector('.stage-state').textContent = 'Ожидание';
      $(`#count-${counters[index]}`).textContent = index === 0 ? selected().length : 0;
    });
    budget($('#budget-scenario').value === 'exhausted' ? 500 : 0);
    $('#raw-count').textContent = '0';
    $('#deferred-count').textContent = '0';
    $('#trace-result').textContent = 'Ожидает прогон';
    $('#trace-result').classList.remove('ready');
    $('#trace-source').textContent = `${selected()[0] || 'Нет источников'} · синтетическая новость`;
    $('#trace-log').replaceChildren();
    log('Запустите симуляцию, чтобы увидеть выполненные шаги.');
    $('#trace-details').hidden = true;
    $('#trace-toggle').textContent = 'Показать детали';
    $('#trace-toggle').setAttribute('aria-expanded', 'false');
    $('#run-status').textContent = 'Готов к запуску';
    $('#run-status').className = 'run-status idle';
    $('#run-button').disabled = false;
    $('#run-button').textContent = '▶ Запустить прогон';
    $('#poll-note').textContent = `Условный опрос раз в ${$('#poll-interval').value} мин. Один ручной прогон = один цикл; автоматического таймера и ожидания этого интервала нет.`;
  }
  async function run() {
    if (running) return;
    reset();
    const sources = selected();
    if (!sources.length) {
      $('#run-status').textContent = 'Выберите хотя бы один источник';
      $('#trace-result').textContent = 'Нет данных';
      $('#trace-log').replaceChildren();
      log('Источники не выбраны: сбор и LLM не запускались.');
      return;
    }
    const token = generation;
    const exhausted = $('#budget-scenario').value === 'exhausted';
    running = true;
    settings.forEach(input => { input.disabled = true; });
    $('#run-button').disabled = true;
    $('#run-button').textContent = 'Прогон выполняется…';
    $('#run-status').textContent = 'Симуляция одного цикла';
    $('#run-status').className = 'run-status running';
    $('#trace-log').replaceChildren();
    const n = sources.length;
    // Deterministic fixtures, not estimates of real source volume or model performance.
    const unique = n * 6 - 1;
    const relevant = n + 2;
    const values = [n, n * 7, unique, relevant, relevant, Math.ceil(n / 2)];
    const costs = [0, 0, 0, 18, 53, 26];
    const messages = [
      `Источники: ${sources.join(', ')}. Интервал ${$('#poll-interval').value} мин — только условная настройка.`,
      `Сбор: ${values[1]} синтетических записей news_raw сохранены в памяти страницы.`,
      `Дедупликация (имитация): ${unique} уникальных, ${values[1] - unique} дублей. Raw не удалён.`,
      `Gemma (имитация): ${relevant} релевантных. Пример score 0,91; условно +18 ₽, не тариф.`,
      `Qwen (имитация): ${relevant} обогащённых. Теги: упаковка, сырьё, рост цен; условно +53 ₽.`,
      `GLM (имитация): ${values[5]} инсайтов. Пример: проверьте условия закупочных контрактов; условно +26 ₽.`
    ];
    let spent = exhausted ? 500 : 0;
    for (let index = 0; index < keys.length; index++) {
      const card = $(`[data-stage="${keys[index]}"]`);
      if (exhausted && index >= 3) {
        card.classList.add('deferred');
        card.querySelector('.stage-state').textContent = 'Пропущено: лимит';
        log(`${keys[index].toUpperCase()}: не выполнялся — условный бюджет исчерпан.`);
        continue;
      }
      card.classList.add('active');
      card.querySelector('.stage-state').textContent = 'Имитация…';
      await new Promise(resolve => setTimeout(resolve, 400));
      if (token !== generation) return; // Reset/replay invalidates every older continuation.
      if (index === 1) {
        raw = sources.flatMap(source => Array.from({ length: 7 }, (_, i) => ({ source, id: `${source}-${i}`, title: 'Синтетическая новость' })));
        $('#raw-count').textContent = raw.length;
      }
      $(`#count-${counters[index]}`).textContent = values[index];
      spent += costs[index];
      budget(spent);
      card.classList.remove('active');
      card.classList.add('complete');
      card.querySelector('.stage-state').textContent = 'Имитация завершена';
      log(messages[index]);
    }
    $('#deferred-count').textContent = exhausted ? unique : 0;
    $('#trace-result').textContent = exhausted ? 'Отложено: лимит LLM' : 'Синтетический инсайт готов';
    $('#trace-result').classList.toggle('ready', !exhausted);
    $('#run-status').textContent = exhausted
      ? `Raw сохранён · отложено ${unique} · новых расходов 0 ₽ (условно)`
      : 'Прогон завершён · условно 97 ₽ (не реальные расходы)';
    $('#run-status').className = 'run-status done';
    $('#run-button').textContent = '↻ Запустить заново';
    $('#run-button').disabled = false;
    settings.forEach(input => { input.disabled = false; });
    running = false;
  }
  $('#run-button').addEventListener('click', run);
  $('#reset-button').addEventListener('click', reset);
  settings.forEach(input => input.addEventListener('change', reset));
  $('#trace-toggle').addEventListener('click', () => {
    const details = $('#trace-details');
    details.hidden = !details.hidden;
    $('#trace-toggle').textContent = details.hidden ? 'Показать детали' : 'Скрыть детали';
    $('#trace-toggle').setAttribute('aria-expanded', String(!details.hidden));
  });
  reset();
})();

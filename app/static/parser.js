(() => {
  const stages = [
    { key: 'sources', count: 'count-sources', value: 6, label: '6 источников подключены', cost: 0 },
    { key: 'collect', count: 'count-collected', value: 42, label: '42 новости собраны', cost: 0 },
    { key: 'dedupe', count: 'count-unique', value: 35, label: '7 дублей объединено', cost: 0 },
    { key: 'gemma', count: 'count-relevant', value: 8, label: '8 релевантных материалов', cost: 18 },
    { key: 'qwen', count: 'count-enriched', value: 8, label: '8 материалов обогащено', cost: 53 },
    { key: 'glm', count: 'count-insights', value: 3, label: '3 инсайта готовы', cost: 26 }
  ];
  const $ = (selector) => document.querySelector(selector);
  const runButton = $('#run-button');
  const status = $('#run-status');
  let running = false;

  function reset() {
    stages.forEach(({ key, count, value }) => {
      const card = document.querySelector(`[data-stage="${key}"]`);
      card.classList.remove('active', 'complete');
      $(`#${count}`).textContent = key === 'sources' ? value : 0;
      card.querySelector('.stage-state').textContent = 'Ожидание';
    });
    $('#budget-used').textContent = '0';
    $('#budget-bar').style.width = '0%';
    $('#trace-result').textContent = 'Ожидает прогон';
    $('#trace-result').classList.remove('ready');
  }

  function wait(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }
  function setBudget(value) {
    $('#budget-used').textContent = value;
    $('#budget-bar').style.width = `${value / 5}%`;
  }

  async function run() {
    if (running) return;
    running = true;
    reset();
    runButton.disabled = true;
    runButton.textContent = 'Прогон выполняется…';
    status.textContent = 'Воркер обрабатывает очередь';
    status.className = 'run-status running';
    let budget = 0;
    for (const stage of stages) {
      const card = document.querySelector(`[data-stage="${stage.key}"]`);
      card.classList.add('active');
      card.querySelector('.stage-state').textContent = 'В работе…';
      await wait(650);
      $(`#${stage.count}`).textContent = stage.value;
      budget += stage.cost;
      setBudget(budget);
      card.classList.remove('active');
      card.classList.add('complete');
      card.querySelector('.stage-state').textContent = stage.label;
    }
    $('#trace-result').textContent = 'Инсайт сформирован';
    $('#trace-result').classList.add('ready');
    status.textContent = 'Прогон завершён · 97 ₽';
    status.className = 'run-status done';
    runButton.textContent = '↻ Запустить заново';
    runButton.disabled = false;
    running = false;
  }

  runButton.addEventListener('click', run);
  $('#trace-toggle').addEventListener('click', () => {
    const details = $('#trace-details');
    const opened = !details.hidden;
    details.hidden = opened;
    $('#trace-toggle').textContent = opened ? 'Показать детали' : 'Скрыть детали';
    $('#trace-toggle').setAttribute('aria-expanded', String(!opened));
  });
})();

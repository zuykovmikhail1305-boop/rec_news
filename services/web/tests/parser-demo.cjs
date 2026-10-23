// PLAYWRIGHT_MODULE=/absolute/path/to/playwright node tests/parser-demo.cjs
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const { pathToFileURL } = require('node:url');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage();
    const errors = [], external = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      const url = route.request().url();
      if (/^https?:/.test(url) && !url.startsWith('http://127.0.0.1:')) {
        external.push(url); return route.abort();
      }
      return route.continue();
    });
    await page.goto(process.env.DEMO_URL || pathToFileURL(path.resolve(__dirname, '../docs/parser-demo.html')).href);
    const text = selector => page.locator(selector).textContent();
    const eq = async (selector, expected) => assert.equal(await text(selector), String(expected), selector);
    const run = async () => {
      await page.click('#run-button');
      await page.waitForFunction(() => !document.querySelector('#run-button').disabled);
    };
    assert.equal(await page.locator('[name="source"]').count(), 6);
    await page.selectOption('#poll-interval', '60');
    assert.match(await text('#poll-note'), /60 мин.*автоматического таймера/);
    for (const input of await page.locator('[name="source"]').all()) await input.uncheck();
    await run();
    await eq('#raw-count', 0); await eq('#count-insights', 0);
    assert.match(await text('#run-status'), /Выберите/);
    console.log('PASS no-source guard and simulated interval');
    for (const input of await page.locator('[name="source"]').all()) await input.check();
    await run();
    for (const [id, value] of Object.entries({sources:6,collected:42,unique:35,relevant:8,enriched:8,insights:3})) await eq(`#count-${id}`, value);
    await eq('#raw-count',42); await eq('#deferred-count',0); await eq('#budget-used',97);
    await page.click('#trace-toggle');
    assert.equal(await page.locator('#trace-details').isVisible(), true);
    assert.match(await text('#trace-log'), /Gemma.*Qwen.*GLM/s);
    await page.click('#trace-toggle');
    assert.equal(await page.locator('#trace-details').isVisible(), false);
    console.log('PASS normal 6-source run and trace toggle');
    await run(); await eq('#raw-count',42); await eq('#budget-used',97);
    console.log('PASS repeat without accumulated raw or cost');
    await page.selectOption('#budget-scenario', 'exhausted');
    await run();
    await eq('#raw-count',42); await eq('#count-unique',35); await eq('#deferred-count',35); await eq('#budget-used',500);
    for (const id of ['relevant','enriched','insights']) await eq(`#count-${id}`,0);
    assert.equal(await page.locator('.stage.deferred').count(),3);
    assert.equal(await page.locator('.stage.llm.complete').count(),0);
    assert.doesNotMatch(await text('#trace-log'), /score|Теги:|Пример: проверьте/);
    console.log('PASS exhausted budget: raw retained, 35 deferred, no LLM completions');
    await page.selectOption('#budget-scenario','normal');
    for (const input of (await page.locator('[name="source"]').all()).slice(0,5)) await input.uncheck();
    await run(); await eq('#count-sources',1); await eq('#raw-count',7); await eq('#count-unique',5); await eq('#count-insights',1);
    assert.match(await text('#trace-source'), /Банк России/);
    console.log('PASS selected source affects counts and trace');
    await page.click('#run-button');
    await page.waitForTimeout(500);
    await page.click('#reset-button');
    await page.waitForTimeout(600);
    await eq('#raw-count',0); await eq('#count-insights',0); await eq('#budget-used',0);
    assert.equal(await page.locator('.stage.active').count(),0);
    // Start a new generation before the previous timer resolves.
    await page.click('#run-button'); await page.click('#reset-button'); await run();
    await eq('#raw-count',7); await eq('#budget-used',97);
    assert.equal(await page.locator('#trace-log li').count(),6);
    console.log('PASS mid-run reset, stale timers cancelled, immediate replay');
    assert.deepEqual(errors,[]); assert.deepEqual(external,[]);
    console.log('PASS zero browser JS errors and zero external network requests');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

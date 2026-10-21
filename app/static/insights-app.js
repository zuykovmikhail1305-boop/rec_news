/* =========================================================
   Инсайты: логика прототипа.
   Извлечено из alfa-insights-prototype.html (блок <script>, состояние/рендер/события).

   Разметка экранов — в HTML-фрагментах app/templates/insights/ (*.html),
   демо-данные — в app/static/insights-data.js (подключается раньше).
   Фрагменты заменяют токены вида [[name]] на значения; списки и динамические
   блоки собираются в JS и передаются как готовый HTML.
   ========================================================= */

/* ---------- state ---------- */
const S = {screen:'login', user:null, company:null, page:'home', insightId:null, filter:'all', mode:'compact',
  fb:{}, chat:{}, roleOverride:{}, muted:{}, digest:'push', srcTab:'research', pendingUser:'anna'};

const $ = (s,el=document)=>el.querySelector(s);
const esc = s=>String(s).replace(/[&<>"]/g,c=>({'&':'&','<':'<','>':'>','"':'"'}[c]));

function roleFor(cid){ if(S.roleOverride[cid]) return S.roleOverride[cid]; const l=S.user.companies.find(x=>x.id===cid); return l?l.role:'cfo'; }
function currentRole(){ return S.company==='group' ? roleFor(S.user.companies[0].id) : roleFor(S.company); }
function companyIds(){ return S.company==='group' ? S.user.companies.map(c=>c.id) : [S.company]; }

function score(ins, role){
  const pw={high:6, medium:3, low:1}[ins.prio];
  const ev=EVENTS[ins.e];
  const rb=(ROLES[role].boost[ev.topic]||0);
  const fb=S.fb[ins.id]==='no'?-10:S.fb[ins.id]==='yes'?1:0;
  return pw+rb*1.5+fb;
}
function feed(){
  const ids=companyIds();
  let list=INSIGHTS.filter(i=>ids.includes(i.c) && !S.muted[EVENTS[i.e].topic]);
  if(S.company==='group'){
    const byEv={};
    list.forEach(i=>{ const r=roleFor(i.c); const sc=score(i,r);
      if(!byEv[i.e] || sc>byEv[i.e].sc){ byEv[i.e]={i, sc, also:(byEv[i.e]?byEv[i.e].also:[])}; }
      byEv[i.e].also = Array.from(new Set([...(byEv[i.e].also||[]), i.c])); });
    return Object.values(byEv).sort((a,b)=>b.sc-a.sc).map(x=>({...x.i, affects:x.also}));
  }
  const r=currentRole();
  return list.map(i=>({...i, sc:score(i,r)})).sort((a,b)=>b.sc-a.sc);
}
function filtered(){
  return feed().filter(i=>{
    if(S.filter==='risk') return i.impact==='risk';
    if(S.filter==='opp') return i.impact==='opp';
    if(S.filter==='bank') return !!EVENTS[i.e].bank;
    return true; });
}
function greet(){ const h=new Date().getHours(); return h<12?'Доброе утро':h<18?'Добрый день':'Добрый вечер'; }

const IC = {
  home:'<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
  ins:'<path d="M12 3v2M5.6 5.6l1.4 1.4M3 12h2m14 0h2m-3.4-6.4L16.2 7M9 18h6m-5 3h4M12 7a5 5 0 0 0-3 9h6a5 5 0 0 0-3-9z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  acc:'<rect x="3" y="6" width="18" height="13" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M3 10h18" stroke="currentColor" stroke-width="1.8"/>',
  pay:'<path d="M5 12h14m-5-5 5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  doc:'<path d="M7 3h7l5 5v13H7z M14 3v5h5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
  cred:'<path d="M4 20V10m5 10V6m5 14v-8m5 8V4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  ved:'<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  src:'<path d="M4 5h16M4 12h16M4 19h10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  set:'<circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M4.9 4.9 7 7m10 10 2.1 2.1M4.9 19.1 7 17m10-10 2.1-2.1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  bell:'<path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4zM10 20a2 2 0 0 0 4 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
  menu:'<path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  send:'<path d="M4 12 20 4l-6 16-2-7z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>'
};
const svg=(k,s=20)=>`<svg viewBox="0 0 24 24" width="${s}" height="${s}" aria-hidden="true">${IC[k]}</svg>`;

/* ---------- template loader ---------- */
/* Фрагменты отдаются маршрутом GET /insights/tpl/{name} без обработки Jinja2.
   Токены [[name]] заменяются значениями из vals (значения уже готовый HTML или
   экранированный текст; экранирование выполняют вызывающие функции через esc()). */
const TPL_NAMES=['login','sms','orgs','shell','home','feed','card','panel','insight','chat','trace','sources','settings','stub'];
const TPLS={};
async function loadTemplates(){
  await Promise.all(TPL_NAMES.map(async n=>{
    const r=await fetch('/insights/tpl/'+n);
    if(!r.ok) throw new Error('Шаблон не найден: '+n+' ('+r.status+')');
    TPLS[n]=await r.text();
  }));
}
function tpl(name, vals){
  const src=TPLS[name]||'';
  return src.replace(/\[\[([A-Za-z_][A-Za-z0-9_]*)\]\]/g,(m,k)=>Object.prototype.hasOwnProperty.call(vals,k)?String(vals[k]):m);
}

/* ---------- render: auth ---------- */
function renderLogin(){
  const u=USERS[S.pendingUser];
  return tpl('login', {
    login: esc(u.login),
    demoUsers: Object.values(USERS).map(x=>`<button class="demo-user ${x.id===S.pendingUser?'sel':''}" data-act="pick" data-id="${x.id}">
      <div class="ava">${x.ini}</div><div><b>${esc(x.full)}</b><span>${x.companies.map(c=>ROLES[c.role].name+' · '+COMPANIES[c.id].name).join(' / ')}</span></div></button>`).join('')
  });
}
function renderSms(){
  return tpl('sms', {});
}
function countFor(cid){ return INSIGHTS.filter(i=>i.c===cid && i.prio!=='low').length; }
function renderOrgs(){
  const u=S.user;
  return tpl('orgs', {
    greeting: esc(u.call),
    count: u.companies.length,
    orgs: u.companies.map(c=>{const co=COMPANIES[c.id];return `<button class="org" data-act="org" data-id="${c.id}"><div class="ava">${co.short}</div><div><b>${esc(co.name)}</b><span>${ROLES[c.role].name} · ${co.okved.split(' · ')[1]}</span></div><span class="count">${countFor(c.id)} инсайта</span></button>`}).join('')
      + (u.companies.length>1?`<button class="org" data-act="org" data-id="group"><div class="ava">Гр</div><div><b>Все компании</b><span>Общая лента без дублей событий</span></div></button>`:'')
  });
}

/* ---------- shell ---------- */
function compLabel(){
  if(S.company==='group') return {n:'Все компании', r:S.user.companies.length+' организации'};
  const co=COMPANIES[S.company]; return {n:co.name, r:ROLES[roleFor(S.company)].name};
}
function renderShell(inner){
  const cl=compLabel(); const n=feed().filter(i=>i.prio==='high').length;
  const nav=[['home','Главная','home'],['ins','Инсайты','ins'],['acc','Счета','acc'],['pay','Платежи','pay'],['doc','Выписки и документы','doc'],['cred','Кредиты','cred'],['ved','ВЭД','ved']];
  return tpl('shell', {
    nav: nav.map(([k,l,ic])=>{const pg=k==='ins'?'insights':k; const act=(S.page===pg||(k==='ins'&&S.page==='insight'))?'active':'';
      return `<a class="${act}" data-nav="${pg}">${svg(ic)}<span>${l}</span>${k==='ins'?`<span class="ai-tag">AI</span>${n?`<span class="badge">${n}</span>`:''}`:''}</a>`}).join(''),
    navBottom: `<a class="${S.page==='sources'?'active':''}" data-nav="sources">${svg('src')}<span>Источники и пул</span></a>
      <a class="${S.page==='settings'?'active':''}" data-nav="settings">${svg('set')}<span>Настройки инсайтов</span></a>`,
    menuIcon: svg('menu',24),
    bellIcon: svg('bell'),
    companyShort: S.company==='group'?'Гр':COMPANIES[S.company].short,
    companyName: esc(cl.n),
    companyRole: esc(cl.r),
    dropdownItems: S.user.companies.map(c=>`<button data-act="setco" data-id="${c.id}"><div class="ava" style="border-radius:9px;background:#F1F2F4">${COMPANIES[c.id].short}</div><div><b>${esc(COMPANIES[c.id].name)}</b><span>${ROLES[roleFor(c.id)].name}</span></div></button>`).join('')
      + (S.user.companies.length>1?`<button data-act="setco" data-id="group"><div class="ava" style="border-radius:9px;background:#F1F2F4">Гр</div><div><b>Все компании</b><span>Объединённая лента</span></div></button>`:''),
    userIni: S.user.ini,
    userCall: esc(S.user.call),
    banner: ['insights','insight','sources','settings'].includes(S.page)?'<div class="demo-banner">Демо-данные: компании синтетические, тексты — примеры работы агента. В продукте факты и позиции банка извлекаются из источников и материалов Альфа Ресерч.</div>':'',
    inner: inner
  });
}

/* ---------- chips ---------- */
const impactChip=i=>i.impact==='risk'?'<span class="chip risk"><span class="dot"></span>Риск</span>':i.impact==='opp'?'<span class="chip opp"><span class="dot"></span>Возможность</span>':'<span class="chip neutral"><span class="dot"></span>Нейтрально</span>';
const bankChip=e=>e.bank?'<span class="chip bank">Есть позиция Альфа Ресерч</span>':'<span class="chip nobank">Аналитики банка нет</span>';
const confChip=e=>e.conf==='confirmed'?'<span class="chip conf-ok">✓ Подтверждено</span>':'<span class="chip conf-one">Один источник</span>';
const prioLbl={high:'Высокий приоритет', medium:'Средний приоритет', low:'Низкий приоритет'};

/* ---------- home ---------- */
function renderHome(){
  const f=feed(); const top=f[0]; const ev=top?EVENTS[top.e]:null;
  const risks=f.filter(i=>i.impact==='risk').length, opps=f.filter(i=>i.impact==='opp').length;
  const co=S.company==='group'?null:COMPANIES[S.company];
  const accs=S.company==='group'
    ? S.user.companies.map(c=>[COMPANIES[c.id].name,'Расчётный счёт ₽ ··· '+(4800+c.id.length*7),(120+c.id.length*31)+' 417 220,50 ₽'])
    : [['Расчётный счёт ₽','··· 4821','184 320 517,40 ₽'],['Депозит','до 15.12.2026','250 000 000,00 ₽']].concat(S.company==='logistics'?[['Валютный счёт CNY','··· 0917','12 840 300,00 ¥']]:[]);
  const hero=top?`<div class="hero-insight">
    <div><div class="kicker"><span class="chip ai">AI</span>Инсайт дня для вас · ${ROLES[S.company==='group'?roleFor(top.c):currentRole()].name}</div>
      <h2>${esc(top.h)}</h2><p>${esc(top.t)}</p>
      <div class="row">${impactChip(top)}${bankChip(ev)}<span class="chip plain">${esc(ev.type)}</span></div></div>
    <div class="hero-side"><div class="row" style="justify-content:flex-end">${risks?`<span class="chip risk">Рисков: ${risks}</span>`:''}${opps?`<span class="chip opp">Возможностей: ${opps}</span>`:''}</div>
      <div class="row"><button class="btn btn-secondary btn-sm" data-open="${top.id}">Подробнее</button><button class="btn btn-primary btn-sm" data-nav="insights">Все инсайты · ${f.length}</button></div></div>
  </div>`:'';
  return tpl('home', {
    greeting: greet(),
    call: esc(S.user.call),
    pageSub: (co?esc(co.name):'Все компании')+' · '+new Date().toLocaleDateString('ru-RU',{day:'numeric',month:'long'}),
    hero,
    accounts: accs.map(a=>`<div class="acc"><span>${esc(a[0])} ${esc(a[1])}</span><b>${a[2]}</b></div>`).join(''),
    quickActions: `<button>${svg('pay')}Новый платёж</button><button>${svg('doc')}Выписка</button><button>${svg('ved')}Валютный перевод</button><button data-nav="insights">${svg('ins')}Инсайты</button>`
  });
}

/* ---------- feed ---------- */
function briefText(f){
  const r=S.company==='group'?null:currentRole();
  const hi=f.filter(i=>i.prio==='high');
  if(!hi.length) return 'Сегодня значимых для вас событий нет. Мы показываем только то, что связано с вашими компаниями.';
  const parts=hi.slice(0,3).map(i=>i.h.charAt(0).toLowerCase()+i.h.slice(1));
  return 'Главное на сегодня'+(r?' для роли «'+ROLES[r].name+'»':'')+': '+parts.join('; ')+'.';
}
function renderInsights(){
  const f=filtered(); const all=feed();
  const co=S.company==='group'?null:COMPANIES[S.company];
  return tpl('feed', {
    feedSubject: co?esc(co.name):'ваши компании',
    brief: esc(briefText(all)),
    filters: [['all','Все'],['risk','Риски'],['opp','Возможности'],['bank','С аналитикой банка']].map(([k,l])=>`<button class="filter ${S.filter===k?'on':''}" data-filter="${k}">${l}</button>`).join(''),
    segButtons: `<button class="${S.mode==='compact'?'on':''}" data-mode="compact">Компактно</button><button class="${S.mode==='detailed'?'on':''}" data-mode="detailed">Подробно</button>`,
    cards: f.length?f.map(i=>cardHtml(i)).join(''):'<div class="empty">По выбранному фильтру событий нет.</div>',
    panel: profilePanel()
  });
}
function cardHtml(i){
  const ev=EVENTS[i.e];
  const aff=i.affects&&i.affects.length>1?`<div class="affects">Затрагивает: ${i.affects.map(c=>COMPANIES[c].name).join(', ')}</div>`:(S.company==='group'?`<div class="affects">${COMPANIES[i.c].name}</div>`:'');
  return tpl('card', {
    id: i.id,
    chips: impactChip(i)+bankChip(ev)+confChip(ev),
    title: esc(i.h),
    desc: esc(i.t),
    detail: S.mode==='detailed'?`<p style="margin-top:10px;color:var(--muted);font-size:.93em"><b style="color:var(--text)">Событие:</b> ${esc(ev.title)}. ${esc(ev.fact)}</p>`:'',
    affects: aff,
    foot: `<span><span class="prio prio-${i.prio}"></span>${prioLbl[i.prio]}</span><span>Горизонт: ${i.horizon}</span><span>${esc(ev.type)}</span><span>${ev.date}</span>${S.fb[i.id]==='yes'?'<span>✓ Вы отметили как полезное</span>':''}`
  });
}
function profilePanel(){
  const ids=companyIds();
  return tpl('panel', {
    companies: ids.map(id=>{const co=COMPANIES[id];return `<div style="margin-bottom:10px"><b style="font-size:14px">${esc(co.name)}</b>
      <div class="feat"><span class="k">Отрасль</span><span>${esc(co.okved)}</span></div>
      <div class="feat"><span class="k">Выручка</span><span>${co.revenue}</span></div>
      <div class="feat"><span class="k">Продукты</span><span>${co.products.join(', ')}</span></div>
      <div class="feat"><span class="k">Регион</span><span>${esc(co.region)}</span></div>
      <div class="feat"><span class="k">ВЭД</span><span>${esc(co.ved)}</span></div>
      <div class="feat"><span class="k">Контрагенты</span><span>${co.counterparties.map(esc).join('<br>')}</span></div>
      <div class="feat"><span class="k">Ваша роль</span><span>${ROLES[roleFor(id)].name}</span></div></div>`}).join('')
  });
}

/* ---------- insight detail ---------- */
function renderInsight(){
  const i=INSIGHTS.find(x=>x.id===S.insightId); const ev=EVENTS[i.e]; const co=COMPANIES[i.c];
  const n=ev.sources.length;
  const bankIdx=n+1;
  return tpl('insight', {
    headerChips: impactChip(i)+`<span class="chip plain"><span class="prio prio-${i.prio}"></span>${prioLbl[i.prio]}</span><span class="chip plain">Горизонт: ${i.horizon}</span>`+confChip(ev),
    title: esc(i.h),
    meta: esc(co.name)+` · подача для роли «${ROLES[roleFor(i.c)].name}» · событие от ${ev.date}`,
    eventType: esc(ev.type),
    eventTitle: esc(ev.title),
    eventFact: esc(ev.fact),
    citesHtml: ev.sources.map((s,k)=>`<span class="cite" data-src="${k+1}">${k+1}</span>`).join(''),
    bankLayer: ev.bank?`<div class="layer bank"><div class="lh">Позиция Альфа-Банка · как это оценивает банк <span class="q">Альфа Ресерч · ${ev.bank.date}</span></div>
      <p>${esc(ev.bank.text)}<span class="cite" data-src="${bankIdx}">${bankIdx}</span></p>
      <div class="quote">${esc(ev.bank.q)} — «${esc(ev.bank.doc)}»</div></div>`
      :`<div class="layer nobank"><div class="lh">⚠ Аналитика Альфа-Банка по этому событию отсутствует</div>
      <p>Выводы ниже сформированы AI на основе внешних источников и данных о компании. Они не отражают позицию Альфа-Банка.</p></div>`,
    companyName: esc(co.name),
    concl: esc(i.concl),
    whyChips: i.why.map(w=>`<span class="chip plain">Признак: ${esc(w)}</span>`).join(''),
    risks: i.risks.map(r=>`<li>${esc(r)}</li>`).join(''),
    opps: i.opps.map(r=>`<li>${esc(r)}</li>`).join(''),
    hyp: esc(i.hyp),
    actions: i.acts.map(a=>`<label class="check"><input type="checkbox"><span>${esc(a)}</span></label>`).join(''),
    analysis: i.qs.length?`<div style="margin-top:8px;font-size:14px;color:var(--muted)"><b style="color:var(--text)">Вопрос для анализа:</b> ${i.qs.map(esc).join(' ')}</div>`:'',
    offer: i.offer?`<div class="offer"><div><div class="tag">Предложение банка</div><b>${esc(i.offer.t)}</b><span>${esc(i.offer.d)}</span></div><button class="btn btn-secondary btn-sm" data-act="mgr">Обсудить с менеджером</button></div>`:'',
    sources: ev.sources.map((s,k)=>`<div class="src" id="src${k+1}"><div class="n">${k+1}</div><div><b>${esc(s.n)}</b><span class="tier t-${s.tier}">Уровень ${s.tier}</span><div class="qt">${esc(s.q)}</div><a class="lnk" href="#" onclick="return false">Открыть первоисточник →</a></div></div>`).join('')
      + (ev.bank?`<div class="src" id="src${bankIdx}"><div class="n">${bankIdx}</div><div><b>${esc(ev.bank.doc)}</b><span class="tier t-R">Альфа Ресерч</span><div class="qt">${esc(ev.bank.q)}</div><a class="lnk" href="#" onclick="return false">Открыть материал →</a></div></div>`:''),
    dataBasis: i.why.map(esc).join(' · '),
    fbButtons: `<button class="fb ${S.fb[i.id]==='yes'?'on':''}" data-fb="yes">👍 Полезно</button>
      <button class="fb ${S.fb[i.id]==='no'?'on':''}" data-fb="no">Не про нас</button>
      <button class="fb ${S.fb[i.id]==='err'?'on':''}" data-fb="err">Ошибка в фактах</button>
      <button class="fb" data-act="share">Переслать коллеге</button>`,
    chat: chatHtml(i)
  });
}

/* ---------- chat ---------- */
const SUGG={
  rate:['Что будет, если ставку снизят в декабре?','Покажи для другой нашей компании','Где первоисточник?','Стоит ли рефинансироваться сейчас?'],
  def:['Как это повлияет на нас через полгода?','Где первоисточник?','Что думает банк?','Покажи для другой нашей компании']
};
function chatHtml(i){
  const h=S.chat[i.id]||[];
  const sg=(SUGG[i.e]||SUGG.def);
  return tpl('chat', {
    messages: h.length?h.map(m=>m.me?`<div class="msg me">${esc(m.t)}</div>`:`<div class="msg bot">${m.html}</div>`).join(''):`<div class="msg bot"><div class="ml a">AI</div>Задайте уточняющий вопрос: другой сценарий, другая компания группы или первоисточник.</div>`,
    suggButtons: sg.map(s=>`<button data-ask="${esc(s)}">${esc(s)}</button>`).join(''),
    sendIcon: svg('send',18)
  });
}
function answer(i, q){
  const ev=EVENTS[i.e]; const ql=q.toLowerCase(); const dis='<div class="dis">Не является инвестиционной рекомендацией.</div>';
  if(/источник|оригинал|откуда/.test(ql)){
    return `<div class="ml f">Факт</div>${ev.sources.map((s,k)=>`[${k+1}] ${esc(s.n)} (уровень ${s.tier}): ${esc(s.q)}`).join('<br>')}`+(ev.bank?`<div class="ml b">Альфа Ресерч</div>«${esc(ev.bank.doc)}», ${ev.bank.date}`:`<div class="ml b">Альфа Ресерч</div>Материалов банка по этому событию нет.`);
  }
  if(/стоит ли|купить|продать|рефинанс|выгодно ли/.test(ql)){
    return `<div class="ml a">AI</div>Я не даю прямых рекомендаций о сделках. Для решения важны: доля долга с плавающей ставкой, срок до погашения, стоимость досрочного погашения и ваш сценарий по ставке.`+(ev.bank?`<div class="ml b">Позиция банка</div>${esc(ev.bank.text)}`:'')+`<div class="ml a">Следующий шаг</div>Обсудить расчёт с вашим менеджером в банке.`+dis;
  }
  if(/компани|группы|другой/.test(ql)){
    const other=S.user.companies.map(c=>c.id).filter(id=>id!==i.c);
    if(!other.length) return `<div class="ml a">AI</div>У вас доступ только к ${esc(COMPANIES[i.c].name)}.`;
    const oi=INSIGHTS.find(x=>x.c===other[0] && x.e===i.e);
    if(!oi) return `<div class="ml a">AI</div>Для ${esc(COMPANIES[other[0]].name)} это событие не прошло порог значимости: в профиле компании нет признаков, через которые оно влияет на бизнес.`;
    return `<div class="ml a">AI · ${esc(COMPANIES[oi.c].name)}</div><b>${esc(oi.h)}</b><br>${esc(oi.t)}<br><a href="#" data-open="${oi.id}" style="color:#1C5DB8">Открыть карточку →</a>`+dis;
  }
  if(/если|сценари|снизят|повыс|через/.test(ql)){
    return `<div class="ml h">Гипотеза</div>${esc(i.hyp)}<div class="ml a">AI</div>Это условный сценарий модели на основе текущих фактов${ev.bank?' и позиции банка':''}, а не прогноз. Для точной оценки нужен расчёт по вашим кредитным договорам и бюджету.`+dis;
  }
  if(/банк|думает|позиц/.test(ql)){
    return ev.bank?`<div class="ml b">Позиция Альфа-Банка</div>${esc(ev.bank.text)}<br><span style="color:var(--muted);font-size:12.5px">«${esc(ev.bank.doc)}», ${ev.bank.date}</span>`:`<div class="ml b">Позиция Альфа-Банка</div>Аналитики банка по этому событию нет. Всё, что я могу сказать, будет выводом AI по внешним источникам.`;
  }
  return `<div class="ml a">AI</div>В материалах по этому событию нет прямого ответа на вопрос. Могу показать, что известно из фактов, или передать вопрос менеджеру.`+dis;
}
function ask(q){
  const i=INSIGHTS.find(x=>x.id===S.insightId); if(!q.trim()) return;
  (S.chat[i.id]=S.chat[i.id]||[]).push({me:true,t:q}); render();
  const cb=$('#cb'); cb.insertAdjacentHTML('beforeend','<div class="msg bot" id="tp"><span class="typing"><i></i><i></i><i></i></span></div>'); cb.scrollTop=cb.scrollHeight;
  setTimeout(()=>{ S.chat[i.id].push({html:answer(i,q)}); render(); const c=$('#cb'); if(c) c.scrollTop=c.scrollHeight; },900);
}

/* ---------- trace modal ---------- */
function traceModal(){
  const i=INSIGHTS.find(x=>x.id===S.insightId); const ev=EVENTS[i.e]; const co=COMPANIES[i.c];
  const steps=[
    ['ok','План',`Тип события «${ev.type}» → нужны: профиль компании, контрагенты, позиция банка, связанные события за 30 дней.`,'GLM-5.3-Flash · 412 токенов'],
    ['ok','get_company_profile('+co.id+')',`Признаки: ${i.why.join('; ')}.`,'0,04 c'],
    ['ok','get_counterparties('+co.id+')',co.counterparties.join('; ')+'.','0,06 c'],
    [ev.bank?'ok':'warn','search_research("'+ev.title.slice(0,40)+'…")',ev.bank?`Найден материал «${ev.bank.doc}» (${ev.bank.date}), фрагмент §2. Релевантность 0,91.`:'Материалов Альфа Ресерч не найдено → has_bank_research = false, добавлен дисклеймер.','RAG: BM25 + вектор + реранкер · 0,3 c'],
    ['ok','Проверка фактов',`${ev.sources.length} источник(а), цитаты найдены в тексте дословно, числа совпадают. Уверенность: ${ev.conf==='confirmed'?'подтверждено':'один источник'}.`,'код + NLI'],
    ['ok','Черновик инсайта','JSON по схеме: факт, '+(ev.bank?'позиция банка, ':'')+'вывод, гипотеза, риски, возможности, действия.','GLM-5.3-Flash · 1 840 токенов'],
    ['warn','Критик · итерация 1','Утверждение «расходы вырастут на 15%» без источника → удалено. Запрошена переформулировка.','GLM-5.3-Flash · 690 токенов'],
    ['ok','Критик · итерация 2','Все утверждения имеют доказательства, слои размечены верно, прямых советов нет. Вердикт: публиковать.','GLM-5.3-Flash · 520 токенов'],
    ['ok','Подача под роль',`Роль «${ROLES[roleFor(i.c)].name}», режим «${S.mode==='compact'?'Компактно':'Подробно'}». Факт и позиция банка перенесены дословно.`,'Gemma 4 26B · 380 токенов']
  ];
  return tpl('trace', {
    traceId: 'tr_'+i.id.replace(/-/g,'_'),
    steps: steps.map((s,k)=>`<div class="step ${s[0]}"><div class="num">${s[0]==='ok'?'✓':'!'}</div><div><b>${k+1}. ${s[1].includes('(')?`<code>${esc(s[1])}</code>`:esc(s[1])}</b><div class="d">${esc(s[2])}</div><div class="m">${esc(s[3])}</div></div></div>`).join('')
  });
}

/* ---------- sources ---------- */
function usedCount(doc){ const e=Object.values(EVENTS).find(x=>x.bank&&x.bank.doc===doc); if(!e) return 0; const key=Object.keys(EVENTS).find(k=>EVENTS[k]===e); return INSIGHTS.filter(i=>i.e===key).length; }
function renderSources(){
  return tpl('sources', {
    tabResearch: S.srcTab==='research'?'on':'',
    tabExt: S.srcTab==='ext'?'on':'',
    researchCount: RESEARCH.length,
    table: S.srcTab==='research'?`<table class="tbl"><tr><th>Материал</th><th>Тема</th><th>Дата</th><th>В инсайтах</th><th>Статус</th></tr>
      ${RESEARCH.map(r=>`<tr><td><b>${esc(r[0])}</b></td><td>${r[2]}</td><td style="white-space:nowrap">${r[1]}</td><td>${usedCount(r[0])}</td><td class="status-ok">Обработан</td></tr>`).join('')}</table>`
      :`<table class="tbl"><tr><th>Уровень</th><th>Тип</th><th>Примеры</th><th>Правило использования</th></tr>
      ${REG.map(r=>`<tr><td><span class="tier t-${r[0]}" style="font-size:12px;font-weight:700;border-radius:5px;padding:2px 8px">${r[0]}</span></td><td><b>${r[1]}</b></td><td>${r[2]}</td><td>${r[3]}</td></tr>`).join('')}</table>`
  });
}
function runIngest(){
  const steps=['Сбор страницы и сохранение оригинала','Очистка от меню и рекламы','Поиск дублей и склейка с событием','Классификация: тип события, ОКВЭД, регион','Извлечение фактов с цитатами','Проверка цитат и чисел','Сопоставление с вашими компаниями','Поиск позиции Альфа Ресерч'];
  const el=$('#pipe'); let k=0;
  const draw=()=>{ el.innerHTML='<div class="pipe">'+steps.map((s,j)=>`<div class="ps ${j<k?'done':j===k?'run':''}"><span class="ic">${j<k?'✓':''}</span>${s}</div>`).join('')+'</div>'; };
  draw();
  const t=setInterval(()=>{ k++; draw(); if(k>=steps.length){ clearInterval(t);
    const names=companyIds().map(id=>COMPANIES[id].name).join(', ');
    el.insertAdjacentHTML('beforeend',`<div class="result">
      <div class="kv"><span>Извлечено фактов</span><b>4</b></div>
      <div class="kv"><span>Подтверждено цитатой</span><b style="color:var(--opp)">3</b></div>
      <div class="kv"><span>Отброшено</span><b style="color:var(--risk)">1 — число не найдено в тексте</b></div>
      <div class="kv"><span>Затрагивает</span><b>${esc(names)}</b></div>
      <div class="kv"><span>Позиция Альфа Ресерч</span><b style="color:var(--hyp)">не найдена → дисклеймер</b></div>
      <p class="note" style="margin:8px 0 0">Демо: в продукте здесь появляется готовый инсайт в ленте.</p></div>`); } },550);
}

/* ---------- settings ---------- */
function renderSettings(){
  const u=S.user;
  const topics=[['rate','Ставки и денежно-кредитная политика'],['commodity','Сырьевые рынки'],['demand','Потребительский спрос'],['labor','Рынок труда'],['bonds','Долговой рынок'],['logistics','Логистика'],['regulation','Регулирование и ВЭД'],['macro','Макроэкономика']];
  return tpl('settings', {
    roleRows: u.companies.map(c=>`<div class="set-row"><div class="t"><b>${esc(COMPANIES[c.id].name)}</b><span>Влияет на отбор и подачу</span></div>
      <select data-role="${c.id}">${Object.entries(ROLES).map(([k,r])=>`<option value="${k}" ${roleFor(c.id)===k?'selected':''}>${r.name}</option>`).join('')}</select></div>`).join(''),
    modeButtons: `<button class="${S.mode==='compact'?'on':''}" data-mode="compact">Компактно</button><button class="${S.mode==='detailed'?'on':''}" data-mode="detailed">Подробно</button>`,
    call: esc(u.call),
    digestSelect: `<select data-act="digest"><option ${S.digest==='push'?'selected':''} value="push">В банке + push</option><option ${S.digest==='mail'?'selected':''} value="mail">В банке + email</option><option ${S.digest==='bank'?'selected':''} value="bank">Только в банке</option></select>`,
    topicRows: topics.map(([k,l])=>`<div class="set-row"><div class="t"><b>${l}</b></div><label class="toggle"><input type="checkbox" data-topic="${k}" ${S.muted[k]?'':'checked'}><span></span></label></div>`).join(''),
    dataRows: ['Отрасль (ОКВЭД) и масштаб','Банковские продукты компании','Регион и ВЭД','Контрагенты по платежам (агрегированно)','Связи внутри группы компаний'].map(l=>`<div class="set-row"><div class="t"><b>${l}</b></div><label class="toggle"><input type="checkbox" checked><span></span></label></div>`).join('')
  });
}

function renderStub(){
  const t={acc:'Счета',pay:'Платежи',doc:'Выписки и документы',cred:'Кредиты',ved:'ВЭД'}[S.page];
  return tpl('stub', { title: t });
}

/* ---------- render & events ---------- */
let modal=false;
function render(){
  document.body.classList.toggle('detailed', S.mode==='detailed' && S.screen==='app');
  const R=$('#root');
  if(S.screen==='login') R.innerHTML=renderLogin();
  else if(S.screen==='sms') R.innerHTML=renderSms();
  else if(S.screen==='orgs') R.innerHTML=renderOrgs();
  else {
    const p=S.page;
    const inner=p==='home'?renderHome():p==='insights'?renderInsights():p==='insight'?renderInsight():p==='sources'?renderSources():p==='settings'?renderSettings():renderStub();
    R.innerHTML=renderShell(inner)+(modal?traceModal():'');
  }
}
function toast(t){ const d=document.createElement('div'); d.className='toast'; d.textContent=t; document.body.appendChild(d); setTimeout(()=>d.remove(),2200); }
function go(page){ S.page=page; modal=false; render(); window.scrollTo(0,0); }

document.addEventListener('click',e=>{
  const t=e.target.closest('[data-act],[data-nav],[data-open],[data-filter],[data-mode],[data-fb],[data-ask],[data-tab],[data-src]');
  if(!t){ const dd=$('#dd'); if(dd && !e.target.closest('.switcher')) dd.classList.add('hidden'); return; }
  const a=t.dataset;
  if(a.nav){ e.preventDefault(); $('#side')&&$('#side').classList.remove('open'); go(a.nav); return; }
  if(a.open){ e.preventDefault(); S.insightId=a.open; const ins=INSIGHTS.find(x=>x.id===a.open); if(S.company!=='group' && ins.c!==S.company) S.company=ins.c; go('insight'); return; }
  if(a.filter){ S.filter=a.filter; render(); return; }
  if(a.mode){ S.mode=a.mode; render(); return; }
  if(a.tab){ S.srcTab=a.tab; render(); return; }
  if(a.src){ const el=$('#src'+a.src); if(el){ el.scrollIntoView({behavior:'smooth',block:'center'}); el.style.background='#FFF7E0'; setTimeout(()=>el.style.background='',1400);} return; }
  if(a.fb){ S.fb[S.insightId]=a.fb; render();
    toast(a.fb==='yes'?'Спасибо! Будем показывать больше похожего':a.fb==='no'?'Понятно, такие события будут ниже в ленте':'Отправлено эксперту на проверку. До проверки инсайт скрыт для других'); return; }
  if(a.ask){ ask(a.ask); return; }
  switch(a.act){
    case 'pick': S.pendingUser=a.id; render(); break;
    case 'login': S.screen='sms'; render(); setTimeout(()=>$('#code')&&$('#code').focus(),50); break;
    case 'sms': S.user=USERS[S.pendingUser]; S.mode=S.user.mode; S.roleOverride={}; S.fb={}; S.chat={};
      if(S.user.companies.length>1){ S.screen='orgs'; } else { S.company=S.user.companies[0].id; S.screen='app'; S.page='home'; } render(); break;
    case 'org': S.company=a.id; S.screen='app'; S.page='home'; render(); break;
    case 'sw': $('#dd').classList.toggle('hidden'); break;
    case 'setco': S.company=a.id; if(S.page==='insight') S.page='insights'; render(); break;
    case 'logout': S.screen='login'; S.user=null; render(); break;
    case 'menu': $('#side').classList.toggle('open'); break;
    case 'trace': modal=true; render(); break;
    case 'close': if(t.classList.contains('overlay') && e.target!==t) break; modal=false; render(); break;
    case 'ask': ask($('#ci').value); break;
    case 'mgr': toast('Запрос менеджеру отправлен (демо)'); break;
    case 'share': toast('Ссылка на инсайт скопирована (демо)'); break;
    case 'ingest': runIngest(); break;
  }
});
document.addEventListener('change',e=>{
  const t=e.target;
  if(t.dataset.role){ S.roleOverride[t.dataset.role]=t.value; render(); toast('Роль обновлена — лента пересчитана'); }
  if(t.dataset.topic){ S.muted[t.dataset.topic]=!t.checked; }
  if(t.dataset.act==='digest'){ S.digest=t.value; }
  if(t.dataset.act==='pdf' && t.files[0]){ const r=$('#pdfres'); r.textContent='«'+t.files[0].name+'» загружен. Демо: в продукте файл разбирается постранично и появляется в списке материалов.'; }
});
document.addEventListener('keydown',e=>{
  if(e.key==='Enter' && e.target.id==='ci') ask(e.target.value);
  if(e.key==='Enter' && e.target.id==='code') document.querySelector('[data-act="sms"]').click();
  if(e.key==='Escape' && modal){ modal=false; render(); }
});

loadTemplates().then(render).catch(e=>{
  $('#root').innerHTML='<div class="empty">Прототип недоступен: '+esc(e.message)+'</div>';
  console.error(e);
});
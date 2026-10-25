const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../backend/static/live-insights.js'), 'utf8');
async function scenario(detail, replies) {
  const requests=[], timers=[], output={innerHTML:''}, message={textContent:''};
  const button={addEventListener(){}, disabled:false};
  const root={dataset:{userId:'u_01', newsId:'news_1', offset:'0'}, querySelector(selector){
    return selector==='[data-retry-insights]' ? button : output;
  }};
  const context={URL,AbortController,console,
    document:{querySelector(selector){
      if(selector==='[data-insights-root]') return detail ? null : root;
      if(selector==='[data-insight-detail]') return detail ? root : null;
      return message;
    }},
    window:{location:{origin:'http://backend', reload(){}},addEventListener(){}},
    setTimeout(callback,delay){const timer={callback,delay};timers.push(timer);return timer;},
    clearTimeout(timer){if(timer)timer.cleared=true;},
    async fetch(url,options){
      requests.push([url.pathname,options.method||'GET',url.searchParams.get('user_id')]);
      assert.equal(options.credentials,'omit');
      const response=replies.shift();assert.ok(response,'Unexpected request');
      return {ok:response.ok!==false,json:async()=>response.data};
    }};
  vm.runInNewContext(source,context);
  for(let i=0;i<20;i++){
    await new Promise(resolve=>setImmediate(resolve));
    const timer=timers.find(t=>!t.cleared&&!t.ran&&t.delay===5000);
    if(timer){timer.ran=true;timer.callback();}
  }
  assert.equal(replies.length,0);
  return requests;
}
(async()=>{
  const home=await scenario(false,[
    {ok:false,data:{retry:true,detail:'waiting'}},
    {data:{user_id:'u_01',html:'waiting',poll:true}},
    {data:{user_id:'u_01'}},
    {data:{user_id:'u_01',html:'pending',poll:true}},
    {data:{user_id:'u_01',html:'ready',poll:false}}
  ]);
  assert.deepEqual(home.map(r=>r[1]),['POST','GET','POST','GET','GET']);
  assert.equal(home[0][0],'/api/insights/generate');
  const card=await scenario(true,[
    {data:{retry:true}}, {data:{poll:true}}, {data:{retry:false}},
    {data:{user_id:'u_01',html:'card',poll:false}}
  ]);
  assert.deepEqual(card.map(r=>r[0]),[
    '/api/insights/news_1/generate','/api/insights/news_1','/api/insights/news_1/generate','/api/insights/news_1']);
  assert.ok([...home,...card].every(r=>r[2]==='u_01'));
  const recovered=await scenario(false,[{ok:false,data:{retry:true}},
    {data:{user_id:'u_01',html:'cached card',recovered_cards:true,poll:false}}]);
  assert.deepEqual(recovered.map(r=>r[1]),['POST','GET']);
  console.log('Live insights: home wait, polling, card generation and deferred job passed');
})().catch(error=>{console.error(error);process.exitCode=1;});

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(process.env.BOARD_HTML || path.join(__dirname, '..', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function board(extra = {}) {
  const storage = new Map();
  const el = {addEventListener() {}, setAttribute() {}, classList: {toggle() {}}, querySelectorAll() { return []; }};
  const ctx = vm.createContext({URLSearchParams, Date, console,
    localStorage: {getItem: k => storage.get(k) ?? null, setItem: (k,v) => storage.set(k,v)},
    location: {search: ''}, history: {replaceState() {}},
    document: {getElementById: () => el, querySelector: () => el, addEventListener() {}},
    window: {addEventListener() {}, getSelection: () => ''}, setInterval() {}, fetch: () => new Promise(() => {}), ...extra});
  vm.runInContext(script, ctx);
  return {run: s => vm.runInContext(s, ctx), set: value => ctx.input = value};
}
function task(overrides = {}) {
  return {ts: 100, last_ts: 110, end_ts: 110, start_ts: 0, prompt: '작업보드의 질문 오탐을 수정해 주세요.',
    outcome: '답변', notes: [], checks: [], steps: [], files: [], n_checks: 0, n_steps: 0, result: '', ...overrides};
}
function fixture(overrides = {}) {
  const t = task(overrides.task);
  return {s: {id: 'session-a', name:'보드', agent:'codex', cwd:'work-board', state:'쉬는 중', last_ts:Date.now()/1000,
    tasks: overrides.tasks || [t], ...overrides.session},
    w: {key: 'work-board', title: '', story: '', tasks: overrides.tasks ? overrides.tasks.map((_,i)=>i) : [0],
      info: {}, ...overrides.work}};
}
function load(b, f) {
  b.set(f);
  b.run('input.s.works=[input.w]; data={sessions:[input.s]}; const W=workOf(input.s,input.w)');
}

test('usage source selection keeps totals, chart points and tool failures with the chosen agent', () => {
  const b=board();
  b.run(`const provider=(tokens,tool)=>({at:1700000000,today:tokens,week:tokens,stats:{hours:[tokens],daily:[{day:'2026-11-17',tokens,prompts:2,tools:10,errors:1}],tools:[{tool,count:10,errors:1}]}});data={sessions:[],sprint:{disabled:true},usage:{...provider(300,'combined'),providers:{claude:provider(100,'ClaudeTool'),codex:provider(200,'CodexTool')}}};`);
  for(const [source,total,tool] of [['all',300,'combined'],['claude',100,'ClaudeTool'],['codex',200,'CodexTool']]) {
    b.run(`ui.usageSource='${source}'`);
    assert.equal(b.run('usageSelection().today'),total);
    const view=b.run('dashView()');
    assert.ok(view.includes(tool));
    assert.ok(b.run('CHARTS.hourly({},500,260)').includes(`${total} 토큰`));
    assert.ok(view.includes(`data-usage-source="${source}" aria-pressed="true"`));
  }
});
test('missing Codex records do not reuse Claude numbers or show collected zero', () => {
  const b=board();
  b.run(`ui.usageSource='codex';data={sessions:[],sprint:{disabled:true},usage:{at:1700000000,today:999,stats:{daily:[{tokens:999}]},providers:{codex:{unavailable:true,error:'Codex 기록이 없습니다.'}}}}`);
  const view=b.run('dashView()');
  assert.ok(view.includes('Codex 기록이 없습니다.'));
  assert.ok(!view.includes('<b>999</b>'));
  assert.ok(!view.includes('data-chart="hourly"'));
});
test('failure percentages exclude unknown and blocked results instead of counting them as successes', () => {
  const b=board();b.run(`data={sessions:[],sprint:{disabled:true},usage:{at:100,stats:{tools:[{tool:'PartialTool',count:100,checked:20,errors:10,unknown:75,blocked:5},{tool:'PendingTool',count:3,checked:0,errors:0,unknown:3}]}}}`);
  const view=b.run('dashView()');
  const partial=view.slice(view.indexOf('title="PartialTool ·')).split('</div>')[0];
  assert.ok(partial.includes('50%') && partial.includes('width:50%'));
  assert.ok(partial.includes('10 / 20회') && partial.includes('미확인 75회'));
  const pending=view.slice(view.indexOf('title="PendingTool ·')).split('</div>')[0];
  assert.ok(pending.includes('미확인'));
  assert.ok(!pending.includes('>0.0%</b>'));
});
test('recorded requests without token events stay unknown while tool results remain visible', () => {
  const b=board();b.run(`ui.usageSource='codex';data={sessions:[],sprint:{disabled:true},usage:{providers:{codex:{at:100,today:0,week:0,coverage:{tokens:'unavailable'},stats:{daily:[{tokens:0,tools:1,checked:0}],tools:[{tool:'RecordedTool',count:1,checked:0,unknown:1}]}}}}}`);
  const view=b.run('dashView()');
  assert.ok(view.includes('사용량을 계산할 토큰 기록이 없습니다.'));
  assert.ok(!view.includes('data-chart="hourly"'));
  assert.ok(view.includes('RecordedTool'));
});

function navigationBoard(search='') {
  const listeners=new Map(),elements=new Map(),storage=new Map(),calls=[];
  const listen=(name,type,fn)=>{const key=name+':'+type;listeners.set(key,[...(listeners.get(key)||[]),fn]);};
  const element=name=>{if(!elements.has(name))elements.set(name,{addEventListener:(type,fn)=>listen(name,type,fn),setAttribute(){},classList:{toggle(){}},querySelectorAll(){return [];}});return elements.get(name);};
  const location={search},history={state:null};
  for(const method of ['pushState','replaceState'])history[method]=(state,title,url)=>{history.state=state;location.search=new URL(url,'http://localhost').search;calls.push({method,state,url});};
  const ctx=vm.createContext({URLSearchParams,Date,console,location,history,
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},
    document:{getElementById:element,querySelector:element,addEventListener:(type,fn)=>listen('document',type,fn)},
    window:{addEventListener:(type,fn)=>listen('window',type,fn),getSelection:()=>''},setInterval(){},fetch:()=>new Promise(()=>{})});
  vm.runInContext(script,ctx);vm.runInContext('render=()=>{};data={sessions:[]}',ctx);
  return {run:s=>vm.runInContext(s,ctx),calls,location,history,
    event:(name,type,event)=>{for(const fn of listeners.get(name+':'+type)||[])fn(event);},
    click(name,matches){this.event(name,'click',{target:{closest:s=>matches[s]||null,classList:{contains:()=>false}},stopPropagation(){}});}};
}
test('navigation regression: deep links remain reloadable and explicit view changes add history only once', () => {
  const b=navigationBoard('?sid=__sprint');
  assert.equal(b.run('ui.sid'),'__sprint');assert.equal(new URLSearchParams(b.location.search).get('sid'),'__sprint');
  b.click('tabs',{'.tab':{dataset:{sid:'__dash'}}});
  b.click('document',{'[data-open-settings]':{dataset:{}}});
  b.run('render();render();render()');
  const pushes=b.calls.filter(c=>c.method==='pushState');
  assert.deepEqual(pushes.map(c=>c.state.sid),['__dash','__settings']);
  b.history.state=pushes[0].state;b.location.search='?sid=__dash';
  b.event('window','popstate',{state:pushes[0].state});
  assert.equal(b.run('ui.sid'),'__dash');
  assert.equal(b.calls.filter(c=>c.method==='pushState').length,2);
  assert.equal(navigationBoard(b.location.search).run('ui.sid'),'__dash');
});
test('navigation regression: browser back restores a session work key without adding a new entry', () => {
  const b=navigationBoard('?sid=__board');
  b.run('ui.work["session-a"]="project-one"');
  b.click('tabs',{'.tab':{dataset:{sid:'session-a'}}});
  const sessionEntry=b.calls.find(c=>c.method==='pushState');
  assert.ok(sessionEntry,'session navigation adds browser history');
  b.click('tabs',{'.tab':{dataset:{sid:'__dash'}}});
  b.run('ui.work["session-a"]="changed"');
  b.location.search='?sid=session-a';b.history.state=sessionEntry.state;
  b.event('window','popstate',{state:sessionEntry.state});
  assert.equal(b.run('ui.sid'),'session-a');assert.equal(b.run('ui.work["session-a"]'),'project-one');
  assert.equal(b.calls.filter(c=>c.method==='pushState').length,2);
});
test('navigation regression: history changes select the matching story before the next request', () => {
  const file=process.env.STORIES_HTML || path.join(__dirname,'..','stories.html');
  const code=fs.readFileSync(file,'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
  const listeners=new Map(),requests=[],location={hash:'#MP-A'},el={};
  const ctx=vm.createContext({Date,console,location,document:{getElementById:()=>el,addEventListener(){}},
    window:{addEventListener:(type,fn)=>listeners.set(type,fn)},setInterval(){},fetch:url=>{requests.push(url);return new Promise(()=>{});}});
  vm.runInContext(code,ctx);location.hash='#MP-B';listeners.get('hashchange')?.();
  assert.equal(vm.runInContext('selected',ctx),'MP-B');assert.equal(requests.at(-1),'/api/state?story=MP-B');
  location.hash='#MP-A';listeners.get('popstate')?.();
  assert.equal(vm.runInContext('selected',ctx),'MP-A');assert.equal(requests.at(-1),'/api/state?story=MP-A');
});

test('disabled Jira does not display zero completion or an invented pace', () => {
  const b=board();b.run('data={sessions:[],sprint:{disabled:true}}');
  const view=b.run('sprintView()');
  assert.ok(view.includes('Jira 연결이 꺼져 있습니다'));
  assert.ok(!view.includes('앞서') && !view.includes('뒤처져') && !view.includes('0%'));
});
test('usage labels its Claude source and missing data is not zero usage', () => {
  const b=board();b.run('data={sessions:[],sprint:{disabled:true},usage:{disabled:true}}');
  const view=b.run('dashView()');
  assert.ok(view.includes('Claude 연결이 꺼져 있습니다'));
  assert.ok(view.includes('Claude · 시간대별 토큰'));
  assert.ok(!view.includes('data-chart="hourly"'));
});

test('usage regression: failure bars use their absolute percentage for every tool', () => {
  const b=board();b.run('data={sessions:[],sprint:{disabled:true},usage:{at:100,stats:{tools:[{tool:"Bash",count:100,errors:13},{tool:"Read",count:100,errors:50},{tool:"Write",count:100,errors:0}]}}}');
  const view=b.run('dashView()');
  for (const [tool,percent] of [['Bash',13],['Read',50],['Write',0]]) {
    const row=view.slice(view.indexOf(`title="${tool} ·`)).split('</div>')[0];
    assert.match(row,new RegExp(`width:${percent}%`));
  }
});
test('usage regression: zero hourly tokens have an honest empty state', () => {
  const b=board();b.run('data={usage:{stats:{hours:Array(24).fill(0)}}}');
  const view=b.run('CHARTS.hourly(null,600,260)');
  assert.ok(view.includes('오늘 기록된 토큰이 없습니다'));
  assert.ok(!view.includes('-1시') && !view.includes('최고'));
});
test('usage regression: charts expose separate units and exact values without curve interpolation', () => {
  const b=board();b.run('data={usage:{stats:{daily:[{day:"2026-11-16",tokens:1234567,prompts:3},{day:"2026-11-17",tokens:0,prompts:0}],hours:Array(24).fill(1234567)}}}');
  for(const name of ['hourly','week']) {
    const chart=b.run(`CHARTS.${name}(null,600,260)`);
    assert.match(chart,/role="img"/);
    assert.ok(chart.includes('토큰') && chart.includes('1,234,567'));
    assert.ok(!/<path[^>]+d="[^"]* C/.test(chart));
  }
  assert.ok(b.run('CHARTS.week(null,600,260)').includes('요청 (건)'));
});
test('usage regression: numerical tables explain local coverage and all failure tools remain accessible', () => {
  const b=board();b.run('data={sessions:[],sprint:{disabled:true},usage:{at:100,stats:{daily:[{day:"2026-11-17",tokens:1234567,prompts:3}],hours:Array(24).fill(0),tools:[...Array.from({length:5},(_,i)=>({tool:"Common"+i,count:100,errors:0})),{tool:"RareFailure",count:1,errors:1}]}}}');
  const view=b.run('dashView()');
  assert.ok(view.includes('<table') && view.includes('1,234,567'));
  assert.ok(view.includes('시간별 수치 보기') && view.includes('일별 수치 보기'));
  assert.ok(view.includes('이 컴퓨터에 기록된') && view.includes('data-usage-source="codex"'));
  assert.ok(view.includes('data-key="usage-coverage"') && view.includes('수집'));
  assert.ok(view.includes('RareFailure'));
});
test('coverage keeps provider rules and unknown counts separate and does not invent missing counts', () => {
  const b=board();
  b.set({usage:{providers:{
    claude:{at:100,coverage:{tokens:'complete',notes:['Claude 캐시 포함 <기준>']},stats:{tools:[{unknown:7}]}},
    codex:{at:200,partial:true,coverage:{tokens:'partial',notes:['Codex 증가분 기준']},stats:{tools:[{unknown:4},{unknown:6}]}}
  }}});
  b.run("data=input;ui.usageSource='all'");
  let view=b.run('usageCoverage(data.usage)');
  assert.ok(view.includes('Claude 캐시 포함 &lt;기준&gt;') && view.includes('Codex 증가분 기준'));
  assert.ok(view.includes('7건') && view.includes('10건'));
  assert.ok(view.includes('기록 집계됨') && view.includes('일부 기록'));
  b.run("ui.usageSource='codex'");view=b.run('usageCoverage(data.usage.providers.codex)');
  assert.ok(!view.includes('Claude 캐시 포함') && view.includes('10건'));
  b.run("data.usage.providers.codex={unavailable:true,error:'기록 없음'}");view=b.run('usageCoverage(data.usage.providers.codex)');
  assert.ok(view.includes('집계 전') && !view.includes('0건'));
});
test('usage regression: account quota is removed and two summaries are beside failures', () => {
  const b=board();b.run('data={sessions:[],sprint:{at:100,items:[]},usage:{at:100,limit:{percent:13,at:100},stats:{}}}');
  const view=b.run('dashView()');
  assert.ok(!view.includes('계정 한도') && !view.includes('7일 사용 비율') && !view.includes('/usage'));
  const summary=view.match(/<div class="usage-summary">([\s\S]*?)<\/div>\s*<\/div>\s*<section class="pn/);
  assert.ok(summary,'AI session and sprint cards share the right summary column');
  assert.ok(summary[1].includes('AI 세션') && summary[1].includes('스프린트'));
});
test('usage regression: missing sources and stale reads remain distinct from collected zero', () => {
  const b=board();b.run('data={sessions:[],sprint:{disabled:true},usage:{unavailable:true,error:"Claude 기록 폴더가 없습니다.",stats:{}}}');
  let view=b.run('dashView()');
  assert.ok(view.includes('Claude 기록 폴더가 없습니다.'));
  assert.ok(!view.includes('data-chart="hourly"'));
  b.run('data.usage={at:100,stale:true,error:"사용량 기록을 읽지 못했습니다.",stats:{daily:[{day:"2026-11-17",tokens:10,prompts:1}],hours:Array(24).fill(0)}}');
  view=b.run('dashView()');
  assert.ok(view.includes('사용량 기록을 읽지 못했습니다.'));
  assert.ok(view.includes('이전 수집값'));
});
test('usage regression: both setting gears are centered and mirrored in their 16 unit viewbox', () => {
  const paths=[...html.split('<script>')[0].matchAll(/<button[^>]*data-open-settings[^>]*>[\s\S]*?<path d="([^"]+)"\/>[\s\S]*?<circle cx="8" cy="8" r="2"/g)].map(x=>x[1]);
  assert.equal(paths.length,2);
  for(const d of paths) {
    const tokens=d.match(/[A-Za-z]|[-+]?(?:\d*\.\d+|\d+)/g);let x=0,y=0,i=0,command;const points=[];
    while(i<tokens.length) {
      if(/[A-Za-z]/.test(tokens[i]))command=tokens[i++];
      if(/[Zz]/.test(command))break;
      const a=Number(tokens[i++]),b=/[HhVv]/.test(command)?0:Number(tokens[i++]);
      if(command==='M'||command==='L'){x=a;y=b;}else if(command==='m'||command==='l'){x+=a;y+=b;}
      else if(command==='h')x+=a;else if(command==='H')x=a;else if(command==='v')y+=a;else if(command==='V')y=a;else assert.fail('unsupported gear command');
      points.push([Number(x.toFixed(3)),Number(y.toFixed(3))]);
      if(command==='M')command='L';if(command==='m')command='l';
    }
    const values=new Set(points.map(p=>p.join(',')));
    for(const [x,y] of points) {
      assert.ok(values.has([Number((16-x).toFixed(3)),y].join(',')),`horizontal mirror missing for ${x},${y}`);
      assert.ok(values.has([x,Number((16-y).toFixed(3))].join(',')),`vertical mirror missing for ${x},${y}`);
    }
  }
});
test('task detail keeps current activity first and omits empty plan and filler results', () => {
  const b=board();load(b,fixture({session:{state:'작업 중'},task:{outcome:'진행',end_ts:0}}));
  const view=b.run('mainView(input.s)');
  assert.ok(view.indexOf('focus-panel')<view.indexOf('작업 진척'));
  assert.ok(!view.includes('plan-panel'));
  assert.ok(!view.includes('세션에서 작업을 진행하고 있습니다.'));
});
test('sprint subtask expansion is a named keyboard button', () => {
  const b=board();b.run('data={sessions:[],sprint:{at:100,items:[{key:"DEMO-1",title:"실제 제목",cat:"new",subs:1,subs_done:0,tags:[],subtasks:[],url:"https://example.com/DEMO-1"}]}}');
  const view=b.run('sprintView()');
  assert.match(view, /<button class="ir-x" data-sub="DEMO-1" aria-expanded="false"/);
  assert.ok(!view.includes('일정보다'));
});

test('URL text is not reused as a goal; latest user request is the source', () => {
  const b = board(); load(b, fixture({task:{notes:[{ts:105,stage:'계획',text:'원인을 고쳐 다시 적용하겠습니다.'}]}}));
  assert.equal(b.run('workGoal(W)'), '작업보드의 질문 오탐을 수정해 주세요.');
});
test('generated titles never replace the inspectable user request in the goal', () => {
  const b = board(); load(b, fixture({work:{title:'질문 오탐 수정',title_source:'summary'}}));
  assert.equal(b.run('workTitle(W)'), '질문 오탐 수정');
  assert.equal(b.run('workGoal(W)'), '작업보드의 질문 오탐을 수정해 주세요.');
});
test('a question is not a current activity when the scanner has no activity report', () => {
  const b=board();load(b,fixture({task:{outcome:'진행',end_ts:0,now:'',notes:[{ts:110,text:'어느 방식이 좋으신가요?'}]},session:{state:'작업 중'}}));
  assert.equal(b.run('workSummary(W)'), '현재 요청의 진행 보고가 아직 없습니다.');
});
test('work progress summarizes requests while checklist remains available in plan details', () => {
  const b=board();load(b,fixture({task:{progress:{source:'계획서 체크리스트',rel:'화면/개선/계획서/계획.md',ts:105,
    items:[{title:'조사',state:'done'},{title:'구현',state:'active'},{title:'검증',state:'pending'}]}}}));
  const p=JSON.parse(b.run('JSON.stringify(workProgress(W))'));
  assert.equal(p.done,1);assert.equal(p.total,3);
  assert.equal(p.current,'구현');
  const html=b.run('progressMarkup(W)');
  assert.ok(html.includes('요청 0/1 완료'));
  assert.ok(html.includes('작업 묶음 기준'));
  assert.ok(!html.includes('role="progressbar"'));
  assert.equal(b.run('latestPlan(W).items.length'),3);
});
test('a full checklist means checklist completion, not deployment or review approval', () => {
  const b=board();load(b,fixture({task:{outcome:'진행',end_ts:0,progress:{items:[{title:'구현',state:'done'}],source:'계획서 체크리스트'}},session:{state:'작업 중'}}));
  assert.equal(b.run('workProgress(W).done'),1);
  assert.equal(b.run('workBadge(W).word'),'작업 중');
  assert.ok(!b.run('progressMarkup(W)').includes('배포 완료'));
});
test('without an explicit denominator progress has no percentage or zero completion claim', () => {
  const b=board();load(b,fixture({task:{n_steps:80,n_checks:120},session:{state:'작업 중'}}));
  const html=b.run('progressMarkup(W)');
  assert.equal(b.run('workProgress(W).total'),0);
  assert.ok(!html.includes('role="progressbar"'));
  assert.ok(!html.includes('%'));
  assert.ok(html.includes('전체 작업 진행 단계'));
});
test('new request never inherits a prior request checklist', () => {
  const b=board();load(b,fixture({tasks:[task({progress:{items:[{title:'과거',state:'done'}]}}),task({ts:200,prompt:'새 작업 요청'})]}));
  assert.equal(b.run('workProgress(W).total'),0);
});
test('request progress is independent of Jira completion counts', () => {
  const b=board();load(b,fixture({work:{story:'MP-1',info:{subtasks:[{title:'조사',cat:'done'},{title:'구현',cat:'indeterminate'}]}}}));
  assert.equal(b.run('workProgress(W).total'),0);
  b.run('W.life="완료"; W.info.resolved_at=new Date(50000).toISOString()');
  assert.equal(b.run('workProgress(W).total'),0);
});
test('every work shows plan, work, changes and completion stages without Jira', () => {
  const b=board();load(b,fixture({task:{outcome:'진행',end_ts:0,n_checks:4,n_steps:1},session:{state:'작업 중'}}));
  assert.equal(b.run('requestStage(W).index'),2);
  const html=b.run('stageMarkup(W)');
  for(const label of ['계획','작업','수정','완료'])assert.ok(html.includes(label));
  assert.ok(html.includes('aria-current="step"'));
  b.run('W.last.n_steps=0');assert.equal(b.run('requestStage(W).index'),1);
  b.run('W.last.n_checks=0');assert.equal(b.run('requestStage(W).index'),0);
});
test('unfinished checklist, waiting or failure never becomes the completion stage', () => {
  const b=board();load(b,fixture({task:{n_steps:2,result:'일부 구현했습니다.',progress:{items:[{title:'남은 일',state:'pending'}]}},session:{state:'쉬는 중'}}));
  assert.notEqual(b.run('requestStage(W).index'),3);
  b.run('W.last.progress=null;W.last.outcome="실패"');
  assert.notEqual(b.run('requestStage(W).index'),3);
  b.run('W.last.outcome="완료";W.last.ask="반영할까요?"');
  assert.notEqual(b.run('requestStage(W).index'),3);
});
test('finished read-only request skips modifications and completes the request flow', () => {
  const b=board();load(b,fixture({task:{n_checks:4,result:'검토 결과를 전달했습니다.',outcome:'답변'},session:{state:'쉬는 중'}}));
  assert.equal(b.run('requestStage(W).index'),3);
  assert.ok(b.run('stageMarkup(W)').includes('수정 없음'));
  assert.equal(b.run('workBadge(W).word'),'응답 종료');
});
test('delegated sessions appear beneath their parent and remain reachable', () => {
  const b=board();load(b,fixture());
  b.run('const child={...input.s,id:"child",agent:"codex",parent_id:input.s.id,parent_work:input.w.key,role:"외부 평가"}; data.sessions.push(child)');
  assert.equal(b.run('rootSessions(data.sessions).length'),1);
  assert.ok(b.run('delegatedMarkup(W)').includes('data-open-session="child"'));
  assert.ok(b.run('delegatedMarkup(W)').includes('외부 평가'));
  assert.equal(b.run('boardEntries().length'),1);
  b.run('data.sessions=[child]');
  assert.equal(b.run('rootSessions(data.sessions).length'),1);
});
test('a parent with only an active delegated session remains visible', () => {
  const b=board();load(b,fixture({session:{state:'쉬는 중'}}));
  b.run('data.sessions.push({...input.s,id:"child",state:"작업 중",parent_id:input.s.id,parent_work:input.w.key})');
  assert.equal(b.run('displayPhase(input.s)'),'run');
  assert.equal(b.run('entryKind(W)'),'run');
  assert.equal(b.run('workBadge(W).word'),'하위 작업 중');
});
test('opening a parent without an explicit work index never stores NaN', () => {
  const b=board();load(b,fixture());
  b.run('selectSession(input.s.id,undefined)');
  assert.equal(b.run('ui.sid'), 'session-a');
  assert.equal(b.run('Number.isNaN(ui.work[input.s.id])'),false);
  assert.doesNotThrow(()=>b.run('mainView(input.s)'));
});
test('completed evaluator uses its own result rather than the parent Jira status', () => {
  const b=board();load(b,fixture({task:{n_checks:4,result:'정적 검토 결과를 전달했습니다.',outcome:'답변'},
    session:{state:'쉬는 중',parent_id:'parent',role:'외부 평가'},
    work:{story:'MP-1',stage:'리뷰 대기',info:{status:'진행 중',mrs:[{state:'opened'}],commits:[{title:'parent commit'}]}}}));
  assert.equal(b.run('workBadge(W).word'),'평가 완료');
  assert.equal(b.run('W.mrs.length'),0);
  assert.equal(b.run('W.commits.length'),0);
  assert.equal(b.run('workSummary(W)'),'정적 검토 결과를 전달했습니다.');
});
test('post-completion detail never shows an old Jira full-progress bar', () => {
  const b=board();load(b,fixture({task:{ts:200,outcome:'진행',end_ts:0},session:{state:'작업 중'},
    work:{story:'MP-1',stage:'완료',info:{resolved_at:new Date(50000).toISOString(),subtasks:[{title:'이전 항목',cat:'done'}]}}}));
  const html=b.run('mainView(input.s)');
  assert.ok(!html.includes('width:100%'));
  assert.ok(!html.includes('subbar'));
});
test('commit reports and generic future prose are not execution plans', () => {
  const b = board();
  load(b, fixture({task:{notes:[{ts:102,stage:'계획',text:'원인을 고쳐 다시 적용하겠습니다.'},
    {ts:103,stage:'진행',text:'변경 사항을 설명하겠습니다.\n1. cb240c8: 오래된 백업 정리를 수정했습니다.\n2. 60b0a41: 시험 보강입니다.'}]}}));
  assert.equal(b.run('latestPlan(W)'), null);
});
test('explicit plan retains only its steps, not the following result list', () => {
  const b = board();
  load(b, fixture({task:{notes:[{ts:102,stage:'계획',text:'작업 계획\n1. 질문 오탐을 재현합니다.\n2. 판정을 수정합니다.\n\n검증 결과\n- 테스트를 통과했습니다.\n- 반영했습니다.'}]}}));
  assert.deepEqual(JSON.parse(b.run('JSON.stringify(latestPlan(W).items)')), ['질문 오탐을 재현합니다.','판정을 수정합니다.']);
});
test('a new request does not inherit a previous plan or unrelated result', () => {
  const b = board();
  const tasks = [task({result:'입력하신 내용을 이해하지 못했습니다.',notes:[{ts:102,stage:'계획',text:'작업 계획\n1. 문서를 읽습니다.\n2. 설명을 작성합니다.'}]}),
    task({ts:200,last_ts:201,end_ts:0,outcome:'계획',prompt:'작업보드의 상태 표시 문제를 평가해 주세요.',notes:[{ts:201,stage:'계획',text:'질문 판정의 근거를 조사하고 있습니다.'}]})];
  load(b,fixture({tasks,session:{state:'작업 중'}}));
  assert.equal(b.run('latestPlan(W)'), null);
  assert.ok(!b.run('changesMarkup(W)').includes('입력하신 내용을 이해하지 못했습니다.'));
  assert.ok(b.run('changesMarkup(W)').includes('질문 판정의 근거'));
});
test('Jira completed work with an old silent session is completed without a current-progress claim', () => {
  const b = board();
  load(b,fixture({task:{outcome:'진행',start_ts:101,end_ts:0},session:{state:'작업 중',last_ts:Date.now()/1000-1800},
    work:{story:'MP-1',stage:'완료',info:{status:'완료',resolved_at:new Date(150000).toISOString()}}}));
  assert.equal(b.run('workBadge(W).word'), 'Jira 완료');
  assert.equal(b.run('entryKind(W)'), 'off');
  assert.ok(b.run('workSummary(W)').includes('완료'));
});
test('a genuine post-completion request is still visible when stalled', () => {
  const b = board();
  load(b,fixture({task:{ts:200,outcome:'진행',start_ts:201,end_ts:0},session:{state:'작업 중',last_ts:Date.now()/1000-1800},
    work:{story:'MP-1',stage:'완료',info:{status:'완료',resolved_at:new Date(150000).toISOString()}}}));
  assert.equal(b.run('workBadge(W).word'), '후속 활동 확인');
  assert.equal(b.run('entryKind(W)'), 'stall');
});
test('a completed issue never hides a real permission request', () => {
  const b = board();
  load(b,fixture({session:{state:'입력 대기',wait:{kind:'perm',text:'설치 권한 승인'}},work:{story:'MP-1',stage:'완료',info:{status:'완료'}}}));
  assert.equal(b.run('entryKind(W)'), 'wait');
  assert.ok(b.run('workSummary(W)').includes('설치 권한 승인'));
});
test('end of an AI turn is not declared business completion; failures stay failures', () => {
  const b = board();load(b,fixture({task:{outcome:'완료',start_ts:101,result:'수정했습니다.'}}));
  assert.equal(b.run('workBadge(W).word'), '응답 종료');
  const failed = board();load(failed,fixture({task:{outcome:'실패',start_ts:101}}));
  assert.equal(failed.run('workBadge(W).word'), '실행 실패');
});
test('activity labels distinguish attempts and other actions without implying successful checks', () => {
  const b = board();load(b,fixture({task:{n_steps:1,n_checks:3}}));
  const markup = b.run('changesMarkup(W)');
  assert.ok(markup.includes('변경 시도 1회'));
  assert.ok(markup.includes('조회·기타 실행 3회'));
  assert.ok(markup.includes('검수'));
});
test('completed task detail explains idle session without calling the task in progress', () => {
  const b = board();load(b,fixture({task:{outcome:'진행',start_ts:101,end_ts:0},
    session:{state:'작업 중',last_ts:Date.now()/1000-1800},
    work:{story:'MP-1',stage:'완료',info:{status:'완료',resolved_at:new Date(150000).toISOString()}}}));
  const view=b.run('mainView(input.s)');
  assert.ok(view.includes('Jira에서 완료된 작업'));
  assert.ok(!view.includes('현재 작업이 진행 중입니다.'));
});
test('unknown completion time is explicit and does not claim current execution', () => {
  const b=board();load(b,fixture({task:{outcome:'진행',start_ts:101,end_ts:0},
    session:{state:'작업 중',last_ts:Date.now()/1000-1800},work:{story:'MP-1',stage:'완료',info:{status:'완료'}}}));
  assert.equal(b.run('entryKind(W)'), 'stall');
  assert.ok(b.run('workSummary(W)').includes('완료 시점'));
  assert.ok(!b.run('mainView(input.s)').includes('현재 작업이 진행 중입니다.'));
});
test('short approval preserves the last explicit plan with its previous-request origin', () => {
  const b=board();load(b,fixture({tasks:[task({notes:[{ts:102,stage:'계획',text:'작업 계획\n1. 질문 오탐을 재현합니다.\n2. 판정을 수정합니다.'}]}),task({ts:200,prompt:'그대로 진행해 주세요.'})]}));
  assert.equal(b.run('latestPlan(W)?.previous'), true);
  assert.ok(b.run('mainView(input.s)').includes('이전 요청에서 세운 계획'));
});
test('new read snapshot reports subsequent activity exactly once', () => {
  const b=board();load(b,fixture({task:{n_steps:1,n_checks:3}}));
  b.run('ui.reviewed[workKey(W)]=reviewSnapshot(W); W.last.n_checks=5; W.nChecks=5');
  assert.deepEqual(JSON.parse(b.run('JSON.stringify(workChanges(W).facts)')), ['조회·기타 실행 2회']);
  b.run('ui.reviewed[workKey(W)]=reviewSnapshot(W)');
  assert.equal(b.run('workChanges(W).changed'), false);
});
test('a final answer containing a plan remains available after approval', () => {
  const b=board();load(b,fixture({tasks:[task({notes:[],result:'작업 계획\n1. 질문 오탐을 재현합니다.\n2. 판정을 수정합니다.\n\n진행할까요?'}),task({ts:200,prompt:'그대로 진행해 주세요.'})]}));
  assert.equal(b.run('latestPlan(W)?.previous'),true);
  assert.deepEqual(JSON.parse(b.run('JSON.stringify(latestPlan(W)?.items)')),['질문 오탐을 재현합니다.','판정을 수정합니다.']);
});
test('past-tense report heading never turns its list into an execution plan', () => {
  const b=board();load(b,fixture({task:{notes:[{ts:102,stage:'진행',text:'다음 순서로 커밋했습니다.\n1. 로그인 오류 수정\n2. 회귀 테스트 보강'}]}}));
  assert.equal(b.run('latestPlan(W)'),null);
});
test('goal uses the extracted user request instead of the beginning of a pasted table', () => {
  const b=board();load(b,fixture({task:{prompt:'어려움 실제 상황 ━━━━━━━━━━ 1. 긴 설명',request:'작업보드를 평가해 주세요.'}}));
  assert.equal(b.run('workGoal(W)'),'작업보드를 평가해 주세요.');
});

test('new child request preserves changes already made within the whole work',()=>{
  const b=board();load(b,fixture({tasks:[task({n_steps:3,result:'수정 결과',outcome:'완료'}),task({ts:200,end_ts:0,outcome:'계획',prompt:'더 짧게 해주세요.'})],session:{state:'작업 중'}}));
  const p=JSON.parse(b.run('JSON.stringify(unitProgress(W))'));
  assert.equal(p.stage.index,2);assert.equal(p.done,1);assert.equal(p.total,2);
  assert.equal(b.run('requestStage(W).index'),0);
  assert.ok(b.run('progressMarkup(W)').includes('요청 1/2 완료'));
});
test('Claude and Codex requests under the same Story share progress and an overview card',()=>{
  const b=board();load(b,fixture({task:{result:'완료',outcome:'완료',n_steps:2},work:{key:'ABC-2',story:'ABC-2',unit:{key:'ABC-1',kind:'story',title:'스토리 제목'}}}));
  b.run('const other={...input.s,id:"other",agent:"claude",state:"작업 중",tasks:[{...input.s.tasks[0],ts:200,end_ts:0,outcome:"계획",result:"",n_steps:0}],works:[{...input.w,key:"ABC-3",story:"ABC-3"}]};data.sessions.push(other)');
  assert.equal(b.run('unitProgress(W).total'),2);
  assert.equal(b.run('unitProgress(W).done'),1);
  assert.equal(b.run('unitProgress(W).stage.index'),2);
  assert.equal(b.run('boardEntries().length'),1);
  assert.equal(b.run('workTitle(W)'),'스토리 제목');
  assert.ok(b.run('progressMarkup(W)').includes('스토리 기준'));
  assert.ok(b.run('historyContent(W)').includes('2개 요청'));
});
test('independent tasks and non-Jira sessions in the same folder stay separate',()=>{
  const b=board();load(b,fixture({task:{result:'끝'}}));
  b.run('data.sessions.push({...input.s,id:"unrelated",works:[{...input.w}],tasks:[{...input.s.tasks[0]}]})');
  assert.equal(b.run('unitProgress(W).total'),1);
  assert.equal(b.run('boardEntries().length'),2);
  b.run('input.w.unit={key:"ABC-1",kind:"task"};data.sessions[1].works[0].unit={key:"ABC-2",kind:"task"}')
  assert.equal(b.run('boardEntries().length'),2);
});
test('queued followups accumulate without independently inventing a completion outcome',()=>{
  const b=board();load(b,fixture({task:{end_ts:0,outcome:'진행',extra:[{ts:105,request:'아이콘으로 표시해주세요.'}]},session:{state:'작업 중'}}));
  assert.equal(b.run('unitProgress(W).total'),2);
  assert.equal(b.run('unitProgress(W).done'),0);
  assert.ok(b.run('progressMarkup(W)').includes('아이콘으로 표시해주세요.'));
});
test('failure interruption and waiting keep whole work incomplete even if Jira says done',()=>{
  for(const outcome of ['실패','중단']) {
    const b=board();load(b,fixture({tasks:[task({outcome,result:'남은 문제'}),task({ts:200,outcome:'완료',result:'후속 답변'})],work:{stage:'완료'}}));
    assert.equal(b.run('unitProgress(W).stage.complete'),false);
    assert.equal(b.run('unitProgress(W).done'),1);
  }
  const b=board();load(b,fixture({task:{result:'질문'},session:{state:'입력 대기',wait:{kind:'perm',text:'승인'}}}));
  assert.equal(b.run('unitProgress(W).done'),0);
});
test('evaluators remain out of the sidebar and reachable through collapsed task details',()=>{
  const b=board();load(b,fixture({task:{result:'끝',outcome:'완료'}}));
  b.run('data.sessions.push({...input.s,id:"child",state:"작업 중",parent_id:input.s.id,parent_work:input.w.key,role:"외부 평가",tasks:[{...input.s.tasks[0],end_ts:0,outcome:"진행"}]})');
  assert.equal(b.run('unitProgress(W).total'),1);
  assert.equal(b.run('unitProgress(W).stage.complete'),false);
  assert.ok(!b.run('tabs(data.sessions)').includes('data-open-session="child"'));
  assert.ok(b.run('mainView(input.s)').includes('data-open-session="child"'));
  assert.ok(!b.run('delegatedMarkup(W)').match(/<details[^>]* open/));
  b.run('ui.infoOpen=true');
  assert.ok(b.run('mainView(input.s)').includes('data-open-session="child"'));
});
test('compact child table is bounded while history retains all requests',()=>{
  const b=board();load(b,fixture({tasks:Array.from({length:8},(_,i)=>task({ts:i+1,result:'끝'}))}));
  assert.equal((b.run('progressMarkup(W)').match(/<tr>/g)||[]).length,6);
  assert.ok(b.run('progressMarkup(W)').includes('전체 8개 요청'));
  assert.ok(b.run('historyContent(W)').includes('8개 요청'));
});

test('hidden evaluators and earlier failed delegated requests still block whole-work completion',()=>{
  const b=board();load(b,fixture({task:{result:'끝',outcome:'완료'}}));
  b.run('const child={...input.s,id:"child",parent_id:input.s.id,parent_work:input.w.key,state:"작업 중",tasks:[{...input.s.tasks[0],end_ts:0,outcome:"진행"}]};data.sessions.push(child);ui.hidden={child:child.last_ts}');
  assert.equal(b.run('unitProgress(W).stage.complete'),false);
  b.run('child.state="종료됨";child.tasks=[{...input.s.tasks[0],outcome:"실패"},{...input.s.tasks[0],ts:200}];child.works=[{...input.w,tasks:[0,1]}]');
  assert.equal(b.run('unitProgress(W).stage.complete'),false);
  b.run('child.tasks[0].outcome="완료"');
  assert.equal(b.run('unitProgress(W).stage.complete'),true);
});
test('native subagents block completion for the current work',()=>{
  const b=board();load(b,fixture({task:{result:'끝',outcome:'완료'},session:{agents:{running:[{ts:105,what:'review'}]}}}));
  assert.equal(b.run('unitProgress(W).stage.complete'),false);
  b.run('input.s.agents.running=[]');
  assert.equal(b.run('unitProgress(W).stage.complete'),true);
});

test('action toolbar shows live servers and accurately labels application-only focus',()=>{
 const b=board();load(b,fixture({session:{runtime:{servers:[{url:'http://127.0.0.1:5173/',port:5173}],terminal:{label:'Passport 열기',detail:'탭은 직접 선택'}}}}));
 const html=b.run('actionToolbar(W)');
 assert.ok(html.includes('http://127.0.0.1:5173/'));assert.ok(html.includes('target="_blank"'));
 assert.ok(html.includes('터미널 열기'));assert.ok(!html.includes('정확한 세션으로'));
});
test('missing terminal or servers never creates a fake open action',()=>{
 const b=board();load(b,fixture());const html=b.run('actionToolbar(W)');
 assert.ok(!html.includes('data-focus-session'));assert.ok(!html.includes('data-server-url'));
 assert.ok(html.includes('스킬'));
});
test('skill favorites and names are data-driven and escaped',()=>{
 const b=board();load(b,fixture());b.run('ui.actionData={[W.s.id]:{skills:[{id:"claude:user::skill:report",label:"주간보고",agent:"claude",favorite:true,available:true},{id:"bad",label:"<img onerror=alert(1)>",description:"",agent:"codex",available:true}]}};ui.skillsOpen=true');
 b.run('ui.skillContext={sid:W.s.id,settingsOnly:false,folder:W.s.cwd}');
 b.run('ui.skillMode="new"');
 const html=b.run('skillDialogContent()');assert.ok(html.includes('주간보고'));assert.ok(html.includes('data-select-skill'));
 assert.ok(!html.includes('<img onerror'));assert.ok(html.includes('&lt;img'));
});

test('disabled automatic connections disappear without changing runtime evidence',()=>{
 const b=board();load(b,fixture({session:{runtime:{servers:[{url:'http://127.0.0.1:5173/',port:5173}],terminal:{label:'Any app',detail:'app only'}}}}));
 b.run('ui.actionData={[W.s.id]:{settings:{auto_servers:false,auto_terminal:false},skills:[]}}');
 const html=b.run('actionToolbar(W)');assert.ok(!html.includes('data-server-url'));assert.ok(!html.includes('data-focus-session'));
});
test('settings expose AI registration help without a preset report or app',()=>{
 const b=board();b.run('data={sessions:[]};ui.actionData={"":{settings:{auto_servers:true,auto_terminal:true,launch_app:""},shortcuts:[],guide:"wb actions add; 등록은 실행하지 않습니다."}}');
 b.run('ui.settingsTab="guide"');
 const html=b.run('settingsView()');assert.ok(html.includes('AI용 도움말'));assert.ok(html.includes('data-copy-action-guide'));
 b.run('ui.settingsTab="skills"');assert.ok(b.run('settingsView()').includes('등록한 바로가기가 없습니다'));assert.ok(!html.includes('주간보고'));assert.ok(!html.includes('Passport'));
});

test('settings is outside scrolling work navigation and has a fixed footer entry',()=>{
 const b=board();assert.ok(!b.run('boardNav()').includes('__settings'));
 assert.match(html,/<div class="side-foot">[\s\S]*?data-open-settings/);
});
test('connection settings render schema values and preserve an editing draft',()=>{
 const b=board();b.run('data={sessions:[]};ui.settingsTab="connections";ui.actionData={"":{connections:[{kind:"jira",label:"Jira",description:"이슈",registered:true,values:{enabled:true,project:"ABC"},fields:[{key:"project",label:"프로젝트 키",type:"text"}],help:"로그인 확인"}]}}');
 assert.ok(b.run('settingsView()').includes('data-connection-edit="jira"'));
 b.run('openConnection("jira");ui.connectionDraft.project="CHANGED"');
 assert.ok(b.run('settingsView()').includes('value="CHANGED"'));
 assert.ok(!b.run('settingsView()').includes('value="ABC"'));
});

test('fresh board offers explicit agent connections and optional work sources',()=>{
 const b=board();
 b.run('data={sessions:[],connections:["claude","codex","git","documents","jira","gitlab","summary"].map(kind=>({kind,label:kind,description:"연결",registered:false,values:{enabled:false},fields:[],source_paths:kind==="claude"?["/Users/another person/.claude/projects"]:[],help:"원본을 변경하지 않습니다."}))}');
 const view=b.run('overview()');
 assert.ok(view.includes('data-key="connection-onboarding"'));
 for(const kind of ['claude','codex','git','documents','jira','gitlab','summary'])assert.ok(view.includes(`data-connect-kind="${kind}"`));
 assert.ok(view.includes('/Users/another person/.claude/projects'));
 assert.match(view,/<details[^>]*data-key="setup-source-claude"[^>]*><summary>기록 위치<\/summary>/);
 assert.ok(!/<details[^>]*data-key="setup-source-claude"[^>]*\bopen\b/.test(view));
 assert.ok(!view.includes('context-table'));
 b.run('openConnection("claude",true)');
 assert.equal(b.run('ui.connectionDraft.enabled'),true);
 assert.equal(b.run('data.connections[0].values.enabled'),false);
 b.run('data.connections[0].registered=true;data.connections[0].values.enabled=true');
 assert.ok(!b.run('overview()').includes('data-key="connection-onboarding"'));
});

test('work names stay generic for any repository when AI summaries are disabled',()=>{
 const b=board();assert.equal(b.run('workName({key:"work-board"})'),'work-board');
});

test('a fresh installation opens connection setup even when the browser remembers an old tab',async()=>{
 const b=board();
 b.run('render=()=>{};ui.sid=SETTINGS;ui.fromLink=false;fetch=async()=>({ok:true,json:async()=>({now:Date.now()/1000,sessions:[],connections:[{kind:"claude",registered:false,values:{enabled:false}}]})})');
 await b.run('refresh()');assert.equal(b.run('ui.sid'),'__board');
 b.run('ui.sid=SETTINGS');await b.run('refresh()');assert.equal(b.run('ui.sid'),'__settings');
});

test('agent setup exposes read locations before saving and missing sources after checking',()=>{
 const b=board();
 b.run('data={sessions:[]};ui.actionData={"":{connections:[{kind:"codex",label:"Codex",description:"세션",registered:false,values:{enabled:false},fields:[],source_paths:["/Users/another person/.codex/state_5.sqlite"],help:"원본 보존"}]}};openConnection("codex",true)');
 let view=b.run('connectionsView()');
 assert.ok(view.includes('/Users/another person/.codex/state_5.sqlite'));
 assert.ok(view.includes('연결 시작'));
 b.run('ui.connectionEdit=null;ui.actionData[""].connections[0].registered=true;ui.actionData[""].connections[0].values.enabled=true;ui.actionData[""].connections[0].check={status:"missing_source",ok:false,checked_at:Date.now()/1000,message:"저장소를 찾지 못했습니다"}');
 view=b.run('connectionsView()');
 assert.ok(view.includes('기록 위치 확인'));assert.ok(view.includes('저장소를 찾지 못했습니다'));
});

test('macOS and Windows updates replace the app and restart without manual drag steps',()=>{
 const b=board();b.run('data={sessions:[]};ui.updates={status:"ready",available:true,os:"macos"}');
 for(const os of ['macos','windows']) {
  b.run(`ui.updates.os="${os}"`);const view=b.run('updatesView()');
  assert.ok(view.includes('업데이트 설치'));assert.ok(view.includes('다시 시작'));assert.ok(!view.includes('드래그'));assert.ok(!view.includes('DMG 열기'));
 }
});

test('current-session skills match the connected agent and disconnected delivery stays disabled',()=>{
 const b=board();load(b,fixture());b.run('W.s.agent="codex";ui.skillContext={sid:W.s.id};ui.actionData={[W.s.id]:{connection:{available:false,message:"연결 없음"},skills:[{id:"c",agent:"claude",name:"claude-only",label:"claude-only"},{id:"x",agent:"codex",name:"codex-only",label:"codex-only"}]}}');
 b.run('skillDraft(W.s.id).selected="x";ui.skillStep="compose"');const html=b.run('skillDialogContent()');assert.ok(html.includes('현재 세션에서 실행'));assert.ok(!html.includes('claude-only'));assert.ok(html.includes('codex-only'));assert.ok(html.includes('data-run-skill="x" disabled'));assert.ok(html.includes('연결 없음'));
});

test('environment controls follow backend capabilities on Mac, Windows and older servers',()=>{
 const b=board();b.run('data={sessions:[]};ui.settingsTab="environment"');
 for(const [environment,terminal,path,shell] of [
  [{os:'macos',terminal_focus_supported:true,terminal_app_configurable:true,shell_label:'터미널'},true,true,'터미널'],
  [{os:'windows',terminal_focus_supported:false,terminal_app_configurable:false,shell_label:'PowerShell'},false,false,'PowerShell'],
  [undefined,false,false,'이 컴퓨터']]) {
  b.set({settings:{auto_servers:true,auto_terminal:true,launch_app:'/Applications/Terminal.app'},environment});b.run('ui.actionData={"":input}');
  const view=b.run('settingsView()');assert.equal(view.includes('name="auto_terminal"'),terminal);assert.equal(view.includes('name="launch_app"'),path);assert.ok(view.includes(shell));
 }
});
test('saving Windows environment preserves unsupported settings by omitting hidden fields',()=>{
 const b=navigationBoard();b.run('ui.actionData={"":{settings:{auto_servers:true,auto_terminal:true,launch_app:"/Applications/Custom.app"}}};boardAction=(action,fields)=>{globalThis.saved=fields.settings}');
 const form={matches:s=>s==='[data-action-settings]',elements:{auto_servers:{checked:false}}};
 b.event('main','submit',{target:form,preventDefault(){}});
 assert.deepEqual(JSON.parse(b.run('JSON.stringify(saved)')),{auto_servers:false});
 b.event('main','input',{target:{dataset:{},closest:s=>s==='form'||s==='[data-action-settings]'?form:null}});
 assert.deepEqual(JSON.parse(b.run('JSON.stringify(ui.environmentDraft)')),{auto_servers:false});
});
test('browser update settings link to official releases and distinguish installation requirements',()=>{
 const b=board();b.run('data={sessions:[]};ui.settingsTab="updates";ui.actionData={"":{app:{version:"1.2.0-rc.1",release_url:"https://github.com/nimo0711/huginn/releases"},environment:{os:"windows"}}}');
 const view=b.run('settingsView()');assert.ok(view.includes('Windows'));assert.ok(view.includes('1.2.0-rc.1'));assert.ok(view.includes('설치본'));assert.ok(view.includes('href="https://github.com/nimo0711/huginn/releases"'));assert.ok(!view.includes('data-update-action="Check"'));
 b.run('ui.actionData={}');assert.ok(b.run('settingsView()').includes('href="https://github.com/nimo0711/huginn/releases"'));
});
test('update settings show bounded progress, recoverable states and safe links without raw errors',()=>{
 const b=board();b.run('data={sessions:[]};ui.settingsTab="updates"');
 for(const [status,match] of [['available','업데이트 다운로드'],['ready','업데이트 설치'],['auth_required','접근 권한'],['unpublished','게시된 릴리즈'],['current','최신 버전'],['error','다시 확인'],['installing','다시 시작']]) {
  b.set({status,available:true,current_version:'1.2.0',latest_version:'1.2.1',os:'macos',signed:false,message:'secret technical /tmp/token',release_url:'javascript:alert(1)'});b.run('ui.updates=input');
  const view=b.run('settingsView()');assert.ok(view.includes(match),status);assert.ok(!view.includes('secret technical'));assert.ok(!view.includes('javascript:'));assert.ok(!view.includes('설치가 완료'));
 }
 b.run('ui.updates={status:"downloading",available:true,downloaded_bytes:1000,total_bytes:10}');
 const progress=b.run('settingsView()');assert.ok(progress.includes('value="100"'));assert.ok(progress.includes('max="100"'));assert.ok(!progress.includes('10000%'));
});
function updateBoard() {
 const timers=new Map();let sequence=0;
 const b=board({setTimeout:(fn,ms)=>{const id=++sequence;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id)});
 b.run('render=()=>{};data={sessions:[]};ui.sid=SETTINGS;ui.settingsTab="updates"');
 return {...b,timers,tick:async()=>{const entry=[...timers.entries()].find(([,t])=>t.ms<1000);if(entry){timers.delete(entry[0]);await entry[1].fn();await Promise.resolve();}}};
}
test('updates are checked once a day in the background and a new version shows a sidebar notice',async()=>{
 const notice={hidden:true,text:{textContent:''},querySelector(){return this.text;}};
 const b=updateBoard();b.run('ui.sid=null;ui.updates=null;globalThis.checks=0');
 b.set(notice);b.run('document.querySelectorAll=s=>s==="[data-open-updates]"?[input]:[]');
 const day=24*60*60*1000,hour=60*60*1000,start=Date.UTC(2026,10,23,9);
 b.run('window.go={main:{AppUpdates:{Info:async()=>ui.updates,Check:async()=>{checks++;return globalThis.next;},Download(){},Install(){}}}}');
 b.run('globalThis.next={status:"current",available:true,current_version:"1.2.0-rc.13",latest_version:"1.2.0-rc.13"}');
 await b.run(`autoCheckUpdates(${start})`);assert.equal(b.run('checks'),1);assert.equal(notice.hidden,true);
 await b.run(`autoCheckUpdates(${start+day-1})`);assert.equal(b.run('checks'),1);
 b.run('globalThis.next={status:"error",available:true}');
 await b.run(`autoCheckUpdates(${start+day})`);assert.equal(b.run('checks'),2);
 await b.run(`autoCheckUpdates(${start+day+hour-1})`);assert.equal(b.run('checks'),2);
 b.run('globalThis.next={status:"available",available:true,current_version:"1.2.0-rc.13",latest_version:"1.2.0-rc.14"}');
 await b.run(`autoCheckUpdates(${start+day+hour})`);assert.equal(b.run('checks'),3);
 assert.equal(notice.hidden,false);assert.equal(notice.text.textContent,'업데이트가 있습니다 · 1.2.0-rc.14');
 await b.run(`autoCheckUpdates(${start+3*day})`);assert.equal(b.run('checks'),3);
 const nav=navigationBoard();nav.run('ui.settingsTab="connections"');
 nav.click('document',{'[data-open-updates]':{dataset:{}}});
 assert.equal(nav.run('ui.sid===SETTINGS && ui.settingsTab'),'updates');
});
test('native binding appearing after startup initializes updates and leaving settings stops polling',async()=>{
 const b=updateBoard();b.run('syncUpdates()');assert.ok(b.timers.size>0);
 b.run('window.go={main:{AppUpdates:{Info:async()=>({status:"idle",available:true,current_version:"2.0.0",os:"windows"}),Check(){},Download(){},Install(){}}}}');
 await b.tick();assert.equal(b.run('ui.updates.current_version'),'2.0.0');
 b.run('ui.updates.status="downloading";syncUpdates()');assert.ok(b.timers.size>0);
 b.run('ui.settingsTab="environment";syncUpdates()');assert.equal(b.timers.size,0);
});
test('rejected native actions clear busy state and repeated download clicks start one operation',async()=>{
 const b=updateBoard();b.run('ui.updates={status:"available",available:true};window.go={main:{AppUpdates:{Info:async()=>ui.updates,Check:async()=>{throw new Error("private technical error")},Download:()=>new Promise(resolve=>{globalThis.finishDownload=resolve;globalThis.downloadCalls=(globalThis.downloadCalls||0)+1}),Install(){}}}}');
 await b.run('runUpdateAction("Check")');assert.equal(b.run('ui.updateBusy'),false);assert.equal(b.run('ui.updates.status'),'error');assert.ok(!b.run('settingsView()').includes('private technical error'));
 b.run('ui.updates.status="available"');const first=b.run('runUpdateAction("Download")');await b.run('runUpdateAction("Download")');assert.equal(b.run('downloadCalls'),1);
 b.run('finishDownload({status:"ready",available:true,current_version:"1.0.0",latest_version:"1.0.1"})');await first;assert.equal(b.run('ui.updateBusy'),false);assert.equal(b.run('ui.updates.status'),'ready');
 b.run('ui.sid=BOARD;syncUpdates()');assert.equal(b.timers.size,0);
});

test('unsupported terminal focus stays hidden even if old session runtime data remains',()=>{
 const b=board();load(b,fixture({session:{runtime:{terminal:{label:'Old terminal',detail:'stale'}}}}));
 b.run('ui.actionData={[W.s.id]:{environment:{os:"windows",terminal_focus_supported:false}}}');
 assert.ok(!b.run('actionToolbar(W)').includes('data-focus-session'));
});
test('late native Info cannot overwrite a completed update check',async()=>{
 const b=updateBoard();b.run('window.go={main:{AppUpdates:{Info:()=>new Promise(resolve=>{globalThis.finishInfo=resolve}),Check:async()=>({status:"available",available:true,latest_version:"2.0.1"}),Download(){},Install(){}}}}');
 const info=b.run('readUpdateInfo()');await Promise.resolve();await b.run('runUpdateAction("Check")');
 b.run('finishInfo({status:"idle",available:true})');await info;
 assert.equal(b.run('ui.updates.status'),'available');assert.equal(b.run('ui.updates.latest_version'),'2.0.1');
 b.run('ui.sid=BOARD;syncUpdates()');assert.equal(b.timers.size,0);
});
test('update Info timeout leaves a retry action rather than an endless loading state',async()=>{
 const b=updateBoard();b.run('window.go={main:{AppUpdates:{Info:()=>new Promise(()=>{}),Check(){},Download(){},Install(){}}}}');
 const info=b.run('readUpdateInfo()');await Promise.resolve();
 const timeout=[...b.timers.values()].find(x=>x.ms===10000);assert.ok(timeout);timeout.fn();await info;
 assert.equal(b.run('ui.updateInfoBusy'),false);assert.equal(b.run('ui.updates.status'),'error');assert.ok(b.run('settingsView()').includes('data-update-action="Check"'));
});
test('returning while an old Info call is pending reads fresh state once that call settles',async()=>{
 const b=updateBoard();b.run('window.go={main:{AppUpdates:{Info:()=>new Promise(resolve=>{globalThis.finishInfo=resolve}),Check(){},Download(){},Install(){}}}};ui.updates={status:"idle",available:true};syncUpdates()');
 await Promise.resolve();b.run('ui.settingsTab="environment";syncUpdates();ui.settingsTab="updates";syncUpdates()');
 b.run('window.go.main.AppUpdates.Info=async()=>({status:"ready",available:true});finishInfo({status:"idle",available:true})');
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(b.run('ui.updates.status'),'ready');
 b.run('ui.sid=BOARD;syncUpdates()');assert.equal(b.timers.size,0);
});
test('unknown and negative download lengths never display invalid progress values',()=>{
 const b=board();b.run('data={sessions:[]};ui.settingsTab="updates"');
 b.run('ui.updates={status:"downloading",available:true,downloaded_bytes:-5,total_bytes:100}');assert.ok(b.run('settingsView()').includes('value="0"'));
 b.run('ui.updates.total_bytes=0');const view=b.run('settingsView()');assert.ok(!/<progress[^>]+value=/.test(view));assert.ok(!view.includes('NaN'));
});

test('release metadata controls the GitHub destination while malformed or foreign URLs use the fallback',()=>{
 const b=board();b.run('data={sessions:[]};ui.settingsTab="updates"');
 b.run('ui.actionData={"":{app:{release_url:"https://github.com/example-owner/Huginn-next/releases"}}}');
 assert.ok(b.run('settingsView()').includes('href="https://github.com/example-owner/Huginn-next/releases"'));
 b.run('ui.updates={status:"available",available:true,release_url:"https://github.com/example-owner/Huginn-next/releases/tag/v2.0.0-rc.1",target:"macos-arm64",os:"macos"}');
 const view=b.run('settingsView()');assert.ok(view.includes('href="https://github.com/example-owner/Huginn-next/releases/tag/v2.0.0-rc.1"'));assert.ok(view.includes('macOS · Apple Silicon'));assert.ok(!view.includes('macos-arm64'));
 for(const url of ['javascript:alert(1)','https://github.com.evil.test/a/b/releases','https://github.com/a/b/releases?token=secret','https://github.com/a/../releases']) {
  b.set(url);b.run('ui.updates.release_url=input');assert.ok(b.run('settingsView()').includes('href="https://github.com/nimo0711/huginn/releases"'));
 }
});

test('known native failure and cancellation messages remain actionable without exposing unexpected errors',()=>{
 const b=board();b.run('data={sessions:[]};ui.settingsTab="updates"');
 for(const [status,message] of [
  ['error','배포 파일의 버전·대상·크기 또는 체크섬이 올바르지 않습니다.'],
  ['error','업데이트를 처리하지 못했습니다. 네트워크 연결을 확인한 뒤 다시 시도해 주세요.'],
  ['error','업데이트 작업이 취소되었습니다.'],
  ['ready','설치를 취소했습니다. 다운로드한 파일은 다시 설치할 수 있습니다.'],
  ['ready','업데이트 설치를 시작하지 못했습니다. 다시 시도해 주세요.']]) {
  b.set({status,message,available:true});b.run('ui.updates=input');assert.ok(b.run('settingsView()').includes(message));
 }
 b.run('ui.updates.message="GET https://private.test?token=secret failed"');assert.ok(!b.run('settingsView()').includes('token=secret'));
});

test('delegation details pair original instructions and results without exposing HTML',()=>{
  const b=board(),f=fixture({task:{delegations:[{id:'review',agent:'claude',label:'서버 검수',prompt:'<script>bad()</script>\n읽기 전용',result:'문제 없음',status:'completed',ts:100,end_ts:120,last_ts:120}]}});
  load(b,f);
  const view=b.run('delegatedMarkup(W)');
  assert.ok(view.includes('위임한 지시'));
  assert.ok(view.includes('결과'));
  assert.ok(view.includes('&lt;script&gt;bad()&lt;/script&gt;'));
  assert.ok(!view.includes('<script>bad()'));
  assert.ok(view.includes('문제 없음'));
  assert.ok(view.includes('20초'));
  assert.ok(view.includes('<details'));
});
test('completed native children remain under the launch work and unresolved children are not called complete',()=>{
  const b=board(),f=fixture({session:{agents:{items:[{id:'one',agent:'claude',label:'이전 작업',parent_task_ts:10,status:'completed',result:'이전 결과'},{id:'two',agent:'claude',label:'현재 검수',parent_task_ts:100,status:'unknown',prompt:'검수',last_ts:110,ts:101}],running:[],done:1}}});
  load(b,f);
  const view=b.run('delegatedMarkup(W)');
  assert.ok(view.includes('현재 검수'));
  assert.ok(!view.includes('이전 결과'));
  assert.ok(view.includes('활동 확인'));
  assert.ok(!view.includes('완료 1'));
});
test('parent navigation names the actual provider and exposes the handoff in child details',()=>{
  const b=board(),f=fixture({session:{parent_id:'parent',parent_work:'work',handoff:{prompt:'원본 지시'}}});
  load(b,f);
  b.run("data.sessions.push({id:'parent',agent:'codex',works:[{key:'work'}]})");
  const view=b.run('handoffMarkup(W)');
  assert.ok(view.includes('부모 Codex 작업으로'));
  assert.ok(view.includes('원본 지시'));
  assert.ok(view.includes('data-work="0"'));
});

test('missing child log cannot erase a confirmed tool result or block a different work',()=>{
 const b=board();load(b,fixture({task:{result:'완료',delegations:[{id:'a',status:'completed',result:'확인한 결과',end_ts:110}]},session:{agents:{items:[{id:'a',status:'unknown',result:'',parent_task_ts:100},{id:'old',status:'waiting',parent_task_ts:10}],running:[{parent_task_ts:10}]}}}));
 assert.equal(b.run('delegationRows(W).find(r=>r.id==="a").result'),'확인한 결과');
 assert.equal(b.run('requestStage(W).complete'),true);
});
test('follow-up delegation pairs each child turn with its own instruction and raw result',()=>{
 const b=board();load(b,fixture({task:{delegations:[{id:'a',child_id:'child',prompt:'첫 검수',ts:10},{id:'b',child_id:'child',prompt:'다시 검수',ts:30}]}}));
 b.run(`const child={id:'child',agent:'codex',parent_id:W.s.id,parent_work:W.w.key,state:'쉬는 중',last_ts:40,tasks:[
 {ts:11,end_ts:20,last_ts:20,instruction:'첫 검수',result:'첫 결과',raw_result:'<div>첫 결과</div>',outcome:'답변'},
 {ts:31,end_ts:40,last_ts:40,instruction:'다시 검수',result:'두 번째 결과',outcome:'답변'}],works:[{key:'검수',tasks:[0,1]}]};data.sessions.push(child);`);
 assert.equal(b.run('delegationRows(W).find(r=>r.id==="a").result'),'<div>첫 결과</div>');
 assert.equal(b.run('delegationRows(W).find(r=>r.id==="b").result'),'두 번째 결과');
 assert.ok(b.run('delegatedMarkup(W)').includes('&lt;div&gt;첫 결과&lt;/div&gt;'));
});

test('an identical prompt from an older child turn never completes a newer delegation',()=>{
 const b=board();load(b,fixture({task:{delegations:[{id:'new',child_id:'child',prompt:'검수',ts:30}]}}));
 b.run(`data.sessions.push({id:'child',agent:'codex',parent_id:W.s.id,parent_work:W.w.key,state:'쉬는 중',last_ts:40,tasks:[
 {ts:11,end_ts:20,last_ts:20,instruction:'검수',result:'옛 결과',outcome:'답변'},
 {ts:31,end_ts:40,last_ts:40,instruction:'검수',result:'새 결과',outcome:'답변'}],works:[]});`);
 assert.equal(b.run('delegationRows(W).find(r=>r.id==="new").result'),'새 결과');
 assert.ok(b.run('delegationRows(W).find(r=>r.id==="new").end_ts>=30'));
});

test('a completed code-only child result remains complete and is safely escaped',()=>{
 const b=board();load(b,fixture());
 b.run(`data.sessions.push({id:'child',agent:'codex',parent_id:W.s.id,parent_work:W.w.key,state:'쉬는 중',last_ts:120,
 tasks:[{ts:101,end_ts:120,last_ts:120,instruction:'코드 출력',result:'',raw_result:'<div>value</div>',outcome:'답변'}],works:[]});`);
 assert.equal(b.run('delegationRows(W)[0].status'),'completed');
 assert.ok(b.run('delegatedMarkup(W)').includes('&lt;div&gt;value&lt;/div&gt;'));
});

test('identical queued delegation requests keep FIFO result order',()=>{
 const b=board();load(b,fixture({task:{delegations:[{id:'first',child_id:'child',prompt:'검수',ts:20},{id:'second',child_id:'child',prompt:'검수',ts:21}]}}));
 b.run(`data.sessions.push({id:'child',agent:'codex',parent_id:W.s.id,parent_work:W.w.key,state:'쉬는 중',last_ts:50,tasks:[
 {ts:30,end_ts:35,last_ts:35,instruction:'검수',result:'첫 결과',outcome:'답변'},
 {ts:40,end_ts:50,last_ts:50,instruction:'검수',result:'둘째 결과',outcome:'답변'}],works:[]});`);
 assert.equal(b.run('delegationRows(W).find(r=>r.id==="first").result'),'첫 결과');
 assert.equal(b.run('delegationRows(W).find(r=>r.id==="second").result'),'둘째 결과');
});

function themeBoard(extra={}) {
  const root={dataset:{}},media={matches:true,listener:null,addEventListener(_,fn){this.listener=fn;}},head={children:[],appendChild(x){this.children.push(x);}};
  const el={addEventListener() {},setAttribute() {},classList:{toggle() {}},querySelectorAll() { return []; }};
  const document={head,documentElement:root,getElementById:()=>el,querySelectorAll:()=>[],addEventListener() {},
    createElement:()=>({dataset:{},textContent:'',remove(){head.children=head.children.filter(x=>x!==this);}}),
    querySelector:s=>s==='style[data-palette]'?head.children.find(x=>'palette' in x.dataset)||null:el};
  const b=board({document,window:{addEventListener() {},getSelection:()=>'',matchMedia:()=>media},...extra});
  b.run('data={sessions:[]};ui.settingsTab="appearance";render=()=>{}');
  return {...b,root,media,head,style:()=>head.children.find(x=>'palette' in x.dataset)};
}

test('appearance offers named themes like a gallery, applies sidebar and accent colours, and follows the system brightness',()=>{
  const b=themeBoard();
  let view=b.run('settingsView()');
  assert.ok(view.includes('data-theme-mode="dark" aria-pressed="true"'));
  assert.ok(view.includes('data-palette-choice="huginn" aria-pressed="true"'));
  for(const label of ['음악에서 고른 색','새로운 조합','다시 만든 클래식','단일 색상','시력 보조 색상','청황색약 대비','적록색약 대비','data-palette-share'])assert.ok(view.includes(label),label);
  assert.equal((view.match(/data-palette-choice=/g)||[]).length,b.run('PALETTES.length'));
  b.run('localStorage.setItem("wb.palette","eggplant");applyTheme()');
  assert.equal(b.root.dataset.sideScheme,'dark');
  const css=b.style().textContent;
  for(const part of ['--d-side:#3f0e40','--l-side:#3f0e40','--d-primary:#cc67cf','--l-primary:#5a1d5c','--l-on-primary:#fbf8f1'])assert.ok(css.includes(part),part);
  for(const key of ['bg','panel','panel2','hover','line','line2','ink','sub','dim'])for(const prefix of ['--d-','--l-'])assert.match(css,new RegExp(prefix+key+':#[0-9a-f]{6}'),prefix+key);
  const [hue]=b.run('toHsl(paletteTokens(PALETTES.find(p=>p.id==="eggplant")).dark.bg)');assert.ok(Math.abs(hue-b.run('toHsl("#3f0e40")[0]'))<.02);
  assert.equal(b.run('localStorage.getItem("wb.paletteCss")'),css);assert.equal(b.run('localStorage.getItem("wb.sideScheme")'),'dark');
  b.run('localStorage.setItem("wb.palette","deutan");applyTheme()');
  for(const part of ['--d-run:#f0e442','--l-done:#0b5f98'])assert.ok(b.style().textContent.includes(part),part);
  b.run('localStorage.setItem("wb.palette","<x>");applyTheme()');
  assert.equal(b.root.dataset.sideScheme,undefined);assert.ok(!b.style().textContent.includes('--d-side'));
  b.run('localStorage.setItem("wb.theme","system");applyTheme()');assert.equal(b.root.dataset.theme,'light');
  b.media.matches=false;b.media.listener();assert.equal(b.root.dataset.theme,undefined);
  b.run('localStorage.setItem("wb.theme","light");applyTheme()');b.media.matches=true;b.media.listener();assert.equal(b.root.dataset.theme,'light');
  b.run('localStorage.setItem("wb.theme","bogus");applyTheme()');assert.equal(b.root.dataset.theme,undefined);
});

test('every built-in theme stays readable and the validation base matches the stylesheet',()=>{
  const b=themeBoard();
  for(const id of b.run('PALETTES.map(p=>p.id)'))assert.doesNotThrow(()=>b.run(`readPalette(paletteJson(PALETTES.find(p=>p.id==="${id}")))`),id);
  const worst=b.run(`PALETTES.filter(p=>p.sidebar).map(p=>{const t=paletteTokens(p);return [p.id,Math.min(...['dark','light'].flatMap(m=>['ink','sub','dim','primary','run','wait','stall','done','info','off'].flatMap(k=>surfacesOf(t[m]).map(x=>contrast(t[m][k],x))))),contrast(t[sidebarScheme(p.sidebar)].dim,p.sidebar)];})`);
  for(const [id,text,side] of worst){assert.ok(text>=4.5,id+' text '+text);assert.ok(side>=4.5,id+' sidebar '+side);}
  const tinted=b.run(`PALETTES.filter(p=>p.sidebar && toHsl(p.sidebar)[1]>.3).flatMap(p=>{const t=paletteTokens(p),hue=toHsl(p.sidebar)[0];return ['dark','light'].flatMap(m=>['bg','panel2'].map(k=>{const [h,s]=toHsl(t[m][k]);return [p.id+' '+m+' '+k,Math.min(Math.abs(h-hue),1-Math.abs(h-hue)),s,Math.min(toHsl(p.sidebar)[1]*.7,.5)];}));})`);
  for(const [name,drift,sat,floor] of tinted){assert.ok(drift<.03,name+' hue');assert.ok(sat>=floor,name+' saturation '+sat);}
  const roles=b.run(`PALETTES.filter(p=>p.sidebar).map(p=>{const t=paletteTokens(p).dark;return [p.id,contrast(t.selected,t['on-selected']),contrast(t.presence,p.sidebar),contrast(t.notify,p.sidebar)];})`);
  assert.equal(roles.length,b.run('PALETTES.filter(p=>p.sidebar).length'));
  for(const [id,selected,presence,notify] of roles){assert.ok(selected>=4.5,id+' selected '+selected);assert.ok(presence>=3,id+' presence');assert.ok(notify>=3,id+' notify');}
  const tritan=b.run('paletteTokens(PALETTES.find(p=>p.id==="tritan")).light');assert.deepEqual([tritan.selected,tritan['on-selected']],['#ffffff','#000000']);
  const css=fs.readFileSync(path.join(__dirname,'..','desktop/theme.css'),'utf8');
  for(const [mode,prefix] of [['dark','--d-'],['light','--l-']])for(const key of ['bg','panel','dim','ink'])
    assert.equal(css.match(new RegExp(prefix+key+':\\s*(#[0-9a-f]{6})'))[1],b.run(`PALETTE_BASE.${mode}.${key}`),mode+key);
});

test('a selected sidebar row repaints its inner buttons and avatar from the selected colour',()=>{
  const rule=html.match(/\.tab\.sel \{([^}]*)\}/)[1];
  for(const token of ['--panel','--panel2','--hover','--line','--line2'])assert.match(rule,new RegExp(token+': color-mix\\(in srgb, var\\(--selected\\) \\d+%, var\\(--on-selected\\)\\)'),token);
});

test('custom theme JSON is validated with readable messages before it is applied',()=>{
  const b=themeBoard();
  assert.deepEqual(JSON.parse(JSON.stringify(b.run(`readPalette('{"name":" 내 테마 ","sidebar":"#123","accent":{"dark":"#8FB4D9","light":"#2f5a86"}}')`))),{name:'내 테마',sidebar:'#112233',accent:{dark:'#8fb4d9',light:'#2f5a86'}});
  for(const [input,message] of [['{"name":',"JSON 형식"],['[]','JSON 객체'],['{"font":"x"}','font'],['{"sidebar":"red"}','#RRGGBB'],
    ['{"sidebar":"#808080"}','사이드바 글자'],['{"accent":{"dark":"#333333","light":"#765714"}}','다크 화면'],['{"accent":{"dark":"#d3b066"}}','accent.light'],['{"accent":"#8fb4d9"}','라이트 화면'],
    ['{"status":{"light":{"run":"#f0e442"}}}','진행 상태색'],['{"sidebar":"#3f0e40","presence":"#3f0e41"}','접속 표시'],['{"notification":"zz"}','notification'],['{"name":"'+'가'.repeat(41)+'"}','name'],['{"sidebar":"#123;}body{x"}','#RRGGBB']])
    assert.throws(()=>b.run(`readPalette(${JSON.stringify(input)})`),e=>e.message.includes(message),input);
});

test('custom themes apply, survive as a card, and can be shared or received as JSON',async()=>{
  const copied=[];let fail=false;
  const b=themeBoard({navigator:{clipboard:{writeText:async text=>{if(fail)throw new Error('denied');copied.push(text);}}}});
  b.run(`applyCustomPalette('{"name":"우리 팀","sidebar":"#0f3350","accent":{"dark":"#8fc0ec","light":"#23598a"}}')`);
  assert.equal(b.run('paletteChoice()'),'custom');assert.ok(b.style().textContent.includes('--d-side:#0f3350'));
  assert.ok(b.run('ui.appearanceStatus.text').includes('우리 팀'));
  assert.ok(b.run('settingsView()').includes('data-palette-choice="custom" aria-pressed="true"'));
  await b.run('sharePalette()');
  assert.deepEqual(JSON.parse(copied[0]),{name:'우리 팀',sidebar:'#0f3350',accent:{dark:'#8fc0ec',light:'#23598a'}});
  b.run(`applyCustomPalette('{"accent":"#111111"}')`);
  assert.equal(b.run('ui.appearanceStatus.error'),true);assert.equal(b.run('paletteChoice()'),'custom');assert.ok(b.style().textContent.includes('#0f3350'));
  fail=true;await b.run('sharePalette()');
  assert.equal(b.run('ui.appearanceTab'),'custom');assert.ok(b.run('ui.customDraft').includes('우리 팀'));
  const view=b.run('settingsView()');
  for(const part of ['data-custom-json','data-custom-file','data-custom-color="sidebar" value="#0f3350"','data-custom-color="selected"','data-custom-color="presence"','data-custom-color="notification"','data-custom-apply'])assert.ok(view.includes(part),part);
  b.run('localStorage.setItem("wb.customTheme","{broken");applyTheme()');assert.equal(b.run('paletteChoice()'),'huginn');
});

test('theme boot script applies stored choices before the stylesheet without trusting unknown values',()=>{
  for(const page of ['index.html','stories.html']) {
    const source=fs.readFileSync(path.join(__dirname,'..',page),'utf8').match(/<script data-theme-boot>([\s\S]*?)<\/script>/)[1];
    const good='html:root{--d-side:#3b1340;--l-primary:#6d2c78}';
    for(const [theme,light,css,scheme,expectedTheme,styled,expectedScheme] of [
      ['system',true,good,'dark','light',true,'dark'],['system',false,null,null,undefined,false,undefined],['dark',true,good,'light',undefined,true,'light'],
      ['light',false,'html:root{--d-side:#000}</style><x>',"\"><x",'light',false,undefined],[null,true,'html:root{--x:url(//evil)}','dark',undefined,false,'dark'],
      [null,true,'body{color:red}',null,undefined,false,undefined]]) {
      const root={dataset:{}},head=[],values={'wb.theme':theme,'wb.paletteCss':css,'wb.sideScheme':scheme};
      vm.runInNewContext(source,{document:{documentElement:root,head:{appendChild:x=>head.push(x)},createElement:()=>({dataset:{}})},localStorage:{getItem:k=>values[k]??null},matchMedia:()=>({matches:light})});
      assert.equal(root.dataset.theme,expectedTheme,page+theme);assert.equal(head.length===1,styled,page+css);
      if(styled)assert.equal(head[0].textContent,css);assert.equal(root.dataset.sideScheme,expectedScheme,page+scheme);
    }
  }
});

test('feedback dialog validates input, posts a feedback action and links only to GitHub',async()=>{
  const requests=[];let reply=null;
  const b=board({setTimeout,clearTimeout,AbortController,fetch:async(url,options)=>{if(options?.method==='POST'){requests.push(JSON.parse(options.body));return {ok:true,json:async()=>reply};}return {ok:true,json:async()=>({token:'t'})};}});
  b.run('data={sessions:[]};ui.actionData={"":{token:"t",at:Date.now()}}');
  const markup=b.run('feedbackMarkup()');
  assert.ok(markup.includes('name="kind" value="bug" checked'));assert.ok(markup.includes('name="title"'));assert.ok(markup.includes('name="body"'));
  b.run(`globalThis.makeForm=(title,body)=>{const status={textContent:'',innerHTML:''},button={disabled:false},fields={title:{value:title,disabled:false,focus(){}},body:{value:body,disabled:false,focus(){}}};
    return {status,button,fields,elements:fields,querySelector:s=>s==='[data-feedback-status]'?status:s==='[data-feedback-submit]'?button:s==='[name=kind]:checked'?{value:'idea'}:null};}`);
  b.run('globalThis.f=makeForm("  ","내용")');await b.run('sendFeedback(f)');
  assert.equal(requests.length,0);assert.equal(b.run('f.status.textContent'),'제목과 내용을 입력해 주세요.');
  reply={ok:true,status:'created',url:'https://github.com/owner/huginn/issues/7',message:'의견을 등록했습니다.'};b.run('globalThis.f=makeForm(" 제안 ","내용")');
  await b.run('sendFeedback(f)');
  assert.deepEqual(requests[0],{action:'feedback',kind:'idea',title:'제안',body:'내용'});
  assert.ok(b.run('f.status.innerHTML').includes('href="https://github.com/owner/huginn/issues/7"'));
  assert.equal(b.run('f.fields.title.disabled'),true);
  reply={ok:true,status:'browser',url:'javascript:alert(1)',message:'작성 페이지로 보냅니다.'};b.run('globalThis.f=makeForm("제안","내용")');
  await b.run('sendFeedback(f)');
  assert.ok(!b.run('f.status.innerHTML').includes('href'));assert.equal(b.run('f.button.disabled'),false);
});

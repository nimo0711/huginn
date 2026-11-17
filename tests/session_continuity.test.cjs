const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(process.env.BOARD_HTML || path.join(__dirname,'..','index.html'),'utf8');
function board(){
  const storage=new Map(),el={addEventListener(){},setAttribute(){},classList:{toggle(){}},querySelectorAll(){return[];}};
  const ctx=vm.createContext({URLSearchParams,Date,console,location:{search:''},history:{replaceState(){}},
    localStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v)},
    document:{getElementById:()=>el,querySelector:()=>el,addEventListener(){}},window:{addEventListener(){},getSelection:()=>''},setInterval(){},fetch:()=>new Promise(()=>{})});
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],ctx);
  return {set:v=>ctx.input=v,run:s=>vm.runInContext(s,ctx)};
}
function task(extra={}){return{ts:1,last_ts:2,end_ts:2,start_ts:0,prompt:'업무',outcome:'답변',notes:[],checks:[],steps:[],files:[],n_checks:0,n_steps:0,result:'결과',...extra};}
test('whole-work progress includes archived counts and unfinished evidence',()=>{
  const b=board();b.set([task({history:{total:101,done:100},n_steps:3}),task({ts:200,end_ts:0,outcome:'계획',result:''})]);
  b.run(`const s={id:'s',agent:'codex',state:'작업 중',last_ts:Date.now()/1000,tasks:input};const w={key:'A',tasks:[0,1],story:'',info:{}};s.works=[w];data={sessions:[s]};const W=workOf(s,w);`);
  assert.equal(b.run('unitProgress(W).total'),102);
  assert.equal(b.run('unitProgress(W).done'),100);
  assert.equal(b.run('unitProgress(W).stage.index'),2);
  assert.equal(b.run('unitProgress(W).stage.complete'),false);
  assert.equal(b.run('unitProgress(W).rows.length'),2);
  assert.match(b.run('historyContent(W)'),/이전 101개 요청/);
});
test('an archived incomplete request never completes a delegated work',()=>{
  const b=board();b.set([task({history:{total:30,done:29}}),task({ts:200})]);
  b.run(`const s={id:'s',agent:'codex',state:'쉬는 중',last_ts:Date.now()/1000,tasks:input};s.works=[{key:'A',tasks:[0,1],story:'',info:{}}];data={sessions:[s]};`);
  assert.equal(b.run('delegatedFinished(s)'),false);
});
test('selected work survives reordering after a new request in that work',()=>{
  const b=board();b.set([task(),task({ts:3})]);
  b.run(`const s={id:'s',agent:'codex',cwd:'demo',state:'쉬는 중',last_ts:Date.now()/1000,tasks:input};const a={key:'A',title:'첫 업무',tasks:[0],story:'',info:{}},bb={key:'B',title:'둘째 업무',tasks:[1],story:'',info:{}};s.works=[a,bb];data={sessions:[s]};selectSession('s',0);s.works=[bb,a];`);
  assert.match(b.run('mainView(s)'),/<h1>첫 업무<\/h1>/);
  assert.doesNotMatch(b.run('mainView(s)'),/<h1>둘째 업무<\/h1>/);
  b.run('s.works=[a,bb]');
  assert.match(b.run('mainView(s)'),/<h1>첫 업무<\/h1>/);
});

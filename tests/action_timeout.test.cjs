const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(process.env.BOARD_HTML||path.join(__dirname,'..','index.html'),'utf8');
for(const mode of ['headers','body'])test('catalog '+mode+' timeout releases actions and offers recovery',async()=>{
 const el={addEventListener(){},setAttribute(){},classList:{toggle(){}},querySelectorAll(){return []}};
 const ctx=vm.createContext({URLSearchParams,Date,console,AbortController,clearTimeout,setTimeout:(fn,ms)=>setTimeout(fn,Math.min(ms,5)),
  localStorage:{getItem(){return null},setItem(){}},location:{search:''},history:{replaceState(){}},document:{getElementById:()=>el,querySelector:()=>el,addEventListener(){}},window:{addEventListener(){},getSelection:()=>''},setInterval(){},fetch:()=>new Promise(()=>{})});
 vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],ctx);
 vm.runInContext('render=()=>{};data={sessions:[]};ui.sid=SETTINGS;',ctx);
 ctx.fetch=(url,options)=>{
  const pending=()=>new Promise((resolve,reject)=>options?.signal?.addEventListener('abort',()=>{const e=new Error('aborted');e.name='AbortError';reject(e)}));
  return mode==='body'?Promise.resolve({ok:true,json:pending}):pending();
 };
 const timeout=new Promise((resolve,reject)=>setTimeout(()=>reject(new Error('action stayed locked after catalog stalled')),200));
 await Promise.race([vm.runInContext("boardAction('configure',{settings:{auto_servers:true}},'')",ctx),timeout]);
 assert.equal(vm.runInContext('ui.actionBusy',ctx),false);
 assert.equal(vm.runInContext("Boolean(ui.actionLoading[''])",ctx),false);
 assert.ok(vm.runInContext('connectionsView()',ctx).includes('다시 불러오기'));
});

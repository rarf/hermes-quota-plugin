// Real Chromium hover against the plugin + desktop's actual SDK Tip/Radix.
// No installs: node tests/widget_hover.cjs DESKTOP_DIR SCRATCH_OUTPUT_DIR
// Caller must scope HOME/HERMES_HOME/TMPDIR to scratch. Never load live auth.
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const assert = require('node:assert/strict');
const desktop = path.resolve(process.argv[2]);
const out = path.resolve(process.argv[3]);
const req = createRequire(path.join(desktop, 'package.json'));
const {build} = req('esbuild');
const {chromium} = req('playwright');
const plugin = path.resolve(__dirname, '../desktop/plugin.js');
const source = fs.readFileSync(plugin, 'utf8');
const fixture = {age_s: 0, providers: {'openai-codex': {accounts: [
  {label: 'Synthetic Amber', plan: 'pro', windows: [{label: 'Session', used_percent: 21, reset_at: '2099-01-01T00:00:00Z'}, {label: 'Weekly', used_percent: 10, reset_at: '2099-01-02T00:00:00Z'}]},
  {label: 'Synthetic Blue', plan: 'plus', windows: [{label: 'Session', used_percent: 42, reset_at: '2099-01-03T00:00:00Z'}, {label: 'Weekly', used_percent: 30, reset_at: '2099-01-04T00:00:00Z'}]},
]}}};
const sdk = `
export {Tip} from ${JSON.stringify(path.join(desktop, 'src/components/ui/tooltip.tsx'))};
export const atom = value => ({get:()=>value,set:v=>{value=v}});
export const useValue = a=>a.get();
export const useQuery = ()=>{const data=${JSON.stringify(fixture)}; if(new URLSearchParams(location.search).has("single")) for(const a of data.providers["openai-codex"].accounts) a.windows=a.windows.slice(0,1); return {data};};
export const useQueryClient = ()=>({invalidateQueries:()=>{}});
export const useMutation = ()=>({});
export const usePluginI18n = ()=>key=>key;
export const cn = (...args)=>args.filter(Boolean).join(' ');
export const fmtDayTime = new Intl.DateTimeFormat('en-US');
export const host = {state:{focusedSessionOwner:atom(null)},navigate:p=>{window.navigated=p}};
export const Input='input',Switch='input',SegmentedControl='span',StatusDot='span';
export const icons={}, PANES_AREA='',ROUTES_AREA='',SIDEBAR_NAV_AREA='',STATUSBAR_AREAS={right:''};`;
(async()=>{
  fs.mkdirSync(out,{recursive:true});
  await build({stdin:{contents: `import React from 'react'; import {createRoot} from 'react-dom/client'; import {StatusBar, setMode} from 'quota-under-test'; setMode(new URLSearchParams(location.search).get('mode')); createRoot(document.getElementById('root')).render(React.createElement(StatusBar));`, resolveDir:desktop,loader:'jsx'}, bundle:true,format:'iife',outfile:path.join(out,'hover.js'),jsx:'automatic',nodePaths:[path.join(desktop,'node_modules'),path.resolve(desktop,'../../node_modules')],plugins:[{name:'offline-sdk-boundaries',setup(b){
    b.onResolve({filter:/^quota-under-test$/},()=>({path:plugin,namespace:'quota'}));
    b.onLoad({filter:/.*/,namespace:'quota'},()=>({contents:source+'\nexport {StatusBar}; export const setMode = v => statusbarModeAtom.set(v || "all");',resolveDir:desktop,loader:'js'}));
    b.onResolve({filter:/^@hermes\/plugin-sdk$/},()=>({path:'sdk',namespace:'fixture'}));
    b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:sdk,resolveDir:desktop,loader:'js'}));
    // Only unrelated i18n/keybind hooks are stubbed; tooltip, input modality,
    // placement, React, Radix, portal and floating geometry run unchanged.
    b.onResolve({filter:/^@\/(i18n|lib\/keybinds\/use-keybind-hint)$/},()=>({path:'hooks',namespace:'hooks'}));
    b.onLoad({filter:/.*/,namespace:'hooks'},()=>({contents:'export const useI18n=()=>({}); export const useKeybindHint=()=>null;'}));
    b.onResolve({filter:/^@\//},a=>({path:path.join(desktop,'src',a.path.slice(2)+'.ts')}));
  }}]});
  const html = '<!doctype html><html><head><link rel="stylesheet" href="hover.css"><style>body{margin:0;background:#202124;color:#eee;font:14px sans-serif}#root{position:fixed;bottom:0;left:0;right:0;height:28px;display:flex;align-items:center;overflow:hidden}button{color:inherit;background:#303134;border:0;height:28px}.tooltip-bubble{background:#eee;color:#171717;padding:8px;font:12px/1.4 sans-serif;z-index:10000}</style></head><body><div id="root"></div><script src="hover.js"></script></body></html>';
  fs.writeFileSync(path.join(out,'index.html'),html);
  const browser = await chromium.launch({headless:true,channel:'chrome'});
  const results=[];
  try {
    for(const single of [false,true]) for(const mode of ['all','worst']) {
      const page=await browser.newPage({viewport:{width:1000,height:720}});
      const errors=[];page.on('pageerror',e=>errors.push(String(e)));
      // Serve only synthetic local assets in-memory; deny every other request.
      await page.route('**/*',route=>{
        const u=new URL(route.request().url());
        const name=u.pathname==='/'?'index.html':u.pathname.slice(1);
        if(u.origin==='http://quota.test' && ['index.html','hover.js','hover.css'].includes(name)) return route.fulfill({path:path.join(out,name),contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
        return route.abort();
      });
      await page.goto('http://quota.test/?mode='+mode+(single?'&single=1':''));
      const button=page.getByRole('button');
      await button.waitFor();
      assert.equal(await button.count(),1);
      await button.hover();
      const bubble=page.locator('[data-slot="tooltip-content"]');
      let visible=true;
      try {await bubble.waitFor({state:'visible',timeout:2500});} catch {visible=false;}
      const evidence={mode,single,visible,button:await button.innerText(),nativeTitle:await button.getAttribute('title'),errors};
      if(visible){
        const label=bubble.locator('[data-slot="tooltip-label"]').first();
        evidence.label=await label.innerText();
        evidence.side=await bubble.getAttribute('data-side');
        evidence.bounds=await bubble.boundingBox();
        evidence.triggerBounds=await button.boundingBox();
        evidence.style=await label.locator(':scope > div').evaluate(e=>({whiteSpace:getComputedStyle(e).whiteSpace,clientHeight:e.clientHeight,scrollHeight:e.scrollHeight}));
      }
      await page.screenshot({path:path.join(out,mode+(single?'-single':'')+'.png')});
      results.push(evidence);
      if(visible){
        assert.equal(evidence.nativeTitle,null);
        assert.match(await button.getAttribute('aria-label'),/OpenAI Codex/);
        assert.match(evidence.button,/Synthetic Amber/); assert.doesNotMatch(evidence.button,/Synthetic Blue/);
        assert.ok(evidence.label.indexOf('Synthetic Amber')<evidence.label.indexOf('Synthetic Blue'));
        for(const part of ['● Synthetic Amber · 79% left','○ Synthetic Blue · 58% left', ...(single?['Session · resets in']:['Session · 79% left · resets in','Weekly · 90% left · resets in','Weekly · 70% left · resets in'])]) assert.ok(evidence.label.includes(part),part);
        for(const part of ['OpenAI Codex · remaining','Shown in status bar','Click for details']) assert.ok(!evidence.label.includes(part),part);
        for(const old of ['priority-eligible','inference routing','selected for display','Generic Session','Plan:','2099-','Click to open Quota pane']) assert.ok(!evidence.label.includes(old),old);
        assert.equal(evidence.label.split('\n').length, 6-(single?2:0));
        if(single) for(const value of ['79% left','58% left']) assert.equal(evidence.label.split(value).length-1,1, 'No duplicated single-window remaining');
        assert.ok(evidence.bounds.width < 400, 'Compact tooltip must not become a wide banner');
        assert.ok(evidence.bounds.height < 200, 'Generic two-window accounts fit a short summary');
        assert.equal((evidence.label.match(/resets/g)||[]).length,single?2:4);
        assert.equal(evidence.style.whiteSpace,'pre-wrap');
        assert.equal(evidence.style.scrollHeight,evidence.style.clientHeight,'Both accounts fully visible without clipping');
        assert.equal(evidence.side,'top');
        assert.ok(evidence.bounds.y>=0);
        assert.ok(evidence.bounds.y+evidence.bounds.height<=evidence.triggerBounds.y);
        await page.mouse.move(900,100); await bubble.waitFor({state:'hidden'});
        await button.click();assert.equal(await page.evaluate(()=>window.navigated),'/quota');
        assert.deepEqual(errors,[]);
      }
      await page.close();
    }
    fs.writeFileSync(path.join(out,'hover-results.json'),JSON.stringify(results,null,2));
    console.log(JSON.stringify(results,null,2));
    assert.ok(results.every(r=>r.visible),'Both status modes must show a real tooltip after mouse hover');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

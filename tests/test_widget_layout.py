"""Real Chromium geometry with the installed host CSS and real widget render.

Opt in: QUOTA_WIDGET_CSS='/path/to/index.css:/path/to/sdk.css' python tests/test_widget_layout.py
Uses the existing chrome-debug session via browser-harness-js. Creates and closes
only its own blank test tab. No credentials or live provider calls.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_widget_balances import BALANCE, render


@unittest.skipUnless(os.environ.get('QUOTA_WIDGET_CSS'), 'set QUOTA_WIDGET_CSS for Chromium layout tests')
class WidgetLayoutTests(unittest.TestCase):
    def test_cards_scroll_without_footer_overlap(self):
        css = '\n'.join(Path(p).read_text() for p in os.environ['QUOTA_WIDGET_CSS'].split(os.pathsep))
        source = os.environ.get('QUOTA_WIDGET_SOURCE')
        options = {'source': source} if source else {}
        data = {'fetched_at': '2025-01-15T12:34:56Z', 'age_s': 7, 'providers': {
            'deepseek': BALANCE,
            'anthropic': {'windows': [{'label': 'Weekly', 'used_percent': 80}],
                           'details': ['Long provider detail note ' * 10, 'x' * 300, 'Last note: currencies and account limits are not combined.']},
        }}
        payload = {'css': css, 'trees': [render(mode=m, data=data, **options) for m in ('clean', 'dense')]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.json'
            path.write_text(json.dumps(payload))
            script = r'''
const payload = JSON.parse(await Bun.file(FIXTURE_PATH).text());
const {targetId} = await session.Target.createTarget({url:'about:blank', background:true});
try {
 await session.use(targetId);
 const result = await session.Runtime.evaluate({returnByValue:true, expression: '(' + ((payload) => {
  document.head.innerHTML = '';
  const style = document.createElement('style'); style.textContent = payload.css;
  document.head.appendChild(style);
  const make = (tree) => {
   if (tree == null || typeof tree === 'boolean') return document.createTextNode('');
   if (Array.isArray(tree)) { const f=document.createDocumentFragment(); tree.forEach(t=>f.appendChild(make(t))); return f; }
   if (typeof tree !== 'object') return document.createTextNode(String(tree));
   const el = document.createElement(tree.type || 'span'); const p=tree.props;
   for (const [k,v] of Object.entries(p)) {
    if (k==='className') el.className=v;
    else if (k==='style') Object.assign(el.style, Object.fromEntries(Object.entries(v).map(([k,v])=>[k, typeof v==='number' && v!==0 && !['flexShrink','lineHeight'].includes(k) ? v+'px' : v])));
    else if (k.startsWith('data-')) el.setAttribute(k,v);
   }
   // Only the widget's constant SVG paths use HTML; fixture text uses text nodes.
   if (p.dangerouslySetInnerHTML) el.innerHTML=p.dangerouslySetInnerHTML.__html;
   else el.appendChild(make(p.children));
   return el;
  };
  const results=[];
  for (const [mode,tree] of payload.trees.entries()) for (const width of [180,240,300,480]) for (const height of [100,160,240,600]) {
   document.body.replaceChildren();
   const box=document.createElement('div'); Object.assign(box.style,{width:width+'px',height:height+'px'});
   document.body.appendChild(box); box.appendChild(make(tree));
   const root=box.firstElementChild;
   const scroll=root.querySelector('[data-quota-scroll]') || root.children[1];
   const footer=root.querySelector('[data-quota-checked]') || root.lastElementChild;
   scroll.scrollTop=scroll.scrollHeight;
   const cards=Array.from(scroll.querySelectorAll('div')).filter(el=>el.className.includes('rounded-lg') && el.className.includes('px-3'));
   const sb=scroll.getBoundingClientRect(), fb=footer.getBoundingClientRect();
   const errors=[];
   if (!['auto','scroll'].includes(getComputedStyle(scroll).overflowY)) errors.push('body has no scroll boundary');
   if (sb.bottom>fb.top+0.5) errors.push('scroll region overlaps footer');
   if (root.scrollWidth>root.clientWidth+1) errors.push('horizontal overflow');
   for (const card of cards) {
    const cb=card.getBoundingClientRect();
    for (const child of card.children) {
     const b=child.getBoundingClientRect();
     if (b.bottom>cb.bottom+0.5) errors.push('card shrank under its text');
     if (b.right>cb.right+0.5) errors.push('card text overflows horizontally');
    }
   }
   if (cards.length!==2) errors.push('expected two real cards');
   results.push({mode,width,height,errors});
  }
  return results;
 }).toString() + ')(' + JSON.stringify(payload) + ')'});
 if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
 if (SCREENSHOT_PATH) {
  const shot = await session.Page.captureScreenshot({format:'png',clip:{x:0,y:0,width:480,height:600,scale:1}});
  await Bun.write(SCREENSHOT_PATH, Buffer.from(shot.data,'base64'));
 }
 return result.result.value;
} finally { await session.Target.closeTarget({targetId}); }
'''.replace('FIXTURE_PATH', json.dumps(str(path))).replace('SCREENSHOT_PATH', json.dumps(os.environ.get('QUOTA_WIDGET_SCREENSHOT')))
            result = subprocess.run(['browser-harness-js', script], text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            cases = json.loads(result.stdout)
            self.assertEqual(len(cases), 32)
            failures = [c for c in cases if c['errors']]
            self.assertEqual(failures, [], json.dumps(failures))
            print('32 real Chromium layout cases passed (2 modes, 4 widths, 4 heights).')


if __name__ == '__main__':
    unittest.main()

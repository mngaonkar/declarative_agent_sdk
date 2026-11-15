/*
 * Tests for md.js. Run directly:  node agents_control/static/md.test.cjs
 * (pytest also runs this, via tests/test_agents_control.py, when node exists.)
 *
 * A DOM shim stands in for the browser so the renderer can be checked without
 * one; it serialises what was built, which is also how the XSS cases are
 * asserted — the output must contain escaped text, never live markup.
 */
'use strict';

// .cjs, and md.js is loaded by reading it rather than require()ing it: a
// parent package.json may declare "type": "module", which would make node
// treat md.js as an ES module and hand back an empty namespace. The browser
// loads it as a classic script, which is what this mirrors.
const fs = require('fs');
const path = require('path');
new Function(fs.readFileSync(path.join(__dirname, 'md.js'), 'utf8')).call(globalThis);
const md = globalThis.AgentMarkdown;

const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function node(tag) {
  return {
    tag, children: [], attrs: {}, _text: null, className: '',
    set textContent(v) { this._text = v; this.children = []; },
    get textContent() { return this._text; },
    appendChild(c) { this.children.push(c); return c; },
    setAttribute(k, v) { this.attrs[k] = v; },
    set href(v) { this.attrs.href = v; }, get href() { return this.attrs.href; },
    set target(v) { this.attrs.target = v; },
    set rel(v) { this.attrs.rel = v; },
  };
}
const doc = {
  createElement: (t) => node(t),
  createTextNode: (t) => ({ tag: '#text', text: t }),
  createDocumentFragment: () => node('#frag'),
};
function html(n) {
  if (n.tag === '#text') return esc(n.text);
  const inner = n._text != null ? esc(n._text) : n.children.map(html).join('');
  if (n.tag === '#frag') return inner;
  if (['hr', 'br'].includes(n.tag)) return `<${n.tag}>`;
  const cls = n.className ? ` class="${n.className}"` : '';
  const attrs = Object.entries(n.attrs).map(([k, v]) => ` ${k}="${esc(v)}"`).join('');
  return `<${n.tag}${cls}${attrs}>${inner}</${n.tag}>`;
}
const render = (text) => html(md.render(text, doc));

let failed = 0;
function check(label, actual, expected) {
  const ok = typeof expected === 'function' ? expected(actual) : actual === expected;
  if (!ok) {
    failed += 1;
    console.error(`FAIL ${label}\n  got:      ${actual}\n  expected: ${expected}`);
  } else {
    console.log(`ok   ${label}`);
  }
}
const has = (needle) => (actual) => actual.includes(needle);
const lacks = (needle) => (actual) => !actual.includes(needle);

// ── inline ────────────────────────────────────────────────────────────────
check('bold', render('a **b** c'), '<p>a <strong>b</strong> c</p>');
check('underscore bold', render('__b__'), '<p><strong>b</strong></p>');
check('italic', render('*i*'), '<p><em>i</em></p>');
check('strike', render('~~x~~'), '<p><del>x</del></p>');
check('inline code', render('use `x = 1`'), '<p>use <code>x = 1</code></p>');
check('code is not re-parsed', render('`**not bold**`'),
      '<p><code>**not bold**</code></p>');
check('bold around italic', render('**b with *i* in**'),
      '<p><strong>b with <em>i</em> in</strong></p>');
check('snake_case survives', render('send_to_agent and max_tokens'),
      '<p>send_to_agent and max_tokens</p>');
check('mention', render('ping @coder.here'),
      '<p>ping <span class="mention">@coder.here</span></p>');
check('mention keeps full stop', render('ask @coder.'),
      '<p>ask <span class="mention">@coder</span>.</p>');

// ── links ─────────────────────────────────────────────────────────────────
check('markdown link', render('[d](https://e.com)'), has('href="https://e.com"'));
check('link opens safely', render('[d](https://e.com)'), has('rel="noopener noreferrer"'));
check('bare url', render('see https://e.com/x now'), has('href="https://e.com/x"'));
check('mailto allowed', render('[m](mailto:a@b.c)'), has('href="mailto:a@b.c"'));
check('relative path allowed', render('[d](/local/page)'), has('href="/local/page"'));

// ── blocks ────────────────────────────────────────────────────────────────
check('heading', render('## T'), '<h4>T</h4>');
check('bullets', render('- a\n- b'), '<ul><li>a</li><li>b</li></ul>');
check('ordered', render('1. a\n2. b'), '<ol><li>a</li><li>b</li></ol>');
check('ordered start', render('3. a'), has('start="3"'));
check('fence with lang', render('```py\nx\n```'), '<pre><code class="lang-py">x</code></pre>');
check('unterminated fence', render('```\nx'), '<pre><code>x</code></pre>');
check('blockquote', render('> q'), '<blockquote>q</blockquote>');
check('rule', render('---'), '<hr>');
check('single newline is a break', render('a\nb'), '<p>a<br>b</p>');
check('blank line splits paragraphs', render('a\n\nb'), '<p>a</p><p>b</p>');
check('empty input', render(''), '');
check('null input', render(null), '');

// ── injection: text from a model is never markup ──────────────────────────
check('html is escaped', render('<img src=x onerror=alert(1)>'),
      has('&lt;img src=x onerror=alert(1)&gt;'));
check('no live img tag', render('<img src=x onerror=alert(1)>'), lacks('<img'));
check('script is escaped', render('<script>alert(1)</script>'), lacks('<script>'));
check('javascript: link is shown as text', render('[c](javascript:alert(1))'),
      '<p>[c](javascript:alert(1))</p>');
check('data: link is shown as text', render('[c](data:text/html,<script>)'),
      has('[c](data:text/html,'));
check('vbscript: link refused', render('[c](vbscript:x)'), lacks('href'));
check('no href survives a bad scheme', render('[c](JaVaScRiPt:x)'), lacks('href'));

// ── the real message from the board ───────────────────────────────────────
const REAL = `Fremont, CA right now (1:45 PM Sat, local):

- **17°C (63°F)**, feels like 16°C — overcast/partly cloudy, no rain.
- **Tomorrow:** high 16°C / low 11°C, but **rain coming** (~9.6 mm).`;
check('real agent message: no raw asterisks', render(REAL), lacks('**'));
check('real agent message: is a list', render(REAL), has('<ul><li>'));
check('real agent message: bold rendered', render(REAL), has('<strong>17°C (63°F)</strong>'));

// ── parseBlocks is pure and inspectable ───────────────────────────────────
check('parseBlocks types', JSON.stringify(md.parseBlocks('# h\n- a\n\np').map((b) => b.type)),
      '["h","ul","p"]');

console.log(failed ? `\n${failed} failure(s)` : '\nall md.js tests passed');
process.exit(failed ? 1 : 0);

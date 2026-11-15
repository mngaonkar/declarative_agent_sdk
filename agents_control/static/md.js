/*
 * Minimal Markdown → DOM for agent messages.
 *
 * Agents answer in Markdown whether or not you ask them to, so a board that
 * renders textContent shows people "**17°C**". This turns the common subset
 * into real elements.
 *
 * It builds nodes with createElement/textContent and never touches innerHTML,
 * so message text cannot inject markup — the text comes from a model, which is
 * to say from whatever that model just read.
 *
 * Deliberately small: no tables, no nested lists, no reference links, no HTML
 * passthrough. Blocks are headings, fenced code, bullet/ordered lists,
 * blockquotes, rules and paragraphs; inline is code, bold, italic, strike,
 * links, bare URLs and @mentions.
 */
(function (root) {
  'use strict';

  // Only these schemes become clickable. A model can emit "javascript:" as
  // easily as "https:", and this renderer is handed its output verbatim.
  const SAFE_SCHEME = /^(https?:|mailto:)/i;

  const FENCE = /^```(\w*)\s*$/;
  const HEADING = /^(#{1,6})\s+(.*)$/;
  const BULLET = /^\s{0,3}[-*+]\s+(.*)$/;
  const ORDERED = /^\s{0,3}(\d{1,9})[.)]\s+(.*)$/;
  const QUOTE = /^\s{0,3}>\s?(.*)$/;
  const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;

  /** Split text into block descriptors. Pure: no DOM, so it is testable. */
  function parseBlocks(text) {
    const lines = String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n');
    const blocks = [];
    let para = [];

    const flushPara = () => {
      if (para.length) {
        blocks.push({ type: 'p', text: para.join('\n') });
        para = [];
      }
    };

    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];

      const fence = FENCE.exec(line);
      if (fence) {
        flushPara();
        const body = [];
        i += 1;
        // An unterminated fence runs to the end rather than swallowing nothing.
        for (; i < lines.length && !FENCE.test(lines[i]); i += 1) body.push(lines[i]);
        blocks.push({ type: 'code', lang: fence[1] || '', text: body.join('\n') });
        continue;
      }

      if (!line.trim()) { flushPara(); continue; }

      if (RULE.test(line)) { flushPara(); blocks.push({ type: 'hr' }); continue; }

      const heading = HEADING.exec(line);
      if (heading) {
        flushPara();
        blocks.push({ type: 'h', level: heading[1].length, text: heading[2].trim() });
        continue;
      }

      const bullet = BULLET.exec(line);
      if (bullet) {
        flushPara();
        const items = [bullet[1]];
        while (i + 1 < lines.length && BULLET.test(lines[i + 1])) {
          items.push(BULLET.exec(lines[i + 1])[1]);
          i += 1;
        }
        blocks.push({ type: 'ul', items });
        continue;
      }

      const ordered = ORDERED.exec(line);
      if (ordered) {
        flushPara();
        const items = [ordered[2]];
        const start = Number(ordered[1]);
        while (i + 1 < lines.length && ORDERED.test(lines[i + 1])) {
          items.push(ORDERED.exec(lines[i + 1])[2]);
          i += 1;
        }
        blocks.push({ type: 'ol', items, start });
        continue;
      }

      const quote = QUOTE.exec(line);
      if (quote) {
        flushPara();
        const quoted = [quote[1]];
        while (i + 1 < lines.length && QUOTE.test(lines[i + 1])) {
          quoted.push(QUOTE.exec(lines[i + 1])[1]);
          i += 1;
        }
        blocks.push({ type: 'quote', text: quoted.join('\n') });
        continue;
      }

      para.push(line);
    }
    flushPara();
    return blocks;
  }

  /** Split one line into inline tokens. Pure, and the trickier half to get right. */
  function parseInline(text) {
    const source = String(text == null ? '' : text);
    const pattern = new RegExp([
      '`([^`]+)`',                                   // code
      // (?:[^*]|\*(?!\*)) so "**bold with *em* inside**" stays one bold run
      // instead of the inner * closing an emphasis the outer ** opened.
      '\\*\\*((?:[^*]|\\*(?!\\*))+)\\*\\*',             // **bold**
      '__((?:[^_]|_(?!_))+)__',                      // __bold__
      '~~([^~]+)~~',                                 // ~~strike~~
      '\\*([^*\\n]+)\\*',                            // *em*
      '(?<![A-Za-z0-9_])_([^_\\n]+)_(?![A-Za-z0-9_])', // _em_, not snake_case
      '\\[([^\\]]*)\\]\\(([^)]*)\\)',                   // [text](url)
      '(https?://[^\\s<>()]+)',                      // bare url
      '@([A-Za-z0-9][A-Za-z0-9_-]*(?:\\.[A-Za-z0-9][A-Za-z0-9_-]*)?)', // @mention
    ].join('|'), 'g');

    const tokens = [];
    let cursor = 0;
    let match;
    while ((match = pattern.exec(source))) {
      if (match.index > cursor) {
        tokens.push({ type: 'text', text: source.slice(cursor, match.index) });
      }
      if (match[1] != null) tokens.push({ type: 'code', text: match[1] });
      else if (match[2] != null) tokens.push({ type: 'strong', text: match[2] });
      else if (match[3] != null) tokens.push({ type: 'strong', text: match[3] });
      else if (match[4] != null) tokens.push({ type: 'del', text: match[4] });
      else if (match[5] != null) tokens.push({ type: 'em', text: match[5] });
      else if (match[6] != null) tokens.push({ type: 'em', text: match[6] });
      else if (match[8] != null) {
        // raw is kept so a rejected scheme can be shown as written rather than
        // quietly losing the URL the reader needs to judge it.
        tokens.push({
          type: 'link', text: match[7] || match[8], href: match[8], raw: match[0],
        });
      } else if (match[9] != null) {
        tokens.push({ type: 'link', text: match[9], href: match[9], raw: match[0] });
      } else if (match[10] != null) {
        tokens.push({ type: 'mention', text: '@' + match[10], name: match[10] });
      }
      cursor = match.index + match[0].length;
    }
    if (cursor < source.length) tokens.push({ type: 'text', text: source.slice(cursor) });
    return tokens;
  }

  function isSafeHref(href) {
    const value = String(href || '').trim();
    if (SAFE_SCHEME.test(value)) return true;
    // Scheme-relative and absolute paths are fine; anything else with a colon
    // before the first slash is a scheme we do not trust.
    return !/^[a-z0-9+.-]*:/i.test(value);
  }

  /** Inline tokens → DOM. `opts.onMention` may replace a mention's node. */
  function renderInline(text, doc, opts) {
    const options = opts || {};
    const fragment = doc.createDocumentFragment();
    parseInline(text).forEach((token) => {
      if (token.type === 'text') {
        fragment.appendChild(doc.createTextNode(token.text));
        return;
      }
      if (token.type === 'mention') {
        const custom = options.onMention && options.onMention(token, doc);
        if (custom) { fragment.appendChild(custom); return; }
        const span = doc.createElement('span');
        span.className = 'mention';
        span.textContent = token.text;
        fragment.appendChild(span);
        return;
      }
      if (token.type === 'link') {
        if (!isSafeHref(token.href)) {
          // Not a link, and not hidden either: show the source text.
          fragment.appendChild(doc.createTextNode(token.raw || token.text));
          return;
        }
        const anchor = doc.createElement('a');
        anchor.href = token.href;
        anchor.textContent = token.text;
        anchor.target = '_blank';
        anchor.rel = 'noopener noreferrer';
        fragment.appendChild(anchor);
        return;
      }
      const tag = { code: 'code', strong: 'strong', em: 'em', del: 'del' }[token.type];
      const node = doc.createElement(tag);
      if (token.type === 'code') {
        node.textContent = token.text;              // code is never re-parsed
      } else {
        node.appendChild(renderInline(token.text, doc, options));
      }
      fragment.appendChild(node);
    });
    return fragment;
  }

  /** Markdown → DOM fragment. */
  function render(text, doc, opts) {
    const document_ = doc || (typeof document !== 'undefined' ? document : null);
    if (!document_) throw new Error('render needs a document');
    const fragment = document_.createDocumentFragment();

    parseBlocks(text).forEach((block) => {
      if (block.type === 'hr') {
        fragment.appendChild(document_.createElement('hr'));
        return;
      }
      if (block.type === 'code') {
        const pre = document_.createElement('pre');
        const code = document_.createElement('code');
        if (block.lang) code.className = `lang-${block.lang}`;
        code.textContent = block.text;
        pre.appendChild(code);
        fragment.appendChild(pre);
        return;
      }
      if (block.type === 'h') {
        const heading = document_.createElement(`h${Math.min(block.level + 2, 6)}`);
        heading.appendChild(renderInline(block.text, document_, opts));
        fragment.appendChild(heading);
        return;
      }
      if (block.type === 'ul' || block.type === 'ol') {
        const list = document_.createElement(block.type);
        if (block.type === 'ol' && block.start && block.start !== 1) {
          list.setAttribute('start', String(block.start));
        }
        block.items.forEach((item) => {
          const li = document_.createElement('li');
          li.appendChild(renderInline(item, document_, opts));
          list.appendChild(li);
        });
        fragment.appendChild(list);
        return;
      }
      const node = document_.createElement(
        block.type === 'quote' ? 'blockquote' : 'p');
      // A single newline inside a paragraph is a line break here, not a space:
      // agents lay out short lines and mean them.
      block.text.split('\n').forEach((line, index) => {
        if (index) node.appendChild(document_.createElement('br'));
        node.appendChild(renderInline(line, document_, opts));
      });
      fragment.appendChild(node);
    });
    return fragment;
  }

  const api = { parseBlocks, parseInline, renderInline, render, isSafeHref };
  root.AgentMarkdown = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);

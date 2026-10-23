// Markdown for coach answers: GitHub-flavoured blocks (fenced code, tables, nested and task lists, quotes, rules)
// and inline formatting. Safe by construction: every bit of text is escaped, raw HTML shows as text, and links
// only open http(s), mailto or in-app (#/...) addresses.
(function (root) {
  "use strict";

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
  const SAFE_URL = /^(https?:\/\/|mailto:|#\/)/i;

  // ---------- inline ----------

  function inline(src) {
    const slots = [];
    const keep = (html) => `\u0000${slots.push(html) - 1}\u0001`;
    const link = (url, html) => (SAFE_URL.test(url)
      ? `<a href="${esc(url)}"${url.startsWith("#/") ? "" : ' target="_blank" rel="noopener noreferrer"'}>${html}</a>` : html);
    let s = String(src);

    // Code spans first: nothing inside them is formatted.
    s = s.replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g, (_, t, code) => {
      const c = code.replace(/\n/g, " ");
      return keep(`<code>${esc(/^ .* $/.test(c) && c.trim() ? c.slice(1, -1) : c)}</code>`);
    });
    // Backslash escapes, then autolinks in angle brackets.
    s = s.replace(/\\([!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~])/g, (_, c) => keep(esc(c)));
    s = s.replace(/<((?:https?:\/\/|mailto:)[^\s<>]+)>/g, (_, u) => keep(link(u, esc(u))));
    // Links and images: [text](url "title"), ![alt](url). Images show as links (no remote loads).
    s = s.replace(/(!?)\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(\s*<?([^\s()<>]+(?:\([^\s()]*\))?)>?(?:\s+["'][^"']*["'])?\s*\)/g,
      (_, img, text, url) => keep(link(url, img ? `${esc(text || url)}` : inline(text))));

    s = esc(s);
    s = s.replace(/(\*\*\*|___)(?=\S)([\s\S]*?\S)\1/g, "<strong><em>$2</em></strong>")
      .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?![\w])/g, "$1<strong>$2</strong>")
      .replace(/(^|[^*\w])\*(?=[^\s*])([^*]*?[^\s*])\*(?![*\w])/g, "$1<em>$2</em>")
      .replace(/(^|[^\w])_(?=[^\s_])([^_]*?[^\s_])_(?![\w])/g, "$1<em>$2</em>")
      .replace(/~~(?=\S)([\s\S]*?\S)~~/g, "<del>$1</del>");
    // Bare URLs (not inside a link we already made), minus trailing punctuation.
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<]+[^\s<.,:;!?)\]'"])/g, (_, pre, u) => pre + keep(link(u.replace(/&amp;/g, "&"), u)));
    // Hard line breaks: a newline inside a paragraph is a line break (as in chat apps).
    s = s.replace(/ *\\?\n/g, "<br>");
    for (let i = 0; i < 4 && s.includes("\u0000"); i++) s = s.replace(/\u0000(\d+)\u0001/g, (_, n) => slots[n]);
    return s;
  }

  // ---------- blocks ----------

  const RE = {
    fence: /^ {0,3}(`{3,}|~{3,})\s*([^`\s]*)[^`]*$/,
    heading: /^ {0,3}(#{1,6})(?:\s+(.*?))?(?:\s+#+)?\s*$/,
    hr: /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/,
    quote: /^ {0,3}> ?/,
    item: /^( *)([-*+]|(\d{1,9})[.)])(?:[ \t]+(.*))?$/,
    tableSep: /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/,
  };
  const blank = (l) => !l.trim();
  const indentOf = (l) => l.match(/^ */)[0].length;
  const startsBlock = (l, next) => RE.fence.test(l) || RE.heading.test(l) || RE.hr.test(l) || RE.quote.test(l)
    || (RE.item.test(l) && !blank(RE.item.exec(l)[4] || "x")) || isTable(l, next);
  function isTable(l, next) {
    return l.includes("|") && next != null && RE.tableSep.test(next) && next.includes("-")
      && cells(next).length === cells(l).length;
  }
  function cells(l) {
    let s = l.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    const out = [];
    let cur = "", code = false;
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (c === "\\" && s[i + 1] === "|") { cur += "|"; i++; continue; }
      if (c === "`") code = !code;
      if (c === "|" && !code) { out.push(cur.trim()); cur = ""; } else cur += c;
    }
    out.push(cur.trim());
    return out;
  }

  function blocks(lines) {
    let html = "", i = 0;
    while (i < lines.length) {
      const l = lines[i];
      let m;
      if (blank(l)) { i++; continue; }

      if ((m = RE.fence.exec(l))) {
        const fence = m[1], lang = m[2], ind = indentOf(l), body = [];
        i++;
        while (i < lines.length && !new RegExp(`^ {0,3}${fence[0]}{${fence.length},}\\s*$`).test(lines[i])) {
          body.push(lines[i].replace(new RegExp(`^ {0,${ind}}`), ""));
          i++;
        }
        i++;  // closing fence (or end of answer)
        html += `<div class="md-code"><div class="md-code-head"><span>${esc(lang || "text")}</span>`
          + `<button type="button" class="md-copy">Copy</button></div><pre><code>${esc(body.join("\n"))}</code></pre></div>`;
        continue;
      }
      if ((m = RE.heading.exec(l))) {
        const level = Math.min(6, m[1].length + 2);  // # sits under the page's own headings
        html += `<h${level}>${inline(m[2] || "")}</h${level}>`;
        i++;
        continue;
      }
      if (RE.hr.test(l)) { html += "<hr>"; i++; continue; }
      if (RE.quote.test(l)) {
        const body = [];
        while (i < lines.length && !blank(lines[i]) && (RE.quote.test(lines[i]) || !startsBlock(lines[i], lines[i + 1]))) {
          body.push(lines[i].replace(RE.quote, ""));
          i++;
        }
        html += `<blockquote>${blocks(body)}</blockquote>`;
        continue;
      }
      if (isTable(l, lines[i + 1])) {
        const head = cells(l), align = cells(lines[i + 1]).map((c) =>
          (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : c.startsWith(":") ? "left" : ""));
        const td = (tag, c, k) => `<${tag}${align[k] ? ` class="al-${align[k]}"` : ""}>${inline(c)}</${tag}>`;
        let t = `<thead><tr>${head.map((c, k) => td("th", c, k)).join("")}</tr></thead><tbody>`;
        i += 2;
        while (i < lines.length && !blank(lines[i]) && lines[i].includes("|")) {
          const row = cells(lines[i]);
          t += `<tr>${head.map((_, k) => td("td", row[k] ?? "", k)).join("")}</tr>`;
          i++;
        }
        html += `<div class="md-table"><table>${t}</tbody></table></div>`;
        continue;
      }
      if ((m = RE.item.exec(l))) {
        const [out, next] = list(lines, i);
        html += out;
        i = next;
        continue;
      }
      const para = [];
      while (i < lines.length && !blank(lines[i]) && (!para.length || !startsBlock(lines[i], lines[i + 1]))) {
        para.push(lines[i].trim());
        i++;
      }
      html += `<p>${inline(para.join("\n"))}</p>`;
    }
    return html;
  }

  function list(lines, start) {
    const first = RE.item.exec(lines[start]);
    const base = first[1].length, ordered = !!first[3], bullet = first[2].slice(-1);
    const same = (m) => m && m[1].length <= base + 1 && m[1].length >= base - 1 && !!m[3] === ordered
      && m[2].slice(-1) === bullet;
    const items = [];
    let i = start, loose = false;
    while (i < lines.length) {
      const m = RE.item.exec(lines[i]);
      if (!same(m) || indentOf(lines[i]) > base + 1) break;
      const pad = m[1].length + m[2].length + 1;
      const body = [m[4] || ""];
      i++;
      let gap = false;
      while (i < lines.length) {
        const l = lines[i];
        if (blank(l)) { gap = true; i++; continue; }
        const ind = indentOf(l);
        if (ind >= pad || (ind > base && RE.item.test(l))) {  // nested content or a nested list
          if (gap) { body.push(""); loose = loose || !RE.item.test(l.slice(Math.min(ind, pad))); }
          body.push(l.slice(Math.min(ind, pad)));
          gap = false;
          i++;
          continue;
        }
        if (!gap && !startsBlock(l, lines[i + 1]) && !RE.item.test(l)) { body.push(l.trim()); i++; continue; }  // lazy line
        break;
      }
      items.push(body);
      if (gap && i < lines.length && same(RE.item.exec(lines[i]))) loose = true;
      if (gap && (i >= lines.length || !same(RE.item.exec(lines[i])))) break;
    }
    const lis = items.map((body) => {
      let task = "";
      const t = /^\[([ xX])\]\s+/.exec(body[0]);
      if (t) {
        task = `<input type="checkbox" disabled${t[1] === " " ? "" : " checked"} aria-label="${t[1] === " " ? "Not done" : "Done"}"> `;
        body[0] = body[0].slice(t[0].length);
      }
      let inner = blocks(body);
      if (!loose) inner = inner.replace(/^<p>([\s\S]*?)<\/p>/, "$1");
      return `<li${task ? ' class="task"' : ""}>${task}${inner}</li>`;
    }).join("");
    const n = ordered ? Number(first[3]) : 1;
    const tag = ordered ? "ol" : "ul";
    return [`<${tag}${ordered && n !== 1 ? ` start="${n}"` : ""}>${lis}</${tag}>`, i];
  }

  function md(src) {
    const lines = String(src ?? "").replace(/\r\n?/g, "\n").replace(/\t/g, "    ").split("\n");
    return blocks(lines);
  }

  root.md = md;
  if (typeof document !== "undefined") {
    // Copy buttons on code blocks, wherever an answer is shown.
    document.addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest(".md-copy");
      if (!b) return;
      const code = b.closest(".md-code").querySelector("code").textContent;
      const done = (label) => { b.textContent = label; setTimeout(() => { b.textContent = "Copy"; }, 1500); };
      navigator.clipboard.writeText(code).then(() => done("Copied"), () => done("Couldn't copy"));
    });
  }
})(typeof window !== "undefined" ? window : globalThis);

/**
 * markdown.js — the only place markdown becomes HTML: marked output always passes through
 * DOMPurify before any innerHTML. Non-module script, loaded after marked and DOMPurify and
 * before browser.js / app.js, which call window.renderMarkdown / window.renderMarkdownInline.
 */
marked.use({ breaks: true, gfm: true });

function renderMarkdown(markdownText) {
  return DOMPurify.sanitize(marked.parse(String(markdownText ?? '')));
}

function renderMarkdownInline(markdownText) {
  return DOMPurify.sanitize(marked.parseInline(String(markdownText ?? '')));
}

window.renderMarkdown       = renderMarkdown;
window.renderMarkdownInline = renderMarkdownInline;

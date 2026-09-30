/**
 * markdown.js — the only place markdown (LLM answers, DB text, help page) becomes HTML: marked output
 * always passes through sanitizeHtml before any innerHTML. Non-module script, loaded after marked and
 * DOMPurify and before browser.js / app.js, which call window.renderMarkdown / window.renderMarkdownInline.
 *
 * The sanitizer keeps plain text formatting only: no forms or inputs (a fake "re-enter your key" form),
 * no style / class / id (a full-page overlay through inline CSS or Tailwind utility classes, or an element
 * shadowing one of the page's ids), no data-* (the page's data-click actions), no images or media (an
 * ![](/v1/...) in LLM or DB text would make every viewer's browser send requests). Links open http(s) targets
 * in a new tab without an opener; any other scheme except mailto loses its href.
 */
marked.use({ breaks: true, gfm: true });

const SANITIZE_OPTIONS = {
  FORBID_TAGS: ['form', 'input', 'textarea', 'select', 'option', 'button', 'style', 'iframe', 'frame',
                'object', 'embed', 'dialog', 'svg', 'math', 'link', 'meta', 'base',
                'img', 'image', 'video', 'audio', 'source', 'picture', 'track'],
  FORBID_ATTR: ['style', 'class', 'id', 'name', 'target', 'popover', 'popovertarget', 'formaction'],
  ALLOW_DATA_ATTR: false,
};

const LINK_PROTOCOLS_IN_NEW_TAB = new Set(['http:', 'https:']);

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName !== 'A') return;
  node.setAttribute('rel', 'noopener noreferrer');
  const href = node.getAttribute('href');
  if (href == null) return;
  let protocol = '';
  try {
    protocol = new URL(href, window.location.href).protocol;
  } catch (exc) {
    console.error('[markdown] dropping unparsable link href:', exc);
  }
  if (LINK_PROTOCOLS_IN_NEW_TAB.has(protocol)) {
    node.setAttribute('target', '_blank');
  } else if (protocol !== 'mailto:') {
    node.removeAttribute('href');
  }
});

function sanitizeHtml(html) {
  return DOMPurify.sanitize(html, SANITIZE_OPTIONS);
}

function renderMarkdown(markdownText) {
  return sanitizeHtml(marked.parse(String(markdownText ?? '')));
}

function renderMarkdownInline(markdownText) {
  return sanitizeHtml(marked.parseInline(String(markdownText ?? '')));
}

window.renderMarkdown       = renderMarkdown;
window.renderMarkdownInline = renderMarkdownInline;

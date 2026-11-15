// Build navigation from the actual visible workflow, including dynamically opened steps.
const main = document.querySelector('main');
const nav = document.createElement('aside');
nav.className = 'workspace-nav';
nav.innerHTML = `<div class="workspace-brand"><svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><rect x="3" y="3" width="26" height="26" rx="7" stroke="currentColor" stroke-width="2"/><path d="m10 16 4 4 8-9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>Canvas 工作台</span></div><p>工作流程</p><nav class="workspace-links" aria-label="页面步骤"></nav>`;
document.body.prepend(nav);
const links = nav.querySelector('nav');
let activeId = '';
function refreshNavigation() {
  const sections = [...main.querySelectorAll('section.card')].filter(section => section.getClientRects().length && section.querySelector('h2'));
  const signature = sections.map(section => section.id || [...main.querySelectorAll('section.card')].indexOf(section)).join('|');
  if (links.dataset.signature === signature) return;
  links.dataset.signature = signature;
  links.replaceChildren();
  for (const [index, section] of sections.entries()) {
    if (!section.id) section.id = `workflow-section-${[...main.querySelectorAll('section.card')].indexOf(section)}`;
    const link = document.createElement('a');
    link.href = `#${section.id}`;
    link.textContent = section.querySelector('h2').textContent.trim().replace(/^0\d\s*/, '');
    link.addEventListener('click', () => {activeId = section.id; markActive();});
    links.append(link);
  }
  markActive();
}
function markActive() {
  const items = [...links.querySelectorAll('a')];
  if (!items.some(a => a.hash === `#${activeId}`)) activeId = items[0]?.hash.slice(1) || '';
  items.forEach(a => a.hash === `#${activeId}` ? a.setAttribute('aria-current','location') : a.removeAttribute('aria-current'));
}
new MutationObserver(refreshNavigation).observe(main, {subtree:true, attributes:true, attributeFilter:['hidden'], childList:true});
refreshNavigation();
let scheduled = false;
window.addEventListener('scroll', () => {
  if (scheduled) return;
  scheduled = true;
  requestAnimationFrame(() => {
    scheduled = false;
    for (const a of links.querySelectorAll('a')) {
      if (document.getElementById(a.hash.slice(1)).getBoundingClientRect().top <= 180) activeId = a.hash.slice(1);
    }
    markActive();
  });
}, {passive:true});

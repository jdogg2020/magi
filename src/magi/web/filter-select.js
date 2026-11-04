/* Searchable dropdowns backed by native selects: only listed values can be chosen. */
const MagiFilterSelect = (() => {
  const controls = new Map();
  function enhance(select) {
    if (controls.has(select.id)) return;
    const name = select.parentElement.firstChild.textContent.trim();
    const wrap = document.createElement('div'); wrap.className = 'filter-select';
    const toggle = document.createElement('button'); toggle.type = 'button';
    toggle.className = 'filter-select-toggle'; toggle.setAttribute('aria-label', name);
    toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-haspopup', 'listbox');
    const panel = document.createElement('div'); panel.className = 'filter-select-panel'; panel.hidden = true;
    const search = document.createElement('input'); search.type = 'search';
    search.placeholder = `Search ${name.toLowerCase()}…`; search.setAttribute('aria-label', `Search ${name.toLowerCase()}`);
    search.setAttribute('role', 'combobox'); search.setAttribute('aria-autocomplete', 'list');
    search.setAttribute('aria-expanded', 'false');
    const list = document.createElement('div'); list.id = select.id + '-choices';
    list.className = 'filter-select-choices'; list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', name);
    search.setAttribute('aria-controls', list.id); toggle.setAttribute('aria-controls', list.id);
    const empty = document.createElement('p'); empty.textContent = 'No matching choices'; empty.setAttribute('role', 'status');
    panel.append(search, list, empty); wrap.append(toggle, panel); select.after(wrap); select.hidden = true;
    let matches = [], active = -1;
    function highlight(index) {
      active = index;
      [...list.children].forEach((item, i) => item.classList.toggle('highlighted', i === index));
      if (index >= 0) { search.setAttribute('aria-activedescendant', list.children[index].id); list.children[index].scrollIntoView({block:'nearest'}); }
      else search.removeAttribute('aria-activedescendant');
    }
    function close(focus = false) {
      panel.hidden = true; toggle.setAttribute('aria-expanded', 'false'); search.setAttribute('aria-expanded', 'false');
      search.removeAttribute('aria-activedescendant'); if (focus) toggle.focus();
    }
    function choose(option) {
      select.value = option.value; select.dispatchEvent(new Event('change', {bubbles:true})); sync(); close(true);
    }
    function draw() {
      const query = search.value.trim().toLocaleLowerCase();
      matches = [...select.options].filter(o => !o.disabled && o.textContent.toLocaleLowerCase().includes(query));
      list.replaceChildren(); active = -1; search.removeAttribute('aria-activedescendant');
      matches.forEach((option, index) => {
        const item = document.createElement('div'); item.id = list.id + '-' + index;
        item.setAttribute('role', 'option'); item.setAttribute('aria-selected', String(option.value === select.value));
        item.textContent = option.textContent;
        // Keep focus on the search box until the pointer click commits the choice.
        item.onmousedown = event => event.preventDefault();
        item.onclick = event => { event.preventDefault(); choose(option); };
        list.append(item);
      });
      empty.hidden = matches.length > 0;
    }
    function sync() {
      toggle.textContent = (select.selectedOptions[0]?.textContent || 'Any') + ' ▾';
      if (!panel.hidden) draw();
    }
    function open() {
      for (const control of controls.values()) control.close();
      panel.hidden = false; search.value = ''; draw();
      toggle.setAttribute('aria-expanded', 'true'); search.setAttribute('aria-expanded', 'true'); search.focus();
    }
    toggle.onclick = () => panel.hidden ? open() : close();
    toggle.onkeydown = event => { if (event.key === 'ArrowDown') { event.preventDefault(); open(); highlight(0 < matches.length ? 0 : -1); } };
    search.oninput = draw;
    search.onkeydown = event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); if (matches.length) highlight(active < 0 ? (event.key === 'ArrowDown' ? 0 : matches.length - 1) : (active + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length);
      } else if (event.key === 'Enter') {
        event.preventDefault(); if (active >= 0) choose(matches[active]); else if (matches.length === 1) choose(matches[0]);
      } else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); }
    };
    wrap.addEventListener('focusout', () => { setTimeout(() => { if (!wrap.contains(document.activeElement)) close(); }, 0); });
    document.addEventListener('pointerdown', event => { if (!wrap.contains(event.target)) close(); });
    select.addEventListener('change', sync);
    new MutationObserver(sync).observe(select, {childList:true, subtree:true, attributes:true});
    controls.set(select.id, {sync, close}); sync();
  }
  function sync() { for (const control of controls.values()) control.sync(); }
  document.addEventListener('DOMContentLoaded', () => document.querySelectorAll('#event-filters select').forEach(enhance));
  return {sync};
})();

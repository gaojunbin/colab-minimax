// Integration docs: tab switching, transport and language toggles, live origin.
(() => {
  'use strict';
  $$('.dyn-origin').forEach((el) => {
    el.textContent = window.location.origin;
  });

  const panels = $$('[data-panel]');
  const showPanel = (name) => {
    panels.forEach((panel) => {
      // Only the initial reveal is delayed; later tab switches animate at once.
      panel.style.removeProperty('--delay');
      panel.hidden = panel.dataset.panel !== name;
    });
  };
  const showGroup = (attr, value) => {
    $$(`[data-${attr}]`).forEach((el) => {
      el.hidden = el.dataset[attr] !== value;
    });
  };

  const tabs = initSegmented($('#seg-docs'), (value) => {
    showPanel(value);
    history.replaceState(null, '', value === 'api' ? '#api' : '#mcp');
  });
  if (window.location.hash === '#api') {
    tabs.set('api', true);
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.panel !== 'api';
    });
  }

  initSegmented($('#seg-transport'), (value) => showGroup('transport', value));

  const langCopy = $('#copy-lang');
  initSegmented($('#seg-lang'), (value) => {
    showGroup('lang', value);
    langCopy.dataset.copyTarget = `code-${value}`;
  });
})();

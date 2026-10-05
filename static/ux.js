/* Progressive workbench enhancements. Bound once across Turbo visits. */
(function () {
  if (window.__mtUx) return;
  window.__mtUx = true;

  function reportError(output, message) {
    output.textContent = message;
    if (window.toast) window.toast(message, 'err');
  }

  function markEdited(event) {
    var form = event.target.closest('#ruleform, #flowform');
    if (!form) return;
    var panel = form.querySelector('.ux-preview');
    if (!panel) return;
    panel.dataset.editVersion = String(Number(panel.dataset.editVersion || 0) + 1);
    if (panel.dataset.previewCompleted !== '1' || panel.querySelector('[data-stale-preview]')) return;
    var note = document.createElement('p');
    note.dataset.stalePreview = '1'; note.className = 'msg warn';
    note.textContent = 'Draft changed since this preview. Run preview again before relying on it.';
    panel.querySelector('.ux-preview-output').prepend(note);
  }
  document.addEventListener('input', markEdited);
  document.addEventListener('change', markEdited);

  document.addEventListener('click', async function (event) {
    var button = event.target.closest('button');
    if (!button) return;
    if (button.hasAttribute('data-open-reply')) {
      var reply = document.getElementById('reply-composer');
      if (reply) { reply.open = true; reply.scrollIntoView({block: 'center'}); reply.querySelector('select').focus(); }
    }
    if (button.hasAttribute('data-add-rule-condition')) {
      var hidden = document.querySelector('.rule-cond[hidden]');
      if (hidden) { hidden.hidden = false; hidden.querySelector('select').focus(); }
      button.hidden = !document.querySelector('.rule-cond[hidden]');
    }
    if (button.hasAttribute('data-open-preview')) {
      var panel = button.closest('form').querySelector('.ux-preview');
      panel.open = true;
      panel.scrollIntoView({block: 'start'});
      panel.querySelector('select').focus();
    }
    if (button.hasAttribute('data-preview-run')) {
      var preview = button.closest('.ux-preview');
      var output = preview.querySelector('.ux-preview-output');
      var data = new FormData(button.closest('form'));
      data.set('preview_kind', preview.dataset.previewKind);
      data.set('preview_id', preview.dataset.previewId);
      ['from', 'to', 'subject', 'body'].forEach(function (key) { data.set('preview_' + key, preview.querySelector('[data-preview-' + key + ']').value); });
      data.set('preview_llm', preview.querySelector('[data-preview-llm]').checked ? '1' : '0');
      button.disabled = true;
      preview.dataset.previewCompleted = '0';
      var version = preview.dataset.editVersion;
      output.textContent = 'Testing your unsaved draft…';
      try {
        var response = await fetch('/automation/preview', {method: 'POST', body: data});
        var result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Preview failed.');
        output.innerHTML = result.html; // escaped, server-rendered report, never mail HTML
        preview.dataset.previewCompleted = '1';
        if (version !== preview.dataset.editVersion) markEdited({target: button});
      } catch (error) { reportError(output, error.message); }
      finally { button.disabled = false; }
    }
  });

  document.addEventListener('change', async function (event) {
    var select = event.target;
    if (!select.hasAttribute('data-preview-message') || !select.value) return;
    var panel = select.closest('.ux-preview');
    var output = panel.querySelector('.ux-preview-output');
    select.disabled = true;
    try {
      var response = await fetch('/messages/' + encodeURIComponent(select.value) + '/preview.json');
      var data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Message could not be loaded.');
      ['from', 'to', 'subject', 'body'].forEach(function (key) { panel.querySelector('[data-preview-' + key + ']').value = data[key === 'from' ? 'from_addr' : key === 'to' ? 'to_addr' : key] || ''; });
      output.textContent = 'Example loaded. Edit it or run the preview.';
      panel.dataset.previewCompleted = '0';
    } catch (error) { reportError(output, error.message); }
    finally { select.disabled = false; }
  });

  function advanced(form, names, title) {
    if (!form || form.querySelector('[data-ux-advanced]')) return;
    var rows = names.map(function (name) { var input = form.querySelector('[name="' + name + '"]'); return input && input.closest('.setrow'); }).filter(Boolean);
    rows = Array.from(new Set(rows));
    if (!rows.length) return;
    var details = document.createElement('details');
    details.className = 'ux-advanced'; details.dataset.uxAdvanced = '1';
    var summary = document.createElement('summary'); summary.textContent = title || 'Advanced';
    details.appendChild(summary);
    rows[0].before(details);
    rows.forEach(function (row) { details.appendChild(row); });
  }

  function settings() {
    var nav = document.querySelector('.setnav');
    if (!nav || nav.dataset.uxReady) return;
    nav.dataset.uxReady = '1';
    var search = document.getElementById('searchidx');
    var endpoints = document.getElementById('ai-search');
    if (search && endpoints) search.appendChild(endpoints);
    var general = document.getElementById('general');
    var status = document.getElementById('status');
    if (general && status && !status.closest('[data-ux-status]')) {
      var details = document.createElement('details'); details.className = 'card ux-advanced';
      details.dataset.uxStatus = '1';
      var summary = document.createElement('summary'); summary.textContent = 'Advanced · effective system status';
      details.appendChild(summary); general.appendChild(details); details.appendChild(status);
    }
    advanced(document.getElementById('llm-form'), ['llm_timeout', 'llm_thinking', 'llm_api_key_clear'], 'Advanced · timeout & reasoning');
    advanced(document.getElementById('rag-form'), ['local_embed_threads', 'embed_protocol', 'embed_timeout', 'embed_query_prefix', 'rerank_protocol', 'rerank_timeout', 'embed_api_key_clear', 'rerank_api_key_clear'], 'Advanced · search protocols & performance');
    var sections = ['general', 'ai', 'mail', 'sorting', 'searchidx'];
    var aliases = {status: 'general', 'ai-search': 'searchidx'};
    function selectSection() {
      var target = location.hash.slice(1) || 'general';
      var node = document.getElementById(target);
      var section = aliases[target] || (node && node.closest('.setbody>section') && node.closest('.setbody>section').id) || 'general';
      sections.forEach(function (id) { var element = document.getElementById(id); if (element) element.hidden = id !== section; });
      nav.querySelectorAll('a').forEach(function (link) { var active = link.hash === '#' + section; link.classList.toggle('on', active); if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current'); });
      if (node) { var parent = node.parentElement; while (parent && parent !== document.body) { if (parent.tagName === 'DETAILS') parent.open = true; parent = parent.parentElement; } }
    }
    selectSection();
    nav.addEventListener('click', function (event) {
      var link = event.target.closest('a');
      if (!link) return;
      event.preventDefault(); history.replaceState(history.state, '', link.hash); selectSection();
      document.querySelector('.setbody').scrollIntoView({block: 'start'});
    });
    if (window.__mtUxSettingsHash) window.removeEventListener('hashchange', window.__mtUxSettingsHash);
    window.__mtUxSettingsHash = selectSection;
    window.addEventListener('hashchange', selectSection);
  }

  function initialize() {
    var disagreements = document.getElementById('learning-disagreements');
    if (disagreements && location.hash === '#learning-disagreements') disagreements.open = true;
    var dashboard = document.getElementById('dash-workbench');
    if (dashboard) Array.from(dashboard.children).sort(function (a, b) { return Number(a.dataset.order) - Number(b.dataset.order); }).forEach(function (child) { dashboard.appendChild(child); });
    settings();
    var composer = document.querySelector('#aform textarea');
    if (!composer) composer = document.querySelector('.assistant-main textarea');
    var prompt = new URLSearchParams(location.search).get('prompt');
    if (composer && prompt) { composer.value = prompt.slice(0, 1000); composer.dispatchEvent(new Event('input', {bubbles: true})); composer.focus(); }
    document.querySelectorAll('.emailbody').forEach(function (body) {
      if (body.dataset.uxQuotes) return;
      body.dataset.uxQuotes = '1';
      var marker = body.querySelector('#divRplyFwdMsg, .gmail_quote, blockquote');
      if (!marker) return;
      var details = document.createElement('details'); details.className = 'quote';
      var summary = document.createElement('summary'); summary.textContent = 'Show quoted history'; details.appendChild(summary);
      marker.before(details);
      // The Outlook reply marker and following siblings are one quoted tail.
      var node = marker;
      while (node) { var next = node.nextSibling; details.appendChild(node); if (marker.matches('blockquote, .gmail_quote')) break; node = next; }
    });
  }
  document.addEventListener('turbo:load', initialize);
  document.addEventListener('turbo:before-cache', function () {
    var nav = document.querySelector('.setnav');
    if (nav) delete nav.dataset.uxReady;
    if (window.__mtUxSettingsHash) {
      window.removeEventListener('hashchange', window.__mtUxSettingsHash);
      window.__mtUxSettingsHash = null;
    }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
  else initialize();
})();

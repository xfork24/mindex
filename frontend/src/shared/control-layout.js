// 表单字段按内容确定宽度；同组字段横向排列，空间不足时自然换行。
(function () {
  'use strict';
  // .delivery-fields/.delivery-platform-form 同样不在列：交付配置改用严格的
  // 两列网格（标签列 + 控件列），.content-flow 的 display:flex!important 会反转网格。
  // .runtime-row 同样不在列：.content-flow 会给 p 施加 flex-basis:100%，
  // 状态文字被迫独占整行，把按钮挤到下一行。
  // .weight-grid/.tuning-row 不在列：settings.css 已用 display:contents 把两者拍平进
  // .tuning-controls；若在此纳管，.content-flow 的 display:flex!important 会把它们反转成
  // 独立 flex 盒，换行落在容器边界而非内容宽度（settings_tuning_ui 的正当换行断言会失败）。
  const flows = '.form-grid,.filter-grid,.fields,.template-fields,.preview-inputs,.source-fields,.tool-fields,.topics-weights,.search-scope-row,.search-filter-row,.person-search-options,.material-scope-controls,.recall-scope-options,.source-options,.recall-primary-options,.recall-filters,.shot-editor-fields,.post-row,.delivery-export-grid,.tuning-controls,.voice-default-row,.work-controls,.topic-material-scope,.source-filters,.segment-filters,.calendar-row,.group-fields,.card-filters,.form-bottom,.coordinates,.cast-entry-row .work-row,.clone-fields,.profile-connection-fields,#capture-form,#process-form,#entity-form,#create-form,.dialogue-row';
  const canvas = document.createElement('canvas'), context = canvas.getContext('2d');
  let pending = false;
  function width(text, style) {
    context.font = style.font || `${style.fontSize} ${style.fontFamily}`;
    return context.measureText(text || '').width;
  }
  function setWidth(node, value, field = false) {
    const next = Math.ceil(value) + 'px';
    if (node.style.getPropertyValue('--control-content-width') !== next) node.style.setProperty('--control-content-width', next);
    const name = field ? 'content-field' : 'content-control', other = field ? 'content-control' : 'content-field';
    if (node.classList.contains(other)) node.classList.remove(other);
    if (!node.classList.contains(name)) node.classList.add(name);
  }
  function resize() {
    pending = false;
    // 逐镜编辑区自行按比例分栏，避免内容测量重新把字段压回固定宽度。
    for (const node of document.querySelectorAll(flows)) if (!node.closest('.editor-stretch-row') && !node.classList.contains('content-flow')) node.classList.add('content-flow');
    for (const control of document.querySelectorAll('main select,dialog select,main input,dialog input')) {
      if (control.closest('.editor-stretch-row')) continue;
      if (control.closest('.agent-drawer') || ['checkbox', 'radio', 'hidden', 'range', 'file', 'color'].includes(control.type)) continue;
      if (!control.getClientRects().length) continue;
      const multi = control.closest('.multi-select');
      const field = control.closest('.group-picker') || control.closest('label,.form-field,.field');
      // 同一个字段有多个输入时保留它的内部关系，只调整控件本身。
      const host = field && !field.querySelector('button') && field.querySelectorAll('select,input:not([type=checkbox],[type=radio])').length === (multi ? 2 : 1) ? field : null;
      const style = getComputedStyle(control);
      let desired;
      if (control.tagName === 'SELECT') {
        const padding = ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth']
          .reduce((sum, key) => sum + (parseFloat(style[key]) || 0), 0);
        desired = Math.max(control.multiple ? 64 : 24, ...[...control.options].map((option) => width(option.textContent, style)))
          + padding + (control.multiple ? 24 : 12);
      } else {
        const sample = control.value || control.placeholder || '';
        desired = Math.max(control.type === 'number' ? 80 : 150, Math.min(460, width(sample, style) + 32));
      }
      if (host) {
        const caption = [...host.childNodes].filter((node) => node.nodeType === 3).map((node) => node.textContent).join(' ').trim()
          || host.querySelector(':scope>legend,:scope>span')?.textContent || '';
        const hostStyle = getComputedStyle(host);
        const inline = hostStyle.display.includes('flex') && hostStyle.flexDirection === 'row';
        const captionWidth = width(caption, hostStyle);
        const fieldWidth = inline ? desired + captionWidth + (parseFloat(hostStyle.columnGap) || 8)
          : Math.max(desired, captionWidth + 8);
        if (host.classList.contains('content-inline') !== inline) host.classList.toggle('content-inline', inline);
        setWidth(host, fieldWidth, true);
      }
      setWidth(multi || control, desired);
      if (multi && control.tagName === 'SELECT') {
        setWidth(control, desired);
        if (!control.classList.contains('content-options')) control.classList.add('content-options');
      }
    }
  }
  function schedule() { if (!pending) { pending = true; requestAnimationFrame(resize); } }
  function start() {
    new MutationObserver(schedule).observe(document.body, {subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ['hidden', 'open', 'placeholder', 'class']});
    document.addEventListener('input', schedule);
    document.addEventListener('change', schedule);
    window.addEventListener('resize', schedule);
    window.addEventListener('mindex:languagechange', schedule);
    document.fonts?.ready.then(schedule);
    schedule();
  }
  window.MindexControlLayout = {refresh: schedule};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();

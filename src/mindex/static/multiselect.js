// 共享人物多选组件：一个边框内包含过滤输入和可调高度的列表。
// 用法（各页 fill 函数内）：
//   MindexMultiSearch.sort(entries)        —— 渲染前按中文字典序排序
//   MindexMultiSearch.attach(select)       —— 首次挂搜索框（幂等）
//   MindexMultiSearch.refilter(select)     —— 重填选项后重放当前过滤
(function () {
  'use strict';
  var ALL_VALUE = '__all__';
  var serial = 0;
  function matches(option, query) {
    return option.value === ALL_VALUE || !query || option.textContent.toLowerCase().includes(query);
  }
  var api = {
    sort: function (entries) {
      return entries.slice().sort(function (a, b) {
        return (a.name || '').localeCompare(b.name || '', 'zh') || (a.id || '').localeCompare(b.id || '');
      });
    },
    attach: function (select) {
      if (!select || !select.multiple || select.dataset.searchable) return;
      select.dataset.searchable = '1';
      if (!select.id) select.id = 'multi-select-' + (++serial);
      var label = select.closest('label');
      if (label) label.htmlFor = select.id;
      var control = document.createElement('div');
      control.className = 'multi-select';
      var list = document.createElement('div');
      list.className = 'multi-list';
      var input = document.createElement('input');
      input.type = 'search';
      input.className = 'multi-search';
      MindexI18n.setAttr(input, "placeholder", MindexI18n.msg("ui.73702e17a417"));
      MindexI18n.setAttr(input, 'aria-label', MindexI18n.msg("ui.78c0a3251da4"));
      input.addEventListener('input', function () {
        var query = input.value.trim().toLowerCase();
        select.dataset.query = query;
        for (var i = 0; i < select.options.length; i++)
        select.options[i].hidden = !matches(select.options[i], query);
      });
      select.parentNode.insertBefore(control, select);
      control.append(input, list);
      list.append(select);
    },
    refilter: function (select) {
      if (!select || !select.multiple || !select.dataset.searchable) return;
      var query = (select.dataset.query || '').toLowerCase();
      for (var j = 0; j < select.options.length; j++)
      {select.options[j].hidden = !matches(select.options[j], query);select.options[j].title = select.options[j].textContent;}
    }
  };
  window.MindexMultiSearch = api;
})();

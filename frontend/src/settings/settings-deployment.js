'use strict';
(() => {
  const editor = document.getElementById('deployment-editor');
  if (!editor) return;
  const status = document.getElementById('deployment-editor-status');
  const validate = document.getElementById('deployment-validate');
  const save = document.getElementById('deployment-save');
  const reload = document.getElementById('deployment-reload');
  let revision = null;
  let dirty = false;
  const msg = (key) => MindexI18n.msg(`ui.${key}`);
  const display = (text) => MindexI18n.setText(status, text);
  const report = (error) => display(MindexI18n.system(error.message));
  async function load() {
    if (dirty && !window.confirm(msg('deployment_discard'))) return;
    const data = await MindexApi.request('/api/settings/deployment');
    revision = data.revision;
    editor.value = JSON.stringify(data.config, null, 2);
    editor.readOnly = !data.writable;
    validate.disabled = save.disabled = !data.writable;
    dirty = false;
    display(!data.writable ? msg('deployment_readonly') : data.restart_required ? msg('deployment_restart') : msg('deployment_edit_help'));
    const fields = document.getElementById('deployment-field-rules');
    fields.replaceChildren();
    for (const [path, rule] of Object.entries(data.fields)) {
      const row = document.createElement('li');
      row.textContent = `${path}: ${!rule.editable ? msg('deployment_field_locked') : rule.migration_required ? msg('deployment_field_migration') : msg('deployment_field_restart')}`;
      fields.append(row);
    }
  }
  editor.addEventListener('input', () => { dirty = true; });
  async function submit(method) {
    const config = JSON.parse(editor.value);
    const result = await MindexApi.request('/api/settings/deployment' + (method === 'POST' ? '/validate' : ''), {
      method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ revision, config })
    });
    if (result.saved) { revision = result.revision; dirty = false; }
    display(result.saved ? (result.restart_required ? msg('deployment_restart') : msg('deployment_saved')) : result.migration_required ? msg('deployment_migration') : msg('deployment_valid'));
  }
  validate.addEventListener('click', () => submit('POST').catch(report));
  save.addEventListener('click', () => submit('PUT').catch(report));
  reload.addEventListener('click', () => load().catch(report));
  window.addEventListener('beforeunload', (event) => {
    if (dirty) { event.preventDefault(); event.returnValue = ''; }
  });
  load().catch(report);
})();

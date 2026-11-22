// AI-first review: "Draft with AI" fills every card (note, codes, care gaps,
// test orders, referral); the clinician edits or approves each item.
//
// The AI's own version of each item is recorded on the server first
// (generate_encounter_note, suggest_codes, record_ai_suggestions), so when the
// clinician approves, the server compares and records whether it was approved
// as-is or edited, and how quickly. This file only drives the screen; it never
// decides what counts as an edit.
//
// Loaded after the main script in index.html and uses its globals
// (state, sessions, activeSessionKey, callTool, showResult, escapeHtml,
// addGapRow, addOrderRow, generateNote, loadSourceLabels).

const SCRATCH_KEY = '__scratch__';
const reviewStore = new Map(); // session key -> { codes, gapSuggestions, orderRows }

function reviewKey() { return activeSessionKey || SCRATCH_KEY; }

// ---------- per-patient state (rows are DOM, so snapshot them on switch) ----------

function captureReviewState() {
  reviewStore.set(reviewKey(), {
    codes: readCodeRows(),
    gapSuggestions: readGapSuggestions(),
    orderRows: readOrderRows(),
  });
}

function restoreReviewState() {
  const key = reviewKey();
  let saved = reviewStore.get(key);
  // A patient picked straight after working without one adopts that scratch work.
  if (!saved && key !== SCRATCH_KEY && reviewStore.has(SCRATCH_KEY)) {
    saved = reviewStore.get(SCRATCH_KEY);
    reviewStore.delete(SCRATCH_KEY);
  }
  saved = saved || { codes: [], gapSuggestions: [], orderRows: [] };
  renderCodeRows(saved.codes);
  renderGapSuggestions(saved.gapSuggestions);
  renderOrderRows(saved.orderRows);
}

// ---------- review outcome text ----------

function reviewSummary(review) {
  if (!review) return '';
  if (review.outcome === 'clinician_entered') return 'Entered by the clinician (no AI version to compare).';
  let text = review.outcome === 'approved_as_is'
    ? 'Approved exactly as the AI suggested'
    : `Approved with clinician edits: ${review.changes.join('; ')}`;
  if (review.review_seconds != null) text += ` (reviewed for ${review.review_seconds}s)`;
  text += '.';
  if (review.possibly_unread) text += ' ⚠ Approved unedited faster than it could be read — flagged in the audit trail for follow-up.';
  return text;
}

// ---------- codes (one row per code) ----------

function addCodeRow(code) {
  const c = code || { code: '', code_system: 'icd-10', description: '' };
  const row = document.createElement('div');
  row.className = 'row code-row';
  row.innerHTML = `
    <div style="flex:0 0 120px"><label>Code</label><input type="text" class="code-value" placeholder="e.g. R73.9" /></div>
    <div style="flex:0 0 110px"><label>System</label><select class="code-system"><option value="icd-10">icd-10</option><option value="cpt">cpt</option></select></div>
    <div><label>Description</label><input type="text" class="code-desc" placeholder="e.g. Hyperglycemia, unspecified" /></div>
    <div style="flex:0 0 auto; align-self:flex-end"><button type="button" class="ghost" title="Remove this code" style="margin:0">×</button></div>`;
  row.querySelector('.code-value').value = c.code;
  row.querySelector('.code-system').value = c.code_system;
  row.querySelector('.code-desc').value = c.description;
  row.querySelector('button').onclick = () => row.remove();
  document.getElementById('codeRows').appendChild(row);
}

function renderCodeRows(codes) {
  document.getElementById('codeRows').innerHTML = '';
  (codes && codes.length ? codes : [null]).forEach(addCodeRow);
}

function readCodeRows() {
  return [...document.querySelectorAll('#codeRows .code-row')].map(r => ({
    code: r.querySelector('.code-value').value.trim(),
    code_system: r.querySelector('.code-system').value,
    description: r.querySelector('.code-desc').value.trim(),
  })).filter(c => c.code || c.description);
}

// ---------- care gap suggestions (accept or dismiss each) ----------

function renderGapSuggestions(items) {
  const wrap = document.getElementById('gapSuggestions');
  wrap.innerHTML = '';
  (items || []).forEach(addGapSuggestion);
}

function addGapSuggestion(item) {
  const wrap = document.getElementById('gapSuggestions');
  const row = document.createElement('div');
  row.className = 'gap-row ai-suggestion';
  row.dataset.itemId = item.item_id;
  row.dataset.confidence = item.fields.confidence;
  row.dataset.reason = item.fields.confidence_reason || '';
  const low = item.fields.confidence < 0.7;
  row.innerHTML = `
    <div class="meta"><span class="src-tag">AI suggested</span><span>confidence ${Number(item.fields.confidence).toFixed(2)}${low ? ' — LOW' : ''}</span></div>
    <textarea class="gap-suggestion-text" style="margin-top:8px; min-height:44px"></textarea>
    ${low ? `<p class="small" style="margin:4px 0 0">AI's reason: ${escapeHtml(item.fields.confidence_reason || '')}</p>` : ''}
    <button type="button" style="margin-top:8px">Accept</button>
    <button type="button" class="secondary" style="margin-top:8px">Dismiss</button>
    <div class="result" style="display:none"></div>`;
  row.querySelector('textarea').value = item.fields.description;
  const [acceptBtn, dismissBtn] = row.querySelectorAll('button');
  acceptBtn.onclick = () => acceptGapSuggestion(row);
  dismissBtn.onclick = () => row.remove();
  wrap.appendChild(row);
}

function readGapSuggestions() {
  return [...document.querySelectorAll('#gapSuggestions .ai-suggestion')].map(r => ({
    item_id: r.dataset.itemId,
    fields: {
      description: r.querySelector('textarea').value,
      confidence: Number(r.dataset.confidence),
      confidence_reason: r.dataset.reason || null,
    },
  }));
}

async function acceptGapSuggestion(row) {
  const description = row.querySelector('textarea').value.trim();
  const resultEl = row.querySelector('.result');
  if (!description) {
    resultEl.style.display = 'block'; resultEl.className = 'result error';
    resultEl.textContent = 'The gap description is empty. Type one, or Dismiss the suggestion.';
    return;
  }
  const gap = { description, confidence: Number(row.dataset.confidence), suggestion_item_id: row.dataset.itemId };
  if (row.dataset.reason) gap.confidence_reason = row.dataset.reason;
  try {
    const data = await callTool('identify_care_gaps', { note_id: state.noteId, gaps: [gap] });
    // A list result comes back from the bridge wrapped as { result: [...] }.
    const [saved] = Array.isArray(data) ? data : (data.result || []);
    row.remove();
    addGapRow(saved);
    showResult('gapsResult', true, reviewSummary(saved.review));
    loadSourceLabels();
  } catch (e) {
    resultEl.style.display = 'block'; resultEl.className = 'result error';
    resultEl.textContent = e.message;
  }
}

// ---------- test orders (several at once) ----------

function addOrderInputRow(order) {
  const o = order || { item_id: '', test_name: '', reason: '' };
  const row = document.createElement('div');
  row.className = 'row order-input-row';
  row.dataset.itemId = o.item_id || '';
  row.innerHTML = `
    <div style="flex:0 0 auto; align-self:flex-end; padding-bottom:10px"><input type="checkbox" class="order-include" checked aria-label="Include this test" /></div>
    <div><label>Test name <span class="src-tag">${o.item_id ? 'AI suggested' : 'Added by you'}</span></label><input type="text" class="order-name" placeholder="e.g. Hemoglobin A1c" /></div>
    <div><label>Reason</label><input type="text" class="order-reason" placeholder="e.g. Confirm suspected diabetes" /></div>
    <div style="flex:0 0 auto; align-self:flex-end"><button type="button" class="ghost" title="Remove this test" style="margin:0">×</button></div>`;
  row.querySelector('.order-name').value = o.test_name;
  row.querySelector('.order-reason').value = o.reason;
  row.querySelector('button').onclick = () => row.remove();
  document.getElementById('orderRows').appendChild(row);
}

function renderOrderRows(rows) {
  document.getElementById('orderRows').innerHTML = '';
  (rows && rows.length ? rows : [null]).forEach(addOrderInputRow);
}

function readOrderRows() {
  return [...document.querySelectorAll('#orderRows .order-input-row')].map(r => ({
    item_id: r.dataset.itemId,
    test_name: r.querySelector('.order-name').value,
    reason: r.querySelector('.order-reason').value,
  }));
}

async function orderSelectedTests() {
  if (!state.noteId) {
    showResult('orderResult', false, 'Generate an encounter note (step 2) before ordering tests.');
    return;
  }
  const rows = [...document.querySelectorAll('#orderRows .order-input-row')]
    .filter(r => r.querySelector('.order-include').checked);
  if (!rows.length) {
    showResult('orderResult', false, 'Tick at least one test to order.');
    return;
  }
  const lines = [];
  for (const r of rows) {
    const testName = r.querySelector('.order-name').value.trim();
    const reason = r.querySelector('.order-reason').value.trim();
    if (!testName || !reason) {
      lines.push(`✗ ${testName || '(no test name)'}: needs both a test name and a reason.`);
      continue;
    }
    const args = { note_id: state.noteId, test_name: testName, reason };
    if (r.dataset.itemId) args.suggestion_item_id = r.dataset.itemId;
    try {
      const order = await callTool('order_test', args);
      addOrderRow(order);
      r.remove();
      lines.push(`✓ ${order.test_name}: ${reviewSummary(order.review)}`);
    } catch (e) {
      lines.push(`✗ ${testName}: ${e.message}`);
    }
  }
  if (!document.querySelector('#orderRows .order-input-row')) addOrderInputRow();
  showResult('orderResult', !lines.some(l => l.startsWith('✗')), lines.join('\n'));
}

// ---------- the AI-first flow ----------

async function applyAiDraft(data) {
  const key = activeSessionKey;
  const stillHere = () => activeSessionKey === key;
  const status = document.getElementById('draftStatus');

  // 1. Record the AI's note as the draft; the clinician edits it in the review box.
  const previousNote = state.noteId;
  await generateNote('ai');
  if (!stillHere() || !state.noteId || state.noteId === previousNote) return;
  const noteId = state.noteId;

  // 2. Codes: record the AI's set now, so approval can be compared against it.
  renderCodeRows(data.codes);
  if (data.codes && data.codes.length) {
    document.getElementById('codeConfidence').value = data.codes_confidence;
    document.getElementById('codeConfidenceReason').value = data.codes_confidence_reason || '';
    await suggestCodes('ai');
    if (!stillHere()) return;
  }

  // 3. Care gaps, tests and referral: recorded as suggestions, accepted one by one.
  const args = { note_id: noteId, care_gaps: data.care_gaps || [], test_orders: data.test_orders || [] };
  if (data.referral) args.referral = data.referral;
  let recorded;
  try {
    recorded = await callTool('record_ai_suggestions', args);
  } catch (e) {
    status.textContent = `Note and codes drafted, but the other suggestions could not be recorded (${e.message}). Enter care gaps and tests yourself.`;
    return;
  }
  if (!stillHere()) return;
  const items = recorded.items || [];
  renderGapSuggestions(items.filter(i => i.kind === 'care_gap'));
  const orders = items.filter(i => i.kind === 'test_order')
    .map(i => ({ item_id: i.item_id, test_name: i.fields.test_name, reason: i.fields.reason }));
  renderOrderRows(orders);
  const referral = items.find(i => i.kind === 'referral');
  if (referral) {
    document.getElementById('referralSpecialty').value = referral.fields.specialty;
    document.getElementById('referralReason').value = referral.fields.reason;
  }

  const count = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  status.textContent = `AI drafted the note (confidence ${Number(data.confidence).toFixed(2)}${data.low_confidence ? ', LOW' : ''}), `
    + `${count((data.codes || []).length, 'code')}, ${count(args.care_gaps.length, 'care gap')}, `
    + `${count(args.test_orders.length, 'test')}${referral ? ' and a referral' : ''}. `
    + 'Review each one: edit anything that is wrong, then approve. Edits are recorded.';
}

// First paint: give the code and test cards one empty row each if nothing
// (a deep-linked session, say) has filled them yet.
if (!document.querySelector('#codeRows .code-row')) renderCodeRows([]);
if (!document.querySelector('#orderRows .order-input-row')) renderOrderRows([]);

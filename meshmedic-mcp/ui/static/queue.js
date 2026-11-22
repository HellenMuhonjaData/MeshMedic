// Phase 2: End consultation, the review queue, reopening an encounter from the server, autosave.
//
// - End consultation hands the recording (and any transcript so far) to
//   background processing and clears the form, so the clinician can see the
//   next patient straight away.
// - The review queue lists this clinician's encounters with where each one
//   is: processing, needs attention (Retry), ready for review, awaiting
//   signature, signed. It refreshes every 10 seconds, quietly.
// - Opening an encounter rebuilds the workspace from the server, never from
//   browser memory, so work survives a reload or a different computer.
// - Note edits save automatically 2 seconds after typing stops.
//
// Uses globals from index.html, ai_review.js and signoff.js.

const recordingBySession = new Map(); // session key -> latest recorded audio Blob
let autosaveTimer = null;
let autosaveNoteId = null;

function rememberRecording(blob) {
  if (activeSessionKey) recordingBySession.set(activeSessionKey, blob);
}

const QUEUE_LABELS = {
  capturing: 'Recording', processing: 'Processing…', needs_attention: 'Needs attention',
  ready_for_review: 'Ready for review', awaiting_signature: 'Awaiting signature', signed: 'Signed', rejected: 'Rejected',
};

function ensureQueueCard() {
  if (document.getElementById('reviewQueue')) return;
  const main = document.querySelector('.layout > main');
  const card = document.createElement('div');
  card.className = 'card';
  card.id = 'reviewQueue';
  card.innerHTML = `
    <h2>Review queue <span class="badge draft" id="queueCount">0</span></h2>
    <p class="hint">Encounters you ended are processed in the background; review and sign them between patients or at the end of clinic. Attention items first, the full note always visible, one signature per note.</p>
    <div id="queueList"><p class="small" style="margin:0">Nothing yet. Select a patient, record or type the consultation, then click End consultation.</p></div>`;
  main.insertBefore(card, main.firstChild);
  const draftBtn = document.querySelector('button.ai');
  if (draftBtn && !document.getElementById('endConsultBtn')) {
    const end = document.createElement('button');
    end.id = 'endConsultBtn';
    end.type = 'button';
    end.textContent = 'End consultation →';
    end.title = 'Process this consultation in the background and move to the next patient';
    end.onclick = endConsultation;
    draftBtn.insertAdjacentElement('afterend', end);
  }
  const edited = document.getElementById('editedNoteText');
  if (edited) edited.addEventListener('input', scheduleAutosave);
}

async function refreshQueue() {
  const list = document.getElementById('queueList');
  if (!list) return;
  const res = await fetch('/api/queue');
  if (!res.ok) return;
  const items = (await res.json()).items || [];
  const open = items.filter(i => i.queue_status !== 'signed' && i.queue_status !== 'rejected');
  document.getElementById('queueCount').textContent = open.length;
  if (!items.length) return;
  list.innerHTML = '';
  items.forEach(item => {
    const row = document.createElement('div');
    row.className = 'gap-row queue-row';
    const started = new Date(item.started_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    row.innerHTML = `
      <div class="meta"><span><strong></strong> <span class="small"></span></span>
        <span class="badge q-${item.queue_status}">${QUEUE_LABELS[item.queue_status] || item.queue_status}</span></div>
      <div class="small q-detail"></div>`;
    row.querySelector('strong').textContent = item.patient_name;
    row.querySelector('.meta .small').textContent = `MRN ${item.mrn || '—'} · started ${started}`;
    const detail = row.querySelector('.q-detail');
    if (item.note_flagged) detail.textContent = 'Low AI confidence — check the reason before signing.';
    if (item.queue_status === 'needs_attention') {
      detail.textContent = `Processing failed after ${item.attempts} attempt(s) (${item.error_class || 'error'}). The recording is kept.`;
      row.appendChild(button('Retry', 'secondary', () => retryEncounter(item.encounter_id)));
    } else if (['ready_for_review', 'awaiting_signature', 'signed'].includes(item.queue_status)) {
      row.appendChild(button(item.queue_status === 'signed' ? 'View' : 'Open for review', '', () => openEncounter(item.encounter_id)));
    }
    list.appendChild(row);
  });
}

function button(label, cls, onclick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = label;
  if (cls) b.className = cls;
  b.onclick = onclick;
  return b;
}

async function endConsultation() {
  const status = document.getElementById('draftStatus');
  if (!state.patientId || !activeSessionKey) { status.textContent = 'Select the patient first (step 1).'; return; }
  const session = sessions.get(activeSessionKey);
  const transcript = document.getElementById('transcript').value.trim();
  const audio = recordingBySession.get(activeSessionKey);
  if (!transcript && !audio) { status.textContent = 'Record or type the consultation before ending it.'; return; }
  status.textContent = 'Handing this consultation to background processing…';
  session.encounterKey = session.encounterKey || crypto.randomUUID();
  const started = await postJson('/api/encounters', {
    ehr_system: state.ehrSystem, patient_id: state.patientId, idempotency_key: session.encounterKey,
  });
  if (!started.ok) { status.textContent = started.data.error || 'Could not start the encounter.'; return; }
  const enc = started.data.encounter_id;
  if (audio) {
    const fd = new FormData();
    fd.append('audio', audio, 'recording.webm');
    const up = await fetch(`/api/encounters/${enc}/audio`, { method: 'POST', body: fd });
    if (!up.ok) { status.textContent = 'The recording could not be saved; nothing was lost — try again.'; return; }
  }
  if (transcript) {
    const t = await postJson(`/api/encounters/${enc}/transcript`, { text: transcript });
    if (!t.ok) { status.textContent = t.data.error || 'The transcript could not be saved.'; return; }
  }
  const ended = await postJson(`/api/encounters/${enc}/end`, {});
  if (!ended.ok) { status.textContent = ended.data.error || 'Could not end the consultation.'; return; }
  session.encounterKey = null;  // the next consultation for this patient is a new encounter
  recordingBySession.delete(activeSessionKey);
  document.getElementById('transcript').value = '';
  status.textContent = 'Consultation ended. It is being processed in the background — you can see your next patient.';
  refreshQueue();
}

async function retryEncounter(encounterId) {
  await postJson(`/api/encounters/${encounterId}/retry`, {});
  refreshQueue();
}

async function postJson(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return { ok: res.ok, data: await res.json().catch(() => ({})) };
}

async function openEncounter(encounterId) {
  const res = await fetch(`/api/encounters/${encounterId}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { alertResult(data.error || 'Could not open the encounter.'); return; }
  const e = data.encounter;
  const [first, ...rest] = (e.patient_name || '').split(' ');
  selectPatient({ patient_id: e.patient_id, first_name: first, last_name: rest.join(' '), mrn: e.mrn }, e.ehr_system);
  applyBundle(data);
  document.querySelector('#editedNoteText')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function alertResult(message) {
  const list = document.getElementById('queueList');
  const p = document.createElement('p');
  p.className = 'result error';
  p.textContent = message;
  list.prepend(p);
}

function applyBundle(data) {
  const note = data.note;
  if (!note) return;
  state.noteId = note.note_id;
  document.getElementById('activeNote').textContent = note.note_id;
  document.getElementById('activeNoteMini').textContent = note.note_id;
  document.getElementById('noteText').value = note.original_text;
  document.getElementById('confidence').value = note.confidence ?? '';
  document.getElementById('confidenceReason').value = note.confidence_reason || '';
  document.getElementById('editedNoteText').value = note.text;
  document.getElementById('noteDecision').style.display = 'block';
  const editable = note.status === 'ai_suggested' || note.status === 'in_review';
  document.getElementById('editedNoteText').readOnly = !editable;
  autosaveNoteId = editable ? note.note_id : null;
  showResult('noteResult', !note.flagged, note.flagged
    ? `Low AI confidence (${Number(note.confidence).toFixed(2)}): ${note.confidence_reason}. Check this note carefully.`
    : `AI draft (confidence ${Number(note.confidence).toFixed(2)}, self-assessed by the model, not validated). Review it in full before marking reviewed.`);

  if (data.code_set) {
    renderCodeRows(data.code_set.codes);
    state.suggestionId = data.code_set.suggestion_id;
    document.getElementById('codesDecision').style.display = data.code_set.status === 'ai_suggested' ? 'block' : 'none';
  }
  const pending = data.suggestions.filter(s => s.status === 'ai_suggested');
  renderGapSuggestions(pending.filter(s => s.kind === 'care_gap'));
  renderOrderRows(pending.filter(s => s.kind === 'test_order')
    .map(s => ({ item_id: s.item_id, test_name: s.fields.test_name, reason: s.fields.reason })));
  const referral = data.suggestions.find(s => s.kind === 'referral');
  if (referral) {
    document.getElementById('referralSpecialty').value = referral.fields.specialty;
    document.getElementById('referralReason').value = referral.fields.reason;
  }
  document.getElementById('gapsList').innerHTML = '';
  document.getElementById('ordersList').innerHTML = '';
  const session = sessions.get(activeSessionKey);
  if (session) { session.gaps = []; session.orders = []; }
  data.care_gaps.forEach(g => addGapRow(g));
  data.orders.forEach(o => addOrderRow(o));

  if (note.status === 'clinician_reviewed') {
    onNoteReviewed({ status: 'approved', note_id: note.note_id, note_text: note.text });
  } else if (note.status === 'signed') {
    signStateByNote.set(note.note_id, { reviewedText: note.text, signed: { signed_by_name: note.signed_by, signed_at: note.signed_at } });
    renderSignPanel();
  } else {
    renderSignPanel();
  }
  loadSourceLabels();
}

function scheduleAutosave() {
  if (!autosaveNoteId || autosaveNoteId !== state.noteId) return;
  clearTimeout(autosaveTimer);
  const status = document.getElementById('draftStatus');
  status.textContent = 'Unsaved changes…';
  autosaveTimer = setTimeout(async () => {
    const text = document.getElementById('editedNoteText').value.trim();
    if (!text || autosaveNoteId !== state.noteId) return;
    const saved = await postJson('/api/tools/edit_encounter_note', { note_id: autosaveNoteId, edited_note_text: text });
    status.textContent = saved.ok ? `Saved ${new Date().toLocaleTimeString()}` : (saved.data.error || 'Could not save — your text is still here.');
  }, 2000);
}

// After Mark reviewed the note is locked for editing; stop autosaving it.
const _originalOnNoteReviewed = onNoteReviewed;
onNoteReviewed = function (reviewed) {  // eslint-disable-line no-global-assign
  if (reviewed && reviewed.note_id === autosaveNoteId) autosaveNoteId = null;
  _originalOnNoteReviewed(reviewed);
};

window.addEventListener('meshmedic:signed-in', () => {
  ensureQueueCard();
  refreshQueue();
  setInterval(() => { if (!document.hidden) refreshQueue(); }, 10000);
});

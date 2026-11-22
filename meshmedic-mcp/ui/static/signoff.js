// Note sign-off in the workspace (Phase 1): Mark reviewed -> Sign note.
//
// Signing sends the SHA-256 of the exact reviewed text; the server refuses
// the signature if the text it has on record is different. The PIN is asked
// for only when the 15-minute signing window is closed. There is no way to
// sign more than the one note on screen.
//
// Uses globals from index.html: state, escapeHtml.

const signStateByNote = new Map(); // note_id -> { reviewedText, signed }

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

function onNoteReviewed(reviewed) {
  if (!reviewed || reviewed.status !== 'approved') return;
  signStateByNote.set(reviewed.note_id, { reviewedText: reviewed.note_text, signed: null });
  renderSignPanel();
}

function renderSignPanel() {
  const panel = document.getElementById('signPanel');
  if (!panel) return;
  const info = state.noteId ? signStateByNote.get(state.noteId) : null;
  if (!info) { panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  const button = document.getElementById('signNoteBtn');
  const result = document.getElementById('signResult');
  if (info.signed) {
    button.style.display = 'none';
    result.className = 'result';
    result.style.display = 'block';
    result.textContent = `🔒 Signed by ${info.signed.signed_by_name} on ${new Date(info.signed.signed_at).toLocaleString()}. The note is locked.`;
  } else {
    button.style.display = '';
    result.style.display = 'none';
  }
}

async function signActiveNote() {
  const noteId = state.noteId;
  const info = noteId ? signStateByNote.get(noteId) : null;
  const result = document.getElementById('signResult');
  if (!info) return;
  const contentHash = await sha256Hex(info.reviewedText);
  const attempt = () => fetch(`/api/notes/${encodeURIComponent(noteId)}/sign`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content_hash: contentHash }),
  });
  let res = await attempt();
  if (res.status === 428) {
    const ok = await window.meshAuth.requestPin();
    if (!ok) return;
    res = await attempt();
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    result.style.display = 'block';
    result.className = 'result error';
    result.textContent = data.error || 'The note could not be signed.';
    return;
  }
  info.signed = data;
  if (typeof logEntry === 'function') logEntry('sign_encounter_note', true, data);
  if (state.noteId === noteId) renderSignPanel();
}

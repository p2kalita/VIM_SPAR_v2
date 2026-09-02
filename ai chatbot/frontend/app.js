/* ──────────────────────────────────────────────────────────
   app.js — RAG Chat UI Frontend Logic
   Matches the new rag-chat-ui.jsx design:
   - Welcome screen with animated orb & example prompts
   - SSE streaming chat
   - File upload (paperclip)
   - Writing style & citation controls
   - Settings modal
   - Toast notifications
   ────────────────────────────────────────────────────────── */

const API_BASE = 'http://127.0.0.1:5000';

// ── State ──────────────────────────────────────────────────────
let messages    = [];       // { role, content }[]
let isStreaming = false;
let activeDocId = null;

// ── DOM refs ───────────────────────────────────────────────────
const topbar          = document.getElementById('topbar');
const newThreadBtn    = document.getElementById('new-thread-btn');
const welcomeSection  = document.getElementById('welcome');
const messagesDiv     = document.getElementById('messages');
const messagesInner   = document.getElementById('messages-inner');
const chatForm        = document.getElementById('chat-form');
const chatInput       = document.getElementById('chat-input');
const sendBtn         = document.getElementById('send-btn');
const attachTrigger   = document.getElementById('attach-trigger-btn');
const attachBtn       = document.getElementById('attach-btn');
const fileInput       = document.getElementById('file-input');
const writingStyle    = document.getElementById('writing-style');
const citationToggle  = document.getElementById('citation-toggle');
const settingsBtnHdr  = document.getElementById('settings-btn');
const settingsBtnCtrl = document.getElementById('settings-btn-ctrl');
const modalOverlay    = document.getElementById('modal-overlay');
const modalClose      = document.getElementById('modal-close');
const saveSettings    = document.getElementById('save-settings');
const apiUrlInput     = document.getElementById('api-url-input');
const endpointInput   = document.getElementById('endpoint-input');
const modelInput      = document.getElementById('model-input');
const toastContainer  = document.getElementById('toast-container');
const uploadProgress  = document.getElementById('upload-progress');

const modelNameDisplay = document.getElementById('model-name-display');
const modelLearnMore   = document.getElementById('model-learn-more');
const vllmBadge        = document.getElementById('vllm-status-badge');

// ── Init ───────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadSettings();
  setGreeting();
  setupChat();
  setupUpload();
  setupModal();
  setupExampleCards();
  setupNewThread();
  // Probe backend on load to show active model in header
  probeBackend();
});

// ── Dynamic Greeting (Indian Standard Time - IST) ────────────
function setGreeting() {
  try {
    const now = new Date();
    const istFormatter = new Intl.DateTimeFormat('en-US', {
      timeZone: 'Asia/Kolkata',
      hour: 'numeric',
      hourCycle: 'h23'
    });
    const parts = istFormatter.formatToParts(now);
    const hourPart = parts.find(p => p.type === 'hour');
    const h = hourPart ? parseInt(hourPart.value, 10) : now.getHours();

    let period = 'morning';
    if (h >= 12 && h < 17) {
      period = 'afternoon';
    } else if (h >= 17 || h < 4) {
      period = 'evening';
    } else {
      period = 'morning';
    }

    const el = document.getElementById('welcome-heading');
    if (el) el.textContent = `Good ${period}, Partha`;
  } catch (_) {
    const h = new Date().getHours();
    const period = (h >= 12 && h < 17) ? 'afternoon' : (h >= 17 || h < 4) ? 'evening' : 'morning';
    const el = document.getElementById('welcome-heading');
    if (el) el.textContent = `Good ${period}, Partha`;
  }
}

// Keep greeting dynamically updated in real-time
setInterval(setGreeting, 30000);

// ── Settings ───────────────────────────────────────────────────
function loadSettings() {
  if (apiUrlInput)   apiUrlInput.value   = localStorage.getItem('api_url')      || API_BASE;
  if (endpointInput) endpointInput.value = localStorage.getItem('llm_endpoint') || '';
  if (modelInput)    modelInput.value    = localStorage.getItem('model_name')   || 'Qwen/Qwen2.5-3B-Instruct';
}

function getApiBase() {
  return localStorage.getItem('api_url') || API_BASE;
}

saveSettings.addEventListener('click', () => {
  const url      = apiUrlInput.value.trim();
  const endpoint = endpointInput.value.trim();
  const model    = modelInput.value.trim();
  if (url)      localStorage.setItem('api_url', url);
  if (endpoint) localStorage.setItem('llm_endpoint', endpoint);
  if (model)    localStorage.setItem('model_name', model);
  closeModal();
  showToast('Settings saved', 'success');
});

// ── Modal ──────────────────────────────────────────────────────
function setupModal() {
  [settingsBtnHdr, settingsBtnCtrl].forEach(btn => {
    btn?.addEventListener('click', () => { openModal(); probeVllmStatus(); });
  });
  modalClose.addEventListener('click', closeModal);
  modalOverlay.addEventListener('click', e => {
    if (e.target === modalOverlay) closeModal();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
  });
}
function openModal()  { modalOverlay.classList.add('open'); }
function closeModal() { modalOverlay.classList.remove('open'); }

// ── Backend probe (llm-info) ────────────────────────────────────
async function probeBackend() {
  try {
    const res  = await fetch(`${getApiBase()}/llm-info`);
    if (!res.ok) return;
    const info = await res.json();
    // Update the header banner model name
    if (info.model && modelNameDisplay) {
      // Strip org prefix for display: "Qwen/Qwen2.5-3B-Instruct" → "Qwen2.5-3B-Instruct"
      const shortName = info.model.includes('/') ? info.model.split('/').pop() : info.model;
      modelNameDisplay.textContent = shortName;
    }
  } catch (_) { /* backend not yet running, ignore */ }
}

async function probeVllmStatus() {
  if (!vllmBadge) return;
  vllmBadge.className = 'vllm-badge vllm-badge--checking';
  vllmBadge.textContent = 'checking…';
  try {
    const res  = await fetch(`${getApiBase()}/llm-info`, { signal: AbortSignal.timeout(5000) });
    if (!res.ok) throw new Error('non-ok');
    const info = await res.json();
    if (info.backend === 'vllm') {
      vllmBadge.className = 'vllm-badge vllm-badge--online';
      vllmBadge.textContent = `online · ${info.model}`;
      // Pre-fill endpoint & model inputs
      if (endpointInput && info.base_url) endpointInput.value = info.base_url;
      if (modelInput    && info.model)    modelInput.value    = info.model;
    } else {
      vllmBadge.className = 'vllm-badge vllm-badge--offline';
      vllmBadge.textContent = `gemini fallback`;
    }
  } catch (_) {
    if (vllmBadge) {
      vllmBadge.className = 'vllm-badge vllm-badge--offline';
      vllmBadge.textContent = 'offline';
    }
  }
}

// ── New Thread ─────────────────────────────────────────────────
function setupNewThread() {
  newThreadBtn?.addEventListener('click', () => {
    messages = [];
    activeDocId = null;
    messagesInner.innerHTML = '';
    showWelcome();
  });
}

// ── Welcome / Chat visibility ──────────────────────────────────
function showWelcome() {
  welcomeSection.style.display = '';
  messagesDiv.classList.remove('visible');
}
function showChat() {
  welcomeSection.style.display = 'none';
  messagesDiv.classList.add('visible');
}

// ── Example prompt cards ───────────────────────────────────────
function setupExampleCards() {
  document.querySelectorAll('.example-card').forEach(card => {
    card.addEventListener('click', () => {
      const prompt = card.dataset.prompt;
      if (prompt) {
        chatInput.value = prompt;
        autoResize();
        chatInput.focus();
        // Auto-send
        handleSubmit();
      }
    });
  });
}

// ── Chat ───────────────────────────────────────────────────────
function setupChat() {
  chatForm.addEventListener('submit', e => {
    e.preventDefault();
    handleSubmit();
  });

  chatInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  });

  chatInput.addEventListener('input', autoResize);
}

function autoResize() {
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + 'px';
}

async function handleSubmit() {
  const text = chatInput.value.trim();
  if (!text || isStreaming) return;

  // Show chat area, hide welcome
  showChat();

  messages.push({ role: 'user', content: text });
  appendUserBubble(text);

  chatInput.value = '';
  chatInput.style.height = 'auto';
  setStreaming(true);

  const assistantBubble = appendThinkingBubble();

  try {
    const body = {
      messages,
      filter_doc_id:  activeDocId  || null,
      writing_style:  writingStyle?.value  || 'default',
      citations:      citationToggle?.checked || false,
      model:          modelInput?.value || localStorage.getItem('model_name') || 'gemini-2.0-flash',
    };

    const res = await fetch(`${getApiBase()}/chat`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    // Stream SSE
    let fullText = '';
    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer    = '';
    let ssesDone  = false;

    replaceThinkingWithCursor(assistantBubble);

    while (!ssesDone) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const payload        = line.slice(6);        // raw — preserves leading spaces in tokens
        const payloadTrimmed = payload.trim();        // for control-message checks only
        if (payloadTrimmed === '[DONE]')              { ssesDone = true; break; }
        if (payloadTrimmed.startsWith('[ERROR]'))     { throw new Error(payloadTrimmed.slice(8)); }
        if (!payloadTrimmed)                          continue;  // skip blank lines
        const token = payload.replace(/\\n/g, '\n'); // unescape newlines, keep spaces
        fullText += token;
        updateStreamBubble(assistantBubble, fullText);
      }
    }

    finaliseStreamBubble(assistantBubble, fullText);
    messages.push({ role: 'assistant', content: fullText });

  } catch (err) {
    console.error('Chat error:', err);
    const errMsg = `⚠️ ${err.message}`;
    updateStreamBubble(assistantBubble, errMsg);
    finaliseStreamBubble(assistantBubble, errMsg);
    showToast(err.message, 'error');
  } finally {
    setStreaming(false);
    scrollToBottom();
  }
}

// ── Bubble rendering ────────────────────────────────────────────
function appendUserBubble(text) {
  const div = document.createElement('div');
  div.className = 'message user';
  div.innerHTML = `
    <div class="msg-bubble">${escHtml(text)}</div>
  `;
  messagesInner.appendChild(div);
  scrollToBottom();
}

function appendThinkingBubble() {
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML = `
    <div class="msg-bubble">
      <div class="thinking-dots">
        <span></span><span></span><span></span>
      </div>
    </div>
  `;
  messagesInner.appendChild(div);
  scrollToBottom();
  return div;
}

function replaceThinkingWithCursor(div) {
  const bubble = div.querySelector('.msg-bubble');
  bubble.innerHTML = '<span class="typing-cursor"></span>';
}

function updateStreamBubble(div, text) {
  const bubble = div.querySelector('.msg-bubble');
  bubble.innerHTML = renderMarkdown(text) + '<span class="typing-cursor"></span>';
  scrollToBottom();
}

function finaliseStreamBubble(div, text) {
  const bubble = div.querySelector('.msg-bubble');
  bubble.innerHTML = renderMarkdown(text);
}

// ── Markdown ────────────────────────────────────────────────────
function renderMarkdown(text) {
  let html = escHtml(text);
  html = html.replace(/```[\w]*\n([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/^### (.+)$/gm, '<strong>$1</strong>');
  html = html.replace(/^## (.+)$/gm, '<strong style="font-size:1.05em">$1</strong>');
  html = html.replace(/^[•\-\*] (.+)$/gm, '• $1');
  html = html.replace(/\n/g, '<br>');
  return html;
}

// ── Upload ──────────────────────────────────────────────────────
function setupUpload() {
  // Both the icon button in the input shell and the ctrl-btn trigger the file picker
  [attachTrigger, attachBtn].forEach(btn => {
    btn?.addEventListener('click', () => fileInput.click());
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files.length) handleFiles(Array.from(fileInput.files));
    fileInput.value = '';
  });
}

async function handleFiles(files) {
  const allowed = ['.pdf','.jpg','.jpeg','.png','.tiff','.tif','.bmp','.webp'];
  const valid = files.filter(f => allowed.some(ext => f.name.toLowerCase().endsWith(ext)));

  if (!valid.length) {
    showToast('No supported files selected (PDF, JPG, PNG, TIFF…)', 'error');
    return;
  }

  showToast(`Uploading ${valid.length} file(s)…`, 'info');

  const formData = new FormData();
  valid.forEach(f => formData.append('files', f));

  try {
    const res = await fetch(`${getApiBase()}/upload`, {
      method: 'POST',
      body:   formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Upload failed');

    const ingested = data.ingested || [];
    const errors   = data.errors   || [];
    if (ingested.length) showToast(`✅ Ingested ${ingested.length} file(s)`, 'success');
    errors.forEach(e => showToast(`⚠️ ${e.filename}: ${e.error}`, 'error'));
  } catch (err) {
    showToast(`Upload error: ${err.message}`, 'error');
    console.error(err);
  }
}

// ── Utilities ────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function scrollToBottom() {
  const mainArea = document.getElementById('main-area');
  if (mainArea) mainArea.scrollTop = mainArea.scrollHeight;
}

function setStreaming(val) {
  isStreaming = val;
  sendBtn.disabled   = val;
  chatInput.disabled = val;
  chatInput.style.opacity = val ? '0.6' : '1';
}

// ── Toast ────────────────────────────────────────────────────────
function showToast(msg, type = 'info') {
  const icons = { success: '✅', error: '❌', info: 'ℹ️' };
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span>${icons[type] || 'ℹ️'}</span><span>${escHtml(msg)}</span>`;
  toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.style.transition = 'opacity 0.3s';
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 350);
  }, 4000);
}

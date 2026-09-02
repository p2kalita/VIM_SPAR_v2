/* ──────────────────────────────────────────────────────────
   SPAR AI Invoice RAG Chatbot — Frontend Logic
   Integrated Single UI Chatbot (Vanilla JS + SSE Streaming)
   ────────────────────────────────────────────────────────── */

const API_BASE = ''; // Use same host by default

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
const docStatsPill    = document.getElementById('doc-stats-pill');

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
  setupNewThread();
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
    // Fallback if Intl is unavailable
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
  if (apiUrlInput)   apiUrlInput.value   = localStorage.getItem('api_url')      || '';
  if (endpointInput) endpointInput.value = localStorage.getItem('llm_endpoint') || '';
  if (modelInput)    modelInput.value    = localStorage.getItem('model_name')   || 'gemini-2.5-flash';
}

function getApiBase() {
  const custom = (localStorage.getItem('api_url') || '').trim();
  if (!custom) return API_BASE;
  return custom.replace(/\/+$/, '');
}

if (saveSettings) {
  saveSettings.addEventListener('click', () => {
    const url      = apiUrlInput ? apiUrlInput.value.trim() : '';
    const endpoint = endpointInput ? endpointInput.value.trim() : '';
    const model    = modelInput ? modelInput.value.trim() : '';
    localStorage.setItem('api_url', url);
    if (endpoint) localStorage.setItem('llm_endpoint', endpoint);
    if (model)    localStorage.setItem('model_name', model);
    closeModal();
    showToast('Settings saved successfully', 'success');
  });
}

// ── Modal ──────────────────────────────────────────────────────
function setupModal() {
  [settingsBtnHdr, settingsBtnCtrl].forEach(btn => {
    btn?.addEventListener('click', () => { openModal(); probeVllmStatus(); });
  });
  modalClose?.addEventListener('click', closeModal);
  modalOverlay?.addEventListener('click', e => {
    if (e.target === modalOverlay) closeModal();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
  });
}
function openModal()  { modalOverlay?.classList.add('open'); }
function closeModal() { modalOverlay?.classList.remove('open'); }

// ── Backend probe (llm-info & store stats) ─────────────────────
async function probeBackend() {
  try {
    let base = getApiBase();
    let res;
    try {
      res = await fetch(`${base}/llm-info`);
    } catch (_) {
      if (base !== '') {
        base = '';
        localStorage.removeItem('api_url');
        res = await fetch(`/llm-info`);
      } else {
        return;
      }
    }
    if (!res || !res.ok) return;
    const info = await res.json();
    
    if (info.model && modelNameDisplay) {
      const shortName = info.model.includes('/') ? info.model.split('/').pop() : info.model;
      modelNameDisplay.textContent = shortName;
    }

    if (info.stats && docStatsPill) {
      const count = info.stats.invoice_count || 0;
      const chunks = info.stats.chunk_count || 0;
      docStatsPill.innerHTML = `
        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" width="16" height="16">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path>
        </svg>
        <span><b>${count}</b> Invoice Document(s) Indexed (${chunks} Vector Chunks)</span>
      `;
    }
  } catch (_) { /* backend probe fallback */ }
}

async function probeVllmStatus() {
  if (!vllmBadge) return;
  vllmBadge.className = 'vllm-badge vllm-badge--checking';
  vllmBadge.textContent = 'checking…';
  try {
    const base = getApiBase();
    let res;
    try {
      res = await fetch(`${base}/llm-info`, { signal: AbortSignal.timeout(4000) });
    } catch (_) {
      res = await fetch(`/llm-info`, { signal: AbortSignal.timeout(4000) });
    }
    if (!res.ok) throw new Error('non-ok');
    const info = await res.json();
    if (info.backend === 'vllm') {
      vllmBadge.className = 'vllm-badge vllm-badge--online';
      vllmBadge.textContent = `online · ${info.model}`;
      if (endpointInput && info.base_url) endpointInput.value = info.base_url;
      if (modelInput    && info.model)    modelInput.value    = info.model;
    } else {
      vllmBadge.className = 'vllm-badge vllm-badge--online';
      vllmBadge.textContent = `Gemini Online (${info.model || 'Flash'})`;
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
    if (messagesInner) messagesInner.innerHTML = '';
    setGreeting();
    showWelcome();
  });
}

// ── Welcome / Chat visibility ──────────────────────────────────
function showWelcome() {
  if (welcomeSection) welcomeSection.style.display = '';
  if (messagesDiv) messagesDiv.classList.remove('visible');
}
function showChat() {
  if (welcomeSection) welcomeSection.style.display = 'none';
  if (messagesDiv) messagesDiv.classList.add('visible');
}

// ── Example prompt cards (reserved for future use) ─────────
function setupExampleCards() {
  document.querySelectorAll('.example-card').forEach(card => {
    card.addEventListener('click', () => {
      const prompt = card.dataset.prompt;
      if (prompt && chatInput) {
        chatInput.value = prompt;
        autoResize();
        chatInput.focus();
        handleSubmit();
      }
    });
  });
}

// ── Chat ───────────────────────────────────────────────────────
function setupChat() {
  if (chatForm) {
    chatForm.addEventListener('submit', e => {
      e.preventDefault();
      handleSubmit();
    });
  }

  if (chatInput) {
    chatInput.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSubmit();
      }
    });
    chatInput.addEventListener('input', autoResize);
  }
}

function autoResize() {
  if (!chatInput) return;
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + 'px';
}

async function handleSubmit() {
  if (!chatInput) return;
  const text = chatInput.value.trim();
  if (!text || isStreaming) return;

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
      model:          modelInput?.value || localStorage.getItem('model_name') || 'gemini-2.5-flash',
    };

    let base = getApiBase();
    let res;
    try {
      res = await fetch(`${base}/chat`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(body),
      });
    } catch (fetchErr) {
      if (base !== '') {
        console.warn(`Fetch to ${base}/chat failed, automatically recovering with same-origin /chat:`, fetchErr);
        localStorage.removeItem('api_url');
        res = await fetch(`/chat`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify(body),
        });
      } else {
        throw fetchErr;
      }
    }

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || err.error || `HTTP ${res.status}`);
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
      if (done) {
        if (buffer.trim()) {
          const remainingLines = buffer.split('\n');
          for (const line of remainingLines) {
            if (!line.startsWith('data: ')) continue;
            const payload = line.slice(6);
            const payloadTrimmed = payload.trim();
            if (payloadTrimmed === '[DONE]') { ssesDone = true; break; }
            if (payloadTrimmed.startsWith('[ERROR]')) { throw new Error(payloadTrimmed.slice(8)); }
            if (!payloadTrimmed) continue;
            const token = payload.replace(/\\n/g, '\n');
            fullText += token;
            updateStreamBubble(assistantBubble, fullText);
          }
        }
        break;
      }

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const payload        = line.slice(6);
        const payloadTrimmed = payload.trim();
        if (payloadTrimmed === '[DONE]')              { ssesDone = true; break; }
        if (payloadTrimmed.startsWith('[ERROR]'))     { throw new Error(payloadTrimmed.slice(8)); }
        if (!payloadTrimmed)                          continue;
        const token = payload.replace(/\\n/g, '\n');
        fullText += token;
        updateStreamBubble(assistantBubble, fullText);
      }
    }

    finaliseStreamBubble(assistantBubble, fullText);
    messages.push({ role: 'assistant', content: fullText });

  } catch (err) {
    console.error('Chat error:', err);
    let msg = err.message || 'Error communicating with assistant';
    if (msg.includes('Failed to fetch') || msg.includes('NetworkError')) {
      msg = 'Unable to reach backend server. Please verify the Flask app is running at http://127.0.0.1:5000.';
    }
    const errMsg = `⚠️ ${msg}`;
    updateStreamBubble(assistantBubble, errMsg);
    finaliseStreamBubble(assistantBubble, errMsg);
    showToast(msg, 'error');
  } finally {
    setStreaming(false);
    scrollToBottom();
  }
}

// ── Bubble rendering ────────────────────────────────────────────
function appendUserBubble(text) {
  const div = document.createElement('div');
  div.className = 'message user';
  div.innerHTML = `<div class="msg-bubble">${escHtml(text)}</div>`;
  messagesInner?.appendChild(div);
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
  messagesInner?.appendChild(div);
  scrollToBottom();
  return div;
}

function replaceThinkingWithCursor(div) {
  const bubble = div.querySelector('.msg-bubble');
  if (bubble) bubble.innerHTML = '<span class="typing-cursor"></span>';
}

function updateStreamBubble(div, text) {
  const bubble = div.querySelector('.msg-bubble');
  if (bubble) {
    bubble.innerHTML = renderMarkdown(text) + '<span class="typing-cursor"></span>';
    scrollToBottom();
  }
}

function finaliseStreamBubble(div, text) {
  const bubble = div.querySelector('.msg-bubble');
  if (bubble) bubble.innerHTML = renderMarkdown(text);
}

// ── Markdown ────────────────────────────────────────────────────
function renderMarkdown(text) {
  let html = escHtml(text);
  html = html.replace(/```[\w]*\n([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/^### (.+)$/gm, '<h4 style="margin:8px 0 4px;font-size:1rem;">$1</h4>');
  html = html.replace(/^## (.+)$/gm, '<h3 style="margin:10px 0 6px;font-size:1.1rem;">$1</h3>');
  html = html.replace(/^[•\-\*] (.+)$/gm, '• $1');
  html = html.replace(/\n/g, '<br>');
  return html;
}

// ── Multi-Invoice Upload ─────────────────────────────────────────
function setupUpload() {
  [attachTrigger, attachBtn].forEach(btn => {
    btn?.addEventListener('click', () => fileInput?.click());
  });

  fileInput?.addEventListener('change', () => {
    if (fileInput.files && fileInput.files.length) {
      handleFiles(Array.from(fileInput.files));
    }
    fileInput.value = '';
  });
}

async function handleFiles(files) {
  const allowed = ['.pdf', '.txt', '.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.webp'];
  const valid = files.filter(f => allowed.some(ext => f.name.toLowerCase().endsWith(ext)));

  if (!valid.length) {
    showToast('Please select supported invoice files (PDF, TXT, JPG, PNG...)', 'error');
    return;
  }

  showToast(`Uploading & indexing ${valid.length} invoice document(s)...`, 'info');

  const formData = new FormData();
  valid.forEach(f => formData.append('files', f));

  try {
    let base = getApiBase();
    let res;
    try {
      res = await fetch(`${base}/upload`, {
        method: 'POST',
        body:   formData,
      });
    } catch (fetchErr) {
      if (base !== '') {
        console.warn(`Upload to ${base}/upload failed, falling back to /upload:`, fetchErr);
        localStorage.removeItem('api_url');
        res = await fetch(`/upload`, {
          method: 'POST',
          body:   formData,
        });
      } else {
        throw fetchErr;
      }
    }

    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.error || 'Upload failed');

    const ingested = data.ingested || [];
    const errors   = data.errors   || [];

    if (ingested.length) {
      const totalChunks = ingested.reduce((acc, curr) => acc + (curr.chunks || 0), 0);
      showToast(`✅ Successfully indexed ${ingested.length} invoice(s) (${totalChunks} chunks)`, 'success');
      probeBackend(); // refresh stats pill
    }
    errors.forEach(e => showToast(`⚠️ ${e.filename}: ${e.error}`, 'error'));
  } catch (err) {
    let msg = err.message || 'Upload error';
    if (msg.includes('Failed to fetch')) {
      msg = 'Unable to reach backend server. Please verify the Flask app is running at http://127.0.0.1:5000.';
    }
    showToast(msg, 'error');
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
  if (sendBtn) sendBtn.disabled = val;
  if (chatInput) {
    chatInput.disabled = val;
    chatInput.style.opacity = val ? '0.6' : '1';
    if (!val) chatInput.focus();
  }
}

// ── Toast ────────────────────────────────────────────────────────
function showToast(msg, type = 'info') {
  if (!toastContainer) return;
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

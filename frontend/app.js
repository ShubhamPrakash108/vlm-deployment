/* ════════════════════════════════════════════════════════
   VLM Analyzer – Frontend JavaScript
   Interfaces with: POST /analyze/stream, GET /health
════════════════════════════════════════════════════════ */

'use strict';

// ───────────────────────────────────────────
// DOM REFS
// ───────────────────────────────────────────
const $ = (id) => document.getElementById(id);

const el = {
  apiKey:          $('apiKey'),
  toggleApiKey:    $('toggleApiKey'),
  modelName:       $('modelName'),
  imageUrl:        $('imageUrl'),
  queryText:       $('queryText'),
  analyzeBtn:      $('analyzeBtn'),
  analyzeBtnText:  $('analyzeBtnText'),
  clearBtn:        $('clearBtn'),

  // preview
  previewBox:      $('previewBox'),
  previewEmpty:    $('previewEmpty'),
  previewImg:      $('previewImg'),
  previewLoading:  $('previewLoading'),
  previewError:    $('previewError'),
  previewMeta:     $('previewMeta'),
  previewMetaDims: $('previewMetaDims'),

  // response
  responseBox:      $('responseBox'),
  responseContent:  $('responseContent'),
  responsePlaceholder: $('responsePlaceholder'),
  responseText:     $('responseText'),
  responseCacheBadge: $('responseCacheBadge'),
  generationBadge:  $('generationBadge'),
  copyBtn:          $('copyBtn'),
  statsRow:         $('statsRow'),
  statTimeVal:      $('statTimeVal'),
  statTokensVal:    $('statTokensVal'),

  // health
  statusDot:   $('statusDot'),
  statusLabel: $('statusLabel'),

  // toast
  toast:    $('toast'),
  toastMsg: $('toastMsg'),
};

// ───────────────────────────────────────────
// STATE
// ───────────────────────────────────────────
let abortController = null;
let imagePreviewDebounce = null;
let currentResponse = '';
let toastTimeout = null;

// ───────────────────────────────────────────
// TOAST
// ───────────────────────────────────────────
function showToast(msg, duration = 3500) {
  el.toastMsg.textContent = msg;
  el.toast.classList.add('show');
  el.toast.classList.remove('hidden');
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => {
    el.toast.classList.remove('show');
  }, duration);
}

// ───────────────────────────────────────────
// HEALTH CHECK
// ───────────────────────────────────────────
async function checkHealth() {
  try {
    const res = await fetch(`/health`, { signal: AbortSignal.timeout(5000) });
    const data = await res.json();
    if (data.status === 'ok') {
      el.statusDot.className = 'status-dot online';
      el.statusLabel.textContent = 'API Online';
    } else {
      el.statusDot.className = 'status-dot error';
      el.statusLabel.textContent = 'Degraded';
    }
  } catch {
    el.statusDot.className = 'status-dot offline';
    el.statusLabel.textContent = 'Offline';
  }
}

// ───────────────────────────────────────────
// IMAGE PREVIEW
// ───────────────────────────────────────────
function setPreviewState(state) {
  el.previewEmpty.classList.add('hidden');
  el.previewImg.classList.add('hidden');
  el.previewLoading.classList.add('hidden');
  el.previewError.classList.add('hidden');
  el.previewMeta.classList.add('hidden');

  if (state === 'empty')   el.previewEmpty.classList.remove('hidden');
  if (state === 'loading') el.previewLoading.classList.remove('hidden');
  if (state === 'error')   el.previewError.classList.remove('hidden');
  if (state === 'loaded') {
    el.previewImg.classList.remove('hidden');
    el.previewMeta.classList.remove('hidden');
  }
}

function loadImagePreview(url) {
  if (!url) {
    setPreviewState('empty');
    return;
  }

  setPreviewState('loading');

  const img = new Image();

  img.onload = () => {
    el.previewImg.src = url;
    el.previewMetaDims.textContent = `${img.naturalWidth} × ${img.naturalHeight}`;
    setPreviewState('loaded');
  };

  img.onerror = () => {
    setPreviewState('error');
  };

  img.src = url;
}

el.imageUrl.addEventListener('input', () => {
  clearTimeout(imagePreviewDebounce);
  const url = el.imageUrl.value.trim();
  if (!url) {
    setPreviewState('empty');
    return;
  }
  setPreviewState('loading');
  imagePreviewDebounce = setTimeout(() => loadImagePreview(url), 600);
});

// ───────────────────────────────────────────
// API KEY TOGGLE
// ───────────────────────────────────────────
el.toggleApiKey.addEventListener('click', () => {
  const isPassword = el.apiKey.type === 'password';
  el.apiKey.type = isPassword ? 'text' : 'password';
});

// ───────────────────────────────────────────
// SAMPLE CHIPS
// ───────────────────────────────────────────
document.getElementById('samplesChips').addEventListener('click', (e) => {
  const chip = e.target.closest('.chip');
  if (!chip) return;
  el.queryText.value = chip.dataset.query;
  el.queryText.focus();
  el.queryText.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
});

// ───────────────────────────────────────────
// COPY RESPONSE
// ───────────────────────────────────────────
el.copyBtn.addEventListener('click', async () => {
  if (!currentResponse) return;
  try {
    await navigator.clipboard.writeText(currentResponse);
    showToast('✓ Response copied to clipboard');
  } catch {
    showToast('Could not copy – please select text manually');
  }
});

// ───────────────────────────────────────────
// CLEAR
// ───────────────────────────────────────────
el.clearBtn.addEventListener('click', () => {
  if (abortController) {
    abortController.abort();
    abortController = null;
  }
  el.imageUrl.value = '';
  el.queryText.value = '';
  currentResponse = '';
  setPreviewState('empty');
  resetResponse();
  setAnalyzeLoading(false);
});

function resetResponse() {
  el.responsePlaceholder.classList.remove('hidden');
  el.responseText.classList.add('hidden');
  el.responseText.textContent = '';
  el.responseText.classList.remove('streaming');
  el.responseCacheBadge.classList.add('hidden');
  el.generationBadge.classList.add('hidden');
  el.copyBtn.classList.add('hidden');
  el.statsRow.classList.add('hidden');
  el.responseBox.classList.remove('active');
}

// ───────────────────────────────────────────
// LOADING STATE
// ───────────────────────────────────────────
function setAnalyzeLoading(loading) {
  if (loading) {
    el.analyzeBtn.disabled = true;
    el.analyzeBtn.classList.add('btn--loading');
    el.analyzeBtnText.textContent = 'Analyzing…';
  } else {
    el.analyzeBtn.disabled = false;
    el.analyzeBtn.classList.remove('btn--loading');
    el.analyzeBtnText.textContent = 'Analyze Image';
  }
}

// ───────────────────────────────────────────
// GENERATE UNIQUE ID
// ───────────────────────────────────────────
function genId() {
  return 'gen_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 6);
}

// ───────────────────────────────────────────
// COUNT TOKENS (approximate word count as proxy)
// ───────────────────────────────────────────
function estimateTokens(text) {
  return Math.ceil(text.trim().split(/\s+/).length * 1.33);
}

// ───────────────────────────────────────────
// ANALYZE
// ───────────────────────────────────────────
el.analyzeBtn.addEventListener('click', () => runAnalysis());

async function runAnalysis() {
  const apiKey    = el.apiKey.value.trim();
  const model     = el.modelName.value.trim();
  const imageUrl  = el.imageUrl.value.trim();
  const query     = el.queryText.value.trim();

  // Validate
  if (!apiKey)   { showToast('⚠️ Please enter your API key'); el.apiKey.focus(); return; }
  if (!model)    { showToast('⚠️ Please enter a model name'); el.modelName.focus(); return; }
  if (!imageUrl) { showToast('⚠️ Please enter an image URL'); el.imageUrl.focus(); return; }
  if (!query)    { showToast('⚠️ Please enter a question'); el.queryText.focus(); return; }

  // Abort any existing request
  if (abortController) abortController.abort();
  abortController = new AbortController();

  const generationId = genId();
  currentResponse = '';

  setAnalyzeLoading(true);
  resetResponse();

  // Show generation badge
  el.generationBadge.textContent = generationId;
  el.generationBadge.classList.remove('hidden');

  const startTime = performance.now();

  try {
    const response = await fetch(`/analyze/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': apiKey,
      },
      body: JSON.stringify({
        generation_id: generationId,
        model,
        image_url: imageUrl,
        query,
      }),
      signal: abortController.signal,
    });

    if (!response.ok) {
      let detail = response.statusText;
      try {
        const err = await response.json();
        detail = err.detail || detail;
      } catch { /* ignore */ }
      throw new Error(`${response.status}: ${detail}`);
    }

    // Show response box
    el.responsePlaceholder.classList.add('hidden');
    el.responseText.classList.remove('hidden');
    el.responseText.classList.add('streaming');
    el.responseBox.classList.add('active');

    // Stream
    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    let done = false;
    while (!done) {
      const { value, done: streamDone } = await reader.read();
      done = streamDone;
      if (value) {
        const chunk = decoder.decode(value, { stream: !done });
        currentResponse += chunk;
        el.responseText.textContent = currentResponse;

        // Auto-scroll
        el.responseText.scrollIntoView({ block: 'end', behavior: 'smooth' });
      }
    }

    // Done streaming
    el.responseText.classList.remove('streaming');
    el.copyBtn.classList.remove('hidden');

    // Cache hint
    if (currentResponse.startsWith('Using cached response')) {
      el.responseCacheBadge.classList.remove('hidden');
    }

    // Stats
    const elapsed = ((performance.now() - startTime) / 1000).toFixed(2);
    const tokens   = estimateTokens(currentResponse);
    el.statTimeVal.textContent   = `${elapsed}s`;
    el.statTokensVal.textContent = `~${tokens} tokens`;
    el.statsRow.classList.remove('hidden');

  } catch (err) {
    if (err.name === 'AbortError') {
      showToast('Request cancelled');
    } else {
      el.responseText.classList.remove('hidden');
      el.responsePlaceholder.classList.add('hidden');
      el.responseText.textContent = `Error: ${err.message}`;
      el.responseText.classList.remove('streaming');
      showToast(`❌ ${err.message}`);
    }
  } finally {
    setAnalyzeLoading(false);
    abortController = null;
  }
}

// ───────────────────────────────────────────
// ENTER KEY SHORTCUT (Ctrl/Cmd + Enter)
// ───────────────────────────────────────────
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault();
    runAnalysis();
  }
});

// ───────────────────────────────────────────
// INIT
// ───────────────────────────────────────────
(function init() {
  checkHealth();
  // Poll health every 30s
  setInterval(checkHealth, 30_000);
})();

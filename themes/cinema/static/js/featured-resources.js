// Template 3: show a saved resource link directly on the homepage.
(() => {
  const backdrop = document.getElementById('cinemaTransferBackdrop');
  const modal = document.getElementById('cinemaTransferModal');
  const titleEl = document.getElementById('cinemaTransferTitle');
  const linkEl = document.getElementById('cinemaTransferLink');
  if (!backdrop || !modal) return;

  let currentUrl = '';
  let trigger = null;

  function setLink(title, url) {
    currentUrl = url;
    titleEl.textContent = title;
    linkEl.textContent = url;
    linkEl.href = url;
  }

  function showResource(button) {
    const title = button.dataset.title || '未命名资源';
    const originalUrl = button.dataset.url || '';
    if (!originalUrl) return;
    trigger = button;
    backdrop.hidden = false;
    modal.hidden = false;
    setLink(title, originalUrl);
    modal.querySelector('[data-cinema-transfer-close]').focus();
  }

  function closeModal() {
    backdrop.hidden = true;
    modal.hidden = true;
    if (trigger) trigger.focus();
  }

  async function copyLink() {
    if (!currentUrl) return;
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error('Clipboard fallback');
      await navigator.clipboard.writeText(currentUrl);
    } catch (_) {
      const input = document.createElement('textarea');
      input.value = currentUrl;
      input.style.position = 'fixed';
      input.style.opacity = '0';
      document.body.appendChild(input);
      input.select();
      document.execCommand('copy');
      input.remove();
    }
    if (window.showCinemaToast) window.showCinemaToast('链接已复制');
  }

  document.querySelectorAll('.cinema-featured-item').forEach(button => {
    button.addEventListener('click', () => showResource(button));
  });
  backdrop.addEventListener('click', closeModal);
  modal.querySelectorAll('[data-cinema-transfer-close]').forEach(button => button.addEventListener('click', closeModal));
  modal.querySelector('#cinemaTransferCopyBtn').addEventListener('click', copyLink);
  modal.querySelector('#cinemaTransferOpenBtn').addEventListener('click', () => {
    if (currentUrl) window.open(currentUrl, '_blank', 'noopener');
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !modal.hidden) closeModal();
  });
})();

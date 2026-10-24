(() => {
  const tabs = [...document.querySelectorAll('#cinemaFilmTabs .cinema-film-tab')];
  const grid = document.getElementById('cinemaFilmsGrid');
  const status = document.getElementById('cinemaFilmsStatus');
  const section = document.getElementById('cinemaFilms');
  if (!tabs.length || !grid || !status || !section) return;

  let requestId = 0;

  function showSkeletons() {
    grid.setAttribute('aria-busy', 'true');
    grid.replaceChildren();
    for (let index = 0; index < 12; index++) {
      const card = document.createElement('div');
      card.className = 'cinema-film-card cinema-film-skeleton';
      card.setAttribute('aria-hidden', 'true');
      card.innerHTML = '<span class="cinema-film-poster"></span><span class="cinema-film-name">&nbsp;</span>';
      grid.appendChild(card);
    }
  }

  function showItems(items) {
    grid.replaceChildren();
    items.forEach((item, index) => {
      const card = document.createElement('a');
      card.className = 'cinema-film-card';
      card.href = `/search?q=${encodeURIComponent(item.title)}`;
      card.setAttribute('aria-label', `搜索${item.title}`);

      const poster = document.createElement('span');
      poster.className = 'cinema-film-poster';
      const fallback = document.createElement('span');
      fallback.className = 'cinema-film-fallback';
      fallback.textContent = item.title;
      poster.appendChild(fallback);

      if (item.poster) {
        const image = document.createElement('img');
        poster.classList.add('is-loading');
        image.alt = '';
        image.loading = 'lazy';
        image.addEventListener('load', () => {
          poster.classList.remove('is-loading');
          image.classList.add('is-loaded');
        }, { once: true });
        image.addEventListener('error', () => {
          poster.classList.remove('is-loading');
          image.remove();
        }, { once: true });
        image.src = item.poster;
        poster.appendChild(image);
      }

      const rank = document.createElement('span');
      rank.className = 'cinema-film-rank';
      rank.textContent = `TOP ${index + 1}`;
      poster.appendChild(rank);

      const title = document.createElement('span');
      title.className = 'cinema-film-name';
      title.textContent = item.title;
      card.append(poster, title);
      grid.appendChild(card);
    });
  }

  async function loadCategory(category) {
    const currentRequest = ++requestId;
    tabs.forEach(tab => {
      const active = tab.dataset.category === category;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', String(active));
    });
    showSkeletons();
    status.hidden = false;
    status.textContent = '正在加载影视热榜…';

    try {
      const response = await fetch(`/api/film-trending?category=${encodeURIComponent(category)}`);
      if (!response.ok) throw new Error('request failed');
      const data = await response.json();
      if (currentRequest !== requestId) return;
      const items = Array.isArray(data.items) ? data.items : [];
      grid.setAttribute('aria-busy', 'false');
      if (!items.length) {
        grid.replaceChildren();
        status.textContent = '暂时无法获取热榜，请稍后再试。';
        return;
      }
      showItems(items);
      status.hidden = !data.stale;
      if (data.stale) status.textContent = '当前展示上次更新的榜单';
    } catch (_) {
      if (currentRequest === requestId) {
        grid.replaceChildren();
        grid.setAttribute('aria-busy', 'false');
        status.textContent = '暂时无法获取热榜，请稍后再试。';
      }
    }
  }

  tabs.forEach(tab => tab.addEventListener('click', () => {
    loadCategory(tab.dataset.category);
    section.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
  loadCategory('电视剧');
})();

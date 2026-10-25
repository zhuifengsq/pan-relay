// Manually curated order for template 3. The server remains the source of truth.
(() => {
    const toggle = document.getElementById('cinemaFeaturedToggle');
    const panel = document.getElementById('cinemaFeaturedPanel');
    const list = document.getElementById('cinemaFeaturedList');
    const count = document.getElementById('cinemaFeaturedCount');
    const empty = document.getElementById('cinemaFeaturedEmpty');
    const status = document.getElementById('cinemaFeaturedStatus');
    if (!toggle || !panel || !list) return;

    let featured = [];
    let busy = false;
    const api = '/admin/api/resources/cinema-featured';

    function render() {
        count.textContent = `${featured.length}/10`;
        empty.hidden = featured.length > 0;
        list.style.setProperty('--featured-rows', String(Math.max(1, Math.min(5, featured.length))));
        list.replaceChildren();
        featured.forEach((item, index) => {
            const row = document.createElement('li');
            row.className = 'cinema-featured-admin-item';
            const number = document.createElement('span');
            number.className = 'cinema-featured-admin-position';
            number.textContent = `${index + 1}.`;
            const name = document.createElement('span');
            name.className = 'cinema-featured-admin-name';
            name.textContent = item.name;
            name.title = item.name;
            const actions = document.createElement('span');
            actions.className = 'cinema-featured-admin-actions';
            for (const [action, label, disabled] of [
                ['up', '上移', index === 0],
                ['down', '下移', index === featured.length - 1],
                ['remove', '移除', false],
            ]) {
                const button = document.createElement('button');
                button.type = 'button';
                button.dataset.action = action;
                button.dataset.id = String(item.id);
                button.textContent = label;
                button.disabled = disabled || busy;
                button.setAttribute('aria-label', `${label} ${item.name}`);
                actions.appendChild(button);
            }
            row.append(number, name, actions);
            list.appendChild(row);
        });
    }

    async function refresh() {
        const response = await fetch(api);
        const body = await response.json();
        if (!response.ok || !body.success) throw new Error(body.message || '加载精选资源失败');
        featured = body.data;
        render();
    }

    async function save(nextIds) {
        if (busy) return;
        busy = true;
        render();
        status.textContent = '正在保存精选资源…';
        try {
            const response = await fetch(api, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ resource_ids: nextIds }),
            });
            const body = await response.json();
            if (!response.ok || !body.success) throw new Error(body.message || '保存失败');
            featured = body.data;
            status.textContent = '已保存，模板三首页将按此顺序展示。';
            if (typeof loadResources === 'function') loadResources();
        } catch (error) {
            status.textContent = error.message;
            try { await refresh(); } catch (_) {}
        } finally {
            busy = false;
            render();
        }
    }

    toggle.addEventListener('click', () => {
        panel.hidden = !panel.hidden;
        toggle.setAttribute('aria-expanded', String(!panel.hidden));
    });

    document.addEventListener('click', (event) => {
        const button = event.target.closest('.cinema-featured-choice');
        if (!button || busy) return;
        const id = Number(button.dataset.id);
        const ids = featured.map(item => item.id);
        if (ids.includes(id)) {
            save(ids.filter(itemId => itemId !== id));
        } else if (ids.length >= 10) {
            panel.hidden = false;
            toggle.setAttribute('aria-expanded', 'true');
            status.textContent = '最多只能选择 10 条，请先移除一条。';
        } else {
            panel.hidden = false;
            toggle.setAttribute('aria-expanded', 'true');
            save([...ids, id]);
        }
    });

    list.addEventListener('click', (event) => {
        const button = event.target.closest('button[data-action]');
        if (!button || busy) return;
        const id = Number(button.dataset.id);
        const ids = featured.map(item => item.id);
        const index = ids.indexOf(id);
        if (index < 0) return;
        if (button.dataset.action === 'remove') ids.splice(index, 1);
        else {
            const other = button.dataset.action === 'up' ? index - 1 : index + 1;
            if (other < 0 || other >= ids.length) return;
            [ids[index], ids[other]] = [ids[other], ids[index]];
        }
        save(ids);
    });

    refresh().catch(error => { status.textContent = error.message; });
})();

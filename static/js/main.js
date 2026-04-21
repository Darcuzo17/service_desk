let assignTarget = null;

const STATUSES = [
  { value: 'new',         label: 'Новая' },
  { value: 'in_progress', label: 'В работе' },
  { value: 'resolved',    label: 'Решена' },
];
const PRIORITY_LABELS = {
  low: 'Низкий', medium: 'Средний', high: 'Высокий', critical: 'Критический',
};

function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
function closeModal(id) { document.getElementById(id).classList.add('hidden'); }
function esc(s) { return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function prettyDate(v) { return v ? new Date(v).toLocaleString('ru-RU') : '—'; }

async function loadTickets(filter = 'all', userId = '', workGroupId = '') {
  const params = new URLSearchParams({ filter });
  if (userId) params.set('user_id', userId);
  if (workGroupId) params.set('work_group_uid', workGroupId);
  const res = await fetch(`/api/tickets?${params.toString()}`);
  if (!res.ok) return;
  const tickets = await res.json();
  const tbody = document.querySelector('#queue tbody');
  tbody.innerHTML = '';
  if (!tickets.length) {
    tbody.innerHTML = '<tr><td colspan="7">Задачи не найдены</td></tr>';
    return;
  }
  tbody.innerHTML = tickets.map(t => {
    const overdueClass = t.is_overdue ? ' class="overdue"' : '';
    return `<tr${overdueClass}>
      <td><a href="/ticket/${t.ticket_uid}">${esc(t.ticket_number)}</a></td>
      <td>${esc(t.summary)}</td>
      <td>
        <select onchange="changeStatus('${t.ticket_uid}', this.value)">
          ${STATUSES.map(s => `<option value="${s.value}" ${s.value===t.status?'selected':''}>${s.label}</option>`).join('')}
        </select>
      </td>
      <td>${esc(t.performer || '—')}</td>
      <td>${prettyDate(t.deadline_at)}</td>
      <td>${esc(PRIORITY_LABELS[t.priority] || t.priority || 'Средний')}</td>
      <td><button onclick="openAssign('${t.ticket_uid}')">Назначить</button></td>
    </tr>`;
  }).join('');
}

async function applyFilter() {
  const f  = document.getElementById('filter-select')?.value || 'all';
  const u  = document.getElementById('user-select')?.value  || '';
  const wg = document.getElementById('wg-select')?.value    || '';
  await loadTickets(f, u, wg);
}

async function loadUsers() {
  const res = await fetch('/api/specialists');
  if (!res.ok) return;
  const users = await res.json();
  const byPerformer = document.getElementById('user-select');
  const assignUser  = document.getElementById('assign-user');
  if (!byPerformer || !assignUser) return;
  const options = users.map(u => `<option value="${u.user_uid}">${esc(u.full_name)}</option>`).join('');
  byPerformer.innerHTML = '<option value="">— по исполнителю —</option>' + options;
  assignUser.innerHTML  = '<option value="">Выбрать исполнителя</option>' + options;
}

function openAssign(uid) {
  assignTarget = uid;
  openModal('assign-modal');
}

async function submitAssign() {
  if (!assignTarget) return;
  const performerUid = document.getElementById('assign-user').value;
  const res = await fetch(`/tickets/${assignTarget}/assign`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ performer_uid: performerUid })
  });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) {
    alert(payload.error || 'Assignment failed');
    return;
  }
  closeModal('assign-modal');
  assignTarget = null;
  await applyFilter();
}

async function changeStatus(uid, status) {
  const res = await fetch(`/tickets/${uid}/status`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status })
  });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) alert(payload.error || 'Status update failed');
  await applyFilter();
}

async function pollNotifications() {
  const res = await fetch('/api/notifications');
  if (!res.ok) return;
  const data = await res.json();
  const badge = document.getElementById('notif-badge');
  const list  = document.getElementById('notif-list');
  if (!badge || !list) return;
  badge.textContent = data.count;
  badge.classList.toggle('hidden', !data.count);
  list.innerHTML = data.items.length
    ? data.items.map(i => `<li><a href="/ticket/${i.ticket_uid}">${esc(i.message)}</a></li>`).join('')
    : '<li class="muted">Нет непрочитанных уведомлений</li>';
}

async function markNotificationsRead() {
  await fetch('/api/notifications/read', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  await pollNotifications();
}

function toggleNotifDropdown() {
  const dd = document.getElementById('notif-dropdown');
  if (!dd) return;
  dd.classList.toggle('hidden');
  if (!dd.classList.contains('hidden')) markNotificationsRead();
}

function initKanban() {
  const cards = document.querySelectorAll('.ticket-card');
  const columns = document.querySelectorAll('.kanban-column');

  cards.forEach(card => {
    card.addEventListener('dragstart', handleDragStart);
  });

  columns.forEach(column => {
    column.addEventListener('dragover', handleDragOver);
    column.addEventListener('drop', handleDrop);
  });

  let draggedCard = null;

  function handleDragStart(e) {
    draggedCard = e.target.closest('.ticket-card');
    e.dataTransfer.effectAllowed = 'move';
  }

  function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  }

  function handleDrop(e) {
    e.preventDefault();
    const targetColumn = e.target.closest('.kanban-column');
    if (!targetColumn || !draggedCard) return;

    const newStatus = targetColumn.dataset.status;
    const ticketUid = draggedCard.dataset.ticketUid;

    // Отправляем запрос на изменение статуса
    fetch('/tickets/' + ticketUid + '/status', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ status: newStatus })
    })
    .then(response => {
      if (response.ok) {
        // Перемещаем карточку
        targetColumn.appendChild(draggedCard);
        // Обновляем страницу для актуальных stats
        location.reload();
      } else {
        alert('Ошибка при изменении статуса');
        return response.json().then(data => alert(data.error || 'Ошибка'));
      }
    })
    .catch(error => {
      console.error('Error:', error);
      alert('Ошибка при изменении статуса');
    });
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  await loadUsers();
  await applyFilter();
  document.getElementById('filter-select')?.addEventListener('change', applyFilter);
  document.getElementById('user-select')?.addEventListener('change', applyFilter);
  document.getElementById('wg-select')?.addEventListener('change', applyFilter);
  setInterval(pollNotifications, 15000);
  pollNotifications();
  // Инициализация канбана, если есть элементы
  if (document.querySelector('.kanban-board')) {
    initKanban();
  }
});

function showToast(message) {
  alert(message);
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  return r.json();
}

async function deactivateUser(uid) {
  if (!confirm('Деактивировать пользователя?')) return;
  const d = await postJSON(`/admin/delete-user/${uid}`);
  if (d.success) location.reload(); else showToast(d.error || 'Ошибка');
}

async function resetPassword(uid) {
  const d = await postJSON(`/admin/reset-password/${uid}`);
  if (d.success) showToast(`Новый пароль: ${d.new_password}`); else showToast(d.error || 'Ошибка');
}

async function deleteCategory(uid) {
  if (!confirm('Деактивировать категорию/услугу?')) return;
  const d = await postJSON(`/admin/delete-category/${uid}`);
  if (d.success) location.reload(); else showToast(d.error || 'Ошибка');
}

async function createWorkGroup() {
  const name = document.getElementById('wg_name').value.trim();
  const desc = document.getElementById('wg_desc').value.trim();
  if (!name) return showToast('Введите название группы');
  const d = await postJSON('/admin/create-work-group', {group_name: name, group_description: desc});
  if (d.success) location.reload(); else showToast(d.error || 'Ошибка');
}

async function deleteWorkGroup(uid) {
  if (!confirm('Удалить рабочую группу?')) return;
  const d = await postJSON(`/admin/delete-work-group/${uid}`);
  if (d.success) location.reload(); else showToast(d.error || 'Ошибка');
}

async function createTicket() {
  const catalog_uid = document.getElementById('new_catalog_uid').value;
  const summary = document.getElementById('new_summary').value.trim();
  const description = document.getElementById('new_description').value.trim();
  const d = await postJSON('/api/tickets', {catalog_uid, summary, description});
  if (d.success) {
    showToast(`Заявка создана: ${d.ticket_number}`);
    window.location.reload();
  } else {
    showToast(d.error || 'Ошибка');
  }
}
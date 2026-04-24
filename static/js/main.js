let assignTarget = null;

const STATUSES = [
  { value: 'new', label: 'Новая' },
  { value: 'in_progress', label: 'В работе' },
  { value: 'resolved', label: 'Решена' },
];

const PRIORITY_LABELS = {
  low: 'Низкий',
  medium: 'Средний',
  high: 'Высокий',
  critical: 'Критический',
};

function openModal(id) {
  document.getElementById(id)?.classList.remove('hidden');
}

function closeModal(id) {
  document.getElementById(id)?.classList.add('hidden');
}

function esc(value) {
  return (value || '').replace(/[&<>"']/g, char => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[char]));
}

function prettyDate(value) {
  return value ? new Date(value).toLocaleString('ru-RU') : '—';
}

function updateTicketPriorityPreview() {
  const catalogSelect = document.getElementById('ticket-catalog');
  const preview = document.getElementById('ticket-priority-preview');
  if (!catalogSelect || !preview) return;
  const selectedOption = catalogSelect.options[catalogSelect.selectedIndex];
  const priorityCode = selectedOption?.dataset?.priority || 'medium';
  preview.textContent = PRIORITY_LABELS[priorityCode] || priorityCode;
}

async function loadTickets(filter = 'all', userId = '', workGroupId = '') {
  const params = new URLSearchParams({ filter });
  if (userId) params.set('user_id', userId);
  if (workGroupId) params.set('work_group_uid', workGroupId);
  const res = await fetch(`/api/tickets?${params.toString()}`);
  if (!res.ok) return;

  const tickets = await res.json();
  const tbody = document.querySelector('#queue tbody');
  if (!tbody) return;

  tbody.innerHTML = '';
  if (!tickets.length) {
    tbody.innerHTML = '<tr><td colspan="7">Задачи не найдены</td></tr>';
    return;
  }

  tbody.innerHTML = tickets.map(ticket => {
    const overdueClass = ticket.is_overdue ? ' class="overdue"' : '';
    return `<tr${overdueClass}>
      <td><a href="/ticket/${ticket.ticket_uid}">${esc(ticket.ticket_number)}</a></td>
      <td>${esc(ticket.summary)}</td>
      <td>
        <select onchange="changeStatus('${ticket.ticket_uid}', this.value)">
          ${STATUSES.map(status => `<option value="${status.value}" ${status.value === ticket.status ? 'selected' : ''}>${status.label}</option>`).join('')}
        </select>
      </td>
      <td>${esc(ticket.performer || '—')}</td>
      <td>${prettyDate(ticket.deadline_at)}</td>
      <td>${esc(PRIORITY_LABELS[ticket.priority] || ticket.priority || 'Средний')}</td>
      <td><button type="button" onclick="openAssign('${ticket.ticket_uid}')">Назначить</button></td>
    </tr>`;
  }).join('');
}

async function applyFilter() {
  const filter = document.getElementById('filter-select')?.value || 'all';
  const userId = document.getElementById('user-select')?.value || '';
  const workGroupId = document.getElementById('wg-select')?.value || '';
  await loadTickets(filter, userId, workGroupId);
}

async function loadUsers() {
  const res = await fetch('/api/specialists');
  if (!res.ok) return;

  const users = await res.json();
  const byPerformer = document.getElementById('user-select');
  const assignUser = document.getElementById('assign-user');
  if (!byPerformer || !assignUser) return;

  const options = users.map(user => (
    `<option value="${user.user_uid}">${esc(user.full_name)}</option>`
  )).join('');

  byPerformer.innerHTML = '<option value="">— по исполнителю —</option>' + options;
  assignUser.innerHTML = '<option value="">Выбрать исполнителя</option>' + options;
}

function openAssign(uid) {
  assignTarget = uid;
  openModal('assign-modal');
}

async function submitAssign() {
  if (!assignTarget) return;

  const performerUid = document.getElementById('assign-user')?.value;
  const res = await fetch(`/tickets/${assignTarget}/assign`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ performer_uid: performerUid })
  });

  const payload = await res.json().catch(() => ({}));
  if (!res.ok) {
    alert(payload.error || 'Не удалось назначить исполнителя');
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
  if (!res.ok) {
    alert(payload.error || 'Не удалось обновить статус');
  }
  await applyFilter();
}

function renderNotifications(data) {
  const badge = document.getElementById('notif-badge');
  const list = document.getElementById('notif-list');
  const markAllButton = document.getElementById('notif-mark-all');
  if (!badge || !list) return;

  badge.textContent = data.count;
  badge.classList.toggle('hidden', !data.count);
  markAllButton?.classList.toggle('hidden', !data.count);

  list.innerHTML = data.items.length
    ? data.items.map(item => `
      <li class="notif-item ${item.is_read ? 'is-read' : 'is-unread'}">
        <a href="${item.ticket_uid ? `/ticket/${item.ticket_uid}` : '#'}" onclick="openNotification(event, '${item.uid}', '${item.ticket_uid || ''}')">
          <span class="notif-message">${esc(item.message)}</span>
          <span class="notif-date">${esc(item.created_at)}</span>
        </a>
      </li>
    `).join('')
    : '<li class="muted notif-empty">Нет уведомлений</li>';
}

async function pollNotifications() {
  const res = await fetch('/api/notifications');
  if (!res.ok) return;
  const data = await res.json();
  renderNotifications(data);
}

async function markNotificationsRead(uid = null) {
  const payload = uid ? { uid } : {};
  await fetch('/api/notifications/read', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    keepalive: true
  });
}

async function markAllNotificationsRead() {
  await markNotificationsRead();
  await pollNotifications();
}

async function openNotification(event, uid, ticketUid) {
  event.preventDefault();
  event.stopPropagation();

  if (uid) {
    await markNotificationsRead(uid);
  }
  await pollNotifications();

  if (ticketUid) {
    window.location.href = `/ticket/${ticketUid}`;
  }
}

async function toggleNotifDropdown(event) {
  event?.stopPropagation();

  const dropdown = document.getElementById('notif-dropdown');
  if (!dropdown) return;

  const isOpening = dropdown.classList.contains('hidden');
  dropdown.classList.toggle('hidden');

  if (isOpening) {
    await pollNotifications();
  }
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

  function handleDragStart(event) {
    draggedCard = event.target.closest('.ticket-card');
    event.dataTransfer.effectAllowed = 'move';
  }

  function handleDragOver(event) {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }

  function handleDrop(event) {
    event.preventDefault();
    const targetColumn = event.target.closest('.kanban-column');
    if (!targetColumn || !draggedCard) return;

    const targetTickets = targetColumn.querySelector('.kanban-tickets');
    if (!targetTickets) return;

    const newStatus = targetColumn.dataset.status;
    const ticketUid = draggedCard.dataset.ticketUid;

    fetch(`/tickets/${ticketUid}/status`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ status: newStatus })
    })
      .then(response => {
        if (response.ok) {
          targetTickets.appendChild(draggedCard);
          location.reload();
        } else {
          alert('Ошибка при изменении статуса');
          return response.json().then(data => alert(data.error || 'Ошибка'));
        }
      })
      .catch(error => {
        console.error('Ошибка:', error);
        alert('Ошибка при изменении статуса');
      });
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  await loadUsers();
  await applyFilter();
  updateTicketPriorityPreview();

  document.getElementById('filter-select')?.addEventListener('change', applyFilter);
  document.getElementById('user-select')?.addEventListener('change', applyFilter);
  document.getElementById('wg-select')?.addEventListener('change', applyFilter);
  document.getElementById('ticket-catalog')?.addEventListener('change', updateTicketPriorityPreview);

  document.addEventListener('click', event => {
    const wrapper = document.getElementById('notif-wrapper');
    const dropdown = document.getElementById('notif-dropdown');
    if (!wrapper || !dropdown || dropdown.classList.contains('hidden')) return;
    if (!wrapper.contains(event.target)) {
      dropdown.classList.add('hidden');
    }
  });

  setInterval(pollNotifications, 15000);
  await pollNotifications();

  if (document.querySelector('.kanban-board')) {
    initKanban();
  }
});

function showToast(message) {
  alert(message);
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  return response.json();
}

async function deactivateUser(uid) {
  if (!confirm('Деактивировать пользователя?')) return;
  const data = await postJSON(`/admin/delete-user/${uid}`);
  if (data.success) {
    location.reload();
  } else {
    showToast(data.error || 'Ошибка');
  }
}

async function resetPassword(uid) {
  const data = await postJSON(`/admin/reset-password/${uid}`);
  if (data.success) {
    showToast(`Новый пароль: ${data.new_password}`);
  } else {
    showToast(data.error || 'Ошибка');
  }
}

async function deleteCategory(uid) {
  if (!confirm('Деактивировать категорию/услугу?')) return;
  const data = await postJSON(`/admin/delete-category/${uid}`);
  if (data.success) {
    location.reload();
  } else {
    showToast(data.error || 'Ошибка');
  }
}

async function createWorkGroup() {
  const name = document.getElementById('wg_name')?.value.trim();
  const desc = document.getElementById('wg_desc')?.value.trim();
  if (!name) return showToast('Введите название группы');

  const data = await postJSON('/admin/create-work-group', {
    group_name: name,
    group_description: desc
  });

  if (data.success) {
    location.reload();
  } else {
    showToast(data.error || 'Ошибка');
  }
}

async function deleteWorkGroup(uid) {
  if (!confirm('Удалить рабочую группу?')) return;
  const data = await postJSON(`/admin/delete-work-group/${uid}`);
  if (data.success) {
    location.reload();
  } else {
    showToast(data.error || 'Ошибка');
  }
}

async function createTicket() {
  const catalogUid = document.getElementById('new_catalog_uid')?.value;
  const summary = document.getElementById('new_summary')?.value.trim();
  const description = document.getElementById('new_description')?.value.trim();
  const data = await postJSON('/api/tickets', {
    catalog_uid: catalogUid,
    summary,
    description
  });

  if (data.success) {
    showToast(`Заявка создана: ${data.ticket_number}`);
    window.location.reload();
  } else {
    showToast(data.error || 'Ошибка');
  }
}

# Service Desk

Веб-приложение для автоматизации обработки заявок внутри компании.

## Технологии

- **Backend:** Python 3.10+, Flask, Flask-Login, Flask-SQLAlchemy, Werkzeug
- **Frontend:** HTML, CSS, Vanilla JavaScript (без фреймворков)
- **База данных:** PostgreSQL 14+ (схема `sm`)
- **ORM:** SQLAlchemy

---

## Структура проекта

```
service_desk/
├── app.py                    # Flask-приложение, все маршруты
├── models.py                 # SQLAlchemy-модели (схема sm.)
├── db_functions.py           # Обёртки для PG-функций + Python fallback
├── requirements.txt          # Зависимости Python
├── README.md
├── .gitignore
├── sql/
│   ├── 01_schema.sql         # CREATE SCHEMA, расширения, таблицы
│   └── 02_functions.sql      # Хранимые функции sm.*
├── static/
│   ├── css/style.css
│   └── js/main.js
└── templates/
    ├── base.html
    ├── login.html
    ├── change_password.html
    ├── home.html
    ├── profile.html
    ├── tickets.html
    ├── admin_users.html
    ├── admin_work_groups.html
    ├── admin_categories.html
    ├── create_user.html
    ├── edit_user.html
    ├── create_category.html
    └── edit_category.html
```

---

## Установка и запуск

### 1. Клонирование репозитория

```bash
git clone <URL репозитория>
cd service_desk
```

### 2. Создание виртуального окружения

```bash
python -m venv venv
```

Активация:
- Windows: `venv\Scripts\activate`
- Linux/macOS: `source venv/bin/activate`

### 3. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 4. Настройка PostgreSQL

#### 4.1. Создать пользователя и базу данных

```sql
CREATE USER service_desk_user WITH PASSWORD 'service123';
CREATE DATABASE service_desk_db OWNER service_desk_user;
GRANT ALL PRIVILEGES ON DATABASE service_desk_db TO service_desk_user;
```

#### 4.2. Применить SQL-скрипты

```bash
psql -U service_desk_user -d service_desk_db -f sql/01_schema.sql
psql -U service_desk_user -d service_desk_db -f sql/02_functions.sql
```

Параметры подключения задаются в `app.py` в блоке `DB_CONFIG`:

```python
DB_CONFIG = {
    "user": "service_desk_user",
    "password": "service123",
    "host": "127.0.0.1",
    "port": "5432",
    "database": "service_desk_db"
}

app.config['SQLALCHEMY_DATABASE_URI'] = (
    f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@"
    f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
```

### 5. Инициализация таблиц и начальных данных

```bash
flask --app app init-db
```

Команда создаёт все таблицы и наполняет БД:
- Администратор: логин `admin`, пароль `Admin123!`
- 5 рабочих групп: IT, HR, Security, AHO, Finance
- 5 категорий каталога услуг
- Стандартная SLA-политика

### 6. Запуск приложения

```bash
python app.py
```

Приложение будет доступно по адресу: [http://127.0.0.1:5000](http://127.0.0.1:5000)

---

## Схема базы данных (sm.)

Все таблицы расположены в схеме `sm`:

| Таблица | Назначение |
|---|---|
| `sm.users` | Пользователи системы |
| `sm.passwords` | Хэши паролей (отдельно от users) |
| `sm.user_roles` | Роли пользователей (admin/manager/specialist/user) |
| `sm.work_groups` | Рабочие группы (IT, HR, АХО и т.д.) |
| `sm.user_work_groups` | Привязка пользователей к группам (M2M) |
| `sm.sla_policies` | Политики SLA (время ответа и решения) |
| `sm.service_catalog` | Каталог услуг (категории для создания заявок) |
| `sm.tickets` | Заявки |
| `sm.ticket_history` | История изменений каждого поля заявки |
| `sm.ticket_param_values` | Комментарии, согласования (гибкий key-value) |

---

## Хранимые функции PostgreSQL

Функции расположены в схеме `sm`. Приложение пытается вызвать их через `db.session.execute(text(...))`. Если функция недоступна — автоматически применяется Python-реализация из `db_functions.py`.

| Функция | Назначение |
|---|---|
| `sm.generate_login(last, first, middle)` | Генерация логина: транслитерация + уникальность |
| `sm.generate_password()` | Генерация временного пароля (буквы, цифры, спецсимволы) |
| `sm.create_user(...)` | Создание пользователя + сброс пароля |
| `sm.reset_password(username)` | Генерация нового временного пароля |
| `sm.translit(text)` | Транслитерация кириллицы в латиницу |
| `sm.hashpassword_postgen(user, password)` | Хэширование пароля через pgcrypto |
| `sm.generate_login(...)` | Генерация уникального логина |

> **Примечание:** В `sm.hashpassword_postgen` в исходном SQL порядок аргументов — `(p_password, p_username)`, однако внутри функции они используются наоборот. Приложение обходит это: после вызова PG-функции хэш всегда перезаписывается Werkzeug-хэшем, чтобы `check_password_hash()` корректно работал.

---

## Роли пользователей

| Роль | Описание |
|---|---|
| `admin` | Полный доступ: все пользователи, все заявки, все группы |
| `manager` | Руководитель: заявки своей группы, согласование, переназначение |
| `specialist` | Специалист: берёт заявки своей группы в работу, комментирует |
| `user` | Обычный пользователь: создаёт заявки, видит только свои |

---

## Рабочие группы

Специалисты привязываются к рабочим группам через `sm.user_work_groups`.  
Каждая категория каталога услуг (`sm.service_catalog`) привязана к одной рабочей группе (`work_group_uid`).  
Специалист видит только заявки категорий, привязанных к его группе.

---

## Основные функции системы

### Авторизация
- Вход по `user_name` (генерируется автоматически: `Иванов.ИО`)
- Обязательная смена пароля при первом входе
- Блокировка после 3 неверных попыток → принудительная смена пароля
- Показать/скрыть пароль, ссылка «Забыли пароль?»

### Главная страница — каталог услуг
- Карточки категорий с иконками
- Создание заявки через модальное окно
- Тип: Запрос (`service_request`) / Инцидент (`incident`)
- Приоритет: Низкий / Средний / Высокий / Критический

### Профиль пользователя
- Просмотр и редактирование личных данных
- Отображение рабочей группы, должности, телефонов
- Список своих заявок с фильтрацией по статусу и дате
- Редактирование заявки в статусе «Новая»

### Доска заявок (специалисты и выше)
- Канбан: Назначено / В работе / Выполнено
- Статистика сверху
- Фильтры: статус, исполнитель, период
- История изменений каждой заявки
- Внутренние комментарии (видны только специалистам)
- Согласование заявок (руководитель / администратор)

### Администрирование
- Создание пользователей (логин генерируется автоматически через `sm.generate_login`)
- Сброс пароля с отображением нового временного пароля
- Управление рабочими группами
- Управление категориями каталога услуг

---

## Процесс согласования

При создании заявки типа `service_request` в `sm.ticket_param_values` записывается `param_name='requires_approval'`. Руководитель или администратор видит блок согласования в модальном окне и может одобрить или отклонить заявку. Решение сохраняется в `ticket_param_values` с `param_type='approval'`, а изменение статуса фиксируется в `sm.ticket_history`.

---

## История изменений

Любое изменение поля заявки (статус, исполнитель, описание, приоритет) фиксируется в `sm.ticket_history` с указанием старого значения, нового значения, автора изменения и времени. История отображается в модальном окне заявки на доске (раскрывающийся блок).

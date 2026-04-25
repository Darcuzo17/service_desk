"""
Главный файл веб-приложения Service Desk.

В этом модуле собрана основная логика учебного проекта:
- настройка Flask и подключения к БД;
- вспомогательные функции для проверки прав и форматирования;
- маршруты авторизации, работы с заявками и администрирования;
- команда начальной инициализации базы данных.=
"""

import os
import re
from datetime import datetime
from werkzeug.security import generate_password_hash
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    jsonify,
    flash,
)
from flask_login import (
    LoginManager,
    login_user,
    login_required,
    logout_user,
    current_user,
)
from sqlalchemy import case

from models import (
    db,
    User,
    Password,
    UserRole,
    WorkGroup,
    UserWorkGroup,
    SlaPolicy,
    ServiceCatalog,
    TicketApproval,
    Ticket,
    TicketHistory,
    TicketParamValue,
    Notification,
    gen_uuid,
    create_user_db,
    reset_password_db,
    verify_password,
    _set_password_hash,
    _ensure_role,
    _ensure_work_group,
    generate_ticket_number,
    compute_response_deadline,
    compute_deadline,
    add_ticket_history,
    notify,
    notify_ticket_update,
    create_approval_chain,
    process_approval_decision,
    format_mobile,
)

# ============================================================
# БАЗОВАЯ НАСТРОЙКА ПРИЛОЖЕНИЯ
# ============================================================
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sd-secret-key-change-in-prod")
app.json.ensure_ascii = False

# Тут держим подключение к БД в явном виде.
# Для учебного проекта так проще: сразу видно, куда приложение ходит.
DB_CONFIG = {
    "user": "service_desk_user",
    "password": "service123",
    "host": "127.0.0.1",
    "port": "5432",
    "database": "service_desk_db",
}
app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@"
    f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
    f"?client_encoding=utf8"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "connect_args": {"client_encoding": "utf8"},
    "pool_pre_ping": True,
}

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = ""
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_uid):
    """Flask-Login загружает пользователя по его идентификатору."""
    return User.query.get(str(user_uid))


# ============================================================
# КОНСТАНТЫ И ПОДПИСИ
# ============================================================
SPECIALIST_ROLES = {"specialist", "manager", "admin"}
TASK_QUEUE_FILTERS = {"all", "my", "overdue"}
CLOSED_TICKET_STATUSES = {"resolved", "closed", "cancelled"}
ROLE_LABELS = {
    "user": "Пользователь",
    "specialist": "Специалист",
    "manager": "Менеджер",
    "admin": "Администратор",
}
PRIORITY_LABELS = {
    "low": "Низкий",
    "medium": "Средний",
    "high": "Высокий",
    "critical": "Критический",
}
APPROVAL_STEP_LABELS = {
    "Manager Approval": "Согласование руководителем",
}
# ============================================================
# НЕБОЛЬШИЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================


def is_strong_password(password_value):
    """Проверка учебной политики сложности пароля."""
    return (
        len(password_value) >= 8
        and re.search(r"[A-Z]", password_value)
        and re.search(r"[a-z]", password_value)
        and re.search(r"[0-9]", password_value)
        and re.search(r'[!@#$%^&*(),.?":{}|<>]', password_value)
    )


def is_specialist(user=None):
    """Определяет, работает ли пользователь с очередью заявок."""
    target_user = user or current_user
    return target_user.role in SPECIALIST_ROLES


def _wg_uids(user):
    """Возвращает идентификаторы рабочих групп пользователя."""
    return [l.work_group_uid for l in user.work_group_links.all()]


def _can_view_ticket(ticket):
    """Проверка, может ли текущий пользователь видеть заявку."""
    if current_user.role == "admin":
        return True
    if ticket.requester_uid == current_user.user_uid:
        return True
    if is_specialist():
        if current_user.role in ("manager", "admin"):
            return True
        return ticket.catalog and ticket.catalog.work_group_uid in _wg_uids(
            current_user
        )
    return False


def _can_edit_ticket(ticket):
    """Проверка права на изменение заявки."""
    if current_user.role == "admin":
        return True
    if is_specialist():
        if current_user.role == "manager":
            return True
        return ticket.catalog and ticket.catalog.work_group_uid in _wg_uids(
            current_user
        )
    return ticket.requester_uid == current_user.user_uid and ticket.status == "new"


def _unread_count():
    """Количество непрочитанных уведомлений в шапке сайта."""
    if current_user.is_authenticated:
        return Notification.query.filter_by(
            user_uid=current_user.user_uid, is_read=False
        ).count()
    return 0


def _admin_forbidden():
    """Единый ответ для admin-only обработчиков с поддержкой JSON и HTML."""
    if request.is_json:
        return jsonify({"error": "Доступ запрещён"}), 403
    return "Доступ запрещён", 403


def _start_ticket_sla(ticket, catalog=None, started_at=None):
    """Запускает SLA-таймеры от нужной точки времени."""
    target_catalog = catalog or ticket.catalog
    if not target_catalog:
        return
    ticket.response_due_at = compute_response_deadline(target_catalog, started_at)
    ticket.deadline_at = compute_deadline(target_catalog, started_at)


def _mark_ticket_responded(ticket, responded_at=None):
    """Фиксирует первую реакцию только один раз."""
    if ticket.responded_at:
        return
    ticket.responded_at = responded_at or datetime.utcnow()


def _apply_ticket_status_dates(ticket, new_status, changed_at=None):
    """Держит resolved_at, closed_at и дату первой реакции в согласованном состоянии."""
    changed_at = changed_at or datetime.utcnow()
    if new_status in {
        "assigned",
        "in_progress",
        "on_hold",
        "resolved",
        "closed",
        "cancelled",
    }:
        _mark_ticket_responded(ticket, changed_at)

    if new_status in {"new", "assigned", "in_progress", "on_hold"}:
        ticket.resolved_at = None
        ticket.closed_at = None
    elif new_status == "resolved":
        ticket.resolved_at = changed_at
        ticket.closed_at = None
    elif new_status == "closed":
        if ticket.resolved_at is None:
            ticket.resolved_at = changed_at
        ticket.closed_at = changed_at
    elif new_status in {"cancelled", "rejected"}:
        ticket.resolved_at = None
        ticket.closed_at = None


def _assign_current_user_on_take(ticket, new_status, changed_at=None):
    """Если специалист сам переводит свободную заявку в работу, сразу делаем его исполнителем."""
    if new_status != "in_progress" or ticket.performer_uid is not None:
        return False
    if not is_specialist():
        return False
    ticket.performer_uid = current_user.user_uid
    add_ticket_history(
        ticket.ticket_uid,
        "performer",
        None,
        current_user.user_uid,
        current_user.user_uid,
    )
    _mark_ticket_responded(ticket, changed_at)
    return True


# ============================================================
# ФИЛЬТРЫ И ОБЩИЕ ШТУКИ ДЛЯ ШАБЛОНОВ
# ============================================================
@app.template_filter("status_label")
def status_label_filter(status_code):
    return {
        "new": "Новая",
        "assigned": "Назначено",
        "in_progress": "В работе",
        "on_hold": "Приостановлено",
        "pending_approval": "На согласовании",
        "approved": "Согласовано",
        "rejected": "Отклонено",
        "resolved": "Решена",
        "closed": "Закрыта",
        "cancelled": "Отменено",
    }.get(status_code, status_code)


@app.template_filter("column_label")
def column_label_filter(column_key):
    return {
        "new_unassigned": "Новые",
        "new_assigned": "Назначено",
        "in_progress": "В работе",
        "resolved": "Решена",
    }.get(column_key, column_key)


@app.template_filter("priority_label")
def priority_label_filter(priority_code):
    return PRIORITY_LABELS.get(priority_code, priority_code)


@app.template_filter("role_label")
def role_label_filter(role_code):
    return ROLE_LABELS.get(role_code, role_code)


@app.template_filter("approval_step_label")
def approval_step_label_filter(step_name):
    return APPROVAL_STEP_LABELS.get(step_name, step_name)


@app.template_filter("datefmt")
def datefmt_filter(date_value, output_format="%d.%m.%Y %H:%M"):
    if not date_value:
        return "—"
    return date_value.strftime(output_format)


@app.template_filter("overdue_for")
def overdue_for_filter(deadline_at):
    if not deadline_at:
        return ""

    current_time = (
        datetime.now(deadline_at.tzinfo) if deadline_at.tzinfo else datetime.utcnow()
    )
    if deadline_at >= current_time:
        return ""

    delta = current_time - deadline_at
    total_seconds = int(delta.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    parts = []
    if days:
        parts.append(f"{days} д.")
    if hours:
        parts.append(f"{hours} ч.")
    if not days and minutes:
        parts.append(f"{minutes} мин.")
    if not parts:
        parts.append("меньше минуты")
    return " ".join(parts[:2])


@app.context_processor
def inject_globals():
    return {
        "unread_count": _unread_count(),
        "is_specialist": is_specialist,
        "now": datetime.utcnow(),
        "PRIORITIES": PRIORITY_LABELS,
        "User": User,
    }


# ============================================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ И УЧЕБНЫХ ДАННЫХ
# ============================================================
@app.cli.command("init-db")
def init_db():
    """Создаёт схему БД и заполняет систему начальными учебными данными."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    db.session.execute(text("CREATE SCHEMA IF NOT EXISTS sm"))
    db.session.commit()
    try:
        db.create_all()
    except SQLAlchemyError:
        # В учебной среде схема могла создаваться по частям и в разных версиях.
        # Если приложение не смогло само достроить таблицы, не падаем сразу.
        # Ниже просто руками создаём то, чего не хватает.
        db.session.rollback()

    db.session.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS sm.approval_routes (
            route_uid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            route_name varchar(200) NOT NULL,
            catalog_uid uuid NOT NULL REFERENCES sm.service_catalog(catalog_uid) ON DELETE CASCADE,
            is_active bool DEFAULT true NULL,
            create_date timestamptz DEFAULT CURRENT_TIMESTAMP NULL,
            create_by uuid NOT NULL REFERENCES sm.users(user_uid)
        )
    """
        )
    )
    db.session.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS sm.approval_steps (
            step_uid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            route_uid uuid NOT NULL REFERENCES sm.approval_routes(route_uid) ON DELETE CASCADE,
            step_order int4 NOT NULL DEFAULT 1,
            step_name varchar(200) NULL,
            approver_uid uuid NULL REFERENCES sm.users(user_uid),
            approver_role varchar(32) NULL
        )
    """
        )
    )
    db.session.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS sm.ticket_approvals (
            approval_uid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_uid uuid NOT NULL REFERENCES sm.tickets(ticket_uid) ON DELETE CASCADE,
            step_order int4 NOT NULL DEFAULT 1,
            step_name varchar(200) NULL,
            approver_uid uuid NULL REFERENCES sm.users(user_uid),
            status varchar(20) NOT NULL DEFAULT 'pending',
            comment text NULL,
            decided_at timestamptz NULL,
            create_date timestamptz DEFAULT CURRENT_TIMESTAMP NULL
        )
    """
        )
    )
    db.session.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS sm.notifications (
            notification_uid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_uid uuid NOT NULL REFERENCES sm.users(user_uid) ON DELETE CASCADE,
            message text NOT NULL,
            ticket_uid uuid NULL REFERENCES sm.tickets(ticket_uid) ON DELETE SET NULL,
            is_read bool DEFAULT false NULL,
            create_date timestamptz DEFAULT CURRENT_TIMESTAMP NULL
        )
    """
        )
    )
    db.session.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS sm.ticket_templates (
            template_uid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            template_name varchar(200) NOT NULL,
            catalog_uid uuid NOT NULL REFERENCES sm.service_catalog(catalog_uid) ON DELETE CASCADE,
            summary varchar(500) NOT NULL,
            description text NOT NULL,
            priority varchar(20) NULL,
            created_by uuid NOT NULL REFERENCES sm.users(user_uid),
            is_public bool DEFAULT false NULL,
            create_date timestamptz DEFAULT CURRENT_TIMESTAMP NULL
        )
    """
        )
    )
    db.session.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS sm.audit_log (
            audit_uid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_uid uuid NULL REFERENCES sm.users(user_uid) ON DELETE SET NULL,
            action varchar(64) NOT NULL,
            entity_type varchar(64) NULL,
            entity_uid uuid NULL,
            details text NULL,
            ip_address varchar(64) NULL,
            create_date timestamptz DEFAULT CURRENT_TIMESTAMP NULL
        )
    """
        )
    )
    # Этот кусок нужен для старых учебных баз.
    # Если структура чуть отстаёт от текущего кода, спокойно дотягиваем её здесь.
    db.session.execute(
        text(
            """
        ALTER TABLE IF EXISTS sm.users
            ADD COLUMN IF NOT EXISTS manager_uid uuid NULL
    """
        )
    )
    db.session.execute(
        text(
            """
        ALTER TABLE IF EXISTS sm.service_catalog
            ADD COLUMN IF NOT EXISTS is_active bool DEFAULT true,
            ADD COLUMN IF NOT EXISTS approval_required bool DEFAULT false
    """
        )
    )
    db.session.execute(
        text(
            """
        ALTER TABLE IF EXISTS sm.tickets
            ADD COLUMN IF NOT EXISTS response_due_at timestamptz NULL,
            ADD COLUMN IF NOT EXISTS responded_at timestamptz NULL,
            ADD COLUMN IF NOT EXISTS deadline_at timestamptz NULL
    """
        )
    )
    db.session.execute(
        text(
            """
        ALTER TABLE IF EXISTS sm.users
            ALTER COLUMN mobile TYPE varchar(20),
            ALTER COLUMN work_phone TYPE varchar(20)
    """
        )
    )
    db.session.commit()

    SYS = "00000000-0000-0000-0000-000000000001"

    if not User.query.filter_by(user_name="admin").first():
        uid = gen_uuid()
        db.session.add(
            User(
                user_uid=uid,
                user_name="admin",
                first_name="Администратор",
                last_name="Системный",
                email="admin@company.ru",
                create_by=SYS,
            )
        )
        db.session.flush()
        db.session.add(
            Password(
                user_uid=uid,
                passwordhash=generate_password_hash("Admin123!"),
                is_first_login=False,
            )
        )
        db.session.add(UserRole(user_uid=uid, role="admin"))

    if not SlaPolicy.query.first():
        for name, resp, res in [
            ("4 часа", 1, 4),
            ("8 часов", 4, 8),
            ("1 день", 8, 24),
            ("3 дня", 24, 72),
        ]:
            db.session.add(
                SlaPolicy(
                    policy_name=name,
                    response_time_hours=resp,
                    resolution_time_hours=res,
                    create_by=SYS,
                )
            )
        db.session.flush()

    sla_std = SlaPolicy.query.filter_by(policy_name="Стандартный").first()
    sla_hi = SlaPolicy.query.filter_by(policy_name="Высокий").first()

    wg_defs = [
        ("IT", "IT-поддержка"),
        ("HR", "Кадры и персонал"),
        ("Security", "Безопасность"),
        ("AHO", "АХО"),
        ("Finance", "Бухгалтерия"),
    ]
    wg_map = {}
    for name, desc in wg_defs:
        wg = WorkGroup.query.filter_by(group_name=name).first()
        if not wg:
            wg = WorkGroup(group_name=name, group_description=desc, create_by=SYS)
            db.session.add(wg)
            db.session.flush()
        wg_map[name] = wg.work_group_uid

    catalog_data = [
        (
            "IT",
            "IT-услуги",
            "monitor",
            "Техническая поддержка, оборудование и ПО",
            "IT",
            None,
            None,
            None,
            False,
        ),
        (
            "IT_SUP",
            "Локальная поддержка",
            "tool",
            "Компьютер, принтер, периферия",
            "IT",
            "IT",
            "incident",
            "medium",
            False,
        ),
        (
            "IT_ACC",
            "Доступы и права",
            "key",
            "Учётные записи, VPN, почта",
            "IT",
            "IT",
            "service_request",
            "medium",
            True,
        ),
        (
            "IT_SW",
            "Программное обеспечение",
            "package",
            "Установка, лицензии, обновление",
            "IT",
            "IT",
            "service_request",
            "low",
            False,
        ),
        (
            "HR",
            "Кадры",
            "users",
            "Кадровые вопросы, документы и отпуска",
            "HR",
            None,
            None,
            None,
            False,
        ),
        (
            "HR_VAC",
            "Отпуск",
            "sun",
            "Оформление отпуска и отгулов",
            "HR",
            "HR",
            "service_request",
            "low",
            True,
        ),
        (
            "HR_DOC",
            "Справки",
            "file-text",
            "Справка о работе, копии документов",
            "HR",
            "HR",
            "service_request",
            "low",
            False,
        ),
        (
            "HR_REG",
            "Оформление",
            "clipboard",
            "Приём, перевод, увольнение",
            "HR",
            "HR",
            "service_request",
            "medium",
            True,
        ),
        ("AHO", "АХО", "home", "АХО", "AHO", None, None, None, False),
        (
            "AHO_FRN",
            "Мебель и оборудование",
            "layers",
            "Заявки на мебель и инвентарь",
            "AHO",
            "AHO",
            "service_request",
            "low",
            False,
        ),
        (
            "AHO_SUP",
            "Канцелярия",
            "pen-tool",
            "Канцелярские товары",
            "AHO",
            "AHO",
            "service_request",
            "low",
            False,
        ),
        (
            "AHO_MOV",
            "Переезд",
            "truck",
            "Переезд отдела или сотрудника",
            "AHO",
            "AHO",
            "service_request",
            "medium",
            True,
        ),
        (
            "FIN",
            "Бухгалтерия",
            "dollar-sign",
            "Финансовые вопросы и документы",
            "Finance",
            None,
            None,
            None,
            False,
        ),
        (
            "FIN_REF",
            "Справка о доходах",
            "bar-chart-2",
            "Справка о доходах, НДФЛ",
            "Finance",
            "FIN",
            "service_request",
            "low",
            False,
        ),
        (
            "FIN_RPT",
            "Авансовый отчёт",
            "credit-card",
            "Оформление авансового отчёта",
            "Finance",
            "FIN",
            "service_request",
            "medium",
            True,
        ),
        (
            "SEC",
            "Безопасность",
            "shield",
            "ИБ и физическая безопасность",
            "Security",
            None,
            None,
            None,
            False,
        ),
        (
            "SEC_PASS",
            "Пропуск",
            "credit-card",
            "Оформление и восстановление пропуска",
            "Security",
            "SEC",
            "service_request",
            "medium",
            True,
        ),
        (
            "SEC_INC",
            "Инцидент безопасности",
            "alert-triangle",
            "Сообщение об инциденте",
            "Security",
            "SEC",
            "incident",
            "high",
            False,
        ),
    ]

    cat_map = {}
    for row in catalog_data:
        key, name, icon, desc, wg_key, parent_key, ttype, prio, appr = row
        path = f"/{key}"
        cat = ServiceCatalog.query.filter_by(catalog_path=path).first()
        if not cat:
            parent_uid = cat_map.get(parent_key)
            if prio == "high":
                picked_sla_uid = (
                    sla_hi.sla_uid if sla_hi else (sla_std.sla_uid if sla_std else None)
                )
            else:
                picked_sla_uid = (
                    sla_std.sla_uid if sla_std else (sla_hi.sla_uid if sla_hi else None)
                )

            cat = ServiceCatalog(
                catalog_name=name,
                catalog_path=path,
                catalog_type="service" if parent_key else "category",
                parent_uid=parent_uid,
                work_group_uid=wg_map.get(wg_key),
                ticket_type=ttype or "service_request",
                priority=prio or "medium",
                sla_uid=picked_sla_uid,
                catalog_description=desc,
                approval_required=appr,
                create_by=SYS,
            )
            db.session.add(cat)
            db.session.flush()
        cat_map[key] = cat.catalog_uid

    db.session.commit()
    print("OK Database initialized.")


# ============================================================
# ВХОД, ВЫХОД И ПРОВЕРКА ПАРОЛЯ
# ============================================================


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect("/")
    error = None
    show_forgot = False
    if request.method == "POST":
        login_val = request.form.get("login", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter(
            db.func.lower(User.user_name) == login_val.lower()
        ).first()
        if not user or user.is_deactivated or user.is_temp_deactivated:
            error = "Неверный логин или пароль"
        else:
            pwd = user.password_record
            if not pwd or not pwd.passwordhash:
                error = "Ошибка учётной записи. Обратитесь к администратору"
            elif not verify_password(user, password):
                pwd.failed_attempts = (pwd.failed_attempts or 0) + 1
                db.session.commit()
                if pwd.failed_attempts >= 3:
                    error = "Превышено количество попыток. Обратитесь к администратору."
                    show_forgot = True
                else:
                    error = f"Неверный логин или пароль. Осталось попыток: {3 - pwd.failed_attempts}"
            else:
                pwd.failed_attempts = 0
                user.last_loggon_date = datetime.utcnow()
                db.session.commit()
                login_user(user)
                if pwd.is_first_login or pwd.must_change_password:
                    return redirect("/change-password")
                return redirect("/")
    return render_template("login.html", error=error, show_forgot=show_forgot)


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if password != confirm:
            error = "Пароли не совпадают"
        elif not is_strong_password(password):
            error = "Пароль должен содержать минимум 8 символов, заглавные и строчные буквы, цифру и спецсимвол"
        else:
            pwd = current_user.password_record
            pwd.passwordhash = generate_password_hash(password)
            pwd.is_first_login = False
            pwd.must_change_password = False
            pwd.failed_attempts = 0
            db.session.commit()
            flash("Пароль успешно изменён", "success")
            return redirect("/")
    return render_template("change_password.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")


# ============================================================
# ГЛАВНАЯ СТРАНИЦА
# ============================================================


def _build_catalog_tree():
    catalog_items = (
        ServiceCatalog.query.filter_by(is_active=True)
        .order_by(ServiceCatalog.catalog_name)
        .all()
    )

    by_parent = {}
    for item in catalog_items:
        by_parent.setdefault(item.parent_uid, []).append(item)

    for siblings in by_parent.values():
        siblings.sort(
            key=lambda item: (
                0 if item.catalog_type == "category" else 1,
                (item.catalog_name or "").lower(),
            )
        )

    services = []

    def build_node(item, trail=None):
        trail = trail or []
        current_trail = [*trail, item.catalog_name]
        children = [
            build_node(child, current_trail)
            for child in by_parent.get(item.catalog_uid, [])
        ]

        service_count = sum(child["service_count"] for child in children)
        node = {
            "catalog_uid": item.catalog_uid,
            "catalog_name": item.catalog_name,
            "catalog_description": item.catalog_description,
            "catalog_type": item.catalog_type,
            "priority": item.priority or "medium",
            "full_path": " / ".join(current_trail),
            "children": children,
            "service_count": service_count,
        }

        if item.catalog_type == "service":
            node["service_count"] = 1
            services.append(
                {
                    "catalog_uid": item.catalog_uid,
                    "catalog_name": item.catalog_name,
                    "priority": item.priority or "medium",
                    "full_path": node["full_path"],
                }
            )

        return node

    tree = [build_node(item) for item in by_parent.get(None, [])]
    return tree, services


@app.route("/")
@login_required
def home():
    q = request.args.get("q", "").strip()
    view = request.args.get("view", "catalog")

    catalog_tree, catalog_services = _build_catalog_tree()

    my_tickets = None
    search_results = None

    if view == "my_tickets":
        my_tickets = (
            Ticket.query.filter(
                db.or_(
                    Ticket.requester_uid == current_user.user_uid,
                    Ticket.recipient_uid == current_user.user_uid,
                )
            )
            .order_by(Ticket.created_at.desc())
            .limit(50)
            .all()
        )

    if q:
        tq = Ticket.query.join(ServiceCatalog)
        if not is_specialist():
            tq = tq.filter(
                db.or_(
                    Ticket.requester_uid == current_user.user_uid,
                    Ticket.recipient_uid == current_user.user_uid,
                )
            )
        search_results = (
            tq.filter(
                db.or_(
                    Ticket.ticket_number.ilike(f"%{q}%"),
                    Ticket.summary.ilike(f"%{q}%"),
                    ServiceCatalog.catalog_name.ilike(f"%{q}%"),
                )
            )
            .order_by(Ticket.created_at.desc())
            .limit(30)
            .all()
        )

    return render_template(
        "home.html",
        catalog_tree=catalog_tree,
        catalog_services=catalog_services,
        my_tickets=my_tickets,
        search_results=search_results,
        view=view,
        q=q,
    )


# ============================================================
# ЖИВОЙ ПОИСК
# ============================================================


@app.route("/api/search")
@login_required
def api_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"tickets": [], "catalog": []})

    # Ищем и услуги, и разделы каталога, чтобы поиск не казался "слепым".
    cat_q = ServiceCatalog.query.filter(
        ServiceCatalog.is_active == True,
        db.or_(
            ServiceCatalog.catalog_name.ilike(f"%{q}%"),
            ServiceCatalog.catalog_description.ilike(f"%{q}%"),
        ),
    ).all()
    # Сначала выводим то, что начинается с запроса.
    # Так результат обычно выглядит логичнее для пользователя.

    def _cat_rank(c):
        starts = c.catalog_name.lower().startswith(q.lower())
        is_svc = c.catalog_type == "service"
        return (0 if starts else 1, 0 if is_svc else 1, c.catalog_name)

    cat_q = sorted(cat_q, key=_cat_rank)[:8]

    # По заявкам тоже фильтруем, но не забываем про права доступа.
    tq = Ticket.query.join(
        ServiceCatalog, Ticket.catalog_uid == ServiceCatalog.catalog_uid, isouter=True
    )
    if not is_specialist():
        tq = tq.filter(
            db.or_(
                Ticket.requester_uid == current_user.user_uid,
                Ticket.recipient_uid == current_user.user_uid,
            )
        )
    tickets = (
        tq.filter(
            db.or_(
                Ticket.ticket_number.ilike(f"%{q}%"),
                Ticket.summary.ilike(f"%{q}%"),
            )
        )
        .order_by(Ticket.created_at.desc())
        .limit(7)
        .all()
    )

    return jsonify(
        {
            "tickets": [
                {
                    "uid": t.ticket_uid,
                    "number": t.ticket_number,
                    "summary": t.summary,
                    "status": t.status,
                    "catalog": t.catalog.catalog_name if t.catalog else "—",
                }
                for t in tickets
            ],
            "catalog": [
                {
                    "uid": c.catalog_uid,
                    "name": c.catalog_name,
                    "type": c.catalog_type,
                    "parent": c.parent.catalog_name if c.parent else None,
                    "is_service": c.catalog_type == "service",
                }
                for c in cat_q
            ],
        }
    )


# ============================================================
# СТРАНИЦА СОГЛАСОВАНИЙ
# ============================================================


@app.route("/approvals")
@login_required
def approvals():
    # Здесь лежат шаги, где текущий пользователь должен принять решение.
    to_approve = (
        TicketApproval.query.filter_by(
            approver_uid=current_user.user_uid, status="pending"
        )
        .order_by(TicketApproval.create_date.desc())
        .all()
    )

    # А тут уже заявки самого пользователя, которые сейчас висят на согласовании.
    waiting = (
        Ticket.query.filter_by(
            requester_uid=current_user.user_uid, status="pending_approval"
        )
        .order_by(Ticket.created_at.desc())
        .all()
    )
    waiting_approvers = {}
    if waiting:
        waiting_ticket_uids = [ticket.ticket_uid for ticket in waiting]
        pending_waiting_approvals = (
            TicketApproval.query.filter(
                TicketApproval.ticket_uid.in_(waiting_ticket_uids),
                TicketApproval.status == "pending",
            )
            .order_by(TicketApproval.ticket_uid, TicketApproval.step_order)
            .all()
        )
        for approval in pending_waiting_approvals:
            waiting_approvers.setdefault(
                approval.ticket_uid,
                approval.approver.full_name() if approval.approver else "—",
            )

    # История собирается не только по чужим решениям,
    # но и по заявкам самого пользователя, чтобы было проще отследить цепочку.
    my_history = (
        TicketApproval.query.join(
            Ticket, Ticket.ticket_uid == TicketApproval.ticket_uid
        )
        .filter(
            TicketApproval.status.in_(["approved", "rejected"]),
            db.or_(
                TicketApproval.approver_uid == current_user.user_uid,
                Ticket.requester_uid == current_user.user_uid,
            ),
        )
        .order_by(TicketApproval.decided_at.desc())
        .limit(50)
        .all()
    )

    # Админу даём полную картину, ему это в админке полезно.
    all_approvals = None
    if current_user.role == "admin":
        all_approvals = (
            TicketApproval.query.order_by(TicketApproval.create_date.desc())
            .limit(200)
            .all()
        )

    return render_template(
        "approvals.html",
        to_approve=to_approve,
        waiting=waiting,
        waiting_approvers=waiting_approvers,
        my_history=my_history,
        all_approvals=all_approvals,
    )


# ============================================================
# ДАННЫЕ ПО КАТАЛОГУ
# ============================================================


@app.route("/api/catalog/<catalog_uid>")
@login_required
def get_catalog_item(catalog_uid):
    cat = ServiceCatalog.query.get_or_404(catalog_uid)
    return jsonify(
        {
            "catalog_uid": cat.catalog_uid,
            "catalog_name": cat.catalog_name,
            "catalog_description": cat.catalog_description,
            "ticket_type": cat.ticket_type,
            "priority": cat.priority,
            "approval_required": cat.approval_required,
        }
    )


# ============================================================
# МОЙ ПРОФИЛЬ
# ============================================================


@app.route("/profile")
@login_required
def profile():
    tickets = (
        Ticket.query.filter(
            db.or_(
                Ticket.requester_uid == current_user.user_uid,
                Ticket.recipient_uid == current_user.user_uid,
            )
        )
        .order_by(Ticket.created_at.desc())
        .limit(20)
        .all()
    )
    manager = (
        User.query.get(current_user.manager_uid) if current_user.manager_uid else None
    )
    return render_template("profile.html", tickets=tickets, manager=manager)


@app.route("/profile/password", methods=["POST"])
@login_required
def profile_password():
    old_pw = request.form.get("old_password", "")
    new_pw = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if not verify_password(current_user, old_pw):
        flash("Текущий пароль неверен", "error")
        return redirect("/profile")
    if new_pw != confirm:
        flash("Пароли не совпадают", "error")
        return redirect("/profile")
    if not is_strong_password(new_pw):
        flash("Пароль слишком слабый", "error")
        return redirect("/profile")
    _set_password_hash(current_user.user_uid, new_pw)
    db.session.commit()
    flash("Пароль изменён", "success")
    return redirect("/profile")


# ============================================================
# ПУБЛИЧНЫЙ ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ
# ============================================================


@app.route("/user/<user_uid>")
@login_required
def user_public_profile(user_uid):
    user = User.query.get_or_404(user_uid)
    manager = User.query.get(user.manager_uid) if user.manager_uid else None
    return render_template(
        "user_profile.html",
        user=user,
        manager=manager,
        work_groups=user.all_work_groups(),
    )


# ============================================================
# УВЕДОМЛЕНИЯ
# ============================================================


@app.route("/api/notifications")
@login_required
def api_notifications():
    notes = (
        Notification.query.filter_by(user_uid=current_user.user_uid)
        .order_by(Notification.create_date.desc())
        .limit(20)
        .all()
    )
    unread_count = Notification.query.filter_by(
        user_uid=current_user.user_uid, is_read=False
    ).count()
    return jsonify(
        {
            "count": unread_count,
            "items": [
                {
                    "uid": n.notification_uid,
                    "message": n.message,
                    "ticket_uid": n.ticket_uid,
                    "is_read": n.is_read,
                    "ticket_number": (
                        n.ticket_rel.ticket_number if n.ticket_rel else None
                    ),
                    "created_at": n.create_date.strftime("%d.%m.%Y %H:%M"),
                }
                for n in notes
            ],
        }
    )


@app.route("/api/notifications/read", methods=["POST"])
@login_required
def mark_notifications_read():
    data = request.get_json() or {}
    uid = data.get("uid")
    if uid:
        note = Notification.query.filter_by(
            notification_uid=uid, user_uid=current_user.user_uid
        ).first()
        if note:
            note.is_read = True
    else:
        Notification.query.filter_by(
            user_uid=current_user.user_uid, is_read=False
        ).update({"is_read": True})
    db.session.commit()
    return jsonify({"success": True})


# ============================================================
# ОБЩАЯ ДОСКА ЗАЯВОК
# ============================================================


@app.route("/tickets")
@login_required
def tickets_board():
    if not is_specialist():
        flash("Доступ запрещён", "error")
        return redirect("/")
    work_groups = (
        WorkGroup.query.filter_by(isactive=True).order_by(WorkGroup.group_name).all()
        if current_user.role == "admin"
        else []
    )
    return render_template("tickets.html", work_groups=work_groups)


def _task_queue_query(filter_name="all", performer_uid=None, work_group_uid=None):
    """
    Формирует выборку для доски заявок.

    Администратор видит все заявки, а руководители и специалисты —
    только заявки своих рабочих групп. Поверх этого накладываются
    фильтры по исполнителю и типу выборки.
    """
    query = Ticket.query.join(
        ServiceCatalog, Ticket.catalog_uid == ServiceCatalog.catalog_uid
    )
    if current_user.role == "admin":
        if work_group_uid:
            query = query.filter(ServiceCatalog.work_group_uid == work_group_uid)
        # Если админ не выбрал группу, значит специально смотрит вообще всё.
    else:
        wg_uids = _wg_uids(current_user)
        query = query.filter(ServiceCatalog.work_group_uid.in_(wg_uids))

    if filter_name == "my":
        query = query.filter(Ticket.performer_uid == current_user.user_uid)
    elif filter_name == "overdue":
        query = query.filter(
            Ticket.deadline_at != None,
            Ticket.deadline_at < datetime.utcnow(),
            ~Ticket.status.in_(CLOSED_TICKET_STATUSES),
        )
    if performer_uid:
        query = query.filter(Ticket.performer_uid == performer_uid)

    priority_sort = case(
        (Ticket.priority == "critical", 4),
        (Ticket.priority == "high", 3),
        (Ticket.priority == "medium", 2),
        else_=1,
    )
    return query.order_by(
        Ticket.deadline_at.asc().nullslast(),
        priority_sort.desc(),
        Ticket.created_at.asc(),
    )


@app.route("/api/tickets", methods=["GET"])
@login_required
def list_task_queue():
    if not is_specialist():
        return jsonify({"error": "Доступ запрещён"}), 403
    filter_name = request.args.get("filter", "all")
    if filter_name not in TASK_QUEUE_FILTERS:
        filter_name = "all"
    user_id = request.args.get("user_id") or None
    work_group_uid = request.args.get("work_group_uid") or None
    tickets = _task_queue_query(
        filter_name=filter_name, performer_uid=user_id, work_group_uid=work_group_uid
    ).all()
    return jsonify(
        [
            {
                "ticket_uid": t.ticket_uid,
                "ticket_number": t.ticket_number,
                "summary": t.summary,
                "status": t.status,
                "performer_uid": t.performer_uid,
                "performer": t.performer.full_name() if t.performer else "—",
                "deadline_at": t.deadline_at.isoformat() if t.deadline_at else None,
                "priority": t.priority,
                "is_overdue": t.is_overdue(),
            }
            for t in tickets
        ]
    )


# ============================================================
# СОЗДАНИЕ ЗАЯВКИ
# ============================================================


@app.route("/tickets/new", methods=["POST"])
@login_required
def create_ticket_form():
    """Создание заявки из обычной HTML-формы на главной странице."""
    catalog_uid = request.form.get("catalog_uid", "").strip()
    summary = request.form.get("summary", "").strip()
    description = request.form.get("description", "").strip()
    if not catalog_uid or not summary or not description:
        flash("Заполните все поля", "error")
        return redirect("/")
    catalog = ServiceCatalog.query.get(catalog_uid)
    if not catalog or not catalog.is_active or catalog.catalog_type == "category":
        flash("Услуга не найдена", "error")
        return redirect("/")

    # Для формы держим простой сценарий:
    # новая заявка создаётся от лица текущего пользователя,
    # а приоритет подтягиваем из выбранной услуги.
    ticket = Ticket(
        ticket_number=generate_ticket_number(),
        catalog_uid=catalog_uid,
        summary=summary,
        description=description,
        requester_uid=current_user.user_uid,
        recipient_uid=current_user.user_uid,
        status="new",
        priority=catalog.priority or "medium",
        created_by=current_user.user_uid,
        updated_by=current_user.user_uid,
    )
    if not catalog.approval_required:
        _start_ticket_sla(ticket, catalog)
    db.session.add(ticket)
    db.session.flush()
    add_ticket_history(ticket.ticket_uid, "status", None, "new", current_user.user_uid)

    if catalog.approval_required:
        create_approval_chain(ticket, catalog, current_user)
    else:
        notify_ticket_update(
            ticket,
            f"Создана новая заявка {ticket.ticket_number}",
            exclude_uid=current_user.user_uid,
        )

    db.session.commit()
    flash(f"Заявка {ticket.ticket_number} создана", "success")
    return redirect(f"/ticket/{ticket.ticket_uid}")


# ============================================================
# ПОЛУЧЕНИЕ ДАННЫХ ПО ЗАЯВКЕ
# ============================================================


@app.route("/ticket/<ticket_uid>")
@login_required
def ticket_detail(ticket_uid):
    ticket = Ticket.query.get_or_404(ticket_uid)
    if not _can_view_ticket(ticket):
        flash("Доступ запрещён", "error")
        return redirect("/")
    specialists = (
        User.query.join(UserRole)
        .filter(UserRole.role.in_(["specialist", "manager", "admin"]))
        .all()
        if current_user.role in ("admin", "manager")
        else []
    )
    comments = (
        ticket.param_values.filter(TicketParamValue.param_type == "comment")
        .order_by(TicketParamValue.create_date)
        .all()
    )
    pending_approvals = (
        ticket.approvals.filter_by(status="pending")
        .order_by(TicketApproval.step_order)
        .all()
    )
    my_pending_approval = next(
        (
            approval
            for approval in pending_approvals
            if approval.approver_uid == current_user.user_uid
        ),
        None,
    )
    can_update_ticket = (
        current_user.role in ("specialist", "manager", "admin")
        and ticket.status != "pending_approval"
    )
    return render_template(
        "ticket_detail.html",
        ticket=ticket,
        specialists=specialists,
        comments=comments,
        pending_approvals=pending_approvals,
        my_pending_approval=my_pending_approval,
        can_update_ticket=can_update_ticket,
    )


@app.route("/api/tickets/<ticket_uid>", methods=["GET"])
@login_required
def get_ticket(ticket_uid):
    """Возвращает полную карточку заявки для фронтенда."""
    ticket = Ticket.query.get_or_404(ticket_uid)
    if not _can_view_ticket(ticket):
        return jsonify({"error": "Доступ запрещён"}), 403

    comments = []
    # Внутренние заметки обычному пользователю не показываем.
    # Иначе он увидит служебную кухню, которая ему вообще не нужна.
    for pv in (
        ticket.param_values.filter(
            TicketParamValue.param_type.in_(["comment", "internal_comment"])
        )
        .order_by(TicketParamValue.create_date)
        .all()
    ):
        if pv.param_type == "internal_comment" and not is_specialist():
            continue
        comments.append(
            {
                "uid": pv.param_value_uid,
                "author": pv.author_rel.full_name() if pv.author_rel else "—",
                "author_uid": pv.author_uid,
                "text": pv.param_value,
                "is_internal": pv.param_type == "internal_comment",
                "created_at": pv.create_date.strftime("%d.%m.%Y %H:%M"),
            }
        )

    history = [
        {
            "field": h.field_name,
            "old": h.old_value,
            "new": h.new_value,
            "by": h.changer.full_name() if h.changer else "—",
            "date": h.changed_date.strftime("%d.%m.%Y %H:%M"),
        }
        for h in ticket.history.order_by(TicketHistory.changed_date).all()
    ]

    approvals = [
        {
            "uid": a.approval_uid,
            "step": approval_step_label_filter(a.step_name) or f"Шаг {a.step_order}",
            "approver": a.approver.full_name() if a.approver else "Руководитель",
            "approver_uid": a.approver_uid,
            "status": a.status,
            "comment": a.comment,
            "decided_at": (
                a.decided_at.strftime("%d.%m.%Y %H:%M") if a.decided_at else None
            ),
        }
        for a in ticket.approvals.order_by(TicketApproval.step_order).all()
    ]

    my_approval = None
    if ticket.status == "pending_approval":
        my_approval = TicketApproval.query.filter_by(
            ticket_uid=ticket_uid,
            approver_uid=current_user.user_uid,
            status="pending",
        ).first()

    return jsonify(
        {
            "ticket_uid": ticket.ticket_uid,
            "ticket_number": ticket.ticket_number,
            "summary": ticket.summary,
            "description": ticket.description,
            "status": ticket.status,
            "priority": ticket.priority,
            "catalog": ticket.catalog.catalog_name if ticket.catalog else "—",
            "catalog_uid": ticket.catalog_uid,
            "requester": ticket.requester.full_name() if ticket.requester else "—",
            "requester_uid": ticket.requester_uid,
            "performer": ticket.performer.full_name() if ticket.performer else None,
            "performer_uid": ticket.performer_uid,
            "response_due_at": (
                ticket.response_due_at.strftime("%d.%m.%Y %H:%M")
                if ticket.response_due_at
                else None
            ),
            "responded_at": (
                ticket.responded_at.strftime("%d.%m.%Y %H:%M")
                if ticket.responded_at
                else None
            ),
            "deadline": (
                ticket.deadline_at.strftime("%d.%m.%Y %H:%M")
                if ticket.deadline_at
                else None
            ),
            "is_overdue": ticket.is_overdue(),
            "created_at": ticket.created_at.strftime("%d.%m.%Y %H:%M"),
            "updated_at": ticket.updated_at.strftime("%d.%m.%Y %H:%M"),
            "resolved_at": (
                ticket.resolved_at.strftime("%d.%m.%Y %H:%M")
                if ticket.resolved_at
                else None
            ),
            "comments": comments,
            "history": history,
            "approvals": approvals,
            "can_edit": _can_edit_ticket(ticket),
            "can_assign": current_user.role in ("admin", "manager"),
            "can_approve": my_approval is not None,
            "my_approval_uid": my_approval.approval_uid if my_approval else None,
        }
    )


# ============================================================
# ИЗМЕНЕНИЕ ЗАЯВКИ
# ============================================================


@app.route("/ticket/<ticket_uid>/update", methods=["POST"])
@login_required
def update_ticket_form(ticket_uid):
    ticket = Ticket.query.get_or_404(ticket_uid)
    if not _can_edit_ticket(ticket):
        flash("Доступ запрещён", "error")
        return redirect(f"/ticket/{ticket_uid}")
    if ticket.status == "pending_approval":
        flash("Пока заявка на согласовании, обычное обновление недоступно", "error")
        return redirect(f"/ticket/{ticket_uid}")
    new_status = request.form.get("status")
    new_performer = request.form.get("performer_uid") or None
    now = datetime.utcnow()
    if new_status and new_status != ticket.status:
        add_ticket_history(
            ticket_uid, "status", ticket.status, new_status, current_user.user_uid
        )
        ticket.status = new_status
        _assign_current_user_on_take(ticket, new_status, now)
        _apply_ticket_status_dates(ticket, new_status, now)
    if "performer_uid" in request.form and new_performer != ticket.performer_uid:
        add_ticket_history(
            ticket_uid,
            "performer",
            ticket.performer_uid,
            new_performer,
            current_user.user_uid,
        )
        ticket.performer_uid = new_performer
        if new_performer:
            _mark_ticket_responded(ticket, now)
    ticket.updated_at = now
    ticket.updated_by = current_user.user_uid
    db.session.commit()
    flash("Заявка обновлена", "success")
    return redirect(f"/ticket/{ticket_uid}")


@app.route("/ticket/<ticket_uid>/approve", methods=["POST"])
@login_required
def approve_ticket_form(ticket_uid):
    ticket = Ticket.query.get_or_404(ticket_uid)
    approval_uid = request.form.get("approval_uid")
    decision = request.form.get("decision")
    comment = (request.form.get("comment") or "").strip()

    approval = TicketApproval.query.filter_by(
        ticket_uid=ticket_uid,
        approval_uid=approval_uid,
        approver_uid=current_user.user_uid,
        status="pending",
    ).first()
    if not approval:
        flash("Согласование для вас не найдено", "error")
        return redirect(f"/ticket/{ticket_uid}")

    try:
        process_approval_decision(
            ticket, approval, decision, comment, current_user.user_uid
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(f"/ticket/{ticket_uid}")

    flash(
        (
            "Решение по согласованию сохранено"
            if decision == "approved"
            else "Заявка отклонена"
        ),
        "success",
    )
    return redirect(f"/ticket/{ticket_uid}")


@app.route("/api/tickets/<ticket_uid>/update", methods=["POST"])
@login_required
def update_ticket(ticket_uid):
    """
    Универсальный JSON-маршрут изменения заявки.

    Через параметр action обрабатываются разные операции:
    взять заявку, назначить исполнителя, сменить статус, согласовать,
    отредактировать поля или удалить запись.
    """
    ticket = Ticket.query.get_or_404(ticket_uid)
    data = request.get_json() or {}
    action = data.get("action")

    # Для согласования отдельная логика прав:
    # там важно не "может редактировать", а "назначен ли человек на текущий шаг".
    # Все остальные изменения уже идут по обычной проверке доступа.
    if action != "approve" and not _can_edit_ticket(ticket):
        return jsonify({"error": "Доступ запрещён"}), 403
    if action != "approve" and ticket.status == "pending_approval":
        return (
            jsonify(
                {"error": "Пока заявка на согласовании, обычное обновление недоступно"}
            ),
            409,
        )
    now = datetime.utcnow()

    if action == "take":
        # Быстрый сценарий: специалист сам забирает заявку себе, без лишних окон.
        if not is_specialist():
            return jsonify({"error": "Только специалист может взять заявку"}), 403
        old_perf = ticket.performer_uid
        old_st = ticket.status
        ticket.performer_uid = current_user.user_uid
        ticket.status = "in_progress"
        add_ticket_history(
            ticket_uid,
            "performer",
            old_perf,
            current_user.user_uid,
            current_user.user_uid,
        )
        add_ticket_history(
            ticket_uid, "status", old_st, "in_progress", current_user.user_uid
        )
        _apply_ticket_status_dates(ticket, "in_progress", now)
        notify_ticket_update(
            ticket,
            f"Заявку {ticket.ticket_number} взял в работу {current_user.full_name()}",
            exclude_uid=current_user.user_uid,
        )

    elif action == "assign":
        # Тут уже ручное назначение, когда это делает руководитель или админ.
        if current_user.role not in ("admin", "manager"):
            return jsonify({"error": "Недостаточно прав"}), 403
        new_perf = data.get("performer_uid") or None
        if new_perf:
            member = (
                UserWorkGroup.query.join(
                    ServiceCatalog,
                    ServiceCatalog.work_group_uid == UserWorkGroup.work_group_uid,
                )
                .filter(
                    ServiceCatalog.catalog_uid == ticket.catalog_uid,
                    UserWorkGroup.user_uid == new_perf,
                )
                .first()
            )
            if not member:
                return (
                    jsonify(
                        {"error": "Исполнитель не состоит в рабочей группе заявки"}
                    ),
                    400,
                )
        old_st = ticket.status
        add_ticket_history(
            ticket_uid,
            "performer",
            ticket.performer_uid,
            new_perf,
            current_user.user_uid,
        )
        ticket.performer_uid = new_perf
        ticket.status = "in_progress" if new_perf else "new"
        _apply_ticket_status_dates(ticket, ticket.status, now)
        if old_st != ticket.status:
            add_ticket_history(
                ticket_uid, "status", old_st, ticket.status, current_user.user_uid
            )
        if new_perf:
            notify(
                new_perf,
                f"Вам назначена заявка {ticket.ticket_number}",
                ticket_uid=ticket.ticket_uid,
            )

    elif action == "status":
        # Отдельно пишем смену статуса в историю, чтобы потом не гадать, кто что менял.
        new_status = data.get("status")
        valid = [
            "new",
            "assigned",
            "in_progress",
            "on_hold",
            "pending_approval",
            "resolved",
            "closed",
            "cancelled",
        ]
        if new_status not in valid:
            return jsonify({"error": "Недопустимый статус"}), 400
        old_st = ticket.status
        add_ticket_history(
            ticket_uid, "status", old_st, new_status, current_user.user_uid
        )
        ticket.status = new_status
        _assign_current_user_on_take(ticket, new_status, now)
        _apply_ticket_status_dates(ticket, new_status, now)
        notify_ticket_update(
            ticket,
            f"Статус заявки {ticket.ticket_number} изменён",
            exclude_uid=current_user.user_uid,
        )

    elif action == "approve":
        # На этом шаге специально не доверяем фронту:
        # ещё раз убеждаемся, что решение отправляет именно нужный согласующий.
        approval_uid = data.get("approval_uid")
        decision = data.get("decision")
        comment = (data.get("comment") or "").strip()
        approval = TicketApproval.query.filter_by(
            ticket_uid=ticket_uid,
            approval_uid=approval_uid,
            approver_uid=current_user.user_uid,
            status="pending",
        ).first()
        if not approval:
            return jsonify({"error": "Запись согласования не найдена"}), 404
        process_approval_decision(
            ticket, approval, decision, comment, current_user.user_uid
        )

    elif action == "edit":
        # Это уже обычное редактирование карточки: тема, описание, исполнитель и всё такое.
        new_summary = (data.get("summary") or "").strip()
        new_desc = (data.get("description") or "").strip()
        new_prio = data.get("priority")
        if new_summary and new_summary != ticket.summary:
            add_ticket_history(
                ticket_uid,
                "summary",
                ticket.summary,
                new_summary,
                current_user.user_uid,
            )
            ticket.summary = new_summary
        if new_desc and new_desc != ticket.description:
            add_ticket_history(
                ticket_uid,
                "description",
                ticket.description[:80],
                new_desc[:80],
                current_user.user_uid,
            )
            ticket.description = new_desc
        if new_prio and new_prio != ticket.priority:
            add_ticket_history(
                ticket_uid, "priority", ticket.priority, new_prio, current_user.user_uid
            )
            ticket.priority = new_prio

    elif action == "delete":
        # Полное удаление — штука опасная, поэтому оставляем только админу.
        if current_user.role != "admin":
            return jsonify({"error": "Только администратор может удалять заявки"}), 403
        db.session.delete(ticket)
        db.session.commit()
        return jsonify({"success": True, "deleted": True})

    else:
        return jsonify({"error": "Неизвестное действие"}), 400

    ticket.updated_at = now
    ticket.updated_by = current_user.user_uid
    db.session.commit()
    return jsonify({"success": True})


@app.route("/tickets/<ticket_uid>/assign", methods=["POST"])
@login_required
def api_assign_ticket(ticket_uid):
    """Короткий API назначения исполнителя прямо с доски заявок."""
    if current_user.role not in ("admin", "manager"):
        return jsonify({"error": "Недостаточно прав"}), 403
    data = request.get_json() or {}
    performer_uid = data.get("performer_uid")
    if not performer_uid:
        return jsonify({"error": "performer_uid is required"}), 400
    ticket = Ticket.query.get_or_404(ticket_uid)
    member = (
        UserWorkGroup.query.join(
            ServiceCatalog,
            ServiceCatalog.work_group_uid == UserWorkGroup.work_group_uid,
        )
        .filter(
            ServiceCatalog.catalog_uid == ticket.catalog_uid,
            UserWorkGroup.user_uid == performer_uid,
        )
        .first()
    )
    if not member:
        return jsonify({"error": "Исполнитель не состоит в рабочей группе заявки"}), 400
    old_perf = ticket.performer_uid
    old_status = ticket.status
    ticket.performer_uid = performer_uid
    ticket.status = "in_progress"
    _apply_ticket_status_dates(ticket, "in_progress", datetime.utcnow())
    add_ticket_history(
        ticket.ticket_uid,
        "performer_uid",
        old_perf,
        performer_uid,
        current_user.user_uid,
    )
    if old_status != "in_progress":
        add_ticket_history(
            ticket.ticket_uid,
            "status",
            old_status,
            "in_progress",
            current_user.user_uid,
        )
    notify(
        performer_uid,
        f"Вы назначены исполнителем по заявке {ticket.ticket_number}",
        ticket.ticket_uid,
    )
    notify_ticket_update(
        ticket,
        f"Исполнитель назначен для заявки {ticket.ticket_number}",
        exclude_uid=current_user.user_uid,
    )
    ticket.updated_at = datetime.utcnow()
    ticket.updated_by = current_user.user_uid
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/tickets/<ticket_uid>/status", methods=["POST"])
@login_required
def api_ticket_status(ticket_uid):
    """Упрощённая смена статуса из таблицы заявок."""
    ticket = Ticket.query.get_or_404(ticket_uid)
    if not _can_edit_ticket(ticket):
        return jsonify({"error": "Доступ запрещён"}), 403
    new_status = (request.get_json() or {}).get("status")
    allowed = {
        "new",
        "assigned",
        "in_progress",
        "on_hold",
        "pending_approval",
        "resolved",
        "closed",
        "cancelled",
    }
    if new_status not in allowed:
        return jsonify({"error": "Недопустимый статус"}), 400
    old_status = ticket.status
    ticket.status = new_status
    change_time = datetime.utcnow()
    ticket.updated_at = change_time
    ticket.updated_by = current_user.user_uid
    if new_status == "new":
        ticket.performer_uid = None
    elif new_status == "assigned":
        if ticket.performer_uid is None:
            ticket.performer_uid = current_user.user_uid
    else:
        _assign_current_user_on_take(ticket, new_status, change_time)
    _apply_ticket_status_dates(ticket, new_status, change_time)
    add_ticket_history(
        ticket.ticket_uid, "status", old_status, new_status, current_user.user_uid
    )
    notify_ticket_update(
        ticket, f"Статус заявки изменён на {new_status}", current_user.user_uid
    )
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/tickets/<ticket_uid>/approve", methods=["POST"])
@login_required
def api_ticket_approve(ticket_uid):
    ticket = Ticket.query.get_or_404(ticket_uid)
    data = request.get_json() or {}
    decision = data.get("decision")
    comment = (data.get("comment") or "").strip()
    approval = (
        TicketApproval.query.filter_by(
            ticket_uid=ticket_uid,
            approver_uid=current_user.user_uid,
            status="pending",
        )
        .order_by(TicketApproval.step_order)
        .first()
    )
    if not approval:
        return jsonify({"error": "Запись согласования не найдена"}), 404
    try:
        process_approval_decision(
            ticket, approval, decision, comment, current_user.user_uid
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    ticket.updated_at = datetime.utcnow()
    ticket.updated_by = current_user.user_uid
    db.session.commit()
    return jsonify({"ok": True})


# ============================================================
# МАССОВЫЕ ДЕЙСТВИЯ ПО ЗАЯВКАМ
# ============================================================


@app.route("/api/tickets/bulk", methods=["POST"])
@login_required
def bulk_update_tickets():
    if not is_specialist():
        return jsonify({"error": "Доступ запрещён"}), 403
    data = request.get_json() or {}
    action = data.get("action")
    new_status = data.get("status")
    uids = data.get("ticket_uids", [])
    valid_statuses = ["in_progress", "on_hold", "resolved", "closed", "cancelled"]
    if action != "bulk_status":
        return jsonify({"error": "Неизвестное действие"}), 400
    if new_status not in valid_statuses:
        return jsonify({"error": "Недопустимый статус"}), 400
    if not uids:
        return jsonify({"error": "Нет заявок для обновления"}), 400
    now = datetime.utcnow()
    tickets = Ticket.query.filter(Ticket.ticket_uid.in_(uids)).all()
    updated = []
    skipped = []

    for ticket in tickets:
        if not _can_edit_ticket(ticket):
            skipped.append(
                {
                    "ticket_uid": ticket.ticket_uid,
                    "ticket_number": ticket.ticket_number,
                    "reason": "forbidden",
                }
            )
            continue
        if ticket.status == "pending_approval":
            skipped.append(
                {
                    "ticket_uid": ticket.ticket_uid,
                    "ticket_number": ticket.ticket_number,
                    "reason": "pending_approval",
                }
            )
            continue
        if ticket.status == new_status:
            skipped.append(
                {
                    "ticket_uid": ticket.ticket_uid,
                    "ticket_number": ticket.ticket_number,
                    "reason": "unchanged",
                }
            )
            continue

        old_status = ticket.status
        ticket.status = new_status
        ticket.updated_at = now
        ticket.updated_by = current_user.user_uid
        _apply_ticket_status_dates(ticket, new_status, now)
        add_ticket_history(
            ticket.ticket_uid,
            "status",
            old_status,
            new_status,
            current_user.user_uid,
        )
        notify_ticket_update(
            ticket,
            f"Статус заявки {ticket.ticket_number} изменён",
            exclude_uid=current_user.user_uid,
        )
        updated.append(ticket.ticket_uid)

    db.session.commit()
    return jsonify(
        {
            "success": True,
            "updated_count": len(updated),
            "updated_ticket_uids": updated,
            "skipped": skipped,
        }
    )


# ============================================================
# КОММЕНТАРИИ К ЗАЯВКЕ
# ============================================================


@app.route("/api/tickets/<ticket_uid>/comment", methods=["POST"])
@login_required
def add_comment(ticket_uid):
    ticket = Ticket.query.get_or_404(ticket_uid)
    if not _can_view_ticket(ticket):
        return jsonify({"error": "Доступ запрещён"}), 403
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Комментарий не может быть пустым"}), 400
    is_internal = data.get("is_internal", False) and is_specialist()
    ptype = "internal_comment" if is_internal else "comment"
    db.session.add(
        TicketParamValue(
            ticket_uid=ticket_uid,
            param_name="comment",
            param_value=text,
            param_type=ptype,
            author_uid=current_user.user_uid,
        )
    )
    ticket.updated_at = datetime.utcnow()
    ticket.updated_by = current_user.user_uid
    notify_ticket_update(
        ticket,
        f"Новый комментарий к заявке {ticket.ticket_number}",
        exclude_uid=current_user.user_uid,
    )
    db.session.commit()
    return jsonify(
        {
            "success": True,
            "comment": {
                "author": current_user.full_name(),
                "author_uid": current_user.user_uid,
                "text": text,
                "is_internal": is_internal,
                "created_at": datetime.utcnow().strftime("%d.%m.%Y %H:%M"),
            },
        }
    )


@app.route("/ticket/<ticket_uid>/add_comment", methods=["POST"])
@login_required
def add_comment_form(ticket_uid):
    ticket = Ticket.query.get_or_404(ticket_uid)
    if not _can_view_ticket(ticket):
        flash("Доступ запрещён", "error")
        return redirect(f"/ticket/{ticket_uid}")
    text = (request.form.get("text") or "").strip()
    if not text:
        flash("Комментарий не может быть пустым", "error")
        return redirect(f"/ticket/{ticket_uid}")
    db.session.add(
        TicketParamValue(
            ticket_uid=ticket_uid,
            param_name="comment",
            param_value=text,
            param_type="comment",
            author_uid=current_user.user_uid,
        )
    )
    ticket.updated_at = datetime.utcnow()
    ticket.updated_by = current_user.user_uid
    notify_ticket_update(
        ticket,
        f"Новый комментарий к заявке {ticket.ticket_number}",
        exclude_uid=current_user.user_uid,
    )
    db.session.commit()
    flash("Комментарий добавлен", "success")
    return redirect(f"/ticket/{ticket_uid}")


# ============================================================
# СПИСОК СПЕЦИАЛИСТОВ ДЛЯ ФОРМ И ФИЛЬТРОВ
# ============================================================


@app.route("/api/specialists")
@login_required
def get_specialists():
    if not is_specialist():
        return jsonify({"error": "Доступ запрещён"}), 403
    if current_user.role == "admin":
        users = (
            User.query.join(UserRole)
            .filter(
                UserRole.role.in_(["specialist", "manager", "admin"]),
                User.is_deactivated == False,
            )
            .order_by(User.last_name)
            .all()
        )
    else:
        users = (
            User.query.join(UserWorkGroup)
            .filter(UserWorkGroup.work_group_uid.in_(_wg_uids(current_user)))
            .join(UserRole, User.user_uid == UserRole.user_uid)
            .filter(
                UserRole.role.in_(["specialist", "manager"]),
                User.is_deactivated == False,
            )
            .order_by(User.last_name)
            .all()
        )
    return jsonify(
        [
            {
                "user_uid": u.user_uid,
                "full_name": u.full_name(),
                "role": u.role,
            }
            for u in users
        ]
    )


# ============================================================
# МОИ ЗАЯВКИ
# ============================================================


@app.route("/api/my-tickets")
@login_required
def my_tickets():
    tickets = (
        Ticket.query.filter(
            db.or_(
                Ticket.requester_uid == current_user.user_uid,
                Ticket.recipient_uid == current_user.user_uid,
            )
        )
        .order_by(Ticket.created_at.desc())
        .all()
    )
    return jsonify(
        [
            {
                "ticket_uid": t.ticket_uid,
                "ticket_number": t.ticket_number,
                "summary": t.summary,
                "status": t.status,
                "priority": t.priority,
                "catalog": t.catalog.catalog_name if t.catalog else "—",
                "created_at": t.created_at.strftime("%d.%m.%Y %H:%M"),
                "deadline": (
                    t.deadline_at.strftime("%d.%m.%Y %H:%M") if t.deadline_at else None
                ),
                "is_overdue": t.is_overdue(),
            }
            for t in tickets
        ]
    )


# ============================================================
# ДАШБОРД СПЕЦИАЛИСТА
# ============================================================


@app.route("/dashboard")
@login_required
def dashboard():
    if not is_specialist():
        return redirect("/")

    my_wg_uids = _wg_uids(current_user) if current_user.role != "admin" else None
    base = Ticket.query.join(
        ServiceCatalog, Ticket.catalog_uid == ServiceCatalog.catalog_uid
    )
    if my_wg_uids:
        base = base.filter(ServiceCatalog.work_group_uid.in_(my_wg_uids))

    # На дашборде держим только живые заявки.
    # Закрытые и отменённые тут уже просто шумят.
    all_tickets = (
        base.filter(~Ticket.status.in_(["closed", "cancelled"]))
        .order_by(Ticket.created_at.desc())
        .all()
    )

    # Разбрасываем заявки по колонкам канбана.
    # Тут логика чуть отличается от простого статуса, поэтому делаем вручную.
    kanban_data = {
        "new_unassigned": [],
        "new_assigned": [],
        "in_progress": [],
        "resolved": [],
    }
    for ticket in all_tickets:
        if ticket.status in ("new", "assigned"):
            if ticket.performer_uid is None:
                kanban_data["new_unassigned"].append(ticket)
            else:
                kanban_data["new_assigned"].append(ticket)
        elif ticket.status == "in_progress":
            kanban_data["in_progress"].append(ticket)
        elif ticket.status == "resolved":
            kanban_data["resolved"].append(ticket)

    # Пока порядок колонок фиксированный, так глазами проще ориентироваться.
    ordered_kanban_data = kanban_data

    my_active = (
        base.filter(
            Ticket.performer_uid == current_user.user_uid,
            Ticket.status.in_(["in_progress"]),
        )
        .order_by(Ticket.deadline_at.asc().nullslast())
        .all()
    )

    all_new = (
        base.filter(Ticket.status == "new", Ticket.performer_uid == None)
        .order_by(Ticket.created_at.desc())
        .limit(10)
        .all()
    )

    overdue_query = base.filter(
        Ticket.deadline_at != None,
        Ticket.deadline_at < datetime.utcnow(),
        ~Ticket.status.in_(CLOSED_TICKET_STATUSES),
    )
    overdue_list = overdue_query.order_by(Ticket.deadline_at).limit(20).all()

    pending_approval = []
    if current_user.role in ("admin", "manager"):
        pending_approval = (
            TicketApproval.query.filter_by(
                approver_uid=current_user.user_uid, status="pending"
            )
            .limit(10)
            .all()
        )

    stats = {
        "my_active": len(my_active),
        "all_new": len(kanban_data["new_unassigned"]),
        "overdue": overdue_query.count(),
        "pending_approval": len(pending_approval),
    }

    return render_template(
        "dashboard.html",
        my_active=my_active,
        all_new=all_new,
        all_overdue=overdue_list,
        pending_approval=pending_approval,
        stats=stats,
        kanban_data=ordered_kanban_data,
    )


# ============================================================
# АДМИНКА: ПОЛЬЗОВАТЕЛИ
# ============================================================


@app.route("/admin/users")
@login_required
def admin_users():
    if current_user.role != "admin":
        return _admin_forbidden()
    users = User.query.order_by(User.last_name, User.first_name).all()
    work_groups = WorkGroup.query.filter_by(isactive=True).all()
    return render_template("admin_users.html", users=users, work_groups=work_groups)


@app.route("/admin/create-user", methods=["GET", "POST"])
@login_required
def create_user():
    """Создание пользователя администратором."""
    if current_user.role != "admin":
        return _admin_forbidden()
    work_groups = WorkGroup.query.filter_by(isactive=True).all()
    all_users = (
        User.query.filter_by(is_deactivated=False).order_by(User.last_name).all()
    )

    if request.method == "POST":
        last_name = request.form["last_name"].strip()
        first_name = request.form["first_name"].strip()
        middle_name = request.form.get("middle_name", "").strip() or None
        email = request.form["email"].strip()
        mobile = request.form.get("mobile", "").strip() or None
        work_phone = request.form.get("work_phone", "").strip() or None
        gender = request.form.get("gender", "").strip() or None
        title = request.form.get("title", "").strip() or None
        department = request.form.get("department", "").strip() or None
        company = request.form.get("company", "").strip() or None
        role = request.form.get("role", "user")
        wg_uid = request.form.get("work_group_uid") or None
        manager_uid = request.form.get("manager_uid") or None

        # Эти данные возвращаем обратно в форму,
        # чтобы после ошибки не приходилось забивать всё заново.
        form_data = {
            "last_name": last_name,
            "first_name": first_name,
            "middle_name": middle_name or "",
            "email": email,
            "mobile": request.form.get("mobile", "").strip(),
            "work_phone": request.form.get("work_phone", "").strip(),
            "gender": gender or "",
            "title": title or "",
            "department": department or "",
            "company": company or "",
            "role": role,
            "work_group_uid": wg_uid or "",
            "manager_uid": manager_uid or "",
        }
        if User.query.filter_by(email=email).first():
            return render_template(
                "create_user.html",
                work_groups=work_groups,
                all_users=all_users,
                form_data=form_data,
                error="Пользователь с таким email уже существует",
            )
        try:
            user_name, temp_pw = create_user_db(
                last_name,
                first_name,
                middle_name,
                email,
                mobile,
                work_phone,
                gender,
                title,
                department,
                company,
                role=role,
                work_group_uid=wg_uid,
                manager_uid=manager_uid,
                creator_uid=current_user.user_uid,
            )
            db.session.commit()
            return render_template(
                "create_user.html",
                work_groups=work_groups,
                all_users=all_users,
                success=True,
                temp_login=user_name,
                temp_password=temp_pw,
            )
        except Exception as e:
            # Ошибку БД наружу не тащим как есть.
            # Пользователю всё равно важнее нормальное объяснение человеческими словами.
            db.session.rollback()
            err_str = str(e)
            if "value too long" in err_str or "StringDataRightTruncation" in err_str:
                error = "Одно из полей слишком длинное (телефон, должность и т.п.). Проверьте данные."
            elif "unique" in err_str.lower() or "UniqueViolation" in err_str:
                if "email" in err_str:
                    error = "Пользователь с таким email уже существует."
                elif "user_name" in err_str:
                    error = "Сгенерированный логин уже занят, попробуйте ещё раз."
                else:
                    error = "Нарушение уникальности: такие данные уже есть в системе."
            elif "not-null" in err_str.lower() or "null value" in err_str.lower():
                error = (
                    "Не заполнено обязательное поле. Проверьте Фамилию, Имя и Email."
                )
            else:
                error = "Ошибка при создании пользователя. Проверьте введённые данные."
            return render_template(
                "create_user.html",
                work_groups=work_groups,
                all_users=all_users,
                form_data=form_data,
                error=error,
            )

    return render_template(
        "create_user.html", work_groups=work_groups, all_users=all_users, error=None
    )


@app.route("/admin/edit-user/<user_uid>", methods=["GET", "POST"])
@login_required
def edit_user(user_uid):
    """Редактирование карточки пользователя."""
    if current_user.role != "admin":
        return _admin_forbidden()
    user = User.query.get_or_404(user_uid)
    work_groups = WorkGroup.query.filter_by(isactive=True).all()
    all_users = (
        User.query.filter(
            User.is_deactivated == False,
            User.user_uid != user_uid,
        )
        .order_by(User.last_name)
        .all()
    )

    if request.method == "POST":
        # Обязательные поля не даём случайно затереть пустотой.
        user.first_name = request.form.get("first_name", "").strip() or user.first_name
        user.last_name = request.form.get("last_name", "").strip() or user.last_name
        user.email = request.form.get("email", "").strip() or user.email
        # А вот необязательные поля можно спокойно обнулить.
        user.middel_name = request.form.get("middle_name", "").strip() or None
        user.mobile = format_mobile(request.form.get("mobile", "").strip()) or None
        user.work_phone = request.form.get("work_phone", "").strip() or None
        user.title = request.form.get("title", "").strip() or None
        user.department = request.form.get("department", "").strip() or None
        user.company = request.form.get("company", "").strip() or None
        user.manager_uid = request.form.get("manager_uid") or None
        user.is_deactivated = "is_deactivated" in request.form
        user.update_date = datetime.utcnow()
        user.update_by = current_user.user_uid
        _ensure_role(user.user_uid, request.form.get("role", user.role))
        new_wg = request.form.get("work_group_uid") or None
        if new_wg:
            UserWorkGroup.query.filter_by(user_uid=user.user_uid).delete()
            _ensure_work_group(user.user_uid, new_wg)
        db.session.commit()
        flash("Пользователь обновлён", "success")
        return redirect("/admin/users")

    return render_template(
        "edit_user.html", user=user, work_groups=work_groups, all_users=all_users
    )


@app.route("/admin/delete-user/<user_uid>", methods=["POST"])
@login_required
def delete_user(user_uid):
    """В учебной версии удаление реализовано как деактивация учётной записи."""
    if current_user.role != "admin":
        return _admin_forbidden()
    user = User.query.get_or_404(user_uid)
    if user.user_uid == current_user.user_uid:
        if request.is_json:
            return jsonify({"error": "Нельзя деактивировать себя"}), 400
        flash("Нельзя деактивировать себя", "error")
        return redirect("/admin/users")
    user.is_deactivated = True
    user.update_date = datetime.utcnow()
    db.session.commit()
    if request.is_json:
        return jsonify({"success": True})
    flash("Пользователь деактивирован", "success")
    return redirect("/admin/users")


@app.route("/admin/reset-password/<user_uid>", methods=["POST"])
@login_required
def admin_reset_password(user_uid):
    """Сбрасывает пароль и возвращает новый временный пароль пользователю."""
    if current_user.role != "admin":
        return _admin_forbidden()
    user = User.query.get_or_404(user_uid)
    new_pass = reset_password_db(user.user_name)
    if not new_pass:
        return jsonify({"error": "Не удалось сбросить пароль"}), 500
    return jsonify({"success": True, "new_password": new_pass})


# ============================================================
# АДМИНКА: КАТАЛОГ УСЛУГ
# ============================================================


@app.route("/admin/categories")
@login_required
def admin_categories():
    """Страница управления категориями и услугами каталога."""
    if current_user.role != "admin":
        return _admin_forbidden()
    cats = (
        ServiceCatalog.query.filter_by(parent_uid=None)
        .order_by(ServiceCatalog.catalog_name)
        .all()
    )
    for c in cats:
        c._services = c.children.order_by(ServiceCatalog.catalog_name).all()
    work_groups = WorkGroup.query.filter_by(isactive=True).all()
    slas = SlaPolicy.query.filter_by(is_active=True).all()
    return render_template(
        "admin_categories.html", categories=cats, work_groups=work_groups, slas=slas
    )


@app.route("/admin/create-category", methods=["GET", "POST"])
@login_required
def create_category():
    """Создание категории верхнего уровня или отдельной услуги."""
    if current_user.role != "admin":
        return _admin_forbidden()
    work_groups = WorkGroup.query.filter_by(isactive=True).all()
    top_cats = ServiceCatalog.query.filter_by(
        catalog_type="category", parent_uid=None, is_active=True
    ).all()
    slas = SlaPolicy.query.filter_by(is_active=True).all()

    if request.method == "POST":
        name = request.form["catalog_name"].strip()
        desc = request.form.get("catalog_description", "").strip() or None
        wg_uid = request.form.get("work_group_uid") or None
        parent_uid = request.form.get("parent_uid") or None
        ttype = request.form.get("ticket_type", "service_request")
        prio = request.form.get("priority", "medium")
        sla_uid = request.form.get("sla_uid") or None
        appr = "approval_required" in request.form
        # Если выбран родитель, значит это уже не верхний раздел, а конкретная услуга внутри него.
        cat_type = "service" if parent_uid else "category"
        db.session.add(
            ServiceCatalog(
                catalog_name=name,
                catalog_path=f'/{name.replace(" ", "_")}',
                catalog_type=cat_type,
                catalog_description=desc,
                work_group_uid=wg_uid,
                parent_uid=parent_uid,
                ticket_type=ttype,
                priority=prio,
                sla_uid=sla_uid,
                approval_required=appr,
                create_by=current_user.user_uid,
            )
        )
        db.session.commit()
        flash("Запись каталога создана", "success")
        return redirect("/admin/categories")

    return render_template(
        "create_category.html",
        work_groups=work_groups,
        top_cats=top_cats,
        slas=slas,
        error=None,
    )


@app.route("/admin/edit-category/<cat_uid>", methods=["GET", "POST"])
@login_required
def edit_category(cat_uid):
    """Редактирование существующей записи каталога."""
    if current_user.role != "admin":
        return _admin_forbidden()
    cat = ServiceCatalog.query.get_or_404(cat_uid)
    work_groups = WorkGroup.query.filter_by(isactive=True).all()
    top_cats = ServiceCatalog.query.filter(
        ServiceCatalog.catalog_type == "category",
        ServiceCatalog.parent_uid == None,
        ServiceCatalog.catalog_uid != cat_uid,
    ).all()
    slas = SlaPolicy.query.filter_by(is_active=True).all()

    if request.method == "POST":
        cat.catalog_name = (
            request.form.get("catalog_name", "").strip() or cat.catalog_name
        )
        cat.catalog_description = (
            request.form.get("catalog_description", "").strip()
            or cat.catalog_description
        )
        cat.work_group_uid = request.form.get("work_group_uid") or cat.work_group_uid
        cat.parent_uid = request.form.get("parent_uid") or cat.parent_uid
        cat.ticket_type = request.form.get("ticket_type", cat.ticket_type)
        cat.priority = request.form.get("priority", cat.priority)
        cat.sla_uid = request.form.get("sla_uid") or cat.sla_uid
        cat.approval_required = "approval_required" in request.form
        cat.is_active = "is_active" in request.form
        cat.update_date = datetime.utcnow()
        cat.update_by = current_user.user_uid
        db.session.commit()
        flash("Запись обновлена", "success")
        return redirect("/admin/categories")

    return render_template(
        "edit_category.html",
        cat=cat,
        work_groups=work_groups,
        top_cats=top_cats,
        slas=slas,
    )


@app.route("/admin/delete-category/<cat_uid>", methods=["POST"])
@login_required
def delete_category(cat_uid):
    """
    Удаление записи каталога.

    Если на категорию или услугу уже ссылаются заявки, удаление запрещаем,
    чтобы не ломать историю работы системы.
    """
    if current_user.role != "admin":
        return _admin_forbidden()
    cat = ServiceCatalog.query.get_or_404(cat_uid)
    ticket_count = Ticket.query.filter_by(catalog_uid=cat_uid).count()
    if ticket_count > 0:
        msg = (
            f"Нельзя удалить: к этой категории привязано {ticket_count} заявок. "
            f"Сначала скройте её (глазик), чтобы запретить новые заявки."
        )
        if request.is_json:
            return jsonify({"error": msg}), 400
        flash(msg, "error")
        return redirect("/admin/categories")
    # Для верхнего раздела отдельно смотрим вложенные услуги,
    # чтобы не снести кусок каталога, который ещё где-то используется.
    for child in cat.children.all():
        if Ticket.query.filter_by(catalog_uid=child.catalog_uid).count() == 0:
            db.session.delete(child)
        else:
            msg = f"Нельзя удалить: дочерняя услуга «{child.catalog_name}» имеет привязанные заявки."
            if request.is_json:
                return jsonify({"error": msg}), 400
            flash(msg, "error")
            return redirect("/admin/categories")
    db.session.delete(cat)
    db.session.commit()
    if request.is_json:
        return jsonify({"success": True})
    flash("Категория деактивирована/удалена", "success")
    return redirect("/admin/categories")


# ============================================================
# АДМИНКА: РАБОЧИЕ ГРУППЫ
# ============================================================


@app.route("/admin/work-groups")
@login_required
def admin_work_groups():
    if current_user.role != "admin":
        return _admin_forbidden()
    groups = WorkGroup.query.order_by(WorkGroup.group_name).all()
    return render_template("admin_work_groups.html", groups=groups)


@app.route("/admin/create-work-group", methods=["POST"])
@login_required
def create_work_group():
    if current_user.role != "admin":
        return _admin_forbidden()
    data = request.get_json() if request.is_json else request.form
    name = (data.get("group_name") or "").strip()
    desc = (data.get("group_description") or "").strip() or None
    if not name:
        if request.is_json:
            return jsonify({"error": "Название обязательно"}), 400
        flash("Название обязательно", "error")
        return redirect("/admin/work-groups")
    if WorkGroup.query.filter_by(group_name=name).first():
        if request.is_json:
            return jsonify({"error": "Группа уже существует"}), 400
        flash("Группа уже существует", "error")
        return redirect("/admin/work-groups")
    wg = WorkGroup(
        group_name=name, group_description=desc, create_by=current_user.user_uid
    )
    db.session.add(wg)
    db.session.commit()
    if request.is_json:
        return jsonify({"success": True, "work_group_uid": wg.work_group_uid})
    flash("Рабочая группа создана", "success")
    return redirect("/admin/work-groups")


@app.route("/admin/delete-work-group/<wg_uid>", methods=["POST"])
@login_required
def delete_work_group(wg_uid):
    if current_user.role != "admin":
        return _admin_forbidden()
    wg = WorkGroup.query.get_or_404(wg_uid)
    has_catalog = ServiceCatalog.query.filter_by(work_group_uid=wg_uid).first()
    if has_catalog:
        if request.is_json:
            return (
                jsonify({"error": "Нельзя удалить группу: есть связанные услуги"}),
                400,
            )
        flash("Нельзя удалить группу: есть связанные услуги", "error")
        return redirect("/admin/work-groups")
    UserWorkGroup.query.filter_by(work_group_uid=wg_uid).delete()
    db.session.delete(wg)
    db.session.commit()
    if request.is_json:
        return jsonify({"success": True})
    flash("Рабочая группа удалена", "success")
    return redirect("/admin/work-groups")


# ============================================================
# ЗАПУСК ПРИЛОЖЕНИЯ
# ============================================================

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=5000)

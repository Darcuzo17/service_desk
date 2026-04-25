"""
Модели и вспомогательные функции для учебного проекта Service Desk.

В файле находятся:
- ORM-модели таблиц схемы `sm`;
- вспомогательные функции для работы с пользователями и заявками;
- простые Python-аналоги части логики, которая в промышленной системе
  могла бы жить в хранимых процедурах PostgreSQL.
"""

import uuid
import re
import random
import string
from datetime import datetime, timedelta

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from sqlalchemy import text, func
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


def gen_uuid():
    """Генерирует UUID в строковом формате для первичных ключей."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# sm.users
# ---------------------------------------------------------------------------
class User(UserMixin, db.Model):
    """Пользователь системы Service Desk."""

    __tablename__ = "users"
    __table_args__ = {"schema": "sm"}

    user_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    user_name = db.Column(db.String(12), unique=True, nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    middel_name = db.Column(db.String(100), nullable=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    mobile = db.Column(db.String(20), nullable=True)
    work_phone = db.Column(db.String(20), nullable=True)
    gender = db.Column(db.String(1), nullable=True)
    title = db.Column(db.String(255), nullable=True)
    department = db.Column(db.String(255), nullable=True)
    company = db.Column(db.String(255), nullable=True)
    manager_uid = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=True
    )
    work_status = db.Column(db.String(20), nullable=True)
    is_vip = db.Column(db.Boolean, default=False)
    is_deactivated = db.Column(db.Boolean, default=False)
    is_temp_deactivated = db.Column(db.Boolean, default=False)
    last_loggon_date = db.Column(db.DateTime, nullable=True)
    password_expires = db.Column(db.DateTime, nullable=True)
    create_date = db.Column(db.DateTime, default=datetime.utcnow)
    update_date = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    create_by = db.Column(db.String(36), nullable=False)
    update_by = db.Column(db.String(36), nullable=True)

    password_record = db.relationship(
        "Password", backref="user", uselist=False, foreign_keys="Password.user_uid"
    )
    work_group_links = db.relationship(
        "UserWorkGroup",
        backref="user",
        lazy="dynamic",
        foreign_keys="UserWorkGroup.user_uid",
    )
    role_record = db.relationship(
        "UserRole", backref="user", uselist=False, foreign_keys="UserRole.user_uid"
    )

    tickets_as_requester = db.relationship(
        "Ticket",
        foreign_keys="Ticket.requester_uid",
        backref="requester",
        lazy="dynamic",
    )
    tickets_as_recipient = db.relationship(
        "Ticket",
        foreign_keys="Ticket.recipient_uid",
        backref="recipient",
        lazy="dynamic",
    )
    tickets_as_performer = db.relationship(
        "Ticket",
        foreign_keys="Ticket.performer_uid",
        backref="performer",
        lazy="dynamic",
    )

    def get_id(self):
        """Flask-Login использует это значение как идентификатор сессии."""
        return str(self.user_uid)

    def full_name(self):
        """Собирает ФИО пользователя в одну строку."""
        parts = [self.last_name, self.first_name]
        if self.middel_name:
            parts.append(self.middel_name)
        return " ".join(parts)

    @property
    def role(self):
        """Возвращает роль пользователя; по умолчанию это обычный user."""
        if self.role_record:
            return self.role_record.role
        return "user"

    @property
    def is_active(self):
        """Пользователь считается активным, если его учётная запись не отключена."""
        return not self.is_deactivated and not self.is_temp_deactivated

    def primary_work_group(self):
        """Возвращает основную рабочую группу пользователя."""
        primary_link = self.work_group_links.filter_by(is_primary=True).first()
        if primary_link:
            return primary_link.work_group
        fallback_link = self.work_group_links.first()
        if fallback_link:
            return fallback_link.work_group
        return None

    def all_work_groups(self):
        """Возвращает все рабочие группы пользователя в порядке назначения."""
        return [
            work_group_link.work_group
            for work_group_link in self.work_group_links.order_by(
                UserWorkGroup.assigned_date
            ).all()
        ]


# ---------------------------------------------------------------------------
# sm.passwords
# ---------------------------------------------------------------------------
class Password(db.Model):
    """Хранение хэша пароля и технических флагов авторизации."""

    __tablename__ = "passwords"
    __table_args__ = {"schema": "sm"}

    user_uid = db.Column(
        db.String(36),
        db.ForeignKey("sm.users.user_uid"),
        primary_key=True,
        nullable=False,
    )
    passwordhash = db.Column(db.Text, nullable=True)
    # Эти поля нужны уже для логики приложения.
    # Через них удобно понять, первый ли это вход и надо ли гнать пользователя менять пароль.
    is_first_login = db.Column(db.Boolean, default=True)
    must_change_password = db.Column(db.Boolean, default=False)
    failed_attempts = db.Column(db.Integer, default=0)


# ---------------------------------------------------------------------------
# РОЛИ ПОЛЬЗОВАТЕЛЕЙ
# ---------------------------------------------------------------------------
class UserRole(db.Model):
    """Роль пользователя внутри системы."""

    __tablename__ = "user_roles"
    __table_args__ = {"schema": "sm"}

    user_uid = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), primary_key=True
    )
    role = db.Column(db.String(32), nullable=False, default="user")


# ---------------------------------------------------------------------------
# sm.work_groups
# ---------------------------------------------------------------------------
class WorkGroup(db.Model):
    """Рабочая группа, которая обслуживает заявки своего направления."""

    __tablename__ = "work_groups"
    __table_args__ = {"schema": "sm"}

    work_group_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    group_name = db.Column(db.String(100), nullable=False)
    isactive = db.Column(db.Boolean, default=True)
    group_description = db.Column(db.Text, nullable=True)
    group_owner_uid = db.Column(db.String(36), nullable=True)
    create_date = db.Column(db.DateTime, default=datetime.utcnow)
    update_date = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    create_by = db.Column(db.String(36), nullable=False)
    update_by = db.Column(db.String(36), nullable=True)

    member_links = db.relationship(
        "UserWorkGroup",
        backref="work_group",
        lazy="dynamic",
        foreign_keys="UserWorkGroup.work_group_uid",
    )
    catalog_items = db.relationship(
        "ServiceCatalog",
        backref="work_group",
        lazy="dynamic",
        foreign_keys="ServiceCatalog.work_group_uid",
    )


# ---------------------------------------------------------------------------
# sm.user_work_groups
# ---------------------------------------------------------------------------
class UserWorkGroup(db.Model):
    """Связь пользователя с одной или несколькими рабочими группами."""

    __tablename__ = "user_work_groups"
    __table_args__ = {"schema": "sm"}

    user_uid = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), primary_key=True
    )
    work_group_uid = db.Column(
        db.String(36), db.ForeignKey("sm.work_groups.work_group_uid"), primary_key=True
    )
    assigned_date = db.Column(db.DateTime, default=datetime.utcnow)
    is_primary = db.Column(db.Boolean, default=False)


# ---------------------------------------------------------------------------
# sm.sla_policies
# ---------------------------------------------------------------------------
class SlaPolicy(db.Model):
    """Политика SLA: время реакции и время решения заявки."""

    __tablename__ = "sla_policies"
    __table_args__ = {"schema": "sm"}

    sla_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    policy_name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    response_time_hours = db.Column(db.Integer, nullable=False, default=8)
    resolution_time_hours = db.Column(db.Integer, nullable=False, default=24)
    is_active = db.Column(db.Boolean, default=True)
    create_date = db.Column(db.DateTime, default=datetime.utcnow)
    create_by = db.Column(db.String(36), nullable=False)


# ---------------------------------------------------------------------------
# sm.service_catalog
# ---------------------------------------------------------------------------
class ServiceCatalog(db.Model):
    """Каталог услуг и категорий, доступных пользователю при создании заявки."""

    __tablename__ = "service_catalog"
    __table_args__ = {"schema": "sm"}

    catalog_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    catalog_name = db.Column(db.String(200), nullable=False)
    catalog_path = db.Column(db.Text, nullable=False)
    parent_uid = db.Column(
        db.String(36), db.ForeignKey("sm.service_catalog.catalog_uid"), nullable=True
    )
    catalog_type = db.Column(db.String(50), nullable=False, default="category")
    work_group_uid = db.Column(
        db.String(36), db.ForeignKey("sm.work_groups.work_group_uid"), nullable=True
    )
    ticket_type = db.Column(db.String(100), default="service_request")
    priority = db.Column(db.String(20), default="medium")
    approval_required = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    sla_uid = db.Column(
        db.String(36), db.ForeignKey("sm.sla_policies.sla_uid"), nullable=True
    )
    create_date = db.Column(db.DateTime, default=datetime.utcnow)
    update_date = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    create_by = db.Column(db.String(36), nullable=False)
    update_by = db.Column(db.String(36), nullable=True)
    catalog_description = db.Column(db.Text, nullable=True)

    children = db.relationship(
        "ServiceCatalog",
        backref=db.backref("parent", remote_side="ServiceCatalog.catalog_uid"),
        lazy="dynamic",
    )
    tickets = db.relationship(
        "Ticket", backref="catalog", lazy="dynamic", foreign_keys="Ticket.catalog_uid"
    )
    sla = db.relationship("SlaPolicy", backref="catalog_items", foreign_keys=[sla_uid])


# ---------------------------------------------------------------------------
# sm.tickets
# ---------------------------------------------------------------------------
class Ticket(db.Model):
    """Основная сущность системы — заявка пользователя."""

    __tablename__ = "tickets"
    __table_args__ = {"schema": "sm"}

    ticket_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    ticket_number = db.Column(db.String(50), unique=True, nullable=False)
    catalog_uid = db.Column(
        db.String(36), db.ForeignKey("sm.service_catalog.catalog_uid"), nullable=False
    )
    summary = db.Column(db.String(500), nullable=False)
    description = db.Column(db.Text, nullable=False)
    requester_uid = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=False
    )
    recipient_uid = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=False
    )
    performer_uid = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=True
    )
    status = db.Column(db.String(50), default="new", nullable=False)
    priority = db.Column(db.String(20), default="medium")
    response_due_at = db.Column(db.DateTime, nullable=True)
    responded_at = db.Column(db.DateTime, nullable=True)
    deadline_at = db.Column(db.DateTime, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    created_by = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=False
    )
    updated_by = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=True
    )

    history = db.relationship(
        "TicketHistory",
        backref="ticket",
        lazy="dynamic",
        cascade="all, delete-orphan",
        foreign_keys="TicketHistory.ticket_uid",
    )
    param_values = db.relationship(
        "TicketParamValue",
        backref="ticket",
        lazy="dynamic",
        cascade="all, delete-orphan",
        foreign_keys="TicketParamValue.ticket_uid",
    )

    creator = db.relationship("User", foreign_keys=[created_by])

    approvals = db.relationship(
        "TicketApproval",
        backref="ticket",
        lazy="dynamic",
        cascade="all, delete-orphan",
        foreign_keys="TicketApproval.ticket_uid",
    )

    def is_overdue(self):
        """Проверяет, просрочена ли заявка относительно рассчитанного дедлайна."""
        if not self.deadline_at or self.status in ("resolved", "closed", "cancelled"):
            return False
        current_time = (
            datetime.now(self.deadline_at.tzinfo)
            if self.deadline_at.tzinfo
            else datetime.utcnow()
        )
        return self.deadline_at < current_time


# ---------------------------------------------------------------------------
# sm.ticket_history
# ---------------------------------------------------------------------------
class TicketHistory(db.Model):
    """История изменений полей заявки."""

    __tablename__ = "ticket_history"
    __table_args__ = {"schema": "sm"}

    history_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    ticket_uid = db.Column(
        db.String(36),
        db.ForeignKey("sm.tickets.ticket_uid", ondelete="CASCADE"),
        nullable=False,
    )
    field_name = db.Column(db.String(100), nullable=False)
    old_value = db.Column(db.Text, nullable=True)
    new_value = db.Column(db.Text, nullable=True)
    changed_by = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=False
    )
    changed_date = db.Column(db.DateTime, default=datetime.utcnow)

    changer = db.relationship("User", foreign_keys=[changed_by])


# ---------------------------------------------------------------------------
# sm.ticket_param_values  (комментарии, решения по согласованию, служебные заметки)
# ---------------------------------------------------------------------------
class TicketParamValue(db.Model):
    """Гибкие параметры заявки: комментарии, согласования, служебные заметки."""

    __tablename__ = "ticket_param_values"
    __table_args__ = {"schema": "sm"}

    param_value_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    ticket_uid = db.Column(
        db.String(36),
        db.ForeignKey("sm.tickets.ticket_uid", ondelete="CASCADE"),
        nullable=False,
    )
    param_name = db.Column(db.String(100), nullable=False)
    param_value = db.Column(db.Text, nullable=True)
    param_type = db.Column(db.String(50), nullable=True)
    author_uid = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=True
    )
    create_date = db.Column(db.DateTime, default=datetime.utcnow)

    author_rel = db.relationship("User", foreign_keys=[author_uid])


class ApprovalRoute(db.Model):
    """Маршрут согласования для конкретной услуги каталога."""

    __tablename__ = "approval_routes"
    __table_args__ = {"schema": "sm"}

    route_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    catalog_uid = db.Column(
        db.String(36), db.ForeignKey("sm.service_catalog.catalog_uid"), nullable=False
    )
    route_name = db.Column(db.String(200), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    create_date = db.Column(db.DateTime, default=datetime.utcnow)
    create_by = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=False
    )

    steps = db.relationship(
        "ApprovalStep",
        backref="route",
        lazy="dynamic",
        cascade="all, delete-orphan",
        foreign_keys="ApprovalStep.route_uid",
    )


class ApprovalStep(db.Model):
    """Один шаг маршрута согласования."""

    __tablename__ = "approval_steps"
    __table_args__ = {"schema": "sm"}

    step_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    route_uid = db.Column(
        db.String(36), db.ForeignKey("sm.approval_routes.route_uid"), nullable=False
    )
    step_order = db.Column(db.Integer, nullable=False, default=1)
    step_name = db.Column(db.String(200), nullable=True)
    approver_uid = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=True
    )
    approver_role = db.Column(db.String(32), nullable=True)

    approver = db.relationship("User", foreign_keys=[approver_uid])


class TicketApproval(db.Model):
    """Экземпляр шага согласования, созданный уже для конкретной заявки."""

    __tablename__ = "ticket_approvals"
    __table_args__ = {"schema": "sm"}

    approval_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    ticket_uid = db.Column(
        db.String(36),
        db.ForeignKey("sm.tickets.ticket_uid", ondelete="CASCADE"),
        nullable=False,
    )
    step_order = db.Column(db.Integer, nullable=False, default=1)
    step_name = db.Column(db.String(200), nullable=True)
    approver_uid = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=True
    )
    status = db.Column(db.String(20), nullable=False, default="pending")
    comment = db.Column(db.Text, nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    create_date = db.Column(db.DateTime, default=datetime.utcnow)

    approver = db.relationship("User", foreign_keys=[approver_uid])


class Notification(db.Model):
    """Уведомления для пользователей о действиях по заявкам."""

    __tablename__ = "notifications"
    __table_args__ = {"schema": "sm"}

    notification_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    user_uid = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=False
    )
    ticket_uid = db.Column(
        db.String(36), db.ForeignKey("sm.tickets.ticket_uid"), nullable=True
    )
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    create_date = db.Column(db.DateTime, default=datetime.utcnow)

    ticket_rel = db.relationship("Ticket", foreign_keys=[ticket_uid])


class TicketTemplate(db.Model):
    """Шаблон заявки для ускоренного создания типовых обращений."""

    __tablename__ = "ticket_templates"
    __table_args__ = {"schema": "sm"}

    template_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    template_name = db.Column(db.String(200), nullable=False)
    catalog_uid = db.Column(
        db.String(36), db.ForeignKey("sm.service_catalog.catalog_uid"), nullable=False
    )
    summary = db.Column(db.String(500), nullable=False)
    description = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(20), nullable=True)
    is_public = db.Column(db.Boolean, default=False)
    created_by = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=False
    )
    create_date = db.Column(db.DateTime, default=datetime.utcnow)


class AuditLog(db.Model):
    """Журнал аудита действий в системе."""

    __tablename__ = "audit_log"
    __table_args__ = {"schema": "sm"}

    audit_uid = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    user_uid = db.Column(
        db.String(36), db.ForeignKey("sm.users.user_uid"), nullable=True
    )
    action = db.Column(db.String(64), nullable=False)
    entity_type = db.Column(db.String(64), nullable=True)
    entity_uid = db.Column(db.String(36), nullable=True)
    details = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    create_date = db.Column(db.DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНАЯ ЛОГИКА ПРИЛОЖЕНИЯ
# ---------------------------------------------------------------------------
# Ниже лежат обычные функции, которые повторяют часть логики базы.
# Для учебного проекта это удобно: код можно читать и проверять прямо в приложении.

_RUS = list("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЫЭЮЯЬЪ")
_ENG = [
    "A",
    "B",
    "V",
    "G",
    "D",
    "E",
    "YO",
    "ZH",
    "Z",
    "I",
    "Y",
    "K",
    "L",
    "M",
    "N",
    "O",
    "P",
    "R",
    "S",
    "T",
    "U",
    "F",
    "KH",
    "C",
    "CH",
    "SH",
    "SHH",
    "Y",
    "E",
    "YU",
    "YA",
    "",
    "",
]


def translit(text_ru: str) -> str:
    """Простая транслитерация кириллицы в латиницу."""
    if not text_ru:
        return ""
    transliterated_chars = []
    for symbol in text_ru.upper():
        try:
            alphabet_index = _RUS.index(symbol)
            transliterated_chars.append(_ENG[alphabet_index])
        except ValueError:
            pass
    return "".join(transliterated_chars)


# ---------------------------------------------------------------------------
# ГЕНЕРАЦИЯ ЛОГИНА
# ---------------------------------------------------------------------------
def generate_login(last_name: str, first_name: str, middle_name: str = None) -> str:
    """Генерирует логин вида `Ivanov.II` и делает его уникальным."""
    base_last_name = translit(last_name)[:8]
    base_first_name = translit(first_name)[:1]
    base_middle_name = translit(middle_name)[:1] if middle_name else ""
    base_login = f"{base_last_name}.{base_first_name}{base_middle_name}"
    login = base_login
    suffix_index = 0
    while User.query.filter_by(user_name=login).first():
        suffix_index += 1
        login = f"{base_login}{suffix_index}"
    return login


# ---------------------------------------------------------------------------
# ГЕНЕРАЦИЯ ВРЕМЕННОГО ПАРОЛЯ
# ---------------------------------------------------------------------------
def generate_password() -> str:
    """Создаёт временный пароль с буквами разного регистра, цифрами и спецсимволами."""
    password_characters = string.ascii_letters + string.digits + "!@#$%&*"
    password_parts = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%&*"),
    ]
    password_parts += [random.choice(password_characters) for _ in range(8)]
    random.shuffle(password_parts)
    return "".join(password_parts)


# ---------------------------------------------------------------------------
# ПРИВОДИМ ТЕЛЕФОН К ОДНОМУ ВИДУ
# ---------------------------------------------------------------------------
def format_mobile(mobile: str):
    """Приводит телефон к формату `+7 (XXX) XXX-XX-XX`."""
    if not mobile:
        return None
    phone_digits = re.sub(r"\D", "", mobile)
    if len(phone_digits) >= 10:
        phone_digits = phone_digits[-10:]
        return f"+7 ({phone_digits[0:3]}) {phone_digits[3:6]}-{phone_digits[6:8]}-{phone_digits[8:10]}"
    return None


# ---------------------------------------------------------------------------
# НОРМАЛИЗУЕМ ПОЛ
# ---------------------------------------------------------------------------
def normalize_gender(gender: str):
    """Нормализует пол к значениям `M`, `F` или `O`."""
    if not gender:
        return None
    normalized_input = gender.upper().strip()
    if normalized_input.startswith("М") or normalized_input.startswith("M"):
        return "M"
    if normalized_input.startswith("Ж") or normalized_input.startswith("F"):
        return "F"
    return "O"


# ---------------------------------------------------------------------------
# ГЕНЕРАЦИЯ НОМЕРА ЗАЯВКИ
# ---------------------------------------------------------------------------
def generate_ticket_number() -> str:
    """Генерирует номер заявки вида `SD-2026-0001`."""
    ticket_count = db.session.query(func.count(Ticket.ticket_uid)).scalar() or 0
    year = datetime.utcnow().year
    return f"SD-{year}-{(ticket_count + 1):04d}"


# ---------------------------------------------------------------------------
# СОЗДАНИЕ ПОЛЬЗОВАТЕЛЯ
# ---------------------------------------------------------------------------
def create_user_db(
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
    role="user",
    work_group_uid=None,
    manager_uid=None,
    creator_uid=None,
) -> tuple:
    """
    Создаёт пользователя и возвращает его логин и временный пароль.

    Возвращаемое значение:
    `(user_name, temp_password)`.
    """
    temp_password = generate_password()
    user_name = generate_login(last_name, first_name, middle_name)
    user_uid = gen_uuid()
    # При первом запуске система может сама создать пользователя.
    # Поэтому тут допускаем, что "создателем" временно выступает сам код.
    system_user_uid = creator_uid or user_uid

    new_user = User(
        user_uid=user_uid,
        user_name=user_name,
        first_name=first_name,
        last_name=last_name,
        middel_name=middle_name or None,
        email=email,
        mobile=format_mobile(mobile),
        work_phone=work_phone or None,
        gender=normalize_gender(gender),
        title=title or None,
        department=department or None,
        company=company or None,
        manager_uid=manager_uid,
        create_by=system_user_uid,
        update_by=system_user_uid,
    )
    db.session.add(new_user)
    db.session.flush()

    _set_password_hash(new_user.user_uid, temp_password)
    _ensure_role(new_user.user_uid, role, creator_uid)
    _ensure_work_group(new_user.user_uid, work_group_uid)
    db.session.commit()

    return user_name, temp_password


# ---------------------------------------------------------------------------
# СБРОС ПАРОЛЯ
# ---------------------------------------------------------------------------
def reset_password_db(user_name: str):
    """
    Генерирует новый временный пароль и сохраняет его хэш.

    Возвращает пароль в открытом виде, чтобы администратор мог показать его
    пользователю один раз после сброса.
    """
    user = User.query.filter_by(user_name=user_name).first()
    if not user:
        return None

    temp_password = generate_password()

    _set_password_hash(user.user_uid, temp_password)

    password_record = Password.query.filter_by(user_uid=user.user_uid).first()
    if password_record:
        password_record.is_first_login = True
        password_record.must_change_password = True
        password_record.failed_attempts = 0

    db.session.commit()
    return temp_password


# ---------------------------------------------------------------------------
# ВНУТРЕННИЕ ХЕЛПЕРЫ
# ---------------------------------------------------------------------------
def _set_password_hash(user_uid: str, plain_password: str):
    """Сохраняет или обновляет хэш пароля пользователя."""
    password_record = Password.query.filter_by(user_uid=user_uid).first()
    password_hash = generate_password_hash(plain_password)
    if password_record:
        password_record.passwordhash = password_hash
    else:
        password_record = Password(
            user_uid=user_uid,
            passwordhash=password_hash,
            is_first_login=True,
            must_change_password=False,
            failed_attempts=0,
        )
        db.session.add(password_record)


def _ensure_role(user_uid: str, role: str, creator_uid: str = None):
    """Создаёт роль пользователя или обновляет её, если запись уже есть."""
    existing_role = UserRole.query.filter_by(user_uid=user_uid).first()
    if existing_role:
        existing_role.role = role
    else:
        db.session.add(UserRole(user_uid=user_uid, role=role))


def _ensure_work_group(user_uid: str, work_group_uid: str = None):
    """Привязывает пользователя к рабочей группе, если она указана."""
    if not work_group_uid:
        return
    existing_work_group_link = UserWorkGroup.query.filter_by(
        user_uid=user_uid, work_group_uid=work_group_uid
    ).first()
    if not existing_work_group_link:
        db.session.add(
            UserWorkGroup(
                user_uid=user_uid, work_group_uid=work_group_uid, is_primary=True
            )
        )


def verify_password(user: User, plain_password: str) -> bool:
    """Проверяет обычный пароль по сохранённому Werkzeug-хэшу."""
    if not user.password_record or not user.password_record.passwordhash:
        return False
    return check_password_hash(user.password_record.passwordhash, plain_password)


def add_ticket_history(ticket_uid, field_name, old_value, new_value, changed_by_uid):
    """Добавляет запись в историю изменений заявки."""
    history_record = TicketHistory(
        ticket_uid=ticket_uid,
        field_name=field_name,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None,
        changed_by=changed_by_uid,
    )
    db.session.add(history_record)


def compute_response_deadline(catalog, base_time=None):
    """Считает срок первой реакции по SLA или по дефолтным правилам."""
    base_time = base_time or datetime.utcnow()
    response_hours = 8
    if getattr(catalog, "sla", None) and getattr(
        catalog.sla, "response_time_hours", None
    ):
        response_hours = catalog.sla.response_time_hours
    elif getattr(catalog, "priority", None) == "critical":
        response_hours = 1
    elif getattr(catalog, "priority", None) == "high":
        response_hours = 2
    elif getattr(catalog, "priority", None) == "low":
        response_hours = 24
    return base_time + timedelta(hours=response_hours)


def compute_deadline(catalog, base_time=None):
    """Рассчитывает дедлайн решения заявки по SLA или по приоритету по умолчанию."""
    base_time = base_time or datetime.utcnow()
    deadline_hours = 24
    if getattr(catalog, "sla", None) and getattr(
        catalog.sla, "resolution_time_hours", None
    ):
        deadline_hours = catalog.sla.resolution_time_hours
    elif getattr(catalog, "priority", None) == "critical":
        deadline_hours = 4
    elif getattr(catalog, "priority", None) == "high":
        deadline_hours = 8
    elif getattr(catalog, "priority", None) == "low":
        deadline_hours = 72
    return base_time + timedelta(hours=deadline_hours)


def notify(user_uid, message, ticket_uid=None):
    """Создаёт уведомление для одного пользователя."""
    db.session.add(
        Notification(
            user_uid=user_uid,
            message=message,
            ticket_uid=ticket_uid,
        )
    )


def notify_ticket_update(ticket, message, exclude_uid=None):
    """Рассылает уведомление всем участникам заявки, кроме исключённого пользователя."""
    recipient_user_ids = {
        ticket.requester_uid,
        ticket.recipient_uid,
        ticket.performer_uid,
    }
    recipient_user_ids = {
        recipient_uid
        for recipient_uid in recipient_user_ids
        if recipient_uid and recipient_uid != exclude_uid
    }
    for recipient_uid in recipient_user_ids:
        notify(recipient_uid, message, ticket_uid=ticket.ticket_uid)


def create_approval_chain(ticket, catalog, requester):
    """Создаёт простую цепочку согласования для заявки."""
    approver_uid = requester.manager_uid
    if not approver_uid:
        # Если руководитель у пользователя не проставлен,
        # берём первого доступного руководителя или админа как запасной вариант.
        # Не идеально, но для учебного проекта так процесс хотя бы не стопорится.
        fallback_manager = db.session.execute(
            text(
                "SELECT u.user_uid FROM sm.users u "
                "JOIN sm.user_roles r ON r.user_uid = u.user_uid "
                "WHERE r.role IN ('manager','admin') LIMIT 1"
            )
        ).first()
        approver_uid = fallback_manager.user_uid if fallback_manager else None
    if approver_uid:
        db.session.add(
            TicketApproval(
                ticket_uid=ticket.ticket_uid,
                step_order=1,
                step_name="Согласование руководителем",
                approver_uid=approver_uid,
                status="pending",
            )
        )
        ticket.status = "pending_approval"
        ticket.response_due_at = None
        ticket.responded_at = None
        ticket.deadline_at = None
        ticket.resolved_at = None
        ticket.closed_at = None
        notify(
            approver_uid,
            f"Требуется согласование заявки {ticket.ticket_number}",
            ticket.ticket_uid,
        )


def process_approval_decision(ticket, approval, decision, comment, actor_uid):
    """Обрабатывает решение по шагу согласования заявки."""
    valid_decisions = {"approved", "rejected"}
    if decision not in valid_decisions:
        raise ValueError("Недопустимое решение согласования")
    old_status = approval.status
    approval.status = decision
    approval.comment = comment or None
    approval.decided_at = datetime.utcnow()
    add_ticket_history(ticket.ticket_uid, "approval", old_status, decision, actor_uid)

    if decision == "rejected":
        previous = ticket.status
        ticket.status = "rejected"
        ticket.response_due_at = None
        ticket.responded_at = None
        ticket.deadline_at = None
        ticket.resolved_at = None
        ticket.closed_at = None
        add_ticket_history(ticket.ticket_uid, "status", previous, "rejected", actor_uid)
        notify_ticket_update(
            ticket, f"Заявка {ticket.ticket_number} отклонена", exclude_uid=actor_uid
        )
        return

    pending_approvals = (
        TicketApproval.query.filter_by(
            ticket_uid=ticket.ticket_uid,
            status="pending",
        )
        .order_by(TicketApproval.step_order)
        .all()
    )
    if pending_approvals:
        next_approval = pending_approvals[0]
        if next_approval.approver_uid:
            notify(
                next_approval.approver_uid,
                f"Требуется согласование заявки {ticket.ticket_number}",
                ticket_uid=ticket.ticket_uid,
            )
    else:
        previous = ticket.status
        ticket.status = "new"
        if ticket.catalog:
            ticket.response_due_at = compute_response_deadline(
                ticket.catalog, approval.decided_at
            )
            ticket.deadline_at = compute_deadline(ticket.catalog, approval.decided_at)
        ticket.resolved_at = None
        ticket.closed_at = None
        add_ticket_history(ticket.ticket_uid, "status", previous, "new", actor_uid)
        notify_ticket_update(
            ticket, f"Заявка {ticket.ticket_number} согласована", exclude_uid=actor_uid
        )

"""
Database layer (SQLAlchemy 2.0 async ORM)
Uniformly supports SQLite (default) and PostgreSQL.
Credentials use email as the unique key; schema changes do not maintain backward compatibility for old data (new tables are directly rebuilt).
"""

import asyncio
import hashlib
import json
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from log import log


# ====================== ORM Models ======================

class Base(DeclarativeBase):
    pass


class AntigravityCredential(Base):
    """Antigravity credentials table (email unique key)"""
    __tablename__ = "antigravity_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)

    # Structured credential fields
    access_token: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    client_id: Mapped[str] = mapped_column(Text, default="")
    client_secret: Mapped[str] = mapped_column(Text, default="")
    project_id: Mapped[str] = mapped_column(Text, default="")
    expiry: Mapped[str] = mapped_column(Text, default="")

    # Status fields
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    error_codes: Mapped[str] = mapped_column(Text, default="[]")
    error_messages: Mapped[str] = mapped_column(Text, default="[]")
    last_success: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Model-level cooldown (JSON: {model_name: cooldown_timestamp})
    model_cooldowns: Mapped[str] = mapped_column(Text, default="{}")

    # Tier status (free/pro/ultra)
    tier: Mapped[Optional[str]] = mapped_column(String(32), default="pro")

    # Timestamps
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time, onupdate=time.time)

    __table_args__ = (
        Index("idx_ag_disabled", "disabled"),
    )


class ConfigItem(Base):
    """Configuration table"""
    __tablename__ = "config"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class UsageLog(Base):
    """Request log table: records one entry per API call (both success and failure)"""
    __tablename__ = "usage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)

    # Request origin
    api_format: Mapped[str] = mapped_column(String(16), default="")        # openai / gemini / anthropic
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    request_id: Mapped[str] = mapped_column(String(64), default="")

    # Call information
    model_name: Mapped[str] = mapped_column(String(128), default="")             # Client requested model name (original, may contain feature prefix)
    upstream_model_name: Mapped[str] = mapped_column(String(128), default="")   # Actual upstream model name used by backend (mapped)
    credential_email: Mapped[str] = mapped_column(String(320), default="")
    is_stream: Mapped[bool] = mapped_column(Boolean, default=False)

    # Results
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    use_time: Mapped[float] = mapped_column(Float, default=0.0)            # Latency (seconds)

    # Token usage
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)  # Cached tokens hitting cache in prompt (included in prompt_tokens)

    # Error summary on failure
    error_message: Mapped[str] = mapped_column(Text, default="")

    # Request body detail log file index (relative path to data/request_logs; empty=not logged)
    request_log_file: Mapped[str] = mapped_column(String(512), default="")

    # Authenticated API Key name or identifier
    api_key_name: Mapped[str] = mapped_column(String(128), default="")

    __table_args__ = (
        Index("idx_ul_created_model", "created_at", "model_name"),
        Index("idx_ul_created_email", "created_at", "credential_email"),
        Index("idx_ul_api_key_name", "api_key_name"),
    )


class ApiKey(Base):
    """API Keys management table for multi-key authentication, rate limiting and quotas"""
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    key_prefix: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Rate limiting & quotas (0 = unlimited)
    rpm_limit: Mapped[int] = mapped_column(Integer, default=0)           # Requests per minute
    daily_limit: Mapped[int] = mapped_column(Integer, default=0)         # Requests per day
    token_limit: Mapped[int] = mapped_column(BigInteger, default=0)      # Total lifetime token quota

    # Model restrictions (JSON array of allowed model names, empty/[] means all models allowed)
    models_allowed: Mapped[str] = mapped_column(Text, default="[]")

    # Timestamps
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    expires_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # None = never expires
    last_used_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Cumulative usage statistics (using BigInteger to prevent 2.1B overflow)
    total_requests: Mapped[int] = mapped_column(BigInteger, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0)

    __table_args__ = (
        Index("idx_api_key_active", "is_active"),
    )


# ====================== JSON Serialization Helpers ======================

def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_json(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _api_key_to_dict(r: ApiKey) -> Dict[str, Any]:
    """Helper: serialize ApiKey ORM model to dictionary."""
    return {
        "id": r.id,
        "key": r.key,
        "key_hash": r.key_hash,
        "key_prefix": r.key_prefix or "",
        "name": r.name,
        "description": r.description or "",
        "is_active": r.is_active,
        "rpm_limit": r.rpm_limit,
        "daily_limit": r.daily_limit,
        "token_limit": r.token_limit,
        "models_allowed": _load_json(r.models_allowed, []),
        "created_at": r.created_at,
        "expires_at": r.expires_at,
        "last_used_at": r.last_used_at,
        "total_requests": r.total_requests,
        "total_tokens": r.total_tokens,
    }


def _row_to_credential_dict(row: AntigravityCredential) -> Dict[str, Any]:
    """ORM row -> Credential dict"""
    return {
        "access_token": row.access_token or "",
        "refresh_token": row.refresh_token or "",
        "client_id": row.client_id or "",
        "client_secret": row.client_secret or "",
        "project_id": row.project_id or "",
        "expiry": row.expiry or "",
    }


def get_cooldown_group(model_name: str) -> str:
    """
    Model cooldown grouping: models of the same family share a cooldown key
    - Name contains argon -> "argon"
    - Name contains gemini -> "gemini"
    - Name contains claude or gpt -> "claude_gpt"
    - Others -> original model name
    """
    lowered = (model_name or "").lower()
    if "argon" in lowered:
        return "argon"
    if "gemini" in lowered:
        return "gemini"
    if "claude" in lowered or "gpt" in lowered:
        return "claude_gpt"
    return model_name


# ====================== Database Manager ======================

class Database:
    """Unified database manager (SQLite / PostgreSQL)"""

    def __init__(self):
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._backend_type = "sqlite"

        # In-memory config cache
        self._config_cache: Dict[str, Any] = {}
        self._config_loaded = False

    # ---------- Initialization ----------

    async def initialize(self) -> None:
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            postgres_uri = os.getenv("POSTGRESQL_URI", "")
            credentials_dir = os.getenv("CREDENTIALS_DIR", "./creds")
            os.makedirs(credentials_dir, exist_ok=True)

            if postgres_uri:
                # PostgreSQL (postgres:// or postgresql:// -> postgresql+asyncpg://)
                dsn = postgres_uri
                if dsn.startswith("postgresql://"):
                    dsn = "postgresql+asyncpg://" + dsn[len("postgresql://"):]
                elif dsn.startswith("postgres://"):
                    dsn = "postgresql+asyncpg://" + dsn[len("postgres://"):]
                elif not dsn.startswith("postgresql+"):
                    dsn = "postgresql+asyncpg://" + dsn
                self._engine = create_async_engine(dsn, pool_size=5, max_overflow=5, pool_pre_ping=True)
                self._backend_type = "postgresql"
            else:
                # SQLite default
                db_path = os.path.join(credentials_dir, "credentials.db")
                self._engine = create_async_engine(
                    f"sqlite+aiosqlite:///{db_path}",
                    # Connection pool: reuse multiple persistent connections (default NullPool has high per-session overhead);
                    # WAL mode allows concurrent read/write; single writer handled by busy_timeout fallback
                    pool_size=5,
                    max_overflow=10,
                    pool_pre_ping=True,
                    connect_args={"timeout": 30},
                )
                # WAL + NORMAL: read/write concurrency + reduce fsync on each commit (PRAGMA must be executed after connection established)
                from sqlalchemy import event

                @event.listens_for(self._engine.sync_engine, "connect")
                def _set_sqlite_pragma(dbapi_conn, conn_record):
                    cursor = dbapi_conn.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA busy_timeout=15000")
                    cursor.execute("PRAGMA cache_size=-8000")
                    cursor.close()

                self._backend_type = "sqlite"

            self._session_factory = async_sessionmaker(
                self._engine, expire_on_commit=False, class_=AsyncSession
            )

            # Create tables (not backward-compatible with old schema: old tables with filename key will be rebuilt with email key)
            async with self._engine.begin() as conn:
                await conn.run_sync(self._create_tables)

            # Mark initialization complete (for session() usage), then load config cache
            self._initialized = True

            # Load config cache
            await self._load_config_cache()

            log.info(f"Database initialized (backend={self._backend_type})")

    def _create_tables(self, sync_conn) -> None:
        """Create tables.
        - Credentials table: drop and rebuild on structural changes (key changed from filename to email)
        - Other tables: add missing columns using ALTER TABLE ADD COLUMN, preserving existing data
        """
        inspector = inspect(sync_conn)
        table_name = AntigravityCredential.__tablename__
        if inspector.has_table(table_name):
            existing_columns = {c["name"] for c in inspector.get_columns(table_name)}
            # Old structure used filename as key and had no email column - drop and rebuild (data cannot be migrated)
            if "filename" in existing_columns or "email" not in existing_columns:
                log.info("Detected legacy credentials table structure (filename key), rebuilding with email key structure")
                sync_conn.execute(text(f"DROP TABLE {table_name}"))

        # Add missing columns for existing tables (ALTER TABLE ADD COLUMN, preserving data)
        for orm_model in (UsageLog, ApiKey):
            table = orm_model.__table__
            if not inspector.has_table(table.name):
                continue
            existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
            dialect = inspector.bind.dialect
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                column_type = column.type.compile(dialect)
                # Add column: use server_default if present; use literal if Python default is scalar; otherwise DEFAULT NULL
                if column.server_default is not None:
                    default_clause = f" DEFAULT {column.server_default.arg}"
                elif column.default is not None and column.default.is_scalar:
                    default_clause = f" DEFAULT {column.default.arg!r}"
                else:
                    default_clause = " DEFAULT NULL"
                sync_conn.execute(
                    text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column_type}{default_clause}")
                )
                log.info(f"Table {table.name} added column {column.name} ({column_type})")

        Base.metadata.create_all(sync_conn)

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            self._engine = None
        self._session_factory = None
        self._initialized = False
        log.debug("Database closed")

    @property
    def backend_type(self) -> str:
        return self._backend_type

    def _ensure_initialized(self):
        if not self._initialized or not self._session_factory:
            raise RuntimeError("Database not initialized")

    @asynccontextmanager
    async def session(self):
        """Get database session (auto commit/rollback)"""
        self._ensure_initialized()
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # ---------- Credential Management ----------

    async def store_credential(
        self, email: str, credential_data: Dict[str, Any]
    ) -> bool:
        """Store or update credential (with email as unique key, preserving status fields)"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            access_token = credential_data.get("access_token") or credential_data.get("token") or ""
            refresh_token = credential_data.get("refresh_token") or ""
            client_id = credential_data.get("client_id") or ""
            client_secret = credential_data.get("client_secret") or ""
            project_id = credential_data.get("project_id") or ""
            expiry_val = credential_data.get("expiry") or ""
            expiry = expiry_val.isoformat() if hasattr(expiry_val, "isoformat") else str(expiry_val)
            tier = credential_data.get("tier")

            if row:
                row.access_token = access_token
                row.refresh_token = refresh_token
                row.client_id = client_id
                row.client_secret = client_secret
                row.project_id = project_id
                row.expiry = expiry
                if tier:
                    row.tier = tier
                row.updated_at = time.time()
            else:
                s.add(AntigravityCredential(
                    email=email,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    client_id=client_id,
                    client_secret=client_secret,
                    project_id=project_id,
                    expiry=expiry,
                    tier=tier or "pro",
                    last_success=time.time(),
                ))
            log.debug(f"Stored credential: {email}")
            return True

    async def get_credential(
        self, email: str
    ) -> Optional[Dict[str, Any]]:
        """Get credential data"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            return _row_to_credential_dict(row) if row else None

    async def list_credentials(self) -> List[str]:
        """List all credential emails (creation order)"""
        async with self.session() as s:
            rows = await s.scalars(
                select(AntigravityCredential.email).order_by(AntigravityCredential.id)
            )
            return list(rows)

    async def delete_credential(self, email: str) -> bool:
        """Delete credential"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if row:
                await s.delete(row)
                return True
            return False

    async def get_next_available_credential(
        self, model_name: Optional[str] = None
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Randomly get an available credential (load balancing + model family cooldown filtering), returns (email, credential)"""
        current_time = time.time()
        cd_group = get_cooldown_group(model_name) if model_name else None
        async with self.session() as s:
            # Lightweight query: only fetch essential small fields (email/cooldown/disabled) to avoid loading full table
            # Large fields like access_token/refresh_token (credentials plaintext can reach several KB)
            rows = (
                await s.execute(
                    select(
                        AntigravityCredential.email,
                        AntigravityCredential.model_cooldowns,
                    )
                    .where(AntigravityCredential.disabled == False)  # noqa: E712
                    .order_by(AntigravityCredential.id)
                )
            ).all()
            if not rows:
                return None
            # Python-side random load balancing (filter model family cooldowns)
            candidates = rows
            if cd_group:
                candidates = []
                for email, model_cooldowns in rows:
                    cooldowns = _load_json(model_cooldowns, {})
                    cd = cooldowns.get(cd_group)
                    if cd is not None and current_time < cd:
                        continue
                    candidates.append((email, model_cooldowns))
            if not candidates:
                return None
            picked_email = random.choice(candidates)[0]
            # Only fetch full data for the selected credential
            row = await s.scalar(
                select(AntigravityCredential).where(
                    AntigravityCredential.email == picked_email
                )
            )
            if not row or row.disabled:
                return None
            return row.email, _row_to_credential_dict(row)

    async def get_available_credential_by_email(
        self, email: str, model_name: Optional[str] = None
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        Get available credential by specified email (used by credential affinity/sticky mode).
        Credential nonexistent / disabled / model family in cooldown -> None.
        """
        if not email:
            return None
        current_time = time.time()
        cd_group = get_cooldown_group(model_name) if model_name else None
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if not row or row.disabled:
                return None
            if cd_group:
                cooldowns = _load_json(row.model_cooldowns, {})
                cd = cooldowns.get(cd_group)
                if cd is not None and current_time < cd:
                    return None
            return row.email, _row_to_credential_dict(row)

    # ---------- State Management ----------

    async def update_credential_state(
        self, email: str, state_updates: Dict[str, Any]
    ) -> bool:
        """Update credential state fields"""
        json_fields = {"error_codes", "error_messages", "model_cooldowns"}
        float_fields = {"last_success"}
        str_fields = {"user_email", "tier", "project_id"}
        bool_fields = {"disabled"}
        async with self.session() as s:
            values: Dict[str, Any] = {}
            for key, value in state_updates.items():
                if key in json_fields:
                    values[key] = _dump_json(value)
                elif key in float_fields:
                    values[key] = float(value) if value is not None else None
                elif key in str_fields:
                    values[key] = value
                elif key in bool_fields:
                    values[key] = bool(value)
            if not values:
                return True
            values["updated_at"] = time.time()
            result = await s.execute(
                AntigravityCredential.__table__.update()
                .where(AntigravityCredential.email == email)
                .values(**values)
            )
            return result.rowcount > 0

    async def get_credential_state(
        self, email: str
    ) -> Dict[str, Any]:
        """Get credential state (excluding error_messages)"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if not row:
                return {}
            return {
                "disabled": bool(row.disabled),
                "error_codes": _load_json(row.error_codes, []),
                "last_success": row.last_success or time.time(),
                "email": row.email,
                "model_cooldowns": _load_json(row.model_cooldowns, {}),
                "tier": row.tier or "pro",
                "project_id": row.project_id or "",
            }

    async def get_all_credential_states(self) -> Dict[str, Dict[str, Any]]:
        """Get all credential states (automatically filter expired CDs)"""
        current_time = time.time()
        async with self.session() as s:
            rows = (await s.scalars(select(AntigravityCredential))).all()
            states = {}
            for row in rows:
                cooldowns = _load_json(row.model_cooldowns, {})
                cooldowns = {k: v for k, v in cooldowns.items() if v > current_time}
                states[row.email] = {
                    "disabled": bool(row.disabled),
                    "error_codes": _load_json(row.error_codes, []),
                    "last_success": row.last_success or time.time(),
                    "email": row.email,
                    "model_cooldowns": cooldowns,
                    "tier": row.tier or "pro",
                    "project_id": row.project_id or "",
                }
            return states

    async def get_credentials_summary(
        self,
        offset: int = 0,
        limit: Optional[int] = None,
        status_filter: str = "all",
        error_code_filter: Optional[str] = None,
        cooldown_filter: Optional[str] = None,
        tier_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Credentials summary (pagination + status filtering, in-memory filtering)"""
        current_time = time.time()
        async with self.session() as s:
            total = await s.scalar(select(func.count()).select_from(AntigravityCredential)) or 0
            disabled_count = await s.scalar(
                select(func.count()).select_from(AntigravityCredential)
                .where(AntigravityCredential.disabled == True)  # noqa: E712
            ) or 0

            stmt = select(AntigravityCredential).order_by(AntigravityCredential.id)
            if status_filter == "enabled":
                stmt = stmt.where(AntigravityCredential.disabled == False)  # noqa: E712
            elif status_filter == "disabled":
                stmt = stmt.where(AntigravityCredential.disabled == True)  # noqa: E712
            rows = (await s.scalars(stmt)).all()

            filter_value = None
            filter_int = None
            filter_none = False
            if error_code_filter and error_code_filter.strip().lower() != "all":
                if error_code_filter.strip().lower() == "none":
                    filter_none = True
                else:
                    filter_value = error_code_filter.strip()
                    try:
                        filter_int = int(filter_value)
                    except ValueError:
                        filter_int = None

            summaries = []
            for row in rows:
                error_codes = _load_json(row.error_codes, [])
                cooldowns = _load_json(row.model_cooldowns, {})
                active_cooldowns = {k: v for k, v in cooldowns.items() if v > current_time}

                if filter_none and error_codes:
                    continue
                if filter_value is not None:
                    match = False
                    for code in error_codes:
                        if code == filter_value or code == filter_int:
                            match = True
                            break
                        if isinstance(code, str) and filter_int is not None:
                            try:
                                if int(code) == filter_int:
                                    match = True
                                    break
                            except ValueError:
                                pass
                    if not match:
                        continue

                summary = {
                    "email": row.email,
                    "disabled": bool(row.disabled),
                    "error_codes": error_codes,
                    "last_success": row.last_success or current_time,
                    "model_cooldowns": active_cooldowns,
                    "tier": row.tier or "pro",
                    "project_id": row.project_id or "",
                }
                if tier_filter and tier_filter in ("free", "pro", "ultra"):
                    if summary["tier"] != tier_filter:
                        continue
                if cooldown_filter == "in_cooldown" and not active_cooldowns:
                    continue
                if cooldown_filter == "no_cooldown" and active_cooldowns:
                    continue
                summaries.append(summary)

            total_count = len(summaries)
            page_items = summaries[offset:offset + limit] if limit is not None else summaries[offset:]
            return {
                "items": page_items,
                "total": total_count,
                "offset": offset,
                "limit": limit,
                "stats": {
                    "total": total,
                    "normal": total - disabled_count,
                    "disabled": disabled_count,
                },
            }

    async def get_duplicate_credentials_by_email(self) -> Dict[str, Any]:
        """Duplicate credentials grouped by email (email is the unique key; structure kept for stats display)"""
        async with self.session() as s:
            rows = (await s.scalars(select(AntigravityCredential).order_by(AntigravityCredential.id))).all()
            emails = [row.email for row in rows]
            return {
                "email_groups": {},
                "duplicate_groups": [],
                "duplicate_count": 0,
                "no_email_files": [],
                "no_email_count": 0,
                "unique_email_count": len(emails),
                "total_count": len(rows),
            }

    async def get_credential_errors(self, email: str) -> Dict[str, Any]:
        """Get credential error information"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if row:
                return {
                    "email": email,
                    "error_codes": _load_json(row.error_codes, []),
                    "error_messages": _load_json(row.error_messages, []),
                }
            return {"email": email, "error_codes": [], "error_messages": []}

    # ---------- Model-level Cooldown ----------

    async def set_model_cooldown(
        self,
        email: str,
        model_name: str,
        cooldown_until: Optional[float],
    ) -> bool:
        """Set/clear model cooldown time (grouped by family: argon / gemini / claude_gpt / other original name)"""
        cd_group = get_cooldown_group(model_name)
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if not row:
                return False
            cooldowns = _load_json(row.model_cooldowns, {})
            if cooldown_until is None:
                cooldowns.pop(cd_group, None)
            else:
                cooldowns[cd_group] = cooldown_until
            row.model_cooldowns = _dump_json(cooldowns)
            row.updated_at = time.time()
            return True

    async def clear_all_model_cooldowns(self, email: str) -> bool:
        """Clear all model cooldowns for a credential"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if not row:
                return False
            row.model_cooldowns = "{}"
            row.updated_at = time.time()
            return True

    async def record_success(
        self, email: str, model_name: Optional[str] = None
    ) -> None:
        """Conditional write after successful call (clear errors + CD for this model family)"""
        async with self.session() as s:
            row = await s.scalar(
                select(AntigravityCredential).where(AntigravityCredential.email == email)
            )
            if not row:
                return
            changed = False
            if _load_json(row.error_codes, []):
                row.error_codes = "[]"
                row.error_messages = "{}"
                row.last_success = time.time()
                row.updated_at = time.time()
                changed = True
            if model_name:
                cd_group = get_cooldown_group(model_name)
                cooldowns = _load_json(row.model_cooldowns, {})
                if cd_group in cooldowns:
                    cooldowns.pop(cd_group)
                    row.model_cooldowns = _dump_json(cooldowns)
                    row.updated_at = time.time()
                    changed = True
            if changed:
                log.debug(f"Recorded success for {email} (model={model_name})")

    # ---------- Configuration Management (In-Memory Cache) ----------

    async def _load_config_cache(self) -> None:
        async with self.session() as s:
            rows = (await s.scalars(select(ConfigItem))).all()
            for item in rows:
                try:
                    self._config_cache[item.key] = json.loads(item.value)
                except (json.JSONDecodeError, TypeError):
                    self._config_cache[item.key] = item.value
            self._config_loaded = True
            log.debug(f"Loaded {len(self._config_cache)} config items")

    async def reload_config_cache(self) -> None:
        self._ensure_initialized()
        self._config_cache.clear()
        await self._load_config_cache()
        log.info("Config cache reloaded from database")

    async def set_config(self, key: str, value: Any) -> bool:
        """Set configuration (write to DB + update cache)"""
        async with self.session() as s:
            item = await s.get(ConfigItem, key)
            serialized = _dump_json(value)
            if item:
                item.value = serialized
                item.updated_at = time.time()
            else:
                s.add(ConfigItem(key=key, value=serialized))
            self._config_cache[key] = value
            return True

    async def get_config(self, key: str, default: Any = None) -> Any:
        self._ensure_initialized()
        return self._config_cache.get(key, default)

    async def get_all_config(self) -> Dict[str, Any]:
        self._ensure_initialized()
        return self._config_cache.copy()

    async def delete_config(self, key: str) -> bool:
        async with self.session() as s:
            item = await s.get(ConfigItem, key)
            if item:
                await s.delete(item)
                self._config_cache.pop(key, None)
                return True
            return False

    # ---------- Request Logs (usage_log) ----------

    async def insert_usage_log(self, entry: Dict[str, Any]) -> bool:
        """Insert a request log entry (failure only logs warning, does not affect main flow)"""
        try:
            async with self.session() as s:
                s.add(UsageLog(
                    created_at=entry.get("created_at") or time.time(),
                    api_format=str(entry.get("api_format", "")),
                    client_ip=str(entry.get("client_ip", "")),
                    request_id=str(entry.get("request_id", "")),
                    model_name=str(entry.get("model_name", "")),
                    upstream_model_name=str(entry.get("upstream_model_name", "")),
                    credential_email=str(entry.get("credential_email", "")),
                    is_stream=bool(entry.get("is_stream", False)),
                    status_code=int(entry.get("status_code", 0) or 0),
                    success=bool(entry.get("success", False)),
                    use_time=float(entry.get("use_time", 0.0) or 0.0),
                    prompt_tokens=int(entry.get("prompt_tokens", 0) or 0),
                    completion_tokens=int(entry.get("completion_tokens", 0) or 0),
                    total_tokens=int(entry.get("total_tokens", 0) or 0),
                    cached_tokens=int(entry.get("cached_tokens", 0) or 0),
                    api_key_name=str(entry.get("api_key_name", "")),
                    error_message=str(entry.get("error_message", ""))[:2000],
                    request_log_file=str(entry.get("request_log_file", ""))[:512],
                ))
                return True
        except SQLAlchemyError as e:
            log.warning(f"Failed to write request log: {e}")
            return False

    async def query_usage_logs(
        self,
        offset: int = 0,
        limit: int = 50,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        model_name: str = "",
        credential_email: str = "",
        api_format: str = "",
        status: str = "all",  # all / success / error
        is_stream: Optional[bool] = None,
        status_code: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Paginated query for request logs, returns (log_list, total_count), newest -> oldest"""
        stmt = select(UsageLog)
        count_stmt = select(func.count()).select_from(UsageLog)

        if start_timestamp:
            stmt = stmt.where(UsageLog.created_at >= start_timestamp)
            count_stmt = count_stmt.where(UsageLog.created_at >= start_timestamp)
        if end_timestamp:
            stmt = stmt.where(UsageLog.created_at <= end_timestamp)
            count_stmt = count_stmt.where(UsageLog.created_at <= end_timestamp)
        if model_name:
            stmt = stmt.where(UsageLog.model_name == model_name)
            count_stmt = count_stmt.where(UsageLog.model_name == model_name)
        if credential_email:
            stmt = stmt.where(UsageLog.credential_email == credential_email)
            count_stmt = count_stmt.where(UsageLog.credential_email == credential_email)
        if api_format:
            stmt = stmt.where(UsageLog.api_format == api_format)
            count_stmt = count_stmt.where(UsageLog.api_format == api_format)
        if status_code is not None:
            stmt = stmt.where(UsageLog.status_code == status_code)
            count_stmt = count_stmt.where(UsageLog.status_code == status_code)
        if status == "success":
            stmt = stmt.where(UsageLog.success.is_(True))
            count_stmt = count_stmt.where(UsageLog.success.is_(True))
        elif status == "error":
            stmt = stmt.where(UsageLog.success.is_(False))
            count_stmt = count_stmt.where(UsageLog.success.is_(False))
        if is_stream is not None:
            stmt = stmt.where(UsageLog.is_stream.is_(is_stream))
            count_stmt = count_stmt.where(UsageLog.is_stream.is_(is_stream))

        async with self.session() as s:
            total = (await s.execute(count_stmt)).scalar() or 0
            rows = (
                (
                    await s.execute(
                        stmt.order_by(UsageLog.created_at.desc(), UsageLog.id.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

        items = [
            {
                "id": r.id,
                "created_at": r.created_at,
                "api_format": r.api_format or "",
                "client_ip": r.client_ip or "",
                "request_id": r.request_id or "",
                "model_name": r.model_name or "",
                "upstream_model_name": r.upstream_model_name or "",
                "credential_email": r.credential_email or "",
                "is_stream": r.is_stream,
                "status_code": r.status_code,
                "success": r.success,
                "use_time": r.use_time,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "cached_tokens": r.cached_tokens or 0,
                "api_key_name": r.api_key_name or "",
                "error_message": r.error_message or "",
                "request_log_file": r.request_log_file or "",
            }
            for r in rows
        ]
        return items, total

    async def usage_log_stats(
        self,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        model_name: str = "",
        credential_email: str = "",
        api_format: str = "",
    ) -> Dict[str, Any]:
        """Aggregated statistics: total requests / success count / error count / token summary / average latency / per-model aggregation"""
        conditions = []
        if start_timestamp:
            conditions.append(UsageLog.created_at >= start_timestamp)
        if end_timestamp:
            conditions.append(UsageLog.created_at <= end_timestamp)
        if model_name:
            conditions.append(UsageLog.model_name == model_name)
        if credential_email:
            conditions.append(UsageLog.credential_email == credential_email)
        if api_format:
            conditions.append(UsageLog.api_format == api_format)

        success_cond = func.cast(UsageLog.success, Integer).label("success_int")

        agg = select(
            func.count().label("total_requests"),
            func.coalesce(func.sum(success_cond), 0).label("success_count"),
            func.coalesce(func.sum(UsageLog.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(UsageLog.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(UsageLog.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(UsageLog.cached_tokens), 0).label("cached_tokens"),
            func.coalesce(func.avg(UsageLog.use_time), 0).label("avg_use_time"),
        ).where(*conditions)

        by_model = (
            select(
                UsageLog.model_name.label("model_name"),
                func.count().label("count"),
                func.coalesce(func.sum(UsageLog.total_tokens), 0).label("total_tokens"),
            )
            .where(*conditions)
            .group_by(UsageLog.model_name)
            .order_by(func.count().desc())
            .limit(50)
        )

        async with self.session() as s:
            row = (await s.execute(agg)).one()
            model_rows = (await s.execute(by_model)).all()

        return {
            "total_requests": row.total_requests or 0,
            "success_count": row.success_count or 0,
            "error_count": (row.total_requests or 0) - (row.success_count or 0),
            "prompt_tokens": row.prompt_tokens or 0,
            "completion_tokens": row.completion_tokens or 0,
            "total_tokens": row.total_tokens or 0,
            "cached_tokens": row.cached_tokens or 0,
            "avg_use_time": round(float(row.avg_use_time or 0), 2),
            "by_model": [
                {"model_name": m.model_name or "(unknown)", "count": m.count, "total_tokens": m.total_tokens}
                for m in model_rows
            ],
        }

    async def delete_usage_logs_before(self, before_timestamp: float) -> int:
        """Delete logs before specified timestamp, returns number of deleted rows"""
        async with self.session() as s:
            result = await s.execute(
                text("DELETE FROM usage_log WHERE created_at < :ts"),
                {"ts": before_timestamp},
            )
            return result.rowcount or 0

    # ---------- API Key Management ----------

    async def list_api_keys(self) -> List[Dict[str, Any]]:
        """List all API keys ordered by newest first"""
        async with self.session() as s:
            result = await s.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
            rows = result.scalars().all()
            return [_api_key_to_dict(r) for r in rows]

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[Dict[str, Any]]:
        """Look up an API key by its SHA-256 hash"""
        async with self.session() as s:
            result = await s.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
            r = result.scalar_one_or_none()
            return _api_key_to_dict(r) if r else None

    async def get_api_key_by_id(self, key_id: int) -> Optional[Dict[str, Any]]:
        """Look up an API key by ID"""
        async with self.session() as s:
            result = await s.execute(select(ApiKey).where(ApiKey.id == key_id))
            r = result.scalar_one_or_none()
            return _api_key_to_dict(r) if r else None

    async def create_api_key(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new API key record"""
        key_str = str(data["key"]).strip()
        key_hash = hashlib.sha256(key_str.encode("utf-8")).hexdigest()
        key_prefix = f"{key_str[:8]}...{key_str[-4:]}" if len(key_str) > 12 else key_str[:8]
        models_allowed = data.get("models_allowed", [])
        if isinstance(models_allowed, list):
            models_allowed_str = _dump_json(models_allowed)
        else:
            models_allowed_str = str(models_allowed)

        row = ApiKey(
            key=key_str,
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=str(data["name"]).strip(),
            description=str(data.get("description", "")).strip(),
            is_active=bool(data.get("is_active", True)),
            rpm_limit=int(data.get("rpm_limit", 0) or 0),
            daily_limit=int(data.get("daily_limit", 0) or 0),
            token_limit=int(data.get("token_limit", 0) or 0),
            models_allowed=models_allowed_str,
            created_at=float(data.get("created_at") or time.time()),
            expires_at=float(data["expires_at"]) if data.get("expires_at") else None,
            last_used_at=None,
            total_requests=0,
            total_tokens=0,
        )
        async with self.session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return _api_key_to_dict(row)

    async def update_api_key(self, key_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update an existing API key record"""
        async with self.session() as s:
            result = await s.execute(select(ApiKey).where(ApiKey.id == key_id))
            row = result.scalar_one_or_none()
            if not row:
                return None

            if "name" in updates:
                row.name = str(updates["name"]).strip()
            if "key" in updates and updates["key"]:
                new_key = str(updates["key"]).strip()
                row.key = new_key
                row.key_hash = hashlib.sha256(new_key.encode("utf-8")).hexdigest()
                row.key_prefix = f"{new_key[:8]}...{new_key[-4:]}" if len(new_key) > 12 else new_key[:8]
            if "description" in updates:
                row.description = str(updates["description"]).strip()
            if "is_active" in updates:
                row.is_active = bool(updates["is_active"])
            if "rpm_limit" in updates:
                row.rpm_limit = int(updates["rpm_limit"] or 0)
            if "daily_limit" in updates:
                row.daily_limit = int(updates["daily_limit"] or 0)
            if "token_limit" in updates:
                row.token_limit = int(updates["token_limit"] or 0)
            if "expires_at" in updates:
                val = updates["expires_at"]
                row.expires_at = float(val) if val else None
            if "models_allowed" in updates:
                m = updates["models_allowed"]
                row.models_allowed = _dump_json(m) if isinstance(m, list) else str(m)

            await s.commit()
            await s.refresh(row)
            return _api_key_to_dict(row)

    async def delete_api_key(self, key_id: int) -> bool:
        """Delete an API key by ID"""
        async with self.session() as s:
            result = await s.execute(select(ApiKey).where(ApiKey.id == key_id))
            row = result.scalar_one_or_none()
            if not row:
                return False
            await s.delete(row)
            await s.commit()
            return True

    async def increment_api_key_usage(self, key_hash: str, req_delta: int = 1, token_delta: int = 0) -> None:
        """Atomically increment request and token delta counters for an API key"""
        async with self.session() as s:
            await s.execute(
                text(
                    "UPDATE api_keys SET "
                    "total_requests = total_requests + :req_delta, "
                    "total_tokens = total_tokens + :token_delta, "
                    "last_used_at = :now "
                    "WHERE key_hash = :key_hash"
                ),
                {
                    "req_delta": max(0, req_delta),
                    "token_delta": max(0, token_delta),
                    "now": time.time(),
                    "key_hash": key_hash,
                },
            )
            await s.commit()

    # ---------- Backend Information ----------

    def get_backend_info(self) -> Dict[str, Any]:
        return {
            "backend_type": self._backend_type,
            "initialized": self._initialized,
            "database_path": (
                os.path.join(os.getenv("CREDENTIALS_DIR", "./creds"), "credentials.db")
                if self._backend_type == "sqlite"
                else None
            ),
            "credentials_dir": os.getenv("CREDENTIALS_DIR", "./creds"),
        }


# ====================== Global Singleton ======================

_database: Optional[Database] = None
_database_lock = asyncio.Lock()


async def get_database() -> Database:
    """Get global database instance"""
    global _database
    if _database is None:
        async with _database_lock:
            if _database is None:
                db = Database()
                await db.initialize()
                _database = db
    return _database


async def close_database() -> None:
    """Close global database"""
    global _database
    if _database:
        await _database.close()
        _database = None

"""Per-user data namespace and identity.

One env var: ``USERS=id:password,id:password,...``.

Each entry is ``id:password``. The HTTP Basic Auth middleware
(``simulator._basic_auth``) gates the whole site: the auth username
IS the user id, the password validates against this map, and the
validated id is stashed on ``request.state.user_id`` for the routes'
:func:`get_user_context` dependency.

On a user's first authenticated request the directory
``<project_root>/data/users/<user_id>/`` is created and seeded from
``fixtures/``. The user then edits their own copy; other users are
isolated.

For local dev / tests, ``USERS`` defaults to ``demo:demo`` so
``python -m simulator`` Just Works. **Set USERS explicitly in
production** (the ``demo:demo`` default is logged loudly at startup).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import final

import structlog
from fastapi import HTTPException, Request

from guidepoint.master_data import (
    JsonFilePaths,
    MasterDataRepository,
    OptStatus,
    build_json_master_data_repository,
)

from simulator._slots import SlotsRepository

_log = structlog.get_logger(__name__)

DEFAULT_USERS = "demo:demo"


@final
@dataclass(frozen=True, slots=True)
class User:
    """A single allowed operator: id + password."""

    id: str
    password: str


@final
class UserRegistry:
    """Allowed users parsed from ``USERS=id:pw,id:pw,...``."""

    def __init__(self, raw: str) -> None:
        users: dict[str, User] = {}
        for chunk in (raw or DEFAULT_USERS).split(","):
            cleaned = chunk.strip()
            if not cleaned:
                continue
            if ":" not in cleaned:
                raise ValueError(
                    f"USERS entry {cleaned!r} missing ':password'; "
                    "expected format 'id:password,id:password,...'"
                )
            user_id, _, password = cleaned.partition(":")
            user_id = user_id.strip()
            password = password.strip()
            if not user_id or not password:
                raise ValueError(
                    f"USERS entry {cleaned!r}: both id and password must be non-empty"
                )
            users[user_id] = User(id=user_id, password=password)
        if not users:
            raise ValueError("USERS resolved to an empty allowlist")
        self._users = users
        if raw == "" or raw is None:
            _log.warning(
                "simulator.users.using_default",
                detail="USERS env var unset; defaulting to 'demo:demo'. "
                "SET USERS BEFORE EXPOSING TO ANY NETWORK.",
            )

    def get(self, user_id: str) -> User:
        """Return the user or raise 403 with the allowed list."""
        user = self._users.get(user_id)
        if user is None:
            allowed = ", ".join(sorted(self._users.keys()))
            raise HTTPException(
                status_code=403,
                detail=f"unknown user {user_id!r}; allowed: {allowed}",
            )
        return user

    def list_ids(self) -> tuple[str, ...]:
        """Return all allowed user ids in insertion order."""
        return tuple(self._users.keys())


@final
@dataclass(frozen=True, slots=True)
class UserPaths:
    """On-disk paths under a single user's namespace."""

    root: Path
    customers_dir: Path
    dealers_dir: Path
    vehicles_dir: Path
    slots_file: Path

    @staticmethod
    def for_user(*, project_root: Path, user_id: str) -> UserPaths:
        root = (project_root / "data" / "users" / user_id).resolve()
        return UserPaths(
            root=root,
            customers_dir=root / "customers",
            dealers_dir=root / "dealers",
            vehicles_dir=root / "vehicles",
            slots_file=root / "slots.json",
        )


@final
@dataclass(frozen=True, slots=True)
class UserContext:
    """Per-user dependency bundle, cached for process lifetime.

    Owns master data (customer / dealer / vehicle) and slots for one
    operator. All case execution uses the global ``CaseDriver`` on
    ``app.state``; this context is purely a master-data namespace.
    """

    user: User
    paths: UserPaths
    master_data: MasterDataRepository
    slots_repo: SlotsRepository


@final
class UserContextRegistry:
    """Lazily builds and caches a :class:`UserContext` per user."""

    def __init__(
        self,
        *,
        project_root: Path,
        user_registry: UserRegistry,
    ) -> None:
        self._project_root = project_root
        self._fixtures_root = (project_root / "fixtures").resolve()
        self._user_registry = user_registry
        self._cache: dict[str, UserContext] = {}
        self._lock = Lock()

    def for_user(self, user_id: str) -> UserContext:
        user = self._user_registry.get(user_id)
        with self._lock:
            ctx = self._cache.get(user.id)
            if ctx is None:
                ctx = self._build(user)
                self._cache[user.id] = ctx
            return ctx

    def invalidate_user(self, user_id: str) -> None:
        """Drop a cached context so the next request reloads master data from disk."""
        with self._lock:
            self._cache.pop(user_id, None)

    def set_opt_status_for_phone(
        self,
        phone: str,
        opt_status: OptStatus,
        *,
        preferred_user_id: str = "",
    ) -> bool:
        """Update ``opt_status`` for the customer with ``phone``; invalidate cache."""
        user_ids = (
            (preferred_user_id,)
            if preferred_user_id
            else self._user_registry.list_ids()
        )
        for user_id in user_ids:
            ctx = self.for_user(user_id)
            for customer in ctx.master_data.list_customers():
                if customer.phone != phone:
                    continue
                if customer.opt_status == opt_status:
                    self.invalidate_user(user_id)
                    return True
                ctx.master_data.save_customer(
                    customer.model_copy(update={"opt_status": opt_status})
                )
                self.invalidate_user(user_id)
                return True
        return False

    def _build(self, user: User) -> UserContext:
        paths = UserPaths.for_user(project_root=self._project_root, user_id=user.id)
        self._seed_if_missing(paths)
        master_data = build_json_master_data_repository(
            paths=JsonFilePaths(
                customers_dir=paths.customers_dir,
                dealers_dir=paths.dealers_dir,
                vehicles_dir=paths.vehicles_dir,
            )
        )
        slots_repo = SlotsRepository(path=paths.slots_file)
        return UserContext(
            user=user,
            paths=paths,
            master_data=master_data,
            slots_repo=slots_repo,
        )

    def _seed_if_missing(self, paths: UserPaths) -> None:
        if paths.root.exists():
            return
        paths.root.mkdir(parents=True, exist_ok=True)
        for sub, dst in (
            ("customers", paths.customers_dir),
            ("dealers", paths.dealers_dir),
            ("vehicles", paths.vehicles_dir),
        ):
            src = self._fixtures_root / sub
            if src.exists():
                shutil.copytree(src, dst, dirs_exist_ok=True)
        src_slots = self._fixtures_root / "slots.json"
        if src_slots.exists():
            paths.slots_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src_slots, paths.slots_file)


def get_user_context(request: Request) -> UserContext:
    """FastAPI dependency: return the per-user context for this request."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(
            status_code=500,
            detail="user_id missing on request.state — auth middleware misconfigured",
        )
    registry: UserContextRegistry = request.app.state.user_contexts
    return registry.for_user(user_id)


__all__ = [
    "DEFAULT_USERS",
    "User",
    "UserContext",
    "UserContextRegistry",
    "UserPaths",
    "UserRegistry",
    "get_user_context",
]

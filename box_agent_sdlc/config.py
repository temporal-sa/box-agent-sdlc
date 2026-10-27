"""Environment-only configuration; no credentials are loaded into generated-code executors."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "postgresql://box:box@localhost:5432/box_sdlc")
    )
    scope: str = field(default_factory=lambda: os.getenv("APP_SCOPE", "box-agent-sdlc"))
    mode: str = field(default_factory=lambda: os.getenv("DEMO_MODE", "scripted"))
    auth_mode: str = field(default_factory=lambda: os.getenv("AUTH_MODE", "token"))
    operator_token: str = field(default_factory=lambda: os.getenv("OPERATOR_TOKEN", ""))
    proxy_user_header: str = "X-Temporal-Auth-Email"
    github_repo: str = field(
        default_factory=lambda: os.getenv("GITHUB_SANDBOX_REPO", "temporal-sa/box-agent-sdlc-storefront")
    )
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
    webhook_secret: str = field(default_factory=lambda: os.getenv("GITHUB_WEBHOOK_SECRET", ""))
    command_actors: tuple[str, ...] = field(
        default_factory=lambda: tuple(x for x in os.getenv("GITHUB_COMMAND_ACTORS", "").split(",") if x)
    )
    public_url: str = field(default_factory=lambda: os.getenv("PUBLIC_URL", "http://localhost:8000"))
    executor_url: str = field(default_factory=lambda: os.getenv("EXECUTOR_URL", "http://localhost:8082"))
    executor_token: str = field(default_factory=lambda: os.getenv("EXECUTOR_TOKEN", ""))
    version: str = field(default_factory=lambda: os.getenv("PIPELINE_VERSION", "v1"))
    max_runs: int = field(default_factory=lambda: int(os.getenv("MAX_ACTIVE_GITHUB_RUNS", "3")))
    default_budget: float = field(default_factory=lambda: float(os.getenv("RUN_BUDGET_USD", "3")))
    lease_seconds: float = field(default_factory=lambda: float(os.getenv("TASK_LEASE_SECONDS", "60")))
    heartbeat_seconds: float = field(default_factory=lambda: float(os.getenv("HEARTBEAT_SECONDS", "15")))
    poll_seconds: float = field(default_factory=lambda: float(os.getenv("POLL_SECONDS", "1")))
    review_window: float = field(default_factory=lambda: float(os.getenv("REVIEW_WINDOW_SECONDS", "60")))
    bake_seconds: float = field(default_factory=lambda: float(os.getenv("CANARY_BAKE_SECONDS", "90")))
    check_seconds: float = field(default_factory=lambda: float(os.getenv("CANARY_CHECK_SECONDS", "15")))
    idle_hours: float = 24
    max_body_bytes: int = 1_048_576
    model_reservation_usd: float = field(
        default_factory=lambda: float(os.getenv("MODEL_RESERVATION_USD", "0.25"))
    )

    def validate(self) -> None:
        if self.mode not in {"live", "scripted"} or self.version not in {"v1", "v2"}:
            raise ValueError("Invalid DEMO_MODE or PIPELINE_VERSION")
        if self.auth_mode not in {"token", "proxy", "development"}:
            raise ValueError("Invalid AUTH_MODE")
        if self.auth_mode == "token" and not self.operator_token:
            raise ValueError("OPERATOR_TOKEN is required for token authentication")
        if self.auth_mode == "development" and self.mode != "scripted":
            raise ValueError("Development authentication is restricted to scripted mode")
        if self.mode == "live" and not self.webhook_secret:
            raise ValueError("Live mode requires GITHUB_WEBHOOK_SECRET")
        if self.lease_seconds <= self.heartbeat_seconds * 2:
            raise ValueError("Task lease must exceed two heartbeat intervals")

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url.replace("postgres://", "postgresql+psycopg://", 1).replace(
            "postgresql://", "postgresql+psycopg://", 1
        )

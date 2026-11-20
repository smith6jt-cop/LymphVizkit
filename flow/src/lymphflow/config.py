"""Runtime settings, read once from the environment (LYMPH_FLOW_* variables).

Defaults are the safe ones: single-user dev identity, loopback bind, uploads capped,
server-side folder registration disabled. Anything that widens exposure has to be
switched on explicitly (see flow/README.md and deploy/DEPLOY.md).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENGINE_VERSION = "1"  # bump when gate/scale/compensation semantics change: invalidates mask cache


def _default_data_dir() -> Path:
    # Source checkout: <repo>/flow/src/lymphflow/config.py -> <repo>/data/flow, which the
    # repository's `data/**` ignore rule already keeps out of git.
    here = Path(__file__).resolve()
    repo = here.parents[3] if len(here.parents) > 3 else None
    if repo is not None and (repo / "flow" / "pyproject.toml").is_file():
        return repo / "data" / "flow"
    return Path.cwd() / "lymphflow_data"


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    auth_mode: str = "dev"              # "dev" | "proxy"
    user_header: str = ""               # required when auth_mode == "proxy"
    host: str = "127.0.0.1"
    port: int = 8765
    url_prefix: str = ""                # e.g. "/flow" when the proxy does not strip the prefix
    max_upload_mb: int = 2048
    register_roots: tuple[Path, ...] = ()
    keep_acquisition_times: bool = False  # $DATE/$BTIM/$ETIM are dropped unless opted in
    cache_mb: int = 4096                # in-memory column / mask cache budget
    job_python: str = ""                # interpreter for job subprocesses ("" = this one)
    extra: dict = field(default_factory=dict)

    # ---- derived paths -------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "lymphflow.sqlite"

    @property
    def events_dir(self) -> Path:
        return self.data_dir / "events"

    @property
    def masks_dir(self) -> Path:
        return self.data_dir / "masks"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "incoming"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.events_dir, self.masks_dir, self.jobs_dir, self.incoming_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_settings(env: dict | None = None) -> Settings:
    e = os.environ if env is None else env
    get = e.get

    auth_mode = (get("LYMPH_FLOW_AUTH_MODE") or "dev").strip().lower()
    if auth_mode not in {"dev", "proxy"}:
        raise ValueError(f"LYMPH_FLOW_AUTH_MODE must be 'dev' or 'proxy', got {auth_mode!r}")
    user_header = (get("LYMPH_FLOW_USER_HEADER") or "").strip()
    if auth_mode == "proxy" and not user_header:
        # Refuse to start rather than fall back to an unauthenticated mode.
        raise ValueError(
            "LYMPH_FLOW_AUTH_MODE=proxy requires LYMPH_FLOW_USER_HEADER (the header in which "
            "the reverse proxy passes the authenticated user name)."
        )

    prefix = (get("LYMPH_FLOW_URL_PREFIX") or "").strip()
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    prefix = prefix.rstrip("/")

    roots = tuple(
        Path(p).expanduser().resolve()
        for p in (get("LYMPH_FLOW_REGISTER_ROOTS") or "").split(os.pathsep)
        if p.strip()
    )
    data_dir = Path(get("LYMPH_FLOW_DATA") or _default_data_dir()).expanduser().resolve()

    return Settings(
        data_dir=data_dir,
        auth_mode=auth_mode,
        user_header=user_header,
        host=(get("LYMPH_FLOW_HOST") or "127.0.0.1").strip(),
        port=int(get("LYMPH_FLOW_PORT") or 8765),
        url_prefix=prefix,
        max_upload_mb=int(get("LYMPH_FLOW_MAX_UPLOAD_MB") or 2048),
        register_roots=roots,
        keep_acquisition_times=_truthy(get("LYMPH_FLOW_KEEP_ACQ_TIMES")),
        cache_mb=int(get("LYMPH_FLOW_CACHE_MB") or 4096),
        job_python=(get("LYMPH_FLOW_JOB_PYTHON") or "").strip(),
    )

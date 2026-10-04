"""Resolve platform runtime boundaries and guard legacy/Profile resource conflicts."""

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from compass_collector.runtime_locks import ProcessLock, RuntimeLockBusy, lock_is_held


@dataclass(frozen=True)
class PlatformRuntime:
    """Keep logs, controls and operation locks under one explicit platform scope."""

    # root 保持既有runtime位置，避免搬迁数据库或已登录Profile。
    root: Path
    # platform 只能是实际注册的两种平台标识，不能用作任意路径。
    platform: str

    def __post_init__(self) -> None:
        """Reject unknown platform names before resolving writable directories."""
        if self.platform not in {"compass", "taobao"}:
            raise ValueError("unsupported runtime platform")

    @property
    def logs(self) -> Path:
        """Return the platform's daily event directory."""
        return self.root / "logs" / self.platform

    @property
    def controls(self) -> Path:
        """Return the platform's instance-addressed Scheduler controls."""
        return self.root / "controls" / self.platform

    def lock_path(self, role: str) -> Path:
        """Return one allowlisted responsibility lock within the platform."""
        if role not in {"gui", "collection", "scheduler"}:
            raise ValueError("unsupported runtime lock role")
        return self.root / "locks" / self.platform / f"{role}.lock"

    def profile_lock_path(self, profile: Path) -> Path:
        """Address the same Profile resource across differently named configs."""
        # 哈希只使用规范化目录，不把Profile路径写入锁元数据。
        identity = sha256(str(profile.resolve()).encode("utf-8")).hexdigest()
        return self.root / "locks" / "profiles" / f"{identity}.lock"

    def reject_legacy_operations(self) -> None:
        """Require old unscoped processes to finish without terminating them."""
        # 两个新进程的探测必须串行，不能把另一个探测的短锁误当作旧进程。
        coordination = ProcessLock(self.root / "locks" / "legacy-probe.lock", "legacy_probe")
        coordination.acquire_wait(timeout=5)
        try:
            for role in ("gui", "collection", "scheduler"):
                if lock_is_held(self.root / "locks" / f"{role}.lock", role):
                    raise RuntimeLockBusy(f"legacy_{role}")
        finally:
            coordination.release()

    def owns_legacy_task(self, day: str, task_id: str) -> bool:
        """Require source manifests to establish legacy date/task folder ownership."""
        # 只有配置中安全的任务目录名可用于寻址，缺少来源证据的旧目录保留。
        if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
            return False
        source = self.root / "raw" / day / task_id
        if source.is_symlink() or not source.resolve().is_relative_to(self.root.resolve()):
            return False
        # 原始Manifest是旧目录归属的证据，不能仅凭任务名称猜测平台。
        manifests = [*source.glob("*/manifest.json")]
        if (source / "manifest.json").is_file():
            manifests.append(source / "manifest.json")
        if not manifests:
            return False
        for manifest in manifests:
            if manifest.is_symlink() or not manifest.resolve().is_relative_to(source.resolve()):
                return False
            try:
                # 旧无平台Manifest属于原抖音实现，不回填成淘宝。
                identity = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(identity, dict) or identity.get("platform", "compass") != self.platform:
                    return False
            except (OSError, ValueError):
                return False
        return True

    @contextmanager
    def operation(self, role: str, profile: Path | None = None):
        """Hold scoped operation and optional Profile locks until normal cleanup."""
        self.reject_legacy_operations()
        with ProcessLock(self.lock_path(role), f"{self.platform}_{role}"):
            if profile is None:
                yield
            else:
                with ProcessLock(self.profile_lock_path(profile), "profile"):
                    yield

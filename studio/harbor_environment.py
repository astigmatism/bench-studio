"""A Harbor Docker environment with static, fail-closed network isolation."""

import json
from pathlib import Path
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import NetworkMode


class IsolatedDocker(DockerEnvironment):
    def __init__(self, *args, run_id=None, **kwargs):
        if not run_id or not all(c.isalnum() or c in "-_" for c in run_id):
            raise ValueError("A valid Bench Studio run ID is required")
        self._studio_run_id = run_id
        super().__init__(*args, **kwargs)
        # This file is read by Compose inside the controller. It grants no extra mounts.
        self._studio_overlay = Path("/tmp") / (
            "bench-studio-" + run_id + "-" + self.session_id + ".json"
        )
        self._studio_overlay.write_text(
            json.dumps(
                {
                    "services": {
                        "main": {
                            "network_mode": "none",
                            "cap_drop": ["ALL"],
                            "security_opt": ["no-new-privileges:true"],
                            "pids_limit": 512,
                            "labels": {
                                "io.bench-studio.run": run_id,
                                "io.service-portal.hidden": "true",
                            },
                        }
                    }
                }
            )
        )

    @staticmethod
    def _requires_egress_control(*args, **kwargs):
        return False

    @property
    def capabilities(self):
        return super().capabilities.model_copy(
            update={"disable_internet": True, "dynamic_network_policy": True}
        )

    def _write_mounts_compose_file(self):
        # Root without DAC_OVERRIDE cannot write host-owned log directories.
        # Grant access only to this trial's writable output mounts.
        for mount in self._mounts:
            if not mount.get("read_only"):
                source = Path(mount["source"])
                source.mkdir(parents=True, exist_ok=True)
                source.chmod(0o777)
        return super()._write_mounts_compose_file()

    @property
    def _docker_compose_paths(self):
        return [*super()._docker_compose_paths, self._studio_overlay]

    async def _apply_network_policy(self, network_policy):
        if network_policy.network_mode != NetworkMode.NO_NETWORK:
            raise RuntimeError("Bench Studio task containers must have no network")

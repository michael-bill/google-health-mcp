import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "image,success", [("google-health-mcp:abc123", True), ("google-health-mcp:old", False)]
)
def test_deployment_detects_swarm_rollback(tmp_path, image, success):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "deploy.sh", checkout / "deploy.sh")
    (checkout / ".env").write_text("HEALTH_DOMAIN=health.example.com\n")
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, body in {
        "git": "echo abc123\n",
        "docker": 'if [ "$1 $2" = "service inspect" ]; then printf "%s\\n" "$TEST_IMAGE"; fi\n',
    }.items():
        command = commands / name
        command.write_text("#!/bin/sh\n" + body)
        command.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "TEST_IMAGE": image,
    }
    result = subprocess.run(
        ["sh", str(checkout / "deploy.sh"), "--updated"], env=env, capture_output=True, text=True
    )
    assert (result.returncode == 0) is success
    if not success:
        assert "rolled back" in result.stderr

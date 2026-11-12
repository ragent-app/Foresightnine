"""The backup helper must keep synthetic private dumps out of the checkout."""
from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/deployment/backup-staging.sh"


def test_backup_rejects_checkout_and_symlink_to_checkout(tmp_path):
    alias = tmp_path / "checkout"
    alias.symlink_to(ROOT, target_is_directory=True)
    for target in (ROOT, alias / "backups"):
        result = subprocess.run(["bash", str(SCRIPT), str(target)], capture_output=True, text=True)
        assert result.returncode == 2


def test_backup_failure_removes_partial_outputs_and_success_uses_private_modes(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text('''#!/usr/bin/env bash
case " $* " in
  *" pg_dump "*) printf 'synthetic-postgres' ;;
  *" redis-cli --rdb "*) if [[ "${FAIL_REDIS:-}" == 1 ]]; then exit 1; fi ;;
  *" cp "*) printf 'synthetic-redis' > "${!#}" ;;
  *" exec -T redis rm "*) : ;;
  *) exit 9 ;;
esac
''')
    docker.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + ":" + os.environ["PATH"])
    failed = tmp_path / "failed"
    result = subprocess.run(["bash", str(SCRIPT), str(failed)], env=dict(env, FAIL_REDIS="1"), capture_output=True)
    assert result.returncode != 0 and list(failed.iterdir()) == []
    output = tmp_path / "success"
    subprocess.run(["bash", str(SCRIPT), str(output)], env=env, check=True, capture_output=True)
    files = list(output.iterdir())
    assert len(files) == 3 and all(item.stat().st_mode & 0o777 == 0o600 for item in files)
    checksum = next(output.glob("sha256-*.txt"))
    subprocess.run(["sha256sum", "-c", str(checksum)], check=True, capture_output=True)

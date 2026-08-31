#!/usr/bin/env bash
set -euo pipefail
ROOT=${1:-/home/lxx/Foresightnine/src}
BASE=${2:-/home/lxx/.codex/oauth-config-baseline}
python3 - "$ROOT" "$BASE" <<'PYINNER'
from pathlib import Path
import shutil, sys
root=Path(sys.argv[1]); base=Path(sys.argv[2])
api=root/'infra/docker/staging.env'
shutil.copyfile(base/'staging.env', api)
web=root/'apps/web/.env.production.local'
if (base/'.env.production.local.absent').exists(): web.unlink(missing_ok=True)
else: shutil.copyfile(base/'.env.production.local', web)
dist=root/'apps/web/dist'
if dist.exists(): shutil.rmtree(dist)
shutil.copytree(base/'dist', dist)
print('restored_oauth_config_and_web_build='+str(root))
PYINNER

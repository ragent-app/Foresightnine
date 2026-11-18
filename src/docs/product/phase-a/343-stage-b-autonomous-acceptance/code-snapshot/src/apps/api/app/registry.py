from __future__ import annotations

from pathlib import Path

from .models import AssetRegistryEntry, AssetRegistrySnapshot

REGISTRY_PATH = Path(__file__).resolve().parents[3] / "packages" / "asset-registry" / "registry" / "v1" / "assets.json"
REGISTRY = AssetRegistrySnapshot.model_validate_json(REGISTRY_PATH.read_text(encoding="utf-8"))
ASSET_INDEX: dict[str, AssetRegistryEntry] = {asset.asset_id: asset for asset in REGISTRY.assets}


def get_asset(asset_id: str) -> AssetRegistryEntry | None:
    return ASSET_INDEX.get(asset_id)


def search_assets(query: str) -> tuple[AssetRegistryEntry, ...]:
    normalized = query.strip().casefold()
    if not normalized:
        return REGISTRY.assets
    return tuple(
        asset for asset in REGISTRY.assets
        if normalized in asset.asset_id.casefold()
        or normalized in asset.display_name.casefold()
        or normalized in asset.economic_exposure_id.casefold()
    )

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from thoth_terminal.security import StartupSecurityError, build_security_posture


@dataclass(frozen=True)
class MoodAsset:
    mood: str
    intensity: int
    frames: list[str] = field(default_factory=list)
    frame_duration_ms: int = 240
    caption: str | None = None
    palette: str = "neutral"
    overlays: list[str] = field(default_factory=list)


class MoodAssetProvider(Protocol):
    def manifest(self) -> dict[str, MoodAsset]:
        ...

    def asset_for(self, mood: str, intensity: int) -> MoodAsset:
        ...


class OriginalPublicAssetProvider:
    def __init__(self) -> None:
        self._manifest = {
            mood: MoodAsset(
                mood=mood,
                intensity=0,
                frames=[],
                frame_duration_ms=260,
                caption=None,
                palette=_palette_for_mood(mood),
            )
            for mood in [
                "neutral",
                "happy",
                "ecstatic",
                "manic",
                "sad",
                "depressed",
                "miserable",
                "angry",
                "enraged",
                "afraid",
                "stressed",
                "terrified",
            ]
        }

    def manifest(self) -> dict[str, MoodAsset]:
        return dict(self._manifest)

    def asset_for(self, mood: str, intensity: int) -> MoodAsset:
        asset = self._manifest.get(str(mood or "neutral").lower())
        if asset is None:
            asset = self._manifest["neutral"]
        return MoodAsset(
            mood=asset.mood,
            intensity=max(0, min(3, int(intensity or 0))),
            frames=list(asset.frames),
            frame_duration_ms=asset.frame_duration_ms,
            caption=asset.caption,
            palette=asset.palette,
            overlays=list(asset.overlays),
        )


class OmoriLocalAssetProvider:
    def __init__(self, *, settings: Any, manifest_path: Path | None = None) -> None:
        posture = build_security_posture(settings)
        if posture.public or posture.public_build or not posture.local_desktop:
            raise StartupSecurityError("Omori local assets are only allowed in local_desktop mode.")
        if not bool(getattr(settings, "enable_omori_local_assets", False)):
            raise StartupSecurityError("Omori local assets require ENABLE_OMORI_LOCAL_ASSETS=true.")
        root = Path(getattr(settings, "omori_public_asset_root"))
        self._manifest_path = manifest_path or (root / "mood_manifest.json")

    def manifest(self) -> dict[str, MoodAsset]:
        if not self._manifest_path.exists():
            return {}
        try:
            payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        out: dict[str, MoodAsset] = {}
        for mood, raw in payload.items():
            if not isinstance(raw, dict):
                continue
            out[str(mood)] = MoodAsset(
                mood=str(mood),
                intensity=int(raw.get("intensity") or 0),
                frames=[str(item) for item in raw.get("frames") or [] if str(item).strip()],
                frame_duration_ms=int(raw.get("frame_duration_ms") or 240),
                caption=str(raw.get("caption")) if raw.get("caption") else None,
                palette=str(raw.get("palette") or _palette_for_mood(str(mood))),
                overlays=[str(item) for item in raw.get("overlays") or [] if str(item).strip()],
            )
        return out

    def asset_for(self, mood: str, intensity: int) -> MoodAsset:
        manifest = self.manifest()
        key = str(mood or "neutral").lower()
        if key in manifest:
            return manifest[key]
        return OriginalPublicAssetProvider().asset_for(key, intensity)


def mood_asset_provider(settings: Any) -> MoodAssetProvider:
    if bool(getattr(settings, "enable_omori_local_assets", False)):
        return OmoriLocalAssetProvider(settings=settings)
    return OriginalPublicAssetProvider()


def mood_asset_payload(settings: Any, mood: str, intensity: int) -> dict[str, Any]:
    asset = mood_asset_provider(settings).asset_for(mood, intensity)
    return {
        "mood": asset.mood,
        "intensity": asset.intensity,
        "frames": asset.frames,
        "frame_duration_ms": asset.frame_duration_ms,
        "caption": asset.caption,
        "palette": asset.palette,
        "overlays": asset.overlays,
    }


def _palette_for_mood(mood: str) -> str:
    mood = str(mood or "").lower()
    if mood in {"happy", "ecstatic", "manic"}:
        return "euphoric"
    if mood in {"sad", "depressed", "miserable"}:
        return "drawdown"
    if mood in {"angry", "enraged"}:
        return "rage"
    if mood in {"afraid", "stressed", "terrified"}:
        return "risk"
    return "neutral"

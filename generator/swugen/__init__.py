"""Synthetic data generator for the Small Water Utility Cyber Lab (Level 1)."""
import json
import os
from pathlib import Path

PROFILE_DIR = Path(os.environ.get("SWU_PROFILE_DIR", Path(__file__).resolve().parents[2] / "profiles"))


def load_profile(utility_id: str, profile_dir: Path = PROFILE_DIR) -> dict:
    from .configuration import validate_profile
    if not utility_id.isidentifier():
        raise ValueError("Utility ID must be an identifier")
    profile = json.loads((profile_dir / f"{utility_id}.json").read_text(encoding="utf-8"))
    validate_profile(profile)
    if profile["id"] != utility_id:
        raise ValueError(f"Profile ID does not match filename: {utility_id}")
    return profile


def load_profiles(profile_dir: Path = PROFILE_DIR) -> dict:
    profiles = {p.stem: load_profile(p.stem, profile_dir) for p in sorted(profile_dir.glob("*.json"))}
    if not profiles:
        raise ValueError(f"No utility profiles found in {profile_dir}")
    return profiles

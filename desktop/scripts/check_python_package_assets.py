"""Verify pinned public media and notices in built wheel and source archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image

KURISU_SHA256 = "cca259ac33ffc7c8170b401a315f9a177a865eb063ba44a4da87c3ab13fa90b7"
KURISU_ICON_SHA256 = "ded30eeb568f26e3df64998131698e472603bf48d531885b27a7c04293d3b0b5"
GENERIC_ICON_SHA256 = "2d9795265224b99619d34320e57b070a081ebc1c55df0152fd3041242dbd953e"
VALID_FRAME_COUNTS = (6, 8, 8, 4, 5, 8, 6, 6, 6)
REQUIRED_FILES = (
    "resources/app_icon/LICENSE.txt",
    "resources/app_icon/amadeus-kurisu.png",
    "resources/app_icon/spritesheet.png",
    "resources/builtin_pet/LICENSE.txt",
    "resources/builtin_pet/pet.amadeus.json",
    "resources/builtin_pet/spritesheet.webp",
    "resources/licenses/CC0-1.0.txt",
    "resources/licenses/KURISU-ASSET-NOTICE.txt",
    "resources/licenses/KURISU-ICON-NOTICE.txt",
    "resources/licenses/runtime-license-manifest.json",
    "resources/provider_catalog/providers.json",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    return parser


def _single_artifact(dist: Path, pattern: str, label: str) -> Path:
    artifacts = sorted(path for path in dist.glob(pattern) if path.is_file())
    if len(artifacts) != 1:
        raise ValueError(f"expected exactly one {label} artifact, found {len(artifacts)}")
    return artifacts[0]


def _read_wheel(path: Path) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        for relative in REQUIRED_FILES:
            expected = f"amadeus_desktop/{relative}"
            matches = [member for member in members if member.filename == expected]
            if len(matches) != 1:
                raise ValueError(f"wheel member count is invalid for {relative}")
            payloads[relative] = archive.read(matches[0])
    return payloads


def _read_sdist(path: Path) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        for relative in REQUIRED_FILES:
            suffix = f"/src/amadeus_desktop/{relative}"
            matches = [
                member for member in members if member.isfile() and member.name.endswith(suffix)
            ]
            if len(matches) != 1:
                raise ValueError(f"sdist member count is invalid for {relative}")
            handle = archive.extractfile(matches[0])
            if handle is None:
                raise ValueError(f"sdist member is unreadable for {relative}")
            payloads[relative] = handle.read()
    return payloads


def _verify_payloads(label: str, payloads: dict[str, bytes]) -> None:
    sheet = payloads["resources/builtin_pet/spritesheet.webp"]
    if hashlib.sha256(sheet).hexdigest() != KURISU_SHA256:
        raise ValueError(f"{label} built-in pet hash is invalid")
    icon = payloads["resources/app_icon/amadeus-kurisu.png"]
    if hashlib.sha256(icon).hexdigest() != KURISU_ICON_SHA256:
        raise ValueError(f"{label} Kurisu application icon hash is invalid")
    if hashlib.sha256(payloads["resources/app_icon/spritesheet.png"]).hexdigest() != (
        GENERIC_ICON_SHA256
    ):
        raise ValueError(f"{label} generic fallback icon hash is invalid")

    with Image.open(BytesIO(icon)) as image:
        image.load()
        if image.format != "PNG" or image.mode not in {"RGB", "RGBA"} or image.size != (1254, 1254):
            raise ValueError(f"{label} Kurisu application icon metadata is invalid")

    with Image.open(BytesIO(sheet)) as image:
        image.load()
        if image.format != "WEBP" or image.mode != "RGBA" or image.size != (6144, 7488):
            raise ValueError(f"{label} built-in pet image metadata is invalid")
        alpha = image.getchannel("A")
        for row, frame_count in enumerate(VALID_FRAME_COUNTS):
            for column in range(8):
                box = (
                    column * 768,
                    row * 832,
                    (column + 1) * 768,
                    (row + 1) * 832,
                )
                is_empty = alpha.crop(box).getbbox() is None
                if is_empty != (column >= frame_count):
                    raise ValueError(f"{label} built-in pet frame occupancy is invalid")

    pet_manifest = json.loads(payloads["resources/builtin_pet/pet.amadeus.json"].decode("utf-8"))
    sheet_spec = pet_manifest.get("spritesheet", {})
    if (
        pet_manifest.get("id") != "builtin-amadeus"
        or pet_manifest.get("license") != "NOASSERTION"
        or sheet_spec.get("path") != "spritesheet.webp"
        or sheet_spec.get("frameWidth") != 768
        or sheet_spec.get("frameHeight") != 832
        or sheet_spec.get("logicalFrameWidth") != 192
        or sheet_spec.get("logicalFrameHeight") != 208
        or sheet_spec.get("columns") != 8
        or sheet_spec.get("rows") != 9
    ):
        raise ValueError(f"{label} built-in pet manifest is invalid")

    for relative in (
        "resources/builtin_pet/LICENSE.txt",
        "resources/licenses/KURISU-ASSET-NOTICE.txt",
    ):
        notice = payloads[relative].decode("utf-8")
        if (
            "NOASSERTION" not in notice
            or KURISU_SHA256 not in notice
            or "not independently verified" not in notice
        ):
            raise ValueError(f"{label} Kurisu notice is incomplete")

    license_manifest = json.loads(
        payloads["resources/licenses/runtime-license-manifest.json"].decode("utf-8")
    )
    components = {
        component.get("name"): component
        for component in license_manifest.get("bundled_components", [])
    }
    component_name = "Amadeus built-in Kurisu 4x high-resolution spritesheet derivative"
    if components.get(component_name) != {
        "name": component_name,
        "version": f"sha256:{KURISU_SHA256}",
        "license": "NOASSERTION",
        "source": "resources/builtin_pet/LICENSE.txt",
    }:
        raise ValueError(f"{label} runtime asset license identity is invalid")
    icon_component_name = "Amadeus Kurisu portrait application icon derivative"
    if components.get(icon_component_name) != {
        "name": icon_component_name,
        "version": f"sha256:{KURISU_ICON_SHA256}",
        "license": "NOASSERTION",
        "source": "resources/licenses/KURISU-ICON-NOTICE.txt",
    }:
        raise ValueError(f"{label} runtime icon license identity is invalid")
    icon_notice = payloads["resources/licenses/KURISU-ICON-NOTICE.txt"].decode("utf-8")
    normalized_icon_notice = " ".join(icon_notice.split())
    if (
        "NOASSERTION" not in icon_notice
        or KURISU_ICON_SHA256 not in icon_notice
        or "not independently verified" not in normalized_icon_notice
    ):
        raise ValueError(f"{label} Kurisu application icon notice is incomplete")

    provider_catalog = json.loads(
        payloads["resources/provider_catalog/providers.json"].decode("utf-8")
    )
    providers = provider_catalog.get("providers")
    if provider_catalog.get("schema_version") != 1 or not isinstance(providers, list):
        raise ValueError(f"{label} provider catalog is invalid")
    provider_ids = {provider.get("id") for provider in providers if isinstance(provider, dict)}
    required_provider_ids = {
        "deepseek",
        "mimo_payg",
        "openai",
        "qwen_cn",
        "qwen_intl",
        "gemini",
        "glm",
        "kimi_payg",
        "doubao_ark",
        "minimax_cn",
        "minimax_intl",
        "siliconflow",
        "stepfun",
        "grok",
        "openrouter",
        "anthropic",
        "custom_openai",
        "custom_anthropic",
        "ollama",
        "lm_studio",
        "vllm",
    }
    if provider_ids != required_provider_ids:
        raise ValueError(f"{label} provider catalog range is invalid")


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        dist = arguments.dist.resolve(strict=True)
        wheel = _single_artifact(dist, "*.whl", "wheel")
        sdist = _single_artifact(dist, "*.tar.gz", "sdist")
        _verify_payloads("wheel", _read_wheel(wheel))
        _verify_payloads("sdist", _read_sdist(sdist))
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": "passed",
                "archives": 2,
                "asset_sha256": KURISU_SHA256,
                "icon_sha256": KURISU_ICON_SHA256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

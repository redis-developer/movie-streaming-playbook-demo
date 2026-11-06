"""Bisect which part of our seed Skill package the cloud Playbook rejects. Creates throwaway
draft Skills (keys bisect-*) and deletes each one it created.

    bash scripts/with_env.sh .venv/bin/python -m scripts.skill_package_bisect
"""
import base64
import io
import json
import zipfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from redis_iris import AgentPlaybooks  # noqa: E402

client = AgentPlaybooks.from_env().client
SKILL_DIR = Path(__file__).resolve().parent.parent / "resources" / "playbook" / "skills" / "check-refund-status"
original = (SKILL_DIR / "SKILL.md").read_text()
refs = {p.relative_to(SKILL_DIR).as_posix(): p.read_text() for p in SKILL_DIR.rglob("*.md") if p.name != "SKILL.md"}


def package(skill_md: str, root: str | None, include_refs: bool, deflate: bool = False, attrs: bool = False) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED) as z:
        files = {"SKILL.md": skill_md, **(refs if include_refs else {})}
        for path, text in files.items():
            name = f"{root}/{path}" if root else path
            if attrs:
                info = zipfile.ZipInfo(name)
                info.external_attr = (0o100644 & 0xFFFF) << 16
                z.writestr(info, text)
            else:
                z.writestr(name, text)
    return base64.b64encode(buf.getvalue()).decode()


def strip(frontmatter_keys: list[str]) -> str:
    head, _, body = original.partition("\n---\n")
    lines = head.splitlines()[1:]  # drop leading ---
    kept, skip_block = [], False
    for line in lines:
        key = line.split(":", 1)[0].strip()
        if not line.startswith((" ", "-")):
            skip_block = key in frontmatter_keys
        if not skip_block:
            kept.append(line)
    return "---\n" + "\n".join(kept) + "\n---\n" + body


def attempt(label, pkg, key):
    try:
        result = client.create_skill(package_base64=pkg, entry_key=key)
        print(f"OK    {label}")
        try:
            client.delete_skill(skill_id=result.entry.entry_id)
        except Exception as exc:  # noqa: BLE001
            print(f"      (cleanup failed: {exc})")
    except Exception as exc:  # noqa: BLE001
        data = getattr(exc, "data", None)
        detail = data.model_dump(by_alias=True, mode="json", exclude_none=True) if hasattr(data, "model_dump") else str(exc)
        print(f"FAIL  {label}: {json.dumps(detail)[:300]}")


print("frontmatter:\n" + original.split("\n---\n")[0] + "\n---")
attempt("as seeded (root folder, deflate, attrs, refs)", package(original, "check-refund-status", True, True, True), "bisect-a")
attempt("root folder, stored, no attrs, refs", package(original, "check-refund-status", True), "bisect-b")
attempt("no root folder, refs", package(original, None, True), "bisect-c")
attempt("root folder, SKILL.md only", package(original, "check-refund-status", False), "bisect-d")
attempt("no allowed-tools", package(strip(["allowed-tools"]), "check-refund-status", True), "bisect-e")
attempt("no metadata", package(strip(["metadata"]), "check-refund-status", True), "bisect-f")
attempt("no license/compatibility", package(strip(["license", "compatibility"]), "check-refund-status", True), "bisect-g")
attempt("name+description only", package(strip(["license", "compatibility", "metadata", "allowed-tools"]), "check-refund-status", True), "bisect-h")
minimal = "---\nname: check-refund-status\ndescription: Look up the live status of a refund.\n---\n# Check refund status\n\nSteps.\n"
attempt("minimal body, same refs", package(minimal, "check-refund-status", True), "bisect-i")

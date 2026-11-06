"""
Skill packages
==============
A Playbook Skill is submitted as a base64 ZIP archive in the `agent-skills` format:

    <root>/SKILL.md              YAML frontmatter (name, description, ...) + instructions
    <root>/references/*.md       supporting documents the agent loads on demand
    <root>/scripts/*             optional files; Playbook never extracts or executes them

The service parses the archive in memory, extracts the manifest and instructions
from SKILL.md, and records one immutable resource descriptor per file. Agents get
the compact preview (name, description, SKILL.md URI) from discovery, then load the
full instructions and only the resources they need.
"""
import base64
import hashlib
import io
import mimetypes
import posixpath
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from .errors import invalid

SKILL_MARKDOWN = "SKILL.md"
SKILL_FORMAT = "agent-skills"
URI_TEMPLATE = "skill://{entry_id}/{version}/{skill_name}/{path}"

TEXT_MIME_BY_EXT = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".csv": "text/csv",
}


def resource_id_for(path: str) -> str:
    """Deterministic opaque resource id: res_ + 24 hex chars."""
    return "res_" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def guess_mime(path: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    if ext in TEXT_MIME_BY_EXT:
        return TEXT_MIME_BY_EXT[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def skill_uri(entry_id: str, version: int, skill_name: str, path: str) -> str:
    return URI_TEMPLATE.format(entry_id=entry_id, version=version, skill_name=skill_name, path=path)


def parse_skill_uri(uri: str) -> Tuple[str, int, str, str]:
    """skill://{entryId}/{version}/{skillName}/{path} -> (entry_id, version, skill_name, path)"""
    if not uri.startswith("skill://"):
        raise invalid(f"not a skill URI: {uri}")
    rest = uri[len("skill://"):]
    parts = rest.split("/", 3)
    if len(parts) != 4:
        raise invalid(f"malformed skill URI: {uri}")
    entry_id, version, skill_name, path = parts
    try:
        version_number = int(version)
    except ValueError as exc:
        raise invalid(f"malformed skill URI version: {uri}") from exc
    return entry_id, version_number, skill_name, path


def build_package_from_dir(directory: Path) -> str:
    """Zip a skill directory (containing SKILL.md) and return base64 for submission."""
    directory = Path(directory)
    if not (directory / SKILL_MARKDOWN).exists():
        raise invalid(f"{directory} has no {SKILL_MARKDOWN}")
    output = io.BytesIO()
    root = directory.name
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(directory.rglob("*")):
            if file_path.is_file() and not file_path.name.startswith("."):
                relative = file_path.relative_to(directory).as_posix()
                info = zipfile.ZipInfo(f"{root}/{relative}")
                info.compress_type = zipfile.ZIP_DEFLATED
                if file_path.suffix in (".sh", ".py") and (file_path.stat().st_mode & 0o111):
                    info.external_attr = (0o100755 & 0xFFFF) << 16
                else:
                    info.external_attr = (0o100644 & 0xFFFF) << 16
                archive.writestr(info, file_path.read_bytes())
    return base64.b64encode(output.getvalue()).decode("ascii")


def parse_frontmatter(markdown: bytes) -> Tuple[dict, str]:
    try:
        text = markdown.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise invalid("SKILL.md must be UTF-8") from exc
    if not text.startswith("---"):
        raise invalid("SKILL.md must begin with YAML frontmatter")
    lines = text.split("\n")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise invalid("SKILL.md frontmatter is not terminated")
    frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(frontmatter, dict):
        raise invalid("SKILL.md frontmatter must be a mapping")
    instructions = "\n".join(lines[end + 1:]).strip("\n")
    return frontmatter, instructions


def parse_package(package_base64: str) -> Tuple[dict, str, List[dict], Dict[str, bytes]]:
    """
    Parse a base64 ZIP skill package.

    Returns (manifest, instructions, resource_descriptors_without_uri, file_bytes_by_path).
    Descriptors get their `uri` once the entry id and version are known.
    """
    try:
        archive_bytes = base64.b64decode(package_base64, validate=True)
    except Exception as exc:
        raise invalid("packageBase64 is not valid base64") from exc
    try:
        reader = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise invalid("package is not a ZIP archive") from exc

    files: Dict[str, bytes] = {}
    executable: Dict[str, bool] = {}
    root: Optional[str] = None
    for info in reader.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if name.startswith("/") or ".." in name.split("/"):
            raise invalid(f"unsafe path in package: {name}")
        parts = name.split("/")
        if len(parts) < 2:
            raise invalid("package files must live under a single root directory")
        if root is None:
            root = parts[0]
        elif parts[0] != root:
            raise invalid("package must have exactly one root directory")
        relative = "/".join(parts[1:])
        files[relative] = reader.read(info)
        mode = (info.external_attr >> 16) & 0o777
        executable[relative] = bool(mode & 0o111)

    if SKILL_MARKDOWN not in files:
        raise invalid("root SKILL.md is required")

    frontmatter, instructions = parse_frontmatter(files[SKILL_MARKDOWN])
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not name or not description:
        raise invalid("SKILL.md frontmatter requires name and description")

    manifest = {
        "name": str(name),
        "description": str(description),
    }
    for key, target in (("license", "license"), ("compatibility", "compatibility"), ("metadata", "metadata")):
        if key in frontmatter and frontmatter[key] is not None:
            manifest[target] = frontmatter[key]
    # agent-skills frontmatter: `allowed-tools: tool_a tool_b` (space-separated string, as the
    # Redis Playbook service requires); a YAML list is accepted here for convenience.
    allowed = frontmatter.get("allowed-tools") or frontmatter.get("allowedTools")
    if isinstance(allowed, str):
        manifest["allowedTools"] = allowed.split()
    elif allowed:
        manifest["allowedTools"] = [str(item) for item in allowed]
    if "metadata" in manifest and isinstance(manifest["metadata"], dict):
        manifest["metadata"] = {str(k): str(v) for k, v in manifest["metadata"].items()}

    descriptors = []
    for path in sorted(files):
        data = files[path]
        descriptors.append({
            "resourceId": resource_id_for(path),
            "path": path,
            "digest": digest_bytes(data),
            "sizeBytes": len(data),
            "mimeType": guess_mime(path),
            "executable": executable.get(path, False),
        })
    return manifest, instructions, descriptors, files

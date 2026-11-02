"""Installed Agent Skills (``SKILL.md``) discovery and workspace injection.

A skill is a directory holding a ``SKILL.md`` file with YAML frontmatter
(``name`` + ``description``). The layouts below were verified against the
installed runtimes on this machine by rendering the model-visible prompt with
``codex debug prompt-input``:

* ``$CODEX_HOME/skills`` (default ``~/.codex/skills``) is always a root.
* ``<workspace>/.codex/skills`` and ``<workspace>/.agents/skills`` are project roots.
* ``--ignore-user-config`` disables skill discovery completely, so the built-in
  Codex adapter must not pass it.
* ``-c skills.config=[{name="x",enabled=false}]`` keeps a skill out of the prompt.

Only file paths named by the server's own discovery are ever read. A browser
request contributes a set of skill names, never a path.
"""

import os
import shutil
import unicodedata
from pathlib import Path

from .errors import CoordinationError

FRONTMATTER_BYTES = 16_384
DESCRIPTION_LIMIT = 1_024
CHILD_LIMIT = 64
FILE_LIMIT = 4_096
SOFTWARE_LABELS = {
    "codex": "Codex",
    "opencode": "OpenCode",
    "zcode": "ZCode",
    "dsh": "DeepSeek Harness",
}


def _home(env_name, default):
    """Resolve a runtime home: an explicit environment override, else the default."""
    value = os.environ.get(env_name)
    return Path(value) if value else Path.home() / default


def _opencode_config_home():
    """OpenCode's global configuration root, honouring XDG on every platform."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) / "opencode") if xdg else (Path.home() / ".config" / "opencode")


def skill_roots(software, workspace=None):
    """Return ``[(path, origin)]`` for a runtime, without reading any file."""
    roots = []
    if workspace is not None:
        workspace = Path(workspace)
    if software == "codex":
        roots.append((_home("CODEX_HOME", ".codex") / "skills", "user"))
        if workspace is not None:
            roots.append((workspace / ".codex" / "skills", "project"))
    elif software == "opencode":
        roots.append((_opencode_config_home() / "skills", "user"))
        if workspace is not None:
            roots.append((workspace / ".opencode" / "skills", "project"))
    elif software == "zcode":
        roots.append((_home("ZCODE_HOME", ".zcode") / "skills", "user"))
        roots.append((_home("ZCODE_HOME", ".zcode") / "plugins" / "skills", "user"))
    elif software == "dsh":
        roots.append((_home("DSH_HOME", ".dsh") / "skills", "user"))
    if workspace is not None:
        # The vendor-neutral `.agents/skills` convention is shared by every runtime.
        roots.append((workspace / ".agents" / "skills", "project"))
    seen, unique = set(), []
    for root, origin in roots:
        key = str(root).casefold()
        if key not in seen:
            seen.add(key)
            unique.append((root, origin))
    return unique


def _frontmatter_line(value):
    """One bounded single-line scalar; never echoes unknown control characters."""
    value = " ".join(str(value).split())
    if len(value) > DESCRIPTION_LIMIT:
        value = value[: DESCRIPTION_LIMIT - 1].rstrip() + "…"
    return (
        " ".join(
            "".join(
                c if c.isprintable() else " " for c in unicodedata.normalize("NFC", value)
            ).split()
        )
        or "未提供描述"
    )


def _display_name(value, fallback):
    """A display label is text, never a path. Fall back to the directory name."""
    text = " ".join(str(value or "").split())
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        text = fallback
    text = "".join(c if c.isprintable() else " " for c in unicodedata.normalize("NFC", text))
    return (" ".join(text.split()) or fallback)[:120]


def read_skill(directory):
    """Parse one skill directory. Returns None when it is not a valid skill."""
    manifest = directory / "SKILL.md"
    try:
        if manifest.is_symlink() or not manifest.is_file():
            return None
        if manifest.stat().st_size > FRONTMATTER_BYTES * 8:
            return None
        with manifest.open("rb") as stream:
            head = stream.read(FRONTMATTER_BYTES)
    except OSError:
        return None
    # A byte-order mark must NOT be tolerated here: the loader rejects such a file
    # ("missing YAML frontmatter delimited by ---"), so reporting it would offer a
    # skill the runtime silently ignores.
    if head.startswith(b"\xef\xbb\xbf"):
        return None
    text = head.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    import yaml

    try:
        closing = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
        data = yaml.safe_load("\n".join(lines[1:closing]))
    except StopIteration:
        return None
    except yaml.YAMLError:
        # Preserve the bounded legacy scalar reader for older imperfect files.
        data = {}
        for line in lines[1:closing]:
            key, _, value = line.partition(":")
            if key.strip() in {"name", "description"} and value.strip():
                data[key.strip()] = value.strip().strip("\"'")
    if not isinstance(data, dict):
        return None
    name = data.get("name") if isinstance(data.get("name"), str) else None
    description = data.get("description") if isinstance(data.get("description"), str) else None
    name = _display_name(name, directory.name)
    try:
        children = sum(
            1 for entry in directory.iterdir() if entry.is_dir() and not entry.is_symlink()
        )
    except OSError:
        children = 0
    return {
        "name": name,
        "description": _frontmatter_line(description or "未提供描述"),
        "path": str(directory),
        "directory": directory.name,
        "files": children,
    }


def _scan(root):
    """Skills directly under a root.

    Verified with `codex debug prompt-input`: only immediate child directories are
    loaded; a nested container such as ``.system/<name>`` is not discovered, so the
    catalogue must not offer skills the runtime would ignore.
    """
    if not root.is_dir():
        return []
    found = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return []
    for entry in entries:
        if len(found) >= CHILD_LIMIT:
            break
        if entry.is_symlink() or not entry.is_dir():
            continue
        skill = read_skill(entry)
        if skill:
            found.append(skill)
    return found


def _disabled_names(software):
    """Names switched off by the runtime's own config, reported so the UI is honest.

    Injection restates the selection explicitly, so a disabled entry does not stop a
    skill from running here; surfacing it avoids promising a skill the vendor hides.
    """
    if software != "codex":
        return set()
    config = _home("CODEX_HOME", ".codex") / "config.toml"
    try:
        if not config.is_file() or config.stat().st_size > 1_048_576:
            return set()
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    disabled, pending = set(), {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("[[") and line.endswith("]]"):
            if pending.get("enabled", "").lower() == "false":
                disabled.update(v for k, v in pending.items() if k in {"name", "path"})
            pending = {}
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key in {"name", "path", "enabled"}:
            if key == "path":
                value = Path(value).parent.name
            pending[key] = value
    if pending.get("enabled", "").lower() == "false":
        disabled.update(v for k, v in pending.items() if k in {"name", "path"})
    return {v for v in disabled if v}


def discover_skills(workspace=None):
    """Group installed skills by software. Duplicates keep the first root's copy."""
    catalogue = []
    for software, label in SOFTWARE_LABELS.items():
        seen, items = set(), []
        disabled = _disabled_names(software)
        for root, origin in skill_roots(software, workspace):
            for skill in _scan(root):
                if skill["name"] in seen:
                    continue
                seen.add(skill["name"])
                items.append(
                    {
                        **skill,
                        "software": software,
                        "origin": origin,
                        "disabled_by_config": skill["name"] in disabled,
                    }
                )
        catalogue.append(
            {
                "software": software,
                "label": label,
                "roots": [
                    {"path": str(root), "origin": origin, "exists": root.is_dir()}
                    for root, origin in skill_roots(software, workspace)
                ],
                "skills": items,
            }
        )
    return {"skills": catalogue, "workspace": str(workspace) if workspace else ""}


def resolve_skill(software, name, workspace=None):
    """Find an installed skill by its catalogue name for a runtime.

    The catalogue displays the frontmatter name (falling back to the directory
    name), so a selection must be matched against that same value. The returned
    skill still carries its real directory path, which injection uses as the
    safe target name. A byte-order-marked manifest is rejected here, matching
    discovery and the runtime, so a selection can never point at a skill the
    loader ignores.
    """
    for root, _origin in skill_roots(software, workspace):
        for skill in _scan(root):
            if skill["name"] == name:
                return skill
    raise CoordinationError(
        "skill_unknown", "本机未发现该技能，请刷新技能列表后重试", {"skill": name}, 404
    )


def validate_skill_selection(names):
    """Validate the shape of a member's skill selection.

    Existence is deliberately not checked here: the correct roots depend on the
    workspace, which the ledger does not know. Injection resolves the names
    against the connected project immediately before execution.
    """
    if not isinstance(names, list) or len(names) > 32:
        raise CoordinationError("invalid_skills", "技能最多选择 32 个", status=422)
    if len(set(names)) != len(names):
        raise CoordinationError("invalid_skills", "技能不能重复选择", status=422)
    for name in names:
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or "/" in name
            or "\\" in name
            or name in {".", ".."}
            or any(ord(c) < 32 for c in name)
        ):
            raise CoordinationError("invalid_skills", "技能名称无效", status=422)
    return names


def validate_skill_names(software, names, workspace=None):
    """Validate a selection and resolve every name in this installation."""
    validate_skill_selection(names)
    for name in names:
        resolve_skill(software, name, workspace)
    return names


INJECTED_DIR = "relayfence-selected"
MARKER_FILE = ".relayfence-injected"
# Each runtime reads a different project-level skill root. ZCode and DSH use the
# vendor-neutral `.agents/skills` convention; staging into `.codex/skills` made
# their selected skills invisible.
PROJECT_SKILL_ROOTS = {
    "codex": (".codex", "skills"),
    "opencode": (".opencode", "skills"),
}


def project_skill_root(workspace, software):
    folder, leaf = PROJECT_SKILL_ROOTS.get(str(software).casefold(), (".agents", "skills"))
    return Path(workspace).resolve() / folder / leaf


def _target_name(skill):
    """Use the source directory name, never untrusted frontmatter, as a path."""
    name = Path(skill["path"]).name
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(c) < 32 for c in name)
    ):
        raise ValueError("技能目录名称无效，已拒绝注入")
    return name


def remove_injected(workspace, software):
    """Remove only a staging directory RelayFence created and marked."""
    staging = project_skill_root(workspace, software) / INJECTED_DIR
    if staging.is_dir() and (staging / MARKER_FILE).is_file():
        shutil.rmtree(staging)


def _manifest(skill, target):
    lines = [
        "Injected by RelayFence for this run only.",
        "skill: " + skill["name"],
        "source: " + skill["path"],
        "target: " + str(target),
    ]
    return "\n".join(lines) + "\n"


def _copy_tree(source, target):
    """Copy only what a skill needs; skip VCS data and never follow symlinks."""
    files = 0
    for path in sorted(source.rglob("*")):
        files += 1
        if files > FILE_LIMIT:
            raise ValueError("技能文件过多，已拒绝注入")
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_symlink():
            continue
        if path.is_dir():
            destination.mkdir(exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    return files


def inject_skills(workspace, software, names):
    """Materialise the selection under the workspace and return its descriptions.

    Files written by a previous run are removed first; nothing outside the
    marker-managed directory is ever deleted, so a user's own skill files are safe.
    """
    workspace = Path(workspace).resolve()
    skills_root = project_skill_root(workspace, software)
    staging = skills_root / INJECTED_DIR
    if staging.is_dir() and (staging / MARKER_FILE).is_file():
        shutil.rmtree(staging)
    elif staging.exists():
        raise ValueError("注入目录已存在且不是 RelayFence 创建的，已拒绝覆盖")
    selected = [resolve_skill(software, name, workspace) for name in names]
    if not selected:
        return []
    # Two display names can share one directory name across roots. Staging both
    # would merge their files under one target, so reject the ambiguous selection.
    targets = [_target_name(skill) for skill in selected]
    if len(set(targets)) != len(targets):
        raise CoordinationError(
            "skill_conflict",
            "所选技能存在同名目录，请只保留其中一个",
            {"skills": [skill["name"] for skill in selected]},
            422,
        )
    skills_root.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    (staging / MARKER_FILE).write_text("relayfence\n", encoding="utf-8")
    injected = []
    for skill in selected:
        target = staging / _target_name(skill)
        # Defence in depth: even a future change to _target_name cannot write
        # outside the marker-managed staging directory.
        if not target.resolve().is_relative_to(staging.resolve()):
            raise ValueError("技能目录越界，已拒绝注入")
        target.mkdir()
        _copy_tree(Path(skill["path"]), target)
        manifest = target / "SKILL.md"
        if not manifest.is_file():
            shutil.copy2(Path(skill["path"]) / "SKILL.md", manifest)
        (target / MARKER_FILE).write_text(_manifest(skill, target), encoding="utf-8")
        injected.append(
            {
                "name": skill["name"],
                "description": skill["description"],
                "path": str(manifest),
            }
        )
    return injected


def skills_config_value(unselected):
    """TOML inline array hiding every skill the member did not select.

    Verified with `codex debug prompt-input`: a `skills.config` entry removes a skill
    from default discovery, and `enabled=true` only re-adds an explicit `path`, so
    hiding by name is the only mechanism that keeps the selection authoritative.
    The value is a separate ``-c`` argument (never a shell string) built from already
    validated names, so it cannot inject extra configuration.
    """
    if not unselected:
        return ""
    entries = ",".join('{name="' + name + '",enabled=false}' for name in sorted(set(unselected)))
    return "skills.config=[" + entries + "]"


def unselected_skills(software, selected, workspace=None):
    """Names the runtime would discover that the member did not select."""
    selected = set(selected)
    discovered = set()
    for root, _origin in skill_roots(software, workspace):
        discovered.update(skill["name"] for skill in _scan(root))
    return sorted(discovered - selected)

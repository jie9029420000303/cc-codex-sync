"""Vendored evidence-based project mapper — verbatim copy of sync-cloud-code-memory v3 (lines 1-452: dataclasses + mapping core only). DO NOT edit logic here; wrap it in identity.py."""


from __future__ import annotations

import argparse
import configparser
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


RULE_NAMES = {"CLAUDE.md", "claude.md", "cloud.md", "CLOUD.md"}
MEMORY_NAMES = {
    "memory.md",
    "memories.md",
    "project-memory.md",
    "project_memory.md",
    "memory.json",
    "memories.json",
    "project-memory.json",
    "project_memory.json",
}
MEMORY_DIRS = {"memory", "memories", "project-memory", "project_memories"}
IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    "dist",
    "build",
    "cache",
    "plugins",
    "telemetry",
    "logs",
    "log",
    "statsig",
    "shell-snapshots",
    "marketplace",
    "marketplaces",
    "commands",
    "ide",
    "todos",
}
PROJECT_MARKERS = {
    ".git",
    "AGENTS.md",
    "CLAUDE.md",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "composer.json",
    "pnpm-lock.yaml",
    "requirements.txt",
}


@dataclass(frozen=True)
class SourceItem:
    path: Path
    source_root: Path
    kind: str
    claude_project_dir: Path


@dataclass
class ProjectEvidence:
    claude_project_dir: Path
    cwd_counts: Counter = field(default_factory=Counter)
    cwd_sources: dict[str, str] = field(default_factory=dict)
    git_branches: Counter = field(default_factory=Counter)
    timestamps: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LocalProject:
    path: Path
    git_root: Path | None
    git_remotes: tuple[str, ...]


@dataclass(frozen=True)
class MatchResult:
    status: str
    path: Path | None
    method: str
    confidence: int
    reason: str
    evidence: dict


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def tokenize(value: str) -> set[str]:
    tokens = {token.lower() for token in re.split(r"[^A-Za-z0-9]+", value) if len(token) >= 3}
    # generic stopwords + current user's home-path components (privacy-scrubbed from vendored original)
    home_tokens = {part.lower() for part in Path.home().parts if len(part) >= 3}
    return {token for token in tokens if token not in {"users", "claude", "code"} | home_tokens}


def slugify(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return slug[:100] or fallback


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def versioned_path(directory: Path, stem: str, suffix: str, date: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for version in range(1, 10_000):
        candidate = directory / f"{stem}.{date}-V{version}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"No available version for {stem}")


def versioned_dir(directory: Path, stem: str, date: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for version in range(1, 10_000):
        candidate = directory / f"{stem}.{date}-V{version}"
        if not candidate.exists():
            candidate.mkdir(parents=True)
            return candidate
    raise RuntimeError(f"No available version for {stem}")


def default_source_roots() -> list[Path]:
    home = Path.home()
    return [
        path
        for path in [
            home / ".claude",
            home / ".cloud-code",
            home / ".config" / "claude",
            home / ".config" / "cloud-code",
            home / "Library" / "Application Support" / "Claude",
            home / "Library" / "Application Support" / "Cloud Code",
        ]
        if path.exists()
    ]


def default_local_roots() -> list[Path]:
    home = Path.home()
    # generic roots (privacy-scrubbed from vendored original; unused by memsync — readers supply their own paths)
    candidates = [
        home / "Documents" / "Codex",
        home / "Documents",
        home / "Projects",
        home / "dev",
    ]
    return [path for path in candidates if path.exists()]


def classify(path: Path) -> str | None:
    if path.name in RULE_NAMES:
        return "rule"
    lower_name = path.name.lower()
    lower_parts = {part.lower() for part in path.parts}
    if lower_name in MEMORY_NAMES:
        return "memory"
    if lower_parts.intersection(MEMORY_DIRS) and path.suffix.lower() in {".md", ".json", ".txt"}:
        return "memory"
    return None


def claude_project_dir_for(path: Path, source_root: Path, kind: str) -> Path:
    source_root = source_root.resolve()
    path = path.resolve()
    if source_root.name in {".claude", ".cloud-code"}:
        projects_root = source_root / "projects"
        try:
            rel = path.relative_to(projects_root)
            if rel.parts:
                return (projects_root / rel.parts[0]).resolve()
        except ValueError:
            pass
    if kind == "rule":
        return path.parent.resolve()
    rel_parts = path.relative_to(source_root).parts if path.is_relative_to(source_root) else path.parts
    for index, part in enumerate(rel_parts):
        if part.lower() in MEMORY_DIRS:
            prefix = rel_parts[:index]
            return source_root.joinpath(*prefix).resolve() if prefix else source_root.resolve()
    return path.parent.resolve()


def walk_source(root: Path, max_files: int) -> list[SourceItem]:
    root = root.resolve()
    items: list[SourceItem] = []
    seen: set[Path] = set()
    count = 0
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in IGNORE_DIRS]
        for filename in files:
            count += 1
            if count > max_files:
                return items
            path = (Path(current_root) / filename).resolve()
            if path in seen:
                continue
            kind = classify(path)
            if not kind:
                continue
            seen.add(path)
            items.append(SourceItem(path=path, source_root=root, kind=kind, claude_project_dir=claude_project_dir_for(path, root, kind)))
    return items


def collect_source_items(source_roots: list[Path], max_files: int) -> list[SourceItem]:
    items: list[SourceItem] = []
    for root in source_roots:
        if root.exists():
            items.extend(walk_source(root, max_files))
    return sorted(items, key=lambda item: (str(item.claude_project_dir), item.kind, str(item.path)))


def parse_claude_jsonl_evidence(source_roots: list[Path], max_jsonl_lines: int) -> dict[Path, ProjectEvidence]:
    evidence: dict[Path, ProjectEvidence] = {}
    for source_root in source_roots:
        projects_root = source_root / "projects"
        if not projects_root.exists():
            continue
        for project_dir in sorted(path for path in projects_root.iterdir() if path.is_dir()):
            project_evidence = ProjectEvidence(claude_project_dir=project_dir.resolve())
            for jsonl_path in sorted(project_dir.glob("*.jsonl")):
                with jsonl_path.open("r", encoding="utf-8", errors="replace") as handle:
                    for index, line in enumerate(handle):
                        if index >= max_jsonl_lines:
                            break
                        if '"cwd"' not in line and '"gitBranch"' not in line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        cwd = record.get("cwd")
                        if isinstance(cwd, str) and cwd:
                            project_evidence.cwd_counts[cwd] += 1
                            project_evidence.cwd_sources.setdefault(cwd, str(jsonl_path.resolve()))
                        branch = record.get("gitBranch")
                        if isinstance(branch, str) and branch:
                            project_evidence.git_branches[branch] += 1
                        timestamp = record.get("timestamp")
                        if isinstance(timestamp, str):
                            project_evidence.timestamps.append(timestamp)
            if project_evidence.cwd_counts:
                evidence[project_dir.resolve()] = project_evidence
    return evidence


def nearest_existing_path(path: Path) -> Path | None:
    current = path.expanduser()
    for candidate in [current, *current.parents]:
        if candidate.exists():
            return candidate.resolve()
    return None


def nearest_marker_root(path: Path, local_roots: list[Path]) -> Path:
    current = path.resolve()
    local_roots = [root.resolve() for root in local_roots if root.exists()]
    for candidate in [current, *current.parents]:
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
        if candidate in local_roots:
            return candidate
    return current


def read_git_remotes(path: Path) -> tuple[Path | None, tuple[str, ...]]:
    current = path.resolve()
    for candidate in [current, *current.parents]:
        config_path = candidate / ".git" / "config"
        if config_path.exists():
            parser = configparser.ConfigParser()
            parser.read(config_path, encoding="utf-8")
            remotes = []
            for section in parser.sections():
                if section.startswith('remote "'):
                    url = parser.get(section, "url", fallback="")
                    if url:
                        remotes.append(normalize_git_url(url))
            return candidate, tuple(sorted(set(remotes)))
    return None, ()


def normalize_git_url(url: str) -> str:
    value = url.strip()
    value = value.removesuffix(".git")
    value = value.replace("git@github.com:", "https://github.com/")
    value = value.replace("ssh://git@github.com/", "https://github.com/")
    return value.lower()


def collect_local_projects(local_roots: list[Path], max_depth: int) -> list[LocalProject]:
    projects: dict[Path, LocalProject] = {}
    for root in local_roots:
        if not root.exists():
            continue
        root = root.resolve()
        git_root, remotes = read_git_remotes(root)
        projects[root] = LocalProject(path=root, git_root=git_root, git_remotes=remotes)
        for current_root, dirs, files in os.walk(root):
            current = Path(current_root)
            try:
                depth = len(current.relative_to(root).parts)
            except ValueError:
                continue
            dirs[:] = [name for name in dirs if name not in IGNORE_DIRS]
            if depth >= max_depth:
                dirs[:] = []
            if set(files).union(dirs).intersection(PROJECT_MARKERS):
                project_path = current.resolve()
                git_root, remotes = read_git_remotes(project_path)
                projects[project_path] = LocalProject(path=project_path, git_root=git_root, git_remotes=remotes)
    return sorted(projects.values(), key=lambda item: str(item.path))


def direct_cwd_match(project_evidence: ProjectEvidence, local_roots: list[Path]) -> MatchResult | None:
    for cwd, count in project_evidence.cwd_counts.most_common():
        cwd_path = Path(cwd).expanduser()
        existing = nearest_existing_path(cwd_path)
        if not existing:
            continue
        mapped = nearest_marker_root(existing, local_roots)
        return MatchResult(
            status="matched",
            path=mapped,
            method="claude_jsonl_cwd",
            confidence=100,
            reason="Claude JSONL cwd exists on disk and was used as direct local-folder evidence.",
            evidence={
                "cwd": cwd,
                "cwd_count": count,
                "cwd_source_jsonl": project_evidence.cwd_sources.get(cwd),
                "nearest_existing_path": str(existing),
                "git_branches": dict(project_evidence.git_branches.most_common(5)),
            },
        )
    return None


def git_remote_match(project_evidence: ProjectEvidence, local_projects: list[LocalProject]) -> MatchResult | None:
    source_remotes: set[str] = set()
    source_cwds: list[str] = []
    for cwd, _count in project_evidence.cwd_counts.most_common():
        existing = nearest_existing_path(Path(cwd))
        if not existing:
            continue
        _git_root, remotes = read_git_remotes(existing)
        if remotes:
            source_cwds.append(cwd)
            source_remotes.update(remotes)
    if not source_remotes:
        return None
    matches = [project for project in local_projects if source_remotes.intersection(project.git_remotes)]
    if len(matches) == 1:
        return MatchResult(
            status="matched",
            path=matches[0].path,
            method="git_remote",
            confidence=95,
            reason="Claude cwd git remote matches exactly one discovered local project git remote.",
            evidence={"source_cwds": source_cwds, "git_remotes": sorted(source_remotes), "matched_git_remotes": list(matches[0].git_remotes)},
        )
    if len(matches) > 1:
        return MatchResult(
            status="ambiguous",
            path=None,
            method="git_remote",
            confidence=80,
            reason="Claude cwd git remote matches multiple local projects.",
            evidence={"source_cwds": source_cwds, "git_remotes": sorted(source_remotes), "candidate_paths": [str(item.path) for item in matches[:20]]},
        )
    return None


def token_fallback_match(claude_project_dir: Path, local_projects: list[LocalProject]) -> MatchResult:
    source_tokens = tokenize(claude_project_dir.name)
    scored = []
    for project in local_projects:
        local_tokens = tokenize(str(project.path))
        overlap = source_tokens.intersection(local_tokens)
        if overlap:
            scored.append((len(overlap), len(str(project.path)), project, sorted(overlap)))
    if not scored:
        return MatchResult(
            status="unmatched",
            path=None,
            method="none",
            confidence=0,
            reason="No cwd, git remote, or token evidence matched a local project.",
            evidence={"source_tokens": sorted(source_tokens)},
        )
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score = scored[0][0]
    best = [item for item in scored if item[0] == best_score]
    if len(best) == 1 and best_score >= 2:
        _score, _length, project, overlap = best[0]
        return MatchResult(
            status="matched",
            path=project.path,
            method="encoded_folder_tokens",
            confidence=60,
            reason="Encoded Claude project folder tokens overlap with one local project path.",
            evidence={"source_tokens": sorted(source_tokens), "overlap_tokens": overlap},
        )
    return MatchResult(
        status="ambiguous",
        path=None,
        method="encoded_folder_tokens",
        confidence=40,
        reason="Encoded Claude project folder tokens matched multiple local projects or too few tokens.",
        evidence={
            "source_tokens": sorted(source_tokens),
            "candidate_paths": [str(item[2].path) for item in best[:20]],
            "overlap_tokens": [item[3] for item in best[:20]],
        },
    )


def match_project(claude_project_dir: Path, evidence_map: dict[Path, ProjectEvidence], local_roots: list[Path], local_projects: list[LocalProject]) -> MatchResult:
    project_evidence = evidence_map.get(claude_project_dir.resolve())
    if project_evidence:
        direct = direct_cwd_match(project_evidence, local_roots)
        if direct:
            return direct
        remote = git_remote_match(project_evidence, local_projects)
        if remote:
            return remote
    return token_fallback_match(claude_project_dir, local_projects)


def read_text(path: Path, max_bytes: int) -> tuple[str, bool]:
    data = path.read_bytes()
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace"), truncated

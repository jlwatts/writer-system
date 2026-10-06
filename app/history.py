"""Vault snapshots in a git dir that is not inside the vault."""

import logging
import os
import re
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

logger = logging.getLogger("writer_app")

HISTORY_DIR = Path(os.getenv("HISTORY_DIR", "./history"))
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_REV_RE = re.compile(r"^[0-9a-f]{40}$")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

EXCLUDE_TEXT = """\
.obsidian/workspace.json
.obsidian/workspace-mobile.json
.obsidian/cache/
.stfolder
.stversions
.stignore
.git
"""


class HistoryError(RuntimeError):
    pass


def snapshot_interval_seconds() -> int:
    return int(os.getenv("SNAPSHOT_INTERVAL_SECONDS", "900"))


def branch_ttl_days() -> int:
    return int(os.getenv("BRANCH_TTL_DAYS", "30"))


@contextmanager
def vault_lock(vault: str):
    from app.main import sanitize_vault_name

    name = sanitize_vault_name(vault)
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(name, threading.Lock())
    with lock:
        yield


def _history_root() -> Path:
    root = Path(HISTORY_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def repo_dir(vault: str) -> Path:
    from app.main import sanitize_vault_name

    return _history_root() / f"{sanitize_vault_name(vault)}.git"


def _exclude_path() -> Path:
    path = _history_root() / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_text(encoding="utf-8") != EXCLUDE_TEXT:
        path.write_text(EXCLUDE_TEXT, encoding="utf-8")
    return path


def ensure_repo(vault: str) -> Path:
    gdir = repo_dir(vault)
    if not (gdir / "HEAD").is_file():
        gdir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "-c",
                "init.defaultBranch=main",
                "-c",
                "advice.defaultBranchName=false",
                "init",
                "--bare",
                str(gdir),
            ],
            check=True,
            capture_output=True,
            cwd=str(gdir.parent),
        )
        subprocess.run(
            ["git", "--git-dir", str(gdir), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True,
            capture_output=True,
            cwd=str(gdir.parent),
        )
    _exclude_path()
    return gdir


def _root(vault: str) -> Path:
    from app.main import vault_root

    return vault_root(vault)


def _run(
    vault: str,
    *args: str,
    index: Path | None = None,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    root = _root(vault)
    gdir = ensure_repo(vault)
    cmd = [
        "git",
        "-c",
        "user.name=writer-system",
        "-c",
        "user.email=writer@localhost",
        "-c",
        "advice.defaultBranchName=false",
        "-c",
        "core.bare=false",
        "-c",
        f"core.excludesFile={_exclude_path()}",
        "--git-dir",
        str(gdir),
        "--work-tree",
        str(root),
        *args,
    ]
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)
    env["GIT_AUTHOR_NAME"] = "writer-system"
    env["GIT_AUTHOR_EMAIL"] = "writer@localhost"
    env["GIT_COMMITTER_NAME"] = "writer-system"
    env["GIT_COMMITTER_EMAIL"] = "writer@localhost"
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    cwd = root if root.is_dir() else gdir.parent
    proc = subprocess.run(
        cmd,
        input=input_bytes,
        capture_output=True,
        env=env,
        cwd=str(cwd),
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace") or proc.stdout.decode(errors="replace")
        raise HistoryError(detail.strip() or f"git {' '.join(args)} failed")
    return proc


def _stdout(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode(errors="replace").strip()


def head_commit(vault: str) -> str | None:
    if not (repo_dir(vault) / "HEAD").is_file():
        return None
    proc = _run(vault, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
    if proc.returncode != 0:
        return None
    return _stdout(proc) or None


def _ref_sha(vault: str, ref: str) -> str | None:
    proc = _run(vault, "rev-parse", "--verify", "--quiet", ref, check=False)
    if proc.returncode != 0:
        return None
    return _stdout(proc) or None


def snapshot_vault(vault: str) -> str | None:
    with vault_lock(vault):
        root = _root(vault)
        if not root.is_dir():
            return None
        ensure_repo(vault)
        _run(vault, "add", "-A")
        if head_commit(vault):
            dirty = _run(vault, "diff", "--cached", "--quiet", check=False).returncode != 0
        else:
            status = _run(vault, "status", "--porcelain")
            dirty = bool(status.stdout.strip())
        if not dirty:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _run(vault, "commit", "-m", f"snapshot {stamp}")
        _run(vault, "gc", "--auto", check=False)
        return head_commit(vault)


def snapshot_all() -> None:
    from app.main import list_vault_names

    for name in list_vault_names():
        try:
            snapshot_vault(name)
        except Exception:
            logger.exception("Snapshot failed for %s", name)
        try:
            purge_old_branches(name)
        except Exception:
            logger.exception("Branch purge failed for %s", name)


def _check_branch(branch: str) -> str:
    if not _BRANCH_RE.fullmatch(branch) or ".." in branch or branch in {".", ".."}:
        raise ValueError("Invalid branch name")
    return branch


def _check_rel(path: str) -> str:
    if not path or "\\" in path or path.startswith("/"):
        raise ValueError("Invalid path")
    parts = Path(path).parts
    if ".." in parts or parts[0] in {"", ".", ".."}:
        raise ValueError("Invalid path")
    return Path(path).as_posix()


def commit_branch(vault: str, branch: str, files: dict[str, str | None]) -> str:
    branch = _check_branch(branch)
    with vault_lock(vault):
        ensure_repo(vault)
        parent = _ref_sha(vault, "refs/heads/main")
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index"
            if parent:
                _run(vault, "read-tree", parent, index=index)
            else:
                _run(vault, "read-tree", "--empty", index=index)
            for raw_path, text in files.items():
                path = _check_rel(raw_path)
                if text is None:
                    _run(vault, "rm", "--cached", "--ignore-unmatch", "--", path, index=index, check=False)
                else:
                    blob = _stdout(_run(vault, "hash-object", "-w", "--stdin", input_bytes=text.encode("utf-8"), index=index))
                    _run(vault, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", index=index)
            tree = _stdout(_run(vault, "write-tree", index=index))
        args = ["commit-tree", tree, "-m", f"device {branch}"]
        if parent:
            args.extend(["-p", parent])
        commit = _stdout(_run(vault, *args))
        _run(vault, "update-ref", f"refs/heads/{branch}", commit)
        return commit


def _show_text(vault: str, spec: str) -> str | None:
    proc = _run(vault, "show", spec, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8")


def _conflict_paths(stdout: str) -> list[str]:
    lines = stdout.splitlines()
    paths: list[str] = []
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith(("CONFLICT", "Auto-merging", "Version ")):
            continue
        paths.append(stripped)
    if paths:
        return paths
    found: list[str] = []
    for line in lines[1:]:
        match = re.search(r"CONFLICT \([^)]+\): (\S+)", line)
        if match:
            found.append(match.group(1))
    return found


def _conflict_file(vault: str, branch: str, path: str) -> dict:
    base = None
    base_rev = _run(vault, "merge-base", "main", branch, check=False)
    if base_rev.returncode == 0 and _stdout(base_rev):
        base = _show_text(vault, f"{_stdout(base_rev)}:{path}")
    return {
        "path": path,
        "base": base,
        "ours": _show_text(vault, f"main:{path}"),
        "theirs": _show_text(vault, f"{branch}:{path}"),
    }


def _apply_tree(vault: str, tree: str) -> None:
    root = _root(vault)
    diff = _run(vault, "diff-tree", "--name-status", "-r", "--no-renames", "-z", "main", tree)
    raw = diff.stdout.decode("utf-8", errors="surrogateescape")
    parts = raw.split("\0")
    index = 0
    while index + 1 < len(parts):
        status = parts[index]
        path = parts[index + 1]
        index += 2
        if not path:
            continue
        dest = root / path
        if status.startswith("D"):
            if dest.is_file():
                dest.unlink()
            continue
        blob = _run(vault, "show", f"{tree}:{path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob.stdout)


def merge_branch(vault: str, branch: str) -> dict:
    branch = _check_branch(branch)
    with vault_lock(vault):
        if not (repo_dir(vault) / "HEAD").is_file():
            raise ValueError("Branch not found")
        if _ref_sha(vault, "refs/heads/main") is None:
            raise ValueError("main has no commits")
        if _ref_sha(vault, f"refs/heads/{branch}") is None:
            raise ValueError("Branch not found")
        proc = _run(vault, "merge-tree", "--write-tree", "--name-only", "main", branch, check=False)
        if proc.returncode not in (0, 1):
            detail = proc.stderr.decode(errors="replace") or proc.stdout.decode(errors="replace")
            raise HistoryError(detail.strip() or "merge-tree failed")
        stdout = proc.stdout.decode(errors="replace")
        lines = [line for line in stdout.splitlines() if line.strip()]
        if not lines or not _REV_RE.fullmatch(lines[0].strip()):
            raise HistoryError(proc.stderr.decode(errors="replace").strip() or "merge-tree returned no tree")
        tree = lines[0].strip()
        conflicts = _conflict_paths(stdout)
        if proc.returncode != 0 or conflicts:
            return {
                "status": "conflict",
                "files": [_conflict_file(vault, branch, path) for path in conflicts],
            }
        main_tree = _stdout(_run(vault, "rev-parse", "main^{tree}"))
        if tree == main_tree:
            return {"status": "unchanged"}
        _apply_tree(vault, tree)
        commit = _stdout(_run(vault, "commit-tree", tree, "-p", "main", "-p", branch, "-m", f"merge {branch}"))
        _run(vault, "update-ref", "refs/heads/main", commit)
        _run(vault, "read-tree", commit)
        return {"status": "merged", "rev": commit}


def purge_old_branches(vault: str, now: datetime | None = None) -> list[str]:
    if not (repo_dir(vault) / "HEAD").is_file():
        return []
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    cutoff = timedelta(days=branch_ttl_days())
    removed: list[str] = []
    with vault_lock(vault):
        listing = _run(vault, "for-each-ref", "--format=%(refname:short)", "refs/heads", check=False)
        if listing.returncode != 0:
            return []
        for name in listing.stdout.decode().split():
            if name == "main":
                continue
            stamped = _run(vault, "log", "-1", "--format=%ct", name, check=False)
            if stamped.returncode != 0 or not stamped.stdout.strip():
                continue
            committed = datetime.fromtimestamp(int(stamped.stdout.decode().strip()), tz=timezone.utc)
            if moment - committed > cutoff:
                _run(vault, "update-ref", "-d", f"refs/heads/{name}")
                removed.append(name)
        if removed:
            _run(vault, "gc", "--auto", check=False)
    return removed


def _relative_file(vault: str, path: str) -> str:
    from app.main import resolve_in_vault, vault_root

    if not (path or "").strip():
        raise HTTPException(status_code=400, detail="Missing file path")
    root = vault_root(vault)
    target = resolve_in_vault(vault, path)
    relative = target.relative_to(root).as_posix()
    if not relative or relative == ".":
        raise HTTPException(status_code=400, detail="Missing file path")
    return relative


def _labels_by_commit(vault: str) -> dict[str, str]:
    if not (repo_dir(vault) / "HEAD").is_file():
        return {}
    proc = _run(
        vault,
        "for-each-ref",
        "--format=%(*objectname)\x1f%(subject)",
        "refs/tags/label",
        check=False,
    )
    if proc.returncode != 0:
        return {}
    labels: dict[str, str] = {}
    for line in proc.stdout.decode(errors="replace").splitlines():
        if "\x1f" not in line:
            continue
        rev, name = line.split("\x1f", 1)
        if _REV_RE.fullmatch(rev) and name.strip():
            labels[rev] = name.strip()
    return labels


def file_history(vault: str, path: str) -> list[dict]:
    relative = _relative_file(vault, path)
    if head_commit(vault) is None:
        return []
    proc = _run(
        vault,
        "log",
        "--follow",
        "--name-only",
        "--format=%H%x1f%cI%x1f%s",
        "--",
        relative,
        check=False,
    )
    if proc.returncode != 0:
        return []
    labels = _labels_by_commit(vault)
    rows = []
    current = None
    named = False
    for line in proc.stdout.decode(errors="replace").splitlines():
        if "\x1f" in line and _REV_RE.fullmatch(line.split("\x1f", 1)[0]):
            rev, committed_at, message = line.split("\x1f", 2)
            current = {
                "rev": rev,
                "committed_at": committed_at,
                "message": message,
                "label": labels.get(rev),
                "path_at_rev": relative,
            }
            rows.append(current)
            named = False
            continue
        if current is not None and line.strip() and not named:
            current["path_at_rev"] = line.strip()
            named = True
    return rows


def _require_commit(vault: str, rev: str) -> None:
    if not _REV_RE.fullmatch(rev or ""):
        raise HTTPException(status_code=400, detail="Invalid revision")
    if not (repo_dir(vault) / "HEAD").is_file():
        raise HTTPException(status_code=404, detail="Revision not found")
    kind = _run(vault, "cat-file", "-t", rev, check=False)
    if kind.returncode != 0 or _stdout(kind) != "commit":
        raise HTTPException(status_code=404, detail="Revision not found")


def _blob_at(vault: str, path: str, rev: str) -> str:
    relative = _relative_file(vault, path)
    _require_commit(vault, rev)
    blob = _run(vault, "show", f"{rev}:{relative}", check=False)
    if blob.returncode != 0:
        historical = next((row["path_at_rev"] for row in file_history(vault, path) if row["rev"] == rev), "")
        if historical and historical != relative:
            blob = _run(vault, "show", f"{rev}:{historical}", check=False)
    if blob.returncode != 0:
        raise HTTPException(status_code=404, detail="File not in revision")
    return blob.stdout.decode("utf-8")


def read_file_at(vault: str, path: str, rev: str) -> str:
    return _blob_at(vault, path, rev)


def set_file_label(vault: str, path: str, rev: str, name: str) -> str | None:
    _require_commit(vault, rev)
    if not any(row["rev"] == rev for row in file_history(vault, path)):
        raise HTTPException(status_code=404, detail="Revision not found")
    cleaned = " ".join((name or "").split())
    if any(ord(ch) < 32 for ch in cleaned):
        raise HTTPException(status_code=400, detail="Invalid label")
    if len(cleaned) > 80:
        raise HTTPException(status_code=400, detail="Label is too long")
    tag = f"label/{rev}"
    with vault_lock(vault):
        _run(vault, "tag", "-d", tag, check=False)
        if not cleaned:
            return None
        _run(vault, "tag", "-a", tag, rev, "-m", cleaned)
    return cleaned


def restore_file(vault: str, path: str, rev: str) -> str:
    text = _blob_at(vault, path, rev)
    from app.main import resolve_in_vault

    target = resolve_in_vault(vault, path)
    with vault_lock(vault):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return text

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.history as history
import app.main as main
from app.main import app

client = TestClient(app)


@pytest.fixture
def vaults(tmp_path, monkeypatch):
    root = tmp_path / "vaults"
    root.mkdir()
    monkeypatch.setattr(main, "VAULTS_DIR", root)
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.delenv("BRANCH_TTL_DAYS", raising=False)
    monkeypatch.delenv("SNAPSHOT_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_DATE", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_DATE", raising=False)
    return root


def _write(vaults: Path, vault: str, path: str, text: str) -> Path:
    dest = vaults / vault / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest


def _git_date(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")


def test_interval_default_is_fifteen_minutes(vaults, monkeypatch):
    monkeypatch.delenv("SNAPSHOT_INTERVAL_SECONDS", raising=False)
    assert history.snapshot_interval_seconds() == 900


def test_snapshot_commits_only_when_dirty(vaults):
    note = _write(vaults, "books", "chapter.md", "Once.")
    first = history.snapshot_vault("books")
    assert first
    message = history.file_history("books", "chapter.md")[0]["message"]
    assert re.fullmatch(r"snapshot \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", message)
    assert not (vaults / "books" / ".git").exists()
    note.write_text("Once.", encoding="utf-8")
    assert history.snapshot_vault("books") is None
    assert history.head_commit("books") == first


def test_snapshot_records_delete_rename_and_add(vaults):
    _write(vaults, "books", "old.md", "original")
    history.snapshot_vault("books")
    added_before = history.head_commit("books")

    _write(vaults, "books", "extra.md", "new note")
    history.snapshot_vault("books")
    extra_rev = history.head_commit("books")
    with pytest.raises(HTTPException) as missing:
        history.read_file_at("books", "extra.md", added_before)
    assert missing.value.status_code == 404
    assert history.read_file_at("books", "extra.md", extra_rev) == "new note"
    assert [row["rev"] for row in history.file_history("books", "extra.md")] == [extra_rev]

    (vaults / "books" / "old.md").rename(vaults / "books" / "renamed.md")
    history.snapshot_vault("books")
    followed = history._run(
        "books", "log", "--follow", "--name-only", "--pretty=format:%H", "--", "renamed.md"
    )
    followed_text = followed.stdout.decode()
    assert "old.md" in followed_text
    assert "renamed.md" in followed_text
    assert history.read_file_at("books", "renamed.md", history.head_commit("books")) == "original"

    (vaults / "books" / "extra.md").unlink()
    history.snapshot_vault("books")
    deleted = history.file_history("books", "extra.md")
    with pytest.raises(HTTPException) as gone:
        history.read_file_at("books", "extra.md", deleted[0]["rev"])
    assert gone.value.status_code == 404
    assert history.read_file_at("books", "extra.md", extra_rev) == "new note"


def test_two_vaults_have_separate_repos(vaults):
    _write(vaults, "books", "a.md", "alpha")
    _write(vaults, "notes", "b.md", "beta")
    history.snapshot_vault("books")
    history.snapshot_vault("notes")
    assert history.repo_dir("books") != history.repo_dir("notes")
    assert history.repo_dir("books").is_dir()
    listed = history._run("books", "ls-tree", "-r", "--name-only", "HEAD")
    names = listed.stdout.decode().split()
    assert names == ["a.md"]
    assert history.read_file_at("notes", "b.md", history.head_commit("notes")) == "beta"


def test_empty_vault_and_excluded_noise_do_not_commit(vaults):
    (vaults / "books").mkdir()
    assert history.snapshot_vault("books") is None
    assert history.head_commit("books") is None

    for relative in (
        ".obsidian/workspace.json",
        ".obsidian/workspace-mobile.json",
        ".obsidian/cache/index",
        ".stfolder/syncthing-folder",
        ".stversions/chapter.md",
        ".stignore",
        "nested/.git/config",
    ):
        _write(vaults, "books", relative, "noise")
    assert history.snapshot_vault("books") is None
    assert history.head_commit("books") is None

    _write(vaults, "books", ".obsidian/app.json", "{}\n")
    assert history.snapshot_vault("books")
    listed = history._run("books", "ls-tree", "-r", "--name-only", "HEAD").stdout.decode()
    assert ".obsidian/app.json" in listed
    assert "workspace.json" not in listed


def test_snapshot_does_not_write_global_git_identity(vaults):
    before = subprocess.run(["git", "config", "--global", "--get", "user.name"], capture_output=True)
    _write(vaults, "books", "a.md", "one")
    history.snapshot_vault("books")
    after = subprocess.run(["git", "config", "--global", "--get", "user.name"], capture_output=True)
    assert before.stdout == after.stdout


def test_history_api_lists_newest_first_and_reads_content(vaults):
    _write(vaults, "books", "chapter.md", "one")
    history.snapshot_vault("books")
    _write(vaults, "books", "chapter.md", "two")
    history.snapshot_vault("books")
    listed = client.get("/api/history", params={"vault": "books", "path": "chapter.md"})
    assert listed.status_code == 200
    commits = listed.json()["commits"]
    assert [row["message"].startswith("snapshot ") for row in commits] == [True, True]
    assert commits[0]["committed_at"] >= commits[1]["committed_at"]
    for row in commits:
        assert set(row) == {"rev", "committed_at", "message", "label"}
        assert row["label"] is None
    body = client.get("/api/history/content", params={
        "vault": "books",
        "path": "chapter.md",
        "rev": commits[0]["rev"],
    })
    assert body.status_code == 200
    assert body.json()["content"] == "two"
    older = client.get("/api/history/content", params={
        "vault": "books",
        "path": "chapter.md",
        "rev": commits[1]["rev"],
    })
    assert older.json()["content"] == "one"


def test_history_api_rejects_bad_paths_and_revs(vaults):
    _write(vaults, "books", "chapter.md", "one")
    history.snapshot_vault("books")
    real = history.head_commit("books")

    bare = TestClient(app)
    assert bare.get("/api/history", params={"vault": "books", "path": "chapter.md"}).status_code == 401

    assert client.get("/api/history", params={"vault": "books"}).status_code == 400
    assert client.get("/api/history", params={"vault": "books", "path": "../secret"}).status_code == 400
    assert client.get("/api/history", params={"vault": "a/b", "path": "chapter.md"}).status_code == 400
    missing = client.get("/api/history/content", params={"vault": "books", "path": "missing.md", "rev": real})
    assert missing.status_code == 404
    for rev in ("main", "HEAD", "abc;touch", "abc"):
        denied = client.get("/api/history/content", params={"vault": "books", "path": "chapter.md", "rev": rev})
        assert denied.status_code == 400
    unknown = client.get("/api/history/content", params={
        "vault": "books",
        "path": "chapter.md",
        "rev": "a" * 40,
    })
    assert unknown.status_code == 404


def test_merge_device_edit_main_left_alone(vaults):
    _write(vaults, "books", "a.md", "base-a")
    _write(vaults, "books", "b.md", "base-b")
    history.snapshot_vault("books")
    history.commit_branch("books", "phone", {"b.md": "from phone"})
    result = history.merge_branch("books", "phone")
    assert result["status"] == "merged"
    assert (vaults / "books" / "b.md").read_text() == "from phone"
    assert (vaults / "books" / "a.md").read_text() == "base-a"


def test_merge_different_files(vaults):
    _write(vaults, "books", "a.md", "base-a")
    _write(vaults, "books", "b.md", "base-b")
    history.snapshot_vault("books")
    history.commit_branch("books", "phone", {"a.md": "device-a"})
    _write(vaults, "books", "b.md", "main-b")
    history.snapshot_vault("books")
    result = history.merge_branch("books", "phone")
    assert result["status"] == "merged"
    assert (vaults / "books" / "a.md").read_text() == "device-a"
    assert (vaults / "books" / "b.md").read_text() == "main-b"


def test_merge_same_text_keeps_the_file(vaults):
    _write(vaults, "books", "a.md", "base")
    history.snapshot_vault("books")
    history.commit_branch("books", "phone", {"a.md": "same"})
    _write(vaults, "books", "a.md", "same")
    history.snapshot_vault("books")
    result = history.merge_branch("books", "phone")
    assert result["status"] in {"merged", "unchanged"}
    assert (vaults / "books" / "a.md").read_text() == "same"


def test_merge_conflict_returns_both_versions(vaults):
    _write(vaults, "books", "a.md", "base")
    history.snapshot_vault("books")
    history.commit_branch("books", "phone", {"a.md": "theirs"})
    _write(vaults, "books", "a.md", "ours")
    history.snapshot_vault("books")
    result = history.merge_branch("books", "phone")
    assert result["status"] == "conflict"
    assert (vaults / "books" / "a.md").read_text() == "ours"
    assert result["files"] == [{
        "path": "a.md",
        "base": "base",
        "ours": "ours",
        "theirs": "theirs",
    }]
    assert history.head_commit("books") == history.file_history("books", "a.md")[0]["rev"]


def test_merge_delete_versus_edit_is_a_conflict(vaults):
    _write(vaults, "books", "a.md", "base")
    history.snapshot_vault("books")
    history.commit_branch("books", "phone", {"a.md": None})
    _write(vaults, "books", "a.md", "ours")
    history.snapshot_vault("books")
    before = history.head_commit("books")
    result = history.merge_branch("books", "phone")
    assert result["status"] == "conflict"
    assert (vaults / "books" / "a.md").read_text() == "ours"
    assert result["files"][0]["theirs"] is None
    assert result["files"][0]["ours"] == "ours"
    assert result["files"][0]["base"] == "base"
    assert history.head_commit("books") == before


def test_merge_both_deletes_stays_clean(vaults):
    note = _write(vaults, "books", "a.md", "base")
    history.snapshot_vault("books")
    history.commit_branch("books", "phone", {"a.md": None})
    note.unlink()
    history.snapshot_vault("books")
    result = history.merge_branch("books", "phone")
    assert result["status"] in {"merged", "unchanged"}
    assert not note.exists()


def test_merge_device_adds_a_file(vaults):
    _write(vaults, "books", "a.md", "base")
    history.snapshot_vault("books")
    history.commit_branch("books", "phone", {"new.md": "hello"})
    result = history.merge_branch("books", "phone")
    assert result["status"] == "merged"
    assert (vaults / "books" / "new.md").read_text() == "hello"


def test_merge_both_add_different_text_conflicts(vaults):
    _write(vaults, "books", "other.md", "keep")
    history.snapshot_vault("books")
    history.commit_branch("books", "phone", {"new.md": "device"})
    _write(vaults, "books", "new.md", "main copy")
    history.snapshot_vault("books")
    before = (vaults / "books" / "new.md").read_text()
    result = history.merge_branch("books", "phone")
    assert result["status"] == "conflict"
    assert (vaults / "books" / "new.md").read_text() == before
    sides = result["files"][0]
    assert sides["base"] is None
    assert sides["ours"] == "main copy"
    assert sides["theirs"] == "device"


def test_merge_identical_branch_writes_nothing(vaults):
    _write(vaults, "books", "a.md", "text")
    history.snapshot_vault("books")
    before = history.head_commit("books")
    history.commit_branch("books", "phone", {"a.md": "text"})
    result = history.merge_branch("books", "phone")
    assert result == {"status": "unchanged"}
    assert history.head_commit("books") == before
    assert (vaults / "books" / "a.md").read_text() == "text"


def test_commit_branch_does_not_touch_the_vault(vaults):
    note = _write(vaults, "books", "a.md", "hello")
    history.snapshot_vault("books")
    history.commit_branch("books", "phone", {"ghost.md": "nope", "a.md": "changed"})
    assert not (vaults / "books" / "ghost.md").exists()
    assert note.read_text() == "hello"


def test_bad_branch_names_are_rejected(vaults):
    _write(vaults, "books", "a.md", "hello")
    history.snapshot_vault("books")
    for name in ("dev/one", "dev one", "dev..one", "..", ""):
        with pytest.raises(ValueError):
            history.commit_branch("books", name, {"a.md": "x"})


def test_missing_branch_does_not_change_the_vault(vaults):
    note = _write(vaults, "books", "a.md", "hello")
    history.snapshot_vault("books")
    before = history.head_commit("books")
    with pytest.raises(ValueError):
        history.merge_branch("books", "missing")
    assert note.read_text() == "hello"
    assert history.head_commit("books") == before


def test_branch_ttl_defaults_to_thirty_days(vaults):
    assert history.branch_ttl_days() == 30


def test_purge_removes_only_stale_branches(vaults, monkeypatch):
    _write(vaults, "books", "a.md", "base")
    old_main = datetime.now(timezone.utc) - timedelta(days=40)
    monkeypatch.setenv("GIT_COMMITTER_DATE", _git_date(old_main))
    monkeypatch.setenv("GIT_AUTHOR_DATE", _git_date(old_main))
    history.snapshot_vault("books")
    monkeypatch.delenv("GIT_COMMITTER_DATE", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_DATE", raising=False)

    stale = datetime.now(timezone.utc) - timedelta(days=31)
    monkeypatch.setenv("GIT_COMMITTER_DATE", _git_date(stale))
    monkeypatch.setenv("GIT_AUTHOR_DATE", _git_date(stale))
    stale_sha = history.commit_branch("books", "old-phone", {"a.md": "old"})
    fresh = datetime.now(timezone.utc) - timedelta(days=10)
    monkeypatch.setenv("GIT_COMMITTER_DATE", _git_date(fresh))
    monkeypatch.setenv("GIT_AUTHOR_DATE", _git_date(fresh))
    history.commit_branch("books", "new-phone", {"a.md": "new"})
    monkeypatch.delenv("GIT_COMMITTER_DATE", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_DATE", raising=False)

    subprocess.run(
        ["git", "--git-dir", str(history.repo_dir("books")), "tag", "milestone", stale_sha],
        check=True,
    )
    removed = history.purge_old_branches("books")
    assert removed == ["old-phone"]
    assert history._ref_sha("books", "refs/heads/new-phone")
    assert history._ref_sha("books", "refs/heads/main")
    assert history.head_commit("books")
    tagged = subprocess.run(
        ["git", "--git-dir", str(history.repo_dir("books")), "rev-parse", "milestone"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert tagged.stdout.strip() == stale_sha
    assert history._ref_sha("books", "refs/heads/old-phone") is None


def test_purge_keeps_main_history_after_the_branch_is_gone(vaults, monkeypatch):
    _write(vaults, "books", "a.md", "v1")
    history.snapshot_vault("books")
    stale = datetime.now(timezone.utc) - timedelta(days=31)
    monkeypatch.setenv("GIT_COMMITTER_DATE", _git_date(stale))
    monkeypatch.setenv("GIT_AUTHOR_DATE", _git_date(stale))
    history.commit_branch("books", "phone", {"a.md": "v2"})
    monkeypatch.delenv("GIT_COMMITTER_DATE", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_DATE", raising=False)
    merged = history.merge_branch("books", "phone")
    assert merged["status"] == "merged"
    assert history.purge_old_branches("books") == ["phone"]
    body = client.get("/api/history/content", params={
        "vault": "books",
        "path": "a.md",
        "rev": merged["rev"],
    })
    assert body.status_code == 200
    assert body.json()["content"] == "v2"
    listed = client.get("/api/history", params={"vault": "books", "path": "a.md"})
    assert listed.status_code == 200
    assert any(row["message"].startswith("snapshot ") for row in listed.json()["commits"])


def test_ttl_can_be_set_to_zero(vaults, monkeypatch):
    _write(vaults, "books", "a.md", "base")
    history.snapshot_vault("books")
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    monkeypatch.setenv("GIT_COMMITTER_DATE", _git_date(yesterday))
    monkeypatch.setenv("GIT_AUTHOR_DATE", _git_date(yesterday))
    monkeypatch.setenv("BRANCH_TTL_DAYS", "0")
    history.commit_branch("books", "phone", {"a.md": "side"})
    assert history.purge_old_branches("books") == ["phone"]


def test_label_and_restore_one_version(vaults):
    note = _write(vaults, "books", "chapter.md", "one")
    history.snapshot_vault("books")
    first = history.head_commit("books")
    note.write_text("two", encoding="utf-8")
    history.snapshot_vault("books")
    second = history.head_commit("books")
    _write(vaults, "books", "other.md", "leave me")

    labeled = client.post("/api/history/label", json={
        "vault": "books",
        "path": "chapter.md",
        "rev": first,
        "name": "  Before the rewrite  ",
    })
    assert labeled.status_code == 200
    assert labeled.json()["label"] == "Before the rewrite"

    listed = client.get("/api/history", params={"vault": "books", "path": "chapter.md"})
    rows = {row["rev"]: row for row in listed.json()["commits"]}
    assert rows[first]["label"] == "Before the rewrite"
    assert rows[second]["label"] is None
    original_message = rows[first]["message"]
    assert original_message.startswith("snapshot ")

    renamed = client.post("/api/history/label", json={
        "vault": "books",
        "path": "chapter.md",
        "rev": first,
        "name": "Draft one",
    })
    assert renamed.status_code == 200
    listed = client.get("/api/history", params={"vault": "books", "path": "chapter.md"})
    again = {row["rev"]: row for row in listed.json()["commits"]}
    assert again[first]["label"] == "Draft one"
    assert again[first]["message"] == original_message

    cleared = client.post("/api/history/label", json={
        "vault": "books",
        "path": "chapter.md",
        "rev": first,
        "name": "   ",
    })
    assert cleared.status_code == 200
    assert cleared.json()["label"] is None

    client.post("/api/history/label", json={
        "vault": "books",
        "path": "chapter.md",
        "rev": first,
        "name": "Keep me",
    })
    restored = client.post("/api/history/restore", json={
        "vault": "books",
        "path": "chapter.md",
        "rev": first,
    })
    assert restored.status_code == 200
    assert restored.json()["content"] == "one"
    assert note.read_text(encoding="utf-8") == "one"
    assert (vaults / "books" / "other.md").read_text(encoding="utf-8") == "leave me"
    assert history.head_commit("books") == second
    kept = client.get("/api/history", params={"vault": "books", "path": "chapter.md"})
    assert {row["rev"]: row for row in kept.json()["commits"]}[first]["label"] == "Keep me"

    missing = client.post("/api/history/restore", json={
        "vault": "books",
        "path": "chapter.md",
        "rev": "a" * 40,
    })
    assert missing.status_code == 404
    bad = client.post("/api/history/label", json={
        "vault": "books",
        "path": "chapter.md",
        "rev": "nope",
        "name": "x",
    })
    assert bad.status_code == 400
    long_name = client.post("/api/history/label", json={
        "vault": "books",
        "path": "chapter.md",
        "rev": first,
        "name": "x" * 81,
    })
    assert long_name.status_code == 400
    bare = TestClient(app)
    denied = bare.post("/api/history/restore", json={
        "vault": "books",
        "path": "chapter.md",
        "rev": first,
    })
    assert denied.status_code == 401


def test_restore_follows_a_renamed_file(vaults):
    note = _write(vaults, "books", "old.md", "original")
    history.snapshot_vault("books")
    old_rev = history.head_commit("books")
    note.rename(vaults / "books" / "renamed.md")
    history.snapshot_vault("books")
    assert history.read_file_at("books", "renamed.md", old_rev) == "original"
    restored = client.post("/api/history/restore", json={
        "vault": "books",
        "path": "renamed.md",
        "rev": old_rev,
    })
    assert restored.status_code == 200
    assert (vaults / "books" / "renamed.md").read_text(encoding="utf-8") == "original"
    assert not (vaults / "books" / "old.md").exists()


def test_snapshot_all_commits_a_dirty_vault_once(vaults):
    _write(vaults, "books", "a.md", "one")
    (vaults / "notes").mkdir()
    history.snapshot_all()
    first = history.head_commit("books")
    assert first
    assert history.head_commit("notes") is None
    history.snapshot_all()
    assert history.head_commit("books") == first

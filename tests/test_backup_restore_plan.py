import hashlib
import io
import json
import tarfile

import pytest

from scripts import backup_restore_plan


def _artifacts(tmp_path, names=("images/photo.jpg",)):
    dump = tmp_path / "private-database.dump"
    # A header-only fixture intentionally does not prove database restorability.
    dump.write_bytes(b"PGDMPfixture")
    media = tmp_path / "private-images.tar.gz"
    with tarfile.open(media, "w:gz") as archive:
        for name in names:
            entry = tarfile.TarInfo(name)
            entry.size = 4
            archive.addfile(entry, io.BytesIO(b"data"))
    return dump, media


def test_manifest_does_not_claim_restore_or_disclose_paths(tmp_path):
    dump, media = _artifacts(tmp_path)
    manifest = backup_restore_plan.inspect_backup(dump, media, "a" * 40)
    assert manifest["database"]["sha256"] == hashlib.sha256(dump.read_bytes()).hexdigest()
    assert manifest["media"]["files"] == 1
    assert manifest["media"]["uncompressed_bytes"] == 4
    assert manifest["database_restore_test"] == "not_performed"
    assert manifest["cross_artifact_consistency"] == "not_verified"
    assert manifest["network_access"] is False
    assert manifest["restore_executed"] is False
    rendered = json.dumps(manifest)
    assert "private-" not in rendered
    assert "photo.jpg" not in rendered
    assert str(tmp_path) not in rendered


@pytest.mark.parametrize("name", ["/images/a", "images/../a", "images/./a", "images//a", "other/a", "images\\a", "images"])
def test_rejects_unsafe_media_paths(tmp_path, name):
    dump, media = _artifacts(tmp_path, (name,))
    with pytest.raises(backup_restore_plan.InvalidBackup, match="unsafe_media_archive"):
        backup_restore_plan.inspect_backup(dump, media, "a" * 40)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_rejects_links_and_special_files(tmp_path, kind):
    dump, media = _artifacts(tmp_path)
    with tarfile.open(media, "w:gz") as archive:
        entry = tarfile.TarInfo("images/link")
        entry.type = kind
        entry.linkname = "/private-target"
        archive.addfile(entry)
    with pytest.raises(backup_restore_plan.InvalidBackup, match="unsafe_media_archive"):
        backup_restore_plan.inspect_backup(dump, media, "a" * 40)


def test_rejects_duplicate_members(tmp_path):
    dump, media = _artifacts(tmp_path, ("images/a", "images/a"))
    with pytest.raises(backup_restore_plan.InvalidBackup, match="unsafe_media_archive"):
        backup_restore_plan.inspect_backup(dump, media, "a" * 40)


@pytest.mark.parametrize("names", [("images/a", "images/a/b"), ("images/a/b", "images/a")])
def test_rejects_a_file_that_is_also_a_parent_directory(tmp_path, names):
    dump, media = _artifacts(tmp_path, names)
    with pytest.raises(backup_restore_plan.InvalidBackup, match="unsafe_media_archive"):
        backup_restore_plan.inspect_backup(dump, media, "a" * 40)


def test_accepts_an_empty_images_directory(tmp_path):
    dump, media = _artifacts(tmp_path)
    with tarfile.open(media, "w:gz") as archive:
        entry = tarfile.TarInfo("images")
        entry.type = tarfile.DIRTYPE
        archive.addfile(entry)
    result = backup_restore_plan.inspect_backup(dump, media, "a" * 40)
    assert result["media"]["files"] == 0


def test_error_output_does_not_reflect_secret_path(tmp_path, capsys):
    missing = tmp_path / "credential-secret"
    assert backup_restore_plan.main(["--dump", str(missing), "--media", str(missing), "--revision", "a" * 40]) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "error", "code": "artifact_read_failed"}


def test_inspection_never_reads_connection_environment(tmp_path, monkeypatch, capsys):
    dump, media = _artifacts(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret@production.invalid/db")
    monkeypatch.setenv("REDIS_URL", "redis://secret@production.invalid")
    assert backup_restore_plan.main(["--dump", str(dump), "--media", str(media), "--revision", "b" * 40]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "offline_inspection"
    assert "secret" not in json.dumps(result)


def test_rejects_non_custom_dump_and_invalid_revision(tmp_path):
    dump, media = _artifacts(tmp_path)
    dump.write_bytes(b"plain SQL")
    with pytest.raises(backup_restore_plan.InvalidBackup, match="not_postgresql_custom_dump"):
        backup_restore_plan.inspect_backup(dump, media, "a" * 40)
    with pytest.raises(backup_restore_plan.InvalidBackup, match="invalid_revision"):
        backup_restore_plan.inspect_backup(dump, media, "not-a-sha")

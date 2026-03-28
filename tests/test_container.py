"""Tests for container management (src/container.py).

Only tests that don't require podman — content-addressed image naming,
build directory hashing, etc.
"""

import pytest

from src.container import compute_image_name


@pytest.fixture
def data_dir_with_container(tmp_path, monkeypatch):
    """Set up a data dir with system/container/ build context."""
    dd = tmp_path / "data"
    build_dir = dd / "system" / "container"
    build_dir.mkdir(parents=True)
    monkeypatch.setattr("src.container.data_dir", lambda: dd)
    return dd, build_dir


class TestComputeImageName:
    def test_prefix(self, data_dir_with_container):
        _, build_dir = data_dir_with_container
        (build_dir / "Containerfile").write_text("FROM ubuntu:24.04\n")
        name = compute_image_name()
        assert name.startswith("agent-kernel-img-")

    def test_hash_length(self, data_dir_with_container):
        _, build_dir = data_dir_with_container
        (build_dir / "Containerfile").write_text("FROM ubuntu:24.04\n")
        name = compute_image_name()
        # Format: agent-kernel-img-{12 hex chars}
        hash_part = name.split("agent-kernel-img-")[1]
        assert len(hash_part) == 12
        assert all(c in "0123456789abcdef" for c in hash_part)

    def test_deterministic(self, data_dir_with_container):
        _, build_dir = data_dir_with_container
        (build_dir / "Containerfile").write_text("FROM ubuntu:24.04\nRUN apt-get update\n")
        name1 = compute_image_name()
        name2 = compute_image_name()
        assert name1 == name2

    def test_content_changes_hash(self, data_dir_with_container):
        _, build_dir = data_dir_with_container
        (build_dir / "Containerfile").write_text("FROM ubuntu:24.04\n")
        name1 = compute_image_name()
        (build_dir / "Containerfile").write_text("FROM ubuntu:24.04\nRUN apt-get update\n")
        name2 = compute_image_name()
        assert name1 != name2

    def test_filename_matters(self, data_dir_with_container):
        """Different filenames with same content produce different hashes."""
        _, build_dir = data_dir_with_container
        (build_dir / "Containerfile").write_text("content\n")
        name1 = compute_image_name()
        (build_dir / "Containerfile").unlink()
        (build_dir / "Containerfile.alt").write_text("content\n")
        name2 = compute_image_name()
        assert name1 != name2

    def test_multiple_files_hashed(self, data_dir_with_container):
        _, build_dir = data_dir_with_container
        (build_dir / "Containerfile").write_text("FROM ubuntu:24.04\n")
        name1 = compute_image_name()
        # Adding an extra file changes the hash
        (build_dir / "extra.sh").write_text("#!/bin/bash\necho hello\n")
        name2 = compute_image_name()
        assert name1 != name2

    def test_no_build_dir_uses_path_hash(self, tmp_path, monkeypatch):
        """When system/container/ doesn't exist, falls back to path-based hash."""
        dd = tmp_path / "data"
        dd.mkdir()
        monkeypatch.setattr("src.container.data_dir", lambda: dd)
        name = compute_image_name()
        assert name.startswith("agent-kernel-img-")
        # Should still be a valid 12-char hex hash
        hash_part = name.split("agent-kernel-img-")[1]
        assert len(hash_part) == 12

    def test_subdirs_ignored(self, data_dir_with_container):
        """Only files (not subdirectories) are hashed."""
        _, build_dir = data_dir_with_container
        (build_dir / "Containerfile").write_text("FROM ubuntu:24.04\n")
        name1 = compute_image_name()
        # Adding a subdirectory should not change the hash
        subdir = build_dir / "scripts"
        subdir.mkdir()
        name2 = compute_image_name()
        assert name1 == name2

    def test_file_order_stable(self, data_dir_with_container):
        """Hash is stable regardless of file creation order (sorted by name)."""
        _, build_dir = data_dir_with_container
        (build_dir / "a_first.sh").write_text("#!/bin/bash\n")
        (build_dir / "b_second.sh").write_text("#!/bin/bash\n")
        (build_dir / "Containerfile").write_text("FROM ubuntu:24.04\n")
        name1 = compute_image_name()
        # Remove and recreate in different order
        for f in build_dir.iterdir():
            if f.is_file():
                f.unlink()
        (build_dir / "Containerfile").write_text("FROM ubuntu:24.04\n")
        (build_dir / "b_second.sh").write_text("#!/bin/bash\n")
        (build_dir / "a_first.sh").write_text("#!/bin/bash\n")
        name2 = compute_image_name()
        assert name1 == name2

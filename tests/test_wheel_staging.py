"""Wheels built from the working tree must not ship modules deleted from it."""

from declarative_agent_sdk.agent_board.machines import _clear_setuptools_staging


def test_stale_staging_is_cleared(tmp_path):
    stale = tmp_path / "build" / "lib" / "pkg" / "tools" / "builtin"
    stale.mkdir(parents=True)
    (stale / "share_file.py").write_text("def share_file(): ...\n")
    (tmp_path / "build" / "bdist.macosx-26.0-arm64").mkdir()
    (tmp_path / "build" / "notes.txt").write_text("not setuptools'")
    _clear_setuptools_staging(tmp_path)
    assert not (tmp_path / "build" / "lib").exists()
    assert not (tmp_path / "build" / "bdist.macosx-26.0-arm64").exists()
    assert (tmp_path / "build" / "notes.txt").exists()      # anything else is left


def test_a_symlinked_lib_is_not_followed(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.py").write_text("x")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "lib").symlink_to(elsewhere)
    _clear_setuptools_staging(tmp_path)
    assert (elsewhere / "keep.py").exists()


def test_no_build_dir_is_fine(tmp_path):
    _clear_setuptools_staging(tmp_path)

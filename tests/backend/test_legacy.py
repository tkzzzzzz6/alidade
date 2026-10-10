"""Compatibility with installs from before the rename to Alidade."""

from pathlib import Path

from alidade.legacy import apply_legacy_env, default_home


def test_old_env_names_fill_new_ones():
    env = {"DATA_FORMULATOR_HOME": "/old/home", "DF_AGENT_CONTEXT_BUDGET": "9000", "OTHER": "x"}
    filled = apply_legacy_env(env)
    assert env["ALIDADE_HOME"] == "/old/home"
    assert env["ALIDADE_AGENT_CONTEXT_BUDGET"] == "9000"
    assert sorted(filled) == ["ALIDADE_AGENT_CONTEXT_BUDGET", "ALIDADE_HOME"]


def test_explicit_new_name_wins():
    env = {"DF_PLUGIN_DIR": "/old", "ALIDADE_PLUGIN_DIR": "/new"}
    assert apply_legacy_env(env) == []
    assert env["ALIDADE_PLUGIN_DIR"] == "/new"


def test_old_data_dir_is_copied_once(tmp_path: Path):
    old = tmp_path / ".data_formulator"
    (old / "sessions").mkdir(parents=True)
    (old / "sessions" / "s1.json").write_text("{}")
    home = default_home(tmp_path)
    assert home == tmp_path / ".alidade"
    assert (home / "sessions" / "s1.json").read_text() == "{}"
    assert old.is_dir()  # kept as a backup

    (home / "sessions" / "s2.json").write_text("{}")
    assert default_home(tmp_path) == home  # no second copy over newer data
    assert (home / "sessions" / "s2.json").exists()


def test_fresh_install_uses_new_dir(tmp_path: Path):
    assert default_home(tmp_path) == tmp_path / ".alidade"
    assert not (tmp_path / ".alidade").exists()

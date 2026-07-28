"""Connecting edits someone's shell startup file, so disconnecting must be exact.

The failure that matters here isn't a wrong cost — it's leaving a stray line in
a profile, or eating a line that wasn't ours.
"""

import pytest

from tollgate import shell

BASE = "http://127.0.0.1:4141"
EXISTING = "# my settings\nexport EDITOR=vim\nalias ll='ls -la'\n"


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / ".zshrc"
    path.write_text(EXISTING)
    return path


def test_connect_then_disconnect_restores_the_file_exactly(profile):
    shell.connect(profile, BASE)
    assert shell.is_connected(profile)
    assert "ANTHROPIC_BASE_URL" in profile.read_text()

    shell.disconnect(profile)
    assert not shell.is_connected(profile)
    assert profile.read_text() == EXISTING


def test_connect_leaves_surrounding_lines_untouched(profile):
    shell.connect(profile, BASE)
    text = profile.read_text()
    for line in EXISTING.splitlines():
        assert line in text


def test_connecting_twice_does_not_stack_two_blocks(profile):
    shell.connect(profile, BASE)
    shell.connect(profile, BASE)
    assert profile.read_text().count(shell.START) == 1

    shell.disconnect(profile)
    assert profile.read_text() == EXISTING


def test_reconnecting_on_a_new_port_replaces_the_old_setting(profile):
    shell.connect(profile, BASE)
    shell.connect(profile, "http://127.0.0.1:9999")
    text = profile.read_text()
    assert "9999" in text
    assert "4141" not in text


def test_disconnect_is_safe_when_nothing_was_connected(profile):
    assert shell.disconnect(profile) is False
    assert profile.read_text() == EXISTING


def test_disconnect_is_safe_when_the_file_does_not_exist(tmp_path):
    assert shell.disconnect(tmp_path / "nope") is False


def test_connect_creates_a_profile_that_did_not_exist(tmp_path):
    fresh = tmp_path / ".zshrc"
    assert shell.connect(fresh, BASE) is True
    assert shell.is_connected(fresh)


def test_a_hand_edited_block_missing_its_end_marker_still_disconnects(profile):
    """Someone will delete the closing marker by hand. Leaving the exports
    behind with no way to remove them is the worst outcome."""
    profile.write_text(EXISTING + f"{shell.START}\nexport ANTHROPIC_BASE_URL={BASE}\n")
    assert shell.disconnect(profile) is True
    assert "ANTHROPIC_BASE_URL" not in profile.read_text()


def test_the_previous_profile_is_backed_up_before_any_edit(profile):
    shell.connect(profile, BASE)
    backup = profile.with_suffix(profile.suffix + ".tollgate-backup")
    assert backup.read_text() == EXISTING


def test_the_block_says_how_to_remove_itself(profile):
    shell.connect(profile, BASE)
    assert "tollgate disconnect" in profile.read_text()


def test_fish_uses_its_own_syntax(tmp_path):
    path = tmp_path / "config.fish"
    shell.connect(path, BASE, shell="/usr/local/bin/fish")
    text = path.read_text()
    assert "set -gx ANTHROPIC_BASE_URL" in text
    assert "export " not in text
    assert shell.disconnect(path) is True


@pytest.mark.parametrize(
    "shell_path,expected",
    [
        ("/bin/zsh", ".zshrc"),
        ("/usr/local/bin/fish", "config.fish"),
        ("/bin/bash", ".bashrc"),
        ("", ".zshrc"),
    ],
)
def test_profile_path_follows_the_users_shell(shell_path, expected, tmp_path):
    assert shell.profile_path(shell_path, home=tmp_path).name == expected


def test_bash_prefers_a_profile_that_already_exists(tmp_path):
    (tmp_path / ".bash_profile").write_text("# hi\n")
    assert shell.profile_path("/bin/bash", home=tmp_path).name == ".bash_profile"

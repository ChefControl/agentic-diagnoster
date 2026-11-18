"""Settings from the environment: the default model follows the key that is set, and the cluster mode is checked."""

import pytest

from diagnoster import config


@pytest.fixture
def env(monkeypatch, tmp_path):
    # No .env from the checkout, and none of the variables these tests set.
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    for name in ("MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLUSTER_MODE"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    ("keys", "model"),
    [
        ({"OPENAI_API_KEY": "x"}, "openai:gpt-6-luna"),
        ({"ANTHROPIC_API_KEY": "x"}, "anthropic:claude-haiku-5-5"),
        ({"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "x"}, "openai:gpt-6-luna"),
        ({}, "openai:gpt-6-luna"),  # no key: runs then fail with a message naming it
    ],
)
def test_the_default_model_follows_the_key_that_is_set(env, keys, model):
    for name, value in keys.items():
        env.setenv(name, value)
    assert config.load_settings().model == model


def test_model_overrides_the_default(env):
    env.setenv("ANTHROPIC_API_KEY", "x")
    env.setenv("MODEL", "anthropic:claude-opus-5-5")
    assert config.load_settings().model == "anthropic:claude-opus-5-5"


def test_the_cluster_mode_is_live_unless_set(env):
    assert config.load_settings().cluster_mode == "live"
    env.setenv("CLUSTER_MODE", "recorded")
    assert config.load_settings().cluster_mode == "recorded"


def test_an_unknown_cluster_mode_is_an_error(env):
    env.setenv("CLUSTER_MODE", "replay")
    with pytest.raises(ValueError, match="use live or recorded"):
        config.load_settings()

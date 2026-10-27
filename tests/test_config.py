"""Model selection is explicit, role aware, and safe to persist with each run."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from box_agent_sdlc.config import Settings
from box_agent_sdlc.pipeline import Pipeline


@pytest.fixture
def model_settings(monkeypatch):
    for name in (
        "DEFAULT_PLANNER_PROVIDER",
        "DEFAULT_CODER_PROVIDER",
        "DEFAULT_REVIEWER_PROVIDER",
        "DEFAULT_ANTHROPIC_MODEL",
        "DEFAULT_OPENAI_MODEL",
        "DEFAULT_GOOGLE_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    return Settings(auth_mode="development", mode="scripted")


def test_reference_role_model_defaults_remain_available(model_settings):
    assert model_settings.agent_model("triager") == "anthropic:claude-sonnet-4-5"
    assert model_settings.agent_model("planner") == "anthropic:claude-sonnet-4-5"
    assert model_settings.agent_model("coder") == "anthropic:claude-sonnet-4-5"
    assert model_settings.agent_model("reviewer") == "openai:gpt-4.1"
    assert model_settings.agent_model("security") == "openai:gpt-4.1"


def test_environment_selects_openai_for_all_roles(model_settings, monkeypatch):
    for role in ("PLANNER", "CODER", "REVIEWER"):
        monkeypatch.setenv(f"DEFAULT_{role}_PROVIDER", "openai")
    monkeypatch.setenv("DEFAULT_OPENAI_MODEL", "gpt-4.1")
    configured = Settings(auth_mode="development", mode="scripted")
    configured.validate()
    assert {configured.agent_model(role) for role in ("planner", "coder", "reviewer")} == {"openai:gpt-4.1"}


def test_saved_models_survive_changed_deployment_defaults(model_settings):
    overrides = {"coder": {"provider": "openai", "model": "gpt-4.1-mini"}}
    saved = model_settings.agent_models(overrides)
    changed = replace(
        model_settings,
        default_planner_provider="openai",
        default_openai_model="different-model",
        default_anthropic_model="different-claude",
    )
    assert changed.agent_model("planner", saved) == "anthropic:claude-sonnet-4-5"
    assert changed.agent_model("coder", saved) == "openai:gpt-4.1-mini"
    assert changed.agent_model("security", saved) == "openai:gpt-4.1"
    assert model_settings.agent_model("planner", {"planner": "openai:legacy-saved-model"}) == (
        "openai:legacy-saved-model"
    )


def test_default_provider_is_validated(model_settings):
    with pytest.raises(ValueError, match="Default agent providers"):
        replace(model_settings, default_planner_provider="unsupported").validate()


@pytest.mark.parametrize("role", ["triager", "planner", "coder", "reviewer", "security"])
async def test_pipeline_dispatches_configured_live_model(model_settings, role):
    settings = replace(model_settings, default_planner_provider="openai", default_coder_provider="openai")
    selected = []

    class Runtime:
        def next_action(self, checkpoint):
            return "tool"

        async def step(self, turn_id, agent_role, agent_input, checkpoint, workspace, model):
            selected.append((agent_role, model))
            return {"done": True}

    class Context:
        async def fault(self, target):
            pass

        async def effect(self, name, request, call, **kwargs):
            return await call()

    run = SimpleNamespace(mode="live", state={"repo": "example/storefront"})
    pipeline = Pipeline(SimpleNamespace(settings=settings), runtime=Runtime())
    await pipeline.agent(Context(), run, {"role": role, "turn_id": "test", "input": {}})
    assert selected == [(role, "openai:gpt-4.1")]

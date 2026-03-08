import cli


def test_step_models_supports_multi_provider_per_role(monkeypatch):
    state = {
        "provider": "g1",
        "providers": {
            "g1": {"type": "gemini", "api_key": "k1"},
            "o1": {"type": "openai", "api_key": "k2", "base_url": "https://api.openai.com/v1"},
        },
        "model_providers": {},
        "models": {},
    }

    monkeypatch.setattr(cli, "get_gemini_models", lambda _key: ["gemini-fast", "gemini-smart"])
    monkeypatch.setattr(cli, "get_openai_models", lambda _key, _base: ["gpt-fast", "gpt-smart"])

    role_provider = {
        "smart": "g1",
        "fast": "o1",
        "coder": "o1",
        "image": "g1",
        "summary": "g1",
    }
    role_model = {
        "smart": "gemini-smart",
        "fast": "gpt-fast",
        "coder": "gpt-smart",
        "image": "gemini-fast",
        "summary": "gemini-fast",
    }

    def fake_select_provider(self, role_key, _entries):
        return role_provider[role_key]

    def fake_select_model(self, _model_list, role_key, _provider_type):
        return role_model[role_key]

    monkeypatch.setattr(cli.StepModels, "_select_provider_for_role", fake_select_provider)
    monkeypatch.setattr(cli.StepModels, "select_model", fake_select_model)

    class FakeCostTracker:
        def __init__(self, db_path):
            self.db_path = db_path

        def get_model_price(self, provider, model_name):
            return {"input": 0.1, "output": 0.2}

    monkeypatch.setattr(cli, "CostTracker", FakeCostTracker)

    step = cli.StepModels(state)
    assert step.run() is True

    assert state["models"] == role_model
    assert state["model_providers"] == role_provider

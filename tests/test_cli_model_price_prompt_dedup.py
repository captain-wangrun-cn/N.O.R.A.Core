import cli


def test_step_models_manual_price_prompt_deduplicated(monkeypatch):
    """同一模型被多个角色复用时，只应手动询价一次。"""
    state = {
        "provider": "gemini",
        "providers": {
            "gemini": {
                "type": "gemini",
                "api_key": "dummy-key",
            }
        },
        "model_providers": {},
        "models": {},
    }

    # 避免真实 API 调用
    monkeypatch.setattr(cli, "get_gemini_models", lambda _key: ["dup-model", "other-model"])

    # 固定四个角色都选择同一个模型
    selections = {
        "smart": "dup-model",
        "fast": "dup-model",
        "coder": "dup-model",
        "image": "dup-model",
        "summary": "dup-model",
    }

    def fake_select_model(self, _model_list, role_key, _provider):
        return selections[role_key]

    monkeypatch.setattr(cli.StepModels, "select_model", fake_select_model)

    # 使用假的 CostTracker，避免真实初始化读取 config.yml
    class FakeCostTracker:
        def __init__(self, db_path):
            self.db_path = db_path

        def get_model_price(self, provider, model_name):
            return None

    monkeypatch.setattr(cli, "CostTracker", FakeCostTracker)

    prompted_models = []

    def fake_prompt_price(self, model_name):
        prompted_models.append(model_name)
        return {"input": 1.0, "output": 2.0}

    monkeypatch.setattr(cli.StepModels, "_prompt_price", fake_prompt_price)

    step = cli.StepModels(state)
    assert step.run() is True

    # 关键断言：虽然 4 个角色都用了同一模型，但只询价 1 次
    assert prompted_models == ["dup-model"]

    # 最终写入的价格也应只有一个模型项
    assert state["model_prices"] == {"dup-model": {"input": 1.0, "output": 2.0}}

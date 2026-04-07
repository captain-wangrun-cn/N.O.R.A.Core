# 多模型架构技术文档（Multi-Model Architecture）

> 目标：说明 N.O.R.A.Core 如何在不同场景下选择模型、绑定 provider、做故障回退，并给出可维护的配置与排障方式。

---

## 1. 设计目标

多模型机制用于同时满足以下目标：

1. **响应速度**：简单判断尽量快。
2. **执行能力**：复杂任务使用更强模型。
3. **多模态能力**：图片输入走专门模型。
4. **成本可控**：不同阶段使用不同价位模型。
5. **可替换与扩展**：模型与 provider 解耦，通过 alias 配置。

---

## 2. 模型别名与职责

当前系统约定的核心 alias：

- `smart`：默认主对话模型（也是 `llm_client` 默认 alias）。
- `fast`：快速判断/轻量场景模型（已初始化，可按业务接入更多路径）。
- `coder`：后脑主执行模型（工具/技能/复杂任务）。
- `image`：图片输入理解模型。
- `summary`：压缩与总结模型（消息压缩、归档等场景）。

---

## 3. 配置结构（`config.yml`）

### 3.1 provider 定义

在 `llm.providers` 下定义多个 provider，并在 `llm.provider` 中指定默认 provider 名称。
每个 provider 至少包含：`type` 与 `api_key`；OpenAI 兼容端可额外包含 `base_url`。

### 3.2 alias 到 provider 绑定

通过 `llm.model_providers` 将 `smart/fast/coder/image/summary` 等 alias 绑定到具体 provider。
未显式绑定 alias 时，回退到 `llm.provider` 指定的默认 provider。

### 3.3 alias 到具体模型名绑定

通过 `llm.models` 为每个 alias 指定具体模型名。
推荐至少完整配置 `smart`、`fast`、`coder`、`image`、`summary`，避免路由后模型缺失。

---

## 4. 代码层路由机制

## 4.1 LLM 工厂（`brain/llm.py`）

`get_llm_client(provider_name=None, model_alias="smart")` 的核心行为：

1. 根据 alias 获取 provider 名（`config.get_model_provider(alias)`）。
2. 根据 provider 获取类型（`gemini` / `openai`）。
3. 返回对应 Provider 实例（`GeminiProvider` / `OpenAIProvider`）。

这使得“业务逻辑只关心 alias，不关心底层厂商”。

## 4.2 控制器初始化（`core/controller.py`）

`NoraController.__init__` 中会初始化：

- `self.llm = llm_client`（默认 alias=`smart`）
- `self.fast_llm = get_llm_client(model_alias="fast")`
- `self.coder_llm = get_llm_client(model_alias="coder")`
- `self.image_llm = get_llm_client(model_alias="image")`

并在 `_reload_llm_clients()` 支持热重载配置后刷新。

## 4.3 前后脑选模（当前实现）

- 前脑主回复：使用 `self.llm`（即 `smart`）。
- 前脑审查：使用 `self.llm`（即 `smart`）。
- 忙碌意图判断：使用 `self.llm`（即 `smart`）。
- 后脑执行：默认 `coder`；若有图片输入或 `use_image_model=true`，切换到 `image`。

> 注：`fast_llm` 已初始化，可用于扩展更低延迟路径；是否在特定前脑分支启用，取决于当前实现与策略。

---

## 5. 回退与容错策略

控制器初始化中采用了“失败回退到 `self.llm`”策略：

- `fast_llm` 初始化失败 → 回退 `self.llm`
- `coder_llm` 初始化失败 → 回退 `self.llm`
- `image_llm` 初始化失败 → 回退 `self.llm`

优点：

- 配置不完整时不至于直接崩溃。
- 可先跑通主流程，再逐步优化多模型配置。

风险：

- 回退后能力边界会变化（例如图片场景退化为文本主模型）。
- 建议在日志中持续关注初始化告警并修复配置。

---

## 6. 成本与性能建议

1. **高频轻任务**优先绑定便宜模型（如 `fast`）。
2. **复杂工具链任务**给 `coder` 更强模型。
3. **图片任务**优先给 `image` 支持多模态能力的模型。
4. `summary` 可选更低成本模型，减少长期运行支出。
5. 在 `cost_tracking.custom_prices` 维护实际价格，避免统计失真。

---

## 7. 常见配置错误与排障

### 问题 1：alias 无法命中 provider

现象：启动时报 provider 不存在或类型不支持。

检查：

- `llm.model_providers.<alias>` 的值是否存在于 `llm.providers` 键中。
- `llm.providers.<name>.type` 是否为 `gemini` 或 `openai`。

### 问题 2：provider 有配置但模型不可用

现象：调用时报模型不存在/权限不足。

检查：

- `llm.models.<alias>` 的模型名是否在该 provider 账户可用。
- API key / base_url 是否正确。

### 问题 3：图片场景效果异常

检查：

- `image` alias 是否绑定到支持图片输入的模型。
- 任务是否触发了图片路由（有图片或 `use_image_model=true`）。

### 问题 4：多模型都“看起来像同一个模型”

原因通常是 alias 都绑定到同一 provider+同一模型名。

建议：

- 明确区分 `fast/smart/coder/image/summary` 的模型名。
- 配置后执行一次重载（或重启）确认生效。

---

## 8. 推荐配置基线（参考）

建议采用“默认 provider + alias 覆盖”的策略：

1. 设置一个稳定的默认 provider（用于兜底）。
2. 给 `fast` 绑定低延迟模型。
3. 给 `coder` 绑定更强推理模型。
4. 给 `image` 绑定支持图片输入的多模态模型。
5. 给 `summary` 绑定低成本高吞吐模型。

并保持 alias 与实际职责一致，避免出现“配置有 alias，但语义不匹配”的情况。

---

## 9. 相关文件

- `brain/llm.py`
- `core/controller.py`
- `config.py`
- `config.example.yml`
- `docs/architecture/dual-process-architecture.md`
- `docs/architecture/message-compression.md`

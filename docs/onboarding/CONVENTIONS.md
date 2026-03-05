# 📏 开发约定

> 本项目的编码、测试、文档等约定。遵守这些约定可以保持一致性并减少冲突。

---

## 1. 代码风格

- **语言：** Python 3.12+
- **异步：** 核心路径使用 `async/await`（asyncio 驱动）
- **类型提示：** 鼓励使用，关键函数/方法必须有
- **日志：** 使用 `logging` 模块，不用 `print`（测试脚本除外）
  - `logger = logging.getLogger(__name__)`
- **导入顺序：** 标准库 → 第三方 → 项目内部

---

## 2. 项目结构约定

| 目录 | 用途 | 约定 |
|------|------|------|
| `core/` | 核心业务逻辑 | 控制器、成本跟踪 |
| `brain/` | LLM 相关 | 提供方、工具、提示词 |
| `adapters/` | 平台适配 | 每个平台一个子目录 |
| `memory/` | 记忆/存储 | 消息历史、向量、RAG |
| `skills/` | 技能插件 | 每个技能一个子目录（`main.py` + `SKILL.md`） |
| `tests/` | 测试 | 文件名 `test_*.py`，pytest 兼容 |
| `docs/` | 文档 | 架构设计、交接、引导 |
| `locales/` | 多语言 | YAML 格式 |

---

## 3. 配置约定

- **不提交** `config.yml`、`.env`（已在 `.gitignore`）
- 新增配置项时，同步更新 `config.example.yml`
- 配置读取统一走 `config.py` 的 helper 函数（如 `get_message_history_config()`）

---

## 4. 测试约定

- 测试放在 `tests/` 目录，命名 `test_*.py`
- 使用 `pytest` 框架
- 异步测试需要 `pytest-asyncio` + `@pytest.mark.asyncio`
- 测试数据库使用临时文件或 `:memory:`，**不要**操作生产数据库
- 运行：`pytest` 或 `pytest tests/test_xxx.py -v`

---

## 5. 提示词修改约定

- **行为规则**：编辑 `brain/templates/system.jinja`
- **人设/性格**：编辑 `brain/templates/persona_nora.jinja`
- **平台特定**：编辑对应适配器目录下的 `PROMPT.md`（如 `adapters/telegram/PROMPT.md`）
- 修改后务必实际测试对话效果，提示词细微变化可能导致行为巨变

---

## 6. 工具/技能约定

### 添加新内置工具
1. 在 `brain/tools.py` 中注册函数
2. 确保 docstring 完整（用于自动生成 schema）
3. 参数类型标注正确（`Optional[int]`、`float` 等均已支持）

### 添加新技能
1. 使用 `create_new_skill` 工具生成标准目录
2. 编写 `SKILL.md`（必须，AI 靠它学习使用方法）
3. 编写 `main.py`（通过环境变量获取配置和路径）
4. 通过 `execute_skill` 调用，**禁止** `exec_command` 直接运行脚本

---

## 7. 文档维护约定

- **每次重大改动后**：更新 `docs/HANDOVER.md`
- **新增子系统时**：在 `docs/architecture/` 添加设计文档并更新 `README.md` 索引
- **接手/交接时**：阅读并更新 `docs/onboarding/` 下的引导文档
- 使用中文撰写文档（与项目主语言一致）

---

## 8. Git 约定

- 主分支：`main`
- 提交信息：简洁描述改动（中英文均可）
- 敏感信息（Key、Token）**永远不要**提交

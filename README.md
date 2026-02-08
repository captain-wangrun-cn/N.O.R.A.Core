# N.O.R.A. Core (Project Echo)

**N.O.R.A.** (Neural Optimized Responsive Assistant) Core is a lightweight, memory-enhanced AI assistant framework designed for personal companionship and high efficiency.

## 🏗 Architecture

- **Brain**: Python Controller + Dynamic LLM Router (Gemini Flash/Pro).
- **Memory (RAG)**: SiliconFlow Embedding API + Qdrant Vector DB.
- **Skills**: Hot-reloadable Python tools (`tools/` directory).
- **Interface**: Telegram Bot API.

## 🚀 Setup

1. Clone the repo:

   ```bash
   git clone https://github.com/captain-wangrun-cn/N.O.R.A.Core.git
   cd N.O.R.A.Core
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Configure using the CLI wizard:

   ```bash
   python cli.py --configure
   ```

   Or use the interactive menu:

   ```bash
   python cli.py
   ```

4. Run:
   ```bash
   python main.py
   ```

## 🛠 Management CLI

N.O.R.A. Core includes a powerful CLI tool (`cli.py`) for configuration and maintenance:

```bash
# Interactive menu
python cli.py

# Direct commands
python cli.py --configure      # Run configuration wizard
python cli.py --test-qdrant    # Test Qdrant connection
python cli.py --test-rag       # Test RAG system
python cli.py --clean-rag      # Clean RAG data
```

See [CLI Usage Guide](docs/CLI_USAGE.md) for detailed documentation.

## 📂 Structure

- `main.py`: Entry point and Bot Controller.
- `brain/`: LLM routing and logic.
- `memory/`: RAG and MongoDB connectors.
- `tools/`: Dynamic skill scripts.
- `utils/`: Helpers.

## 📝 License

Private / Personal Use Only.

# Global Configuration for N.O.R.A. Core
import os
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

# Telegram Bot Token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# LLM Provider Configuration
# Supported values: "gemini", "openai"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()

# API Keys
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Model Name Abstractions
# You can change these to point to different models, e.g., "gpt-4o", "gemini-1.5-pro-latest"
MODEL_SMART = os.getenv("MODEL_SMART", "gemini-1.5-pro-latest")
MODEL_FAST = os.getenv("MODEL_FAST", "gemini-1.5-flash-latest")
MODEL_CODER = os.getenv("MODEL_CODER", "gpt-4o") # Example, can be same as smart

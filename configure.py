# N.O.R.A. Core - Interactive Configuration Wizard (YAML Edition)
import questionary
import os
import yaml
import google.generativeai as genai
import openai

CONFIG_FILE = "config.yml"

def get_gemini_models(api_key):
    """Fetches available Gemini models."""
    try:
        genai.configure(api_key=api_key)
        models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        return [model.replace("models/", "") for model in models]
    except Exception as e:
        questionary.print(f"Could not fetch Gemini models: {e}", style="bold red").ask()
        return []

def get_openai_models(api_key):
    """Fetches available OpenAI models."""
    try:
        client = openai.OpenAI(api_key=api_key)
        models = [model.id for model in client.models.list()]
        return sorted([m for m in models if "gpt" in m.lower()])
    except Exception as e:
        questionary.print(f"Could not fetch OpenAI models: {e}", style="bold red").ask()
        return []

def select_model(model_list, role):
    """Asks the user to select a model from a list or enter manually."""
    if not model_list:
        return questionary.text(f"Enter the model name for the '{role}' role:").ask()
        
    choices = model_list + [questionary.Separator(), "--- Manually Enter ---"]
    selection = questionary.select(
        f"Select the model for the '{role}' role:",
        choices=choices,
    ).ask()
    
    if selection == "--- Manually Enter ---":
        return questionary.text(f"Manually enter model name for '{role}':").ask()
    return selection

def run_wizard():
    print("Welcome to the N.O.R.A. Core YAML Configuration Wizard!")
    
    # Load existing config if it exists
    existing_config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            existing_config = yaml.safe_load(f) or {}

    # --- Start Questions ---
    
    telegram_token = questionary.password(
        "Please enter your Telegram Bot Token:",
        default=existing_config.get("telegram", {}).get("bot_token", "")
    ).ask()
    
    provider = questionary.select(
        "Which primary LLM provider will you use?",
        choices=["gemini", "openai"],
        default=existing_config.get("llm", {}).get("provider", "gemini")
    ).ask()
    
    gemini_key = ""
    openai_key = ""
    
    if provider == 'gemini':
        gemini_key = questionary.password(
            "Please enter your Google Gemini API Key:",
            default=existing_config.get("llm", {}).get("api_keys", {}).get("gemini", "")
        ).ask()
    else:
        openai_key = questionary.password(
            "Please enter your OpenAI API Key:",
            default=existing_config.get("llm", {}).get("api_keys", {}).get("openai", "")
        ).ask()

    # --- Model Selection ---
    models = {}
    model_list = []
    if provider == 'gemini' and gemini_key:
        model_list = get_gemini_models(gemini_key)
    elif provider == 'openai' and openai_key:
        model_list = get_openai_models(openai_key)
        
    models['smart'] = select_model(model_list, 'smart (for complex reasoning)')
    models['fast'] = select_model(model_list, 'fast (for chat and simple tasks)')
    models['coder'] = select_model(model_list, 'coder (for code generation)')

    # --- Build Final Config ---
    final_config = {
        'telegram': {'bot_token': telegram_token},
        'llm': {
            'provider': provider,
            'api_keys': {
                'gemini': gemini_key,
                'openai': openai_key
            },
            'models': models
        }
    }
    
    # --- Confirmation ---
    print("\n--- Configuration Summary ---")
    print(yaml.dump(final_config, indent=2))
    
    if questionary.confirm("Save this configuration?").ask():
        with open(CONFIG_FILE, 'w') as f:
            yaml.dump(final_config, f, indent=2)
        print(f"✅ Configuration saved to {CONFIG_FILE}")
    else:
        print("Configuration aborted.")

if __name__ == "__main__":
    run_wizard()

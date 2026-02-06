# N.O.R.A. Core - Interactive Configuration Wizard
import questionary
import os
from dotenv import set_key, find_dotenv

def run_wizard():
    """
    Guides the user through setting up the .env file for the project.
    """
    print("Welcome to the N.O.R.A. Core Configuration Wizard!")
    print("I'll help you set up the necessary API keys and settings.")
    print("Press Ctrl+C at any time to exit without saving.")
    
    env_path = find_dotenv()
    if not env_path:
        env_path = ".env" # Create it if it doesn't exist
    
    questions = [
        {
            "type": "password",
            "name": "TELEGRAM_BOT_TOKEN",
            "message": "Please enter your Telegram Bot Token:",
            "validate": lambda text: len(text) > 20 or "Token seems too short."
        },
        {
            "type": "select",
            "name": "LLM_PROVIDER",
            "message": "Which primary LLM provider will you use?",
            "choices": ["gemini", "openai"],
            "default": "gemini"
        },
        {
            "type": "password",
            "name": "GEMINI_API_KEY",
            "message": "Please enter your Google Gemini API Key:",
            "when": lambda answers: answers["LLM_PROVIDER"] == "gemini"
        },
        {
            "type": "password",
            "name": "OPENAI_API_KEY",
            "message": "Please enter your OpenAI API Key:",
            "when": lambda answers: answers["LLM_PROVIDER"] == "openai"
        },
        {
            "type": "confirm",
            "name": "proceed",
            "message": "Configuration complete. Save these settings to your .env file?",
            "default": True
        }
    ]
    
    try:
        answers = questionary.prompt(questions)
        
        if not answers or not answers.get("proceed"):
            print("Configuration aborted. No changes were made.")
            return

        # Save to .env file
        for key, value in answers.items():
            if key != "proceed" and value:
                set_key(env_path, key, value)
                
        print(f"✅ Configuration successfully saved to {env_path}")
        print("You can now run 'python main.py' to start N.O.R.A. Core.")

    except KeyboardInterrupt:
        print("\nConfiguration aborted by user.")

if __name__ == "__main__":
    run_wizard()


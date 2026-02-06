from telegram import Update
from telegram.ext import ContextTypes

from brain.llm import llm_client
from brain.prompts import NORA_SOUL_PROMPT

class NoraController:
    """
    Handles the core business logic of the bot.
    It orchestrates the flow of data between the user, the LLM, and the memory systems.
    """
    def __init__(self):
        self.llm = llm_client
        # In-memory session store (L1 Cache). Will be replaced by session.py later.
        self.sessions = {}
        print("NoraController initialized.")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler for the /start command."""
        chat_id = update.effective_chat.id
        self.sessions[chat_id] = [] # Reset history on /start
        await context.bot.send_message(
            chat_id=chat_id,
            text="N.O.R.A. Core [v2.0.0-alpha] online. Systems nominal."
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Main handler for text messages."""
        chat_id = update.effective_chat.id
        user_text = update.message.text
        
        # --- Memory (Phase 2 Placeholder) ---
        # 1. Retrieve history (L1 Cache)
        history = self.sessions.setdefault(chat_id, [])
        # 2. Query RAG (L2 Cache) - Not implemented yet
        
        # --- Brain ---
        # 3. Call the LLM
        response_text = await self.llm.chat(
            system_prompt=NORA_SOUL_PROMPT,
            user_prompt=user_text,
            history=history
        )
        
        # --- Update Memory ---
        # 4. Update L1 Cache
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": response_text})
        
        # 5. Simple history cap to prevent overflow
        if len(history) > 20: # Keeps the last 10 turns
            self.sessions[chat_id] = history[-20:]
            
        # --- Respond ---
        await context.bot.send_message(chat_id=chat_id, text=response_text)

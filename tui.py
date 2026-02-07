# nora-core/tui.py
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Header, Footer, Static, RichLog

class TUI(App):
    """A Textual UI for monitoring N.O.R.A. Core."""

    CSS_PATH = "tui.css"
    BINDINGS = [("d", "toggle_dark", "Toggle dark mode")]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._is_running = False

    def on_mount(self) -> None:
        """Called when app starts."""
        self.title = "N.O.R.A. Core Dashboard"
        self._is_running = True

    def on_unmount(self) -> None:
        """Called when app stops."""
        self._is_running = False

    def update_status(self, text: str):
        """Method to update the status panel from an external thread."""
        self.query_one("#status_panel", Static).update(text)

    def write_log(self, text: str):
        """Method to write to the log panel from an external thread."""
        self.query_one("#log_panel", RichLog).write(text)

if __name__ == "__main__":
    app = TUI()
    app.run()

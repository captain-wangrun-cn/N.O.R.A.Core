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

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()
        with Container():
            yield RichLog(id="log_panel", wrap=True, highlight=True)
            yield Static("Initializing...", id="status_panel")
        yield Footer()

    def on_mount(self) -> None:
        """Called when app starts."""
        self.title = "N.O.R.A. Core Dashboard"
        self._is_running = True
        self.query_one("#log_panel", RichLog).write("TUI Mounted and Ready.")
        self.query_one("#status_panel", Static).update("System Active")

    def on_unmount(self) -> None:
        """Called when app stops."""
        self._is_running = False

    def action_toggle_dark(self) -> None:
        """An action to toggle dark mode."""
        self.dark = not self.dark

    def update_status(self, text: str):
        """Method to update the status panel from an external thread."""
        self.query_one("#status_panel", Static).update(text)

    def write_log(self, text: str):
        """Method to write to the log panel from an external thread."""
        self.query_one("#log_panel", RichLog).write(text)

# This subclass is used in main.py to handle the ready event
class NoraTUI(TUI):
    def __init__(self, tui_ready_event, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tui_ready_event = tui_ready_event

    def on_mount(self) -> None:
        super().on_mount()
        self.tui_ready_event.set() # Signal that the TUI is ready

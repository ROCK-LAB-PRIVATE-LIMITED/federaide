import json
import os
from pathlib import Path

FEDERAIDE_SYS_DIR = os.path.join(str(Path.home()), ".federaide")
DEFAULT_TEAM_DIR = os.path.join(str(Path.home()), ".federate")
GLOBAL_SETTINGS_FILE = os.path.join(FEDERAIDE_SYS_DIR, "global_settings.json")


def get_team_dir() -> str:
    """Lightweight team directory lookup without importing agent/LLM dependencies."""
    if os.path.exists(GLOBAL_SETTINGS_FILE):
        try:
            with open(GLOBAL_SETTINGS_FILE, "r", encoding="utf-8") as f:
                active = json.load(f).get("active_team_dir")
                if active:
                    return os.path.abspath(os.path.expanduser(active))
        except Exception:
            pass
    return DEFAULT_TEAM_DIR


def load_team_settings() -> dict:
    """Lightweight team settings lookup for standalone IDE mode."""
    defaults = {"theme": "tokyo-night"}
    path = os.path.join(get_team_dir(), "team_settings.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    return defaults


from rich.style import Style
from textual.widgets import TextArea


class PygmentsTextArea(TextArea):
    """TextArea subclass providing Pygments syntax highlighting via rich.syntax.Syntax."""

    def __init__(self, *args, hl_path: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._hl_path = hl_path
        self._hl_cache = None

    def _get_highlight_spans(self) -> list:
        code = self.text
        hl_path = getattr(self, "_hl_path", "") or ""
        is_dark = True
        try:
            t_obj = self.app.available_themes.get(self.app.theme)
            if t_obj is not None:
                is_dark = getattr(t_obj, "dark", True)
        except Exception:
            pass

        cache_key = (code, hl_path, is_dark)
        cached = getattr(self, "_hl_cache", None)
        if cached and cached[0] == cache_key:
            return cached[1]

        spans_by_line = [[]]
        try:
            from rich.syntax import Syntax
            theme_name = "monokai" if is_dark else "friendly"
            lexer_name = Syntax.guess_lexer(hl_path, code=code[:2048]) if hl_path else "text"
            syntax = Syntax("", lexer_name, theme=theme_name)
            theme = Syntax.get_theme(theme_name)
            style_cache = {}
            line_idx = 0
            col = 0
            null_style = Style.null()
            for _, tok_type, val in syntax.lexer.get_tokens_unprocessed(code):
                style = style_cache.get(tok_type)
                if style is None:
                    raw = theme.get_style_for_token(tok_type)
                    if isinstance(raw, Style) and raw.bgcolor is not None:
                        style = Style(
                            color=raw.color,
                            bold=raw.bold,
                            dim=raw.dim,
                            italic=raw.italic,
                            underline=raw.underline,
                            strike=raw.strike,
                        )
                    else:
                        style = raw
                    style_cache[tok_type] = style

                parts = val.split("\n")
                for i, part in enumerate(parts):
                    if i > 0:
                        line_idx += 1
                        col = 0
                        spans_by_line.append([])
                    if part:
                        end_col = col + len(part)
                        if style != null_style:
                            spans_by_line[line_idx].append((col, end_col, style))
                        col = end_col
        except Exception:
            spans_by_line = []

        self._hl_cache = (cache_key, spans_by_line)
        return spans_by_line

    def get_line(self, line_index: int):
        line = super().get_line(line_index)
        spans_by_line = self._get_highlight_spans()
        if line_index < len(spans_by_line):
            for start, end, style in spans_by_line[line_index]:
                line.stylize(style, start, end)
        return line
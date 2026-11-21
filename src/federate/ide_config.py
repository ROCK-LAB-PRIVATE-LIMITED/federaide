import json
import os
import threading
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
from textual.message import Message
from textual.widgets import TextArea

_PYGMENTS_THEME_CACHE = {}


def _get_cached_theme_styles(theme_name: str):
    cached = _PYGMENTS_THEME_CACHE.get(theme_name)
    if cached is not None:
        return cached
    from rich.syntax import Syntax
    theme = Syntax.get_theme(theme_name)
    _PYGMENTS_THEME_CACHE[theme_name] = (theme, {})
    return _PYGMENTS_THEME_CACHE[theme_name]


class PygmentsTextArea(TextArea):
    """TextArea subclass providing non-blocking Pygments syntax highlighting via rich.syntax.Syntax."""

    def __init__(self, *args, hl_path: str = "", **kwargs):
        self._hl_path = hl_path
        self._hl_spans = []
        self._hl_version = 0
        self._hl_timer = None
        self._hl_lexer = None
        self._hl_loaded_theme = ""
        self._hl_initialized = False
        super().__init__(*args, **kwargs)

    def on_mount(self) -> None:
        if not self._hl_initialized:
            self._hl_initialized = True
            self._highlight_initial()

    def load_text(self, text: str) -> None:
        super().load_text(text)
        self._hl_version += 1
        self._hl_spans = []
        self._hl_lexer = None
        self._hl_loaded_theme = ""
        self._hl_initialized = False
        if self.is_mounted:
            self._hl_initialized = True
            self._highlight_initial()

    async def _on_message(self, message: Message) -> None:
        await super()._on_message(message)
        if message.__class__.__name__ == "Changed":
            self._on_doc_changed()

    def _get_theme_name(self) -> str:
        is_dark = True
        try:
            t_obj = self.app.available_themes.get(self.app.theme)
            if t_obj is not None:
                is_dark = getattr(t_obj, "dark", True)
        except Exception:
            pass
        return "monokai" if is_dark else "friendly"

    def _build_lexer(self, hl_path: str, code_sample: str, theme_name: str):
        from rich.syntax import Syntax
        lexer_name = Syntax.guess_lexer(hl_path, code=code_sample) if hl_path else "text"
        if lexer_name == "text":
            return None
        return Syntax("", lexer_name, theme=theme_name).lexer

    def _lex_to_spans(self, lexer, code: str, theme_name: str, expected_version: int = None):
        spans_by_line = [[]]
        theme, style_cache = _get_cached_theme_styles(theme_name)
        line_idx = 0
        col = 0
        null_style = Style.null()
        for _, tok_type, val in lexer.get_tokens_unprocessed(code):
            if expected_version is not None and expected_version != self._hl_version:
                return None
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
        return spans_by_line

    def _highlight_initial(self) -> None:
        hl_path = getattr(self, "_hl_path", "") or ""
        theme_name = self._get_theme_name()
        doc = self.document
        line_count = doc.line_count

        try:
            if line_count <= 1500:
                code = self.text
                if len(code) <= 65536:
                    lexer = self._build_lexer(hl_path, code[:2048], theme_name)
                    if lexer is None:
                        self._apply_highlight_result(self._hl_version, [], None, theme_name)
                        return
                    spans = self._lex_to_spans(lexer, code, theme_name)
                    self._apply_highlight_result(self._hl_version, spans, lexer, theme_name)
                    return

            # Large file: immediately highlight top visible lines, then lex full file in background
            preview_lines = []
            preview_chars = 0
            for i in range(min(line_count, 250)):
                line_str = doc.get_line(i)
                preview_lines.append(line_str)
                preview_chars += len(line_str) + 1
                if preview_chars >= 16384:
                    break
            preview_code = "\n".join(preview_lines)
            lexer = self._build_lexer(hl_path, preview_code[:2048], theme_name)
            if lexer is None:
                self._apply_highlight_result(self._hl_version, [], None, theme_name)
                return
            preview_spans = self._lex_to_spans(lexer, preview_code, theme_name) or []
            self._apply_highlight_result(self._hl_version, preview_spans, lexer, theme_name)
        except Exception:
            pass

        self._schedule_highlight(immediate=True)

    def _lex_single_line(self, line_text: str, theme_name: str) -> list:
        if not line_text or self._hl_lexer is None or len(line_text) > 4096:
            return []
        try:
            theme, style_cache = _get_cached_theme_styles(theme_name)
            null_style = Style.null()
            spans = []
            col = 0
            for _, tok_type, val in self._hl_lexer.get_tokens_unprocessed(line_text):
                if not val:
                    continue
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
                end_col = col + len(val)
                if style != null_style:
                    spans.append((col, end_col, style))
                col = end_col
            return spans
        except Exception:
            return []

    def _on_doc_changed(self) -> None:
        self._hl_version += 1
        doc = self.document
        new_count = doc.line_count
        old_count = len(self._hl_spans)
        row = self.cursor_location[0]

        if old_count > 0:
            diff = new_count - old_count
            if 0 < diff < 500:
                insert_at = max(0, min(row, old_count))
                self._hl_spans[insert_at:insert_at] = [[] for _ in range(diff)]
            elif -500 < diff < 0:
                del_start = max(0, min(row + 1, old_count))
                del_end = min(old_count, del_start + (-diff))
                if del_end > del_start:
                    del self._hl_spans[del_start:del_end]

            if self._hl_lexer is not None:
                theme_name = self._get_theme_name()
                if diff != 0 and 0 <= row - 1 < len(self._hl_spans) and row - 1 < new_count:
                    self._hl_spans[row - 1] = self._lex_single_line(doc.get_line(row - 1), theme_name)
                if 0 <= row < len(self._hl_spans) and row < new_count:
                    self._hl_spans[row] = self._lex_single_line(doc.get_line(row), theme_name)

        self._schedule_highlight(immediate=False)

    def _schedule_highlight(self, immediate: bool = False) -> None:
        if self._hl_timer is not None:
            try:
                self._hl_timer.stop()
            except Exception:
                pass
            self._hl_timer = None

        if immediate:
            self._dispatch_highlight_worker()
        else:
            try:
                self._hl_timer = self.set_timer(0.18, self._dispatch_highlight_worker)
            except Exception:
                pass

    def _dispatch_highlight_worker(self) -> None:
        self._hl_timer = None
        if not self.is_mounted:
            return
        try:
            app = self.app
        except Exception:
            return

        version = self._hl_version
        hl_path = getattr(self, "_hl_path", "") or ""
        theme_name = self._get_theme_name()
        code = self.text

        def _worker():
            spans_by_line = []
            lexer = None
            try:
                lexer = self._build_lexer(hl_path, code[:2048], theme_name)
                if lexer is None:
                    app.call_from_thread(self._apply_highlight_result, version, [], None, theme_name)
                    return
                spans_by_line = self._lex_to_spans(lexer, code, theme_name, expected_version=version)
                if spans_by_line is None:
                    return
            except Exception:
                spans_by_line = []

            try:
                app.call_from_thread(self._apply_highlight_result, version, spans_by_line, lexer, theme_name)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_highlight_result(self, version: int, spans_by_line: list, lexer, theme_name: str) -> None:
        if version != self._hl_version:
            return
        self._hl_spans = spans_by_line
        self._hl_lexer = lexer
        self._hl_loaded_theme = theme_name
        for cache_attr in ("_line_cache", "_render_cache"):
            c = getattr(self, cache_attr, None)
            if c is not None and hasattr(c, "clear"):
                c.clear()
        if self.is_mounted:
            try:
                self.refresh_lines(self.scroll_offset.y, max(1, self.size.height + 2))
            except Exception:
                pass
            self.refresh()

    def get_line(self, line_index: int):
        line = super().get_line(line_index)
        if not self._hl_initialized:
            self._hl_initialized = True
            self._highlight_initial()

        try:
            theme_name = self._get_theme_name()
            if self._hl_loaded_theme and self._hl_loaded_theme != theme_name:
                self._hl_loaded_theme = theme_name
                self._schedule_highlight(immediate=True)
        except Exception:
            theme_name = "monokai"

        spans_by_line = self._hl_spans
        if line_index < len(spans_by_line):
            line_len = len(line)
            for start, end, style in spans_by_line[line_index]:
                if start < line_len:
                    line.stylize(style, start, min(end, line_len))
        elif self._hl_lexer is not None and len(line) <= 4096:
            line_len = len(line)
            for start, end, style in self._lex_single_line(line.plain, theme_name):
                if start < line_len:
                    line.stylize(style, start, min(end, line_len))
        return line
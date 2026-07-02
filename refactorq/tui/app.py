from __future__ import annotations

from typing import Any

from refactorq.core.tui.models import TuiReviewPayload
from refactorq.tui.widgets import (
    FilterSelection,
    filter_rows,
    render_candidate_panel,
    render_operational_panel,
    render_summary,
    select_options,
)

ComposeResult = Any

_TEXTUAL_IMPORT_ERROR: ModuleNotFoundError | None = None

try:
    from textual.app import App  # type: ignore[import-not-found]
    from textual.containers import Horizontal, Vertical  # type: ignore[import-not-found]
    from textual.widgets import DataTable, Footer, Header, Select, Static  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:
    if exc.name and exc.name.startswith("textual"):
        _TEXTUAL_IMPORT_ERROR = exc
    else:
        raise


def _require_textual() -> None:
    if _TEXTUAL_IMPORT_ERROR is not None:
        raise RuntimeError("Textual support requires the optional 'textual' package.") from _TEXTUAL_IMPORT_ERROR


if _TEXTUAL_IMPORT_ERROR is None:

    class RefactorQTuiApp(App[None]):  # type: ignore[misc]
        CSS = """
        Screen {
            layout: vertical;
        }

        #body {
            height: 1fr;
        }

        #sidebar {
            width: 36;
            min-width: 28;
        }

        #main {
            width: 1fr;
        }

        #detail {
            width: 44;
            min-width: 32;
        }

        #filters,
        #operational,
        #summary,
        #drilldown {
            height: auto;
            border: round $surface;
            padding: 0 1;
            margin-bottom: 1;
        }

        #candidate-table {
            height: 1fr;
        }

        Select {
            margin-bottom: 1;
        }
        """

        BINDINGS = [("q", "quit", "Quit")]

        def __init__(self, payload: TuiReviewPayload) -> None:
            super().__init__()
            self.payload = payload
            self.filters = FilterSelection()
            self.selected_candidate_id = payload.drilldown.candidate.id if payload.drilldown is not None else None
            self.filtered_rows = list(payload.candidate_rows)

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with Horizontal(id="body"):
                with Vertical(id="sidebar"):
                    yield Static(id="filters")
                    yield Static(id="operational")
                with Vertical(id="main"):
                    yield Static(id="summary")
                    yield DataTable(id="candidate-table")
                with Vertical(id="detail"):
                    yield Static(id="drilldown")
            yield Footer()

        def on_mount(self) -> None:
            kind_select = Select[str](select_options(self.payload.filters.kinds, all_label="All kinds"), value="all", id="kind-filter")
            language_select = Select[str](select_options(self.payload.filters.languages, all_label="All languages"), value="all", id="language-filter")
            apply_mode_select = Select[str](select_options(self.payload.filters.apply_modes, all_label="All apply modes"), value="all", id="apply-mode-filter")
            status_select = Select[str](select_options(self.payload.filters.statuses, all_label="All states"), value="all", id="status-filter")
            filters = self.query_one("#filters", Static)
            filters.mount(
                Static("[b]Filters[/b]\nRead-only review filters.", markup=True),
                kind_select,
                language_select,
                apply_mode_select,
                status_select,
            )

            table = self.query_one(DataTable)
            table.cursor_type = "row"
            table.zebra_stripes = True
            table.add_columns("id", "title", "kind", "language", "apply", "state", "confidence")
            self._refresh_view()

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.value == Select.BLANK:
                return
            value = str(event.value)
            if event.select.id == "kind-filter":
                self.filters.kind = value
            elif event.select.id == "language-filter":
                self.filters.language = value
            elif event.select.id == "apply-mode-filter":
                self.filters.apply_mode = value
            elif event.select.id == "status-filter":
                self.filters.status = value
            self._refresh_view()

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            self.selected_candidate_id = str(event.row_key.value) if event.row_key is not None else None
            self._update_drilldown()

        def _refresh_view(self) -> None:
            self.filtered_rows = filter_rows(self.payload.candidate_rows, self.filters)
            if self.selected_candidate_id not in {row.candidate_id for row in self.filtered_rows}:
                self.selected_candidate_id = self.filtered_rows[0].candidate_id if self.filtered_rows else None
            self._update_summary()
            self._update_table()
            self._update_drilldown()
            self._update_operational()

        def _update_summary(self) -> None:
            self.query_one("#summary", Static).update(render_summary(self.payload, self.filtered_rows, self.filters))

        def _update_operational(self) -> None:
            self.query_one("#operational", Static).update(render_operational_panel(self.payload))

        def _update_table(self) -> None:
            table = self.query_one(DataTable)
            table.clear(columns=False)
            for row in self.filtered_rows:
                state = "selected" if row.selected else "excluded"
                table.add_row(
                    row.candidate_id,
                    row.title,
                    row.kind,
                    row.language,
                    row.apply_mode_hint,
                    state,
                    f"{row.confidence:.2f}",
                    key=row.candidate_id,
                )
            if self.filtered_rows:
                target = self.selected_candidate_id or self.filtered_rows[0].candidate_id
                index = next((idx for idx, row in enumerate(self.filtered_rows) if row.candidate_id == target), 0)
                table.move_cursor(row=index)

        def _update_drilldown(self) -> None:
            self.query_one("#drilldown", Static).update(render_candidate_panel(self.payload, self.selected_candidate_id))

else:

    class _MissingTextualApp:
        def __init__(self, payload: TuiReviewPayload) -> None:
            _require_textual()
            self.payload = payload




def create_tui_app(payload: TuiReviewPayload) -> Any:
    _require_textual()
    return RefactorQTuiApp(payload)


def launch_tui(payload: TuiReviewPayload, **run_kwargs: Any) -> None:
    _require_textual()
    create_tui_app(payload).run(**run_kwargs)


__all__ = ["create_tui_app", "launch_tui"]

import asyncio
import copy
import threading
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, Static, TextArea

from .backend import OPTIONS, Backend, BackendError, Edit
from .demo import ROOT, DemoBackend, demo_scan
from .model import Entry, Policy, Scan, Usage, size
from .scan import scan_directory


class Report(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def __init__(self, title: str, content: str | Table | Text, raw: bool = False):
        super().__init__()
        self.title_text, self.content, self.raw = title, content, raw

    def compose(self) -> ComposeResult:
        with Vertical(id="report-dialog"):
            yield Label(self.title_text, classes="dialog-title", markup=False)
            if self.raw:
                yield TextArea(str(self.content), read_only=True, show_line_numbers=False, id="raw")
            else:
                with VerticalScroll():
                    yield Static(self.content, markup=False)
            yield Button("Close  [Esc]", id="close")

    @on(Button.Pressed, "#close")
    def close(self) -> None:
        self.dismiss()


class PolicyEditor(ModalScreen[Edit | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, backend: Backend | DemoBackend, path: Path, policy: Policy):
        super().__init__()
        self.backend, self.path, self.policy = backend, path, policy
        self.edit: Edit | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Label("EDIT STORAGE POLICY", classes="dialog-title")
            yield Static(Text(str(self.path)), id="edit-path")
            yield Select([(key.replace("_", " "), key) for key in OPTIONS],
                         value="compression", allow_blank=False, id="option")
            yield Static("", id="option-help")
            yield Input(placeholder="Value, or - to restore inheritance", id="value")
            yield Static("", id="preview")
            with Horizontal(classes="buttons"):
                yield Button("Preview", variant="primary", id="preview-button")
                yield Button("Apply change", variant="warning", id="apply", disabled=True)
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.update_help()

    @on(Select.Changed, "#option")
    def option_changed(self) -> None:
        self.update_help()

    def update_help(self) -> None:
        option = str(self.query_one("#option", Select).value)
        effective = self.policy.effective.get(option, "unknown")
        source = ("explicit" if option in self.policy.explicit else "filesystem default"
                  if option in self.policy.from_filesystem else "inherited / default")
        self.query_one("#option-help", Static).update(Text(
            f"{OPTIONS.get(option, '')}\nCurrent: {effective} ({source}). Use - to inherit."))
        self.invalidate()

    @on(Input.Changed, "#value")
    def invalidate(self) -> None:
        self.edit = None
        self.query_one("#apply", Button).disabled = True
        self.query_one("#preview", Static).update("")

    @on(Input.Submitted, "#value")
    @on(Button.Pressed, "#preview-button")
    def preview(self) -> None:
        self.query_one("#preview", Static).update("Checking supported options…")
        self.prepare_change(str(self.query_one("#option", Select).value),
                            self.query_one("#value", Input).value)

    @work(exclusive=True, group="prepare")
    async def prepare_change(self, option: str, value: str) -> None:
        try:
            edit = await asyncio.to_thread(self.backend.prepare, self.path, option, value)
            self.show_preview(edit, "")
        except (BackendError, OSError) as exc:
            self.show_preview(None, str(exc))

    def show_preview(self, edit: Edit | None, error: str) -> None:
        if not self.is_mounted:
            return
        if edit and (str(self.query_one("#option", Select).value) != edit.option or
                     self.query_one("#value", Input).value.strip() != edit.value):
            return
        self.edit = edit
        self.query_one("#apply", Button).disabled = edit is None
        if edit:
            scope = ("DIRECTORY + EXISTING DESCENDANTS: bcachefs recursively propagates "
                     "inheritance; explicit descendant overrides remain. Future children inherit."
                     if edit.directory else "SELECTED FILE: all hard links share this inode policy.")
            error = (f"{edit.option}: {edit.previous or '(inherited)'} → "
                     f"{'inherit' if edit.value == '-' else edit.value}\n\n{scope}\n\n"
                     "Existing data may be rewritten by background reconciliation. This is not "
                     "an immediate conversion or a guarantee of achieved durability.")
        self.query_one("#preview", Static).update(Text(error))

    @on(Button.Pressed, "#apply")
    def apply_change(self) -> None:
        if self.edit:
            self.dismiss(self.edit)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class DirectoryTable(DataTable):
    BINDINGS = [Binding("right,l", "enter", "Open", show=False),
                Binding("left,h,backspace", "parent", "Up", show=False)]

    def action_enter(self) -> None:
        self.app.action_enter()

    def action_parent(self) -> None:
        self.app.action_parent()


class ScanUpdate(Message):
    def __init__(self, result: Scan, generation: int):
        super().__init__()
        self.result, self.generation = result, generation


class BcduApp(App):
    TITLE = "bcdu"
    CSS_PATH = "style.tcss"
    BINDINGS = [Binding("q", "quit", "Quit"), Binding("f", "filesystem", "Filesystem"),
                Binding("e", "edit", "Edit policy"), Binding("x", "extents", "Extents"),
                Binding("i", "details", "Details"), Binding("slash", "search", "Filter"),
                Binding("u", "raw_usage", "Raw report", show=False),
                Binding("s", "sort", "Sort"), Binding("r", "refresh", "Refresh"),
                Binding("question_mark", "help", "Help"),
                Binding("escape", "clear_filter", show=False),
                Binding("ctrl+c", "quit", show=False, priority=True)]

    def __init__(self, path: Path, *, demo: bool = False, read_only: bool = False,
                 binary: str | None = None):
        super().__init__()
        self.demo = demo
        self.path = ROOT if demo else path
        self.root = self.path
        self.backend = DemoBackend(read_only) if demo else Backend(path, binary, read_only)
        self.scan = Scan(self.path)
        self.usage = Usage()
        self.rows: list[Entry] = []
        self.policies: dict[Path, Policy] = {}
        self.stop_scan = threading.Event()
        self.generation = 0
        self.sort_key = "logical"
        self.editing = False
        self.wanted_selection: Path | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="brand"):
            yield Static("◈  bcdu", id="logo")
            yield Static("B C A C H E F S   S T O R A G E   E X P L O R E R", id="tagline")
            yield Static("DEMO" if self.demo else "READ ONLY" if self.backend.read_only else "LIVE",
                         id="mode")
        yield Static("", id="path", markup=False)
        with Horizontal(id="metrics"):
            yield Static("Loading…", id="metric-used", classes="metric", markup=False)
            yield Static("Loading…", id="metric-compression", classes="metric", markup=False)
            yield Static("Loading…", id="metric-health", classes="metric", markup=False)
        with Horizontal(id="main"):
            with Vertical(id="browser"):
                yield Static("  DIRECTORY CONTENTS", id="browser-heading")
                yield Input(placeholder="Filter this directory…  Esc clears", id="filter")
                yield DirectoryTable(id="files", cursor_type="row", zebra_stripes=True,
                                     show_row_labels=False)
            with VerticalScroll(id="details"):
                yield Static("", id="details-body", markup=False)
        yield Static("", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "textual-dark"
        table = self.query_one("#files", DataTable)
        for label, key, width in [("Name", "name", None), ("Logical ↓", "logical", 12),
                                   ("Allocated", "allocated", 12), ("Share", "share", 14),
                                   ("Files", "files", 9)]:
            table.add_column(label, key=key, width=width)
        self.query_one("#filter").display = False
        table.focus()
        self.start_scan()
        self.load_usage()

    def on_resize(self) -> None:
        if self.is_mounted:
            self.query_one("#details").display = self.size.width >= 110
            self.query_one("#tagline").display = self.size.width >= 85

    def on_unmount(self) -> None:
        self.stop_scan.set()

    def start_scan(self) -> None:
        self.stop_scan.set()
        self.stop_scan = threading.Event()
        self.generation += 1
        self.scan = Scan(self.path)
        self.query_one("#path", Static).update(Text(f"  {self.path}  /"))
        self.query_one("#files", DataTable).clear()
        self.rows = []
        self.query_one("#status", Static).update("  Scanning…  ↑↓ select  → open  ← parent")
        self.read_directory(self.path, self.generation, self.stop_scan)

    @work(group="scan")
    async def read_directory(self, path: Path, generation: int, stop: threading.Event) -> None:
        def report(result: Scan) -> None:
            if not stop.is_set():
                self.post_message(ScanUpdate(copy.deepcopy(result), generation))
        try:
            if self.demo:
                report(demo_scan(path))
            else:
                await asyncio.to_thread(scan_directory, path, stop, report)
        except OSError as exc:
            self.scan_error(str(exc), generation)

    def on_scan_update(self, message: ScanUpdate) -> None:
        self.receive_scan(message.result, message.generation)

    def scan_error(self, error: str, generation: int) -> None:
        if generation == self.generation:
            self.query_one("#status", Static).update(Text(error))
            self.notify(error, severity="error", timeout=8)

    def receive_scan(self, scan: Scan, generation: int) -> None:
        if generation != self.generation:
            return
        self.scan = scan
        self.render_rows()
        status = "Complete" if scan.complete else "Scanning… partial totals"
        self.query_one("#status", Static).update(Text(
            f"  {status} · {scan.files:,} files · {size(scan.logical)} logical · "
            f"{size(scan.allocated)} allocated · {scan.errors:,} read errors   ↑↓ select  → open  ← parent"))

    def selected(self) -> Entry | None:
        index = self.query_one("#files", DataTable).cursor_row
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def render_rows(self) -> None:
        table = self.query_one("#files", DataTable)
        selected = self.selected()
        previous = self.wanted_selection or (selected.path if selected else None)
        needle = self.query_one("#filter", Input).value.casefold()
        rows = [e for e in self.scan.entries if needle in e.path.name.casefold()]
        rows.sort(key=(lambda e: e.path.name.casefold()) if self.sort_key == "name" else
                  (lambda e: (-getattr(e, self.sort_key), e.path.name.casefold())))
        table.clear()
        self.rows = rows
        total = self.scan.allocated if self.sort_key == "allocated" else self.scan.logical
        for index, entry in enumerate(rows):
            number = entry.allocated if self.sort_key == "allocated" else entry.logical
            fraction = number / total if total else 0
            blocks = round(fraction * 8)
            share = Text("━" * blocks + "─" * (8 - blocks), style="#51d6c1")
            share.append(f" {fraction:3.0%}", style="#90a6ba")
            icon = "▸" if entry.kind == "dir" else "↗" if entry.kind == "link" else "·"
            name = Text(f"{icon} {entry.path.name}", style="#75e4d0" if entry.kind == "dir" else "#dce6f0")
            if not entry.complete:
                name.append(" …", style="#f0ba6b")
            if entry.errors:
                name.append(" !", style="#f0ba6b")
            table.add_row(name, size(entry.logical), size(entry.allocated), share,
                          f"{entry.files:,}", key=str(index))
        matching = next((i for i, e in enumerate(rows) if e.path == previous), 0)
        table.move_cursor(row=matching)
        if previous and any(e.path == previous for e in rows):
            self.wanted_selection = None
        self.show_details()

    @on(DataTable.RowHighlighted, "#files")
    def highlight(self) -> None:
        self.show_details()

    @on(DataTable.RowSelected, "#files")
    def open_selected(self) -> None:
        self.action_enter()

    def show_details(self) -> None:
        entry = self.selected()
        self.query_one("#details-body", Static).update(self.detail_text(entry))
        if entry and entry.path not in self.policies:
            self.load_policy(entry.path)

    @work(exclusive=True, group="policy")
    async def load_policy(self, path: Path) -> None:
        policy = await asyncio.to_thread(self.backend.policy, path)
        self.receive_policy(path, policy)

    def receive_policy(self, path: Path, policy: Policy) -> None:
        self.policies[path] = policy
        if self.selected() and self.selected().path == path:
            self.query_one("#details-body", Static).update(self.detail_text(self.selected()))

    def detail_text(self, entry: Entry | None) -> Text:
        text = Text()
        text.append("SELECTED ENTRY\n\n", style="bold #51d6c1")
        if not entry:
            text.append("No matching entries.\n\n← parent · / filter · r refresh")
            return text
        text.append(entry.path.name + "\n", style="bold #e7edf5")
        text.append(f"{entry.kind.upper()}  ·  {entry.files:,} files\n\n", style="#90a6ba")
        text.append(f"Logical data     {size(entry.logical)}\nAllocated blocks {size(entry.allocated)}\n")
        text.append("\nAllocated is st_blocks × 512. It does not include the true cost of "
                    "compression, replicas, or EC parity. Shared data can be counted again.\n\n", style="#90a6ba")
        text.append("EFFECTIVE POLICY\n", style="bold #51d6c1")
        policy = self.policies.get(entry.path)
        if policy is None:
            text.append("Reading attributes…\n")
        elif not policy.effective:
            text.append("Unavailable on this filesystem or kernel.\n")
        else:
            for option in OPTIONS:
                label = option.replace("_", " ")
                value = policy.effective.get(option, "—")
                source = ("explicit" if option in policy.explicit else "filesystem"
                          if option in policy.from_filesystem else "inherited")
                text.append(f"\n{label}\n", style="#90a6ba")
                text.append(f"{value}  ", style="bold #e7edf5")
                text.append(source, style="#f0ba6b" if source == "explicit" else "#697f94")
            text.append("\n\nPolicy is the desired state; existing extents may differ. "
                        "A directory can contain mixed child policies.\n", style="#90a6ba")
        if policy and policy.errors:
            text.append("\n" + "; ".join(policy.errors), style="#f0ba6b")
        if entry.note or entry.errors:
            text.append(f"\n{entry.note}  Read errors: {entry.errors}\n", style="#f0ba6b")
        text.append("\ne edit policy  ·  x inspect file extents\nf filesystem  ·  i expand details", style="#51d6c1")
        return text

    @work(exclusive=True, group="usage")
    async def load_usage(self) -> None:
        self.receive_usage(await asyncio.to_thread(self.backend.usage))

    def receive_usage(self, usage: Usage) -> None:
        self.usage = usage
        percent = f"{usage.used / usage.capacity:.0%}" if usage.used is not None and usage.capacity else "—"
        self.query_one("#metric-used", Static).update(Text(
            f"PHYSICAL FILESYSTEM USAGE\n{size(usage.used)} / {size(usage.capacity)}  ·  {percent}", style="#75e4d0"))
        compressed = sum(c.compressed for c in usage.compression)
        uncompressed = sum(c.uncompressed for c in usage.compression)
        ratio = f"{uncompressed/compressed:.2f}×" if compressed else "—"
        self.query_one("#metric-compression", Static).update(Text(
            f"ACCOUNTED COMPRESSION\n{ratio}  ·  {size(uncompressed) if compressed else 'Unavailable'} uncompressed",
            style="#c9b1ff"))
        offline = sum(d.state in ("offline", "failed") for d in usage.devices)
        health = (f"{size(usage.degraded)} degraded · {offline} offline/failed" if usage.degraded is not None
                  else "Degraded accounting unavailable")
        if usage.error:
            health = "Accounting unavailable / warning · press f"
        self.query_one("#metric-health", Static).update(Text(
            "DURABILITY & DEVICES\n" + health, style="#f0ba6b" if offline or usage.degraded or usage.error else "#75e4d0"))

    def action_enter(self) -> None:
        entry = self.selected()
        if entry and entry.kind == "dir":
            self.path = entry.path
            self.query_one("#filter", Input).value = ""
            self.start_scan()
        elif entry:
            self.action_details()

    def action_parent(self) -> None:
        if self.path == self.root:
            self.notify("At the browsing root. Start bcdu with a parent path to browse higher.")
            return
        self.wanted_selection = self.path
        self.path = self.path.parent
        self.query_one("#filter", Input).value = ""
        self.start_scan()

    def action_refresh(self) -> None:
        self.policies.clear()
        self.start_scan()
        self.load_usage()

    def action_sort(self) -> None:
        order = ["logical", "allocated", "name"]
        self.sort_key = order[(order.index(self.sort_key) + 1) % len(order)]
        table = self.query_one("#files", DataTable)
        for key in order:
            label = key.title() + (" ↑" if key == "name" else " ↓") if key == self.sort_key else key.title()
            table.columns[next(k for k in table.columns if k.value == key)].label = Text(label)
        self.render_rows()

    def action_search(self) -> None:
        self.query_one("#filter").display = True
        self.query_one("#filter", Input).focus()

    @on(Input.Changed, "#filter")
    def filter_changed(self) -> None:
        self.render_rows()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self) -> None:
        self.query_one("#files", DataTable).focus()

    def action_clear_filter(self) -> None:
        field = self.query_one("#filter", Input)
        field.value = ""
        field.display = False
        self.query_one("#files", DataTable).focus()

    def action_details(self) -> None:
        self.push_screen(Report("ENTRY DETAILS", self.detail_text(self.selected())))

    def action_filesystem(self) -> None:
        usage = self.usage
        report = Text()
        report.append("PHYSICAL ACCOUNTING\n", style="bold #51d6c1")
        report.append(f"Mount: {self.backend.mount.path}\nSize: {size(usage.capacity)}    "
                      f"Used: {size(usage.used)}    Free: {size(usage.free)}\n"
                      f"Online reserved: {size(usage.reserved)}\n")
        if usage.error:
            report.append("\n" + usage.error + "\n", style="#f0ba6b")
        report.append("\nCOMPRESSION  ·  accounted stored / uncompressed bytes\n", style="bold #c9b1ff")
        for c in usage.compression:
            ratio = f"{c.ratio:.2f}×" if c.ratio is not None else "—"
            report.append(f"{c.algorithm:16} {size(c.compressed):>12} / {size(c.uncompressed):>12}  {ratio}\n")
        if not usage.compression:
            report.append("Compression counters unavailable.\n")
        report.append("\nDATA LAYOUT  ·  actual accounting, not requested policy\n", style="bold #51d6c1")
        for row in usage.layouts:
            state = f"−{row.missing} durability units" if row.missing else "undegraded"
            label = "EC data+parity" if row.kind == "ec" else "Replicated"
            report.append(f"{label:15} {row.geometry:6} {size(row.bytes):>12}  {state}\n",
                          style="#f0ba6b" if row.missing else "")
        if not usage.layouts:
            report.append("Layout/degraded counters unavailable in this tools version.\n")
        report.append("\nDEVICES\n", style="bold #51d6c1")
        for device in usage.devices:
            fraction = device.used / device.capacity if device.used is not None and device.capacity else 0
            bar = "━" * round(fraction * 12) + "─" * (12 - round(fraction * 12))
            report.append(f"{device.label:22} {device.state:8} {bar}  "
                          f"{size(device.used)} / {size(device.capacity)}\n",
                          style="#f0ba6b" if device.state in ("offline", "failed") else "#dce6f0")
        report.append("\nCounters are a point-in-time report, not a scrub. Missing data is shown as —.\n"
                      "Physical pool usage includes overhead; pathname totals include shared data again.\n"
                      "Device durability weights can differ from the number of copies.\n", style="#90a6ba")
        self.push_screen(Report("FILESYSTEM OVERVIEW", report))

    def action_raw_usage(self) -> None:
        self.push_screen(Report("RAW BCACHEFS USAGE REPORT", self.usage.raw or self.usage.error or
                                "No report available yet.", raw=True))

    def action_edit(self) -> None:
        if self.editing:
            self.notify("A policy command is already running.")
            return
        if self.backend.read_only:
            self.notify("Read-only mode: editing disabled.", severity="warning")
            return
        entry = self.selected()
        if entry:
            self.push_screen(PolicyEditor(self.backend, entry.path,
                                          self.policies.get(entry.path, Policy())), self.confirm_edit)

    def confirm_edit(self, edit: Edit | None) -> None:
        if edit:
            self.editing = True
            self.query_one("#status", Static).update("  Applying policy; keep bcdu open until it finishes…")
            self.apply_edit(edit)

    @work(group="edit")
    async def apply_edit(self, edit: Edit) -> None:
        try:
            result = await asyncio.to_thread(self.backend.apply, edit)
        except (BackendError, OSError) as exc:
            result = f"Policy command failed or was incomplete: {exc}\nRefresh before retrying."
        self.edit_finished(result)

    def edit_finished(self, result: str) -> None:
        self.editing = False
        self.policies.clear()
        self.show_details()
        self.query_one("#status", Static).update("  Policy command finished · r refresh")
        self.push_screen(Report("POLICY RESULT", Text(result)))

    def action_extents(self) -> None:
        entry = self.selected()
        if entry:
            if entry.kind != "file":
                self.notify("Select a regular file to inspect its actual extents.")
                return
            self.notify("Reading selected inode and snapshot…")
            self.load_extents(entry.path)

    @work(exclusive=True, group="extents")
    async def load_extents(self, path: Path) -> None:
        try:
            report = await asyncio.to_thread(self.backend.inspect_extents, path)
        except (BackendError, OSError) as exc:
            report = str(exc)
        self.push_screen(Report(f"EXTENTS · {path.name}", report, raw=True))

    def action_help(self) -> None:
        self.push_screen(Report("BCDU · KEYBOARD & ACCOUNTING", Text(
            "↑ / ↓        Select a row\n→ / Enter    Open a directory; expand file details\n"
            "← / Backspace  Parent directory (within the starting root)\n"
            "/            Filter the current directory; Enter returns to rows\n"
            "s            Sort: logical bytes → allocated blocks → name\n"
            "i            Expand selected entry details (also useful in narrow terminals)\n"
            "f            Filesystem: physical space, compression, EC, degradation, devices\n"
            "u            Raw bcachefs usage report\n"
            "x            Query actual extents of the selected file, if supported\n"
            "e            Edit policy, preview scope, then explicitly apply\n"
            "r            Rescan directory and refresh filesystem counters\n"
            "Esc          Close dialog / clear filter\nq            Quit\n\n"
            "Logical = sum of regular-file lengths. Allocated = st_blocks × 512.\n"
            "Neither measures compressed, replicated, EC-inclusive physical usage.\n"
            "Hard links, reflinks, and snapshots are counted per pathname. Symlinks and\n"
            "other mounts are skipped. Directory metadata is excluded. Read errors\n"
            "mean incomplete totals. Scanning a changing tree is not an atomic snapshot.\n\n"
            "The filesystem report supplies actual pool accounting. Exact exclusive\n"
            "physical bytes and compression ratios per directory are unavailable.\n"
            "Effective policy can differ from existing extents. Directory edits use\n"
            "bcachefs set-file-option propagation and may schedule data rewrites.\n"
            "No automatic sudo. --read-only disables all policy commands.\n")))

    def action_quit(self) -> None:
        if self.editing:
            self.notify("A policy command is still running; wait for its result.", severity="warning")
        else:
            self.stop_scan.set()
            self.exit()

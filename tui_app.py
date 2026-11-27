from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import replace
from typing import Dict, List, Optional

from loguru import logger
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from config import (
    AppConfig,
    load_config,
    parse_and_validate_yaml,
    render_commented_yaml,
    save_config,
)
from exporter import export_questions, load_history
from chaoxing_client import ChaoxingClient, ExportCancelledError

FORMAT_LABELS = [
    ("TXT 文本 (.txt)", "txt"),
    ("Markdown (.md)", "md"),
    ("Word 文档 (.docx)", "docx"),
    ("PDF 文档 (.pdf)", "pdf"),
    ("JSON 数据 (.json)", "json"),
]


def _open_local_path(path: str) -> None:
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"路径不存在: {path}")
    abs_path = os.path.abspath(path)
    if hasattr(os, "startfile"):
        os.startfile(abs_path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", abs_path])
    else:
        subprocess.Popen(["xdg-open", abs_path])


class LoginModal(ModalScreen[Optional[tuple]]):
    """启动登录弹窗：每次启动手动输入手机号和密码，不在本地保存任何凭证。"""

    BINDINGS = [Binding("escape", "quit_app", "退出")]

    CSS = """
    LoginModal {
        align: center middle;
    }
    #login-box {
        width: 62;
        max-width: 92%;
        height: auto;
        max-height: 95%;
        overflow-y: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #login-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #login-tip {
        color: $text-muted;
        margin-bottom: 1;
    }
    .login-label {
        margin-top: 1;
    }
    #login-status {
        margin-top: 1;
        color: $warning;
        min-height: 1;
    }
    #login-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    #login-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, cfg: AppConfig):
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        with Container(id="login-box"):
            yield Static("Chaoxing-QuizKit · 登录", id="login-title")
            yield Static("出于安全考虑，程序不会保存密码或 Cookie，每次启动需手动登录。", id="login-tip")
            yield Label("手机号", classes="login-label")
            yield Input(placeholder="请输入学习通绑定的手机号", id="login-user")
            yield Label("密码", classes="login-label")
            yield Input(placeholder="请输入登录密码", password=True, id="login-pass")
            yield Static("", id="login-status")
            with Horizontal(id="login-buttons"):
                yield Button("退出", id="btn-login-exit")
                yield Button("登录并加载课程", variant="primary", id="btn-login-submit")

    def on_mount(self) -> None:
        self.query_one("#login-user", Input).focus()

    @on(Input.Submitted, "#login-user")
    def _focus_password(self) -> None:
        self.query_one("#login-pass", Input).focus()

    @on(Input.Submitted, "#login-pass")
    def _submit_from_pass(self) -> None:
        self._trigger_login()

    @on(Button.Pressed, "#btn-login-exit")
    def action_quit_app(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-login-submit")
    def _on_submit_click(self) -> None:
        self._trigger_login()

    def _trigger_login(self) -> None:
        username = self.query_one("#login-user", Input).value.strip()
        password = self.query_one("#login-pass", Input).value
        status = self.query_one("#login-status", Static)
        if not username or not password:
            status.update("请输入手机号和密码")
            return
        btn = self.query_one("#btn-login-submit", Button)
        btn.disabled = True
        status.update("正在登录并获取课程列表，请稍候...")
        self._do_login(username, password)

    @work(thread=True)
    def _do_login(self, username: str, password: str) -> None:
        try:
            client = ChaoxingClient(timeout=self.cfg.request_timeout, proxy=self.cfg.proxy)
            client.login(username, password)
            courses = client.get_all_courses()

            def _on_ok():
                if not self.is_mounted:
                    return
                self.dismiss((client, courses))

            self.app.call_from_thread(_on_ok)
        except Exception as exc:
            err_msg = str(exc)

            def _on_err(msg: str = err_msg):
                if not self.is_mounted:
                    return
                self.query_one("#btn-login-submit", Button).disabled = False
                self.query_one("#login-status", Static).update(f"登录失败: {msg}")

            self.app.call_from_thread(_on_err)


class ExportScopeModal(ModalScreen[Optional[dict]]):
    """导出前题库预览与范围/格式设置弹窗。"""

    BINDINGS = [
        Binding("escape", "cancel_modal", "取消"),
        Binding("ctrl+s", "confirm_export", "确认导出"),
    ]

    CSS = """
    ExportScopeModal {
        align: center middle;
    }
    #scope-box {
        width: 92%;
        max-width: 96;
        height: 92%;
        layout: vertical;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #scope-title {
        text-style: bold;
        color: $accent;
        height: auto;
        margin-bottom: 1;
    }
    #scope-summary {
        border: round $panel;
        padding: 0 1;
        height: auto;
        margin-bottom: 1;
        min-height: 3;
    }
    #scope-scroll {
        height: 1fr;
    }
    .sec-title {
        text-style: bold;
        margin-top: 1;
        color: $primary;
    }
    #modal-range-list {
        height: 8;
        border: round $panel;
        margin-top: 1;
    }
    #modal-range-actions {
        height: auto;
        margin-top: 1;
    }
    #modal-range-actions Button {
        margin-right: 1;
    }
    #modal-formats {
        height: 7;
        border: round $panel;
        margin-top: 1;
    }
    #modal-toggles {
        height: auto;
        margin-top: 1;
    }
    #modal-img-hint {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }
    #modal-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    #modal-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, client: ChaoxingClient, course: dict, cfg: AppConfig):
        super().__init__()
        self.client = client
        self.course = course
        self.cfg = cfg
        self.preview_data: Optional[dict] = None

    def compose(self) -> ComposeResult:
        with Container(id="scope-box"):
            yield Static(
                f"题库预览与导出配置 · {self.course['name']} (ID: {self.course['course_id']})",
                id="scope-title",
            )
            yield Static("正在加载自测题库统计与章节目录...", id="scope-summary")
            with VerticalScroll(id="scope-scroll"):
                yield Label("导出范围模式", classes="sec-title")
                yield Select(
                    [
                        ("全部题目（推荐）", "all"),
                        ("按章节勾选", "chapters"),
                        ("按题型勾选", "types"),
                        ("按难度勾选", "easy"),
                    ],
                    value=self.cfg.default_range_mode,
                    id="modal-range-mode",
                )
                with Horizontal(id="modal-range-actions"):
                    yield Button("全选当前列表", id="btn-range-all", compact=True)
                    yield Button("清空当前列表", id="btn-range-none", compact=True)
                yield SelectionList(id="modal-range-list")
                yield Label("本次导出格式（已预填全局配置）", classes="sec-title")
                yield SelectionList(
                    *[(label, val, val in self.cfg.export_formats) for label, val in FORMAT_LABELS],
                    id="modal-formats",
                )
                with Vertical(id="modal-toggles"):
                    yield Checkbox("同时生成无答案刷题版", value=self.cfg.export_no_answer_edition, id="modal-no-answer")
                    yield Checkbox("下载图片到本地并内嵌文档", value=self.cfg.download_images, id="modal-download-img")
                    yield Static(
                        "  · 勾选：下载图片到课程目录 images/，Word/PDF 直接显示图片、Markdown 离线可读（图片多时导出稍慢）\n"
                        "  · 不勾选：跳过下载、导出更快，但 Word/PDF 仅显示图片链接，Markdown 需联网加载图片",
                        id="modal-img-hint",
                    )
                    yield Checkbox("按分组拆分文件", value=self.cfg.bulk_files, id="modal-bulk-files")
            with Horizontal(id="modal-buttons"):
                yield Button("取消 (Esc)", id="btn-scope-cancel")
                yield Button("确认开始导出 (Ctrl+S)", variant="primary", id="btn-scope-start", disabled=True)

    def on_mount(self) -> None:
        self._refresh_range_list()
        self._load_preview()

    @work(thread=True)
    def _load_preview(self) -> None:
        try:
            data = self.client.preview_course(self.course)
            self.app.call_from_thread(self._apply_preview, data, None)
        except Exception as exc:
            self.app.call_from_thread(self._apply_preview, None, str(exc))

    def _apply_preview(self, data: Optional[dict], err: Optional[str]) -> None:
        if not self.is_mounted:
            return
        summary = self.query_one("#scope-summary", Static)
        start_btn = self.query_one("#btn-scope-start", Button)
        if err:
            summary.update(f"[red]加载题库预览失败: {err}[/red]")
            return
        self.preview_data = data
        caps = data.get("capabilities") or {}
        total = data.get("total") or 0
        if total <= 0:
            summary.update("[yellow]该课程未开通自测或自测题库为 0 题，无法导出。[/yellow]")
            start_btn.disabled = True
            return

        types_desc = "、".join(f"{it['name']}:{it['count']}" for it in data["types"]) or "无"
        easy_desc = "、".join(f"{it['name']}:{it['count']}" for it in data["easy"]) or "无"
        summary.update(
            f"自测题库总题量：[bold green]{total}[/bold green] 题 | 章节数：{len(data['chapters'])} | 文件夹数：{len(data['dirs'])}\n"
            f"题型分布：{types_desc}\n"
            f"难度分布：{easy_desc}"
        )
        mode_opts = [("全部题目（推荐）", "all")]
        if caps.get("chapters", bool(data.get("chapters"))):
            mode_opts.append(("按章节勾选", "chapters"))
        if caps.get("types", bool(data.get("types"))):
            mode_opts.append(("按题型勾选", "types"))
        if caps.get("easy", bool(data.get("easy"))):
            mode_opts.append(("按难度勾选", "easy"))

        mode_sel = self.query_one("#modal-range-mode", Select)
        mode_sel.set_options(mode_opts)
        valid_modes = {val for _, val in mode_opts}
        mode_sel.value = self.cfg.default_range_mode if self.cfg.default_range_mode in valid_modes else "all"

        start_btn.disabled = False
        self._refresh_range_list()

    @on(Select.Changed, "#modal-range-mode")
    def _on_mode_changed(self) -> None:
        self._refresh_range_list()

    def _refresh_range_list(self) -> None:
        mode = str(self.query_one("#modal-range-mode", Select).value)
        sel_list = self.query_one("#modal-range-list", SelectionList)
        actions = self.query_one("#modal-range-actions", Horizontal)
        sel_list.clear_options()

        if mode == "all" or not self.preview_data:
            sel_list.display = False
            actions.display = False
            return

        sel_list.display = True
        actions.display = True
        if mode == "chapters":
            opts = [(f"{c['name']} ({c['count']} 题)", c["name"], True) for c in self.preview_data["chapters"]]
            sel_list.add_options(opts)
        elif mode == "types":
            opts = [(f"{t['name']} ({t['count']} 题)", t["name"], True) for t in self.preview_data["types"]]
            sel_list.add_options(opts)
        elif mode == "easy":
            opts = [(f"{e['name']} ({e['count']} 题)", e["name"], True) for e in self.preview_data["easy"]]
            sel_list.add_options(opts)

    @on(Button.Pressed, "#btn-range-all")
    def _select_all_range(self) -> None:
        self.query_one("#modal-range-list", SelectionList).select_all()

    @on(Button.Pressed, "#btn-range-none")
    def _deselect_all_range(self) -> None:
        self.query_one("#modal-range-list", SelectionList).deselect_all()

    @on(Button.Pressed, "#btn-scope-cancel")
    def action_cancel_modal(self) -> None:
        self.dismiss(None)

    def action_confirm_export(self) -> None:
        btn = self.query_one("#btn-scope-start", Button)
        if not btn.disabled:
            self._confirm_export()

    @on(Button.Pressed, "#btn-scope-start")
    def _confirm_export(self) -> None:
        formats = list(self.query_one("#modal-formats", SelectionList).selected)
        if not formats:
            self.app.notify("请至少选择一种导出格式", severity="warning")
            return

        mode = str(self.query_one("#modal-range-mode", Select).value)
        scope: dict = {"mode": mode}
        scope_summary = "全部题目"

        if self.preview_data and mode != "all":
            chosen = list(self.query_one("#modal-range-list", SelectionList).selected)
            if not chosen:
                self.app.notify("当前范围列表未勾选任何项，请至少勾选一项", severity="warning")
                return
            if mode == "chapters":
                scope["chapters"] = chosen
                scope_summary = f"章节({len(chosen)}个): " + ", ".join(chosen[:3]) + ("..." if len(chosen) > 3 else "")
            elif mode == "types":
                type_map = {it["name"]: it for it in self.preview_data["types"]}
                scope["types"] = [type_map[name] for name in chosen if name in type_map]
                scope_summary = "题型: " + ", ".join(chosen)
            elif mode == "easy":
                easy_map = {it["name"]: it for it in self.preview_data["easy"]}
                scope["easy"] = [easy_map[name] for name in chosen if name in easy_map]
                scope_summary = "难度: " + ", ".join(chosen)

        run_cfg = replace(
            self.cfg,
            export_formats=formats,
            export_no_answer_edition=self.query_one("#modal-no-answer", Checkbox).value,
            download_images=self.query_one("#modal-download-img", Checkbox).value,
            bulk_files=self.query_one("#modal-bulk-files", Checkbox).value,
        )
        self.dismiss({
            "scope": scope,
            "scope_summary": scope_summary,
            "run_cfg": run_cfg,
            "preview_data": self.preview_data,
        })


class QuizKitApp(App):
    """学习通自测题库导出助手 TUI 主应用。"""

    TITLE = "Chaoxing-QuizKit"
    SUB_TITLE = "学习通题库导出工具箱 · TUI 配置版"

    BINDINGS = [
        Binding("ctrl+t", "toggle_theme", "切换主题"),
        Binding("ctrl+r", "refresh_all", "刷新数据"),
        Binding("ctrl+q", "quit", "退出"),
    ]

    CSS = """
    Screen {
        background: $background;
    }
    .toolbar {
        height: auto;
        margin: 0 0 1 0;
        align-vertical: middle;
    }
    .toolbar Input {
        width: 32;
        margin-right: 1;
    }
    .toolbar Select {
        width: 20;
        margin-right: 1;
    }
    .toolbar Checkbox {
        margin-right: 1;
    }
    .toolbar Button {
        margin-right: 1;
    }
    #courses-table, #history-table {
        height: 1fr;
        border: round $panel;
    }
    #task-summary-box {
        border: round $panel;
        padding: 1;
        margin-bottom: 1;
        height: auto;
    }
    #task-progress {
        margin: 1 0;
    }
    #task-log {
        height: 1fr;
        border: round $panel;
    }
    .cfg-section-title {
        text-style: bold;
        color: $accent;
        margin-top: 1;
        margin-bottom: 0;
    }
    .cfg-row {
        height: auto;
        margin-top: 1;
        align-vertical: middle;
    }
    .cfg-label {
        width: 28;
    }
    .cfg-input {
        width: 44;
    }
    #quick-formats, #full-formats {
        height: 7;
        width: 44;
        border: round $panel;
    }
    #yaml-editor {
        height: 1fr;
        border: round $panel;
    }
    .cfg-actions {
        height: auto;
        margin-top: 1;
    }
    .cfg-actions Button {
        margin-right: 1;
    }
    """

    def __init__(
        self,
        config_path: str = "config.yml",
        initial_client: Optional[ChaoxingClient] = None,
        initial_courses: Optional[List[dict]] = None,
        skip_login: bool = False,
    ):
        super().__init__()
        logger.remove()
        self.config_path = config_path
        self.cfg: AppConfig = load_config(config_path)
        self.client: Optional[ChaoxingClient] = initial_client
        self.courses: List[dict] = initial_courses or []
        self.skip_login = skip_login
        self._course_map: Dict[str, dict] = {}
        self._history_records: List[dict] = []
        self._stop_event: Optional[threading.Event] = None
        self._task_running = False
        self._courses_busy = False
        self._last_output_dir = os.path.abspath(self.cfg.save_path)
        self.theme = "textual-light" if self.cfg.theme == "light" else "textual-dark"

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="main-tabs"):
            with TabPane("课程列表", id="tab-courses"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="搜索课程名称或ID...", id="course-search")
                    yield Checkbox("仅看有题课程", value=False, id="only-nonempty-check")
                with Horizontal(classes="toolbar"):
                    yield Button("扫描自测题量", id="btn-scan-counts")
                    yield Button("预览并导出", variant="primary", id="btn-open-preview")
                    yield Button("刷新课程", id="btn-refresh-courses")
                yield DataTable(id="courses-table", cursor_type="row")

            with TabPane("导出任务", id="tab-tasks"):
                with Container(id="task-summary-box"):
                    yield Static("当前任务：暂无运行中的导出任务", id="task-course-label")
                    yield Static("进度状态：等待操作", id="task-status-label")
                    yield ProgressBar(total=100, show_eta=False, id="task-progress")
                    yield Static("", id="task-output-label")
                    with Horizontal(classes="toolbar"):
                        yield Button("停止当前任务", variant="error", id="btn-stop-task", disabled=True)
                        yield Button("打开输出目录", id="btn-task-open-dir")
                yield RichLog(id="task-log", highlight=True, markup=True)

            with TabPane("导出历史", id="tab-history"):
                with Horizontal(classes="toolbar"):
                    yield Button("打开选中文件/目录", variant="primary", id="btn-hist-open-file")
                    yield Button("打开输出目录", id="btn-hist-open-dir")
                    yield Button("刷新历史", id="btn-hist-refresh")
                yield DataTable(id="history-table", cursor_type="row")

            with TabPane("配置中心", id="tab-config"):
                with TabbedContent(id="config-subtabs"):
                    with TabPane("快捷配置", id="cfg-quick"):
                        with VerticalScroll():
                            yield Static("常用导出设置（保存后立即写入 config.yml）", classes="cfg-section-title")
                            with Horizontal(classes="cfg-row"):
                                yield Label("默认导出格式（多选）", classes="cfg-label")
                                yield SelectionList(
                                    *[(lbl, val, val in self.cfg.export_formats) for lbl, val in FORMAT_LABELS],
                                    id="quick-formats",
                                )
                            yield Checkbox("同时生成无答案刷题版", value=self.cfg.export_no_answer_edition, id="quick-no-answer")
                            yield Checkbox("按分组拆分为多个文件 (bulk_files)", value=self.cfg.bulk_files, id="quick-bulk")
                            yield Checkbox("下载题目图片到本地 images/ 目录", value=self.cfg.download_images, id="quick-download-img")
                            yield Checkbox("导出完成后自动打开文件/目录", value=self.cfg.auto_open_output, id="quick-auto-open")
                            with Horizontal(classes="cfg-row"):
                                yield Label("导出保存目录", classes="cfg-label")
                                yield Input(value=self.cfg.save_path, id="quick-save-path", classes="cfg-input")
                            with Horizontal(classes="cfg-actions"):
                                yield Button("保存快捷配置", variant="primary", id="btn-save-quick")

                    with TabPane("完整配置", id="cfg-full"):
                        with VerticalScroll(id="full-cfg-scroll"):
                            yield Static("基础与界面", classes="cfg-section-title")
                            with Horizontal(classes="cfg-row"):
                                yield Label("界面主题 (theme)", classes="cfg-label")
                                yield Select([("暗色模式 (dark)", "dark"), ("亮色模式 (light)", "light")], value=self.cfg.theme, id="full-theme")

                            yield Static("导出格式与文件组织", classes="cfg-section-title")
                            with Horizontal(classes="cfg-row"):
                                yield Label("导出格式 (export_formats)", classes="cfg-label")
                                yield SelectionList(
                                    *[(lbl, val, val in self.cfg.export_formats) for lbl, val in FORMAT_LABELS],
                                    id="full-formats",
                                )
                            yield Checkbox("生成无答案刷题版", value=self.cfg.export_no_answer_edition, id="full-no-answer")
                            yield Checkbox("按分组拆分文件 (bulk_files)", value=self.cfg.bulk_files, id="full-bulk")
                            with Horizontal(classes="cfg-row"):
                                yield Label("分文件依据 (bulk_split_by)", classes="cfg-label")
                                yield Select([("按章节 (chapter)", "chapter"), ("按题型 (type)", "type")], value=self.cfg.bulk_split_by, id="full-split-by")
                            with Horizontal(classes="cfg-row"):
                                yield Label("保存目录 (save_path)", classes="cfg-label")
                                yield Input(value=self.cfg.save_path, id="full-save-path", classes="cfg-input")
                            yield Checkbox("允许覆盖同名文件", value=self.cfg.allow_overwrite_files, id="full-overwrite")
                            yield Checkbox("同时保存时间戳备份", value=self.cfg.save_timestamp_backup, id="full-ts-backup")
                            yield Checkbox("导出完成后自动打开", value=self.cfg.auto_open_output, id="full-auto-open")

                            yield Static("内容与图片处理", classes="cfg-section-title")
                            yield Checkbox("保留章节标签", value=self.cfg.include_chapter_tag, id="full-chap-tag")
                            yield Checkbox("包含难易度与解析", value=self.cfg.include_analysis, id="full-analysis")
                            yield Checkbox("全局去重完全相同的题目", value=self.cfg.deduplicate_questions, id="full-dedup")
                            yield Checkbox("下载图片到本地 images/", value=self.cfg.download_images, id="full-download-img")
                            with Horizontal(classes="cfg-row"):
                                yield Label("图片最大宽度像素 (0不缩放)", classes="cfg-label")
                                yield Input(value=str(self.cfg.image_max_width_px), id="full-img-max-w", classes="cfg-input")
                            with Horizontal(classes="cfg-row"):
                                yield Label("PDF 中文字体路径 (可留空)", classes="cfg-label")
                                yield Input(value=self.cfg.pdf_font_path, id="full-pdf-font", classes="cfg-input")

                            yield Static("导出范围默认设置", classes="cfg-section-title")
                            with Horizontal(classes="cfg-row"):
                                yield Label("默认范围模式", classes="cfg-label")
                                yield Select(
                                    [("全部题目 (all)", "all"), ("按章节 (chapters)", "chapters"), ("按题型 (types)", "types"), ("按难度 (easy)", "easy")],
                                    value=self.cfg.default_range_mode,
                                    id="full-range-mode",
                                )
                            yield Checkbox("全量导出时补充未归类散题", value=self.cfg.include_unclassified_extra, id="full-extra")

                            yield Static("网络与稳定性", classes="cfg-section-title")
                            with Horizontal(classes="cfg-row"):
                                yield Label("单批最大组卷数 (1-500)", classes="cfg-label")
                                yield Input(value=str(self.cfg.max_batch_size), id="full-max-batch", classes="cfg-input")
                            with Horizontal(classes="cfg-row"):
                                yield Label("请求超时秒数", classes="cfg-label")
                                yield Input(value=str(self.cfg.request_timeout), id="full-timeout", classes="cfg-input")
                            with Horizontal(classes="cfg-row"):
                                yield Label("任务轮询间隔秒数", classes="cfg-label")
                                yield Input(value=str(self.cfg.poll_interval_sec), id="full-poll", classes="cfg-input")
                            with Horizontal(classes="cfg-row"):
                                yield Label("失败最大重试次数", classes="cfg-label")
                                yield Input(value=str(self.cfg.max_retries), id="full-retries", classes="cfg-input")
                            yield Checkbox("导出后自动清理临时自测卷", value=self.cfg.auto_clean_temp_tests, id="full-auto-clean")
                            with Horizontal(classes="cfg-row"):
                                yield Label("代理地址 (如 http://127.0.0.1:7890)", classes="cfg-label")
                                yield Input(value=self.cfg.proxy, id="full-proxy", classes="cfg-input")

                            with Horizontal(classes="cfg-actions"):
                                yield Button("保存完整配置", variant="primary", id="btn-save-full")
                                yield Button("恢复默认配置", id="btn-reset-default")

                    with TabPane("YAML 编辑", id="cfg-yaml"):
                        try:
                            yield TextArea(
                                render_commented_yaml(self.cfg),
                                language="yaml",
                                show_line_numbers=True,
                                id="yaml-editor",
                            )
                        except Exception:
                            yield TextArea(
                                render_commented_yaml(self.cfg),
                                show_line_numbers=True,
                                id="yaml-editor",
                            )
                        with Horizontal(classes="cfg-actions"):
                            yield Button("重新加载", id="btn-yaml-reload")
                            yield Button("校验并保存 YAML", variant="primary", id="btn-yaml-save")

        yield Footer()

    def on_mount(self) -> None:
        courses_table = self.query_one("#courses-table", DataTable)
        courses_table.add_columns("课程名称", "课程 ID", "班级 ID", "自测题量")

        hist_table = self.query_one("#history-table", DataTable)
        hist_table.add_columns("导出时间", "课程名称", "导出范围", "题数", "格式", "主输出路径")

        self._refresh_history_table()
        self._sync_ui_from_config()

        if self.skip_login:
            self._refresh_courses_table()
        else:
            self.push_screen(LoginModal(self.cfg), self._on_login_done)

    def _on_login_done(self, result: Optional[tuple]) -> None:
        if result is None:
            self.exit()
            return
        self.client, self.courses = result
        self._refresh_courses_table()
        self.notify(f"登录成功，共加载 {len(self.courses)} 门课程")

    def action_toggle_theme(self) -> None:
        new_theme = "light" if self.cfg.theme == "dark" else "dark"
        self.cfg.theme = new_theme
        self.theme = "textual-light" if new_theme == "light" else "textual-dark"
        save_config(self.cfg, self.config_path)
        self._sync_ui_from_config()
        self.notify(f"已切换为{'亮色' if new_theme == 'light' else '暗色'}主题")

    def _set_courses_busy(self, busy: bool) -> None:
        self._courses_busy = busy
        self.query_one("#btn-scan-counts", Button).disabled = busy
        self.query_one("#btn-refresh-courses", Button).disabled = busy

    def action_refresh_all(self) -> None:
        self._refresh_history_table()
        if self.client and not self._courses_busy and not self._task_running:
            self._set_courses_busy(True)
            self._reload_courses_worker()

    # -------------------- 课程列表逻辑 --------------------

    def _refresh_courses_table(self) -> None:
        keyword = self.query_one("#course-search", Input).value.strip().lower()
        only_nonempty = self.query_one("#only-nonempty-check", Checkbox).value

        table = self.query_one("#courses-table", DataTable)
        table.clear()
        self._course_map = {}

        for item in self.courses:
            if keyword and keyword not in item["name"].lower() and keyword not in item["course_id"]:
                continue
            qcnt = item.get("qbank_count")
            if only_nonempty and (qcnt is None or qcnt <= 0):
                continue

            key = f"{item['course_id']}|{item['class_id']}"
            cnt_text = item.get("qbank_label") or ("待扫描" if qcnt is None else str(qcnt))
            table.add_row(
                item["name"],
                item["course_id"],
                item["class_id"] or "-",
                cnt_text,
                key=key,
            )
            self._course_map[key] = item

    @on(Input.Changed, "#course-search")
    def _on_search_changed(self) -> None:
        self._refresh_courses_table()

    @on(Checkbox.Changed, "#only-nonempty-check")
    def _on_only_nonempty_changed(self) -> None:
        self._refresh_courses_table()

    @on(Button.Pressed, "#btn-refresh-courses")
    def _on_refresh_courses_btn(self) -> None:
        if not self.client:
            return
        if self._courses_busy or self._task_running:
            self.notify("当前有扫描或导出任务正在运行，请稍候", severity="warning")
            return
        self._set_courses_busy(True)
        self._reload_courses_worker()

    @work(thread=True)
    def _reload_courses_worker(self) -> None:
        try:
            courses = self.client.get_all_courses()
            def _done():
                self.courses = courses
                self._refresh_courses_table()
                self.notify(f"已刷新课程列表，共 {len(courses)} 门")
            self.call_from_thread(_done)
        except Exception as exc:
            self.call_from_thread(self.notify, f"刷新课程失败: {exc}", severity="error")
        finally:
            self.call_from_thread(self._set_courses_busy, False)

    @on(Button.Pressed, "#btn-scan-counts")
    def _on_scan_counts_btn(self) -> None:
        if not self.client:
            return
        if self._courses_busy or self._task_running:
            self.notify("当前有扫描或导出任务正在运行，请稍候", severity="warning")
            return
        self._set_courses_busy(True)
        self.notify("开始后台扫描各课程的自测题量...")
        self._scan_counts_worker()

    @work(thread=True)
    def _scan_counts_worker(self) -> None:
        try:
            total = len(self.courses)
            for idx, item in enumerate(self.courses, 1):
                try:
                    cnt, label = self.client.scan_course_metric(item)
                    item["qbank_count"] = cnt
                    item["qbank_label"] = label
                except Exception:
                    item["qbank_count"] = 0
                    item["qbank_label"] = "0 题"
                if idx % 5 == 0 or idx == total:
                    self.call_from_thread(self._refresh_courses_table)

            def _finish():
                has_q = sum(1 for c in self.courses if (c.get("qbank_count") or 0) > 0)
                self.notify(f"扫描完成：{has_q}/{total} 门课程开通了自测题库")

            self.call_from_thread(_finish)
        finally:
            self.call_from_thread(self._set_courses_busy, False)

    def _get_selected_course(self) -> Optional[dict]:
        table = self.query_one("#courses-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        row_key, _ = table.coordinate_to_cell_key((table.cursor_row, 0))
        return self._course_map.get(str(row_key.value))

    @on(Button.Pressed, "#btn-open-preview")
    def _on_preview_btn(self) -> None:
        course = self._get_selected_course()
        if not course:
            self.notify("请先在课程列表中选择一门课程", severity="warning")
            return
        self._open_export_modal(course)

    @on(DataTable.RowSelected, "#courses-table")
    def _on_course_row_selected(self, event: DataTable.RowSelected) -> None:
        course = self._course_map.get(str(event.row_key.value))
        if course:
            self._open_export_modal(course)

    def _open_export_modal(self, course: dict) -> None:
        if self._task_running:
            self.notify("当前有导出任务正在执行，请等待完成或先停止任务", severity="warning")
            return
        if not self.client:
            return
        self.push_screen(ExportScopeModal(self.client, course, self.cfg), lambda res: self._on_scope_confirmed(course, res))

    def _on_scope_confirmed(self, course: dict, res: Optional[dict]) -> None:
        if not res:
            return
        self.query_one("#main-tabs", TabbedContent).active = "tab-tasks"
        self._start_export_task(course, res["scope"], res["scope_summary"], res["run_cfg"], res.get("preview_data"))

    # -------------------- 导出任务执行逻辑 --------------------

    def _start_export_task(
        self,
        course: dict,
        scope: dict,
        scope_summary: str,
        run_cfg: AppConfig,
        preview_data: Optional[dict],
    ) -> None:
        self._task_running = True
        self._stop_event = threading.Event()
        self.query_one("#btn-stop-task", Button).disabled = False
        self.query_one("#task-course-label", Static).update(
            f"当前任务：{course['name']} (ID: {course['course_id']}) | 范围：{scope_summary}"
        )
        self.query_one("#task-status-label", Static).update("进度状态：正在准备导出...")
        self.query_one("#task-output-label", Static).update("")
        self.query_one("#task-progress", ProgressBar).update(total=100, progress=0)
        log_widget = self.query_one("#task-log", RichLog)
        log_widget.clear()
        self._run_export_worker(course, scope, scope_summary, run_cfg, preview_data)

    @on(Button.Pressed, "#btn-stop-task")
    def _on_stop_task(self) -> None:
        if self._stop_event:
            self._stop_event.set()
            self.query_one("#task-status-label", Static).update("进度状态：正在安全中止并清理临时试卷...")
            self.query_one("#btn-stop-task", Button).disabled = True

    @on(Button.Pressed, "#btn-task-open-dir")
    def _on_task_open_dir(self) -> None:
        target = self._last_output_dir or self.cfg.save_path
        os.makedirs(target, exist_ok=True)
        try:
            _open_local_path(target)
        except Exception as exc:
            self.notify(f"打开目录失败: {exc}", severity="error")

    @work(thread=True)
    def _run_export_worker(
        self,
        course: dict,
        scope: dict,
        scope_summary: str,
        run_cfg: AppConfig,
        preview_data: Optional[dict],
    ) -> None:
        def _log(msg: str) -> None:
            self.call_from_thread(self.query_one("#task-log", RichLog).write, msg)

        def _prog(done: int, total: int, msg: str) -> None:
            def _u():
                self.query_one("#task-progress", ProgressBar).update(total=max(total, 1), progress=done)
                self.query_one("#task-status-label", Static).update(f"进度状态：{msg}")
            self.call_from_thread(_u)

        try:
            questions = self.client.export_course_selftest(
                course,
                scope=scope,
                preview_data=preview_data,
                max_batch_size=run_cfg.max_batch_size,
                poll_interval=run_cfg.poll_interval_sec,
                max_retries=run_cfg.max_retries,
                include_unclassified_extra=run_cfg.include_unclassified_extra,
                deduplicate=run_cfg.deduplicate_questions,
                auto_clean=run_cfg.auto_clean_temp_tests,
                progress_cb=_prog,
                log_cb=_log,
                stop_event=self._stop_event,
            )

            if not questions:
                def _empty():
                    self.query_one("#task-status-label", Static).update("进度状态：未抓取到有效题目，已结束")
                    self.notify("未抓取到题目，未生成文件", severity="warning")
                self.call_from_thread(_empty)
                return

            _log(f"抓取完成（共 {len(questions)} 题），正在生成导出文件...")
            summary = export_questions(
                course_id=course["course_id"],
                course_name=course["name"],
                questions=questions,
                cfg=run_cfg,
                session=self.client.client,
                scope_summary=scope_summary,
                log_cb=_log,
                stop_event=self._stop_event,
            )
            self._last_output_dir = summary["output_dir"]

            def _done():
                self.query_one("#task-progress", ProgressBar).update(total=100, progress=100)
                self.query_one("#task-status-label", Static).update(
                    f"进度状态：导出成功！共 {summary['count']} 题，生成 {len(summary['files'])} 个文件"
                )
                self.query_one("#task-output-label", Static).update(f"主输出路径：{summary['primary_path']}")
                self._refresh_history_table()
                self.notify(f"导出成功！共 {summary['count']} 题")
                if run_cfg.auto_open_output:
                    try:
                        _open_local_path(summary["primary_path"])
                    except Exception:
                        pass

            self.call_from_thread(_done)

        except ExportCancelledError:
            _log("[yellow]任务已被用户安全中止，临时自测卷已清理。[/yellow]")
            self.call_from_thread(
                lambda: self.query_one("#task-status-label", Static).update("进度状态：已中止（临时自测卷已清理）")
            )
        except Exception as exc:
            err_msg = str(exc)
            _log(f"[red]导出失败: {err_msg}[/red]")

            def _on_err(msg: str = err_msg) -> None:
                self.query_one("#task-status-label", Static).update(f"进度状态：导出失败 - {msg}")
                self.notify(f"导出失败: {msg}", severity="error")

            self.call_from_thread(_on_err)
        finally:
            def _cleanup():
                self._task_running = False
                self.query_one("#btn-stop-task", Button).disabled = True
            self.call_from_thread(_cleanup)

    # -------------------- 导出历史逻辑 --------------------

    def _refresh_history_table(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        self._history_records = load_history(self.cfg.save_path)
        for idx, rec in enumerate(self._history_records):
            fmts = ", ".join(rec.get("formats", []))
            table.add_row(
                rec.get("time", ""),
                rec.get("course_name", ""),
                rec.get("scope", ""),
                str(rec.get("count", 0)),
                fmts,
                rec.get("primary_path", ""),
                key=str(idx),
            )

    @on(Button.Pressed, "#btn-hist-refresh")
    def _on_hist_refresh(self) -> None:
        self._refresh_history_table()

    @on(Button.Pressed, "#btn-hist-open-dir")
    def _on_hist_open_dir(self) -> None:
        target = self.cfg.save_path or "out"
        os.makedirs(target, exist_ok=True)
        try:
            _open_local_path(target)
        except Exception as exc:
            self.notify(f"打开目录失败: {exc}", severity="error")

    @on(Button.Pressed, "#btn-hist-open-file")
    def _on_hist_open_file(self) -> None:
        table = self.query_one("#history-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            self.notify("请先选择一条历史记录", severity="warning")
            return
        row_key, _ = table.coordinate_to_cell_key((table.cursor_row, 0))
        idx = int(row_key.value)
        path = self._history_records[idx].get("primary_path", "")
        try:
            _open_local_path(path)
        except Exception as exc:
            self.notify(f"打开文件失败: {exc}", severity="error")

    # -------------------- 配置中心同步与保存 --------------------

    def _sync_ui_from_config(self) -> None:
        # 快捷配置
        q_fmts = self.query_one("#quick-formats", SelectionList)
        q_fmts.clear_options()
        q_fmts.add_options([(lbl, val, val in self.cfg.export_formats) for lbl, val in FORMAT_LABELS])
        self.query_one("#quick-no-answer", Checkbox).value = self.cfg.export_no_answer_edition
        self.query_one("#quick-bulk", Checkbox).value = self.cfg.bulk_files
        self.query_one("#quick-download-img", Checkbox).value = self.cfg.download_images
        self.query_one("#quick-auto-open", Checkbox).value = self.cfg.auto_open_output
        self.query_one("#quick-save-path", Input).value = self.cfg.save_path

        # 完整配置
        self.query_one("#full-theme", Select).value = self.cfg.theme
        f_fmts = self.query_one("#full-formats", SelectionList)
        f_fmts.clear_options()
        f_fmts.add_options([(lbl, val, val in self.cfg.export_formats) for lbl, val in FORMAT_LABELS])
        self.query_one("#full-no-answer", Checkbox).value = self.cfg.export_no_answer_edition
        self.query_one("#full-bulk", Checkbox).value = self.cfg.bulk_files
        self.query_one("#full-split-by", Select).value = self.cfg.bulk_split_by
        self.query_one("#full-save-path", Input).value = self.cfg.save_path
        self.query_one("#full-overwrite", Checkbox).value = self.cfg.allow_overwrite_files
        self.query_one("#full-ts-backup", Checkbox).value = self.cfg.save_timestamp_backup
        self.query_one("#full-auto-open", Checkbox).value = self.cfg.auto_open_output
        self.query_one("#full-chap-tag", Checkbox).value = self.cfg.include_chapter_tag
        self.query_one("#full-analysis", Checkbox).value = self.cfg.include_analysis
        self.query_one("#full-dedup", Checkbox).value = self.cfg.deduplicate_questions
        self.query_one("#full-download-img", Checkbox).value = self.cfg.download_images
        self.query_one("#full-img-max-w", Input).value = str(self.cfg.image_max_width_px)
        self.query_one("#full-pdf-font", Input).value = self.cfg.pdf_font_path
        self.query_one("#full-range-mode", Select).value = self.cfg.default_range_mode
        self.query_one("#full-extra", Checkbox).value = self.cfg.include_unclassified_extra
        self.query_one("#full-max-batch", Input).value = str(self.cfg.max_batch_size)
        self.query_one("#full-timeout", Input).value = str(self.cfg.request_timeout)
        self.query_one("#full-poll", Input).value = str(self.cfg.poll_interval_sec)
        self.query_one("#full-retries", Input).value = str(self.cfg.max_retries)
        self.query_one("#full-auto-clean", Checkbox).value = self.cfg.auto_clean_temp_tests
        self.query_one("#full-proxy", Input).value = self.cfg.proxy

        # YAML 编辑器
        self.query_one("#yaml-editor", TextArea).load_text(render_commented_yaml(self.cfg))

    def _apply_new_config(self, new_cfg: AppConfig, tip: str) -> None:
        self.cfg = new_cfg
        save_config(self.cfg, self.config_path)
        self.theme = "textual-light" if self.cfg.theme == "light" else "textual-dark"
        if self.client:
            self.client.timeout = self.cfg.request_timeout
            self.client.set_proxy(self.cfg.proxy)
        self._sync_ui_from_config()
        self._refresh_history_table()
        self.notify(tip)

    @on(Button.Pressed, "#btn-save-quick")
    def _on_save_quick(self) -> None:
        fmts = list(self.query_one("#quick-formats", SelectionList).selected)
        if not fmts:
            self.notify("请至少选择一种默认导出格式", severity="warning")
            return
        save_path = self.query_one("#quick-save-path", Input).value.strip() or "out"
        new_cfg = replace(
            self.cfg,
            export_formats=fmts,
            export_no_answer_edition=self.query_one("#quick-no-answer", Checkbox).value,
            bulk_files=self.query_one("#quick-bulk", Checkbox).value,
            download_images=self.query_one("#quick-download-img", Checkbox).value,
            auto_open_output=self.query_one("#quick-auto-open", Checkbox).value,
            save_path=save_path,
        )
        self._apply_new_config(new_cfg, "快捷配置已保存到 config.yml")

    @on(Button.Pressed, "#btn-save-full")
    def _on_save_full(self) -> None:
        fmts = list(self.query_one("#full-formats", SelectionList).selected)
        if not fmts:
            self.notify("请至少选择一种默认导出格式", severity="warning")
            return
        try:
            img_max_w = int(self.query_one("#full-img-max-w", Input).value.strip() or "1200")
            max_batch = int(self.query_one("#full-max-batch", Input).value.strip() or "500")
            timeout = int(self.query_one("#full-timeout", Input).value.strip() or "20")
            poll = float(self.query_one("#full-poll", Input).value.strip() or "2.0")
            retries = int(self.query_one("#full-retries", Input).value.strip() or "2")
            if max_batch < 1 or max_batch > 500:
                raise ValueError("单批最大组卷数必须在 1-500 之间")
            if timeout < 1 or poll < 0.5 or retries < 1 or img_max_w < 0:
                raise ValueError("数值参数超出合法范围")
        except ValueError as exc:
            self.notify(f"参数校验失败: {exc}", severity="error")
            return

        new_cfg = AppConfig(
            theme=str(self.query_one("#full-theme", Select).value),
            export_formats=fmts,
            export_no_answer_edition=self.query_one("#full-no-answer", Checkbox).value,
            bulk_files=self.query_one("#full-bulk", Checkbox).value,
            bulk_split_by=str(self.query_one("#full-split-by", Select).value),
            save_path=self.query_one("#full-save-path", Input).value.strip() or "out",
            allow_overwrite_files=self.query_one("#full-overwrite", Checkbox).value,
            save_timestamp_backup=self.query_one("#full-ts-backup", Checkbox).value,
            auto_open_output=self.query_one("#full-auto-open", Checkbox).value,
            include_chapter_tag=self.query_one("#full-chap-tag", Checkbox).value,
            include_analysis=self.query_one("#full-analysis", Checkbox).value,
            deduplicate_questions=self.query_one("#full-dedup", Checkbox).value,
            download_images=self.query_one("#full-download-img", Checkbox).value,
            image_max_width_px=img_max_w,
            pdf_font_path=self.query_one("#full-pdf-font", Input).value.strip(),
            default_range_mode=str(self.query_one("#full-range-mode", Select).value),
            include_unclassified_extra=self.query_one("#full-extra", Checkbox).value,
            max_batch_size=max_batch,
            request_timeout=timeout,
            poll_interval_sec=poll,
            max_retries=retries,
            auto_clean_temp_tests=self.query_one("#full-auto-clean", Checkbox).value,
            proxy=self.query_one("#full-proxy", Input).value.strip(),
        )
        self._apply_new_config(new_cfg, "完整配置已保存到 config.yml")

    @on(Button.Pressed, "#btn-reset-default")
    def _on_reset_default(self) -> None:
        self._apply_new_config(AppConfig(), "已恢复为默认配置并写入 config.yml")

    @on(Button.Pressed, "#btn-yaml-reload")
    def _on_yaml_reload(self) -> None:
        try:
            loaded = load_config(self.config_path)
            self.cfg = loaded
            self.theme = "textual-light" if self.cfg.theme == "light" else "textual-dark"
            if self.client:
                self.client.timeout = self.cfg.request_timeout
                self.client.set_proxy(self.cfg.proxy)
            self._sync_ui_from_config()
            self._refresh_history_table()
            self.notify("已从磁盘重新加载 config.yml")
        except Exception as exc:
            self.notify(f"加载 YAML 失败: {exc}", severity="error")

    @on(Button.Pressed, "#btn-yaml-save")
    def _on_yaml_save(self) -> None:
        raw = self.query_one("#yaml-editor", TextArea).text
        try:
            parsed = parse_and_validate_yaml(raw)
        except ValueError as exc:
            self.notify(f"校验失败，未保存: {exc}", severity="error")
            return
        self._apply_new_config(parsed, "YAML 配置校验通过并已保存生效")


def run_tui(config_path: str = "config.yml") -> None:
    logger.remove()
    app = QuizKitApp(config_path=config_path)
    app.run()

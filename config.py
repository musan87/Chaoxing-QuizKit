from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Any, List

import yaml

VALID_FORMATS = ("txt", "md", "docx", "pdf", "json")
VALID_THEMES = ("dark", "light")
VALID_SPLIT_BY = ("chapter", "type")
VALID_RANGE_MODES = ("all", "chapters", "types", "easy")


@dataclass
class AppConfig:
    # 基础与界面
    theme: str = "dark"

    # 导出格式与文件组织
    export_formats: List[str] = field(default_factory=lambda: ["txt", "md"])
    export_no_answer_edition: bool = False
    bulk_files: bool = False
    bulk_split_by: str = "chapter"
    save_path: str = "out"
    allow_overwrite_files: bool = True
    save_timestamp_backup: bool = False
    auto_open_output: bool = False

    # 内容与图片处理
    include_chapter_tag: bool = True
    include_analysis: bool = True
    deduplicate_questions: bool = False
    download_images: bool = True
    image_max_width_px: int = 1200
    pdf_font_path: str = ""

    # 导出范围默认设置
    default_range_mode: str = "all"
    include_unclassified_extra: bool = True

    # 网络与稳定性
    max_batch_size: int = 500
    request_timeout: int = 20
    poll_interval_sec: float = 2.0
    max_retries: int = 2
    auto_clean_temp_tests: bool = True
    proxy: str = ""


def _yaml_scalar(value: Any) -> str:
    dumped = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True).strip()
    if dumped.endswith("\n..."):
        dumped = dumped[:-4].strip()
    elif dumped.endswith("..."):
        dumped = dumped[:-3].strip()
    return dumped


def render_commented_yaml(cfg: AppConfig) -> str:
    """生成带完整中文注释的 config.yml 内容。"""
    fmt_list = _yaml_scalar(list(cfg.export_formats))
    lines = [
        "# ================= Chaoxing-QuizKit 配置文件 =================",
        "# 修改后可在 TUI 界面点击「重新加载」生效，或直接在 TUI「配置中心」中修改保存。",
        "# 注意：本程序不会保存手机号、密码或 Cookie，每次启动均需手动登录。",
        "",
        "# -------------------- 基础与界面设置 --------------------",
        "# 界面主题模式，可选: [dark, light]",
        f"theme: {_yaml_scalar(cfg.theme)}",
        "",
        "# -------------------- 导出格式与文件组织 --------------------",
        "# 导出格式列表（支持多选），可选: [txt, md, docx, pdf, json]",
        f"export_formats: {fmt_list}",
        "# 是否同时生成无答案刷题版（隐藏正确答案与解析，适用于 txt/md/docx/pdf）",
        f"export_no_answer_edition: {_yaml_scalar(bool(cfg.export_no_answer_edition))}",
        "# 是否以散装分文件形式保存（true: 按章节或题型拆分为多个文件，false: 合并为单文件）",
        f"bulk_files: {_yaml_scalar(bool(cfg.bulk_files))}",
        "# 散装分文件时的拆分依据，可选: [chapter, type]（chapter=按章节，type=按题型）",
        f"bulk_split_by: {_yaml_scalar(cfg.bulk_split_by)}",
        "# 导出保存目录（相对路径或绝对路径，默认为 out）",
        f"save_path: {_yaml_scalar(cfg.save_path)}",
        "# 是否允许覆盖同名文件（false 时仅保存带时间戳的文件）",
        f"allow_overwrite_files: {_yaml_scalar(bool(cfg.allow_overwrite_files))}",
        "# 是否额外保存一份带时间戳的历史备份文件",
        f"save_timestamp_backup: {_yaml_scalar(bool(cfg.save_timestamp_backup))}",
        "# 导出完成后自动打开生成的文件或目录",
        f"auto_open_output: {_yaml_scalar(bool(cfg.auto_open_output))}",
        "",
        "# -------------------- 内容与图片处理 --------------------",
        "# 导出文本中是否在题号旁标注所属章节",
        f"include_chapter_tag: {_yaml_scalar(bool(cfg.include_chapter_tag))}",
        "# 导出文本中是否包含题目难易度与解析信息",
        f"include_analysis: {_yaml_scalar(bool(cfg.include_analysis))}",
        "# 是否对跨章节完全重复的题目进行全局去重",
        f"deduplicate_questions: {_yaml_scalar(bool(cfg.deduplicate_questions))}",
        "# 是否将题目/答案中的图片下载到本地 images/ 目录并内嵌到文档（true: Word/PDF 直接显示图片、MD 离线可读；false: 仅保留图片链接，Word/PDF 不显示图片）",
        f"download_images: {_yaml_scalar(bool(cfg.download_images))}",
        "# 下载图片最大宽度像素上限（>0 时等比缩放，0 表示保持原图尺寸）",
        f"image_max_width_px: {_yaml_scalar(int(cfg.image_max_width_px))}",
        "# PDF 导出使用的中文字体路径（留空自动检测 Windows 系统微软雅黑/黑体/宋体）",
        f"pdf_font_path: {_yaml_scalar(cfg.pdf_font_path)}",
        "",
        "# -------------------- 导出范围默认设置 --------------------",
        "# 默认导出范围模式，可选: [all, chapters, types, easy]",
        f"default_range_mode: {_yaml_scalar(cfg.default_range_mode)}",
        "# 全量导出时，是否额外按难度/题型补充抓取未归入文件夹的散题",
        f"include_unclassified_extra: {_yaml_scalar(bool(cfg.include_unclassified_extra))}",
        "",
        "# -------------------- 网络与稳定性设置 --------------------",
        "# 单次自测组卷最大题数（超星接口上限为 500）",
        f"max_batch_size: {_yaml_scalar(int(cfg.max_batch_size))}",
        "# HTTP 请求超时时间（秒）",
        f"request_timeout: {_yaml_scalar(int(cfg.request_timeout))}",
        "# 组卷任务状态轮询间隔（秒）",
        f"poll_interval_sec: {_yaml_scalar(float(cfg.poll_interval_sec))}",
        "# 单批次失败最大重试次数",
        f"max_retries: {_yaml_scalar(int(cfg.max_retries))}",
        "# 每批抓取完成后是否自动删除账号中的临时自测卷",
        f"auto_clean_temp_tests: {_yaml_scalar(bool(cfg.auto_clean_temp_tests))}",
        "# HTTP/HTTPS 代理地址（如 http://127.0.0.1:7890，留空表示不使用代理）",
        f"proxy: {_yaml_scalar(cfg.proxy)}",
        "",
    ]
    return "\n".join(lines)


def _as_bool(val: Any, name: str) -> bool:
    if isinstance(val, bool):
        return val
    raise ValueError(f"配置项 `{name}` 必须为布尔值 (true/false)")


def _as_int(val: Any, name: str, min_val: int = 0, max_val: int | None = None) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"配置项 `{name}` 必须为整数")
    if val < min_val:
        raise ValueError(f"配置项 `{name}` 不能小于 {min_val}")
    if max_val is not None and val > max_val:
        raise ValueError(f"配置项 `{name}` 不能大于 {max_val}")
    return val


def _as_float(val: Any, name: str, min_val: float = 0.0) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"配置项 `{name}` 必须为数字")
    fval = float(val)
    if fval < min_val:
        raise ValueError(f"配置项 `{name}` 不能小于 {min_val}")
    return fval


def validate_config_dict(raw: dict[str, Any]) -> AppConfig:
    """校验字典数据并构造 AppConfig（缺省字段使用默认值）。"""
    if not isinstance(raw, dict):
        raise ValueError("YAML 根节点必须是键值对映射")

    defaults = asdict(AppConfig())
    merged = {**defaults, **raw}

    theme = str(merged["theme"]).strip().lower()
    if theme not in VALID_THEMES:
        raise ValueError(f"配置项 `theme` 仅支持 {list(VALID_THEMES)}")

    fmts_raw = merged["export_formats"]
    if isinstance(fmts_raw, str):
        fmts_raw = [x.strip().lower() for x in fmts_raw.split(",") if x.strip()]
    if not isinstance(fmts_raw, list) or not fmts_raw:
        raise ValueError("配置项 `export_formats` 必须是非空列表，例如 [txt, md]")
    export_formats: List[str] = []
    for item in fmts_raw:
        fmt = str(item).strip().lower()
        if fmt not in VALID_FORMATS:
            raise ValueError(f"不支持的导出格式 `{fmt}`，可选值: {list(VALID_FORMATS)}")
        if fmt not in export_formats:
            export_formats.append(fmt)

    bulk_split_by = str(merged["bulk_split_by"]).strip().lower()
    if bulk_split_by not in VALID_SPLIT_BY:
        raise ValueError(f"配置项 `bulk_split_by` 仅支持 {list(VALID_SPLIT_BY)}")

    save_path = str(merged["save_path"] or "out").strip()
    pdf_font_path = str(merged["pdf_font_path"] or "").strip()

    default_range_mode = str(merged["default_range_mode"]).strip().lower()
    if default_range_mode not in VALID_RANGE_MODES:
        raise ValueError(f"配置项 `default_range_mode` 仅支持 {list(VALID_RANGE_MODES)}")

    return AppConfig(
        theme=theme,
        export_formats=export_formats,
        export_no_answer_edition=_as_bool(merged["export_no_answer_edition"], "export_no_answer_edition"),
        bulk_files=_as_bool(merged["bulk_files"], "bulk_files"),
        bulk_split_by=bulk_split_by,
        save_path=save_path,
        allow_overwrite_files=_as_bool(merged["allow_overwrite_files"], "allow_overwrite_files"),
        save_timestamp_backup=_as_bool(merged["save_timestamp_backup"], "save_timestamp_backup"),
        auto_open_output=_as_bool(merged["auto_open_output"], "auto_open_output"),
        include_chapter_tag=_as_bool(merged["include_chapter_tag"], "include_chapter_tag"),
        include_analysis=_as_bool(merged["include_analysis"], "include_analysis"),
        deduplicate_questions=_as_bool(merged["deduplicate_questions"], "deduplicate_questions"),
        download_images=_as_bool(merged["download_images"], "download_images"),
        image_max_width_px=_as_int(merged["image_max_width_px"], "image_max_width_px", min_val=0),
        pdf_font_path=pdf_font_path,
        default_range_mode=default_range_mode,
        include_unclassified_extra=_as_bool(merged["include_unclassified_extra"], "include_unclassified_extra"),
        max_batch_size=_as_int(merged["max_batch_size"], "max_batch_size", min_val=1, max_val=500),
        request_timeout=_as_int(merged["request_timeout"], "request_timeout", min_val=1),
        poll_interval_sec=_as_float(merged["poll_interval_sec"], "poll_interval_sec", min_val=0.5),
        max_retries=_as_int(merged["max_retries"], "max_retries", min_val=1),
        auto_clean_temp_tests=_as_bool(merged["auto_clean_temp_tests"], "auto_clean_temp_tests"),
        proxy=str(merged["proxy"] or "").strip(),
    )


def parse_and_validate_yaml(raw_text: str) -> AppConfig:
    """解析 YAML 字符串并执行严格校验。"""
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 语法错误: {exc}") from exc
    if data is None:
        data = {}
    return validate_config_dict(data)


def load_config(path: str = "config.yml") -> AppConfig:
    """加载配置文件；若不存在则自动创建带中文注释的默认配置。"""
    if not os.path.exists(path):
        cfg = AppConfig()
        save_config(cfg, path)
        return cfg
    with open(path, "r", encoding="utf8") as f:
        text = f.read()
    cfg = parse_and_validate_yaml(text)
    return cfg


def save_config(cfg: AppConfig, path: str = "config.yml") -> None:
    """将配置写入带中文注释的 YAML 文件。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf8") as f:
        f.write(render_commented_yaml(cfg))

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Callable, Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from PIL import Image as PILImage
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer

from config import AppConfig
from chaoxing_client import ExportCancelledError, normalize_image_url, sanitize_filename

LogCallback = Optional[Callable[[str], None]]

DEFAULT_CJK_FONT_CANDIDATES = [
    r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\Deng.ttf',
    r'C:\Windows\Fonts\simhei.ttf',
    r'C:\Windows\Fonts\simsun.ttc',
    r'C:\Windows\Fonts\simkai.ttf',
    '/System/Library/Fonts/PingFang.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
]

_XML_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def _clean_xml_text(text: str) -> str:
    if not text:
        return ''
    return _XML_CTRL_RE.sub('', str(text))


def _log(log_cb: LogCallback, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def _collect_image_urls(questions: List[dict]) -> List[str]:
    urls: List[str] = []
    seen = set()
    for q in questions:
        for u in q.get('question_images') or []:
            if u and normalize_image_url(u) and u not in seen:
                seen.add(u)
                urls.append(u)
        for opt_imgs in q.get('option_images') or []:
            for u in opt_imgs or []:
                if u and normalize_image_url(u) and u not in seen:
                    seen.add(u)
                    urls.append(u)
        for u in q.get('answer_images') or []:
            if u and normalize_image_url(u) and u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


def download_images_for_questions(
    questions: List[dict],
    images_dir: str,
    session,
    max_width_px: int = 1200,
    timeout: int = 20,
    log_cb: LogCallback = None,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, str]:
    """下载题库中引用的图片到本地 images_dir，并统一转为 Word/PDF 友好的 PNG/JPEG。"""
    urls = _collect_image_urls(questions)
    if not urls:
        return {}

    os.makedirs(images_dir, exist_ok=True)
    url_map: Dict[str, str] = {}
    _log(log_cb, f'检测到 {len(urls)} 张题目图片，正在下载到 {images_dir} ...')

    for idx, url in enumerate(urls, 1):
        if stop_event and stop_event.is_set():
            raise ExportCancelledError('用户已停止导出任务')
        digest = hashlib.md5(url.encode('utf8')).hexdigest()
        existing_png = os.path.join(images_dir, f'{digest}.png')
        existing_jpg = os.path.join(images_dir, f'{digest}.jpg')
        if os.path.exists(existing_png):
            url_map[url] = existing_png
            continue
        if os.path.exists(existing_jpg):
            url_map[url] = existing_jpg
            continue

        try:
            fetch_url = normalize_image_url(url)
            if not fetch_url:
                continue
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://mooc1.chaoxing.com/',
            }
            resp = session.get(fetch_url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            img = PILImage.open(io.BytesIO(resp.content))
            img.load()

            if max_width_px and max_width_px > 0 and img.width > max_width_px:
                new_h = max(1, int(img.height * (max_width_px / img.width)))
                img = img.resize((max_width_px, new_h), PILImage.Resampling.LANCZOS)

            has_alpha = img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)
            if has_alpha:
                out_path = existing_png
                if img.mode != 'RGBA':
                    img = img.convert('RGBA')
                img.save(out_path, format='PNG', optimize=True)
            else:
                out_path = existing_jpg
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(out_path, format='JPEG', quality=92)
            url_map[url] = out_path
        except Exception as exc:
            _log(log_cb, f'图片下载失败 ({idx}/{len(urls)}): {url} -> {exc}')

    _log(log_cb, f'图片本地化完成：成功 {len(url_map)}/{len(urls)} 张')
    return url_map


def _image_ref_text(url: str, image_map: Dict[str, str], base_dir: str) -> str:
    local = image_map.get(url)
    if local and os.path.exists(local):
        rel = os.path.relpath(local, base_dir).replace('\\', '/')
        return rel
    return url


def _format_answer_text(q: dict) -> str:
    ans = (q.get('answer') or '').strip()
    if ans:
        return ans
    if q.get('answer_images'):
        return '(见下方图片)'
    return '(题库未提供参考答案)'


def _render_txt_content(
    questions: List[dict],
    cfg: AppConfig,
    image_map: Dict[str, str],
    base_dir: str,
    hide_answer: bool = False,
) -> str:
    lines: List[str] = []
    for idx, q in enumerate(questions, 1):
        q_type = q.get('type', '')
        header = f'{idx}.'
        if q_type:
            header += f' [{q_type}]'
        if cfg.include_chapter_tag and q.get('chapter'):
            header += f" ({q['chapter']})"
        lines.append(header)

        q_text = q.get('question', '')
        if q_text:
            lines.append(q_text)
        for img_u in q.get('question_images') or []:
            lines.append(f"[图片: {_image_ref_text(img_u, image_map, base_dir)}]")

        opt_imgs_list = q.get('option_images') or []
        for o_idx, opt in enumerate(q.get('options') or []):
            lines.append(f'  {opt}')
            if o_idx < len(opt_imgs_list):
                for img_u in opt_imgs_list[o_idx] or []:
                    lines.append(f"  [图片: {_image_ref_text(img_u, image_map, base_dir)}]")

        if not hide_answer:
            lines.append(f'正确答案: {_format_answer_text(q)}')
            for img_u in q.get('answer_images') or []:
                lines.append(f"[答案图片: {_image_ref_text(img_u, image_map, base_dir)}]")
            if cfg.include_analysis and q.get('analysis'):
                lines.append(f"信息: {q['analysis']}")
        else:
            lines.append('我的作答: __________')
        lines.append('')
    return '\n'.join(lines)


def _render_markdown_content(
    title: str,
    questions: List[dict],
    cfg: AppConfig,
    image_map: Dict[str, str],
    base_dir: str,
    hide_answer: bool = False,
) -> str:
    suffix = '（无答案刷题版）' if hide_answer else ''
    lines: List[str] = [f'# {title}{suffix}', '', f'> 共 {len(questions)} 道题', '']
    last_chap = None
    for idx, q in enumerate(questions, 1):
        chap = q.get('chapter') or ''
        if cfg.include_chapter_tag and chap and chap != last_chap:
            lines.append(f'## {chap}')
            lines.append('')
            last_chap = chap

        q_type = q.get('type', '')
        prefix = f'**{idx}.' + (f' [{q_type}]** ' if q_type else '** ')
        q_text = q.get('question', '').replace('\n', '  \n')
        lines.append(f'{prefix}{q_text}')
        lines.append('')

        for img_u in q.get('question_images') or []:
            ref = _image_ref_text(img_u, image_map, base_dir)
            lines.append(f'![题目图片]({ref})')
            lines.append('')

        opt_imgs_list = q.get('option_images') or []
        if q.get('options'):
            for o_idx, opt in enumerate(q.get('options') or []):
                lines.append(f'- {opt}')
                if o_idx < len(opt_imgs_list):
                    for img_u in opt_imgs_list[o_idx] or []:
                        ref = _image_ref_text(img_u, image_map, base_dir)
                        lines.append(f'  ![选项图片]({ref})')
            lines.append('')

        if not hide_answer:
            ans = _format_answer_text(q).replace('\n', '  \n')
            lines.append(f'> **正确答案**：{ans}')
            for img_u in q.get('answer_images') or []:
                ref = _image_ref_text(img_u, image_map, base_dir)
                lines.append(f'> ![答案图片]({ref})')
            if cfg.include_analysis and q.get('analysis'):
                ana = q['analysis'].replace('\n', ' ')
                lines.append(f'> *信息*：{ana}')
            lines.append('')
        lines.append('---')
        lines.append('')
    return '\n'.join(lines)


def _add_docx_image_or_link(doc: Document, url: str, image_map: Dict[str, str], max_inches: float = 5.5):
    local = image_map.get(url)
    if local and os.path.exists(local):
        try:
            with PILImage.open(local) as im:
                w_px = im.width
            width_in = min(max_inches, max(1.2, w_px / 110.0))
            p = doc.add_paragraph()
            p.add_run().add_picture(local, width=Inches(width_in))
            return
        except Exception:
            pass
    p = doc.add_paragraph(_clean_xml_text(f'[图片链接: {url}]'))
    if p.runs:
        p.runs[0].font.color.rgb = RGBColor(0x55, 0x77, 0xAA)


def write_docx_file(
    path: str,
    title: str,
    questions: List[dict],
    cfg: AppConfig,
    image_map: Dict[str, str],
    hide_answer: bool = False,
) -> None:
    doc = Document()
    normal_style = doc.styles['Normal']
    normal_style.font.name = '微软雅黑'
    normal_style.font.size = Pt(10.5)
    normal_style._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    suffix = '（无答案刷题版）' if hide_answer else ''
    doc.add_heading(_clean_xml_text(f'{title}{suffix}'), level=1)
    doc.add_paragraph(f'共 {len(questions)} 道题')

    last_chap = None
    for idx, q in enumerate(questions, 1):
        chap = q.get('chapter') or ''
        if cfg.include_chapter_tag and chap and chap != last_chap:
            doc.add_heading(_clean_xml_text(chap), level=2)
            last_chap = chap

        q_type = q.get('type', '')
        p_q = doc.add_paragraph()
        p_q.paragraph_format.space_before = Pt(6)
        p_q.paragraph_format.space_after = Pt(2)
        run_num = p_q.add_run(_clean_xml_text(f'{idx}. ' + (f'[{q_type}] ' if q_type else '')))
        run_num.bold = True
        p_q.add_run(_clean_xml_text(q.get('question', '')))

        for img_u in q.get('question_images') or []:
            _add_docx_image_or_link(doc, img_u, image_map)

        opt_imgs_list = q.get('option_images') or []
        for o_idx, opt in enumerate(q.get('options') or []):
            p_opt = doc.add_paragraph(_clean_xml_text(f'    {opt}'))
            p_opt.paragraph_format.space_after = Pt(1)
            if o_idx < len(opt_imgs_list):
                for img_u in opt_imgs_list[o_idx] or []:
                    _add_docx_image_or_link(doc, img_u, image_map, max_inches=4.5)

        if not hide_answer:
            p_ans = doc.add_paragraph()
            p_ans.paragraph_format.space_after = Pt(2)
            r_lbl = p_ans.add_run('正确答案: ')
            r_lbl.bold = True
            r_lbl.font.color.rgb = RGBColor(0x0B, 0x7A, 0x3E)
            r_val = p_ans.add_run(_clean_xml_text(_format_answer_text(q)))
            r_val.font.color.rgb = RGBColor(0x0B, 0x7A, 0x3E)

            for img_u in q.get('answer_images') or []:
                _add_docx_image_or_link(doc, img_u, image_map)

            if cfg.include_analysis and q.get('analysis'):
                p_ana = doc.add_paragraph(_clean_xml_text(f"信息: {q['analysis']}"))
                if p_ana.runs:
                    p_ana.runs[0].font.size = Pt(9.5)
                    p_ana.runs[0].font.color.rgb = RGBColor(0x66, 0x66, 0x66)

    doc.save(path)


_REGISTERED_PDF_FONTS: Dict[str, str] = {}


def _ensure_pdf_cjk_font(preferred_path: str = '') -> str:
    candidates = []
    if preferred_path and os.path.exists(preferred_path):
        candidates.append(preferred_path)
    candidates.extend(DEFAULT_CJK_FONT_CANDIDATES)

    for path in candidates:
        if not os.path.exists(path):
            continue
        abs_path = os.path.abspath(path)
        if abs_path in _REGISTERED_PDF_FONTS:
            return _REGISTERED_PDF_FONTS[abs_path]
        try:
            font_name = f"QuizKit_CJK_{hashlib.md5(abs_path.encode('utf8')).hexdigest()[:8]}"
            if abs_path.lower().endswith('.ttc'):
                pdfmetrics.registerFont(TTFont(font_name, abs_path, subfontIndex=0))
            else:
                pdfmetrics.registerFont(TTFont(font_name, abs_path))
            _REGISTERED_PDF_FONTS[abs_path] = font_name
            return font_name
        except Exception:
            continue
    raise RuntimeError('未找到可用的系统中文字体文件（请在 config.yml 中指定 pdf_font_path）')


def _rl_safe_text(text: str) -> str:
    return xml_escape(_clean_xml_text(text)).replace('\n', '<br/>')


def _append_pdf_image_or_link(story: list, url: str, image_map: Dict[str, str], style_link: ParagraphStyle, max_w: float, max_h: float = 280):
    local = image_map.get(url)
    if local and os.path.exists(local):
        try:
            with PILImage.open(local) as im:
                iw, ih = im.size
            if iw > 0 and ih > 0:
                scale = min(max_w / iw, max_h / ih, 0.85)
                w_pt = max(24, iw * scale)
                h_pt = max(24, ih * scale)
                story.append(RLImage(local, width=w_pt, height=h_pt))
                story.append(Spacer(1, 3))
                return
        except Exception:
            pass
    story.append(Paragraph(f'[图片: {_rl_safe_text(url)}]', style_link))


def write_pdf_file(
    path: str,
    title: str,
    questions: List[dict],
    cfg: AppConfig,
    image_map: Dict[str, str],
    hide_answer: bool = False,
) -> None:
    font_name = _ensure_pdf_cjk_font(cfg.pdf_font_path)

    margin = 16 * mm
    usable_w = A4[0] - 2 * margin
    doc = SimpleDocTemplate(
        path,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title=title,
    )

    s_title = ParagraphStyle('QuizKitTitle', fontName=font_name, fontSize=16, leading=22, spaceAfter=8, wordWrap='CJK')
    s_chap = ParagraphStyle('QuizKitChap', fontName=font_name, fontSize=13, leading=18, spaceBefore=10, spaceAfter=6, textColor=HexColor('#1D4ED8'), wordWrap='CJK')
    s_q = ParagraphStyle('QuizKitQ', fontName=font_name, fontSize=10.5, leading=15, spaceBefore=6, spaceAfter=2, wordWrap='CJK')
    s_opt = ParagraphStyle('QuizKitOpt', fontName=font_name, fontSize=10, leading=14, leftIndent=14, spaceAfter=1, wordWrap='CJK')
    s_ans = ParagraphStyle('QuizKitAns', fontName=font_name, fontSize=10, leading=14, textColor=HexColor('#0B7A3E'), spaceAfter=2, wordWrap='CJK')
    s_ana = ParagraphStyle('QuizKitAna', fontName=font_name, fontSize=9, leading=13, textColor=HexColor('#666666'), spaceAfter=4, wordWrap='CJK')
    s_link = ParagraphStyle('QuizKitLink', fontName=font_name, fontSize=9, leading=13, textColor=HexColor('#3B82F6'), spaceAfter=2, wordWrap='CJK')

    suffix = '（无答案刷题版）' if hide_answer else ''
    story = [
        Paragraph(_rl_safe_text(f'{title}{suffix}'), s_title),
        Paragraph(_rl_safe_text(f'共 {len(questions)} 道题'), s_ana),
    ]

    last_chap = None
    for idx, q in enumerate(questions, 1):
        chap = q.get('chapter') or ''
        if cfg.include_chapter_tag and chap and chap != last_chap:
            story.append(Paragraph(_rl_safe_text(chap), s_chap))
            last_chap = chap

        q_type = q.get('type', '')
        q_head = f'{idx}. ' + (f'[{q_type}] ' if q_type else '')
        story.append(Paragraph(_rl_safe_text(q_head + q.get('question', '')), s_q))

        for img_u in q.get('question_images') or []:
            _append_pdf_image_or_link(story, img_u, image_map, s_link, usable_w)

        opt_imgs_list = q.get('option_images') or []
        for o_idx, opt in enumerate(q.get('options') or []):
            story.append(Paragraph(_rl_safe_text(opt), s_opt))
            if o_idx < len(opt_imgs_list):
                for img_u in opt_imgs_list[o_idx] or []:
                    _append_pdf_image_or_link(story, img_u, image_map, s_link, usable_w - 20)

        if not hide_answer:
            ans = _format_answer_text(q)
            story.append(Paragraph(f'正确答案: {_rl_safe_text(ans)}', s_ans))
            for img_u in q.get('answer_images') or []:
                _append_pdf_image_or_link(story, img_u, image_map, s_link, usable_w)
            if cfg.include_analysis and q.get('analysis'):
                story.append(Paragraph(f"信息: {_rl_safe_text(q['analysis'])}", s_ana))
        story.append(Spacer(1, 4))

    doc.build(story)


def _write_single_dataset(
    base_dir: str,
    stem: str,
    title: str,
    questions: List[dict],
    cfg: AppConfig,
    image_map: Dict[str, str],
    now_str: str,
) -> List[str]:
    generated: List[str] = []
    os.makedirs(base_dir, exist_ok=True)

    def _target_paths(ext: str, extra_tag: str = '') -> List[str]:
        primary_file = os.path.join(base_dir, f'{stem}{extra_tag}.{ext}')
        ts_file = os.path.join(base_dir, f'{stem}-{now_str}{extra_tag}.{ext}')
        if not cfg.allow_overwrite_files:
            return [ts_file]
        paths = [primary_file]
        if cfg.save_timestamp_backup and ts_file not in paths:
            paths.append(ts_file)
        return paths

    for fmt in cfg.export_formats:
        if fmt == 'json':
            payload = json.dumps(questions, indent=4, ensure_ascii=False)
            for p in _target_paths('json'):
                with open(p, 'w', encoding='utf8') as f:
                    f.write(payload)
                generated.append(p)

        elif fmt == 'txt':
            txt_ans = _render_txt_content(questions, cfg, image_map, base_dir, hide_answer=False)
            for p in _target_paths('txt'):
                with open(p, 'w', encoding='utf8') as f:
                    f.write(txt_ans)
                generated.append(p)
            if cfg.export_no_answer_edition:
                txt_no = _render_txt_content(questions, cfg, image_map, base_dir, hide_answer=True)
                for p in _target_paths('txt', '-无答案刷题版'):
                    with open(p, 'w', encoding='utf8') as f:
                        f.write(txt_no)
                    generated.append(p)

        elif fmt == 'md':
            md_ans = _render_markdown_content(title, questions, cfg, image_map, base_dir, hide_answer=False)
            for p in _target_paths('md'):
                with open(p, 'w', encoding='utf8') as f:
                    f.write(md_ans)
                generated.append(p)
            if cfg.export_no_answer_edition:
                md_no = _render_markdown_content(title, questions, cfg, image_map, base_dir, hide_answer=True)
                for p in _target_paths('md', '-无答案刷题版'):
                    with open(p, 'w', encoding='utf8') as f:
                        f.write(md_no)
                    generated.append(p)

        elif fmt == 'docx':
            for p in _target_paths('docx'):
                write_docx_file(p, title, questions, cfg, image_map, hide_answer=False)
                generated.append(p)
            if cfg.export_no_answer_edition:
                for p in _target_paths('docx', '-无答案刷题版'):
                    write_docx_file(p, title, questions, cfg, image_map, hide_answer=True)
                    generated.append(p)

        elif fmt == 'pdf':
            for p in _target_paths('pdf'):
                write_pdf_file(p, title, questions, cfg, image_map, hide_answer=False)
                generated.append(p)
            if cfg.export_no_answer_edition:
                for p in _target_paths('pdf', '-无答案刷题版'):
                    write_pdf_file(p, title, questions, cfg, image_map, hide_answer=True)
                    generated.append(p)

    return generated


def export_questions(
    course_id: str,
    course_name: str,
    questions: List[dict],
    cfg: AppConfig,
    session=None,
    scope_summary: str = '全部题目',
    log_cb: LogCallback = None,
    stop_event: Optional[threading.Event] = None,
) -> dict:
    """根据配置将题目导出为多种格式并写入历史记录，返回导出摘要。"""
    if not questions:
        return {'files': [], 'output_dir': os.path.abspath(cfg.save_path), 'count': 0}

    safe_name = sanitize_filename(course_name)
    now_dt = datetime.now()
    now_str = now_dt.strftime('%Y-%m-%d_%H-%M-%S')

    root_out = cfg.save_path or 'out'
    os.makedirs(root_out, exist_ok=True)

    target_dir = os.path.join(root_out, safe_name)
    os.makedirs(target_dir, exist_ok=True)

    image_map: Dict[str, str] = {}
    if cfg.download_images and session is not None:
        images_dir = os.path.join(target_dir, 'images')
        image_map = download_images_for_questions(
            questions,
            images_dir=images_dir,
            session=session,
            max_width_px=cfg.image_max_width_px,
            timeout=cfg.request_timeout,
            log_cb=log_cb,
            stop_event=stop_event,
        )

    generated_files: List[str] = []
    if not cfg.bulk_files:
        stem = safe_name
        generated_files.extend(
            _write_single_dataset(target_dir, stem, course_name, questions, cfg, image_map, now_str)
        )
    else:
        groups: OrderedDict[str, List[dict]] = OrderedDict()
        split_key = 'chapter' if cfg.bulk_split_by == 'chapter' else 'type'
        for q in questions:
            g_name = (q.get(split_key) or '未分类').strip() or '未分类'
            groups.setdefault(g_name, []).append(q)
        for idx, (g_name, g_questions) in enumerate(groups.items(), 1):
            safe_group = sanitize_filename(g_name)
            stem = f'{idx:02d}_{safe_group}'
            generated_files.extend(
                _write_single_dataset(target_dir, stem, f'{course_name} - {g_name}', g_questions, cfg, image_map, now_str)
            )

    abs_files = [os.path.abspath(p) for p in generated_files]
    primary_path = abs_files[0] if len(abs_files) == 1 else os.path.abspath(target_dir)

    record = {
        'time': now_dt.strftime('%Y-%m-%d %H:%M:%S'),
        'course_id': str(course_id),
        'course_name': course_name,
        'scope': scope_summary,
        'count': len(questions),
        'formats': list(cfg.export_formats),
        'bulk_files': bool(cfg.bulk_files),
        'primary_path': primary_path,
        'files': abs_files,
    }
    append_history(root_out, record)
    _log(log_cb, f'所有格式导出完成，共生成 {len(abs_files)} 个文件 -> {primary_path}')

    return {
        'files': abs_files,
        'primary_path': primary_path,
        'output_dir': os.path.abspath(target_dir),
        'count': len(questions),
        'record': record,
    }


def history_file_path(save_path: str) -> str:
    return os.path.join(save_path or 'out', 'history.json')


def load_history(save_path: str) -> List[dict]:
    path = history_file_path(save_path)
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def append_history(save_path: str, record: dict) -> None:
    os.makedirs(save_path or 'out', exist_ok=True)
    history = load_history(save_path)
    history.insert(0, record)
    history = history[:200]
    path = history_file_path(save_path)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)

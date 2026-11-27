from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from functools import wraps
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad
from loguru import logger
from lxml.etree import HTML
from requests import Session

LogCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[int, int, str], None]]


def get_time(length: int = 10) -> str:
    return str(time.time()).replace('.', '')[:length]


_WINDOWS_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """移除 Windows 文件名中不允许的字符与控制字符、规避保留名并限制最大长度。"""
    cleaned = re.sub(r'[\x00-\x1f\x7f\\/:*?"<>|]+', '_', name or '').strip(' .')
    if max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[:max_len].strip(' .')
    if not cleaned:
        return 'unknown_course'
    if cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = f'_{cleaned}'
    return cleaned


def normalize_text(text: str) -> str:
    """规范化学习通 HTML 文本中的空白与不间断空格。"""
    if not text:
        return ''
    text = text.replace('\xa0', ' ').replace('\u3000', ' ')
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


DEFAULT_IMAGE_BASE_URL = 'https://mooc1.chaoxing.com/'


def normalize_image_url(src: str, base_url: str = DEFAULT_IMAGE_BASE_URL) -> str:
    """过滤编辑器装饰图标并将相对图片路径补全为完整 URL。"""
    s = (src or '').strip()
    if not s:
        return ''
    low = s.lower()
    if low.startswith(('javascript:', 'about:', 'data:')):
        return ''
    if 'filetypeimages/' in low or ('/editor' in low and '/dialogs/attachment' in low):
        return ''
    if s.startswith('//'):
        return 'https:' + s
    if not re.match(r'^https?://', s, re.I):
        return urljoin(base_url, s)
    return s


def extract_elem_content(elem) -> tuple[str, List[str]]:
    """提取节点的纯文本和去重后的图片链接列表。"""
    if elem is None:
        return '', []
    raw_text = ''.join(elem.xpath('.//text()'))
    text = normalize_text(raw_text)
    imgs: List[str] = []
    seen = set()
    for src in elem.xpath('.//img/@src'):
        s = normalize_image_url(src)
        if s and s not in seen:
            seen.add(s)
            imgs.append(s)
    return text, imgs


def _emit_log(log_cb: LogCallback, message: str) -> None:
    logger.info(message)
    if log_cb:
        log_cb(message)


def _emit_progress(progress_cb: ProgressCallback, done: int, total: int, message: str) -> None:
    if progress_cb:
        progress_cb(done, total, message)


class ExportCancelledError(RuntimeError):
    """用户主动取消导出任务时抛出。"""


class ChaoxingClient:
    def __init__(self, timeout: int = 20, proxy: str = ''):
        self.default_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        self.client = Session()
        self.timeout = int(timeout) if timeout and timeout > 0 else 20
        self.proxies: Dict[str, str] = {}
        self.set_proxy(proxy)
        self.exam_base = 'https://mooc1.chaoxing.com/exam-ans'

    def set_proxy(self, proxy: str):
        p = (proxy or '').strip()
        if p:
            self.proxies = {'http': p, 'https': p}
        else:
            self.proxies = {}
        self.client.proxies = dict(self.proxies)

    @wraps(requests.get)
    def get(self, url, **kwargs):
        temp_headers = self.default_headers.copy()
        temp_headers.update(kwargs['headers'] if 'headers' in kwargs else {})
        kwargs['headers'] = temp_headers
        kwargs.setdefault('proxies', self.proxies)
        kwargs.setdefault('timeout', self.timeout)
        return self.client.get(url, **kwargs)

    @wraps(requests.post)
    def post(self, url, **kwargs):
        temp_headers = self.default_headers.copy()
        temp_headers.update(kwargs['headers'] if 'headers' in kwargs else {})
        kwargs['headers'] = temp_headers
        kwargs.setdefault('proxies', self.proxies)
        kwargs.setdefault('timeout', self.timeout)
        return self.client.post(url, **kwargs)

    def login(self, username: str, password: str) -> str:
        """登录超星学习通，不保存任何凭证到本地。"""
        def encrypt(text: str) -> str:
            key = b'u2oh6Vu^'
            pad_pkcs7 = pad(text.encode(), DES.block_size, style='pkcs7')
            des = DES.new(key, DES.MODE_ECB)
            return des.encrypt(pad_pkcs7).hex()

        api = 'https://passport2.chaoxing.com/fanyalogin'
        data = {
            'fid': '-1',
            'uname': username.strip(),
            'password': encrypt(password),
            'refer': 'https%3A%2F%2Fi.chaoxing.com',
            't': 'true',
            'forbidotherlogin': '0',
            'validate': None,
        }
        resp = self.post(api, data=data)
        resp.raise_for_status()
        res_json = resp.json()
        logger.debug('login response status={}', res_json.get('status'))
        if not res_json.get('status'):
            err_msg = res_json.get('msg2') or res_json.get('msg') or '登录失败，请检查手机号或密码'
            raise RuntimeError(err_msg)
        cookies = '; '.join(f'{k}={v}' for k, v in self.client.cookies.get_dict().items())
        return cookies

    def get_mooc2_courses(self):
        """获取学习通个人课程列表（MOOC2）。"""
        url = 'https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courses/list?v=2&start=0&size=500&catalogId=0&superstarClass=0'
        resp = self.get(url)
        resp.raise_for_status()
        html = HTML(resp.text)
        courses = []
        seen = set()
        for a in html.xpath('//div[contains(@class, "course")]//a[@href]'):
            href = a.attrib.get('href', '')
            if 'courseid=' not in href.lower():
                continue
            name = normalize_text(''.join(a.xpath('.//span[contains(@class, "course-name")]//text()')))
            if not name:
                continue
            qs = parse_qs(urlparse(href).query)
            course_id = (qs.get('courseid') or qs.get('courseId') or [''])[0]
            class_id = (qs.get('clazzid') or qs.get('classId') or [''])[0]
            cpi = (qs.get('cpi') or [''])[0]
            if not course_id or not class_id:
                continue
            key = (course_id, class_id)
            if key in seen:
                continue
            seen.add(key)
            courses.append((course_id, class_id, cpi, name))
        return courses

    def get_all_courses(self):
        """获取当前账号加入的全部学习通课程列表。"""
        items = []
        for cid, clzid, cpi, cname in self.get_mooc2_courses():
            items.append({
                'course_id': str(cid),
                'class_id': str(clzid),
                'cpi': str(cpi),
                'name': cname,
                'qbank_count': None,
                'qbank_label': None,
            })
        return items

    # -------------------- MOOC2 课程自测接口 --------------------

    def get_mooc2_course_tokens(self, course_id, class_id, cpi):
        """进入学生课程页并提取 openc、examEnc、t 等自测所需参数。"""
        stu_url = (
            f'https://mooc1.chaoxing.com/visit/stucoursemiddle'
            f'?courseid={course_id}&clazzid={class_id}&cpi={cpi}&ismooc2=1'
        )
        resp = self.get(stu_url)
        resp.raise_for_status()
        html = HTML(resp.text)

        def get_val(elem_id):
            vals = html.xpath(f'//input[@id="{elem_id}"]/@value')
            return vals[0] if vals else ''

        openc = get_val('openc')
        exam_enc = get_val('examEnc')
        t = get_val('t') or get_time(13)
        if not openc or not exam_enc:
            raise RuntimeError(f'无法提取课程 {course_id} 的 openc 或 examEnc，可能不是有效课程页')
        return openc, exam_enc, t

    def get_mooc2_qbank_count(self, course_id, class_id, cpi) -> int:
        resp = self.get(
            f'{self.exam_base}/mooc2/exam/exam-question-count',
            params={
                'courseId': course_id,
                'classId': class_id,
                'cpi': cpi,
                'createType': '0',
                'doNoRepeat': 'false',
            },
        )
        resp.raise_for_status()
        return int(resp.json().get('count', 0))

    def get_mooc2_qbank_dirs(self, course_id, class_id, cpi) -> List[dict]:
        resp = self.get(
            f'{self.exam_base}/exam/selftest-qbankdirs-new',
            params={
                'courseId': course_id,
                'classId': class_id,
                'cpi': cpi,
                'doNoRepeat': 'false',
                'isPc': 'true',
            },
        )
        resp.raise_for_status()
        if not resp.text.strip():
            return []
        html = HTML(resp.text)
        dirs = []
        top_lis = html.xpath('//div[contains(@class, "menuFile")]/ul/li')
        if not top_lis:
            top_lis = html.xpath('//ul/li')

        for top_li in top_lis:
            top_name = normalize_text(''.join(top_li.xpath('./div/p//text()')))
            leaf_inputs = top_li.xpath('.//input[contains(@class, "dirCheckNum")]')
            for inp in leaf_inputs:
                dirids_raw = inp.attrib.get('dirids', '[]')
                try:
                    dir_ids = json.loads(dirids_raw)
                except json.JSONDecodeError:
                    continue
                cnt_texts = inp.xpath('./following-sibling::span[1]/text()')
                cnt = int(cnt_texts[0].strip()) if cnt_texts and cnt_texts[0].strip().isdigit() else 0
                if cnt <= 0 or not dir_ids:
                    continue
                leaf_name = normalize_text(''.join(inp.xpath('../../p//text()')))
                dirs.append({
                    'chapter': top_name or '默认章节',
                    'name': leaf_name or str(dir_ids),
                    'dirs': dir_ids,
                    'count': cnt,
                })
        return dirs

    def get_mooc2_qbank_types(self, course_id, class_id, cpi) -> List[dict]:
        resp = self.get(
            f'{self.exam_base}/exam/selftest-qbanktypes',
            params={'isPc': 'true', 'courseId': course_id, 'classId': class_id, 'cpi': cpi, 'doNoRepeat': 'false'},
        )
        resp.raise_for_status()
        if not resp.text.strip():
            return []
        html = HTML(resp.text)
        items = []
        for inp in html.xpath('//input[contains(@class, "typeQueNum")]'):
            cnt_texts = inp.xpath('./following-sibling::span[1]/text()')
            cnt = int(cnt_texts[0].strip()) if cnt_texts and cnt_texts[0].strip().isdigit() else 0
            q_type = int(inp.attrib.get('data', 0))
            type_id = int(inp.attrib.get('typeid', inp.attrib.get('typeId', 0)))
            name = normalize_text(''.join(inp.xpath('../../p//text()')))
            if cnt > 0:
                items.append({'name': name, 'type': q_type, 'typeId': type_id, 'count': cnt})
        return items

    def get_mooc2_qbank_easy(self, course_id, class_id, cpi) -> List[dict]:
        resp = self.get(
            f'{self.exam_base}/exam/selftest-qbankeasy',
            params={'isPc': 'true', 'courseId': course_id, 'classId': class_id, 'cpi': cpi, 'doNoRepeat': 'false'},
        )
        resp.raise_for_status()
        if not resp.text.strip():
            return []
        html = HTML(resp.text)
        items = []
        for inp in html.xpath('//input[contains(@class, "easyNum")]'):
            cnt_texts = inp.xpath('./following-sibling::span[1]/text()')
            cnt = int(cnt_texts[0].strip()) if cnt_texts and cnt_texts[0].strip().isdigit() else 0
            easy = int(inp.attrib.get('data', 0))
            name = normalize_text(''.join(inp.xpath('../../p//text()')))
            if cnt > 0:
                items.append({'name': name, 'easy': easy, 'count': cnt})
        return items

    def preview_mooc2_course(self, course_id: str, class_id: str, cpi: str) -> dict:
        """获取 MOOC2 课程自测题库概览（总数、章节汇总、文件夹、题型、难度、tokens）。"""
        openc, exam_enc, t = self.get_mooc2_course_tokens(course_id, class_id, cpi)
        total = self.get_mooc2_qbank_count(course_id, class_id, cpi)
        dirs = self.get_mooc2_qbank_dirs(course_id, class_id, cpi) if total > 0 else []
        types = self.get_mooc2_qbank_types(course_id, class_id, cpi) if total > 0 else []
        easy = self.get_mooc2_qbank_easy(course_id, class_id, cpi) if total > 0 else []

        chapter_map: OrderedDict[str, dict] = OrderedDict()
        for d in dirs:
            cname = d['chapter']
            if cname not in chapter_map:
                chapter_map[cname] = {'name': cname, 'count': 0, 'dirs': []}
            chapter_map[cname]['count'] += d['count']
            chapter_map[cname]['dirs'].append(d)

        return {
            'total': total,
            'chapters': list(chapter_map.values()),
            'dirs': dirs,
            'types': types,
            'easy': easy,
            'capabilities': {
                'chapters': len(chapter_map) > 0,
                'types': len(types) > 0,
                'easy': len(easy) > 0,
            },
            'tokens': (openc, exam_enc, t),
        }

    def parse_mooc2_paper_questions(self, html_text: str, chapter_name: str = '') -> List[dict]:
        """从 reVersionPaperMarkContentNew 页面解析所有题目结构化信息（含独立图片列表与难度）。"""
        html = HTML(html_text)
        questions: List[dict] = []
        for qdiv in html.xpath('//div[contains(@class, "questionLi") and @data]'):
            q_data_id = qdiv.attrib.get('data', '')

            type_texts = qdiv.xpath('.//h3[contains(@class, "mark_name")]/span[contains(@class, "colorShallow")]/text()')
            q_type = type_texts[0].strip().strip('()（）') if type_texts else ''

            qt_nodes = qdiv.xpath('.//h3[contains(@class, "mark_name")]//span[contains(@class, "qtContent")]')
            if qt_nodes:
                stem, q_imgs = extract_elem_content(qt_nodes[0])
            else:
                h3_nodes = qdiv.xpath('.//h3[contains(@class, "mark_name")]')
                raw_stem, q_imgs = extract_elem_content(h3_nodes[0]) if h3_nodes else ('', [])
                stem = re.sub(r'^\d+\.\s*(?:\([^)]+\))?\s*', '', raw_stem).strip()

            options: List[str] = []
            option_images: List[List[str]] = []
            for li in qdiv.xpath('.//ul[contains(@class, "qtDetail")]/li'):
                opt_text, opt_imgs = extract_elem_content(li)
                if opt_text or opt_imgs:
                    options.append(opt_text)
                    option_images.append(opt_imgs)

            ans_parts: List[str] = []
            ans_imgs: List[str] = []
            seen_ans_imgs = set()
            for ans_node in qdiv.xpath('.//*[contains(@class, "rightAnswerContent")]'):
                a_txt, a_img_list = extract_elem_content(ans_node)
                if a_txt:
                    ans_parts.append(a_txt)
                for img_u in a_img_list:
                    if img_u not in seen_ans_imgs:
                        seen_ans_imgs.add(img_u)
                        ans_imgs.append(img_u)
            answer_str = '\n'.join(ans_parts)

            analysis_parts: List[str] = []
            difficulty = ''
            for ana_node in qdiv.xpath('.//*[contains(@class, "qtAnalysis")]'):
                t_str, _ = extract_elem_content(ana_node)
                if t_str:
                    analysis_parts.append(t_str)
            for diff_node in qdiv.xpath('.//div[contains(@class, "analysis")]'):
                t_str = normalize_text(''.join(diff_node.xpath('.//text()')))
                if t_str:
                    analysis_parts.append(t_str)
                    m_diff = re.search(r'[（(](易|中|难)[）)]', t_str)
                    if m_diff:
                        difficulty = m_diff.group(1)
            analysis_str = ' | '.join(analysis_parts)

            questions.append({
                'id': q_data_id,
                'chapter': chapter_name,
                'type': q_type,
                'difficulty': difficulty,
                'question': stem,
                'question_images': q_imgs,
                'options': options,
                'option_images': option_images,
                'answer': answer_str,
                'answer_images': ans_imgs,
                'analysis': analysis_str,
            })
        return questions

    def _cancel_pending_self_tasks(self, course_id, class_id, cpi):
        try:
            self.post(
                f'{self.exam_base}/mooc2/exam/cancel-pending-self-task',
                params={'courseId': course_id, 'classId': class_id, 'cpi': cpi, 'excludeIds': '0'},
            )
        except Exception as exc:
            logger.warning('清理挂起自测任务失败: {}', exc)

    def _delete_mooc2_selftest(self, t_id, class_id):
        try:
            resp = self.get(
                f'{self.exam_base}/exam/test/deleteTestNew',
                params={'id': t_id, 'classId': class_id, 'recyle': 1},
            )
            logger.debug('清理临时试卷 {}: {}', t_id, resp.text)
        except Exception as exc:
            logger.warning('删除临时试卷 {} 失败: {}', t_id, exc)

    def _find_created_test_in_list(self, course_id, class_id, cpi, openc, exam_enc, t, target_title):
        list_url = (
            f'{self.exam_base}/mooc2/exam/exam-list'
            f'?courseid={course_id}&clazzid={class_id}&cpi={cpi}&ut=s&t={t}&enc={exam_enc}&openc={openc}&type=1'
        )
        resp = self.get(list_url)
        resp.raise_for_status()
        html = HTML(resp.text)

        pattern = re.compile(rf"goTest\(['\"]?{re.escape(str(course_id))}['\"]?,(\d+),(\d+),")
        fallback = None
        for li in html.xpath('//li'):
            onclick = li.attrib.get('onclick', '') or ''.join(li.xpath('.//@onclick'))
            m = pattern.search(onclick)
            if not m:
                continue
            pair = (m.group(1), m.group(2))
            if fallback is None:
                fallback = pair
            li_text = ''.join(li.xpath('.//text()'))
            if target_title in li_text:
                return pair
        if fallback is not None:
            return fallback
        m = pattern.search(resp.text)
        if m:
            return m.group(1), m.group(2)
        return None, None

    def fetch_mooc2_selftest_batch(
        self,
        course_id,
        class_id,
        cpi,
        openc,
        exam_enc,
        t,
        title,
        select_type,
        question_num,
        select_dirs=None,
        select_types=None,
        select_easy=None,
        chapter_name='',
        max_retries=2,
        poll_interval=2.0,
        auto_clean=True,
        stop_event: Optional[threading.Event] = None,
    ) -> List[dict]:
        """串行创建临时自测卷 -> 轮询完成 -> 读取答案解析页 -> 自动删除临时试卷。"""
        payload = {
            'courseId': str(course_id),
            'classId': str(class_id),
            'cpi': str(cpi),
            'createType': '0',
            'limitTime': '',
            'openc': openc,
            'questionNum': str(question_num),
            'selectType': str(select_type),
            'selectDirs': json.dumps(select_dirs or [], ensure_ascii=False),
            'selectTypes': json.dumps(select_types or [], ensure_ascii=False),
            'selectEasy': json.dumps(select_easy or [], ensure_ascii=False),
            'selectTopics': '[]',
            'selectLabels': '[]',
            'doNoRepeat': 'false',
            'title': title,
            'selftestMode': '1',
            'recommendSet': '{}',
            'zykCourseId': '0',
            'zykEnc': '',
        }

        last_err = None
        attempts = max(int(max_retries), 1)
        for attempt in range(1, attempts + 1):
            if stop_event and stop_event.is_set():
                raise ExportCancelledError('用户已停止导出任务')
            t_id = None
            try:
                r_create = self.post(f'{self.exam_base}/mooc2/exam/create-self-test', data=payload)
                r_create.raise_for_status()
                cj = r_create.json()
                if not cj.get('status'):
                    msg = cj.get('msg', '')
                    if '正在生成' in msg or 'pending' in msg.lower():
                        self._cancel_pending_self_tasks(course_id, class_id, cpi)
                        time.sleep(1)
                        continue
                    raise RuntimeError(f'创建自测失败: {cj}')

                task_id = cj.get('taskId')
                paper_ok = False
                for _ in range(45):
                    if stop_event and stop_event.is_set():
                        self._cancel_pending_self_tasks(course_id, class_id, cpi)
                        raise ExportCancelledError('用户已停止导出任务')
                    time.sleep(max(float(poll_interval), 0.5))
                    r_stat = self.get(
                        f'{self.exam_base}/mooc2/exam/selftest-autopapertask-status',
                        params={'courseId': course_id, 'classId': class_id, 'cpi': cpi, 'taskId': task_id},
                    )
                    r_stat.raise_for_status()
                    sj = r_stat.json()
                    status = sj.get('taskStatus')
                    if status == 'ok':
                        paper_ok = True
                        break
                    if status in ('fail', 'invalid'):
                        raise RuntimeError(f'自测组卷任务异常: {sj}')

                if not paper_ok:
                    raise TimeoutError(f'等待自测组卷超时 (taskId={task_id})')

                t_id, rel_id = self._find_created_test_in_list(course_id, class_id, cpi, openc, exam_enc, t, title)
                if not t_id or not rel_id:
                    raise RuntimeError(f'在自测列表中未找到刚创建的临时试卷 ({title})')

                if stop_event and stop_event.is_set():
                    raise ExportCancelledError('用户已停止导出任务')

                r_paper = self.get(
                    f'{self.exam_base}/exam/test/reVersionPaperMarkContentNew',
                    params={
                        'courseId': course_id,
                        'classId': class_id,
                        'p': '1',
                        'id': rel_id,
                        'ut': 's',
                        'cpi': cpi,
                        'newMooc': 'true',
                        'openc': openc,
                    },
                )
                r_paper.raise_for_status()
                return self.parse_mooc2_paper_questions(r_paper.text, chapter_name=chapter_name)
            except ExportCancelledError:
                raise
            except Exception as exc:
                last_err = exc
                logger.warning('批次 [{}] 第 {} 次尝试失败: {}', title, attempt, exc)
                self._cancel_pending_self_tasks(course_id, class_id, cpi)
                time.sleep(2)
            finally:
                if auto_clean and t_id:
                    self._delete_mooc2_selftest(t_id, class_id)

        raise RuntimeError(f'批次 [{title}] 连续 {attempts} 次失败: {last_err}')

    @staticmethod
    def _question_fingerprint(q: dict):
        return (
            q.get('type', ''),
            q.get('question', ''),
            tuple(q.get('options', [])),
            q.get('answer', ''),
        )

    @staticmethod
    def _build_dir_batches(dirs: List[dict], max_batch_size: int):
        batches = []
        cur_chapter = None
        cur_items = []
        cur_sum = 0
        for d in dirs:
            cnt = min(d['count'], max_batch_size)
            if cur_items and (d['chapter'] != cur_chapter or cur_sum + cnt > max_batch_size):
                batches.append((cur_chapter, cur_items, cur_sum))
                cur_items = []
                cur_sum = 0
            cur_chapter = d['chapter']
            cur_items.append({'selectNum': cnt, 'dirs': d['dirs']})
            cur_sum += cnt
        if cur_items:
            batches.append((cur_chapter, cur_items, cur_sum))
        return batches

    def export_mooc2_selftest(
        self,
        course_id: str,
        class_id: Optional[str] = None,
        cpi: Optional[str] = None,
        course_name: Optional[str] = None,
        scope: Optional[dict] = None,
        preview_data: Optional[dict] = None,
        max_batch_size: int = 500,
        poll_interval: float = 2.0,
        max_retries: int = 2,
        include_unclassified_extra: bool = True,
        deduplicate: bool = False,
        auto_clean: bool = True,
        progress_cb: ProgressCallback = None,
        log_cb: LogCallback = None,
        stop_event: Optional[threading.Event] = None,
    ) -> List[dict]:
        """按指定范围导出 MOOC2 课程内自测题库，返回结构化题目列表。"""
        course_id = str(course_id)
        if not class_id or not cpi or not course_name:
            for cid, clzid, mcpi, cname in self.get_mooc2_courses():
                if cid == course_id:
                    class_id = class_id or clzid
                    cpi = cpi or mcpi
                    course_name = course_name or cname
                    break
        if not class_id or not cpi:
            raise RuntimeError(f'在当前账号的 MOOC2 课程列表中未找到课程 {course_id}')
        course_name = course_name or f'course_{course_id}'

        preview = preview_data or self.preview_mooc2_course(course_id, class_id, cpi)
        openc, exam_enc, t = preview['tokens']
        total_qbank = preview['total']
        dirs = preview['dirs']
        type_items = preview['types']
        easy_items = preview['easy']

        if total_qbank <= 0:
            _emit_log(log_cb, f'课程 [{course_name}] ({course_id}) 未开通自测或自测题库为 0 题，已跳过导出。')
            return []

        scope = scope or {'mode': 'all'}
        mode = scope.get('mode', 'all')
        run_tag = get_time(10)

        dir_batches = []
        direct_plans = []  # (label, select_type, num, kwargs, chapter_name)
        filter_types = None
        filter_easy = None
        run_unclassified_extra = False

        if mode == 'all':
            dir_batches = self._build_dir_batches(dirs, max_batch_size)
            run_unclassified_extra = include_unclassified_extra
            if not dirs:
                for item in easy_items:
                    num = min(item['count'], max_batch_size)
                    direct_plans.append((
                        f"难度_{item['name']}", '3', num,
                        {'select_easy': [{'selectNum': num, 'easy': item['easy']}]}, '',
                    ))
                for it in type_items:
                    num = min(it['count'], max_batch_size)
                    direct_plans.append((
                        f"题型_{it['name']}", '2', num,
                        {'select_types': [{'selectNum': num, 'type': it['type'], 'typeId': it['typeId']}]}, '',
                    ))
                if not direct_plans:
                    num = min(total_qbank, max_batch_size)
                    direct_plans.append(('随机抽题', '0', num, {}, ''))
                run_unclassified_extra = False

        elif mode == 'chapters':
            selected_chapters = set(scope.get('chapters') or [])
            chosen_dirs = [d for d in dirs if d['chapter'] in selected_chapters]
            dir_batches = self._build_dir_batches(chosen_dirs, max_batch_size)

        elif mode == 'dirs':
            selected_keys = {tuple(x) for x in (scope.get('dir_ids') or [])}
            chosen_dirs = [d for d in dirs if tuple(d['dirs']) in selected_keys]
            dir_batches = self._build_dir_batches(chosen_dirs, max_batch_size)

        elif mode == 'types':
            chosen_types = scope.get('types') or []
            if chosen_types:
                if sum(it['count'] for it in chosen_types) <= max_batch_size:
                    st_num = sum(it['count'] for it in chosen_types)
                    direct_plans.append((
                        '按所选题型合并', '2', st_num,
                        {'select_types': [{'selectNum': it['count'], 'type': it['type'], 'typeId': it['typeId']} for it in chosen_types]},
                        '',
                    ))
                elif all(it['count'] <= max_batch_size for it in chosen_types):
                    for it in chosen_types:
                        direct_plans.append((
                            f"题型_{it['name']}", '2', it['count'],
                            {'select_types': [{'selectNum': it['count'], 'type': it['type'], 'typeId': it['typeId']}]},
                            '',
                        ))
                else:
                    # 包含超过单批 500 题上限的题型：通过章节全量拉取后精准过滤
                    dir_batches = self._build_dir_batches(dirs, max_batch_size)
                    filter_types = {it['name'] for it in chosen_types}
                    if include_unclassified_extra:
                        run_unclassified_extra = True

        elif mode == 'easy':
            chosen_easy = scope.get('easy') or []
            if chosen_easy:
                if sum(it['count'] for it in chosen_easy) <= max_batch_size:
                    num = sum(it['count'] for it in chosen_easy)
                    direct_plans.append((
                        '按所选难度合并', '3', num,
                        {'select_easy': [{'selectNum': it['count'], 'easy': it['easy']} for it in chosen_easy]},
                        '',
                    ))
                elif all(it['count'] <= max_batch_size for it in chosen_easy):
                    for it in chosen_easy:
                        direct_plans.append((
                            f"难度_{it['name']}", '3', it['count'],
                            {'select_easy': [{'selectNum': it['count'], 'easy': it['easy']}]},
                            '',
                        ))
                else:
                    dir_batches = self._build_dir_batches(dirs, max_batch_size)
                    filter_easy = {it['name'] for it in chosen_easy}
                    if include_unclassified_extra:
                        run_unclassified_extra = True

        extra_plans = []
        if run_unclassified_extra:
            for item in easy_items:
                if item['easy'] in (0, 2):
                    num = min(item['count'], max_batch_size)
                    extra_plans.append((
                        f"补充_难度_{item['name']}", '3', num,
                        {'select_easy': [{'selectNum': num, 'easy': item['easy']}]}, '补充未归类',
                    ))
            small_types = [it for it in type_items if it['type'] in (4, 7)]
            if small_types and sum(it['count'] for it in small_types) <= max_batch_size:
                st_num = sum(it['count'] for it in small_types)
                extra_plans.append((
                    '补充_简答计算', '2', st_num,
                    {'select_types': [{'selectNum': it['count'], 'type': it['type'], 'typeId': it['typeId']} for it in small_types]},
                    '补充未归类',
                ))
            for it in type_items:
                if it['type'] == 2:
                    num = min(it['count'], max_batch_size)
                    extra_plans.append((
                        '补充_填空题', '2', num,
                        {'select_types': [{'selectNum': num, 'type': it['type'], 'typeId': it['typeId']}]},
                        '补充未归类',
                    ))

        total_steps = len(dir_batches) + len(direct_plans) + len(extra_plans)
        if total_steps == 0:
            _emit_log(log_cb, '当前筛选范围没有可抽取的批次，未抓取到题目。')
            return []

        _emit_log(log_cb, f'课程 [{course_name}] ({course_id}) 题库总量 {total_qbank}，共规划 {total_steps} 个抓取批次')

        all_questions: List[dict] = []
        seen_fps = set()
        step_done = 0

        def _append_questions(qs_list: List[dict], enforce_dedup: bool) -> int:
            added = 0
            for q in qs_list:
                if filter_types is not None and q.get('type') not in filter_types:
                    continue
                if filter_easy is not None and q.get('difficulty') not in filter_easy:
                    continue
                fp = self._question_fingerprint(q)
                if enforce_dedup and fp in seen_fps:
                    continue
                seen_fps.add(fp)
                all_questions.append(q)
                added += 1
            return added

        # 1. 目录分批抓取
        for idx, (chap, select_dirs, batch_cnt) in enumerate(dir_batches, 1):
            if stop_event and stop_event.is_set():
                raise ExportCancelledError('用户已停止导出任务')
            title = f'tmp_dir_{run_tag}_{idx}'
            _emit_log(log_cb, f'[{step_done + 1}/{total_steps}] 抓取章节 [{chap}] ({batch_cnt} 题)...')
            qs = self.fetch_mooc2_selftest_batch(
                course_id, class_id, cpi, openc, exam_enc, t,
                title=title, select_type='1', question_num=batch_cnt,
                select_dirs=select_dirs, chapter_name=chap,
                max_retries=max_retries, poll_interval=poll_interval,
                auto_clean=auto_clean, stop_event=stop_event,
            )
            added = _append_questions(qs, enforce_dedup=deduplicate)
            step_done += 1
            _emit_progress(progress_cb, step_done, total_steps, f'章节 [{chap}] 完成，累计 {len(all_questions)} 题')
            _emit_log(log_cb, f'章节 [{chap}] 返回 {len(qs)} 题，保留 {added} 题，累计 {len(all_questions)} 题')

        # 2. 按题型/难度直接抽取批次
        for idx, (label, s_type, num, kw, chap) in enumerate(direct_plans, 1):
            if stop_event and stop_event.is_set():
                raise ExportCancelledError('用户已停止导出任务')
            title = f'tmp_direct_{run_tag}_{idx}'
            _emit_log(log_cb, f'[{step_done + 1}/{total_steps}] 抓取 [{label}] ({num} 题)...')
            qs = self.fetch_mooc2_selftest_batch(
                course_id, class_id, cpi, openc, exam_enc, t,
                title=title, select_type=s_type, question_num=num,
                chapter_name=chap, max_retries=max_retries, poll_interval=poll_interval,
                auto_clean=auto_clean, stop_event=stop_event, **kw,
            )
            added = _append_questions(qs, enforce_dedup=deduplicate or (len(direct_plans) > 1))
            step_done += 1
            _emit_progress(progress_cb, step_done, total_steps, f'[{label}] 完成，累计 {len(all_questions)} 题')
            _emit_log(log_cb, f'[{label}] 返回 {len(qs)} 题，保留 {added} 题，累计 {len(all_questions)} 题')

        # 3. 补充未归类散题
        if extra_plans:
            need_extra = (mode != 'all') or (total_qbank > len(all_questions))
            for e_idx, (label, s_type, e_num, kw, chap) in enumerate(extra_plans, 1):
                if not need_extra:
                    step_done += 1
                    _emit_progress(progress_cb, step_done, total_steps, f'跳过 [{label}]')
                    continue
                if stop_event and stop_event.is_set():
                    raise ExportCancelledError('用户已停止导出任务')
                title = f'tmp_extra_{run_tag}_{e_idx}'
                _emit_log(log_cb, f'[{step_done + 1}/{total_steps}] 抓取 [{label}] ({e_num} 题)...')
                qs = self.fetch_mooc2_selftest_batch(
                    course_id, class_id, cpi, openc, exam_enc, t,
                    title=title, select_type=s_type, question_num=e_num,
                    chapter_name=chap, max_retries=max_retries, poll_interval=poll_interval,
                    auto_clean=auto_clean, stop_event=stop_event, **kw,
                )
                added = _append_questions(qs, enforce_dedup=True)
                step_done += 1
                _emit_progress(progress_cb, step_done, total_steps, f'[{label}] 新增 {added} 题，累计 {len(all_questions)} 题')
                _emit_log(log_cb, f'[{label}] 返回 {len(qs)} 题，新增 {added} 题，累计 {len(all_questions)} 题')

        return all_questions

    # -------------------- 统一课程调度入口 --------------------

    def preview_course(self, course: dict) -> dict:
        """获取课程自测题库概览与可筛选范围。"""
        return self.preview_mooc2_course(course['course_id'], course['class_id'], course['cpi'])

    def scan_course_metric(self, course: dict) -> tuple[int, str]:
        """扫描单门课程的自测题量。"""
        cnt = self.get_mooc2_qbank_count(course['course_id'], course['class_id'], course['cpi'])
        return cnt, f'{cnt} 题'

    def export_course_selftest(
        self,
        course: dict,
        scope: Optional[dict] = None,
        preview_data: Optional[dict] = None,
        max_batch_size: int = 500,
        poll_interval: float = 2.0,
        max_retries: int = 2,
        include_unclassified_extra: bool = True,
        deduplicate: bool = False,
        auto_clean: bool = True,
        progress_cb: ProgressCallback = None,
        log_cb: LogCallback = None,
        stop_event: Optional[threading.Event] = None,
    ) -> List[dict]:
        """导出课程自测题库。"""
        return self.export_mooc2_selftest(
            course['course_id'],
            class_id=course.get('class_id'),
            cpi=course.get('cpi'),
            course_name=course.get('name'),
            scope=scope,
            preview_data=preview_data,
            max_batch_size=max_batch_size,
            poll_interval=poll_interval,
            max_retries=max_retries,
            include_unclassified_extra=include_unclassified_extra,
            deduplicate=deduplicate,
            auto_clean=auto_clean,
            progress_cb=progress_cb,
            log_cb=log_cb,
            stop_event=stop_event,
        )

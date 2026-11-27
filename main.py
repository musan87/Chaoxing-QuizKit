from __future__ import annotations

import argparse
import os
import sys
import traceback

from config import load_config


def _get_app_dir() -> str:
    """返回脚本或打包后可执行文件所在目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    parser = argparse.ArgumentParser(description='Chaoxing-QuizKit - 学习通题库导出工具箱')
    parser.add_argument('--config', default='config.yml', help='指定配置文件路径（默认 config.yml）')
    args = parser.parse_args()

    # 右键/双击打开时工作目录可能为 C:\Windows\System32，先切回程序所在目录
    app_dir = _get_app_dir()
    if app_dir and os.path.isdir(app_dir):
        os.chdir(app_dir)

    load_config(args.config)
    from tui_app import run_tui
    run_tui(config_path=args.config)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        try:
            input('\n程序异常退出，按回车键关闭窗口...')
        except EOFError:
            pass

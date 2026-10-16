"""
应用级配置（记住上次使用的目录与选项），纯标准库。

存放在 %LOCALAPPDATA%/PicHarvest/config.json。
不保存任何图片内容或 OCR 文本。
"""

from __future__ import annotations

import json
import os

from models import TaskConfig
from utils import file_utils
from utils.logger import get_logger

logger = get_logger('config')

DEFAULTS = {
    'last_root_dir': '',
    'last_output_dir': '',
    'last_result_dir': '',
    'window_geometry': '',
    'recent_roots': [],
    'has_seen_welcome': False,
}

MAX_RECENT = 8


class AppConfig:
    """简单的 JSON 配置存储。"""

    def __init__(self, path: str | None = None):
        self.path = path or file_utils.get_config_path()
        self.data = dict(DEFAULTS)
        self.load()

    # --------------------------------------------------

    def load(self) -> None:
        try:
            if os.path.isfile(self.path):
                with open(self.path, 'r', encoding='utf-8') as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    self.data.update(loaded)
        except (OSError, ValueError) as exc:
            logger.warning('读取配置失败（将使用默认值）：%s', exc)

    def save(self) -> None:
        try:
            file_utils.ensure_dir(os.path.dirname(os.path.abspath(self.path)))
            with open(self.path, 'w', encoding='utf-8') as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning('保存配置失败：%s', exc)

    # --------------------------------------------------

    def get(self, key: str, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value) -> None:
        self.data[key] = value

    def add_recent_root(self, path: str) -> None:
        if not path:
            return
        recent = [p for p in self.data.get('recent_roots', []) if p != path]
        recent.insert(0, path)
        self.data['recent_roots'] = recent[:MAX_RECENT]
        self.data['last_root_dir'] = path

    # -------------------------------------------------- 任务配置

    def load_task_config(self) -> TaskConfig:
        """从配置中恢复上次的任务设置。"""
        raw = self.data.get('task_config')
        if not raw:
            return TaskConfig()
        try:
            config = TaskConfig.from_dict(raw)
            if not config.rule.root_dir:
                config.rule.root_dir = self.get('last_root_dir', '')
            if not config.output.output_dir:
                config.output.output_dir = self.get('last_output_dir', '')
            return config
        except Exception as exc:
            logger.warning('恢复上次任务配置失败：%s', exc)
            return TaskConfig()

    def save_task_config(self, config: TaskConfig) -> None:
        try:
            self.data['task_config'] = config.to_dict()
            if config.rule.root_dir:
                self.data['last_root_dir'] = config.rule.root_dir
            if config.output.output_dir:
                self.data['last_output_dir'] = config.output.output_dir
            self.save()
        except Exception as exc:
            logger.warning('保存任务配置失败：%s', exc)


_instance: AppConfig | None = None


def get_config() -> AppConfig:
    global _instance
    if _instance is None:
        _instance = AppConfig()
    return _instance

"""配置加载：YAML + 默认值深合并。"""

import copy
import os

import yaml

DEFAULTS = {
    "emby": {
        "base_url": "http://127.0.0.1:8096",
        "api_key": "",
        "libraries": [],
        "item_types": ["Episode", "Movie"],
        "timeout": 30,
    },
    "paths": {
        "strm_roots": ["/media/strm"],
        "path_map": [],
        "emby_path_map": [],
        "data_dir": "/config/data",
        "glossary_dir": "/config/glossary",
    },
    "detect": {
        "zh_markers": ["zh", "chi", "chs", "cht", "zho", ".cn."],
        "min_extract_bytes": 2000,
        "exclude_series": [],
    },
    "translate": {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen3-8b-translate",
        "num_ctx": 4096,
        "batch_size": 10,
        "context_window": 2,
        "temperature": 0.2,
        "top_p": 0.9,
        "max_retries": 3,
        "request_timeout": 180,
        "per_item_budget_min": 60,
    },
    "schedule": {
        "window": ["01:00", "07:00"],
        "last_fire": "06:00",
        "scan_interval_min": 5,
        "rescanned_hours": 24,
    },
    "asr": {"enabled": False, "endpoint": ""},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str = None) -> dict:
    """加载配置；path 为空时依次尝试 ./config.yaml、/config/config.yaml。"""
    if not path:
        for cand in ("config.yaml", "/config/config.yaml"):
            if os.path.isfile(cand):
                path = cand
                break
    if not path or not os.path.isfile(path):
        raise SystemExit(
            "未找到配置文件。用 --config 指定，或把 config.example.yaml 复制为 config.yaml 后填写。"
        )
    with open(path, "r", encoding="utf-8") as f:
        user = yaml.safe_load(f) or {}
    return _deep_merge(DEFAULTS, user)

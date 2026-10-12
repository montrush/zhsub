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
        "probe_workers": 4,     # 扫描兜底 ffprobe 的并发数（Emby 元数据先行分类，探测只兜底）
        "fail_retry_hours": 24, # 探测/翻译类失败的冷却时长（小时），0=不冷却
    },
    "translate": {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen3-8b-translate",
        "api_key": "",        # 带鉴权的网关用；ollama 留空
        "num_gpu": None,      # ollama GPU 卸载层数；None=服务端自动
        # GPU 加速端点：启用后翻译/资料收集优先走它，失败自动回退 base_url
        "gpu": {
            "enabled": False,
            "base_url": "",        # 如 http://192.168.1.88:11435
            "model": "",           # 留空 = 用上面的 model
            "provider": "ollama",  # ollama | openai（OpenAI 兼容 /v1/chat/completions）
            "api_key": "",
            "num_gpu": None,
        },
        "num_ctx": 4096,
        "batch_size": 10,
        "context_window": 2,
        "temperature": 0.2,
        "top_p": 0.9,
        "max_retries": 3,
        "request_timeout": 180,
        "per_item_budget_min": 60,
        # 单次生成 token 上限；None=不设。批翻译 10 行+语境远用不满 2048，
        # 设上限防止模型偶发跑飞（无限循环生成）占死 GPU 队列（2026-10-12 夜译实测）
        "num_predict": 2048,
        # 1012 改默认 False：不关思考时 qwen3 单行字幕也生成 ~300 token（实测 316 vs 正常
        # ~25，整集慢 5-10×）。此前 think=false 偶发空回复（1011 实测）已由客户端
        # reasoning 字段兜底 + 批次重试 + 位置对齐修复覆盖。显式设 null = 跟随模型默认。
        "think": False,
    },
    "schedule": {
        "window": ["01:00", "07:00"],
        "windows": {},        # 分日覆盖 mon..sun；[] = 当天不干活；可跨零点 ["22:00","02:00"]
        "last_fire_min": 60,  # 距窗口结束 N 分钟不再开新集（0=干满全程；旧 last_fire 已废弃）
        "scan_interval_min": 5,
        "rescanned_hours": 24,
    },
    "research": {
        "enabled": True,      # 翻译前允许联网收集译名资料
        "auto": True,         # 缺词表时自动触发（pipeline 内）
        "proxy": "",          # 搜索/TMDB 代理，如 http://192.168.1.88:10900；留空直连
        "search_provider": "ddg",
        "tmdb_api_key": "",
        "max_rounds": 6,
        "num_ctx": 8192,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8085,
        "worker_thread": True,   # web 进程内随启夜窗 worker
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

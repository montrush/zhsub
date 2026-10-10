# 词表库（glossary library）

每部剧一个 JSON，**用「系列名 + TMDB 号」识别**。`research` 流程生成的词表、
手工整理的译名表都放这里（运行时目录由 `paths.glossary_dir` 指定，容器里一般是
`/config/glossary`；本目录是随仓库分发的种子词表，可复制/软链到运行目录）。

## 查找优先级

```
地狱厨房.tmdb2370.json   ← 1. 系列名 + TMDB 号（最准）
地狱厨房.json            ← 2. 仅系列名
_default.json            ← 3. 全局兜底
```

## 文件格式

```json
{
  "title": "地狱厨房",
  "tmdb_id": 2370,
  "terms": {
    "Hell's Kitchen": "地狱厨房",
    "Gordon Ramsay": "戈登·拉姆齐"
  },
  "notes": "实境秀竞技类：口语化、语气夸张，评委点评可带情绪词",
  "meta": {"source": "model | manual | tmdb_fallback"}
}
```

平铺格式 `{"原文": "译名", ...}` 也兼容（老词表直接拷进来就能用）。

## 生成方式

- WebUI「词表库」页 → 输入系列名 + TMDB 号 → 联网收集（qwen + TMDB/搜索，代理可在设置页配）
- 命令行：`zhsub research --series 地狱厨房 --tmdb 2370`
- 手工：照上面格式写，或把旧管线词表 json 改名放入

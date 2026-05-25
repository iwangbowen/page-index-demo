# 模式 A 完整提取过程：有目录、有页码、单级结构

> **场景**：`annual-report.pdf`，共 300 页，目录在第 4 页（物理），一级目录，含逻辑页码，逻辑页 1 = 物理页 5（偏移量 +4）。

---

## 场景文档结构

```
物理页 1  封面
物理页 2  版权页
物理页 3  前言
物理页 4  目录（TABLE OF CONTENTS）
物理页 5  Chapter 1 Introduction（逻辑页 1）
  ...
物理页 29 Chapter 2 Market Overview（逻辑页 25）
  ...
物理页 71 Chapter 3 Financial Performance（逻辑页 67）
  ...（共 9 章，至物理页 300）
```

目录页原文：
```
TABLE OF CONTENTS
Chapter 1  Introduction ........... 1
Chapter 2  Market Overview ........ 25
Chapter 3  Financial Performance .. 67
Chapter 4  Risk Analysis .......... 112
Chapter 5  Operations ............. 145
Chapter 6  Sustainability .......... 178
Chapter 7  Governance .............. 210
Chapter 8  Outlook ................. 245
Chapter 9  Appendix ................ 278
```

---

## Step 0 — 程序入口

**调用链**：`page_index()` → `page_index_main(doc, opt)` → `asyncio.run(page_index_builder())`

```python
def page_index_main(doc, opt=None):
    logger = JsonLogger(doc)
    page_list = get_page_tokens(doc, model=opt.model)  # 解析所有页面
    asyncio.run(page_index_builder())                   # 进入异步主流程
```

---

## Step 1 — PDF 文本提取

**函数**：`get_page_tokens(pdf_path, model, pdf_parser="PyPDF2")`
**文件**：`pageindex/utils.py`

```python
def get_page_tokens(pdf_path, model=None, pdf_parser="PyPDF2"):
    pdf_reader = PyPDF2.PdfReader(pdf_path)
    page_list = []
    for page_num in range(len(pdf_reader.pages)):
        page_text = pdf_reader.pages[page_num].extract_text()
        token_length = litellm.token_counter(model=model, text=page_text)
        page_list.append((page_text, token_length))
    return page_list
```

**输入**：`annual-report.pdf`（300 页）

**输出**：`page_list`，长度为 300 的列表，每个元素为 `(页面文本, token数)`

```python
page_list = [
    ("ANNUAL REPORT 2023\nA Global Company\n...", 312),     # page_list[0] = 物理第 1 页
    ("Copyright © 2023...\nAll rights reserved...", 189),   # page_list[1] = 物理第 2 页
    ("Preface\nThis report covers...", 445),                # page_list[2] = 物理第 3 页
    ("TABLE OF CONTENTS\n"                                  # page_list[3] = 物理第 4 页（目录）
     "Chapter 1  Introduction: 1\n"
     "Chapter 2  Market Overview: 25\n"
     ...
     "Chapter 9  Appendix: 278\n", 680),
    ("Chapter 1  Introduction\n...", 520),                  # page_list[4] = 物理第 5 页
    ...                                                     # page_list[5..299]
]
```

- **无 LLM 调用**，纯 PDF 解析
- PyPDF2（默认）或 PyMuPDF 可选（由 `pdf_parser` 参数决定）

---

## Step 2 — 目录页检测

**函数**：`check_toc(page_list, opt)` → `find_toc_pages(start_page_index=0, ...)` → `toc_extractor(...)`
**文件**：`pageindex/page_index.py`

### 2-1 顺序扫描目录页

```python
def find_toc_pages(start_page_index, page_list, opt, logger=None):
    last_page_is_yes = False
    toc_page_list = []
    i = start_page_index  # i=0 开始
    while i < len(page_list):
        if i >= opt.toc_check_page_num and not last_page_is_yes:  # 超过20页且未发现TOC就停止
            break
        detected_result = toc_detector_single_page(page_list[i][0], model=opt.model)
        if detected_result == 'yes':
            toc_page_list.append(i)
            last_page_is_yes = True
        elif detected_result == 'no' and last_page_is_yes:   # 早停：TOC结束后的第一个非TOC页
            break
        i += 1
    return toc_page_list
```

**LLM 调用**（顺序，每页一次）：

| 轮次 | 页（0-indexed） | 内容特征 | LLM 判断 | `last_page_is_yes` |
|------|---------------|---------|---------|---------------------|
| i=0 | 封面 | 无章节列表 | `no` | `False` |
| i=1 | 版权页 | 无章节列表 | `no` | `False` |
| i=2 | 前言 | 无章节列表 | `no` | `False` |
| i=3 | 目录页 | 含章节列表+页码 | `yes` | `True` → `toc_page_list=[3]` |
| i=4 | 正文第 1 页 | `no` 且 `last_page_is_yes=True` | → **早停** | — |

**输出**：`toc_page_list = [3]`（共 5 次 LLM 调用）

### 2-2 提取目录内容 + 判断有无页码

```python
def toc_extractor(page_list, toc_page_list, model):
    toc_content = ""
    for page_index in toc_page_list:          # 只有 page_index=3
        toc_content += page_list[page_index][0]
    toc_content = transform_dots_to_colon(toc_content)   # 将 ...... 替换为 :
    has_page_index = detect_page_index(toc_content, model=model)  # LLM判断是否含页码
    return {"toc_content": toc_content, "page_index_given_in_toc": has_page_index}
```

`detect_page_index` LLM Prompt（1 次调用）：
```
You will be given a table of contents. Detect if there are page numbers/indices.
Given text: TABLE OF CONTENTS\nChapter 1  Introduction: 1\n...
Reply: {"thinking": ..., "page_index_given_in_toc": "yes or no"}
```
→ 返回 `"yes"`

**`check_toc` 最终输出**：
```python
{
    "toc_content": "TABLE OF CONTENTS\n"
                   "Chapter 1  Introduction: 1\n"
                   "Chapter 2  Market Overview: 25\n"
                   "Chapter 3  Financial Performance: 67\n"
                   "Chapter 4  Risk Analysis: 112\n"
                   "Chapter 5  Operations: 145\n"
                   "Chapter 6  Sustainability: 178\n"
                   "Chapter 7  Governance: 210\n"
                   "Chapter 8  Outlook: 245\n"
                   "Chapter 9  Appendix: 278\n",
    "toc_page_list": [3],
    "page_index_given_in_toc": "yes"
}
```

---

## Step 3 — 模式 A：有页码目录处理

**进入**：`meta_processor(mode='process_toc_with_page_numbers', ...)`
**函数**：`process_toc_with_page_numbers(toc_content, toc_page_list, page_list, ...)`

### 3a — 目录文本 JSON 化

**函数**：`toc_transformer(toc_content)`

```python
def toc_transformer(toc_content, model=None):
    init_prompt = """
    Transform the whole table of contents into JSON.
    structure: "1" for first section, "1.1" for first subsection, etc.
    { "table_of_contents": [
        { "structure": "x.x.x", "title": ..., "page": <page number or None> },
        ...
    ]}"""
    prompt = init_prompt + '\n Given table of contents\n:' + toc_content
    last_complete, finish_reason = llm_completion(model=model, prompt=prompt, return_finish_reason=True)
    if_complete = check_if_toc_transformation_is_complete(toc_content, last_complete, model)
    if if_complete == "yes" and finish_reason == "finished":
        return convert_page_to_int(extract_json(last_complete)['table_of_contents'])
    # 若未完成，续写循环（最多 5 次）...
```

- 1 次 LLM 生成 JSON
- 1 次 LLM 验证完整性（`check_if_toc_transformation_is_complete`）
- 9 条目录一次输出完整 → 无需续写

**输出** `toc_with_page_number`（注意 structure 只有 `"1"~"9"`，无 `"1.1"` 子节点）：
```python
[
    {"structure": "1", "title": "Chapter 1  Introduction",          "page": 1},
    {"structure": "2", "title": "Chapter 2  Market Overview",       "page": 25},
    {"structure": "3", "title": "Chapter 3  Financial Performance", "page": 67},
    {"structure": "4", "title": "Chapter 4  Risk Analysis",         "page": 112},
    {"structure": "5", "title": "Chapter 5  Operations",            "page": 145},
    {"structure": "6", "title": "Chapter 6  Sustainability",        "page": 178},
    {"structure": "7", "title": "Chapter 7  Governance",            "page": 210},
    {"structure": "8", "title": "Chapter 8  Outlook",               "page": 245},
    {"structure": "9", "title": "Chapter 9  Appendix",              "page": 278},
]
```

### 3b — 去除页码，扫描正文找物理页码

```python
toc_no_page_number = remove_page_number(copy.deepcopy(toc_with_page_number))
# → 删除所有 page 字段

start_page_index = toc_page_list[-1] + 1  # = 3+1 = 4（0-indexed）
main_content = ""
for page_index in range(start_page_index, min(start_page_index + toc_check_page_num, len(page_list))):
    # page_index = 4, 5, ..., 23（最多扫 toc_check_page_num=20 页）
    main_content += f"<physical_index_{page_index+1}>\n{page_list[page_index][0]}\n<physical_index_{page_index+1}>\n\n"
```

构造的 `main_content`（带物理页标签的 20 页文本片段）：
```
<physical_index_5>
Chapter 1  Introduction
This report covers...
<physical_index_5>

<physical_index_6>
1.1 Company History...
<physical_index_6>

...（共 20 页，到 physical_index_24）...

<physical_index_24>
Chapter 2  Market Overview
Global market trends...
<physical_index_24>
```

**函数**：`toc_index_extractor(toc_no_page, main_content)` — 1 次 LLM 调用

```python
def toc_index_extractor(toc, content, model=None):
    prompt = """
    Add the physical_index to the TOC JSON.
    Pages contain tags like <physical_index_X> to indicate physical page location.
    Only add physical_index for sections that appear in the provided pages.
    Keep <physical_index_X> format."""
    prompt += '\nTable of contents:\n' + str(toc) + '\nDocument pages:\n' + content
    response = llm_completion(model=model, prompt=prompt)
    return extract_json(response)
```

**LLM 输出**（仅扫描前 20 页，只能看到 Ch1 和 Ch2 的起始位置）：
```python
[
    {"structure": "1", "title": "Chapter 1  Introduction",         "physical_index": "<physical_index_5>"},
    {"structure": "2", "title": "Chapter 2  Market Overview",      "physical_index": "<physical_index_24>"},
    {"structure": "3", "title": "Chapter 3  Financial Performance","physical_index": None},
    {"structure": "4", "title": "Chapter 4  Risk Analysis",        "physical_index": None},
    # ... Ch3~Ch9 超出扫描范围，physical_index = None
]
```

### 3c — 解析物理页码标签为整数

**函数**：`convert_physical_index_to_int()`
正则：`physical_index_(\d+)` → `int`

**输出**：
```python
[
    {"structure": "1", "title": "Chapter 1  Introduction",    "physical_index": 5},
    {"structure": "2", "title": "Chapter 2  Market Overview", "physical_index": 24},
    ...  # 其余 physical_index 仍为 None
]
```

### 3d — 配对并计算偏移量（众数投票）

**函数**：`extract_matching_page_pairs()` + `calculate_page_offset()`

```python
def calculate_page_offset(pairs):
    differences = []
    for pair in pairs:
        difference = pair['physical_index'] - pair['page']  # 物理页 - 逻辑页
        differences.append(difference)
    # 统计每个差值出现次数，取众数
    difference_counts = {}
    for diff in differences:
        difference_counts[diff] = difference_counts.get(diff, 0) + 1
    return max(difference_counts.items(), key=lambda x: x[1])[0]
```

**`pairs`（配对数据）**：
```python
[
    {"title": "Chapter 1  Introduction", "page": 1, "physical_index": 5},
    # 5 - 1 = 4 ✓
]
# 仅有 1 对有效数据（Ch2 的 physical_index=24 但 page=25，差值=-1，可能匹配出错）
# 众数 = 4，offset = 4
```

**输出**：`offset = 4`

### 3e — 批量应用偏移，计算全部物理页码

**函数**：`add_page_offset_to_toc_json(toc_with_page_number, offset=4)`

```python
def add_page_offset_to_toc_json(data, offset):
    for i in range(len(data)):
        if data[i].get('page') is not None and isinstance(data[i]['page'], int):
            data[i]['physical_index'] = data[i]['page'] + offset  # 逻辑页 + 4
            del data[i]['page']
    return data
```

**输出**（9 个章节全部通过 `逻辑页 + 4` 计算出物理页码）：
```python
[
    {"structure": "1", "title": "Chapter 1  Introduction",          "physical_index": 5},    # 1+4
    {"structure": "2", "title": "Chapter 2  Market Overview",       "physical_index": 29},   # 25+4
    {"structure": "3", "title": "Chapter 3  Financial Performance", "physical_index": 71},   # 67+4
    {"structure": "4", "title": "Chapter 4  Risk Analysis",         "physical_index": 116},  # 112+4
    {"structure": "5", "title": "Chapter 5  Operations",            "physical_index": 149},  # 145+4
    {"structure": "6", "title": "Chapter 6  Sustainability",        "physical_index": 182},  # 178+4
    {"structure": "7", "title": "Chapter 7  Governance",            "physical_index": 214},  # 210+4
    {"structure": "8", "title": "Chapter 8  Outlook",               "physical_index": 249},  # 245+4
    {"structure": "9", "title": "Chapter 9  Appendix",              "physical_index": 282},  # 278+4
]
```

`process_none_page_numbers()` 检查：全部条目均有 `physical_index`，无需额外处理。

---

## Step 4 — 越界校验

**函数**：`validate_and_truncate_physical_indices(toc, page_list_length=300, start_index=1)`
**文件**：`pageindex/page_index.py`

```python
max_allowed_page = page_list_length + start_index - 1  # = 300 + 1 - 1 = 300
for item in toc_with_page_number:
    if item['physical_index'] > max_allowed_page:
        item['physical_index'] = None   # 超出文档范围则置 None
```

**本场景**：最大物理页 282 ≤ 300，所有条目合法，无截断。

---

## Step 5 — 并发验证准确率

**函数**：`verify_toc(page_list, toc_with_page_number, start_index=1)`
**文件**：`pageindex/page_index.py`

```python
async def verify_toc(page_list, list_result, start_index=1, N=None, model=None):
    # 早停检查：最后一条 physical_index=282 >= 300/2=150 → 不早停
    last_physical_index = 282

    # N=None → 全量检查（9 条全部验证）
    tasks = [
        check_title_appearance(item, page_list, start_index, model)
        for item in indexed_sample_list
    ]
    results = await asyncio.gather(*tasks)   # 9 个 LLM 请求全并发
```

每条 LLM Prompt 格式：
```
Check if the given section appears or starts in the given page_text.
Section title: Chapter 3  Financial Performance
Page text: [page_list[70][0]]   ← 物理页 71 的文本
Reply: {"thinking": ..., "answer": "yes or no"}
```

**假设结果**：Chapter 4 的物理页码偏差 1 页（116 实际应为 115）

```python
accuracy = 0.89   # 8/9 正确
incorrect_results = [
    {"list_index": 3, "title": "Chapter 4  Risk Analysis", "page_number": 116}
]
```

---

## Step 6 — 定向修正错误条目

**函数**：`fix_incorrect_toc_with_retries(..., max_attempts=3)`
**文件**：`pageindex/page_index.py`

```python
async def process_and_check_item(incorrect_item):
    list_index = 3   # Chapter 4 在列表中的索引

    # 找前一个正确条目（list_index=2，Chapter 3，physical_index=71）
    prev_correct = 71

    # 找后一个正确条目（list_index=4，Chapter 5，physical_index=149）
    next_correct = 149

    # 拼接页面范围 [71, 149]，每页加物理页标签
    content_range = "".join([
        f"<physical_index_{i}>\n{page_list[i-1][0]}\n<physical_index_{i}>\n"
        for i in range(71, 150)
    ])

    # LLM 在该范围内精确定位 Chapter 4 起始物理页
    physical_index_int = await single_toc_item_index_fixer("Chapter 4  Risk Analysis", content_range)
    # → {"physical_index": "<physical_index_115>"} → int 115

    # 验证修正结果
    check_result = await check_title_appearance({..., 'physical_index': 115}, page_list, ...)
    # → {"answer": "yes"}
```

**修正后**：`toc_with_page_number[3]['physical_index'] = 115`（从 116 改为 115）

---

## Step 7 — 补充前言节点

**函数**：`add_preface_if_needed(toc_with_page_number)`
**文件**：`pageindex/utils.py`

```python
def add_preface_if_needed(data):
    if data[0]['physical_index'] > 1:   # 5 > 1 → True
        preface_node = {"structure": "0", "title": "Preface", "physical_index": 1}
        data.insert(0, preface_node)    # 在最前面插入前言节点，覆盖物理页 1-4
```

**输出**（10 条，新增了 Preface）：
```python
[
    {"structure": "0", "title": "Preface",                          "physical_index": 1},
    {"structure": "1", "title": "Chapter 1  Introduction",         "physical_index": 5},
    {"structure": "2", "title": "Chapter 2  Market Overview",      "physical_index": 29},
    ...
    {"structure": "9", "title": "Chapter 9  Appendix",             "physical_index": 282},
]
```

---

## Step 8 — 并发检测章节是否从页首开始

**函数**：`check_title_appearance_in_start_concurrent()`
**文件**：`pageindex/page_index.py`

```python
async def check_title_appearance_in_start_concurrent(structure, page_list, model=None, logger=None):
    tasks = []
    valid_items = []
    for item in structure:
        if item.get('physical_index') is not None:
            page_text = page_list[item['physical_index'] - 1][0]
            tasks.append(check_title_appearance_in_start(item['title'], page_text, model=model))
            valid_items.append(item)
    results = await asyncio.gather(*tasks)   # 10 个 LLM 请求全并发
```

每条 LLM Prompt：
```
Check if the current section starts in the BEGINNING of the page_text.
If there are other contents before the current section title, answer no.
Section title: Chapter 2  Market Overview
Page text: [物理页 29 的文本]
Reply: {"start_begin": "yes or no"}
```

**假设结果**：Chapter 5 与 Chapter 4 共享最后一页（Chapter 5 不在页首开始）

```python
# 每条加 appear_start 字段
[
    {"title": "Preface",                   "physical_index": 1,   "appear_start": "yes"},
    {"title": "Chapter 1  Introduction",   "physical_index": 5,   "appear_start": "yes"},
    {"title": "Chapter 2  Market Overview","physical_index": 29,  "appear_start": "yes"},
    {"title": "Chapter 3  ...",            "physical_index": 71,  "appear_start": "yes"},
    {"title": "Chapter 4  Risk Analysis",  "physical_index": 115, "appear_start": "yes"},
    {"title": "Chapter 5  Operations",     "physical_index": 149, "appear_start": "no"},   # 与 Ch4 共享页 149
    {"title": "Chapter 6  Sustainability", "physical_index": 182, "appear_start": "yes"},
    {"title": "Chapter 7  Governance",     "physical_index": 214, "appear_start": "yes"},
    {"title": "Chapter 8  Outlook",        "physical_index": 249, "appear_start": "yes"},
    {"title": "Chapter 9  Appendix",       "physical_index": 282, "appear_start": "yes"},
]
```

---

## Step 9 — 计算 start_index / end_index，构建层级树

**函数**：`post_processing(structure, end_physical_index=300)` → `list_to_tree(structure)`
**文件**：`pageindex/utils.py`

### end_index 计算规则

```python
def post_processing(structure, end_physical_index):
    for i, item in enumerate(structure):
        item['start_index'] = item['physical_index']
        if i < len(structure) - 1:
            next_item = structure[i + 1]
            if next_item['appear_start'] == 'yes':
                item['end_index'] = next_item['physical_index'] - 1  # 下章从新页开始
            else:
                item['end_index'] = next_item['physical_index']       # 下章与本章共享当前页
        else:
            item['end_index'] = end_physical_index   # 最后一章延伸至文档末尾
```

| 章节 | start_index | 下章 appear_start | end_index 计算 |
|------|------------|---------------------|----------------|
| Preface | 1 | Ch1=yes | 5-1=**4** |
| Chapter 1 | 5 | Ch2=yes | 29-1=**28** |
| Chapter 2 | 29 | Ch3=yes | 71-1=**70** |
| Chapter 3 | 71 | Ch4=yes | 115-1=**114** |
| Chapter 4 | 115 | Ch5=**no** | 149（共享该页）=**149** |
| Chapter 5 | 149 | Ch6=yes | 182-1=**181** |
| Chapter 6 | 182 | Ch7=yes | 214-1=**213** |
| Chapter 7 | 214 | Ch8=yes | 249-1=**248** |
| Chapter 8 | 249 | Ch9=yes | 282-1=**281** |
| Chapter 9 | 282 | 最后一章 | **300** |

### list_to_tree 建树

```python
def list_to_tree(data):
    nodes = {}
    root_nodes = []
    for item in data:
        structure = item['structure']    # "0", "1", "2", ...
        node = {'title': ..., 'start_index': ..., 'end_index': ..., 'nodes': []}
        nodes[structure] = node
        parent_structure = '.'.join(structure.split('.')[:-1])   # "1" → "" → 无父节点
        # 所有一级节点均无父节点 → 全部加入 root_nodes
        if not parent_structure:
            root_nodes.append(node)
```

**因为只有一级目录（structure = "0"~"9"），所有节点均为根节点，`nodes` 均为空。**

**`toc_tree` 输出**：
```python
[
    {"title": "Preface",                         "start_index": 1,   "end_index": 4},
    {"title": "Chapter 1  Introduction",         "start_index": 5,   "end_index": 28},
    {"title": "Chapter 2  Market Overview",      "start_index": 29,  "end_index": 70},
    {"title": "Chapter 3  Financial Performance","start_index": 71,  "end_index": 114},
    {"title": "Chapter 4  Risk Analysis",        "start_index": 115, "end_index": 149},
    {"title": "Chapter 5  Operations",           "start_index": 149, "end_index": 181},
    {"title": "Chapter 6  Sustainability",       "start_index": 182, "end_index": 213},
    {"title": "Chapter 7  Governance",           "start_index": 214, "end_index": 248},
    {"title": "Chapter 8  Outlook",              "start_index": 249, "end_index": 281},
    {"title": "Chapter 9  Appendix",             "start_index": 282, "end_index": 300},
]
```

---

## Step 10 — 递归大节点细分（决策）

**函数**：`process_large_node_recursively(node, page_list, opt)`
**文件**：`pageindex/page_index.py`

对所有根节点**并发执行**（`asyncio.gather`）：

```python
async def process_large_node_recursively(node, page_list, opt=None, logger=None):
    node_page_list = page_list[node['start_index']-1:node['end_index']]
    token_num = sum([page[1] for page in node_page_list])

    if (node['end_index'] - node['start_index'] > opt.max_page_num_each_node    # 默认 10 页
            and token_num >= opt.max_token_num_each_node):                       # 默认 20000 token
        # 触发递归细分：对该节点重跑 process_no_toc（模式 C）
        ...
```

**本场景各节点判断**：

| 章节 | 页数 | 估算 token | 条件①页>10 | 条件②token≥20000 | 是否细分 |
|------|------|----------|-----------|----------------|---------|
| Preface | 4 | ~1800 | ❌ | ❌ | **否** |
| Chapter 1 | 24 | ~11000 | ✅ | ❌ | **否** |
| Chapter 2 | 42 | ~19000 | ✅ | ❌（略低）| **否** |
| Chapter 3 | 44 | ~20000 | ✅ | ✅（恰好） | **触发** |
| Chapter 4 | 35 | ~16000 | ✅ | ❌ | **否** |
| Chapter 5~9 | 各 30~50 | 依情况 | 依情况 | 依情况 | 视情况 |

**若 Chapter 3 触发细分**：
1. 对物理页 71-114 重跑 **`process_no_toc`（模式 C）**，LLM 从正文提取子标题
2. 生成子节点列表（如 3.1、3.2、3.3...）
3. 对子节点再次调用 `check_title_appearance_in_start_concurrent` 和 `post_processing`
4. 子节点附加到 `Chapter 3` 的 `nodes` 字段
5. 对所有子节点**递归**重复 Step 10 的判断

---

## 最终输出

```python
{
    "doc_name": "annual-report.pdf",
    "structure": [
        {"title": "Preface",                         "start_index": 1,   "end_index": 4},
        {"title": "Chapter 1  Introduction",         "start_index": 5,   "end_index": 28},
        {"title": "Chapter 2  Market Overview",      "start_index": 29,  "end_index": 70},
        {
            "title": "Chapter 3  Financial Performance",
            "start_index": 71, "end_index": 114,
            "nodes": [                                   # ← 若 token≥20000，递归细分后生成
                {"title": "3.1 Revenue Analysis",    "start_index": 71,  "end_index": 85},
                {"title": "3.2 Cost Structure",       "start_index": 86,  "end_index": 99},
                {"title": "3.3 Profit Margins",       "start_index": 100, "end_index": 114},
            ]
        },
        {"title": "Chapter 4  Risk Analysis",        "start_index": 115, "end_index": 149},
        {"title": "Chapter 5  Operations",           "start_index": 149, "end_index": 181},
        {"title": "Chapter 6  Sustainability",       "start_index": 182, "end_index": 213},
        {"title": "Chapter 7  Governance",           "start_index": 214, "end_index": 248},
        {"title": "Chapter 8  Outlook",              "start_index": 249, "end_index": 281},
        {"title": "Chapter 9  Appendix",             "start_index": 282, "end_index": 300},
    ]
}
```

---

## LLM 调用全量统计

| 步骤 | 函数 | 调用次数 | 方式 | 说明 |
|------|------|--------|------|------|
| Step 2-1 | `toc_detector_single_page` | 5 次 | 顺序 | 扫到第 4 页后早停 |
| Step 2-2 | `detect_page_index` | 1 次 | 顺序 | 判断目录有无页码 |
| Step 3a | `toc_transformer` | 1~2 次 | 顺序 | 生成 JSON + 验证完整性 |
| Step 3b | `toc_index_extractor` | 1 次 | 顺序 | 扫后 20 页找物理页 |
| Step 5 | `check_title_appearance` | 9 次 | **全并发** | `asyncio.gather`，一轮完成 |
| Step 6 | `single_toc_item_index_fixer` + `check_title_appearance` | 2 次 | **并发** | 定位 + 验证 |
| Step 8 | `check_title_appearance_in_start` | 10 次 | **全并发** | `asyncio.gather`，一轮完成 |
| （可选）Step 10 | 模式 C 递归 | N 次 | 顺序+并发 | 仅 token≥20000 的大节点触发 |
| **合计（无递归）** | — | **~31 次** | — | 约 21 次为并发，实际等待时间≈串行10次 |

---

## 关键算法简明说明

### 偏移量众数投票

$$\text{offset} = \arg\max_{d}\,\bigl|\{i \mid \text{physical\_index}_i - \text{page}_i = d\}\bigr|$$

只需少量配对（甚至 1 对）即可推算全局偏移，再批量应用，避免扫描整本书。

### end_index 边界确定（appear_start 联合判断）

```
若下一章 appear_start = 'yes'（该章从新页第一行开始）：
    本章 end_index = 下章 physical_index - 1   （两章不共享任何页）

若下一章 appear_start = 'no'（该章中间页开始）：
    本章 end_index = 下章 physical_index       （两章共享该页）
```

### 大节点递归细分（双阈值 AND 条件）

```
触发细分条件（必须同时满足）：
① 页数 > max_page_num_each_node（默认 10）
② token 总数 ≥ max_token_num_each_node（默认 20000）
```

只有页多 **且** 内容多的节点才会触发，避免对短章节浪费 LLM 调用。

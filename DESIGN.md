# 设计说明与需求对照

本文说明**为什么这样设计**，并逐条对照需求书的验收标准。

---

## 一、编码前发现的 9 个问题与解决方式

| # | 需求中的隐患 | 解决方式 | 代码位置 |
|---|---|---|---|
| 1 | 流程图是"复制 → OCR"，但第三节要求"任何复制前必须看到 OCR 结果与最终文件名"——两者矛盾 | 把 OCR 提到复制之前，**在源文件上只读识别**，复制是最后一步。既满足"预览后才落盘"，又不产生临时垃圾文件 | `core/task_manager.py` 的 `run_ocr()` / `execute()` |
| 2 | PaddleOCR 在 Python 3.13/3.14 无轮子，且 2.x/3.x 输出结构不同 | OCR 引擎抽象 + 自动降级 `paddle → rapid → none`；输出用 `normalize_ocr_payload()` 递归归一化，兼容 2.x/3.x/纯列表四种形态 | `ocr/engine.py`, `ocr/paddle_engine.py` |
| 3 | `cv2.imread()` 读不了中文路径，会静默返回 None | 统一用 **Pillow 解码**（原生支持 Unicode 路径）再转 ndarray；底层读写用 `\\?\` 扩展路径 | `utils/file_utils.py: load_image_rgb`, `to_long_path` |
| 4 | "AI 智能判断"与"禁止上云"冲突 | 默认用**本地启发式引擎**（字段别名库 + 文档模板 + 行级打分）；可选 Ollama 适配器**强制校验 127.0.0.1**，另有单元测试断言外网地址被拒绝 | `ocr/suggester.py`, `tests/test_parser.py: TestOllamaSafety` |
| 5 | Windows 文件名陷阱（保留设备名、结尾点空格、大小写、260 长度） | 全部集中在 validators 处理；冲突判定大小写不敏感 | `utils/validators.py`, `core/filename_manager.py` |
| 6 | 仅靠 `os.path.exists` 判断"不覆盖"存在竞态 | 复制用 `os.O_CREAT\|O_EXCL` **独占创建**，由内核保证；`FileExistsError` 时重新分配 `-1` | `utils/file_utils.py: copy_no_overwrite` |
| 7 | 撤销可能误删用户文件 | 四重校验：created 标记 + 文件存在 + 大小一致 + 路径在输出目录内 | `core/extractor.py: undo_files` |
| 8 | OCR 引擎非线程安全 | 单实例 + 可重入锁串行化 + 模型只加载一次 + 每张处理完释放 ndarray | `ocr/*_engine.py`, `core/task_manager.py` |
| 9 | `**` 递归可能失控 | 无 `**` 时按模板深度定向下钻；有 `**` 时加深度上限与 inode 防环 | `core/scanner.py` |

---

## 二、架构分层

```
┌────────────────────────────────────────────────────┐
│ ui/            PySide6，唯一允许弹窗与阻塞的层        │
│   main_window  向导容器 + 线程编排                    │
│   workers      QThread（扫描/OCR/复制/撤销）          │
└───────────────┬────────────────────────────────────┘
                │ 只通过 models.py 的数据类 + 回调通信
┌───────────────▼────────────────────────────────────┐
│ core/   纯标准库，可独立测试，不 import PySide6/numpy │
│   task_manager  编排：scan → ocr → name → execute    │
│   scanner       只读扫描（定向剪枝）                  │
│   path_matcher  模板解析与 DP 匹配                    │
│   filename_manager  命名拼装 + 防覆盖分配器            │
│   extractor     唯一写盘者 + 撤销                     │
└───────────────┬────────────────────────────────────┘
                │
     ┌──────────┼───────────┬──────────┐
     ▼          ▼           ▼          ▼
   ocr/     database/   reports/    utils/
  引擎抽象   SQLite     CSV/JSON/   校验/文件/日志
  预处理     历史       Excel       /配置
  解析/AI
```

**为什么 core 不依赖 numpy/PySide6**：这样核心逻辑可以在任何环境
（CI、无图形界面服务器、别人的电脑）跑测试，也让"换 OCR 引擎"
不会波及扫描与复制。

---

## 三、关键流程（内部实现顺序）

```
scan(root, template)
  ├─ 校验根目录与模板
  ├─ 无 ** → _scan_fixed_depth：逐层下钻，字面量层直接拼路径不枚举
  │    深度 = 模板层数，绝不越界遍历
  ├─ 有 ** → _scan_recursive：遍历 + 路径匹配 + 深度上限 + inode 防环
  └─ 每条目录/文件错误 → 收集到 errors，不影响整体

run_ocr(records)
  ├─ get_engine(prefer)  模型加载一次
  └─ 对每张图：
       preprocess(EXIF纠正 → 缩放 → 灰度 → CLAHE → 降噪 → [纠偏/锐化/二值化])
       → engine.recognize(ndarray) → 文本 + 置信度
       → parser.parse（字段提取 + 键值对）
       → NameSuggester.suggest（① 字段 → ② AI → ③ 人工）
       → 写回记录状态，异常只落到该记录

rebuild_names()
  └─ 对每条记录：拼名 → 清洗 → 分配器分配唯一名（考虑磁盘已有 + 本任务已分配 + 大小写）

execute()
  ├─ precheck（输出目录可写 / 磁盘空间 / 源文件是否还在）
  └─ 逐条：dest 目录 → allocate → copy_no_overwrite（O_EXCL）
           → 校验大小 → 写回记录与数据库 → 回调进度
      失败只记录，继续下一个；取消则余下标 CANCELED，已复制的保留
```

---

## 四、命名优先级（需求书第十节）

```
① 指定字段（含别名：姓名/名字/客户名称/收货人/申请人/户名…）
     同行"标签:值" → 同行"标签 值" → 标签行下一行 → 宽松匹配
     ✔ 命中即采用，method = field

② 本地 AI 判断（仅在 ① 失败时）
     候选来源：键值对（带标签权重）+ 裸行
     打分：长度甜点区 / 中文占比 / 数字惩罚 / 停用词 / 标签语义 / OCR置信度
     ≥ 阈值(默认0.60) → 采用，method = ai
     < 阈值           → 保留建议名但标记"需人工确认"
     得分 ≤ 0         → 不推荐任何名字（防止把身份证号当名字）

③ 人工确认
     界面逐条 采用/修改/重新OCR/跳过；"需人工确认"默认不复制
```

**刻意不做的事**：字段失败后绝不"取 OCR 第一段文字"。
`tests/test_parser.py` 里有多条断言专门守着这条线。

---

## 五、验收标准对照（需求书第三十六节）

| 验收项 | 状态 | 证据 |
|---|---|---|
| 指定 `*/1440/SAMPLE_NA.jpg` 准确找到所有图片 | ✅ | `tests/test_scanner.py::test_scan_1440_only` |
| 执行前告诉用户"从哪些完整路径提取哪些图片" | ✅ | 第 2 步表格逐条列出 `rel_path`，未点击执行前不写盘（`test_no_copy_in_scan_phase`） |
| 复制而非移动/修改原文件 | ✅ | 只调用 `copy_no_overwrite`；集成测试比对源目录 SHA256 指纹 |
| 本地 OCR 读取图片文字 | ✅ | `ocr/engine.py` 三引擎抽象，全部本地推理 |
| 优先使用指定字段 | ✅ | `test_parser.py::test_field_priority` |
| 字段失败时用 AI 判断 | ✅ | `test_field_fail_falls_to_ai` |
| AI 不可靠时人工确认 | ✅ | `test_low_confidence_marked_unreliable`、界面"需人工确认"流程 |
| 最终文件名包含来源文件夹 | ✅ | `test_filename_manager.py::TestNameCompose` |
| 冲突自动 `-1/-2/-3` | ✅ | `test_sequential_numbering`、`test_second_run_creates_suffixes` |
| 任何情况下不覆盖已有文件 | ✅ | `O_EXCL` + `test_extractor_never_overwrites` |
| 单文件失败不导致整体失败 | ✅ | `test_corrupt_file_does_not_stop_others` |
| 有日志与报告 | ✅ | `TaskLogger` 逐文件记录；CSV/JSON/Excel 报告 |
| 可打包成普通用户能用的 exe | ✅ | `build_exe.py` + `packaging/PicHarvest.spec` |

---

## 六、刻意保留的取舍

| 取舍 | 原因 |
|---|---|
| 默认扁平化输出（来源写进文件名） | 需求书第十七节明确"第一版默认扁平化"，同名携带来源信息更直观；`keep_structure` 开关已实现 |
| 单张 OCR 无法强杀 | 进程内推理无法安全中断，强杀会破坏引擎状态。改为超时告警 + 可在任务间取消 |
| `[` `]` 不作为通配符 | 真实文件夹名常含方括号（`AB[1]`），按通配符解析会误匹配；只保留 `*` `?` `**` |
| Ollama 需手动开启 | 默认零依赖、零配置；开启后仍强制本地回环地址 |
| 报告输出到 `<输出目录>/_reports` | 与图片同处一地便于查找；不需要可在设置里关闭报告 |

---

## 七、如果要继续扩展

1. **新增 OCR 引擎**：继承 `ocr/engine.py: OcrEngine`，实现 `load()`/`recognize()`，
   在 `_create_engine()` 注册即可，扫描与复制模块完全不用动。
2. **接入云端 OCR**：需求书第二十节禁止默认上云。若要做，建议新增
   `CloudOcrEngine` 并在设置里默认关闭 + 明确告知用户，
   同时保持 `get_engine()` 的降级链不变。
3. **多文件类型**：`utils/file_utils.IMAGE_EXTENSIONS` 与 `only_images` 开关已参数化。
4. **增量任务**：数据库已记录每个文件的目标路径与指纹，可据此实现"只处理新增图片"。

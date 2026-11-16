# 图片智能提取与 OCR 自动命名工具

从一个大目录里**按路径层级**批量挑出指定图片 → 本地 OCR 识别 → 按
`来源文件夹_识别名称.jpg` 命名 → 复制到新目录，全程**不改动原始文件**。

> ### 下载即用（Windows 64 位）
>
> **[⬇ 下载 PicHarvest.zip（约 130 MB）](https://github.com/MuziLee-J/PicHarvest/releases/latest)**
> — 解压后双击 `PicHarvest\PicHarvest.exe`。
> 不需要安装 Python，不需要联网，OCR 模型已打包在内。
>
> 只想从源码跑或自行打包，见下方[安装](#二安装)。

```
20250101/                              D:\整理结果\
├── CASE001/1440/SAMPLE_NA.jpg    →    ├── CASE001_张三.jpg
├── CASE002/1440/SAMPLE_NA.jpg    →    ├── CASE002_张三.jpg
├── CASE003/1440/SAMPLE_NA.jpg    →    └── CASE003_李四.jpg
└── CASE003/2880/SAMPLE_NA.jpg    ✗（规则是 1440，不会命中）
```

---

## 一、三条不可妥协的原则

| 原则 | 实现方式 |
|---|---|
| **原始文件永不改动** | 扫描与 OCR 都是只读；唯一写盘动作是"复制"；集成测试用 SHA256 目录指纹证明源目录零变化 |
| **目标文件永不覆盖** | 用 `os.O_CREAT\|O_EXCL` 独占创建，文件已存在时由**操作系统内核**报错，再自动改用 `-1`；不是"先检查再写"的竞态写法 |
| **执行前必须确认** | 扫描与执行是两个独立阶段。用户在看到完整来源路径、OCR 结果、最终文件名之前，程序不会写入任何文件 |

---

## 二、安装

```bat
:: 建议 Python 3.11 或 3.12（3.13/3.14 上 OCR 库支持不完整）
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

**OCR 引擎二选一：**

| 引擎 | 体积 | 说明 |
|---|---|---|
| `rapidocr-onnxruntime` | ~100 MB | 轻量、CPU 友好、开箱即用，**推荐先装这个** |
| `paddlepaddle` + `paddleocr` | 1 GB+ | 精度更高，适合复杂版面；需 Python 3.11/3.12 |

两个都不装也能运行：程序会退化为"按路径提取 + 用来源文件夹命名"，
并在启动时明确提示——**绝不会把图片偷偷上传到云端**。

---

## 三、使用流程（向导四步）

### 第 1 步 · 设置任务

- **根目录**：`D:\示例数据\20250101`（点"读取目录结构"可从真实目录里选文件夹名）
- **提取方式**
  - 普通模式：三个下拉框 → `第一层=所有文件夹`、`第二层=1440`、`图片=SAMPLE_NA.jpg`
  - 高级模式：直接写模板 → `*/1440/SAMPLE_NA.jpg`
- **输出目录**：`D:\整理结果`
- **命名字段**：`姓名`（可填多个，逗号分隔，按顺序优先）

页面只保留最常改的几项；报告格式、来源信息、AI/Ollama、超时、自定义模型等
都收在各节的 **高级选项** 里，点一下展开，不占地方。

点 **扫描文件**。

### 第 2 步 · 扫描结果

程序列出**每一条完整来源路径**，支持缩略图、预览、筛选、排序、勾选/取消。
此时磁盘上**没有任何变化**——可以放心反复扫描。

### 第 3 步 · OCR 与命名

对每张图展示：`原图 → OCR 文本 → 识别字段 → 建议名 → 最终文件名`。

命名的严格优先级：

```
① 用户指定字段命中        → 直接采用（置信度 0.92~0.98）
② 本地 AI 判断（可靠时）  → 采用（可在设置里调置信度阈值 0.45/0.60/0.75）
③ 都不行                  → 标记「需人工确认」，交给你逐条处理
```

> 字段失败时**不会**随便取 OCR 第一行——这是刻意设计。身份证上的
> "中华人民共和国居民身份证"、发票上的"￥1000.00"都会被打分模型排除。

逐条可 **采用 / 修改 / 重新OCR / 跳过**；也可"全部采用建议名"后抽查。

> **输出目录选错了？** 不用重新 OCR。这一步底部就能直接改「输出目录」，
> 改完文件名自动重算、识别结果原样保留；每张图的详情里会显示
> **将复制到** 哪个位置，复制前心里有数。
标记为"需人工确认"的文件默认**不会被复制**（除非勾选"包含未确认项"）。

### 第 4 步 · 执行与结果

实时进度、成功/失败/跳过计数、当前处理项、日志；
支持 **暂停 / 继续 / 取消**（取消后已复制的文件保留）。

> **🛑 强制停止**：识别过程中随时可以点。它会把后台 OCR 子进程直接
> `kill` 掉——实测 **0.3 秒**内生效，不用等当前这张跑完。
> 已识别完的文件结果保留，正在处理的那一张会丢弃。
> 单张识别也支持超时自动终止（默认 60 秒）。

结束后：生成 CSV / JSON / Excel 报告，可 **打开输出目录 / 导出报告 / 撤销本次任务**。

---

## 四、路径模板语法

| 写法 | 含义 | 例子 |
|---|---|---|
| `*` | 匹配**恰好一层**目录名 | `*/1440/a.jpg` 命中 `CASE101/1440/a.jpg`，不命中 `a/b/1440/a.jpg` |
| `**` | 匹配**任意层级**目录 | `**/1440/a.jpg` 上面两个都命中 |
| `?` | 匹配单个字符 | `FU?/1440/a.jpg` |
| `*.jpg` | 文件名通配 | `*/1440/*.jpg` |
| 最后一段 | 总是文件名 | 必须是文件名，不能是 `**` |

- 大小写不敏感（与 NTFS 行为一致）
- `[` `]` 按普通字符处理（真实文件夹名常含方括号）
- 直接粘贴绝对路径会自动转换为相对模板

---

## 五、命名与重名规则

```text
默认：{来源文件夹}_{识别名称}.{原扩展名}
      CASE001_张三.jpg

可配置：位置（前缀/后缀）、连接符、来源层级（第1/2/3层）
      张三_CASE001.jpg   或   CASE001-张三.jpg
```

重名时**依次追加** `-1`、`-2`、`-3`：

```text
CASE001_张三.jpg        ← 已存在
CASE001_张三-1.jpg      ← 新文件
CASE001_张三-1.jpg      ← 已存在
CASE001_张三-2.jpg      ← 新文件
```

同一个"张三"来自不同来源时，因为带了来源前缀，本身就是不同的名字：

```text
CASE001_张三.jpg
CASE002_张三.jpg      ← 不冲突，一眼看得出出处
```

文件名会经过完整清洗：非法字符 `\ / : * ? " < > |` 替换、首尾点与空格去除、
控制字符清理、Windows 保留设备名（CON/NUL/COM1…）规避、超长截断、
`\\?\` 长路径支持。

---

## 五点五、OCR 识别效果不好怎么办

**先诊断，再决定要不要训练**：

```bat
python tools/diagnose_ocr.py --root "D:\示例数据\20250101" --limit 5
```

它会对同一张图跑四种预处理强度，打印每行文字的置信度，
并告诉你问题到底出在**图像质量**、**命名字段**还是**模型能力**。

提升路径按性价比排序（前两项都不需要训练）：

| 手段 | 成本 | 说明 |
|---|---|---|
| **换更强的预训练模型** | 几分钟 | 设置里填 PP-OCRv4/v5 **server** 模型路径，不用标注、不用 GPU |
| **调预处理强度** | 几秒 | 用诊断工具选置信度最高的那档 |
| **纠正记忆** | 自动积累 | 你每次手工改名都会被记住，相同文本再次出现时直接沿用 |
| **微调识别模型** | 1~2 天 + GPU | 需要 ≥500 条**单行截图**标注，见 [`docs/OCR_训练指南.md`](docs/OCR_训练指南.md) |

**关于"用已有数据训练"**：可以，但要注意数据形态——识别模型吃的是
**单行文字截图 + 正确文字**，不是整张照片。本程序提供
**工具 → 导出 OCR 训练数据**，会裁出姓名所在的行并生成 PaddleOCR
可直接使用的 `train.txt`，同时附一份 `review.csv` 供你核对标签。

> ⚠️ 程序刻意不做"相似文本"的模糊匹配：同一套表单换个姓名时，
> 两段 OCR 文本相似度往往超过 99%，模糊匹配会把上一个人的名字
> 安到这个人头上。只有**完全相同的文本**才会复用修正。

---

## 六、任务历史、日志、报告

| 内容 | 位置 |
|---|---|
| 任务历史（SQLite） | `%LOCALAPPDATA%\PicHarvest\history.db` |
| 运行日志 | `%LOCALAPPDATA%\PicHarvest\logs\` |
| 配置 | `%LOCALAPPDATA%\PicHarvest\config.json` |
| 处理报告 | `<输出目录>\_reports\处理报告_<任务号>_<时间>.{csv,json,xlsx}` |

报告字段：序号 / 来源路径 / 来源文件夹 / 原始文件名 / OCR结果 / 识别名称 /
提取方式 / 置信度 / 最终文件名 / 目标路径 / 状态 / 重名处理 / 文件大小 / 错误。

**撤销**只删除"本任务创建的、且大小与记录一致、且位于输出目录内"的文件；
三条校验缺一不可，原始目录永远不会被触碰。装了 `send2trash` 会送进回收站。

---

## 七、打包成 exe / 分发给其他电脑

```bat
python build_exe.py --zip
```

产物：

| 文件 | 大小 | 用途 |
|---|---|---|
| `dist\PicHarvest\PicHarvest.exe` | 316 MB（目录） | 本机直接运行 |
| `dist\PicHarvest.zip` | **130 MB** | **拷给别人用**：解压 → 双击 `PicHarvest\PicHarvest.exe` |
| `dist\PicHarvest-源码.zip` | 153 KB | 换电脑继续开发（含测试与工具） |

- 压缩包里自带 `使用说明.txt`（面向最终用户的图文步骤 + FAQ）
- 普通用户**不需要装 Python**，解压即用
- RapidOCR 的模型已打进包内，**首次运行不需要联网**
- 已在**中文路径 + 全新解压位置**实测通过（Qt 加载 → 真实 OCR 识别 →
  防覆盖机制验证，退出码 0）
- 若需要 PaddleOCR：取消 `packaging/PicHarvest.spec` 中
  `add_package('paddleocr')` 两行的注释后重新打包
  （PaddleOCR 首次运行会下载模型到 `~/.paddleocr`，体积 1GB+）

**打包体积说明**：316 MB 里约 200 MB 是 ONNXRuntime 与 OCR 模型、
约 90 MB 是 Qt。这是"完全离线、开箱即用"的代价——换来的是
普通用户双击就能用，且不需要任何网络。

---

## 七点五、打包后自检（发给用户前建议先跑一次）

> 拿到 zip 的人如果打不开程序，让他执行下面这条命令，并把生成的
> 报告文件发回来，就能定位问题。

```bat
dist\PicHarvest\PicHarvest.exe --selftest
```

它会检查：Qt / Pillow / numpy 能否导入 → 生成一张测试图 →
预处理 → **真实调用 OCR 识别"姓名：张三"** → 字段提取 →
**验证"已存在文件拒绝覆盖"机制**，并把结果写到
`%LOCALAPPDATA%\PicHarvest\selftest_report.txt`（退出码 0 = 全部通过）。

由于 GUI 程序没有控制台窗口，这份报告文件是判断打包是否健康的可靠依据；
用户遇到问题时报的也是这个文件。

---

## 八、性能

- **OCR 模型只加载一次**，单实例串行调用（PaddleOCR/RapidOCR 都不是线程安全的）
- **识别速度主要取决于送进模型的图片大小**：耗时大致与像素数成正比。
  默认 `识别精度 = 平衡（最长边 1600）`；实测 3000×2200 的扫描件，
  1600 比 2200 快 **1.3 倍**且文字完全一致，1200 快 **1.7–2.0 倍**
  （但小字/糊图会掉字，所以只是"速度优先"选项）。
- **多进程并发默认关闭**（`同时识别 = 1`）。ONNXRuntime 默认已把一个进程铺满
  所有核心，再开进程只会互相抢核：本机 16 逻辑核实测 16 张图，
  串行 10.4s、2 进程 11.9s、4 进程 18.0s。只有当你的机器单进程明显吃不满
  CPU 时，把它调大才有收益。
- 扫描无 `**` 时按模板**定向下钻**：`*/1440/x.jpg` 只枚举第一层目录再直查
  `1440` 子目录，**不会**遍历 `1440` 下的任何子目录
- 表格用 `QAbstractTableModel` + 排序代理，几万行不卡
- 缩略图后台线程池加载 + LRU 缓存（默认 64px / 800 张）
- 复制按 512KB 分块，每处理完一个文件立即写库，崩溃可追溯

---

## 九、测试

```bat
python -m unittest discover -s tests -t . -v    # 151 个测试
python tools/smoke_test.py                      # 端到端冒烟（含真实 OCR）
python tools/ui_test.py                         # 无头 GUI 全流程自检
python tools/test_force_stop.py                 # 验证强制停止/超时真的生效
python tools/diagnose_ocr.py --root ... --limit 5   # OCR 效果诊断
python tools/make_test_data.py                  # 只生成测试数据
```

覆盖需求书第三十三节全部场景：

| 场景 | 测试 |
|---|---|
| 1 正常提取 `*/1440/...` | `test_scanner.test_scan_1440_only` |
| 2 切换 2880 不误提 1440 | `test_scanner.test_scan_2880_only` |
| 3 OCR 识别"姓名：张三" | `test_parser` + `test_integration.TestFullPipelineWithOcr` |
| 4 同名张三来自不同来源 | `test_filename_manager.test_different_sources_stay_unique` |
| 5 完全相同文件名 → `-1/-2` | `test_filename_manager.test_same_source_same_name_gets_suffix` |
| 6 目标已有文件不覆盖 | `test_extractor.test_extractor_never_overwrites` |
| 7 OCR 失败转人工确认 | `test_extractor` + `test_integration.test_ocr_failure_needs_review` |
| 8 中文路径 | `test_scanner.test_chinese_path` |
| 9 损坏图片只跳过自己 | `test_extractor.test_corrupt_file_does_not_stop_others` |
| 10 取消后已复制文件保留 | `test_extractor.test_cancel_keeps_copied_files` |

每个集成测试都会对比源目录的 SHA256 指纹，**证明原始文件零改动**。

---

## 十、项目结构

```
main.py                     入口（高DPI / 全局异常兜底 / --ocr-worker / --selftest）
models.py                   全部数据结构（ImageRecord / TaskConfig / 状态常量）
build_exe.py                一键打包
packaging/PicHarvest.spec   PyInstaller 配置
ui/       PySide6 界面：主窗口 / 设置 / 扫描 / OCR命名 / 执行结果 / 线程 / 组件
core/     纯标准库：path_matcher 扫描 filename_manager 命名 extractor 复制
         task_manager 编排 correction_memory 纠正记忆
ocr/      engine 抽象 / paddle / rapid / process_engine 子进程代理
         worker_process 子进程入口 / preprocess / parser / suggester
database/ SQLite 任务历史 + 纠正记忆
reports/  CSV / JSON / Excel 报告 + training_export 训练数据导出
utils/    validators 校验 / file_utils 文件 / logger 日志 / config 配置
docs/     OCR_训练指南.md
tools/    诊断、强制停止验证、测试数据生成、冒烟测试、界面自检
tests/    unittest 测试套件（151 个）
```

**OCR 为什么要跑在子进程里**：PaddleOCR / RapidOCR 的推理是阻塞的
C++ 调用，Python 线程无法中断它。放进子进程后，父进程 `kill()` 就能
立即终止——这是"强制停止"能真正生效的唯一办法，也顺带让单张超时
成为可能。

**分层铁律**：`core/` 与 `ocr/parser|suggester` 不导入 PySide6、不导入 numpy，
因此可以在任何环境跑测试；换 OCR 引擎不需要动扫描与复制模块
（`OcrEngine` 是抽象基类，新增引擎只要实现 `load()` / `recognize()`）。

---

## 十一、常见问题

**Q：提示"没有可用的本地 OCR 引擎"？**
A：执行 `pip install rapidocr-onnxruntime`（约 100MB）。或继续只用提取功能。
菜单"工具 → 检测 OCR 环境"可随时查看状态。

**Q：PaddleOCR 装不上？**
A：确认 Python 版本是 3.11/3.12；`pip install paddlepaddle paddleocr`。
也可以直接用 RapidOCR，大多数清晰扫描件效果接近。

**Q：OCR 识别不准怎么办？**
A：① 设置里把预处理调成"强力"（纠偏+锐化+二值化）；
② 把命名字段改成图里真实存在的标签（如"客户名称"）；
③ 降低 AI 阈值到 0.45；④ 在命名页手工改（最终以你改的为准）。

**Q：扫描很慢？**
A：确认模板不含 `**`（含 `**` 会遍历整棵树）。`*/1440/x.jpg` 只访问
根目录的一级目录 + 其中的 `1440` 子目录。

**Q：单张图 OCR 卡住能中断吗？**
A：能。默认开启"子进程隔离"（设置页可关），OCR 跑在独立子进程中，
点 **🛑 强制停止** 会直接 `kill` 掉子进程，当前这张立即放弃（实测 0.3 秒内）；
单张也有超时兜底（默认 60 秒）自动终止。
只有手动关掉子进程隔离后，进程内 OCR 才无法被中断，那时只能等它自己返回。

**Q：会不会把图片传到网上？**
A：不会。OCR 全在本机；唯一的网络代码是**可选**的本地 Ollama 适配器，
且强制校验地址必须是 `127.0.0.1`/`localhost`，填外网地址会被拒绝。

---

## 十二、数据安全提示

- 本工具处理的可能是含个人信息的证件/单据图片。图片与 OCR 文本
  **只在本机内存与本地磁盘**中流转，程序不含任何上传逻辑。
- 输出目录中的 `_reports\` 里的报告包含 OCR 文本，请按你的数据管理
  规范保管或删除。
- 卸载程序不会删除历史库与日志，需要时手工删除
  `%LOCALAPPDATA%\PicHarvest\`。

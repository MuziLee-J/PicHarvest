"""
OCR 子进程入口。

为什么需要子进程：
    PaddleOCR / RapidOCR 的推理是阻塞的 C/C++ 调用，Python 线程无法中断它。
    在子进程里跑，父进程用 kill() 就能**立刻**停掉——这才是真正的"强制停止"，
    也顺带让"单张超时"成为可能（线程模式做不到）。

通信协议：stdin / stdout 各一行 JSON（UTF-8），与父进程 ocr/process_engine.py 对应。

    父 → 子（启动首行，配置）：
        {"engine": "auto", "rec_model_path": "", "det_model_path": ""}
    子 → 父（就绪）：
        {"type": "ready", "ok": true, "engine": "RapidOCR…"}
    父 → 子（每张图）：
        {"path": "D:/x.jpg", "preset": "standard", "max_side": 2200}
    子 → 父（结果）：
        {"ok": true, "text": "...", "lines": [...], "scores": [...],
         "boxes": [[x1,y1,x2,y2], ...], "engine": "...", "elapsed": 1.23}

重要细节：
    fd 1 会被重定向到 stderr，协议走 dup 出来的副本。
    PaddleOCR 等库在 C++ 层直接往 stdout 打印，不重定向会污染 JSON 流。
"""

from __future__ import annotations

import json
import os
import sys
import traceback


def _open_protocol_stream():
    """复制 stdout 作为协议通道，并把 fd 1 重定向到 stderr。"""
    try:
        protocol_fd = os.dup(1)
        os.dup2(2, 1)                       # 之后所有 stdout 输出都进 stderr
        return os.fdopen(protocol_fd, 'w', encoding='utf-8',
                         buffering=1, newline='\n')
    except OSError:
        # 极端情况下退化为普通 stdout
        return sys.stdout


def _configure_stdio() -> None:
    """强制 UTF-8，避免中文在管道里按 cp936 编码。"""
    for stream, kwargs in (
        (sys.stdin, {'encoding': 'utf-8', 'errors': 'replace'}),
        (sys.stderr, {'encoding': 'utf-8', 'errors': 'replace'}),
    ):
        try:
            stream.reconfigure(**kwargs)     # type: ignore[attr-defined]
        except Exception:
            pass


def main() -> int:
    _configure_stdio()
    protocol = _open_protocol_stream()

    def send(payload: dict) -> None:
        try:
            protocol.write(json.dumps(payload, ensure_ascii=False) + '\n')
            protocol.flush()
        except (BrokenPipeError, ValueError, OSError):
            raise SystemExit(0)

    # ---------- 1. 读取配置 ----------
    try:
        first = sys.stdin.readline()
        if not first:
            return 0
        config = json.loads(first)
    except Exception as exc:
        send({'type': 'ready', 'ok': False, 'error': f'配置解析失败：{exc}'})
        return 2

    # ---------- 2. 加载引擎（只加载一次） ----------
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from ocr.engine import get_engine
        engine = get_engine(
            config.get('engine', 'auto'),
            rec_model_path=config.get('rec_model_path', ''),
            det_model_path=config.get('det_model_path', ''),
        )
        engine.load()
        send({'type': 'ready', 'ok': True, 'engine': engine.display_name,
              'engine_key': engine.name})
    except Exception as exc:
        send({'type': 'ready', 'ok': False,
              'error': f'{type(exc).__name__}: {exc}',
              'traceback': traceback.format_exc()[-1500:]})
        return 3

    # ---------- 3. 循环处理请求 ----------
    from ocr.preprocess import preprocess

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            send({'ok': False, 'error': '请求不是合法 JSON'})
            continue

        action = request.get('action', 'ocr')
        if action == 'shutdown':
            send({'ok': True, 'bye': True})
            return 0
        if action == 'ping':
            send({'ok': True, 'pong': True})
            continue

        path = request.get('path', '')
        try:
            image = preprocess(path,
                               request.get('preset', 'standard'),
                               int(request.get('max_side', 2200) or 2200))
        except Exception as exc:
            send({'ok': False, 'error': f'图片读取失败：{exc}', 'path': path})
            continue

        # 预处理会缩放图片，这里记下尺寸，父进程导出训练数据时要用它
        # 把文本框映射回原图坐标
        try:
            height, width = image.shape[:2]
            size = [int(width), int(height)]
        except Exception:
            size = [0, 0]

        try:
            result = engine.recognize(image)
        except Exception as exc:
            send({'ok': False, 'error': f'OCR 异常：{exc}', 'path': path,
                  'traceback': traceback.format_exc()[-1200:]})
            continue
        finally:
            del image

        send({
            'ok': not result.error or bool(result.text),
            'text': result.text,
            'lines': list(result.lines),
            'scores': [float(s) for s in result.scores],
            'boxes': result.boxes,
            'engine': result.engine,
            'elapsed': round(result.elapsed, 3),
            'error': result.error or '',
            'path': path,
            'size': size,
        })

    return 0


if __name__ == '__main__':
    raise SystemExit(main())

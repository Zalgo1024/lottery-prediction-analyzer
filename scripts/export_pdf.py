# -*- coding: utf-8 -*-
"""结题报告.docx -> PDF（Word COM 16.0，SaveAs2 FileFormat=17）。

2026-10-21 从 `scripts/_export_pdf.py` 转正：
  · 去掉 `_` 前缀 —— 它是要长期用的运维脚本，顶着 `_` 会被 `.gitignore` 的
    `scripts/_*.py` 拦掉，以后改完根本进不了库；
  · 路径改为相对仓库根（原来硬编码 `E:\\707\\...`，换机即失效）。

依赖：Windows + 已安装 Word（win32com）。非 Windows 或没装 Word 时直接报错退出，
不做降级 —— 静默生成一个空 PDF 比失败更难发现。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DOCX = BASE / "docs" / "结题报告.docx"
PDF = BASE / "docs" / "结题报告.pdf"


def main() -> int:
    if not DOCX.exists():
        print(f"❌ 源文件不存在: {DOCX}")
        return 1
    try:
        import pythoncom
        import win32com.client
    except ImportError as e:
        print(f"❌ 缺少 Word COM 依赖（仅 Windows 可用）: {e}")
        return 1

    pythoncom.CoInitialize()
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    try:
        doc = word.Documents.Open(str(DOCX), ReadOnly=True)
        doc.SaveAs2(str(PDF), FileFormat=17)
        doc.Close(False)
    finally:
        word.Quit()

    st = os.stat(PDF)
    print(f"pdf: {st.st_size} bytes, mtime: "
          f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

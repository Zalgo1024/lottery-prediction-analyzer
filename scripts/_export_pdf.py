# -*- coding: utf-8 -*-
"""结题报告.docx -> PDF（Word COM 16.0，SaveAs2 FileFormat=17）。"""
import os
import time

import pythoncom
import win32com.client

pythoncom.CoInitialize()
word = win32com.client.DispatchEx("Word.Application")
word.Visible = False
try:
    doc = word.Documents.Open(r"E:\707\docs\结题报告.docx", ReadOnly=True)
    doc.SaveAs2(r"E:\707\docs\结题报告.pdf", FileFormat=17)
    doc.Close(False)
finally:
    word.Quit()

pdf = r"E:\707\docs\结题报告.pdf"
st = os.stat(pdf)
print("pdf:", st.st_size, "bytes, mtime:", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)))

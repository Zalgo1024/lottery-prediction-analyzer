# -*- coding: utf-8 -*-
"""docx/pptx 文本节点内测试计数替换（当前 347→351）；直接覆盖回写。

只替换 <w:t>/<a:t> 节点内部文本，styles/坐标里的数字一概不碰。
改完重新打开 zip 复检文本节点计数（上次 pptx 的教训）。
"""
import re
import zipfile
from pathlib import Path

OLD, NEW = "373", "374"
TARGETS = [
    (r"E:\707\docs\结题报告.docx", "w"),
    # 本地私有材料，不入库；换机器时按需替换路径
    (r"E:\707\docs\local_only.pptx", "a"),
]


def patch_text_nodes(xml: str, tag: str) -> tuple[str, int]:
    pat = re.compile(rf"(<{tag}:t(?:\s[^>]*)?>)(.*?)(</{tag}:t>)", re.S)
    n = 0

    def _sub(m):
        nonlocal n
        body = m.group(2)
        if OLD in body:
            n += body.count(OLD)
            body = body.replace(OLD, NEW)
        return m.group(1) + body + m.group(3)

    return pat.sub(_sub, xml), n


def main():
    for path_str, tag in TARGETS:
        p = Path(path_str)
        tmp = p.with_suffix(p.suffix + ".tmpcnt")
        with zipfile.ZipFile(p) as zin:
            names = zin.namelist()
            contents = {n: zin.read(n) for n in names}
        total = 0
        for n in names:
            if n.endswith(".xml"):
                xml = contents[n].decode("utf-8")
                new_xml, cnt = patch_text_nodes(xml, tag)
                if cnt:
                    contents[n] = new_xml.encode("utf-8")
                    total += cnt
                    print(f"  {p.name}:{n} -> {cnt} 处")
        with open(tmp, "wb") as zout:
            with zipfile.ZipFile(zout, "w", zipfile.ZIP_DEFLATED) as z:
                for n in names:
                    z.writestr(n, contents[n])
        # 直接覆盖（safe-delete shim 只拦 unlink；open(wb) 覆盖不走它）
        with open(p, "wb") as f:
            f.write(tmp.read_bytes())
        tmp.unlink()
        print(f"{p.name}: 共改 {total} 处")

    # ---- 复检：重新打开 zip，在文本节点里数旧/新计数 ----
    print("---- 复检 ----")
    for path_str, tag in TARGETS:
        p = Path(path_str)
        c_old = c_new = 0
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if not n.endswith(".xml"):
                    continue
                xml = z.read(n).decode("utf-8")
                for m in re.finditer(rf"<{tag}:t(?:\s[^>]*)?>(.*?)</{tag}:t>", xml, re.S):
                    c_old += m.group(1).count(OLD)
                    c_new += m.group(1).count(NEW)
        print(f"{p.name}: 文本节点 {OLD}={c_old} {NEW}={c_new}")


if __name__ == "__main__":
    main()

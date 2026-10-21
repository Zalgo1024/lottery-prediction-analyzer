# 一次性脚本归档（oneoff）

2026-10-21 从 `scripts/` 根目录移到这里的 **43 个**历史脚本。

## 它们是什么

都是一次性用途，跑过就结束，**不是**项目运行时依赖：

| 前缀 | 用途 | 举例 |
|---|---|---|
| `_probe_*` | 临时探测某个数据现象 | `_probe_pending_26109.py`、`_probe_lift.py` |
| `_sync_docs_*` | 某次文档批量改字（改完即弃） | `_sync_docs_594.py` |
| `_inspect_*` | 抽查 docx/pptx 的节点结构 | `_inspect_504_nodes.py` |
| `_verify_*` | 一次性复核某份报告/批次 | `_verify_pdf_518.py` |
| `_dbg_*` / `_patch*` | 调 bug、打补丁 | `_dbg_git.py`、`_patch342.py` |

## 为什么没删

不是为了复用，是为了**可追溯**：结题报告里若干结论（例如「双色球冷门头奖实得 = 热门 1.484×」）
的验算过程就写在这些脚本里。留着能在被质疑时把过程重放一遍，删了就只剩一句无据可查的结论。

## 能跑吗

**不保证。** 移动后相对层级变了（这些脚本多数写的是
`sys.path.insert(0, str(Path(__file__).parent.parent))`，在 `scripts/` 根时指向仓库根，
现在指向了 `scripts/`）。真要重跑，把 `parent.parent` 改成 `parent.parent.parent`，
或直接从仓库根 `python -m oneoff.xxx` 之外的方式自行调整。

## 以后的规矩

- 新的一次性脚本**直接放这个目录**；
- `scripts/` 根目录下的 `_*.py` 仍被 `.gitignore` 拦截（`scripts/_*.py`），
  是故意的：根目录只放长期运维脚本，别再堆临时文件；
- 真正要长期用的运维脚本**不要**用 `_` 前缀。反例摆在眼前：`_export_pdf.py` 和
  `_rebuild_market_closure.py` 本来每年都要跑，却因为 `_` 前缀一直进不了库，
  2026-10-21 已改名为 `scripts/export_pdf.py` / `scripts/rebuild_market_closure.py`。

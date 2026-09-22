"""打包：<batch>/assets/ 下全部 md → <batch>/<batch名>.zip（供平台「上传」导入）。

用法：
    python pack_bundle.py <batch>/

zip 内保持 assets/ 下的相对路径（平台按 frontmatter 归类，路径仅便于人工查看）。
打包前请先通过 lint_assets.py（CRITICAL/HIGH 为 0）。仅用 Python 3 标准库。
"""
import argparse
import sys
import zipfile
from pathlib import Path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("batch", type=Path)
    a = ap.parse_args()
    assets = a.batch / "assets"
    files = sorted(assets.rglob("*.md")) if assets.exists() else []
    if not files:
        print(f"[打包] {assets} 下无 md，未生成 zip")
        return 1
    out = a.batch / f"{a.batch.resolve().name}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(assets).as_posix())
    by_top = {}
    for f in files:
        top = f.relative_to(assets).parts[0]
        by_top[top] = by_top.get(top, 0) + 1
    print(f"[打包] {len(files)} 个 md（{', '.join(f'{k} {v}' for k, v in sorted(by_top.items()))}）→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

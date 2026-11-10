#!/usr/bin/env python3
"""在本地复现 GitHub Actions 的 runner 环境并跑同一条门禁命令。

为什么要有它：workflow 只是薄壳，真正的门禁逻辑在 `ci/gate.py`。但「本地能过」
不等于「CI 能过」—— 本地有 `.venv`、有 `data/` 下的运行产物、有历史 ref，
CI 全都没有。这个脚本把 CI 的四个前提条件复制到临时目录里：

    1. 干净 clone（无 .venv、无 data/ 运行产物、无本机 ref）
    2. 全新 venv + pip 安装 ast-grep-cli（版本按 workflow 的 pin）
    3. 把 venv/bin 放到 PATH 首位（等价于 setup-python + pip install）
    4. 执行 workflow 里那条一模一样的命令

用法：
    python3 ci/ci_sim.py                       # 用本地已提交的 main
    python3 ci/ci_sim.py --repo wjzxl/miqilinone   # 用真实远端
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AST_GREP_SPEC = "ast-grep-cli>=0.51,<0.52"   # 与 .github/workflows/gate.yml 保持一致


def sh(args: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:5])} 失败：{proc.stderr.strip()[:400]}")
    return proc.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本地复现 GitHub Actions runner")
    ap.add_argument("--repo", default="", help="owner/name；留空则用本地已提交内容")
    ap.add_argument("--ref", default="main", help="远端分支")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="mw-ci-sim-"))
    checkout = workdir / "repo"
    try:
        if args.repo:
            print(f"[1/4] clone https://github.com/{args.repo}.git（{args.ref}）")
            sh(["git", "clone", "-q", "--depth", "1", "--branch", args.ref,
                f"https://github.com/{args.repo}.git", str(checkout)])
        else:
            print("[1/4] 用本地已提交内容构造干净副本")
            sh(["git", "-C", str(ROOT), "archive", args.ref, "-o", str(workdir / "src.tar")])
            checkout.mkdir(parents=True)
            sh(["tar", "-xf", str(workdir / "src.tar"), "-C", str(checkout)])

        print("[2/4] 建全新 venv 并安装 ast-grep-cli（模拟 setup-python + pip install）")
        sh([sys.executable, "-m", "venv", str(checkout / ".venv")])
        vpy = checkout / ".venv" / "bin" / "python"
        sh([str(vpy), "-m", "pip", "install", "-q", "--upgrade", "pip"])
        sh([str(vpy), "-m", "pip", "install", "-q", AST_GREP_SPEC])

        env = dict(os.environ)
        env["PATH"] = f"{checkout / '.venv' / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        env.pop("PYTHONPATH", None)
        version = sh([str(checkout / ".venv" / "bin" / "ast-grep"), "--version"]).strip()
        print(f"      ast-grep: {version}")
        print(f"      python  : {sh([str(vpy), '-V']).strip()}")

        print("[3/4] 执行 workflow 同款命令：python3 ci/gate.py --report data/ci/gate-report.json")
        proc = subprocess.run(
            ["python3", "ci/gate.py", "--report", "data/ci/gate-report.json"],
            cwd=str(checkout), capture_output=True, text=True, env=env, timeout=1800,
        )
        print(proc.stdout[-4000:])
        if proc.stderr.strip():
            print(proc.stderr[-1500:], file=sys.stderr)

        print("[4/4] 汇总")
        report_path = checkout / "data" / "ci" / "gate-report.json"
        if not report_path.is_file():
            print("没有生成 gate-report.json —— 门禁自身失败", file=sys.stderr)
            return 1
        report = json.loads(report_path.read_text())
        summary = report.get("summary", {})
        for check in report.get("checks", []):
            flag = "PASS" if check.get("ok") else "FAIL"
            print(f"  [{flag}] {check['name']:<22} {check.get('seconds', 0):>6.1f}s  "
                  f"{str(check.get('evidence', {}).get('summary', ''))[:80]}")
        print(f"\nGATE {'GREEN' if summary.get('ok') else 'RED'}: "
              f"{summary.get('passed')}/{summary.get('total')} 项通过")
        return 0 if summary.get("ok") else 1
    finally:
        if args.keep:
            print(f"临时目录保留在 {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

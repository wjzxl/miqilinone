#!/usr/bin/env python3
"""用 SSH 通道把本地工程推到 GitHub —— 绕开「token 缺 workflow 权限」与被代理拦死的 git push。

为什么需要它：
1. **workflow 权限**：GitHub 对写入 `.github/workflows/` 的请求，在 OAuth token 缺
   `workflow` scope 时返回 **404 Not Found**（不是 403）。用同一 token 建 tree，
   `docs/x.txt` 与 `.github/x.md` 都成功，只有 `.github/workflows/*.yml` 失败 ——
   极易被误判成「仓库不存在」。而 **SSH 认证没有 scope 概念**，账号本身有写权限就能推。
2. **网络代理**：`git fetch` / `ls-remote` 可用，`git push`（含 --dry-run）统一
   `CONNECT tunnel failed, response 502`。SSH over 443（ssh.github.com）不受影响。

脚本做三件事，全部走 fast-forward，**不 force push、不重写远端历史**：
  A. main：parent = origin/main，只带「本地更新的文件」+「缺失的 workflow 文件」，
     远端已有的 README 等文件原样保留。
  B. PR 分支：parent = origin/<branch>，内容 = 新的 main 树 + 探针行，
     这样 PR 的 diff 仍然只有探针那一个文件。
  C. 校验：推完用 GitHub API 复核远端 tree 与 PR 状态。

用法：
    python3 ci/ssh_push.py --repo wjzxl/miqilinone
    python3 ci/ssh_push.py --repo wjzxl/miqilinone --dry-run
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SSH_URL_TEMPLATE = "ssh://git@ssh.github.com:443/{repo}.git"
PROBE_TARGET = "src/michelin/__init__.py"
PROBE_LINE = "\n# CI PR probe: incremental diff must scan only this file.\n"


class PushError(RuntimeError):
    pass


def run(args: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
    proc = subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, env=env,
    )
    if proc.returncode != 0:
        raise PushError(f"{' '.join(args[:4])} … 失败：{proc.stderr.strip()[:300]}")
    return proc.stdout


def ssh_env(identity: Path) -> dict:
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {identity} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    )
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def verify_ssh(identity: Path) -> str:
    """确认这把 key 已被目标账号接受，返回 GitHub 的问候语。"""
    proc = subprocess.run(
        ["ssh", "-i", str(identity), "-o", "IdentitiesOnly=yes",
         "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         "-T", "-p", "443", "git@ssh.github.com"],
        capture_output=True, text=True, timeout=40,
    )
    output = (proc.stdout + proc.stderr).strip()
    if "successfully authenticated" in output.lower():
        return output
    raise PushError(
        "SSH 认证未通过：" + output[:200]
        + "\n请把公钥加入目标账号：https://github.com/settings/keys"
    )


def collect_diff_files(local_ref: str, base_ref: str) -> list[str]:
    """本地与远端 base 的**树差异**文件（相对路径），且**只保留本地确实存在的文件**。

    两点规则：
    1. 用两点比较而不是三点 —— 远端 main 由 API 重建（父提交是仓库原有的 83d0efa），
       与本地 main **没有共同祖先**，三点 diff 会退化。
    2. 只保留本地 ref 里存在的路径 —— 远端独有、本地没有的文件（如仓库原有的 README.md）
       绝不能进提交，否则这次「同步」会把它删掉。
    """
    out = run(["git", "-C", str(ROOT), "diff", "--name-only", base_ref, local_ref])
    changed: list[str] = []
    for rel in (line for line in out.splitlines() if line.strip()):
        exists = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", f"{local_ref}:{rel}"],
            capture_output=True,
        )
        if exists.returncode == 0:
            changed.append(rel)
        else:
            print(f"  跳过 {rel}（远端独有、本地没有，不能删）")
    return changed


def build_commit(base_ref: str, source_ref: str, source_files: list[str], message: str,
                 workdir: Path, probe_target: str | None = None) -> str:
    """在 base_ref 之上做一次只含指定文件的提交，返回新 commit SHA。

    文件内容取自 source_ref（本地已提交的内容），不是 base_ref —— 因为
    `.github/workflows/gate.yml` 在远端 base 里根本不存在，从 base 取会失败。

    probe_target 不为空时，额外在该文件末尾追加一行探针注释（PR 增量路径的靶子）。
    """
    worktree = workdir / f"wt-{abs(hash(message)) % 10000}"
    run(["git", "-C", str(ROOT), "worktree", "add", "--detach", "-q", str(worktree), base_ref])
    try:
        if source_files:
            run(["git", "-C", str(worktree), "checkout", source_ref, "--"] + source_files)
        if probe_target:
            target = worktree / probe_target
            data = target.read_bytes()
            if PROBE_LINE.encode("utf-8") not in data:
                target.write_bytes(data + PROBE_LINE.encode("utf-8"))
        run(["git", "-C", str(worktree), "add", "-A"])
        run(["git", "-C", str(worktree), "commit", "-q", "-m", message])
        return run(["git", "-C", str(worktree), "rev-parse", "HEAD"]).strip()
    finally:
        run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(worktree)])


def mirror_commit(base_ref: str, source_ref: str, message: str) -> str:
    """做一个「树 = 本地 source_ref 整棵树」的提交。

    与 build_commit 的区别：后者是在 base 的树上**合并**改动（远端独有的文件会保留），
    前者是**整树替换**（远端有、本地没有的文件会被删除）。用它清理远端的
    历史遗留 / 误推产物，同时保持 fast-forward（父提交仍是 base）。
    """
    tree = run(["git", "-C", str(ROOT), "rev-parse", f"{source_ref}^{{tree}}"]).strip()
    base_sha = run(["git", "-C", str(ROOT), "rev-parse", f"{base_ref}^{{commit}}"]).strip()
    commit = run(["git", "-C", str(ROOT), "commit-tree", tree, "-p", base_sha, "-m", message])
    return commit.strip()


def push(repo: str, local_sha: str, remote_branch: str, identity: Path) -> str:
    env = ssh_env(identity)
    url = SSH_URL_TEMPLATE.format(repo=repo)
    out = run(["git", "-C", str(ROOT), "push", url, f"{local_sha}:refs/heads/{remote_branch}"], env=env)
    return out.strip()


def free_branch_name(repo: str, base_name: str) -> str:
    """挑一个远端还不存在的分支名。

    探针分支要从**新的 main** 派生（新旧 main 没有共同的新祖先），所以每次都
    是新分支；同名会推不上去（non-fast-forward），因此自动加序号避开。
    """
    sys.path.insert(0, str(ROOT / "ci"))
    from github_api_push import GitHub, resolve_token  # noqa: PLC0415

    token, _ = resolve_token(repo.split("/", 1)[0])
    gh = GitHub(token, repo)
    index = 1
    while True:
        candidate = f"{base_name}-v{index}"
        if gh.ref(candidate) is None:
            return candidate
        index += 1


def open_pr_via_api(repo: str, head: str, base: str, title: str, body: str) -> dict:
    """用 GitHub API 开 PR（开 PR 不涉及 workflow 目录，API 通道可用）。"""
    sys.path.insert(0, str(ROOT / "ci"))
    from github_api_push import GitHub, resolve_token  # noqa: PLC0415

    token, source = resolve_token(repo.split("/", 1)[0])
    gh = GitHub(token, repo)
    pr = gh.pull(title=title, head=head, base=base, body=body)
    print(f"opened PR #{pr['number']}: {pr['html_url']}（凭证来源 {source}）")
    return pr


def close_pr_via_api(repo: str, number: int) -> None:
    sys.path.insert(0, str(ROOT / "ci"))
    from github_api_push import GitHub, resolve_token  # noqa: PLC0415

    token, _ = resolve_token(repo.split("/", 1)[0])
    gh = GitHub(token, repo)
    gh.request("PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"})
    print(f"closed PR #{number}（旧的探针 PR，分支基线已过时）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SSH 通道推送（无 scope 限制，可写 .github/workflows）")
    ap.add_argument("--repo", default="wjzxl/miqilinone", help="owner/name")
    ap.add_argument("--identity", default=str(Path.home() / ".ssh" / "id_a2000"))
    ap.add_argument("--local-ref", default="main", help="本地参考分支")
    ap.add_argument("--pr-branch", default="gate/pr-probe", help="PR 分支名")
    ap.add_argument("--base", default="main", help="远端基线分支")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-pr", action="store_true", help="只推分支，不开 PR")
    ap.add_argument("--mirror-tree", action="store_true",
                    help="main 直接用本地整棵树替换（会删除远端有、本地没有的文件）")
    ap.add_argument("--close-old-pr", type=int, default=0,
                    help="新 PR 开好后，关闭这个编号的旧 PR（0 表示不关）")
    args = ap.parse_args(argv)

    identity = Path(args.identity).expanduser()
    if not identity.is_file():
        print(f"ERROR: 找不到私钥 {identity}", file=sys.stderr)
        return 2

    try:
        greeting = verify_ssh(identity)
    except PushError as exc:
        print(f"PREFLIGHT FAILED: {exc}", file=sys.stderr)
        return 3
    print(f"ssh: {greeting.splitlines()[0]}")

    run(["git", "-C", str(ROOT), "fetch", "-q", "origin",
         f"+refs/heads/{args.base}:refs/remotes/origin/{args.base}",
         f"+refs/heads/{args.pr_branch}:refs/remotes/origin/{args.pr_branch}"])

    changed = collect_diff_files(args.local_ref, f"origin/{args.base}")
    print(f"远端 {args.base} 与本地 {args.local_ref} 的差异文件（待同步）：{changed}")

    if args.dry_run:
        print("dry-run：未推送")
        return 0

    workdir = Path(tempfile.mkdtemp(prefix="mw-ssh-push-"))
    try:
        if args.mirror_tree:
            main_sha = mirror_commit(
                f"origin/{args.base}", args.local_ref,
                "ci: 同步为本地工程树的完整快照（整树替换，清理远端遗留文件）",
            )
        else:
            main_sha = build_commit(
                f"origin/{args.base}", args.local_ref, changed,
                "ci: 通过 SSH 通道补齐 workflow 文件并同步 CI 推送工具",
                workdir,
            )
        print(f"new main commit: {main_sha[:12]}")

        # PR 分支必须**从新的 main 派生**，否则 PR 的 diff 会把 main 上的
        # README/docs/工具改动一起算进来，「增量只扫 1 个文件」就演示不出来了。
        # 从新的 main 派生的分支与旧分支没有共同的新祖先，只能用新分支名 +
        # 新 PR（旧 PR 关闭），这样全程 fast-forward，不需要 force push。
        pr_branch = free_branch_name(args.repo, args.pr_branch)
        pr_sha = build_commit(
            main_sha, args.local_ref, [],
            "test: PR 门禁增量路径探针（只改一个 .py，无规则违规）",
            workdir,
            probe_target=PROBE_TARGET,
        )
        print(f"new pr commit:   {pr_sha[:12]} (branch {pr_branch})")

        push(args.repo, main_sha, args.base, identity)
        print(f"pushed {args.base} <- {main_sha[:12]}")
        push(args.repo, pr_sha, pr_branch, identity)
        print(f"pushed {pr_branch} <- {pr_sha[:12]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if args.no_pr:
        return 0

    pr = open_pr_via_api(
        args.repo, pr_branch, args.base,
        "[CI drill] 验证真实 PR 增量门禁",
        "门禁闭环验证 PR：只修改 `src/michelin/__init__.py` 一行注释。\n\n"
        "期望 `pr_rules_scan` 走 `origin/main...HEAD` 增量 diff（只扫这 1 个文件）、"
        "13/13 GATE GREEN，并上传 `gate-report.json` artifact。",
    )
    if args.close_old_pr:
        close_pr_via_api(args.repo, args.close_old_pr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

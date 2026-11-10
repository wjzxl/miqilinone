"""在 git push 被网络代理拦截时，用 GitHub Git Data API 把本地 HEAD 推到远端并开 PR。

为什么需要它：某些网络环境下 `git push`（含 --dry-run）会被代理以
`CONNECT tunnel failed, response 502` 拒绝，而 `git fetch` / REST API 正常。
本模块不依赖 git 的 receive-pack 通道，直接构造 blob → tree → commit → ref，
语义等价于一次普通 push，且能保留远端已有的文件（base_tree 合并，不覆盖 README）。

授权：token 依次取自 MW_GITHUB_TOKEN、MW_GITHUB_TOKEN_FILE 指向的文件、
GitHub Copilot CLI 登录态（macOS 钥匙串 service `copilot-cli`）、`git credential fill`。
token 只在内存里使用，绝不回显、绝不写进仓库。
要求目标仓库 `permissions.push=true`（fine-grained PAT 还需要把该仓库纳入
repository access；写 .github/workflows/ 下的文件还需要 workflow 权限）。

**账号一致性是硬约束**：本脚本只接受 `login == 目标仓库 owner` 的凭证，
避免在多账号机器上误推、误建 PR。凭证不匹配就直接失败，而不是"凑合推一下"。

用法：
    python3 ci/github_api_push.py --repo owner/name --dry-run
    python3 ci/github_api_push.py --repo owner/name             # 推 main + 开 PR
    python3 ci/github_api_push.py --repo owner/name --no-pr     # 只推 main

推完远端 commit SHA 与本地不同（API 重建了对象），脚本会打印本地同步命令，
但不会自动 reset --hard 你的工作树。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
PROBE_TARGET = "src/michelin/__init__.py"
PROBE_LINE = "\n# CI PR probe: incremental diff must scan only this file.\n"
WORKFLOW_PREFIX = ".github/workflows/"


class APIError(RuntimeError):
    def __init__(self, status: int, path: str, body: str):
        super().__init__(f"GitHub API {status} on {path}: {body[:400]}")
        self.status = status
        self.path = path
        self.body = body


COPILOT_CLI_SERVICE = "copilot-cli"
COPILOT_CLI_ACCT = "https://github.com:{login}:github"


def _keychain_read(service: str, account: str) -> str | None:
    """从 macOS 钥匙串读一条通用密码；读不到返回 None（不弹窗、不回显）。"""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def copilot_cli_token(login: str) -> str | None:
    """取 GitHub Copilot CLI 为 `login` 保存的 GitHub token。

    CLI 登录后把凭证存在钥匙串 service=`copilot-cli`，account 形如
    `https://github.com:<login>:github`（不同版本后缀可能不同）。
    本地 `~/.copilot/data.db` 的 accounts 表只记录 login，不存 token 本身，
    所以必须走钥匙串。
    """
    candidates = [
        COPILOT_CLI_ACCT.format(login=login),
        f"https://github.com:{login}",
        login,
    ]
    for account in candidates:
        value = _keychain_read(COPILOT_CLI_SERVICE, account)
        if value:
            return value
    # 兜底：取 service 下的第一条目，看它的 acct 是否指向目标账号。
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", COPILOT_CLI_SERVICE],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r'"acct"<blob>="([^"]*)"', proc.stdout)
    if not match:
        return None
    acct = match.group(1)
    if login not in acct:
        return None
    return _keychain_read(COPILOT_CLI_SERVICE, acct)


def _git_credential_token() -> str | None:
    cred = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    rows = dict(line.split("=", 1) for line in cred.stdout.splitlines() if "=" in line)
    return rows.get("password") or None


def credential_candidates(account: str) -> list[tuple[str, str]]:
    """按优先级列出 (来源, token) 候选，全部只存在于内存。"""
    candidates: list[tuple[str, str]] = []
    env = os.environ.get("MW_GITHUB_TOKEN")
    if env:
        candidates.append(("env:MW_GITHUB_TOKEN", env.strip()))
    token_file = os.environ.get("MW_GITHUB_TOKEN_FILE")
    if token_file:
        path = Path(token_file).expanduser()
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if not value:
                raise RuntimeError(f"token 文件为空：{token_file}")
            candidates.append((f"file:{token_file}", value))
    copilot = copilot_cli_token(account)
    if copilot:
        candidates.append((f"keychain:{COPILOT_CLI_SERVICE}", copilot))
    git_cred = _git_credential_token()
    if git_cred:
        candidates.append(("git-credential", git_cred))
    return candidates


def token_login(token: str) -> str | None:
    req = urllib.request.Request(
        f"{API}/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "michelin-workbench-api-push",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()).get("login")
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None


def resolve_token(account: str) -> tuple[str, str]:
    """返回 (token, 来源)。只接受 login == account 的凭证。"""
    candidates = credential_candidates(account)
    if not candidates:
        raise RuntimeError(
            f"找不到 GitHub 凭证（目标账号 {account}）。\n"
            "请设置 MW_GITHUB_TOKEN / MW_GITHUB_TOKEN_FILE，"
            "或先用 `copilot login` 登录对应账号，"
            "或用 git credential 配置钥匙串。"
        )
    seen: set[str] = set()
    for source, token in candidates:
        if token in seen:
            continue
        seen.add(token)
        login = token_login(token)
        if login is None:
            print(f"  跳过 {source}：无法通过 /user 校验该凭证", file=sys.stderr)
            continue
        if login != account:
            print(f"  跳过 {source}：凭证账号是 {login}，与目标账号 {account} 不一致",
                  file=sys.stderr)
            continue
        return token, source
    raise RuntimeError(
        f"没有任何可用凭证属于账号 {account}。\n"
        "为避免推错账号，本脚本不会使用其它身份的 token。\n"
        f"请用 `copilot login` 登录 {account}，或提供 {account} 的 PAT。"
    )


class GitHub:
    # 网络代理环境下偶发 SSL EOF / 502 / 503，重试比直接失败更可靠。
    RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(self, token: str, repo: str, attempts: int = 4):
        self.token = token
        self.repo = repo
        self.attempts = attempts

    def request(self, method: str, path: str, payload: dict | None = None):
        url = f"{API}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        last: Exception | None = None
        for attempt in range(self.attempts):
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "michelin-workbench-api-push",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                if exc.code in self.RETRY_STATUS and attempt < self.attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise APIError(exc.code, path, body) from None
            except (urllib.error.URLError, ssl.SSLError, TimeoutError, ConnectionError) as exc:
                last = exc
                if attempt < self.attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"网络错误 {path}: {exc}") from None
        raise RuntimeError(f"网络错误 {path}: {last}")

    def repo_info(self) -> dict:
        return self.request("GET", f"/repos/{self.repo}")

    def ref(self, name: str) -> str | None:
        try:
            return self.request("GET", f"/repos/{self.repo}/git/ref/heads/{name}")["object"]["sha"]
        except APIError as exc:
            if exc.status == 404:
                return None
            # GitHub 对「一个 commit 都没有」的仓库返回 409 而不是 404。
            if exc.status == 409 and "Git Repository is empty" in exc.body:
                return None
            raise

    def commit_tree(self, sha: str) -> str:
        return self.request("GET", f"/repos/{self.repo}/git/commits/{sha}")["tree"]["sha"]

    def blob(self, content: bytes) -> str:
        return self.request("POST", f"/repos/{self.repo}/git/blobs", {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })["sha"]

    def tree(self, entries: list[dict], base_tree: str | None = None) -> str:
        payload: dict = {"tree": entries}
        if base_tree:
            payload["base_tree"] = base_tree
        return self.request("POST", f"/repos/{self.repo}/git/trees", payload)["sha"]

    def commit(self, message: str, tree: str, parents: list[str]) -> str:
        return self.request("POST", f"/repos/{self.repo}/git/commits", {
            "message": message, "tree": tree, "parents": parents,
        })["sha"]

    def create_ref(self, ref: str, sha: str) -> None:
        self.request("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{ref}", "sha": sha})

    def update_ref(self, ref: str, sha: str) -> None:
        self.request("PATCH", f"/repos/{self.repo}/git/refs/heads/{ref}", {"sha": sha, "force": False})

    def pull(self, title: str, head: str, base: str, body: str) -> dict:
        return self.request("POST", f"/repos/{self.repo}/pulls", {
            "title": title, "head": head, "base": base, "body": body,
        })

    def runs(self, head_sha: str) -> dict:
        return self.request("GET", f"/repos/{self.repo}/actions/runs?head_sha={head_sha}&per_page=5")


def tracked_files(root: Path) -> list[tuple[str, int, bytes]]:
    """返回 (相对路径, git mode, 内容)，完全以本地 HEAD 的索引为准。"""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-s", "-z"],
        capture_output=True, check=True,
    ).stdout.split(b"\0")
    files: list[tuple[str, int, bytes]] = []
    for row in out:
        if not row:
            continue
        meta, _, raw_path = row.partition(b"\t")
        parts = meta.decode("ascii").split()
        mode = int(parts[0], 8)
        rel = raw_path.decode("utf-8")
        files.append((rel, mode, (root / rel).read_bytes()))
    return files


def check_permission(gh: GitHub) -> dict:
    info = gh.repo_info()
    perms = info.get("permissions") or {}
    if info.get("message") and not info.get("full_name"):
        raise RuntimeError(f"仓库不可访问：{info.get('message')}")
    if perms.get("push") is not True:
        raise RuntimeError(
            f"凭证对 {info.get('full_name')} 没有 push 权限（permissions={perms}）。\n"
            "需要以下之一：① 换用该账号/该仓库有 contents:write 的 PAT；"
            "② 让仓库所有者把当前凭证账号加为可写协作者；"
            "③ 把仓库转移或 fork 到当前凭证账号下。\n"
            "注意：写 .github/workflows/ 下的文件还需要 token 具备 workflow 权限。"
        )
    return info


def push_main(gh: GitHub, root: Path, base: str, files: list[tuple[str, int, bytes]]) -> dict:
    base_sha = gh.ref(base)
    base_tree = gh.commit_tree(base_sha) if base_sha else None

    with ThreadPoolExecutor(max_workers=6) as pool:
        shas = list(pool.map(lambda item: gh.blob(item[2]), files))
    entries = [
        {"path": rel, "mode": f"{mode:o}", "type": "blob", "sha": sha}
        for (rel, mode, _), sha in zip(files, shas)
    ]

    workflow_entries = [e for e in entries if e["path"].startswith(WORKFLOW_PREFIX)]
    skipped: list[str] = []
    try:
        tree_sha = gh.tree(entries, base_tree)
    except APIError as exc:
        # GitHub 对「要写入 .github/workflows/ 但 token 没有 workflow 权限」的请求
        # 返回 404 Not Found（不是 403），很容易被误判成「仓库不存在」。
        if exc.status != 404 or not workflow_entries:
            raise
        non_workflow = [e for e in entries if not e["path"].startswith(WORKFLOW_PREFIX)]
        tree_sha = gh.tree(non_workflow, base_tree)
        skipped = sorted(e["path"] for e in workflow_entries)

    commit_sha = gh.commit(
        "chore: 初始化 AI 米其林智能工作台 P0-P5 可运行骨架\n\n"
        f"{len(files)} 个文件：事件证据链 / 独立 verifier / 六段式验证 skill / 双后端规则 /\n"
        "外部仓库泛化 / 13 项 CI 门禁 / 真实 PR diff / 运行时隔离与预算熔断。\n"
        "通过 Git Data API 提交（git push 通道被网络代理拦截）。",
        tree_sha, [base_sha] if base_sha else [],
    )
    if base_sha:
        gh.update_ref(base, commit_sha)
    else:
        gh.create_ref(base, commit_sha)
    return {"base_before": base_sha, "commit": commit_sha, "tree": tree_sha,
            "files": len(files), "skipped_files": skipped}


def open_pr(gh: GitHub, base: str, branch: str, files: list[tuple[str, int, bytes]]) -> dict:
    """在 main 之上做一个只改一个 .py 的 PR，专门验证增量门禁路径。"""
    main_sha = gh.ref(base)
    if not main_sha:
        raise RuntimeError("main 不存在，无法开 PR")
    target = dict((rel, content) for rel, _, content in files)
    if PROBE_TARGET not in target:
        raise RuntimeError(f"探针目标不存在：{PROBE_TARGET}")
    probe_sha = gh.blob(target[PROBE_TARGET] + PROBE_LINE.encode("utf-8"))
    probe_tree = gh.tree(
        [{"path": PROBE_TARGET, "mode": "100644", "type": "blob", "sha": probe_sha}],
        gh.commit_tree(main_sha),
    )
    probe_commit = gh.commit(
        "test: PR 门禁增量路径探针（只改一个 .py，无规则违规）", probe_tree, [main_sha]
    )
    try:
        gh.create_ref(branch, probe_commit)
    except APIError as exc:
        if exc.status != 422:
            raise
        gh.update_ref(branch, probe_commit)
    pr = gh.pull(
        title="[CI drill] 验证真实 PR 增量门禁",
        head=branch, base=base,
        body=("这是一次门禁闭环验证 PR：只修改 `src/michelin/__init__.py` 一行注释，"
              "期望 `pr_rules_scan` 走 `origin/main...HEAD` 增量 diff、13/13 GATE GREEN，"
              "并上传 gate-report.json artifact。"),
    )
    return {"branch": branch, "commit": probe_commit, "pr_number": pr["number"],
            "pr_url": pr["html_url"], "changed_file": PROBE_TARGET}


def workflow_scope_hint() -> str:
    return (
        "缺 workflow 权限：GitHub 对「写入 .github/workflows/」的请求在缺 workflow scope 时\n"
        "返回 404 Not Found（不是 403），不要误判成仓库不存在。\n"
        "解决办法（任选其一）：\n"
        "  ① 用 wjzxl 账号创建 fine-grained PAT，勾选 Contents: Read and write、\n"
        "     Workflows: Read and write、Pull requests: Read and write，\n"
        "     存到 ~/.config/michelin/gh_token（chmod 600），再用 MW_GITHUB_TOKEN_FILE 指向它；\n"
        "  ② 或用 `copilot login --with-token` 把该 PAT 交给 Copilot CLI 保管，\n"
        "     本脚本会自动从钥匙串读取；\n"
        "  ③ 或直接在 GitHub 网页端手动新建 .github/workflows/gate.yml（路径与内容见本地文件）。"
    )


def probe_capabilities(gh: GitHub, repo: str) -> dict:
    """报告当前凭证能写什么：账号、scope、能否写 .github/workflows/。"""
    login = token_login(gh.token)
    scopes: list[str] = []
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"{API}/user",
                headers={"Authorization": f"Bearer {gh.token}",
                         "Accept": "application/vnd.github+json",
                         "User-Agent": "michelin-workbench-api-push"}), timeout=30) as resp:
            for key, value in resp.headers.items():
                if key.lower() == "x-oauth-scopes":
                    scopes = [s.strip() for s in value.split(",") if s.strip()]
    except (urllib.error.HTTPError, urllib.error.URLError, ssl.SSLError):
        pass

    base_sha = gh.ref("main")
    base_tree = gh.commit_tree(base_sha) if base_sha else None
    probe = gh.blob(b"probe\n")
    can_write_workflows = False
    detail = ""
    try:
        gh.request("POST", f"/repos/{repo}/git/trees", {
            "tree": [{"path": f"{WORKFLOW_PREFIX}.mw-capability-probe.yml",
                      "mode": "100644", "type": "blob", "sha": probe}],
            **({"base_tree": base_tree} if base_tree else {}),
        })
        can_write_workflows = True
    except APIError as exc:
        detail = f"{exc.status} {exc.body[:120]}"
    return {
        "login": login,
        "oauth_scopes": scopes,
        "has_workflow_scope": "workflow" in scopes,
        "can_write_workflows": can_write_workflows,
        "workflow_probe": detail or "OK",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Git Data API 推送 + 真实 PR 门禁闭环")
    ap.add_argument("--repo", default="", help="owner/name；默认从 origin 解析")
    ap.add_argument("--account", default="", help="凭证账号；默认取目标仓库的 owner（必须一致）")
    ap.add_argument("--base", default="main")
    ap.add_argument("--branch", default="gate/pr-probe")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--dry-run", action="store_true", help="只做权限预检与文件统计，不写远端")
    ap.add_argument("--no-pr", action="store_true", help="只推 main，不开 PR")
    ap.add_argument("--pr-only", action="store_true",
                    help="不开新 commit，只基于远端已有 main 开探针 PR")
    ap.add_argument("--only-workflows", action="store_true",
                    help="只推 .github/workflows/ 下的文件（凭证升级到有 workflow 权限后补推用）")
    ap.add_argument("--capabilities", action="store_true",
                    help="只报告凭证能力（账号、scope、能否写 workflows），不写远端")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    repo = args.repo
    if not repo:
        url = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        repo = url.split("github.com/", 1)[-1].removesuffix(".git") if "github.com/" in url else ""
    if not repo or "/" not in repo:
        print("ERROR: 无法解析目标仓库，请用 --repo owner/name 指定", file=sys.stderr)
        return 2
    owner = repo.split("/", 1)[0]
    account = args.account or owner

    files = tracked_files(root)
    total = sum(len(content) for _, _, content in files)
    print(f"repo={repo}  account={account}  base={args.base}  files={len(files)}  "
          f"bytes={total:,}  dry_run={args.dry_run}")

    try:
        token, source = resolve_token(account)
    except RuntimeError as exc:
        print(f"PREFLIGHT FAILED: {exc}", file=sys.stderr)
        return 3
    print(f"credential: {source}（账号 {account}，已通过 /user 校验）")

    gh = GitHub(token, repo)
    try:
        info = check_permission(gh)
    except (RuntimeError, APIError) as exc:
        print(f"PREFLIGHT FAILED: {exc}", file=sys.stderr)
        return 3
    print(f"preflight: {info.get('full_name')} private={info.get('private')} "
          f"push={info.get('permissions', {}).get('push')} default={info.get('default_branch')}")

    if args.capabilities:
        caps = probe_capabilities(gh, repo)
        print(json.dumps(caps, indent=2, ensure_ascii=False))
        return 0 if caps.get("can_write_workflows") else 1

    if args.only_workflows:
        wf = [f for f in files if f[0].startswith(WORKFLOW_PREFIX)]
        if not wf:
            print("没有 .github/workflows/ 下的文件可推", file=sys.stderr)
            return 2
        result = {"repo": repo, "workflows": push_main(gh, root, args.base, wf)}
        skipped = result["workflows"].get("skipped_files") or []
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if skipped:
            print(f"\n仍被拒绝：{skipped}\n{workflow_scope_hint()}", file=sys.stderr)
            return 4
        return 0

    if args.dry_run:
        base_sha = gh.ref(args.base)
        print(json.dumps({
            "ok": True, "mode": "dry-run", "files": len(files), "bytes": total,
            "base_sha": base_sha, "base_has_readme": bool(base_sha),
            "would_push_main": True, "would_open_pr": not args.no_pr,
            "pr_changed_file": PROBE_TARGET,
            "workflow_files": sorted(f[0] for f in files if f[0].startswith(WORKFLOW_PREFIX)),
        }, indent=2, ensure_ascii=False))
        return 0

    result: dict = {"repo": repo, "base": args.base}
    if args.pr_only:
        result["push"] = {"skipped": True, "reason": "--pr-only：不改动远端 main"}
        print("pr-only: 复用远端已有 main，不产生新 commit")
    else:
        result["push"] = push_main(gh, root, args.base, files)
        print(f"pushed {args.base}: {result['push']['commit'][:12]} ({len(files)} files)")
        skipped = result["push"].get("skipped_files") or []
        if skipped:
            print(f"\n注意：{len(skipped)} 个 workflow 文件未能写入（凭证缺 workflow 权限）：",
                  file=sys.stderr)
            for path in skipped:
                print(f"  - {path}", file=sys.stderr)
            print(workflow_scope_hint(), file=sys.stderr)
    if not args.no_pr:
        result["pr"] = open_pr(gh, args.base, args.branch, files)
        print(f"opened PR #{result['pr']['pr_number']}: {result['pr']['pr_url']}")
        try:
            runs = gh.runs(result["pr"]["commit"])
            result["workflow_runs"] = [
                {"id": r["id"], "name": r["name"], "status": r["status"],
                 "conclusion": r.get("conclusion"), "html_url": r["html_url"]}
                for r in runs.get("workflow_runs", [])
            ]
        except (APIError, RuntimeError) as exc:
            result["workflow_runs_error"] = str(exc)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n本地同步（可选）：git -C {root} fetch origin && "
          f"git -C {root} reset --hard origin/{args.base}")
    print("查询门禁结果：python3 ci/github_api_push.py --repo ... --dry-run 之外，"
          "也可直接看 PR 的 Checks / Actions 页面与 gate-report.json artifact。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

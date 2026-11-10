"""console script 入口；`bin/ledger` 是同一份逻辑的无后缀脚本版。"""

from __future__ import annotations

import sys

from .core import total


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] != "total":
        print("USAGE: ledger total <amount> ...")
        return 2
    print(f"TOTAL={total(args[1:])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

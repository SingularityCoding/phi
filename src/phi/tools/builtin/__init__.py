"""汇总全部经统一注册表和 dispatcher 执行的内置 Tool。"""

from phi.tools.builtin.files import edit_file, read_file, write_file
from phi.tools.builtin.search import find_paths, grep_files, list_directory
from phi.tools.builtin.shell import run_bash

BUILTIN_TOOLS = (
    read_file,
    write_file,
    edit_file,
    run_bash,
    grep_files,
    find_paths,
    list_directory,
)

__all__ = ["BUILTIN_TOOLS"]

"""提供 ``python -m phi`` 与控制台脚本共用的应用入口。"""

from phi.cli import app


def main() -> None:
    """把进程控制权交给 Typer CLI Host。"""

    app()

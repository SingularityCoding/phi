"""定义 Model 边界可供 Harness 按类型处理的失败。"""

from __future__ import annotations


class ModelError(Exception):
    """表示发生在 Model 边界的失败基类。"""


class ModelHTTPError(ModelError):
    """表示非成功 HTTP 响应或 HTTP 传输失败。"""

    def __init__(self, status_code: int, body: str) -> None:
        """保留状态码与响应体，供上层基于结构而非文案选择策略。"""

        self.status_code = status_code
        self.body = body
        super().__init__(f"Model HTTP request failed with status {status_code}: {body}")


class ModelContextLimitError(ModelHTTPError):
    """表示提供方以结构化错误码拒绝超出 Context 限制的请求。"""


class ModelProtocolError(ModelError):
    """表示传输成功、但响应不符合 Model 协议。"""

    def __init__(self, detail: str) -> None:
        """记录可诊断的协议违规详情。"""

        self.detail = detail
        super().__init__(detail)


class ModelTimeoutError(ModelError):
    """表示 Model 请求超过配置的超时时间。"""

    def __init__(self) -> None:
        """使用稳定的对外错误文案初始化超时错误。"""

        super().__init__("Model request timed out")

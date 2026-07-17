import pytest

from phi.model import (
    ContentDelta,
    FinishEvent,
    ModelProtocolError,
    ReasoningDelta,
    ResponseAssembler,
    ToolCall,
    ToolCallDelta,
    Usage,
    UsageEvent,
)


def test_assembler_groups_fragmented_tool_calls_by_index():
    assembler = ResponseAssembler()

    assembler.absorb(
        ToolCallDelta(index=1, id="call_weather", name="weather", arguments_fragment='{"city":')
    )
    assembler.absorb(
        ToolCallDelta(index=0, id="call_read", name="read_file", arguments_fragment='{"path":')
    )
    assembler.absorb(ToolCallDelta(index=1, arguments_fragment='"Shanghai"}'))
    assembler.absorb(ToolCallDelta(index=0, arguments_fragment='"README.md"}'))

    response = assembler.build()

    assert response.tool_calls == [
        ToolCall(id="call_read", name="read_file", arguments={"path": "README.md"}),
        ToolCall(id="call_weather", name="weather", arguments={"city": "Shanghai"}),
    ]


def test_assembler_absorbs_usage_after_finish_without_clobbering_finish_reason():
    assembler = ResponseAssembler()
    usage = Usage(prompt_tokens=10, completion_tokens=4, total_tokens=14)
    finish_raw = {"choices": [{"finish_reason": "stop"}]}
    usage_raw = {"choices": [{"delta": {}, "finish_reason": None}], "usage": {}}

    assembler.absorb(ReasoningDelta("checking"))
    assembler.absorb(ContentDelta("done"))
    assembler.absorb(FinishEvent(finish_reason="stop", raw=finish_raw))
    assembler.absorb(UsageEvent(usage=usage, raw=usage_raw))

    response = assembler.build()

    assert response.reasoning == "checking"
    assert response.content == "done"
    assert response.finish_reason == "stop"
    assert response.usage == usage
    assert response.raw == usage_raw


@pytest.mark.parametrize("arguments", ["{not-json", "[]", '{"value": NaN}', '{"value": Infinity}'])
def test_assembler_rejects_tool_call_arguments_that_are_not_json_objects(arguments: str):
    assembler = ResponseAssembler()
    assembler.absorb(
        ToolCallDelta(
            index=0,
            id="call_bad",
            name="broken",
            arguments_fragment=arguments,
        )
    )

    with pytest.raises(ModelProtocolError):
        assembler.build()

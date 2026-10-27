import json

import httpx
import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.usage import RequestUsage

from box_agent_sdlc.agents import AgentRuntime, ScriptedRuntime, WorkspaceTools
from box_agent_sdlc.agents.runtime import ProviderRejected
from box_agent_sdlc.integrations import Workspace


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def request(self, messages, settings, parameters):
        self.requests.append((list(messages), parameters))
        return next(self.responses)


class Tools:
    def __init__(self):
        self.calls = []

    async def invoke(self, name, args, workspace, op_id):
        self.calls.append((name, args, workspace, op_id))
        return "export const discount = .1;"


def response(name, args, call="call-1"):
    return ModelResponse(
        parts=[ToolCallPart(tool_name=name, args=args, tool_call_id=call)],
        usage=RequestUsage(input_tokens=100, output_tokens=20),
    )


async def test_agent_yields_after_each_call_and_checkpoint_survives_restart():
    model = Model(
        [
            response("read_file", {"path": "src/cart.js"}),
            response(
                "final_result", {"summary": "Fixed discount logic and tests", "tests_passed": True}, "final"
            ),
        ]
    )
    tools = Tools()
    runtime = AgentRuntime(tools, model_factory=lambda _: model)
    ws = Workspace("demo/storefront", "runs/test/logic")
    first = await runtime.step(
        "turn-1", "coder", {"subtask": "Fix the discount"}, {}, ws, "openai:test-model"
    )
    assert not first.done and first.checkpoint["next_action"] == "tool"
    assert len(model.requests) == 1 and tools.calls == []
    assert first.usage["requests"] == 1
    assert first.usage["input_tokens"] == 100
    assert first.usage["price_known"] is False
    # New process: the checkpoint contains all request/response state, not a live Agent.
    runtime = AgentRuntime(tools, model_factory=lambda _: model)
    second = await runtime.step(
        "turn-1", "coder", {}, json.loads(json.dumps(first.checkpoint)), ws, "openai:test-model"
    )
    assert len(model.requests) == 1
    assert tools.calls[0][-1] == "turn-1:tool:call-1"
    assert second.usage == {} and second.checkpoint["next_action"] == "model"
    third = await runtime.step("turn-1", "coder", {}, second.checkpoint, ws, "openai:test-model")
    assert third.done and third.output["tests_passed"]
    assert len(model.requests) == 2
    again = await runtime.step("turn-1", "coder", {}, third.checkpoint, ws, "openai:test-model")
    assert again.done and len(model.requests) == 2 and again.usage == {}


async def test_read_only_role_cannot_write_even_if_provider_requests_it():
    tools = Tools()
    model = Model([response("write_file", {"path": "x", "content": "bad"})])
    runtime = AgentRuntime(tools, model_factory=lambda _: model)
    ws = Workspace("demo/storefront", "branch")
    first = await runtime.step("t", "reviewer", {}, {}, ws, "openai:test")
    second = await runtime.step("t", "reviewer", {}, first.checkpoint, ws, "openai:test")
    assert tools.calls == []
    assert "not permitted" in json.dumps(second.checkpoint)
    names = {t.name for t in model.requests[0][1].function_tools}
    assert "write_file" not in names


async def test_invalid_final_schema_is_checkpointed_for_correction():
    model = Model([response("final_result", {"summary": "missing tests flag"})])
    runtime = AgentRuntime(model_factory=lambda _: model)
    result = await runtime.step("t", "coder", {}, {}, None, "openai:test")
    assert not result.done
    assert "Invalid structured result" in json.dumps(result.checkpoint)


async def test_final_allowed_request_finishes_with_verified_structured_findings_without_more_tools():
    class NoShippingMatch(Tools):
        async def invoke(self, name, args, workspace, op_id):
            self.calls.append((name, args, workspace, op_id))
            return []

    model = Model(
        [response("search_code", {"pattern": "shipping rate"}, f"search-{i}") for i in range(14)]
        + [response("final_result", {"questions": ["What shipping rate should apply below $75?"]}, "last")]
    )
    tools = NoShippingMatch()
    runtime = AgentRuntime(tools, model_factory=lambda _: model)
    checkpoint = {}
    workspace = Workspace("demo/storefront", "branch")
    for _ in range(14):
        proposed = await runtime.step("t", "clarifier", {}, checkpoint, workspace, "openai:test")
        completed = await runtime.step("t", "clarifier", {}, proposed.checkpoint, workspace, "openai:test")
        checkpoint = json.loads(json.dumps(completed.checkpoint))
    # A replacement runtime uses the persisted count, so restart cannot reset the allowance.
    runtime = AgentRuntime(tools, model_factory=lambda _: model)
    result = await runtime.step("t", "clarifier", {}, checkpoint, workspace, "openai:test")
    assert result.done
    assert result.output == {"questions": ["What shipping rate should apply below $75?"]}
    assert result.checkpoint["request_count"] == len(model.requests) == 15
    assert len(tools.calls) == 14
    assert model.requests[-2][1].function_tools
    assert model.requests[-1][1].function_tools == []
    assert [tool.name for tool in model.requests[-1][1].output_tools] == ["final_result"]
    assert not model.requests[-1][1].allow_text_output
    last_prompt = model.requests[-1][0][-1].parts[0].content
    assert "verified findings" in last_prompt and "Do not invent" in last_prompt


async def test_final_request_keeps_schema_validation_and_does_not_add_paid_allowance():
    from box_agent_sdlc.agents.runtime import AgentProtocolError

    model = Model([response("final_result", {"summary": "Unverified work; tests flag is missing"})])
    runtime = AgentRuntime(model_factory=lambda _: model)
    checkpoint = {
        "turn_id": "t",
        "role": "coder",
        "request_count": 39,
        "next_action": "model",
        "messages": [],
    }
    result = await runtime.step("t", "coder", {}, checkpoint, None, "openai:test")
    assert not result.done and result.checkpoint["request_count"] == 40
    assert "Invalid structured result" in json.dumps(result.checkpoint)
    assert model.requests[-1][1].function_tools == []
    with pytest.raises(AgentProtocolError, match="40-request turn limit"):
        await runtime.step("t", "coder", {}, result.checkpoint, None, "openai:test")
    assert len(model.requests) == 1


async def test_new_turn_reuses_recorded_agent_history():
    model = Model(
        [
            response("final_result", {"questions": []}),
            response("final_result", {"questions": ["Placement?"]}, "f2"),
        ]
    )
    runtime = AgentRuntime(model_factory=lambda _: model)
    first = await runtime.step("one", "clarifier", {"issue_title": "First"}, {}, None, "openai:test")
    next_turn = await runtime.step(
        "two", "clarifier", {"issue_title": "Second"}, first.checkpoint, None, "openai:test"
    )
    assert next_turn.done
    assert "First" in json.dumps(next_turn.checkpoint["messages"])
    assert "Second" in json.dumps(next_turn.checkpoint["messages"])
    assert next_turn.checkpoint["request_count"] == 1


async def test_provider_rejected_input_is_permanent():
    class Reject:
        async def request(self, *_):
            error = RuntimeError("sensitive provider response")
            error.status_code = 401
            raise error

    with pytest.raises(ProviderRejected, match="401"):
        await AgentRuntime(model_factory=lambda _: Reject()).step("t", "triager", {}, {}, None, "openai:test")


async def test_mutating_workspace_tools_require_durable_effect_ledger():
    class Workspaces:
        calls = 0

        async def write_file(self, workspace, path, content, op_id):
            self.calls += 1
            return {"sha": "a" * 40}

    manager = Workspaces()
    ws = Workspace("demo/storefront", "branch")
    tools = WorkspaceTools(manager)
    with pytest.raises(RuntimeError, match="effect ledger"):
        await tools.invoke("write_file", {"path": "x", "content": "y"}, ws, "op")
    ledger = {}

    async def effect(op_id, request, execute, reconcile):
        if op_id not in ledger:
            ledger[op_id] = await execute()
        return ledger[op_id]

    tools.effect = effect
    await tools.invoke("write_file", {"path": "x", "content": "y"}, ws, "op")
    await tools.invoke("write_file", {"path": "x", "content": "y"}, ws, "op")
    assert manager.calls == 1


async def test_scripted_execution_is_labelled_and_has_zero_paid_usage():
    result = await ScriptedRuntime().step("t", "planner", {"issue_title": "Discount code"})
    assert result.done and result.usage["requests"] == 0
    assert result.event["scripted"] and not result.event["model_called"]
    assert len(result.output["subtasks"]) == 2


def test_budget_preflight_requires_known_price_and_reserves_output_allowance():
    reservation = AgentRuntime.reservation_usd("openai:gpt-4.1", {"issue_title": "A small change"}, {})
    assert reservation is not None and reservation > 0
    assert AgentRuntime.reservation_usd("openai:unknown-nonexistent-model", {}, {}) is None
    large = AgentRuntime.reservation_usd("openai:gpt-4.1", {"issue_title": "A larger change" * 1000}, {})
    assert large > reservation


async def test_real_pydantic_ai_openai_adapter_emits_tool_schema_and_checkpoints_provider_output():
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.url.path == "/v1/responses"
        assert body["max_output_tokens"] == 8000
        assert any(tool["name"] == "final_result" for tool in body["tools"])
        return httpx.Response(
            200,
            json={
                "id": "resp-test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-4.1",
                "output": [
                    {
                        "id": "fc-test",
                        "type": "function_call",
                        "status": "completed",
                        "name": "final_result",
                        "call_id": "call-final",
                        "arguments": '{"questions":[]}',
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        sdk = AsyncOpenAI(
            api_key="test-key", max_retries=0, base_url="https://models.test/v1", http_client=http
        )
        model = OpenAIResponsesModel("gpt-4.1", provider=OpenAIProvider(openai_client=sdk))
        result = await AgentRuntime(model_factory=lambda _: model).step(
            "t", "clarifier", {"issue_title": "Discount"}, {}, None, "openai:gpt-4.1"
        )
    assert len(seen) == 1 and result.done and result.output == {"questions": []}
    assert result.usage["input_tokens"] == 10 and result.usage["price_known"]
    assert "resp-test" in json.dumps(result.checkpoint["messages"])

from agents.observer_agent import ObserverAgent
from types import SimpleNamespace


def test_parse_eval_response_with_direct_json():
    observer = ObserverAgent.__new__(ObserverAgent)
    parsed = observer._parse_eval_response(
        '{"hallucination_detected": true, "incorrect_claim": "x", "correct_fact": "y", "details": "z"}'
    )
    assert parsed is not None
    assert parsed["hallucination_detected"] is True
    assert parsed["incorrect_claim"] == "x"
    assert parsed["correct_fact"] == "y"


def test_parse_eval_response_with_wrapped_json_text():
    observer = ObserverAgent.__new__(ObserverAgent)
    parsed = observer._parse_eval_response(
        'Result: {"hallucination_detected": false, "incorrect_claim": "", "correct_fact": "", "details": ""}'
    )
    assert parsed is not None
    assert parsed["hallucination_detected"] is False


def test_format_tool_facts_uses_runtime_tool_facts():
    observer = ObserverAgent.__new__(ObserverAgent)
    userdata = SimpleNamespace(runtime_tool_facts={"availability": {"times": ["07:00"]}})
    output = observer._format_tool_facts(userdata)
    assert '"availability"' in output

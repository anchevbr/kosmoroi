import httpx
import pytest
from greek_news.config import Settings
from greek_news.enrichment import EvidenceAnalysis
from greek_news.llm import ModelFailure, OpenAIClient, strict_schema
from greek_news.article_schema import ArticleDraft


def test_provider_request_has_output_cap_strict_schema_and_no_stored_conversation():
    requests = []
    def respond(request):
        import json
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"status": "completed", "model": "configured-model",
            "usage": {"input_tokens": 20, "output_tokens": 10},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"answer":"ok"}'}]}]})
    client = OpenAIClient(Settings(_env_file=None, llm_provider="openai", llm_api_key="synthetic-test-key"), transport=httpx.MockTransport(respond))
    result = client.request("Analyze only this data", {"text": "source"}, {"type": "object", "properties": {"answer": {"type": "string"}}}, 800)
    assert result.data == {"answer": "ok"} and result.input_tokens == 20
    assert requests[0]["max_output_tokens"] == 800 and requests[0]["store"] is False
    assert requests[0]["text"]["format"]["schema"]["required"] == ["answer"]
    assert requests[0]["text"]["format"]["schema"]["additionalProperties"] is False


def test_search_uses_citation_urls_as_leads_and_limits_tool_calls():
    def respond(request):
        import json
        body = json.loads(request.content)
        assert body["tools"][0]["type"] == "web_search" and body["max_tool_calls"] == 1
        return httpx.Response(200, json={"status":"completed", "usage":{"input_tokens":10,"output_tokens":10},
            "output":[{"type":"web_search_call","action":{"sources":[{"url":"https://news.example/story"}]}},
                      {"type":"message","content":[{"type":"output_text","text":"News lead", "annotations":[{"type":"url_citation","url":"https://news.example/story"}]}]}]})
    client = OpenAIClient(Settings(_env_file=None, llm_provider="openai", llm_api_key="synthetic-test-key"), transport=httpx.MockTransport(respond))
    result = client.request("Discover", {"query":"local news"}, None, 800, search=True)
    assert result.data["urls"] == ["https://news.example/story"]


def test_incomplete_response_is_never_treated_as_an_article():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status":"incomplete",
                "usage":{"input_tokens":100,"output_tokens":50}, "output":[]}))
    client = OpenAIClient(Settings(_env_file=None, llm_provider="openai", llm_api_key="synthetic-test-key"), transport=transport)
    with pytest.raises(ModelFailure) as error:
        client.request("Analyze", {}, {}, 200)
    assert error.value.result.input_tokens == 100


def test_strict_output_contract_has_no_tuple_prefixitems_or_optional_object_keys():
    def walk(value):
        if isinstance(value, dict):
            assert "prefixItems" not in value and "default" not in value
            if value.get("type") == "object" and "properties" in value:
                assert set(value["required"]) == set(value["properties"])
                assert value["additionalProperties"] is False
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for model in [ArticleDraft, EvidenceAnalysis]:
        walk(strict_schema(model.model_json_schema()))

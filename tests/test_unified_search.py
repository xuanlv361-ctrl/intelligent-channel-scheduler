from __future__ import annotations

import json

import pytest
from backend.search_service import UnifiedSearchService
from src.services.console_service import Store
from tests.enterprise_test_client import authenticated_test_client


def store_with_rows(tmp_path):
    path=tmp_path/"search.sqlite3";store=Store(path)
    batch={"batch_id":"IMP-SEARCH","source_sha256":"a"*64,
           "source_type":"measured_uat_browser_collector","created_at":"2026-07-28T00:00:00+00:00",
           "normalized_rows":[{
             "request_id":"REQ-SEARCH-1","response_id":"RESP-1","channel_id":"19",
             "channel_name":"fixture-channel","requested_model":"deepseek-test",
             "actual_model":"deepseek-observed","http_status":429,
             "error_message":"rate limited token=secret","timestamp":"2026-07-28T00:00:00+00:00",
             "environment_id":"china_uat","source_type":"measured_uat_browser_collector",
             "_classification":"valid"}]}
    store.save_batch(batch)
    return path,store


def test_search_groups_authorized_local_entities_and_preserves_provenance(tmp_path):
    path,store=store_with_rows(tmp_path)
    result=UnifiedSearchService(path,store).search("deepseek",environment_id="china_uat")
    assert {item["entity_type"] for item in result["items"]}=={"model"}
    assert all(item["environment_id"]=="china_uat" for item in result["items"])
    assert all(item["source_type"]=="measured_uat_browser_collector" for item in result["items"])
    assert result["provenance"]["sample_size"]==len(result["items"])


def test_search_never_returns_sensitive_fields_or_values(tmp_path):
    path,store=store_with_rows(tmp_path)
    payload=json.dumps(UnifiedSearchService(path,store).search("REQ-SEARCH"),ensure_ascii=False).lower()
    for forbidden in ('"authorization":','"cookie":','"raw_prompt":','"raw_response":',"token=secret"):
        assert forbidden not in payload


def test_search_environment_source_pagination_and_validation(tmp_path):
    path,store=store_with_rows(tmp_path);service=UnifiedSearchService(path,store)
    assert service.search("REQ",environment_id="overseas")["items"]==[]
    assert service.search("REQ",source_type="demo_mock")["items"]==[]
    first=service.search("fixture",limit=1)
    if first["next_cursor"]:
        assert service.search("fixture",limit=1,cursor=first["next_cursor"])["items"]!=first["items"]
    with pytest.raises(ValueError,match="search_query_required"):service.search(" ")
    with pytest.raises(ValueError,match="search_environment_invalid"):service.search("x","all-envs")
    with pytest.raises(ValueError,match="search_source_type_invalid"):service.search("x",source_type="secret")


def test_search_api_contract_uses_current_local_store(tmp_path,monkeypatch):
    path,store=store_with_rows(tmp_path)
    import backend.app as module
    monkeypatch.setattr(module,"STORE",store)
    web=authenticated_test_client(
        module.app,runtime=module.ENTERPRISE_HTTP,roles=("qa_auditor",))
    response=web.get(
        "/api/v1/search",params={"q":"REQ-SEARCH","environment_id":"china_uat","limit":10})
    assert response.status_code==200
    payload=response.json()
    assert payload["items"][0]["entity_type"]=="request"
    assert set(payload["items"][0])=={
        "entity_type","primary_id","title","summary","environment_id","source_type",
        "is_mock","sample_or_evidence_id","updated_at","destination_path","matched_fields"}
    assert web.get("/api/v1/search",params={"q":"x","environment_id":"all"}).status_code==200

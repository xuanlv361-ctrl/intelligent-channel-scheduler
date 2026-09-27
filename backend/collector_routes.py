from __future__ import annotations
from typing import Any,Callable
from urllib.parse import urlsplit
from fastapi import APIRouter,HTTPException,Request
from pydantic import BaseModel,Field
from .browser_import_service import BrowserImportService,_collector_context

class ConfirmBody(BaseModel):
    collection_id:str
    payload_sha256:str
    environment_id:str
    confirmed:bool=False

def build_collector_router(service:BrowserImportService,network_calls:Callable[[],int],
                           runtime_settings:Callable[[],Any]|None=None)->APIRouter:
    router=APIRouter(prefix="/api/v1/collector",tags=["collector"])
    def principal(request:Request):
        return getattr(request.state,"principal_context",None)
    @router.get("/status")
    def status(request:Request,environment_id:str="china_uat"):
        service._scope(principal(request),"collector.session.read")
        if environment_id=="china_uat" and runtime_settings:
            runtime=runtime_settings().status("china_uat")
            if not runtime["logs_page_url"]:
                return {
                  "status":"blocked","environment_id":"china_uat",
                  "environment":"china_uat","environment_name":"国内 UAT",
                  "blocking_reason":"log_sync_log_page_unconfirmed",
                  "blocking_message":"经过评审的国内 UAT 日志页尚未由本机操作员明确确认。",
                  "read_only":True,"console_url":"https://uat.weimeta.cn",
                  "console_host":"uat.weimeta.cn","log_page_url":None,
                  "currency":"CNY","configuration_consistent":False,
                  "allowed_hosts":[],"source_type":"measured_uat_browser_collector",
                  "authentication":"manual_browser_login",
                  "cookies_persisted":False,"credentials_persisted":False,
                  "log_page_status":runtime["log_page_status"],
                  "default_limits":{
                    "maximum_pages":10,"maximum_records":500,
                    "page_delay_ms":1500}}
        if environment_id=="overseas" and runtime_settings:
            runtime=runtime_settings().status("overseas")
            if runtime["logs_page_url"]:
                return {"status":"ready","environment_id":"overseas","environment":"overseas",
                  "environment_name":"海外站","blocking_reason":None,"read_only":True,
                  "console_url":"https://weimeta.ai","console_host":"weimeta.ai",
                  "log_page_url":runtime["logs_page_url"],"currency":None,
                  "configuration_consistent":True,
                  "allowed_hosts":["weimeta.ai"],"source_type":"measured_overseas_browser_collector",
                  "authentication":"manual_browser_login","cookies_persisted":False,
                  "credentials_persisted":False,"log_page_status":runtime["log_page_status"],
                  "browser_live_validation_status":runtime["browser_live_validation_status"],
                  "default_limits":{"maximum_pages":10,"maximum_records":500,"page_delay_ms":1500}}
        try:
            normalized,config,hosts,source_type=_collector_context(environment_id)
            state="ready";blocking_reason=None
        except ValueError as exc:
            if str(exc)!="overseas_log_page_unconfirmed":
                raise HTTPException(400,detail={"code":str(exc),"message":str(exc)})
            normalized="overseas";config={"display_name":"海外站","console_base_url":"https://weimeta.ai",
              "logs_page_url":None,"currency":None};hosts=frozenset({"weimeta.ai"})
            source_type=None;state="blocked";blocking_reason=str(exc)
        return {"status":state,"environment_id":normalized,"environment":normalized,
          "environment_name":config.get("display_name"),"blocking_reason":blocking_reason,
          "console_url":config.get("console_base_url"),
          "console_host":urlsplit(str(config.get("console_base_url") or "")).hostname,
          "log_page_url":config.get("logs_page_url"),"currency":config.get("currency"),
          "configuration_consistent":state=="ready",
          "read_only":True,"allowed_hosts":sorted(hosts),
          "source_type":source_type,"authentication":"manual_browser_login","cookies_persisted":False,
          "credentials_persisted":False,"default_limits":{"maximum_pages":10,"maximum_records":500,"page_delay_ms":1500}}
    @router.get("/command-preview")
    def command_preview(request:Request,environment_id:str,date_from:str,date_to:str,
                        maximum_pages:int=10,maximum_records:int=500):
        state=status(request,environment_id)
        if state["status"]!="ready" or not state["configuration_consistent"]:
            raise HTTPException(409,detail={"code":state.get("blocking_reason") or
              "collector_environment_inconsistent","message":"环境配置不一致，禁止生成或执行采集命令。"})
        command=(
          "python -m collector.collector_cli"
          f" --environment-id {environment_id}"
          f" --source-type {state['source_type']}"
          f" --console-url {state['console_url']}"
          f" --log-page {state['log_page_url']}"
          f" --date-from {date_from} --date-to {date_to}"
          f" --maximum-pages {maximum_pages} --maximum-records {maximum_records}"
        )
        return {**state,"command":command}
    @router.post("/import/preview")
    def preview(body:dict[str,Any],request:Request):
        try:return service.preview(body,principal=principal(request))
        except ValueError as exc:raise HTTPException(400,detail={"code":"COLLECTOR_PREVIEW_REJECTED","message":str(exc)})
    @router.post("/import/confirm")
    def confirm(body:ConfirmBody,request:Request):
        try:return service.confirm(body.collection_id,body.payload_sha256,body.confirmed,
                                   body.environment_id,principal=principal(request))
        except LookupError as exc:raise HTTPException(404,detail={"code":"NOT_FOUND","message":str(exc)})
        except ValueError as exc:raise HTTPException(409,detail={"code":"COLLECTOR_CONFIRM_REJECTED","message":str(exc)})
        except Exception as exc:
            if "UNIQUE constraint" in str(exc):raise HTTPException(409,detail={"code":"IMMUTABLE_IMPORT_EXISTS","message":"Import already exists."})
            raise
    @router.get("/collections")
    def collections(request:Request,environment_id:str):
        try:return {"environment_id":environment_id,"items":service.collections(
          environment_id,principal=principal(request))}
        except ValueError as exc:raise HTTPException(400,detail={"code":str(exc),"message":str(exc)})
    @router.get("/collections/{collection_id}")
    def collection(collection_id:str,environment_id:str,request:Request):
        item=service.get(collection_id,environment_id,principal=principal(request))
        if not item:raise HTTPException(404,detail={"code":"NOT_FOUND","message":"Collection not found."})
        return item
    @router.get("/progress")
    def progress(request:Request,environment_id:str):
        try:items=service.collections(environment_id,principal=principal(request))
        except ValueError as exc:raise HTTPException(400,detail={"code":str(exc),"message":str(exc)})
        latest=items[0] if items else None
        return {"state":"idle","environment_id":environment_id,"latest_collection":latest,"collection_count":len(items),
          "real_model_api_calls_during_collection":0,"uat_execution_network_calls":network_calls()}
    return router

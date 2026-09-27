from __future__ import annotations
import argparse,json,urllib.request
from pathlib import Path
from .pagination_controller import PaginationLimits
from .uat_browser_collector import CollectorConfig,UatBrowserCollector

def parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(description="只读 Weimeta UAT 日志采集器")
    p.add_argument("--environment-id",required=True,choices=("china_uat","overseas"))
    p.add_argument("--source-type",required=True,choices=(
      "measured_uat_browser_collector","measured_overseas_browser_collector"))
    p.add_argument("--console-url",required=True,help="仅允许 https://uat.weimeta.cn 或 https://admin-uat.weimeta.cn")
    p.add_argument("--log-page",action="append",required=True,help="已由授权会话观察确认的只读日志页面；可重复")
    p.add_argument("--date-from");p.add_argument("--date-to")
    p.add_argument("--maximum-pages",type=int,default=10);p.add_argument("--maximum-records",type=int,default=500)
    p.add_argument("--page-delay-ms",type=int,default=1500)
    p.add_argument("--output",type=Path,default=Path("output/uat_browser_collection_preview.json"))
    p.add_argument("--preview-url",default="http://127.0.0.1:8000/api/v1/collector/import/preview")
    return p

def main()->int:
    args=parser().parse_args()
    config=CollectorConfig(args.console_url,args.log_page,args.date_from,args.date_to,
      PaginationLimits(args.maximum_pages,args.maximum_records,args.page_delay_ms),
      environment_id=args.environment_id,source_type=args.source_type)
    result=UatBrowserCollector(config).collect()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    if result.get("status")=="captured":
        request=urllib.request.Request(args.preview_url,data=json.dumps(result).encode(),method="POST",
          headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(request,timeout=20) as response:
            print(response.read().decode())
    return 0

if __name__=="__main__":raise SystemExit(main())

from backend.enterprise_tenant_migrations import EnterpriseTenantMigrator

migrator = EnterpriseTenantMigrator(r"E:\wy\intelligent_channel_scheduler\data\routing_quality_console.sqlite3")
result = migrator.migrate()
print("=== 迁移结果 ===")
tables = result.get("tables", {})
for table, info in tables.items():
    status = info.get("status", "?") if isinstance(info, dict) else str(info)
    print(f"{table:45s} {status}")
print(f"\n总计: {result.get('summary', {})}")

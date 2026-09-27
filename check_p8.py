import sqlite3
db = sqlite3.connect(r"E:\wy\intelligent_channel_scheduler\data\routing_quality_console.sqlite3")
tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]

missing = []
ok = []
for t in tables:
    cols = db.execute(f"PRAGMA table_info({t})").fetchall()
    pk_cols = [c[1] for c in cols if c[5]]
    has_tenant = any(c[1] == "tenant_id" for c in cols)
    has_composite_pk = len(pk_cols) >= 2 and "tenant_id" in pk_cols
    
    if has_tenant and has_composite_pk:
        ok.append(t)
    else:
        missing.append((t, pk_cols, has_tenant))

print(f"OK ({len(ok)} tables):")
for t in ok[:10]:
    print(f"  {t}")
if len(ok) > 10:
    print(f"  ... and {len(ok)-10} more")

print(f"\nMISSING ({len(missing)} tables):")
for t, pk, has_t in missing:
    reason = []
    if not has_t:
        reason.append("no tenant_id col")
    if "tenant_id" not in pk:
        reason.append("PK w/o tenant")
    print(f"  {t:45s} PK={pk} [{', '.join(reason)}]")
db.close()

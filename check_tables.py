import sqlite3
db = sqlite3.connect(r"E:\wy\intelligent_channel_scheduler\data\routing_quality_console.sqlite3")
tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
for t in tables:
    cols = db.execute(f"PRAGMA table_info({t})").fetchall()
    pk_cols = [c[1] for c in cols if c[5]]
    has_tenant = any(c[1] == "tenant_id" for c in cols)
    count = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    flag = "OK" if has_tenant else "MISSING"
    print(f"{flag:8s} {t:45s} PK={pk_cols} rows={count}")
db.close()

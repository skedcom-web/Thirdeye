import sqlite3
from pathlib import Path
from datetime import datetime, timezone

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def seed_database():
    db_path = Path("data/thirdeye.db")
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    print("Seeding Tamil Nadu geography...")

    # Insert State
    try:
        conn.execute(
            "INSERT INTO states (name, code, status, active, created_at) VALUES (?, ?, ?, ?, ?)",
            ("Tamil Nadu", "TN", "ACTIVE", 1, utcnow())
        )
        print("Inserted state: Tamil Nadu (TN)")
    except sqlite3.IntegrityError:
        print("State Tamil Nadu (TN) already exists.")

    state_row = conn.execute("SELECT id FROM states WHERE code = 'TN'").fetchone()
    state_id = state_row["id"]

    # Insert 38 Districts of Tamil Nadu
    districts = [
        ("Ariyalur", "AR"),
        ("Chengalpattu", "CGL"),
        ("Chennai", "CHN"),
        ("Coimbatore", "CBE"),
        ("Cuddalore", "CUD"),
        ("Dharmapuri", "DPI"),
        ("Dindigul", "DGL"),
        ("Erode", "ERD"),
        ("Kallakurichi", "KKI"),
        ("Kanchipuram", "KPM"),
        ("Kanyakumari", "KK"),
        ("Karur", "KRR"),
        ("Krishnagiri", "KGI"),
        ("Madurai", "MDU"),
        ("Mayiladuthurai", "MYD"),
        ("Nagapattinam", "NGP"),
        ("Namakkal", "NKL"),
        ("Nilgiris", "NIL"),
        ("Perambalur", "PBL"),
        ("Pudukkottai", "PDK"),
        ("Ramanathapuram", "RMD"),
        ("Ranipet", "RPT"),
        ("Salem", "SLM"),
        ("Sivaganga", "SVG"),
        ("Tenkasi", "TKS"),
        ("Thanjavur", "TJV"),
        ("Theni", "TNI"),
        ("Thoothukudi", "TUT"),
        ("Tiruchirappalli", "TRY"),
        ("Tirunelveli", "TNV"),
        ("Tirupathur", "TPT"),
        ("Tiruppur", "TPU"),
        ("Tiruvallur", "TLR"),
        ("Tiruvannamalai", "TVM"),
        ("Tiruvarur", "TVR"),
        ("Vellore", "VEL"),
        ("Viluppuram", "VPM"),
        ("Virudhunagar", "VDG")
    ]

    inserted_districts = 0
    for name, code in districts:
        try:
            conn.execute(
                "INSERT INTO districts (state_id, name, code, status, certification_status, publication_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (state_id, name, code, "CONFIGURED", "PENDING", "NOT_PUBLISHED", utcnow())
            )
            inserted_districts += 1
        except sqlite3.IntegrityError:
            pass

    print(f"Seeded {inserted_districts} new districts for Tamil Nadu.")

    # Insert Official TN Sources
    print("Seeding official Tamil Nadu government websites...")
    from . import registry
    sources = registry.SEED_SOURCES

    inserted_sources = 0
    for s in sources:
        try:
            # We want to check if it exists in sources first
            existing = conn.execute("SELECT id FROM sources WHERE name = ?", (s["name"],)).fetchone()
            if existing is None:
                host = s["url"].split("//")[-1].split("/")[0]
                cur = conn.execute(
                    """
                    INSERT INTO sources
                        (name, department, url, host, source_type, adapter, active,
                         crawl_frequency, state_id, discovery_method, lifecycle_status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 'daily', ?, 'listing_page', 'ACTIVE', ?)
                    """,
                    (s["name"], s["department"], s["url"], host, s["source_type"], s["adapter"], state_id, utcnow())
                )
                source_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO source_versions
                        (source_id, version, name, department, url, discovery_method, active,
                         crawl_frequency, changed_by, changed_at, change_reason)
                    VALUES (?, 1, ?, ?, ?, 'listing_page', 1, 'daily', 'system', ?, 'initial seed')
                    """,
                    (source_id, s["name"], s["department"], s["url"], utcnow())
                )
                inserted_sources += 1
            else:
                # Update existing source with state_id and new URL
                conn.execute("UPDATE sources SET state_id = ?, url = ?, adapter = ? WHERE id = ?", (state_id, s["url"], s["adapter"], existing["id"]))
        except Exception as e:
            print(f"Error seeding source {s['name']}: {e}")

    conn.commit()
    conn.close()
    print(f"Seeded {inserted_sources} new official Tamil Nadu sources.")

if __name__ == "__main__":
    seed_database()

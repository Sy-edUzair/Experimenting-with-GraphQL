import os
import csv
import sys
import logging
import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

OUTPUT_FILE = "star_counts.csv"


def dump(db_url: str) -> None:
    log.info("Connecting to database …")
    conn = psycopg2.connect(db_url)

    with conn.cursor() as cur:
        log.info("Querying latest_star_counts view …")
        cur.execute(
            """
            SELECT
                node_id,
                name_with_owner,
                owner_login,
                name,
                star_count,
                recorded_at
            FROM latest_star_counts
            ORDER BY star_count DESC
            """
        )
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]

    conn.close()

    log.info("Writing %d rows to %s …", len(rows), OUTPUT_FILE)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)

    log.info("Dump complete: %s (%d rows)", OUTPUT_FILE, len(rows))


if __name__ == "__main__":
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    dump(db_url)
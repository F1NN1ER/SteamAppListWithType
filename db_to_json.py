import json
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "Data")
DB_PATH = os.path.join(DB_DIR, "data.db")
JSON_PATH = os.path.join(DB_DIR, "Json")


def generate_json(conn):
    os.makedirs(JSON_PATH, exist_ok=True)
    cur = conn.cursor()
    cur.execute("SELECT appid, type FROM Type")
    rows = cur.fetchall()

    # All.json
    all_data = {str(appid): type_ for appid, type_ in rows}
    with open(os.path.join(JSON_PATH, "All.json"), "w", encoding="utf-8") as f:
        json.dump(all_data, f, indent=2)
    print(f"已导出 {len(all_data)} 条记录到 All.json")

    # type.json
    grouped: dict[str, list[int]] = {}
    for appid, type_ in rows:
        key = type_ if type_ else "NULL"
        grouped.setdefault(key, []).append(appid)

    for type_name, data in grouped.items():
        filename = f"{type_name}.json"
        with open(os.path.join(JSON_PATH, filename), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"已导出 {len(data)} 条记录到 {filename}")

    print(f"共导出 {len(grouped)} 个分类文件到 {JSON_PATH}")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    try:
        generate_json(conn)
    finally:
        conn.close()

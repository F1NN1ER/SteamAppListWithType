import os
import sqlite3
import sys
from datetime import datetime

from steam.client import SteamClient
from steam.enums import EResult

# 每个检查点扫描多少 id
CHECKPOINT = 100_000
# 连续多少检查点无数据停止
EMPTY_STREAK_STOP = 2
# 硬上限
HARD_CEILING = 100_000_000
# 段内每处理多少个 id 打印一次
PROGRESS_EVERY = 10_00
# 单批请求的 appid 数量
BATCH_SIZE = 1000

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "Data")
DB_PATH = os.path.join(DB_DIR, "data.db")


# 初始化数据库
def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # 类型表
    cur.execute("""
                create table if not exists Type
                (
                    appid         integer primary key,
                    name          text,
                    type          text,
                    change_number integer,
                    updated_at    text
                )
                """)
    # 记录表
    cur.execute("""
                create table if not exists sync_state
                (
                    id                 integer primary key check (id = 1),
                    last_appid         integer not null default 0,
                    last_change_number integer not null default 0,
                    last_run           text
                )
                """)
    cur.execute("insert or ignore into sync_state (id) values (1)")
    conn.commit()
    return conn


# 匿名登录
def login(client: SteamClient, max_retries: int = 5, retry_delay: int = 5) -> None:
    for attempt in range(1, max_retries + 1):
        result = client.anonymous_login()
        if result == EResult.OK:
            print("登录成功")
            return
        print(f"登录失败（{result.name}），重试 {attempt}/{max_retries}")
        if attempt < max_retries:
            client.disconnect()
            client.sleep(retry_delay)
    raise RuntimeError("登录失败")


# 获取上次断点
def get_state(conn):
    cur = conn.cursor()
    cur.execute(
        "select last_appid, last_change_number, last_run from sync_state where id = 1"
    )
    return cur.fetchone()


# 保存当前进度
def set_state(conn, last_appid=None, last_change_number=None, last_run=None):
    cur = conn.cursor()
    fields, values = [], []
    if last_appid is not None:
        fields.append("last_appid = ?")
        values.append(last_appid)
    if last_change_number is not None:
        fields.append("last_change_number = ?")
        values.append(last_change_number)
    if last_run is not None:
        fields.append("last_run = ?")
        values.append(last_run)
    if not fields:
        return
    values.append(1)
    cur.execute(f"update sync_state set {', '.join(fields)} where id = ?", values)
    conn.commit()


# 提取appid、名称、类型为待写入行
def extract_rows(apps: dict) -> list:
    rows = []
    now = datetime.now().isoformat(timespec="seconds")
    for appid, info in apps.items():
        if isinstance(info, dict):
            common = info.get("common", {}) or {}
            name = common.get("name")
            app_type = common.get("type")
            change_number = info.get("_change_number")
        else:
            common = getattr(info, "common", None)
            name = getattr(common, "name", None) if common else None
            app_type = getattr(common, "type", None) if common else None
            change_number = getattr(info, "_change_number", None)

        if name is None:
            continue
        rows.append(
            (int(appid), name, (app_type or "unknown").lower(), change_number, now)
        )
    return rows


# 向数据库写入行
def save_rows(conn, rows):
    if not rows:
        return
    cur = conn.cursor()
    cur.executemany(
        """insert or replace into Type
           (appid, name, type, change_number, updated_at)
           values (?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


# 按appids返回有效的apps
def fetch_data(client: SteamClient, app_ids: list, on_progress=None) -> dict:
    result = {}
    total = len(app_ids)
    for i in range(0, total, BATCH_SIZE):
        chunk = app_ids[i : i + BATCH_SIZE]
        try:
            resp = client.get_product_info(apps=chunk)
        except Exception as e:
            print(f"请求失败 appid {chunk[0]}-{chunk[-1]}: {e}")
            continue
        apps = (
            resp.get("apps", {})
            if isinstance(resp, dict)
            else getattr(resp, "apps", {})
        )
        result.update(apps)
        if on_progress:
            on_progress(min(i + BATCH_SIZE, total), total, len(result))
    return result


def main() -> int:
    client = SteamClient()
    conn = init_db()
    login(client)
    last_appid, last_cn, _ = get_state(conn)
    ceiling = HARD_CEILING
    start = last_appid + 1

    print(f"从起点 {start} 处开始扫描")

    empty_streak = 0
    total_new = 0
    try:
        for chunk_start in range(start, ceiling + 1, CHECKPOINT):
            chunk_end = min(chunk_start + CHECKPOINT - 1, ceiling)
            ids = list(range(chunk_start, chunk_end + 1))

            last_print = [0]

            def progress(done, total, kept, _start=chunk_start, _last=last_print):
                if done - _last[0] >= PROGRESS_EVERY or done == total:
                    print(
                        f"  [{_start + done - 1}] 段内进度 {done}/{total}，本段已保留 {kept} 条"
                    )
                    _last[0] = done

            apps = fetch_data(client, ids, on_progress=progress)
            rows = extract_rows(apps)
            save_rows(conn, rows)
            total_new += len(rows)
            # change number
            max_cn = max(
                (
                    int(info.get("_change_number") or 0)
                    for info in apps.values()
                    if isinstance(info, dict)
                ),
                default=0,
            )
            new_cn = max(last_cn, max_cn)
            # 保存状态
            set_state(
                conn,
                last_appid=chunk_end,
                last_change_number=new_cn,
                last_run=datetime.now().isoformat(timespec="seconds"),
            )
            last_appid, last_cn = chunk_end, new_cn

            print(
                f"checkpoint: 扫到 {chunk_end}，本段新增 {len(rows)} 条，"
                f"累计 {total_new} 条，change_number {new_cn}"
            )

            empty_streak = empty_streak + 1 if not rows else 0
            if empty_streak >= EMPTY_STREAK_STOP:
                print("已完成本次扫描")
                break

        resp = client.get_changes_since(
            last_cn or 1, app_changes=False, package_changes=False
        )
        if resp is not None:
            real_cn = resp.current_change_number
            if real_cn and real_cn > last_cn:
                set_state(
                    conn,
                    last_change_number=real_cn,
                    last_run=datetime.now().isoformat(timespec="seconds"),
                )
                print(f"已将 change_number 更新为服务器真实值 {real_cn}")
    finally:
        client.logout()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

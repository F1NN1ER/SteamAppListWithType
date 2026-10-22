import argparse
import os
import sqlite3
import sys
import time
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
PROGRESS_EVERY = 1_000
# 单批请求的 appid 数量
BATCH_SIZE = 5000
# 批次之间的停顿，避免触发 Steam 限流
BATCH_PAUSE = 0.5
# 失败批次的重试次数
BATCH_RETRIES = 3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "Data")
DB_PATH = os.path.join(DB_DIR, "data.db")


# 初始化数据库
def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
                create table if not exists Info
                (
                    appid
                    integer
                    primary
                    key,
                    name
                    text,
                    type
                    text,
                    limited_status
                    text
                )
                """)
    cur.execute("select name from sqlite_master where type='table' and name='Type'")
    if cur.fetchone():
        cur.execute(
            "insert or ignore into Info (appid, name, type, limited_status) "
            "select appid, name, type, null from Type"
        )
        cur.execute("drop table Type")
    # 记录表
    cur.execute("""
                create table if not exists sync_state
                (
                    id
                    integer
                    primary
                    key
                    check
                (
                    id =
                    1
                ),
                    last_appid integer not null default 0,
                    last_change_number integer not null default 0,
                    last_run text
                    )
                """)
    cur.execute("insert or ignore into sync_state (id) values (1)")
    cols = [row[1] for row in cur.execute("pragma table_info(sync_state)")]
    if "rebuild_needed" not in cols:
        cur.execute(
            "alter table sync_state add column rebuild_needed integer not null default 0"
        )
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
        "select last_appid, last_change_number, last_run, rebuild_needed "
        "from sync_state where id = 1"
    )
    return cur.fetchone()


# 保存当前进度
def set_state(
        conn,
        last_appid=None,
        last_change_number=None,
        last_run=None,
        rebuild_needed=None,
):
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
    if rebuild_needed is not None:
        fields.append("rebuild_needed = ?")
        values.append(1 if rebuild_needed else 0)
    if not fields:
        return
    values.append(1)
    cur.execute(f"update sync_state set {', '.join(fields)} where id = ?", values)
    conn.commit()


# 获取 Info 表中 appid 的最大值
def get_max_appid(conn):
    cur = conn.cursor()
    cur.execute("select max(appid) from Info")
    row = cur.fetchone()
    return row[0] if row and row[0] else 0


# 提取appid、名称、类型为待写入行
def extract_rows(apps: dict) -> list:
    rows = []
    for appid, info in apps.items():
        if isinstance(info, dict):
            common = info.get("common", {}) or {}
            name = common.get("name")
            app_type = common.get("type")
        else:
            common = getattr(info, "common", None)
            name = getattr(common, "name", None) if common else None
            app_type = getattr(common, "type", None) if common else None

        if name is None:
            continue
        rows.append(
            (int(appid), name, (app_type or "unknown").lower(), None)
        )
    return rows


# 向数据库写入行
def save_rows(conn, rows):
    if not rows:
        return
    cur = conn.cursor()
    cur.executemany(
        """insert or replace into Info
           (appid, name, type, limited_status)
           values (?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


# 按appids返回有效的apps
def fetch_data(client: SteamClient, app_ids: list, on_progress=None) -> dict:
    result = {}
    total = len(app_ids)
    for i in range(0, total, BATCH_SIZE):
        chunk = app_ids[i: i + BATCH_SIZE]
        apps = None
        last_error = None
        for attempt in range(1, BATCH_RETRIES + 1):
            try:
                resp = client.get_product_info(apps=chunk)
                if resp is None:
                    raise RuntimeError("PICS 返回空响应")
                apps = (
                    resp.get("apps", {})
                    if isinstance(resp, dict)
                    else getattr(resp, "apps", {})
                )
                last_error = None
                break
            except Exception as e:
                last_error = e
                print(
                    f"请求失败 appid {chunk[0]}-{chunk[-1]}"
                    f"（第 {attempt}/{BATCH_RETRIES} 次）: {e}"
                )
                time.sleep(2 * attempt)
        if last_error is not None:
            raise RuntimeError(
                f"请求失败 appid {chunk[0]}-{chunk[-1]}，已重试 {BATCH_RETRIES} 次"
            ) from last_error
        result.update(apps)
        if on_progress:
            on_progress(min(i + BATCH_SIZE, total), total, len(result))
        time.sleep(BATCH_PAUSE)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="全量/回填扫描 Steam appid 空间")
    ap.add_argument(
        "--scan-from",
        type=int,
        default=0,
        help="从指定 appid 开始扫描（默认从上次检查点继续）",
    )
    ap.add_argument(
        "--scan-to",
        default="auto",
        help="'auto' 扫描到 id 空间结束，或指定最大 appid",
    )
    args = ap.parse_args()

    client = SteamClient()
    conn = init_db()
    login(client)
    last_appid, last_cn, _, rebuild_needed = get_state(conn)
    auto = args.scan_to == "auto"
    ceiling = HARD_CEILING if auto else int(args.scan_to)
    start = args.scan_from or last_appid + 1

    # 首次运行或全量重建时，在扫描前记录 baseline：
    # 扫描本身会抓到当前全量状态，扫描期间产生的变更由下一次增量补齐。
    # 续扫时不刷新，避免跳过中断期间发生的变更。
    if not last_cn or ((rebuild_needed or args.scan_from) and start <= 1):
        resp = client.get_changes_since(
            1, app_changes=False, package_changes=False
        )
        if resp is not None and resp.current_change_number:
            last_cn = resp.current_change_number
            set_state(
                conn,
                last_change_number=last_cn,
                last_run=datetime.now().isoformat(timespec="seconds"),
            )
            print(f"baseline changenumber: {last_cn}")

    print(f"从起点 {start} 处开始扫描" + ("（扫描到 id 空间结束）" if auto else ""))

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
            # 记录“已扫描到的上限”，而不是表里的最大 appid。
            # 否则中断后会从最大 appid 继续，跳过中间未扫描/失败的空洞。
            set_state(
                conn,
                last_appid=chunk_end,
                last_run=datetime.now().isoformat(timespec="seconds"),
            )
            last_appid = chunk_end

            print(
                f"checkpoint: 扫到 {chunk_end}，本段新增 {len(rows)} 条，"
                f"累计 {total_new} 条"
            )

            if auto:
                empty_streak = empty_streak + 1 if not rows else 0
                if empty_streak >= EMPTY_STREAK_STOP:
                    print("已完成本次扫描")
                    break

        # 扫描完整跑完才清除重建标记；中途异常/超时会保留标记以便下次续扫。
        set_state(
            conn,
            rebuild_needed=False,
            last_run=datetime.now().isoformat(timespec="seconds"),
        )
    finally:
        client.logout()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

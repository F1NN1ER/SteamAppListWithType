import sys
from datetime import datetime

from steam.client import SteamClient

from sync_all_data import (
    init_db,
    login,
    get_state,
    set_state,
    extract_rows,
    save_rows,
    fetch_data,
)


# 获取最新 app 变更
def fetch_changes(client: SteamClient, change_number: int, max_retries: int = 3):
    for attempt in range(1, max_retries + 1):
        resp = client.get_changes_since(
            change_number, app_changes=True, package_changes=False
        )
        if resp is not None:
            return resp
        print(f"获取变更超时，重试 {attempt}/{max_retries}")
    raise RuntimeError("获取变更失败")


def main() -> int:
    conn = init_db()
    _, last_cn, _ = get_state(conn)

    if not last_cn:
        print("未找到 change_number，请先运行 sync_all_data 完成全量同步")
        conn.close()
        return 1

    client = SteamClient()
    login(client)

    try:
        print(f"从 change_number {last_cn} 开始增量同步")
        resp = fetch_changes(client, last_cn)

        if resp.force_full_app_update:
            print("变更跨度过大，请运行 sync_all_data 重新全量同步")
            return 1

        current_cn = resp.current_change_number
        changed_appids = [c.appid for c in resp.app_changes]

        print(f"自 {last_cn} 起共 {len(changed_appids)} 个 app 变更")

        if not changed_appids:
            set_state(
                conn,
                last_change_number=current_cn,
                last_run=datetime.now().isoformat(timespec="seconds"),
            )
            print("无变更，已更新 change_number")
            return 0

        # 分批拉取变更 app 的产品信息并写入
        def on_progress(done, total, kept):
            print(f"已拉取 {done}/{total} 个，保留 {kept} 条")

        apps = fetch_data(client, changed_appids, on_progress=on_progress)
        rows = extract_rows(apps)
        save_rows(conn, rows)

        set_state(
            conn,
            last_change_number=current_cn,
            last_run=datetime.now().isoformat(timespec="seconds"),
        )
        skipped = len(changed_appids) - len(rows)
        print(f"增量同步完成，更新 {len(rows)} 条，跳过 {skipped} 条")
    finally:
        client.logout()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

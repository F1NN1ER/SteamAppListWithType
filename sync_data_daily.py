import sys
from datetime import datetime, timedelta

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

# 两次同步间隔超过该值时，PICS 的变更窗口（约 5000 个 change number）可能已丢失
STALE_AFTER = timedelta(hours=4)


# 获取最新 app 变更
def fetch_changes(client: SteamClient, change_number: int, max_retries: int = 3):
    for attempt in range(1, max_retries + 1):
        resp = client.get_changes_since(
            change_number, app_changes=True, package_changes=False
        )
        if resp is not None:
            return resp
        print(f"获取变更超时，重试 {attempt}/{max_retries}")
        client.sleep(2 * attempt)
    raise RuntimeError("获取变更失败")


def main() -> int:
    conn = init_db()
    last_appid, last_cn, last_run, rebuild_needed = get_state(conn)

    if rebuild_needed:
        print("检测到未完成的全量重建，继续执行全量同步")
        conn.close()
        return 1

    if not last_cn:
        print("未找到 change_number，请先运行 sync_all_data 完成全量同步")
        conn.close()
        return 1

    client = SteamClient()
    login(client)

    try:
        print(f"从 change_number {last_cn} 开始增量同步")
        resp = fetch_changes(client, last_cn)

        current_cn = resp.current_change_number
        force_full = bool(getattr(resp, "force_full_app_update", False))
        effective_since = getattr(resp, "since_change_number", 0) or 0
        if effective_since > last_cn:
            print(
                f"warning: 请求的起始 change_number {last_cn} 早于 PICS 保留窗口 "
                f"（实际从 {effective_since} 开始），变更已丢失",
                file=sys.stderr,
            )
            force_full = True

        if last_run:
            try:
                if datetime.now() - datetime.fromisoformat(last_run) > STALE_AFTER:
                    print(
                        f"warning: 距上次同步（{last_run}）超过 "
                        f"{STALE_AFTER.total_seconds() // 3600:g} 小时，"
                        "变更窗口可能已丢失",
                        file=sys.stderr,
                    )
                    force_full = True
            except ValueError:
                pass

        if force_full:
            # 重置扫描断点并标记重建；下一次 sync_all_data 会从 1 开始全量回填。
            # 同时把 change_number 设为当前值，重建完成后增量可正常衔接。
            print("需要全量重建，交由 sync_all_data 处理", file=sys.stderr)
            set_state(
                conn,
                last_appid=0,
                last_change_number=current_cn,
                rebuild_needed=1,
                last_run=datetime.now().isoformat(timespec="seconds"),
            )
            return 1

        changed_appids = [c.appid for c in (resp.app_changes or [])]

        print(f"自 {last_cn} 起共 {len(changed_appids)} 个 app 变更")

        if changed_appids:
            def on_progress(done, total, kept):
                print(f"已拉取 {done}/{total} 个，保留 {kept} 条")

            apps = fetch_data(client, changed_appids, on_progress=on_progress)
            rows = extract_rows(apps)
            save_rows(conn, rows)
            skipped = len(changed_appids) - len(rows)
            print(f"增量同步完成，更新 {len(rows)} 条，跳过 {skipped} 条")

        if current_cn:
            set_state(
                conn,
                last_change_number=current_cn,
                last_run=datetime.now().isoformat(timespec="seconds"),
                rebuild_needed=0,
            )
    finally:
        client.logout()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

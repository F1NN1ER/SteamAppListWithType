# Steam 物品类型数据库

每 2 小时自动同步所有公开 AppID 类型数据。

## 脚本

| 脚本                   | 用途                      |
|----------------------|-------------------------|
| `sync_all_data.py`   | 全量同步，从零开始顺序遍历 appid 空间。 |
| `sync_data_daily.py` | 增量同步，只拉取发生变更的 appid。    |
| `db_to_json.py`      | 将数据库表导出为按类型分类的 Json 文件。 |

## 说明

- 全类型数据：`Data/All.json`
- 数据缺失或首次部署时，在 Actions 页面手动运行 `Sync Data`，把 `scan_from` 填 `1` 执行一次全量回填；平时留空即可。

## 预览

```json
{
  "660": "dlc",
  "1256": "dlc",
  "1257": "dlc",
  "3838": "music",
  "3839": "music",
  "4856": "dlc",
  "8650": "dlc",
  "8660": "dlc"
}
```

喜欢的话可以订阅一下我的脚本 [Better SteamPY](https://greasyfork.org/zh-CN/scripts/503737-better-steampy)

> 数据来源于`Steam PICS`

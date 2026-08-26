"""正式 Worker 兼容入口。

`python -m integrations.worker` 保持部署命令兼容，实际只启动 MySQL 运行队列和
纯巡检编排器。旧 SQLite、复盘、Mode、广告和建议链路不再从该入口触达。
"""

from integrations.runtime_worker_main import main

if __name__ == "__main__":
    main()

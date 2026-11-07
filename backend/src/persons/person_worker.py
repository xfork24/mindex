"""Detached CLI task runner; secrets are resolved from current runtime settings, never argv."""

from __future__ import annotations

import sys
from pathlib import Path

from filelock import Timeout

from persons.person_processing import PersonTasks
from settings.runtime_settings import RuntimeConfig
from shared.config import Config
from shared.storage import Store


def main():
    store = Store(Path(sys.argv[1]))
    identifier = sys.argv[2]
    row = store.one("SELECT * FROM person_tasks WHERE id=?", (identifier,))
    if row is None:
        raise SystemExit("找不到任务")
    config = Config.model_validate_json(row["config"])
    runtime = RuntimeConfig(config, store)
    config.person.api_key = runtime.current().person.api_key
    manager = PersonTasks.__new__(PersonTasks)
    manager.runtime, manager.store, manager.threads = runtime, store, {}
    lock = manager.lock(row["source_id"])
    try:
        lock.acquire()
    except Timeout as exc:
        store.execute("UPDATE person_tasks SET status='failed',error=? WHERE id=?", (str(exc), identifier))
        return
    manager.run(identifier, config, lock)


if __name__ == "__main__":
    main()

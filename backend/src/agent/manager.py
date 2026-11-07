"""Dispatch by the persisted session owner; preference changes affect new sessions only."""

from __future__ import annotations

import asyncio

from agent.runtime import AgentRuntime


class RuntimeManager:
    def __init__(self, builtin, dsh, settings):
        self.runtimes = {"builtin": builtin, "dsh": dsh}
        self.settings = settings
        self.store = builtin.store
        self.feed = builtin.feed
        self.attachments = builtin.attachments
        self.watcher = builtin.watcher
        self._started = False

    def owner(self, sid) -> AgentRuntime:
        return self.runtimes[self.store.get(sid)["runtime"]]

    async def start(self):
        if self._started:
            return
        self._started = True
        for row in self.store.list():
            self.owner(row["id"]).recover(row["id"])
        for runtime in self.runtimes.values():
            runtime._started = True
        self.watcher.resume_all()

    async def create_or_resume(self, session_id=None, title=""):
        await self.start()
        if session_id:
            return self.store.get(session_id)
        return self.store.create(title, runtime=self.settings().runtime)

    async def forget(self, sid):
        import shutil

        for runtime in self.runtimes.values():
            runtime._sessions.pop(sid, None)
        directory = self.store.store.path.parent / "dsh" / sid
        if directory.exists():
            await asyncio.to_thread(shutil.rmtree, directory)

    async def close(self):
        tasks = list(self.watcher.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for runtime in self.runtimes.values():
            for sid in list(runtime._sessions):
                await runtime.cancel(sid)

    def __setattr__(self, name, value):
        if (
            name in ("registry", "llm_factory", "title_llm_factory", "approvals")
            and "runtimes" in self.__dict__
        ):
            for runtime in self.runtimes.values():
                setattr(runtime, name, value)
        else:
            object.__setattr__(self, name, value)

    def __getattr__(self, name):
        if name in ("registry", "llm_factory", "title_llm_factory", "approvals"):
            return getattr(self.runtimes["builtin"], name)

        # Shared operations always receive a session id as their first argument.
        if hasattr(self.runtimes["builtin"], name):

            def dispatch(sid, *args, **kwargs):
                return getattr(self.owner(sid), name)(sid, *args, **kwargs)

            return dispatch
        raise AttributeError(name)

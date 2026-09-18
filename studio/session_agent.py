"""Harbor agent interface for the Studio session protocol."""

from harbor.agents.base import BaseAgent


class StudioSessionAgent(BaseAgent):
    def __init__(self, *args, core, **kwargs):
        self.core = core
        super().__init__(*args, **kwargs)

    @staticmethod
    def name():
        return "studio-session"

    def version(self):
        return "1"

    async def setup(self, environment):
        # The controller prepared the pinned checkout before starting measurement.
        pass

    async def run(self, instruction, environment, context):
        row = await self.core.run()
        context.metadata = {"session": row}
        return row

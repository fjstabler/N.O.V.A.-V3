"""Money questions, answered without the model seeing the answer.

Every tool here raises `FinalAnswer`. That is not a stylistic choice: an
ordinary tool result is appended to the model's message list and sent with the
next request, so a tool that returns a balance is a tool that puts that balance
in a prompt. Raising ends the turn with the module's own sentence and the
figures never leave the machine.

It also fixes the wording. Handed "£340 available, 11 days to payday", a model
will helpfully add "so yes, you can afford it" — and a verdict from a system
its owner can reprogram is not a limit, it is something to argue with. The
module answers with numbers and stops.
"""

from __future__ import annotations

from typing import Annotated

from ...finance.module import FinanceModule
from ...finance.service import FinanceService
from ...runtime.errors import FinalAnswer, SkillError
from ..base import Param, Skill, tool


class FinanceSkill(Skill):
    name = "finance"
    description = "Balances, what is committed before payday, and the cooling-off queue."

    def is_available(self) -> tuple[bool, str]:
        if not self.ctx.settings.finance.enabled:
            return False, "finance is disabled in settings"
        return True, ""

    prompt_hint = (
        "Money questions go to the finance tools. They answer directly and their replies "
        "are final — do not restate, summarise or add to them, and never offer an opinion "
        "on whether a purchase is sensible. You will not see the figures, which is "
        "deliberate."
    )

    #: Only used when the finance service is not running — see `_finance`.
    _module: FinanceModule | None = None

    async def _finance(self) -> FinanceModule:
        """The module, shared with the service wherever possible.

        One instance, so the ledger has one writer and the cooling-off queue
        the service is watching is the same queue these tools add to. The
        fallback exists because a skill must still answer if the service failed
        to start — its own instance answers questions perfectly well, it just
        has nothing watching in the background.
        """
        service = self.ctx.service("finance", FinanceService)
        if service is not None and service.module is not None:
            return service.module

        settings = self.ctx.settings.finance
        if self._module is None:
            self._module = FinanceModule(settings, self.ctx.paths.data_dir)
            await self._module.open()
        else:
            self._module.reconfigure(settings)
        return self._module

    # -------------------------------------------------------------- questions

    @tool(
        "How much is available to spend, and what a given spend would leave. "
        "Use for 'how much have I got', 'can I afford £200', 'what's left'."
    )
    async def affordability(
        self,
        amount: Annotated[
            float, Param("The spend being considered, in pounds. 0 to just ask what is available.")
        ] = 0.0,
    ) -> str:
        finance = await self._finance()
        raise FinalAnswer(await finance.affordability(amount))

    @tool("What is still due to leave the account before payday, and the balance behind it.")
    async def committed(self) -> str:
        finance = await self._finance()
        raise FinalAnswer(await finance.committed())

    # ------------------------------------------------------------ cooling off

    @tool(
        "Put a purchase on the cooling-off list. Use when someone says they want to "
        "buy something — it is not a decision, it is a note to ask them again later."
    )
    async def want_to_buy(
        self,
        item: Annotated[str, Param("What they want to buy")],
        amount: Annotated[float, Param("Price in pounds")],
    ) -> str:
        finance = await self._finance()
        raise FinalAnswer(await finance.want(item, amount))

    @tool("List purchases waiting out their cooling-off period, with the total.")
    async def waiting_list(self) -> str:
        finance = await self._finance()
        raise FinalAnswer(await finance.queue())

    @tool("Record what happened to a waiting purchase: bought, dropped, or still thinking.")
    async def decide(
        self,
        outcome: Annotated[str, Param("bought, dropped, or still thinking")],
        item: Annotated[str, Param("Which one, if more than one is waiting")] = "",
    ) -> str:
        finance = await self._finance()
        raise FinalAnswer(await finance.decide(item, outcome))

    @tool("How much was saved by dropping purchases after they cooled off.")
    async def dropped(
        self,
        days: Annotated[int, Param("How far back to look, in days")] = 30,
    ) -> str:
        finance = await self._finance()
        raise FinalAnswer(await finance.dropped(max(1, days)))

    # --------------------------------------------------------------- ingestion

    @tool("Read a bank statement CSV into the ledger.")
    async def import_statement(
        self,
        path: Annotated[str, Param("Path to the CSV; blank uses the configured one")] = "",
    ) -> str:
        finance = await self._finance()
        raise FinalAnswer(await finance.import_statement(path))

    # --------------------------------------------------------------- transfers

    @tool(
        "Run the payday split, moving the configured amount into savings. Refuses "
        "unless transfers are enabled in settings, and logs a dry run otherwise."
    )
    async def payday_split(self) -> str:
        finance = await self._finance()
        try:
            raise FinalAnswer(await finance.payday_split(triggered_by="asked"))
        except SkillError as exc:
            # A refusal is also an answer the model has no business rewording:
            # "over the cap, nothing was moved" must not come back softened.
            raise FinalAnswer(str(exc.message)) from exc

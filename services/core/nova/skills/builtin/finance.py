"""Money questions, answered without the model seeing the answer.

Every tool here raises `FinalAnswer`. That is not a stylistic choice: an
ordinary tool result is appended to the model's message list and sent with the
next request, so a tool that returns a balance is a tool that puts that balance
in a prompt. Raising ends the turn with the module's own sentence and the
figures never leave the machine.

It also fixes the wording. Handed "£340 available, 11 days to payday", a model
will improvise a verdict, differently each time, from figures it half
understands. The module does recommend things now — `should_i_buy` exists
because the owner asked for an advisor rather than a reporter — but the
recommendation is worked out from the numbers here, by the same thresholds
every time, and arrives as a finished sentence.

The model's contribution is one judgement: whether the thing is a need or a
want. That is a question about the item and not about the money, so it can be
answered without seeing an account, which is exactly why the line is drawn
there.
"""

from __future__ import annotations

from typing import Annotated, Literal

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
        "are final — do not restate, summarise or add to them. You will not see any "
        "figures, which is deliberate. For 'should I buy this', use finance_should_i_buy "
        "and judge only whether the item is a need or a want, from what the item is. You "
        "have no idea what they can afford and must not guess; the tool does the rest."
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

    @tool(
        "Whether to buy something now or wait, with the reasoning. Use for 'should I "
        "buy X', 'is it a good idea to get X', 'can I justify X'. You classify the "
        "item; the tool weighs it against money you cannot see."
    )
    async def should_i_buy(
        self,
        item: Annotated[str, Param("What they are thinking of buying")],
        amount: Annotated[float, Param("Price in pounds")],
        kind: Annotated[
            Literal["need", "want"],
            Param(
                "Your judgement of the item itself, ignoring their finances entirely — "
                "'need' for food, medicine, repairs, travel to work, replacing something "
                "broken and depended on; 'want' for everything discretionary. If it is "
                "genuinely borderline, say want."
            ),
        ],
    ) -> str:
        finance = await self._finance()
        raise FinalAnswer(await finance.advise(item, amount, kind))

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

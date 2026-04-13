from utils.helpers import format_message

HEADER = "📊 *Portfolio Volatility Summary*\n"
BALANCED = "\n✅ Portfolio is balanced."


class Summary:
    def __init__(self):
        self.lines: list[str] = []
        self.rebalances: list[str] = []

    def add_summary(self, msg: str) -> None:
        self.lines.append(msg)

    def add_rebalance(self, msg: str) -> None:
        self.rebalances.append(msg)

    def flush_summary(self) -> str:
        sections = [HEADER, *self.lines]
        sections.append("")
        sections.extend(self.rebalances if self.rebalances else [BALANCED])

        self.lines.clear()
        self.rebalances.clear()
        return format_message("\n".join(sections))

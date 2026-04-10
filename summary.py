from utils.helpers import format_message


class Summary:
    def __init__(self):
        self.lines = []
        self.rebalances = []

    def _set_header(self):
        self.lines.insert(0, "📊 *Portfolio Volatility Summary*\n")

    def add_summary(self, msg: str):
        self.lines.append(msg)

    def add_rebalance(self, msg: str):
        self.rebalances.append(msg)

    def flush_summary(self) -> str:
        self._set_header()
        all_msgs = "\n".join(
            self.lines + (
                [""] + self.rebalances
                if self.rebalances else ["\n✅ Portfolio is balanced."]
            )
        )
        self.lines.clear()
        self.rebalances.clear()

        return format_message(all_msgs)

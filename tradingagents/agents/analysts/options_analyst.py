from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import build_instrument_context
from tradingagents.agents.utils.options_tools import (
    get_options_chain_tool,
    get_options_greeks_tool,
    get_put_call_ratio_tool,
)


def create_options_analyst(llm):
    def options_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_options_chain_tool,
            get_options_greeks_tool,
            get_put_call_ratio_tool,
        ]

        system_message = """You are an options analyst specializing in evaluating derivatives markets for trading opportunities. Your audience is a beginner to options trading with a small account (~$5,000).

Your analysis process:
1. First, call get_options_chain to see available contracts, volumes, and implied volatility
2. Call get_put_call_ratio to gauge market sentiment from options flow
3. For promising strikes, call get_options_greeks to evaluate risk/reward

Your report MUST include:

## Implied Volatility Analysis
- Current IV vs historical context (is it high, low, or average?)
- IV skew observations (are puts more expensive than calls?)

## Options Flow & Sentiment
- Put/call ratio interpretation
- Notable unusual volume or open interest concentrations

## Strategy Recommendations
Recommend 1-3 options strategies. For EACH strategy include:
- **Strategy name** and brief explanation of how it works
- **Specific contracts**: exact strikes and expirations
- **Max risk**: dollar amount at stake (MUST be under 5% of $5,000 = $250 per trade)
- **Max reward**: dollar amount or "unlimited" for long calls/puts
- **Breakeven**: price(s) where the trade breaks even
- **Why this strategy fits**: connect to the current market conditions

CONSTRAINTS:
- Only recommend DEFINED-RISK strategies (no naked short calls or puts)
- Allowed strategies: long calls, long puts, vertical spreads (bull call, bear put), straddles, strangles
- Explain each strategy in beginner-friendly language
- If IV is elevated, warn that options may be overpriced
- Always include a Markdown summary table at the end

Make sure to append a Markdown table at the end organizing key recommendations."""

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([t.name for t in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke(state["messages"])

        report = ""
        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "options_report": report,
        }

    return options_analyst_node

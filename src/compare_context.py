"""Full context vs compact context: one question, run twice (component 4).

The conversation: the user describes a profile with income 5,001, gets a report,
then asks "למה לא קיבלתי הנחה בארנונה?". The same question is run once with the
full history and once with the compact context. Run:  python -m src.compare_context

Note: the decision component here is the mock. The comparison is therefore about
what information is available in the context, not about language-model quality.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .agent import build_agent
from .state import build_context, context_size

SETUP = "גיל 26, סטודנט, מועסק, הכנסה 5,001"
QUESTION = "למה לא קיבלתי הנחה בארנונה?"


def run(mode: str) -> Dict[str, Any]:
    agent = build_agent(context_mode=mode)
    agent.handle_message("c", SETUP)
    state = agent.store.get("c")
    state.add_message("user", QUESTION)  # context as the decision component sees it for the question
    size = context_size(build_context(state, mode))
    state.messages.pop()
    state.turn -= 1
    state.pending_user = False
    answer = agent.handle_message("c", QUESTION)["text"]
    correct = "5001" in answer and "5000" in answer  # the real reason: income 5001 against a limit of 5000
    return {"mode": mode, "messages": size["messages"], "chars": size["chars"], "answer": answer, "correct": correct}


def compare() -> List[Dict[str, Any]]:
    return [run("full"), run("compact")]


def main() -> None:
    rows = compare()
    print("| הקשר | הודעות | תווים | התשובה נכונה | התשובה |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {'מלא' if r['mode'] == 'full' else 'מצומצם'} | {r['messages']} | {r['chars']} | "
              f"{'כן' if r['correct'] else 'לא'} | {r['answer']} |")


if __name__ == "__main__":
    main()

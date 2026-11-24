"""Create the trace files of real runs (mock decision component, no key needed).

Run:  python -m src.make_traces      (writes to docs/traces/)
"""
from __future__ import annotations

from pathlib import Path

from .agent import Trace, build_agent
from .mocks import FlakyLoader

OUT = Path(__file__).resolve().parent.parent / "docs" / "traces"
MESSAGE = "גיל 26, סטודנט, מועסק, הכנסה 4,000"


def main() -> None:
    runs = {
        "01_normal_run.jsonl": (build_agent(), [MESSAGE]),
        "02_transient_failure_recovered.jsonl": (build_agent(loader=FlakyLoader(failures=2)), [MESSAGE]),
        "03_transient_failure_controlled_stop.jsonl": (build_agent(loader=FlakyLoader(failures=None)), [MESSAGE]),
    }
    for filename, (agent, messages) in runs.items():
        trace = Trace()
        for message in messages:
            result = agent.handle_message("demo", message, trace)
        trace.save(OUT / filename)
        print(f"{filename}: {len(trace.events)} events, final status = {result['status']}")


if __name__ == "__main__":
    main()

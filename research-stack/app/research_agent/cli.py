from __future__ import annotations

import asyncio
import sys

from .agent import ResearchAgent
from .config import settings


async def main(question: str) -> None:
    agent = ResearchAgent(settings)
    await agent.startup()
    try:
        report = await agent.run(question)
        print(report.answer_markdown)
        for w in report.warnings:
            print("Warning:", w, file=sys.stderr)
        if report.open_gaps:
            print("\nOpen gaps:", *report.open_gaps, sep="\n- ")
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "What is retrieval-augmented generation?"))

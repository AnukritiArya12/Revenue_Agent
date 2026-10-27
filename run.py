
import argparse
import sys

from agent.revenue_agent import DeepRevenueAgent


def main():
    parser = argparse.ArgumentParser(
        description="Deep Revenue Agent"
    )

    parser.add_argument(
        "--document",
        "--pdf",
        dest="document",
        default="TCS_FY26.pdf",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("DEEP REVENUE AGENT - OLLAMA + DEEPAGENTS")
    print("=" * 60)
    print(f"Document: {args.document}")

    try:
        agent = DeepRevenueAgent()
        result = agent.extract_revenue(args.document)

        print("\n" + "=" * 60)
        print("REVENUE EXTRACTION REPORT")
        print("=" * 60)

        print("Company Name       :", result.company_name or "N/A")
        print("Fiscal Year        :", result.fiscal_year or "N/A")
        print("Scope              :", result.scope.upper())
        print("Reported Revenue   :", result.revenue_from_operations or "N/A")
        print("Other Income       :", result.other_income or "N/A")
        print("Total Income       :", result.total_income or "N/A")
        print("Currency           :", result.currency or "N/A")
        print("Unit / Scale       :", result.unit_scale or "N/A")
        print("Source Pages       :", result.source_pages)
        print("Matched Line       :", result.matched_line or "N/A")
        print("Supporting Evidence:")
        print(result.supporting_evidence or "N/A")
        print("=" * 60)

    except Exception as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

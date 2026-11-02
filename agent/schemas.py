"""Pydantic schemas for Deep Revenue Agent output."""

from typing import Literal, Optional, List
from pydantic import BaseModel, Field


class RevenueDetails(BaseModel):
    """Structured revenue extracted from financial reports."""

    company_name: Optional[str] = Field(
        default=None,
        description="Company name as stated in the report"
    )

    fiscal_year: Optional[str] = Field(
        default=None,
        description="Reporting fiscal year or period"
    )

    scope: Literal["consolidated", "standalone", "unknown"] = Field(
        default="unknown",
        description="consolidated, standalone, or unknown"
    )

    revenue_from_operations: Optional[str] = Field(
        default=None,
        description="Exact reported revenue, net sales, or turnover"
    )

    other_income: Optional[str] = Field(
        default=None,
        description="Other non-operating income, if available"
    )

    total_income: Optional[str] = Field(
        default=None,
        description="Total income, if separately reported"
    )

    currency: Optional[str] = Field(
        default=None,
        description="Reporting currency, such as INR or USD"
    )

    unit_scale: Optional[str] = Field(
        default=None,
        description="Reported scale: thousand, million, crore, etc."
    )

    source_pages: List[int] = Field(
        default_factory=list,
        description="PDF pages containing supporting revenue evidence"
    )

    matched_line: Optional[str] = Field(
        default=None,
        description="Exact revenue line-item label"
    )

    supporting_evidence: Optional[str] = Field(
        default=None,
        description="Verbatim supporting excerpt from the report"
    )

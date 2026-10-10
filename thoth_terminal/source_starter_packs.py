from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import Source, SourceState


@dataclass(frozen=True)
class StarterSource:
    name: str
    label: str
    feed_url: str
    category: str
    priority: int = 2
    interval_minutes: int = 180


@dataclass(frozen=True)
class StarterPack:
    id: str
    name: str
    description: str
    sources: tuple[StarterSource, ...]


STARTER_PACKS: tuple[StarterPack, ...] = (
    StarterPack(
        id="ai-research",
        name="AI research",
        description="New papers from the two core public arXiv feeds for artificial intelligence and machine learning.",
        sources=(
            StarterSource(
                name="starter_arxiv_cs_ai",
                label="arXiv — Artificial Intelligence",
                feed_url="https://export.arxiv.org/rss/cs.AI",
                category="research",
                interval_minutes=360,
            ),
            StarterSource(
                name="starter_arxiv_cs_lg",
                label="arXiv — Machine Learning",
                feed_url="https://export.arxiv.org/rss/cs.LG",
                category="research",
                interval_minutes=360,
            ),
        ),
    ),
    StarterPack(
        id="ai-industry",
        name="AI industry",
        description="Public reporting and official company announcements for a compact industry pulse.",
        sources=(
            StarterSource(
                name="starter_techcrunch_ai",
                label="TechCrunch — Artificial Intelligence",
                feed_url="https://techcrunch.com/category/artificial-intelligence/feed/",
                category="news",
                interval_minutes=90,
            ),
            StarterSource(
                name="starter_nvidia_news",
                label="NVIDIA — Newsroom",
                feed_url="https://nvidianews.nvidia.com/cats/press_release.xml",
                category="company",
                interval_minutes=180,
            ),
            StarterSource(
                name="starter_amd_financial_news",
                label="AMD — Financial news",
                feed_url="https://ir.amd.com/news-events/press-releases/rss?category=financial",
                category="company",
                interval_minutes=180,
            ),
            StarterSource(
                name="starter_intel_financial_news",
                label="Intel — Financial news",
                feed_url="https://www.intc.com/news-events/press-releases/rss?category=financial",
                category="company",
                interval_minutes=180,
            ),
        ),
    ),
    StarterPack(
        id="semiconductor-filings",
        name="Semiconductor filings",
        description="Official filing feeds for NVIDIA, AMD, and Intel. Useful, quiet, and high-signal.",
        sources=(
            StarterSource(
                name="starter_nvidia_sec_filings",
                label="NVIDIA — SEC filings",
                feed_url="https://investor.nvidia.com/rss/SECFiling.aspx?Exchange=CIK&Symbol=0001045810",
                category="filings",
                priority=1,
                interval_minutes=120,
            ),
            StarterSource(
                name="starter_amd_sec_filings",
                label="AMD — SEC filings",
                feed_url="https://ir.amd.com/financial-information/sec-filings/rss",
                category="filings",
                priority=1,
                interval_minutes=120,
            ),
            StarterSource(
                name="starter_intel_sec_filings",
                label="Intel — SEC filings",
                feed_url="https://www.intc.com/filings-reports/all-sec-filings/rss",
                category="filings",
                priority=1,
                interval_minutes=120,
            ),
        ),
    ),
)


def starter_pack_payloads() -> list[dict[str, Any]]:
    return [
        {
            "id": pack.id,
            "name": pack.name,
            "description": pack.description,
            "sources": [
                {"name": source.label, "url": source.feed_url}
                for source in pack.sources
            ],
        }
        for pack in STARTER_PACKS
    ]


def install_starter_pack(session: Session, *, pack_id: str) -> tuple[list[Source], int]:
    pack = next((row for row in STARTER_PACKS if row.id == str(pack_id or "").strip()), None)
    if pack is None:
        raise ValueError("unknown starter pack")
    now = utcnow()
    installed: list[Source] = []
    created = 0
    for definition in pack.sources:
        source = session.query(Source).filter(Source.name == definition.name).first()
        if source is None:
            source = Source(
                name=definition.name,
                category=definition.category,
                priority=int(definition.priority),
                polling_interval_seconds=int(definition.interval_minutes) * 60,
                config={
                    "connector": "rss",
                    "feed_url": definition.feed_url,
                    "enabled": True,
                    "fetch_article": True,
                    "max_items_per_run": 25,
                    "download_article_images": False,
                    "store_raw_html_on_failure": False,
                    "default_domains": [],
                    "default_tags": ["starter-pack"],
                    "default_event_types": [],
                    "starter_pack": pack.id,
                    "rights_class": "public",
                },
                created_at=now,
            )
            session.add(source)
            session.flush()
            session.add(
                SourceState(
                    source_id=int(source.id),
                    last_run_at=None,
                    last_success_at=None,
                    next_run_at=now,
                    consecutive_failures=0,
                    backoff_until=None,
                    cursor={},
                    updated_at=now,
                )
            )
            created += 1
        installed.append(source)
    session.flush()
    return installed, created
